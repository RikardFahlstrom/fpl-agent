"""SQLite warehouse for FPL snapshots, actuals and reference data.

The FPL API serves current state only: prices, ownership, form and the price-change
projections are overwritten in place with no historical endpoint. Whatever is not
captured before a gameweek turns cannot be recovered, so this module exists to make
capture cheap and idempotent.

Shape of the schema: typed columns for the fields the model reads, plus a `raw` JSON
column holding the untouched payload. A new FPL field (defensive_contribution was added
this season) then costs nothing until something wants to model on it.

Writers take plain dicts straight from the API so they are testable without network.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DB_PATH = Path("data/fpl.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    gameweek    INTEGER,
    kind        TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE IF NOT EXISTS team (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    short_name TEXT NOT NULL,
    raw        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player (
    element_id   INTEGER PRIMARY KEY,
    web_name     TEXT NOT NULL,
    first_name   TEXT,
    second_name  TEXT,
    team_id      INTEGER,
    element_type INTEGER,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_snapshot (
    snapshot_id                  INTEGER NOT NULL REFERENCES snapshot(id),
    element_id                   INTEGER NOT NULL REFERENCES player(element_id),
    now_cost                     INTEGER,
    form                         REAL,
    points_per_game              REAL,
    total_points                 INTEGER,
    minutes                      INTEGER,
    status                       TEXT,
    chance_of_playing_next_round INTEGER,
    selected_by_percent          REAL,
    expected_goals_per_90        REAL,
    expected_assists_per_90      REAL,
    expected_goals_conceded_per_90 REAL,
    starts_per_90                REAL,
    penalties_order              INTEGER,
    price_change_percent         REAL,
    price_change_locked_until    TEXT,
    price_change_projections     TEXT,
    transfers_in_event           INTEGER,
    transfers_out_event          INTEGER,
    news                         TEXT,
    raw                          TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, element_id)
);

CREATE TABLE IF NOT EXISTS fixture (
    id                INTEGER PRIMARY KEY,
    event             INTEGER,
    team_h            INTEGER,
    team_a            INTEGER,
    team_h_difficulty INTEGER,
    team_a_difficulty INTEGER,
    team_h_score      INTEGER,
    team_a_score      INTEGER,
    kickoff_time      TEXT,
    finished          INTEGER,
    raw               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_gameweek (
    element_id                 INTEGER NOT NULL REFERENCES player(element_id),
    round                      INTEGER NOT NULL,
    fixture_id                 INTEGER,
    opponent_team              INTEGER,
    was_home                   INTEGER,
    minutes                    INTEGER,
    total_points               INTEGER,
    goals_scored               INTEGER,
    assists                    INTEGER,
    clean_sheets               INTEGER,
    goals_conceded             INTEGER,
    bonus                      INTEGER,
    bps                        INTEGER,
    starts                     INTEGER,
    expected_goals             REAL,
    expected_assists           REAL,
    expected_goal_involvements REAL,
    expected_goals_conceded    REAL,
    defensive_contribution     INTEGER,
    value                      INTEGER,
    selected                   INTEGER,
    transfers_balance          INTEGER,
    raw                        TEXT NOT NULL,
    PRIMARY KEY (element_id, round)
);

CREATE TABLE IF NOT EXISTS game_config (
    captured_at TEXT PRIMARY KEY,
    scoring     TEXT NOT NULL,
    rules       TEXT NOT NULL,
    settings    TEXT NOT NULL,
    chips       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS my_squad (
    snapshot_id    INTEGER NOT NULL REFERENCES snapshot(id),
    element_id     INTEGER NOT NULL,
    position       INTEGER,
    multiplier     INTEGER,
    is_captain     INTEGER,
    is_vice_captain INTEGER,
    selling_price  INTEGER,
    purchase_price INTEGER,
    PRIMARY KEY (snapshot_id, element_id)
);

CREATE TABLE IF NOT EXISTS my_state (
    snapshot_id     INTEGER PRIMARY KEY REFERENCES snapshot(id),
    entry_id        INTEGER,
    bank            INTEGER,
    squad_value     INTEGER,
    free_transfers  INTEGER,
    transfer_cost   INTEGER,
    chips           TEXT
);

CREATE TABLE IF NOT EXISTS projection (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshot(id),
    gameweek        INTEGER NOT NULL,
    element_id      INTEGER NOT NULL REFERENCES player(element_id),
    model_version   TEXT NOT NULL,
    expected_points REAL NOT NULL,
    p_start         REAL,
    expected_minutes REAL,
    fixture_count   INTEGER,
    components      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (snapshot_id, gameweek, element_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_projection_gw ON projection(gameweek, model_version);
CREATE INDEX IF NOT EXISTS idx_player_snapshot_element ON player_snapshot(element_id);
CREATE INDEX IF NOT EXISTS idx_player_gameweek_round   ON player_gameweek(round);
CREATE INDEX IF NOT EXISTS idx_fixture_event           ON fixture(event);
CREATE INDEX IF NOT EXISTS idx_snapshot_captured       ON snapshot(captured_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(value: Any) -> Optional[float]:
    """FPL sends most numerics as strings; empty and null both mean absent."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> Optional[int]:
    number = _f(value)
    return None if number is None else int(number)


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the warehouse, creating the file and schema if absent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def target_gameweek(bootstrap: dict) -> Optional[int]:
    """The gameweek decisions are being made for: the next one, else the current one."""
    events = bootstrap.get("events") or []
    for event in events:
        if event.get("is_next"):
            return event["id"]
    for event in events:
        if event.get("is_current"):
            return event["id"]
    for event in events:
        if not event.get("finished"):
            return event["id"]
    return None


def create_snapshot(conn: sqlite3.Connection, bootstrap: dict, kind: str = "manual") -> int:
    cur = conn.execute(
        "INSERT INTO snapshot (captured_at, gameweek, kind) VALUES (?, ?, ?)",
        (_now(), target_gameweek(bootstrap), kind),
    )
    return cur.lastrowid


def snapshot_taken_today(conn: sqlite3.Connection) -> bool:
    """Whether a snapshot already exists for today (UTC), so capture stays idempotent."""
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT 1 FROM snapshot WHERE substr(captured_at, 1, 10) = ? LIMIT 1", (today,)
    ).fetchone()
    return row is not None


def upsert_teams(conn: sqlite3.Connection, bootstrap: dict) -> int:
    rows = [
        (t["id"], t["name"], t["short_name"], json.dumps(t, sort_keys=True))
        for t in bootstrap.get("teams") or []
    ]
    conn.executemany(
        """INSERT INTO team (id, name, short_name, raw) VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               name = excluded.name, short_name = excluded.short_name, raw = excluded.raw""",
        rows,
    )
    return len(rows)


def upsert_players(conn: sqlite3.Connection, bootstrap: dict) -> int:
    now = _now()
    rows = [
        (e["id"], e["web_name"], e.get("first_name"), e.get("second_name"),
         e.get("team"), e.get("element_type"), now, now)
        for e in bootstrap.get("elements") or []
    ]
    conn.executemany(
        """INSERT INTO player (element_id, web_name, first_name, second_name,
                               team_id, element_type, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(element_id) DO UPDATE SET
               web_name = excluded.web_name, team_id = excluded.team_id,
               element_type = excluded.element_type, last_seen = excluded.last_seen""",
        rows,
    )
    return len(rows)


def record_player_snapshot(conn: sqlite3.Connection, snapshot_id: int, bootstrap: dict) -> int:
    rows = []
    for e in bootstrap.get("elements") or []:
        rows.append((
            snapshot_id, e["id"], _i(e.get("now_cost")), _f(e.get("form")),
            _f(e.get("points_per_game")), _i(e.get("total_points")), _i(e.get("minutes")),
            e.get("status"), _i(e.get("chance_of_playing_next_round")),
            _f(e.get("selected_by_percent")), _f(e.get("expected_goals_per_90")),
            _f(e.get("expected_assists_per_90")), _f(e.get("expected_goals_conceded_per_90")),
            _f(e.get("starts_per_90")), _i(e.get("penalties_order")),
            _f(e.get("price_change_percent")), e.get("price_change_locked_until"),
            json.dumps(e.get("price_change_projections") or [], sort_keys=True),
            _i(e.get("transfers_in_event")), _i(e.get("transfers_out_event")),
            e.get("news"), json.dumps(e, sort_keys=True),
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO player_snapshot (
               snapshot_id, element_id, now_cost, form, points_per_game, total_points,
               minutes, status, chance_of_playing_next_round, selected_by_percent,
               expected_goals_per_90, expected_assists_per_90,
               expected_goals_conceded_per_90, starts_per_90, penalties_order,
               price_change_percent, price_change_locked_until, price_change_projections,
               transfers_in_event, transfers_out_event, news, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def record_game_config(conn: sqlite3.Connection, bootstrap: dict) -> None:
    """Scoring weights and rules, captured so past decisions stay explainable.

    Keyed by capture time: FPL has changed scoring mid-season before, and a projection
    made under old weights should still be reproducible.
    """
    config = bootstrap.get("game_config") or {}
    conn.execute(
        """INSERT OR REPLACE INTO game_config (captured_at, scoring, rules, settings, chips)
           VALUES (?, ?, ?, ?, ?)""",
        (
            _now(),
            json.dumps(config.get("scoring") or {}, sort_keys=True),
            json.dumps(config.get("rules") or {}, sort_keys=True),
            json.dumps(bootstrap.get("game_settings") or {}, sort_keys=True),
            json.dumps(bootstrap.get("chips") or [], sort_keys=True),
        ),
    )


def upsert_fixtures(conn: sqlite3.Connection, fixtures: Iterable[dict]) -> int:
    rows = [
        (f["id"], f.get("event"), f.get("team_h"), f.get("team_a"),
         f.get("team_h_difficulty"), f.get("team_a_difficulty"),
         f.get("team_h_score"), f.get("team_a_score"), f.get("kickoff_time"),
         1 if f.get("finished") else 0, json.dumps(f, sort_keys=True))
        for f in fixtures
    ]
    conn.executemany("INSERT OR REPLACE INTO fixture VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def record_player_gameweeks(conn: sqlite3.Connection, history: Iterable[dict]) -> int:
    """Store per-gameweek actuals from element-summary. These are the calibration targets."""
    rows = []
    for h in history:
        rows.append((
            h["element"], h["round"], h.get("fixture"), h.get("opponent_team"),
            1 if h.get("was_home") else 0, _i(h.get("minutes")), _i(h.get("total_points")),
            _i(h.get("goals_scored")), _i(h.get("assists")), _i(h.get("clean_sheets")),
            _i(h.get("goals_conceded")), _i(h.get("bonus")), _i(h.get("bps")),
            _i(h.get("starts")), _f(h.get("expected_goals")), _f(h.get("expected_assists")),
            _f(h.get("expected_goal_involvements")), _f(h.get("expected_goals_conceded")),
            _i(h.get("defensive_contribution")), _i(h.get("value")), _i(h.get("selected")),
            _i(h.get("transfers_balance")), json.dumps(h, sort_keys=True),
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO player_gameweek VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def record_my_team(conn: sqlite3.Connection, snapshot_id: int, entry_id: Optional[int],
                   my_team: dict) -> int:
    """Store the authenticated squad: selling prices and free transfers exist nowhere else."""
    picks = my_team.get("picks") or []
    conn.executemany(
        "INSERT OR REPLACE INTO my_squad VALUES (?,?,?,?,?,?,?,?)",
        [(snapshot_id, p["element"], p.get("position"), p.get("multiplier"),
          1 if p.get("is_captain") else 0, 1 if p.get("is_vice_captain") else 0,
          _i(p.get("selling_price")), _i(p.get("purchase_price"))) for p in picks],
    )

    transfers = my_team.get("transfers") or {}
    limit, made = _i(transfers.get("limit")), _i(transfers.get("made"))
    free = None if limit is None else max(0, limit - (made or 0))
    conn.execute(
        "INSERT OR REPLACE INTO my_state VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, entry_id, _i(transfers.get("bank")), _i(transfers.get("value")),
         free, _i(transfers.get("cost")),
         json.dumps(my_team.get("chips") or [], sort_keys=True)),
    )
    return len(picks)

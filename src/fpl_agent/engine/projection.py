"""Expected-points projections.

Every scoring weight is read from the captured `game_config`; nothing is hardcoded.
The one exception is the defensive-contribution threshold, which FPL does not publish
(see scoring.DC_THRESHOLDS for how it was derived).

Each projection stores its components, so a wrong number can be traced to the term that
produced it rather than re-derived. That is what makes P4's calibration actionable:
"forwards over-projected" is only useful if you can see which term was hot.

This is v0 and deliberately uncalibrated - the rate constants below are stated
assumptions, not fitted values. Fitting them is the point of the learning loop.

    fpl-agent project              # project the upcoming gameweek
    fpl-agent project --gameweek 5
"""

import argparse
import json
import logging
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from . import lineups, storage
from .scoring import DC_THRESHOLDS, POSITIONS, Scoring

logger = logging.getLogger("fpl_projection")

MODEL_VERSION = "0.5.0"

# Transfer value is judged over three gameweeks, so a good fixture run counts and a
# single-week spike does not dominate the decision.
HORIZON_GAMEWEEKS = 3

# --- stated assumptions, to be replaced by fitted values in P5 -----------------------
# Minutes a player gets when they start, and when they appear off the bench.
START_MINUTES = 82.0
CAMEO_MINUTES = 18.0
# Chance an unflagged player with no history yet keeps starting. Applies at the start of
# a season, when nobody has appearances.
BASE_START_PROB = 0.85
# Once gameweeks have been played, a fit player with no appearance at all is not first
# choice: absence of appearances is evidence, not absence of evidence. Without this a
# reserve goalkeeper reads as an 85% starter and ranks among the best value in the game.
UNUSED_START_PROB = 0.10
# From a published lineup. A prediction is not certainty - RotoWire is guessing at the
# manager - so a predicted starter is not 1.0, and a confirmed one still leaves late
# withdrawals. A player at a club with a published lineup who is not in it is the
# rotation case that FPL's own flag never reports.
LINEUP_STARTER_PROB = 0.90
LINEUP_CONFIRMED_STARTER_PROB = 0.97
LINEUP_OMITTED_PROB = 0.15
# Chance a fit player who does not start still appears off the bench. Applies only to
# available players: someone ruled out does not get a cameo.
BENCH_CAMEO_PROB = 0.35
# Fixture difficulty is 1-5 with 3 as neutral; this scales expected goals conceded.
NEUTRAL_DIFFICULTY = 3.0
DIFFICULTY_SENSITIVITY = 0.25
# Bonus is modelled from realised bonus per appearance, damped toward zero early in the
# season when the sample is tiny.
BONUS_PRIOR_APPEARANCES = 3.0
# Minutes of evidence required before a per-90 rate is taken at face value. Below this a
# rate is pulled toward its positional prior, in proportion to how thin the sample is.
# Without this, 2 goals in 63 minutes reads as an xG90 of 2.0, and a player with no
# minutes at all reports every per-90 rate as a confident 0.0 - which for expected goals
# conceded means a *certain* clean sheet.
RATE_PRIOR_MINUTES = 270.0
# Fallback when a team has no played minutes to estimate from (preseason).
LEAGUE_CONCEDED_PER_90 = 1.4
# Appearances of evidence before a per-appearance rate (bonus, defensive contribution,
# starting) is trusted. One player hitting the threshold in his only appearance is not a
# 100% rate, and one start in his club's two games is not a nailed-on starter.
APPEARANCE_PRIOR = 3.0
# Fallback start rate when there are no rows to compute one from (preseason). The prior
# actually used is starts/games for the player's own position, derived from the data in
# _player_history the way league_yellow_per_90 and league_dc_rate are - a goalkeeper and
# a centre-back are not drawn from the same distribution.
LEAGUE_START_PRIOR = 0.35


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def shrink(rate: float, minutes: float, prior: float,
           prior_minutes: float = RATE_PRIOR_MINUTES) -> float:
    """Pull a per-90 rate toward a prior in proportion to how little evidence supports it.

    A player with no minutes gets the prior outright; one with a full season keeps
    essentially their own rate.
    """
    if minutes <= 0:
        return prior
    return (rate * minutes + prior * prior_minutes) / (minutes + prior_minutes)


def positional_priors(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, dict[str, float]]:
    """Median per-90 attacking rates by position, over players with real minutes."""
    priors: dict[str, dict[str, float]] = {}
    for position_id, position in POSITIONS.items():
        rows = conn.execute(
            """SELECT expected_goals_per_90 AS xg, expected_assists_per_90 AS xa
               FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
               WHERE ps.snapshot_id = ? AND p.element_type = ? AND ps.minutes >= 180""",
            (snapshot_id, position_id),
        ).fetchall()
        goals = sorted(_f(r["xg"]) for r in rows)
        assists = sorted(_f(r["xa"]) for r in rows)
        priors[position] = {
            "xg90": goals[len(goals) // 2] if goals else 0.0,
            "xa90": assists[len(assists) // 2] if assists else 0.0,
        }
    return priors


def team_conceded_rates(conn: sqlite3.Connection, snapshot_id: int) -> dict[int, float]:
    """Expected goals conceded per 90, per team.

    Conceding is a team property, so it is estimated from the team's players who have
    actually played rather than from the individual - who may have no minutes at all.
    """
    rates: dict[int, float] = {}
    for row in conn.execute(
        """SELECT p.team_id,
                  SUM(ps.expected_goals_conceded_per_90 * ps.minutes) AS weighted,
                  SUM(ps.minutes) AS minutes
           FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
           WHERE ps.snapshot_id = ? AND ps.minutes > 0
           GROUP BY p.team_id""",
        (snapshot_id,),
    ):
        minutes = _f(row["minutes"])
        rates[row["team_id"]] = (_f(row["weighted"]) / minutes) if minutes else LEAGUE_CONCEDED_PER_90
    return rates


def availability(snap: sqlite3.Row) -> float:
    """Chance the player is fit and eligible at all.

    Kept separate from the start rate because they are different questions: a fit
    substitute has a real chance of a cameo, a suspended one has none. Conflating them
    hands injured players bench-appearance points and card penalties they cannot earn.
    """
    chance = snap["chance_of_playing_next_round"]
    if chance is not None:
        return max(0.0, min(1.0, chance / 100.0))
    if snap["status"] and snap["status"] != "a":
        return 0.0
    return 1.0


def start_rate(history: dict[str, float], season_started: bool = False) -> float:
    """How often this player starts, given they are available.

    The denominator is `games` - every fixture of his club's that he was in the squad
    for, benchings included - not `appearances`, which counts only games he played. On
    appearances an unused substitute is invisible: one start and one benching read as a
    certain starter, four times what the record supports. Benchings are already in the
    warehouse (element-summary carries a row per team fixture; 614 of 1236 rows were at
    zero minutes when this was written) and were being grouped away.

    Thin records are pulled toward the start rate of the player's position, so two starts
    out of two is not yet proof of a nailed-on starter. That shrink is skipped in one
    direction: a fit player who has started nothing once the season is under way keeps
    UNUSED_START_PROB outright. Shrinking him *up* toward a positional prior of roughly
    0.3 would quietly restore the 0.2.0 reserve-goalkeeper bug - never being picked is
    evidence, not thin evidence.

    `season_started` distinguishes the two reasons a player has no games at all: the
    season has not begun, or he has been available and not picked.
    """
    games = history.get("games", 0.0)
    if not games:
        return UNUSED_START_PROB if season_started else BASE_START_PROB
    starts = history.get("starts", 0.0)
    if not starts and season_started:
        return UNUSED_START_PROB
    prior = history.get("start_prior", LEAGUE_START_PRIOR)
    rate = (starts + prior * APPEARANCE_PRIOR) / (games + APPEARANCE_PRIOR)
    return max(0.0, min(1.0, rate))


def clean_sheet_probability(expected_conceded: float) -> float:
    """Poisson P(team concedes zero)."""
    return math.exp(-max(0.0, expected_conceded))


def project_player(snap: sqlite3.Row, position: str, fixtures: list[dict],
                   history: dict[str, float], scoring: Scoring,
                   priors: dict[str, float], team_conceded: float,
                   season_started: bool = False,
                   lineup_rate: Optional[float] = None) -> dict[str, Any]:
    """Expected points for one player over the given fixtures (0 for a blank, 2 for a double)."""
    available = availability(snap)
    # A published lineup speaks to selection; FPL's flag speaks to fitness. They answer
    # different questions, so the lineup replaces the start rate rather than availability.
    rate = start_rate(history, season_started) if lineup_rate is None else lineup_rate
    p_start = available * rate
    p_appear = available * (rate + (1 - rate) * BENCH_CAMEO_PROB)
    expected_minutes = p_start * START_MINUTES + (p_appear - p_start) * CAMEO_MINUTES
    minutes_share = expected_minutes / 90.0

    played = _f(snap["minutes"])
    xg90 = shrink(_f(snap["expected_goals_per_90"]), played, priors.get("xg90", 0.0))
    xa90 = shrink(_f(snap["expected_assists_per_90"]), played, priors.get("xa90", 0.0))
    xgc90 = team_conceded

    components = {"appearance": 0.0, "goals": 0.0, "assists": 0.0, "clean_sheet": 0.0,
                  "goals_conceded": 0.0, "defensive_contribution": 0.0,
                  "bonus": 0.0, "cards": 0.0}

    for fixture in fixtures:
        difficulty = fixture["difficulty"]
        # Harder fixture -> more goals conceded, fewer scored.
        defensive_scale = 1 + (difficulty - NEUTRAL_DIFFICULTY) * DIFFICULTY_SENSITIVITY
        attacking_scale = 1 - (difficulty - NEUTRAL_DIFFICULTY) * DIFFICULTY_SENSITIVITY

        components["appearance"] += (
            p_appear * scoring.short_play
            + p_start * (scoring.long_play - scoring.short_play)
        )
        components["goals"] += xg90 * minutes_share * attacking_scale * scoring.goal(position)
        components["assists"] += xa90 * minutes_share * attacking_scale * scoring.assist(position)

        expected_conceded = xgc90 * defensive_scale
        p_cs = clean_sheet_probability(expected_conceded) * p_start
        components["clean_sheet"] += p_cs * scoring.clean_sheet(position)
        components["goals_conceded"] += (
            (expected_conceded / 2.0) * p_start * scoring.goal_conceded(position)
        )

        if DC_THRESHOLDS.get(position) is not None:
            components["defensive_contribution"] += (
                history.get("dc_rate", 0.0) * p_start * scoring.defensive_contribution(position)
            )

        appearances = history.get("appearances", 0.0)
        bonus_rate = history.get("bonus", 0.0) / (appearances + BONUS_PRIOR_APPEARANCES)
        components["bonus"] += bonus_rate * p_appear

        cards_rate = history.get("yellow_per_90", 0.0)
        components["cards"] += cards_rate * minutes_share * _f(scoring.w.get("yellow_cards"))

    expected_points = sum(components.values())
    return {
        "expected_points": round(expected_points, 3),
        "p_start": round(p_start, 3),
        "expected_minutes": round(expected_minutes, 1),
        "fixture_count": len(fixtures),
        "components": {k: round(v, 3) for k, v in components.items()},
    }


def _player_history(conn: sqlite3.Connection) -> dict[int, dict[str, float]]:
    """Per-player realised rates, used where the season aggregates do not carry them.

    Two denominators, and the difference matters. `appearances` counts games actually
    played, because bonus, cards and defensive contribution can only be earned on the
    pitch. `games` counts every row, played or not, because being left out is precisely
    the evidence a start rate needs; see start_rate.
    """
    history: dict[int, dict[str, float]] = {}
    position_of: dict[int, str] = {}
    # Every row, minutes or not. A start implies minutes, so the numerator is the same
    # either way; it is the denominator that was wrong.
    games_query = """
        SELECT pg.element_id, p.element_type, COUNT(*) AS games,
               SUM(pg.starts) AS starts
        FROM player_gameweek pg JOIN player p ON p.element_id = pg.element_id
        GROUP BY pg.element_id
    """
    position_totals: dict[str, list[float]] = {}
    for row in conn.execute(games_query):
        position = POSITIONS.get(row["element_type"], "MID")
        position_of[row["element_id"]] = position
        games = float(row["games"] or 0)
        starts = float(row["starts"] or 0)
        history[row["element_id"]] = {
            "games": games, "starts": starts,
            "appearances": 0.0, "bonus": 0.0, "minutes": 0.0,
        }
        totals = position_totals.setdefault(position, [0.0, 0.0])
        totals[0] += starts
        totals[1] += games

    # Played games only: these three are per-appearance rates.
    query = """
        SELECT pg.element_id, COUNT(*) AS appearances, SUM(pg.bonus) AS bonus,
               SUM(pg.minutes) AS minutes
        FROM player_gameweek pg JOIN player p ON p.element_id = pg.element_id
        WHERE pg.minutes > 0 GROUP BY pg.element_id
    """
    for row in conn.execute(query):
        entry = history[row["element_id"]]
        entry["appearances"] = float(row["appearances"] or 0)
        entry["bonus"] = float(row["bonus"] or 0)
        entry["minutes"] = float(row["minutes"] or 0)

    # Rates that need per-row inspection rather than a SUM.
    detail = """
        SELECT pg.element_id, p.element_type, pg.defensive_contribution, pg.raw, pg.minutes
        FROM player_gameweek pg JOIN player p ON p.element_id = pg.element_id
        WHERE pg.minutes > 0
    """
    hits: dict[int, list[int]] = {}
    yellows: dict[int, float] = {}
    for row in conn.execute(detail):
        position = POSITIONS.get(row["element_type"], "MID")
        threshold = DC_THRESHOLDS.get(position)
        if threshold is not None:
            hits.setdefault(row["element_id"], []).append(
                1 if (row["defensive_contribution"] or 0) >= threshold else 0
            )
        yellows[row["element_id"]] = yellows.get(row["element_id"], 0.0) + _f(
            json.loads(row["raw"]).get("yellow_cards")
        )

    # League-wide rates, derived from the data rather than assumed, used as the priors
    # that thin samples are pulled toward.
    total_minutes = sum(e.get("minutes", 0.0) for e in history.values())
    league_yellow_per_90 = (
        (sum(yellows.values()) / total_minutes * 90) if total_minutes else 0.0
    )
    all_flags = [flag for flags in hits.values() for flag in flags]
    league_dc_rate = (sum(all_flags) / len(all_flags)) if all_flags else 0.0
    # Starts per game by position: goalkeepers rotate far less than forwards, so one
    # league-wide number would flatter the reserve keeper and punish the rotated striker.
    league_start_rates = {
        position: (starts / games) if games else LEAGUE_START_PRIOR
        for position, (starts, games) in position_totals.items()
    }

    for element_id, entry in history.items():
        minutes = entry.get("minutes", 0.0)
        appearances = entry.get("appearances", 0.0)
        entry["start_prior"] = league_start_rates.get(
            position_of.get(element_id, ""), LEAGUE_START_PRIOR)
        if not appearances:
            # Bench-only: no time on the pitch, so no per-appearance evidence at all.
            # He is in `history` solely for the start rate, and holding these at zero
            # leaves him exactly as he was when he was absent from the dict entirely.
            entry["yellow_per_90"] = 0.0
            entry["dc_rate"] = 0.0
            continue

        # A yellow card in a 5-minute cameo is not an 18-per-90 booking rate.
        raw_yellow = (yellows.get(element_id, 0.0) / minutes * 90) if minutes else 0.0
        entry["yellow_per_90"] = shrink(raw_yellow, minutes, league_yellow_per_90)

        flags = hits.get(element_id, [])
        raw_dc = (sum(flags) / len(flags)) if flags else league_dc_rate
        entry["dc_rate"] = (
            (raw_dc * appearances + league_dc_rate * APPEARANCE_PRIOR)
            / (appearances + APPEARANCE_PRIOR)
        )
    return history


def _fixtures_by_team(conn: sqlite3.Connection, gameweek: int) -> dict[int, list[dict]]:
    """Each team's fixtures in the gameweek: empty for a blank, two for a double."""
    by_team: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT team_h, team_a, team_h_difficulty, team_a_difficulty FROM fixture "
        "WHERE event = ? AND finished = 0", (gameweek,)
    ):
        by_team.setdefault(row["team_h"], []).append(
            {"difficulty": row["team_h_difficulty"] or NEUTRAL_DIFFICULTY, "home": True})
        by_team.setdefault(row["team_a"], []).append(
            {"difficulty": row["team_a_difficulty"] or NEUTRAL_DIFFICULTY, "home": False})
    return by_team


class SettledProjection(RuntimeError):
    """Raised when re-projecting would overwrite a projection that has been graded."""


def graded_projections(conn: sqlite3.Connection, snapshot_id: int, gameweek: int,
                       model_version: str) -> int:
    """How many of these projections have already been graded against actuals.

    The rows a re-projection would replace, not every outcome for the gameweek: a
    *new* snapshot projecting a played gameweek writes new rows and mutates nothing,
    so it is not this rule's business.
    """
    return conn.execute(
        """SELECT COUNT(*) FROM outcome o JOIN projection pr ON pr.id = o.projection_id
           WHERE pr.snapshot_id = ? AND pr.gameweek = ? AND pr.model_version = ?""",
        (snapshot_id, gameweek, model_version),
    ).fetchone()[0]


def project_gameweek(conn: sqlite3.Connection, gameweek: Optional[int] = None,
                     model_version: str = MODEL_VERSION) -> int:
    """Project every player for a gameweek from the most recent snapshot.

    Refuses to overwrite a projection that settle has already graded. `INSERT OR
    REPLACE` deletes the old row and inserts a new one with a new autoincrement id,
    so the `outcome` row grading it would be orphaned - which the foreign key already
    caught, as an opaque IntegrityError. The refusal is the same one, said out loud:
    a graded projection is the record of what the model believed *at decision time*,
    and rewriting it under today's code turns the learning loop into a tautology.
    """
    snapshot = conn.execute(
        "SELECT id, gameweek FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not snapshot:
        raise LookupError("no snapshot captured yet; run `fpl-agent snapshot`")
    gameweek = gameweek or snapshot["gameweek"]
    if gameweek is None:
        raise LookupError("no target gameweek; the season may be over")

    graded = graded_projections(conn, snapshot["id"], gameweek, model_version)
    if graded:
        raise SettledProjection(
            f"gameweek {gameweek} has been settled: {graded} of snapshot "
            f"{snapshot['id']}'s projections under model {model_version} are already "
            f"graded against actuals. Re-projecting would rewrite what the model "
            f"believed before the gameweek was played, and the calibration would then "
            f"be scoring today's code against a result it can see. Bump MODEL_VERSION "
            f"to project the same gameweek again, or take a new snapshot")

    scoring = Scoring.from_db(conn)
    history = _player_history(conn)
    fixtures = _fixtures_by_team(conn, gameweek)
    priors = positional_priors(conn, snapshot["id"])
    team_conceded = team_conceded_rates(conn, snapshot["id"])
    played = conn.execute("SELECT MAX(round) AS r FROM player_gameweek").fetchone()["r"]
    season_started = bool(played)
    # Lineups are published for the next round only, so later horizon gameweeks fall
    # back to historical start rates.
    lineup_rates = lineups.lineup_start_rates(
        conn, gameweek, LINEUP_STARTER_PROB, LINEUP_OMITTED_PROB,
        LINEUP_CONFIRMED_STARTER_PROB)
    if lineup_rates:
        logger.info("gameweek %s: using published lineups for %s players",
                    gameweek, len(lineup_rates))
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    query = """
        SELECT ps.*, p.element_type, p.team_id
        FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
        WHERE ps.snapshot_id = ?
    """
    for snap in conn.execute(query, (snapshot["id"],)):
        position = POSITIONS.get(snap["element_type"], "MID")
        result = project_player(
            snap, position, fixtures.get(snap["team_id"], []),
            history.get(snap["element_id"], {}), scoring,
            priors.get(position, {}),
            team_conceded.get(snap["team_id"], LEAGUE_CONCEDED_PER_90),
            season_started,
            lineup_rates.get(snap["element_id"]),
        )
        rows.append((snapshot["id"], gameweek, snap["element_id"], model_version,
                     result["expected_points"], result["p_start"],
                     result["expected_minutes"], result["fixture_count"],
                     json.dumps(result["components"], sort_keys=True), now))

    conn.executemany(
        """INSERT OR REPLACE INTO projection
           (snapshot_id, gameweek, element_id, model_version, expected_points,
            p_start, expected_minutes, fixture_count, components, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    logger.info("projected %s players for gameweek %s (model %s)",
                len(rows), gameweek, model_version)
    return len(rows)


class HorizonMissing(LookupError):
    """Raised when a caller asks for a horizon that was never projected."""


def stored_horizon(conn: sqlite3.Connection, snapshot_id: int, start_gameweek: int,
                   weeks: int = HORIZON_GAMEWEEKS,
                   model_version: str = MODEL_VERSION) -> dict[int, float]:
    """Per-player totals over the horizon, read from projections already stored.

    Reading rather than projecting is the point. A consumer that projects on the way
    past writes rows nobody asked for, under whatever MODEL_VERSION the code is on
    today, so `projection` stops being the record of what was believed at decision
    time and "projections for gameweek N under model X" stops being a countable fact.
    `make deadline` runs `project` before `recommend` for exactly this reason.

    Every gameweek in the range must be present. A missing one is not a blank - a
    blank gameweek still stores a row per player, with `fixture_count` 0 - it is a
    horizon that was never run, and silently totalling two weeks of a three-week
    horizon would understate every player by a week.
    """
    projected = {
        row["gameweek"] for row in conn.execute(
            """SELECT DISTINCT gameweek FROM projection
               WHERE snapshot_id = ? AND model_version = ?
                 AND gameweek BETWEEN ? AND ?""",
            (snapshot_id, model_version, start_gameweek, start_gameweek + weeks - 1),
        )
    }
    missing = [gw for gw in range(start_gameweek, start_gameweek + weeks)
               if gw not in projected]
    if missing:
        raise HorizonMissing(
            f"no projections stored for gameweek{'s' if len(missing) > 1 else ''} "
            f"{', '.join(str(gw) for gw in missing)} on snapshot {snapshot_id} under "
            f"model {model_version}; run `fpl-agent project --horizon {weeks}` "
            f"(or `make deadline`, which projects before it recommends) first")

    totals: dict[int, float] = {}
    for row in conn.execute(
        """SELECT element_id, SUM(expected_points) AS total FROM projection
           WHERE model_version = ? AND gameweek BETWEEN ? AND ?
             AND snapshot_id = ? GROUP BY element_id""",
        (model_version, start_gameweek, start_gameweek + weeks - 1, snapshot_id),
    ):
        totals[row["element_id"]] = row["total"]
    return totals


def project_horizon(conn: sqlite3.Connection, start_gameweek: Optional[int] = None,
                    weeks: int = HORIZON_GAMEWEEKS,
                    model_version: str = MODEL_VERSION) -> dict[int, float]:
    """Project each gameweek in the horizon and return the per-player totals.

    Blanks contribute nothing and doubles contribute twice, which is the point of
    summing over fixtures rather than gameweeks.

    This writes. Callers that only need the numbers read `stored_horizon` instead.
    """
    snapshot = conn.execute(
        "SELECT id, gameweek FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not snapshot:
        raise LookupError("no snapshot captured yet; run `fpl-agent snapshot`")
    start = start_gameweek or snapshot["gameweek"]
    if start is None:
        raise LookupError("no target gameweek; the season may be over")

    for gameweek in range(start, start + weeks):
        project_gameweek(conn, gameweek, model_version)

    logger.info("horizon: gameweeks %s-%s", start, start + weeks - 1)
    return stored_horizon(conn, snapshot["id"], start, weeks, model_version)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Project expected points for a gameweek.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int, default=None)
    parser.add_argument("--top", type=int, default=15, help="how many to print")
    parser.add_argument("--horizon", type=int, default=None,
                        help=f"project this many gameweeks and total them "
                             f"(default {HORIZON_GAMEWEEKS} via the recommender)")
    args = parser.parse_args(argv)

    config.load()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    conn = storage.connect(args.db)
    try:
        if args.horizon:
            project_horizon(conn, args.gameweek, weeks=args.horizon)
        else:
            project_gameweek(conn, args.gameweek)
        # One row per player over the whole horizon, not one per gameweek.
        rows = conn.execute(
            """SELECT p.web_name, t.short_name AS team, ps.now_cost,
                      SUM(pr.expected_points) AS xp, AVG(pr.p_start) AS p_start,
                      SUM(pr.fixture_count) AS fixtures,
                      COUNT(DISTINCT pr.gameweek) AS gameweeks
               FROM projection pr
               JOIN player p ON p.element_id = pr.element_id
               JOIN team t ON t.id = p.team_id
               JOIN player_snapshot ps ON ps.snapshot_id = pr.snapshot_id
                                      AND ps.element_id = pr.element_id
               WHERE pr.model_version = ?
                 AND pr.snapshot_id = (SELECT MAX(id) FROM snapshot)
               GROUP BY pr.element_id
               ORDER BY xp DESC LIMIT ?""",
            (MODEL_VERSION, args.top),
        ).fetchall()
        span = rows[0]["gameweeks"] if rows else 0
        label = f"xP({span}gw)" if span > 1 else "xP"
        print(f"\n{'player':16} {'team':5} {'price':>6} {label:>9} {'P(start)':>9} {'fix':>4}")
        for r in rows:
            print(f"{r['web_name']:16} {r['team']:5} £{r['now_cost']/10:>4.1f}m "
                  f"{r['xp']:>9.2f} {r['p_start']:>9.2f} {r['fixtures']:>4}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

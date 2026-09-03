"""Predicted lineups, resolved to FPL players and stored in the warehouse.

Minutes dominate FPL scoring: expected points multiply through the chance of starting, so
an error there corrupts every other term at once. FPL's own signal,
`chance_of_playing_next_round`, is only populated when there is *news*, so it catches
injuries and misses rotation entirely. A published lineup catches both.

Lineups are captured rather than fetched at projection time, for the same reason prices
are: predictions change up to kickoff, and a projection has to be reproducible from
stored state.

They apply only to the gameweek they were published for. RotoWire publishes the next
round of fixtures, so a three-gameweek horizon uses lineups for the first gameweek and
falls back to historical start rates for the rest.
"""

import logging
import sqlite3
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

from ..rotowire_scraper import LineupPlayer, MatchLineup

logger = logging.getLogger("fpl_lineups")

# Characters NFKD does not decompose, which appear in Premier League squads.
TRANSLITERATE = str.maketrans({"Đ": "D", "đ": "d", "Ø": "O", "ø": "o",
                               "Ł": "L", "ł": "l", "ß": "ss", "æ": "ae", "Æ": "Ae"})

# Kept strict, because a low bar inside a club matches the wrong player rather than none:
# at 0.55, "Haaland" searched in Liverpool's squad matched "Alexander". The awkward real
# cases - "Djordje Petrovic" against "Đorđe Petrović", "Ruben Dias" against "Rúben dos
# Santos Gato Alves Dias" - are caught by the shared-token rule below, not by similarity.
TEAM_MATCH_THRESHOLD = 0.75


def fold(name: str) -> str:
    """Strip diacritics and case so RotoWire and FPL spellings compare equal."""
    name = (name or "").translate(TRANSLITERATE)
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def _name_forms(player: dict) -> list[str]:
    """Every spelling FPL offers for one player.

    `first_name` alone matters: FPL calls Liverpool's goalkeeper `A.Becker` with the first
    name `Alisson`, which is the only name RotoWire uses.
    """
    first = fold(player.get("first_name") or "")
    second = fold(player.get("second_name") or "")
    web = fold(player.get("web_name") or "")
    forms = {web, second, first, f"{first} {second}".strip(), f"{first} {web}".strip()}
    return [f for f in forms if f]


def squad_index(bootstrap: dict) -> dict[str, list[dict]]:
    """FPL players grouped by club short name, from a raw bootstrap payload.

    Taken from the payload rather than from the store, so resolution works during a
    snapshot - where the store's in-memory model has not been built yet.
    """
    teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams") or []}
    index: dict[str, list[dict]] = {}
    for element in bootstrap.get("elements") or []:
        short = teams.get(element.get("team"))
        if short:
            index.setdefault(short, []).append(element)
    return index


def resolve_element_id(index: dict[str, list[dict]], name: str,
                       team_short: str) -> Optional[int]:
    """Find the FPL player a lineup entry refers to, within its club."""
    target = fold(name)
    target_tokens = set(target.split())

    best_id, best_score = None, 0.0
    for player in index.get(team_short, []):
        for form in _name_forms(player):
            if form == target:
                return player["id"]
            score = SequenceMatcher(None, target, form).ratio()
            # A shared surname is strong evidence once the club already matches.
            if target_tokens & set(form.split()):
                score = max(score, 0.8)
            if score > best_score:
                best_id, best_score = player["id"], score

    return best_id if best_score >= TEAM_MATCH_THRESHOLD else None


def record_lineups(conn: sqlite3.Connection, snapshot_id: int, gameweek: int,
                   matches: list[MatchLineup], bootstrap: dict) -> tuple[int, list[str]]:
    """Store resolved lineups. Returns (rows written, names that could not be resolved)."""
    index = squad_index(bootstrap)
    rows = []
    unresolved: list[str] = []
    seen: set[tuple[int, int]] = set()

    for match in matches:
        # The injury list is written first so a doubtful starter keeps its flag.
        for player in match.injuries + match.players:
            element_id = resolve_element_id(index, player.name, player.team)
            if element_id is None:
                unresolved.append(f"{player.name} ({player.team})")
                continue
            key = (gameweek, element_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append((snapshot_id, gameweek, element_id, player.team, player.name,
                         player.position, 1 if player.is_starter else 0,
                         player.injury, 1 if match.confirmed else 0))

    conn.executemany(
        "INSERT OR REPLACE INTO predicted_lineup VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    if unresolved:
        logger.warning("could not resolve %s lineup entries: %s",
                       len(unresolved), ", ".join(unresolved[:8]))
    logger.info("stored %s lineup entries for gameweek %s", len(rows), gameweek)
    return len(rows), unresolved


def lineup_start_rates(conn: sqlite3.Connection, gameweek: int,
                       starter: float, omitted: float,
                       confirmed_starter: float) -> dict[int, float]:
    """Selection probability per player, from the most recent lineups for a gameweek.

    Covers every player at a club with a published lineup, not only those named in it:
    a player his manager left out is the rotation case FPL's own flag never reports, and
    silence about him is the signal.
    """
    row = conn.execute(
        "SELECT MAX(snapshot_id) AS id FROM predicted_lineup WHERE gameweek = ?",
        (gameweek,),
    ).fetchone()
    if not row or row["id"] is None:
        return {}
    snapshot_id = row["id"]

    listed: dict[int, float] = {}
    teams_with_lineups: set[str] = set()
    for entry in conn.execute(
        "SELECT * FROM predicted_lineup WHERE snapshot_id = ? AND gameweek = ?",
        (snapshot_id, gameweek),
    ):
        teams_with_lineups.add(entry["team"])
        if entry["injury"] == "OUT":
            listed[entry["element_id"]] = 0.0
        elif entry["is_starter"]:
            listed[entry["element_id"]] = (
                confirmed_starter if entry["confirmed"] else starter)
        else:
            # In the injury list but not ruled out: doubtful.
            listed[entry["element_id"]] = omitted

    # Everyone else at a club with a published lineup was left out of it.
    rates = dict(listed)
    for player in conn.execute(
        """SELECT p.element_id, t.short_name AS team FROM player p
           JOIN team t ON t.id = p.team_id"""
    ):
        if player["team"] in teams_with_lineups and player["element_id"] not in rates:
            rates[player["element_id"]] = omitted
    return rates

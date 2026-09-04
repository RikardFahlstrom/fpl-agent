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
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Optional

from ..rotowire_scraper import INJURY_STATUS, LineupPlayer, MatchLineup

logger = logging.getLogger("fpl_lineups")

# RotoWire shorthand meaning the player cannot be picked at all. Derived from the
# scraper's own table rather than restated beside it, so a code added there cannot
# quietly stop being zeroed here - plus the codes that table misses. The page writes a
# suspension as both SUSP and SUS and only SUSP is mapped, so a suspended player named
# in the injury list was scoring the 0.15 meant for someone merely left out. FPL's own
# `s` flag catches most of them, and "most" is the kind of almost-right this project
# keeps being bitten by: a suspended player cannot play, so he belongs at 0 for exactly
# the reason OUT does.
UNAVAILABLE = {code for code, status in INJURY_STATUS.items() if status == "OUT"} | {"SUS"}

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


def fixture_events(fixtures: Iterable[dict],
                   bootstrap: dict) -> dict[frozenset[str], int]:
    """Which unfinished gameweek each pair of clubs meets in, by club short name.

    The RotoWire page carries no gameweek label, so the only way to know which round a
    scraped match belongs to is to ask the fixture list which event holds that pair.

    Keyed on the *unordered* pair: RotoWire's home/away is a page ordering and FPL's is
    the real one, and two clubs meet at most once in any event either way, so nothing is
    lost by not insisting the two agree. Finished fixtures are excluded and the earliest
    surviving event wins, which is what keeps the unordered key honest across a season -
    LIV-MCI sits in two events, and the reverse leg only becomes the answer once the
    first has been played. That rule is also the double-gameweek rule.
    """
    teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams") or []}
    events: dict[frozenset[str], int] = {}
    for fixture in fixtures:
        event = fixture.get("event")
        if event is None or fixture.get("finished"):
            continue
        home, away = teams.get(fixture.get("team_h")), teams.get(fixture.get("team_a"))
        if not home or not away:
            continue
        pair = frozenset((home, away))
        if pair not in events or event < events[pair]:
            events[pair] = event
    return events


@dataclass
class LineupCapture:
    """What a lineup capture actually filed, and under which gameweeks.

    `gameweeks` is a list, not the one that was asked for, because filing is decided per
    fixture and a page caught mid-changeover can legitimately carry two rounds. The
    caller reports what landed rather than what it requested - a summary line that names
    the target gameweek regardless is how a misfiling stays invisible.
    """
    rows: int
    unresolved: list[str] = field(default_factory=list)
    gameweeks: list[int] = field(default_factory=list)
    unplaceable: list[str] = field(default_factory=list)


def record_lineups(conn: sqlite3.Connection, snapshot_id: int, gameweek: int,
                   matches: list[MatchLineup], bootstrap: dict,
                   fixture_events: Optional[dict[frozenset[str], int]] = None
                   ) -> LineupCapture:
    """Store resolved lineups, each match under the gameweek its fixture is actually in.

    `gameweek` is the snapshot's target, and it is only a *guess* at where a scraped
    match belongs. RotoWire shows the next round to be played; FPL flips `is_next` the
    instant a deadline passes. So every snapshot taken between the gameweek N deadline
    and that round's last kickoff - a Saturday night, any Friday-to-Monday round, and
    precisely when the hourly `deadline` cron fires - filed gameweek N lineups under
    N + 1. `lineup_start_rates` takes the latest snapshot for a gameweek, so a later
    correct scrape usually overwrote it, but only if one happened before `project` ran.

    Pass `fixture_events` (from `fixture_events()`) and each match is filed under the
    event that actually holds its pair of clubs; the target is used only for a pair no
    unfinished fixture claims, and that fallback is named in a warning rather than taken
    silently. Omitting the map keeps the old behaviour, which is what makes this
    testable without a fixture table.

    A doubtful starter is published twice - once in the XI, once in the injury list
    beneath it - and the two entries disagree about `is_starter`. Neither wins outright:
    the row keeps the injury flag *and* the XI's `is_starter`, because they are answers
    to different questions and dropping either loses real information. Taking the injury
    entry whole, as this did, recorded a named starter as benched and injured at once.
    """
    index = squad_index(bootstrap)
    unresolved: list[str] = []
    unplaceable: list[str] = []
    merged: dict[tuple[int, int], list] = {}

    for match in matches:
        event = gameweek
        if fixture_events is not None:
            found = fixture_events.get(frozenset((match.home_team, match.away_team)))
            if found is None:
                unplaceable.append(f"{match.home_team}-{match.away_team}")
            else:
                event = found

        # The injury list is read first so a doubtful starter keeps its flag.
        for player in match.injuries + match.players:
            element_id = resolve_element_id(index, player.name, player.team)
            if element_id is None:
                unresolved.append(f"{player.name} ({player.team})")
                continue
            key = (event, element_id)
            row = merged.get(key)
            if row is None:
                merged[key] = [snapshot_id, event, element_id, player.team,
                               player.name, player.position,
                               1 if player.is_starter else 0, player.injury,
                               1 if match.confirmed else 0]
                continue
            # Same player, second entry: the XI decides selection, the injury list the flag.
            if player.is_starter:
                row[5] = player.position    # the XI carries the position he will play
                row[6] = 1
            row[7] = row[7] or player.injury

    rows = [tuple(row) for row in merged.values()]
    conn.executemany(
        "INSERT OR REPLACE INTO predicted_lineup VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    if unresolved:
        logger.warning("could not resolve %s lineup entries: %s",
                       len(unresolved), ", ".join(unresolved[:8]))
    if unplaceable:
        logger.warning(
            "no unfinished fixture holds %s, so %s filed under the snapshot's target "
            "gameweek %s, which may be the wrong round: %s",
            "these pairs" if len(unplaceable) > 1 else "this pair",
            "they were" if len(unplaceable) > 1 else "it was",
            gameweek, ", ".join(unplaceable))
    gameweeks = sorted({row[1] for row in rows})
    for event in gameweeks:
        logger.info("stored %s lineup entries for gameweek %s",
                    sum(1 for row in rows if row[1] == event), event)
    return LineupCapture(rows=len(rows), unresolved=unresolved,
                         gameweeks=gameweeks, unplaceable=unplaceable)


def lineup_start_rates(conn: sqlite3.Connection, gameweek: int,
                       starter: float, omitted: float,
                       confirmed_starter: float) -> dict[int, float]:
    """Selection probability per player, from the most recent lineups for a gameweek.

    Covers every player at a club with a published lineup, not only those named in it:
    a player his manager left out is the rotation case FPL's own flag never reports, and
    silence about him is the signal.

    Three cases, and a doubt is only ever counted once. Unavailable is zero - OUT, and a
    suspension under either of the two codes RotoWire writes it as, because a banned
    player is no more selectable than an injured one. Named in the XI is
    the starter rate *whatever* the injury flag says: a QUES starter is a player RotoWire
    expects to play through a knock, and his fitness is already priced by FPL's
    `chance_of_playing_next_round`, which `project_player` multiplies through this rate.
    Demoting him here too charged the same doubt twice - 75% fit times a 0.15 rate meant
    for players nobody named, reading as benched *and* injured. Doubtful and not named is
    the omitted rate, like any other player left out.
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
        if entry["injury"] in UNAVAILABLE:
            listed[entry["element_id"]] = 0.0
        elif entry["is_starter"]:
            listed[entry["element_id"]] = (
                confirmed_starter if entry["confirmed"] else starter)
        else:
            # Listed but not named in the XI: doubtful, or simply left out.
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

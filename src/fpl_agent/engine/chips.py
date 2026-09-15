"""Chips: what FPL says you hold, and what each one would be worth in each week.

FPL's `my-team` payload lists every chip with `status_for_entry` (available, active,
played), `start_event` and `stop_event`. Since 2024/25 there are two sets a season, each
with a wall: a chip unused when its set expires is simply lost. Nothing here hardcodes
that rule - the window is read from the payload, so the second set opens when FPL says
it does and the wall moves when FPL moves it.

Everything below is a function of stored rows. The projection range the values are read
from is the *chip window* (`window_end`), which `project --chips` projects to; the values
themselves are computed in later steps of issue #76 (bench boost and triple captain from
the held squad, free hit and wildcard from a rebuilt one).

    fpl-agent project --horizon 3 --chips     # project every week to the set's expiry
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import warehouse

#: FPL's chip names, and what a person calls them.
CHIP_TITLES = {"bboost": "bench boost", "3xc": "triple captain",
               "freehit": "free hit", "wildcard": "wildcard"}


@dataclass(frozen=True)
class ChipState:
    """One chip as FPL reports it for the entry."""
    name: str
    status: str                  # available | active | played | unavailable
    start_event: Optional[int]
    stop_event: Optional[int]
    played_in: Optional[int]     # the gameweek it was played, if played

    @property
    def title(self) -> str:
        return CHIP_TITLES.get(self.name, self.name)

    def open_in(self, gameweek: int) -> bool:
        """Whether the chip's window covers this gameweek."""
        after_start = self.start_event is None or gameweek >= self.start_event
        before_stop = self.stop_event is None or gameweek <= self.stop_event
        return after_start and before_stop

    def evaluable(self, gameweek: int) -> bool:
        """Available and inside its window: the only state worth a value."""
        return self.status == "available" and self.open_in(gameweek)

    def describe(self, gameweek: int) -> str:
        """The state in the reader's words, for the block."""
        if self.status == "played":
            return f"played" + (f" in GW{self.played_in}" if self.played_in else "")
        if self.status == "active":
            return "active this gameweek"
        if self.status == "available" and not self.open_in(gameweek):
            if self.start_event and gameweek < self.start_event:
                return f"not until GW{self.start_event}"
            return "expired"
        return self.status


def chip_states(chips_json: Optional[str]) -> list[ChipState]:
    """Every chip in the stored payload, in FPL's order; empty when nothing was stored."""
    if not chips_json:
        return []
    try:
        chips = json.loads(chips_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(chips, list):
        return []
    states = []
    for chip in chips:
        if not isinstance(chip, dict) or "name" not in chip:
            continue
        played = chip.get("played_by_entry") or []
        states.append(ChipState(
            name=str(chip["name"]),
            status=str(chip.get("status_for_entry") or "unavailable"),
            start_event=_int(chip.get("start_event")),
            stop_event=_int(chip.get("stop_event")),
            played_in=_int(played[0]) if played else None))
    return states


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def stored_chips(conn: sqlite3.Connection, snapshot_id: int) -> list[ChipState]:
    row = conn.execute("SELECT chips FROM my_state WHERE snapshot_id = ?",
                       (snapshot_id,)).fetchone()
    return chip_states(row["chips"] if row else None)


def window_end(states: list[ChipState], gameweek: int) -> Optional[int]:
    """The last gameweek of the chip set in play at `gameweek` - the wall.

    The furthest `stop_event` among chips whose window covers the gameweek, whatever
    their status: a set with every chip played still defines the range, so the stored
    projections do not shrink the week you play your last one. None when the payload
    holds nothing, which is a market-only capture with no squad.
    """
    stops = [s.stop_event for s in states if s.open_in(gameweek) and s.stop_event]
    return max(stops) if stops else None


def chip_window(conn: sqlite3.Connection, gameweek: int) -> Optional[int]:
    """`window_end` for the latest capture with a squad, or None."""
    capture = warehouse.with_squad(conn)
    if capture is None:
        return None
    return window_end(stored_chips(conn, capture.id), gameweek)


# --------------------------------------------------------------------------
# Captain
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Pick:
    """One player's claim on the armband for a gameweek."""
    element_id: int
    name: str
    team: str
    xp: float
    fixtures: int
    is_captain: bool     # the armband as captured


def captain_picks(conn: sqlite3.Connection, snapshot_id: int, gameweek: int,
                  model_version: str,
                  squad: Optional[list[dict[str, Any]]] = None) -> list[Pick]:
    """The XI ranked by projected points for the gameweek, best first.

    The captain is the model's, not the payload's: the `is_captain` flag rides along
    so the line can say when they differ. Reads the stored projection for the capture,
    which is the decision-time record; no projection means an empty list, and the
    caller says "not evaluated" rather than guessing.

    `squad` overrides the captured XI - a list of dicts with `element_id`, `position`
    - so the post-move squad can be ranked too.
    """
    if squad is None:
        rows = conn.execute(
            """SELECT element_id, position, multiplier FROM my_squad
               WHERE snapshot_id = ?""", (snapshot_id,)).fetchall()
        squad = [dict(r) for r in rows]
    xi = [p for p in squad if (p.get("position") or 99) <= 11]
    if not xi:
        return []
    ids = [p["element_id"] for p in xi]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT pr.element_id, p.web_name, t.short_name AS team,
                   pr.expected_points, pr.fixture_count
            FROM projection pr
            JOIN player p ON p.element_id = pr.element_id
            LEFT JOIN team t ON t.id = p.team_id
            WHERE pr.snapshot_id = ? AND pr.gameweek = ? AND pr.model_version = ?
              AND pr.element_id IN ({placeholders})
            ORDER BY pr.expected_points DESC""",
        (snapshot_id, gameweek, model_version, *ids)).fetchall()
    armband = {p["element_id"] for p in xi if (p.get("multiplier") or 1) > 1}
    return [Pick(r["element_id"], r["web_name"], r["team"] or "?",
                 round(r["expected_points"], 2), r["fixture_count"] or 0,
                 r["element_id"] in armband)
            for r in rows]


def captain_line(picks: list[Pick]) -> str:
    """The `Captain:` line: the model's pick, the runner-up, and whether it is who
    the armband is on."""
    if not picks:
        return "not evaluated - no projection for the XI"
    best = picks[0]
    text = f"{best.name} ({best.team}), {best.xp:.1f} xP"
    if best.fixtures > 1:
        text += f" over {best.fixtures} fixtures"
    if len(picks) > 1:
        text += f"; next {picks[1].name} {picks[1].xp:.1f}"
    held = next((p for p in picks if p.is_captain), None)
    if held is None:
        text += " - no armband captured"
    elif held is best:
        text += " - armband already on him"
    else:
        text += f" - armband is on {held.name} ({held.xp:.1f})"
    return text

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


# --------------------------------------------------------------------------
# Values: what each chip would be worth in each week to the wall
# --------------------------------------------------------------------------

#: How close to the best remaining week "now" has to be to be played now. A chip held
#: for a marginally better week is one you may never get - injuries, prices, a
#: rescheduled fixture - and the set has a wall, so ties go to now. A stated assumption,
#: to be fitted once a season of chip outcomes exists.
CHIP_NOISE_BAND = 0.15

#: How many gameweeks a wildcard is valued over: a rebuild you then live with, so more
#: than the transfer horizon; less than the whole window, which is fixture noise by the
#: end. A stated assumption, like the band.
WILDCARD_HORIZON = 6

#: The bar each chip has to clear before "now" is even considered. The best week of the
#: window is not enough on its own: the current week is the only one with predicted
#: lineups, so it beats a lineup-less future week almost by construction, and a
#: best-of-window rule alone would burn every chip in the first week it was valued.
#: A bench boost is worth playing when the bench is worth a strong starter or two; a
#: triple captain when the captain's week is a double or an elite single. Stated
#: assumptions in points, to be fitted like the band.
CHIP_BARS = {"bboost": 15.0, "3xc": 9.0}

STARTING_XI = 11


@dataclass(frozen=True)
class ChipValue:
    """What one chip would be worth in one gameweek, and what makes the number."""
    gameweek: int
    value: float
    note: str            # "Haaland, 2 fixtures" / "P12, P13, P14, P15"


@dataclass(frozen=True)
class Verdict:
    """One chip's answer: play now, hold for a named week, or why it has no answer."""
    state: ChipState
    gameweek: int
    values: tuple[ChipValue, ...]    # one per projected week from now to the wall
    now: Optional[ChipValue]
    best: Optional[ChipValue]
    play_now: bool
    reason: str                      # the reader's words, always

    @property
    def title(self) -> str:
        return self.state.title

    @property
    def evaluated(self) -> bool:
        return self.now is not None

    @property
    def short(self) -> str:
        """The clause for the block: the verdict and the two numbers behind it."""
        if not self.evaluated:
            return ("not evaluated" if self.state.evaluable(self.gameweek)
                    else self.state.describe(self.gameweek))
        bar = CHIP_BARS.get(self.state.name)
        if self.play_now:
            return f"PLAY NOW (+{self.now.value:.1f}, bar {bar:.0f})"
        if self.best and self.best.gameweek != self.now.gameweek and self.best.value >= (bar or 0):
            return f"hold for GW{self.best.gameweek} (+{self.best.value:.1f} vs +{self.now.value:.1f} now)"
        return f"hold (+{self.now.value:.1f}, bar {bar:.0f})"


def squad_projections(conn: sqlite3.Connection, snapshot_id: int, model_version: str,
                      element_ids: list[int], first: int, last: int
                      ) -> dict[int, dict[int, tuple[float, int, str, str]]]:
    """gameweek -> element_id -> (xp, fixtures, name, team) for the stored range."""
    if not element_ids:
        return {}
    placeholders = ",".join("?" * len(element_ids))
    rows = conn.execute(
        f"""SELECT pr.gameweek, pr.element_id, pr.expected_points, pr.fixture_count,
                   p.web_name, t.short_name AS team
            FROM projection pr
            JOIN player p ON p.element_id = pr.element_id
            LEFT JOIN team t ON t.id = p.team_id
            WHERE pr.snapshot_id = ? AND pr.model_version = ?
              AND pr.gameweek BETWEEN ? AND ?
              AND pr.element_id IN ({placeholders})""",
        (snapshot_id, model_version, first, last, *element_ids)).fetchall()
    by_week: dict[int, dict[int, tuple[float, int, str, str]]] = {}
    for r in rows:
        by_week.setdefault(r["gameweek"], {})[r["element_id"]] = (
            r["expected_points"], r["fixture_count"] or 0, r["web_name"], r["team"] or "?")
    return by_week


def apply_move(squad: list[dict[str, Any]], move: Optional[dict[str, Any]]
               ) -> list[dict[str, Any]]:
    """The squad after the recommended transfer: the incoming player takes the
    outgoing one's slot. No move, or a move for a player not held, changes nothing."""
    if not move:
        return list(squad)
    out_id, in_id = move["out"]["element_id"], move["in"]["element_id"]
    if not any(p["element_id"] == out_id for p in squad):
        return list(squad)
    return [dict(p, element_id=in_id) if p["element_id"] == out_id else p for p in squad]


def bench_boost_values(by_week, squad) -> list[ChipValue]:
    """The bench's projected points, week by week: what the chip adds."""
    bench = [p["element_id"] for p in squad if (p.get("position") or 99) > STARTING_XI]
    values = []
    for gameweek in sorted(by_week):
        week = by_week[gameweek]
        held = [week[e] for e in bench if e in week]
        if not held:
            continue
        values.append(ChipValue(gameweek, round(sum(x[0] for x in held), 2),
                                ", ".join(x[2] for x in held)))
    return values


def triple_captain_values(by_week, squad) -> list[ChipValue]:
    """The best XI player's projected points, week by week: the extra 1x the chip adds."""
    xi = [p["element_id"] for p in squad if (p.get("position") or 99) <= STARTING_XI]
    values = []
    for gameweek in sorted(by_week):
        week = by_week[gameweek]
        held = [week[e] for e in xi if e in week]
        if not held:
            continue
        xp, fixtures, name, _ = max(held, key=lambda x: x[0])
        note = name + (f", {fixtures} fixtures" if fixtures > 1 else "")
        values.append(ChipValue(gameweek, round(xp, 2), note))
    return values


def decide(state: ChipState, values: list[ChipValue], gameweek: int,
           band: float = CHIP_NOISE_BAND, bar: Optional[float] = None) -> Verdict:
    """Play now if this week clears the chip's bar *and* is the best remaining week or
    within the band of it; otherwise hold. The reason is written for the page."""
    bar = CHIP_BARS.get(state.name, 0.0) if bar is None else bar
    now = next((v for v in values if v.gameweek == gameweek), None)
    if now is None:
        return Verdict(state, gameweek, tuple(values), None, None, False,
                       "not evaluated - no projection for this gameweek")
    best = max(values, key=lambda v: v.value)
    weeks_left = len(values)
    if now.value < bar:
        later = [v for v in values if v.gameweek != gameweek and v.value >= bar]
        ahead = (f"GW{later[0].gameweek} clears it (+{later[0].value:.1f}, {later[0].note})"
                 if later else f"no week to GW{values[-1].gameweek} clears it either")
        return Verdict(state, gameweek, tuple(values), now, best, False,
                       f"hold: +{now.value:.1f} now ({now.note}), under the {bar:.0f} bar; "
                       f"{ahead}")
    if now.gameweek == best.gameweek or now.value >= best.value * (1 - band):
        why = ("the best of the" if now.gameweek == best.gameweek else
               f"within {band:.0%} of GW{best.gameweek} (+{best.value:.1f}), best of the")
        return Verdict(state, gameweek, tuple(values), now, best, True,
                       f"play now: +{now.value:.1f} ({now.note}) clears the {bar:.0f} bar, "
                       f"{why} {weeks_left} weeks left")
    return Verdict(state, gameweek, tuple(values), now, best, False,
                   f"hold for GW{best.gameweek}: +{best.value:.1f} ({best.note}) "
                   f"against +{now.value:.1f} now")


def not_evaluated(state: ChipState, gameweek: int, why: str) -> Verdict:
    return Verdict(state, gameweek, (), None, None, False,
                   f"{state.describe(gameweek)} - {why}" if state.evaluable(gameweek)
                   else state.describe(gameweek))


def evaluate(conn: sqlite3.Connection, snapshot_id: int, gameweek: int,
             model_version: str, squad: list[dict[str, Any]],
             states: Optional[list[ChipState]] = None) -> list[Verdict]:
    """Every chip's verdict for the gameweek, in FPL's order.

    `squad` is the one the values are summed over - the post-move squad, by the owner's
    choice, so the chip advice and the transfer advice agree. Chips that are played,
    expired or not yet open are reported as such and not valued; free hit and wildcard
    are "not evaluated" until the rebuild optimiser exists (issue #76, steps 3-4).
    """
    states = stored_chips(conn, snapshot_id) if states is None else states
    if not states:
        return []
    wall = window_end(states, gameweek) or gameweek
    ids = [p["element_id"] for p in squad]
    by_week = squad_projections(conn, snapshot_id, model_version, ids, gameweek, wall)
    verdicts = []
    for state in states:
        if not state.evaluable(gameweek):
            verdicts.append(not_evaluated(state, gameweek, ""))
        elif not squad:
            verdicts.append(not_evaluated(state, gameweek, "no squad captured"))
        elif state.name == "bboost":
            verdicts.append(decide(state, bench_boost_values(by_week, squad), gameweek))
        elif state.name == "3xc":
            verdicts.append(decide(state, triple_captain_values(by_week, squad), gameweek))
        else:
            verdicts.append(not_evaluated(state, gameweek,
                                          "not evaluated yet - needs the squad rebuild"))
    return verdicts


def chips_line(verdicts: list[Verdict], gameweek: int) -> str:
    """The `Chips:` line: one clause per chip, then the wall."""
    if not verdicts:
        return "not evaluated - no chips captured (an authenticated snapshot records them)"
    parts = [f"{v.title} {v.short}" for v in verdicts]
    wall = window_end([v.state for v in verdicts], gameweek)
    tail = f"; set expires after GW{wall} ({wall - gameweek + 1} weeks left)" if wall else ""
    return " · ".join(parts) + tail

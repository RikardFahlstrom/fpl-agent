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
from dataclasses import dataclass, field
from typing import Optional

from . import squad as squads
from . import warehouse
from .held import BUYABLE, ChipState, HeldSquad, read as read_held


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
    holding = read_held(conn, capture) if capture else None
    if holding is None:
        return None
    return window_end(list(holding.chips), gameweek)


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
    opponents: tuple[str, ...] = ()       # "BHA away", one per fixture
    difficulties: tuple[int, ...] = ()    # FPL's 1-5, one per fixture
    components: dict[str, float] = field(default_factory=dict)  # the xP by source


def captain_picks(conn: sqlite3.Connection, gameweek: int, model_version: str,
                  holding: HeldSquad) -> list[Pick]:
    """The held XI ranked by projected points for the gameweek, best first.

    The captain is the model's, not the payload's: the `is_captain` flag rides along
    so the line can say when they differ. Reads the stored projection for the held
    squad's capture, which is the decision-time record; no projection means an empty
    list, and the caller says "not evaluated" rather than guessing.
    """
    xi = holding.xi
    if not xi:
        return []
    snapshot_id = holding.snapshot_id
    ids = [p.element_id for p in xi]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT pr.element_id, p.web_name, p.team_id, t.short_name AS team,
                   pr.expected_points, pr.fixture_count, pr.difficulties, pr.components
            FROM projection pr
            JOIN player p ON p.element_id = pr.element_id
            LEFT JOIN team t ON t.id = p.team_id
            WHERE pr.snapshot_id = ? AND pr.gameweek = ? AND pr.model_version = ?
              AND pr.element_id IN ({placeholders})
            ORDER BY pr.expected_points DESC""",
        (snapshot_id, gameweek, model_version, *ids)).fetchall()
    armband = {p.element_id for p in xi if (p.multiplier or 1) > 1}
    opponents = _opponents(conn, gameweek)
    return [Pick(r["element_id"], r["web_name"], r["team"] or "?",
                 round(r["expected_points"], 2), r["fixture_count"] or 0,
                 r["element_id"] in armband,
                 tuple(opponents.get(r["team_id"], ())),
                 tuple(json.loads(r["difficulties"] or "[]")),
                 json.loads(r["components"] or "{}"))
            for r in rows]


def _opponents(conn: sqlite3.Connection, gameweek: int) -> dict[int, list[str]]:
    """Each team's opponents in the gameweek as "BHA away", in kickoff order."""
    rows = conn.execute(
        """SELECT f.team_h, f.team_a, th.short_name AS home, ta.short_name AS away
           FROM fixture f
           LEFT JOIN team th ON th.id = f.team_h
           LEFT JOIN team ta ON ta.id = f.team_a
           WHERE f.event = ? ORDER BY f.kickoff_time""", (gameweek,)).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["team_h"], []).append(f"{r['away'] or '?'} at home")
        out.setdefault(r["team_a"], []).append(f"{r['home'] or '?'} away")
    return out


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


#: How `components` keys read in a sentence.
_SOURCES = (("goals", "goals"), ("assists", "assists"), ("clean_sheet", "clean sheet"),
            ("bonus", "bonus"), ("saves", "saves"),
            ("defensive_contribution", "defensive contribution"))

#: Below this, a source is not worth a word; below this gap, two picks are a coin flip.
_SOURCE_FLOOR = 0.25
_COIN_FLIP = 0.3


def captain_why(picks: list[Pick], *, short: bool = False) -> str:
    """Why the pick is the pick: the fixture and what the xP is made of, then how far
    the runner-up and the armband holder are behind. `short` keeps the first
    sentence only, for a push.

    The numbers are the projection's own, so the reader can argue with the model
    rather than with a name. Empty when there is nothing to explain.
    """
    if not picks:
        return ""
    best = picks[0]
    first = ""
    if best.opponents:
        fixture = " and ".join(best.opponents)
        if best.difficulties:
            fixture += " (difficulty " + "/".join(str(d) for d in best.difficulties) + ")"
        first = f"{best.name} faces {fixture}"
    sources = sorted(((label, best.components.get(key, 0.0)) for key, label in _SOURCES
                      if best.components.get(key, 0.0) >= _SOURCE_FLOOR),
                     key=lambda kv: -kv[1])[:3]
    if sources:
        made = ", ".join(f"{v:.1f} from {label}" for label, v in sources)
        first = (f"{first}; the {best.xp:.1f} xP is {made}" if first
                 else f"{best.name}'s {best.xp:.1f} xP is {made}")
    second = ""
    if len(picks) > 1 and not short:
        runner = picks[1]
        gap = best.xp - runner.xp
        parts = [f"{runner.name} is {gap:.1f} behind" if gap >= 0.05
                 else f"{runner.name} is level"]
        held = next((p for p in picks if p.is_captain), None)
        if held is not None and held is not best and held is not runner:
            parts.append(f"{held.name} {best.xp - held.xp:.1f} behind")
        second = ", ".join(parts)
        if gap < _COIN_FLIP:
            second += ", so the armband is a coin flip"
        elif held is not None and held is not best:
            second += ", so moving the armband is worth doing"
    return " ".join(f"{t}." for t in (first, second) if t)


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
#: Free hit and wildcard are *differences* - the rebuilt squad against the held one -
#: so their bars are gains: a free hit is for a week the held squad is badly placed for
#: (blanks, a run of hard fixtures), a wildcard for a squad that is well behind what
#: the money could buy over the wildcard horizon.
CHIP_BARS = {"bboost": 15.0, "3xc": 9.0, "freehit": 8.0, "wildcard": 15.0}


@dataclass(frozen=True)
class ChipValue:
    """What one chip would be worth in one gameweek, and what makes the number."""
    gameweek: int
    value: float
    note: str            # "Haaland, 2 fixtures" / "P12, P13, P14, P15"
    squad: tuple[str, ...] = ()   # a rebuild: the fifteen it would buy, XI first


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


def bench_boost_values(by_week, holding: HeldSquad) -> list[ChipValue]:
    """The bench's projected points, week by week: what the chip adds."""
    bench = [p.element_id for p in holding.bench]
    values = []
    for gameweek in sorted(by_week):
        week = by_week[gameweek]
        held = [week[e] for e in bench if e in week]
        if not held:
            continue
        values.append(ChipValue(gameweek, round(sum(x[0] for x in held), 2),
                                ", ".join(x[2] for x in held)))
    return values


def triple_captain_values(by_week, holding: HeldSquad) -> list[ChipValue]:
    """The best XI player's projected points, week by week: the extra 1x the chip adds."""
    xi = [p.element_id for p in holding.xi]
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


# --------------------------------------------------------------------------
# Rebuilds: free hit and wildcard
# --------------------------------------------------------------------------

def market(conn: sqlite3.Connection, snapshot_id: int, model_version: str,
           first: int, last: int) -> dict[int, dict[int, squads.Candidate]]:
    """gameweek -> element_id -> Candidate for every buyable player, per week.

    Buyable is `held.BUYABLE`, the same set `recommend` offers. The cost is today's
    price: a rebuild is priced at what the market asks now.
    """
    rows = conn.execute(
        f"""SELECT pr.gameweek, pr.element_id, p.web_name, p.team_id, p.element_type,
                   ps.now_cost, pr.expected_points
            FROM projection pr
            JOIN player p ON p.element_id = pr.element_id
            JOIN player_snapshot ps ON ps.snapshot_id = pr.snapshot_id
                                   AND ps.element_id = pr.element_id
            WHERE pr.snapshot_id = ? AND pr.model_version = ?
              AND pr.gameweek BETWEEN ? AND ?
              AND ps.status IN ({",".join("?" * len(BUYABLE))})""",
        (snapshot_id, model_version, first, last, *BUYABLE)).fetchall()
    by_week: dict[int, dict[int, squads.Candidate]] = {}
    for r in rows:
        by_week.setdefault(r["gameweek"], {})[r["element_id"]] = squads.Candidate(
            r["element_id"], r["web_name"], r["team_id"], r["element_type"],
            r["now_cost"], r["expected_points"])
    return by_week


def _summed(by_week: dict[int, dict[int, squads.Candidate]],
            weeks: list[int]) -> list[squads.Candidate]:
    """Candidates with their points summed over `weeks` (present in every week)."""
    if not weeks or any(w not in by_week for w in weeks):
        return []
    first = by_week[weeks[0]]
    out = []
    for element_id, c in first.items():
        total = 0.0
        for w in weeks:
            other = by_week[w].get(element_id)
            if other is None:
                break
            total += other.xp
        else:
            out.append(squads.Candidate(c.element_id, c.name, c.team_id, c.element_type,
                                        c.cost, round(total, 3)))
    return out


def held_squad(holding: HeldSquad, candidates: list[squads.Candidate],
               held_xp: dict[int, float]) -> Optional[squads.Squad]:
    """The held fifteen as a Squad over the same weeks, so the comparison is like for
    like: its XI is the best legal one, its bench discounted the same way."""
    by_id = {c.element_id: c for c in candidates}
    players = []
    for p in holding.players:
        c = by_id.get(p.element_id)
        if c is None:
            # Held but not buyable (injured, suspended): still in the squad, and his
            # projection - however small - is what he is worth to it.
            c = squads.Candidate(p.element_id, p.name, p.team_id, p.element_type,
                                 p.selling_price, held_xp.get(p.element_id, 0.0))
        players.append(c)
    if len(players) != 15 or not squads.legal(players, 10 ** 9, team_limit=99):
        return None
    return squads.Squad(tuple(players), squads.best_xi(players),
                        sum(p.cost for p in players))


def rebuild_values(by_week, holding: HeldSquad, held_by_week, span: int,
                   wall: int) -> list[ChipValue]:
    """The rebuilt squad's objective minus the held squad's, per week, over `span`
    weeks from that week (capped at the wall), bought with the held squad's budget
    under its club limit. A week the projections do not reach contributes no value
    rather than a guess."""
    owned = {p.element_id for p in holding.players}
    values = []
    for gameweek in sorted(by_week):
        weeks = [w for w in range(gameweek, min(gameweek + span, wall + 1))]
        candidates = _summed(by_week, weeks)
        if not candidates:
            continue
        held_xp = {e: sum(held_by_week.get(w, {}).get(e, (0.0,))[0] for w in weeks)
                   for e in owned}
        held = held_squad(holding, candidates, held_xp)
        best = squads.best_squad(candidates, holding.budget, holding.team_limit)
        if held is None or best is None:
            continue
        gain = best.objective - held.objective
        arrivals = [p.name for p in best.xi if p.element_id not in owned]
        note = ", ".join(arrivals[:4]) + ("…" if len(arrivals) > 4 else "")
        listing = tuple(f"{p.name} £{p.cost / 10:.1f}m {p.xp:.1f}"
                        for p in (*best.xi, *best.bench))
        values.append(ChipValue(gameweek, round(gain, 2), note or "no change", listing))
    return values


def decide(state: ChipState, values: list[ChipValue], gameweek: int,
           band: float = CHIP_NOISE_BAND, bar: Optional[float] = None) -> Verdict:
    """Play now if this week clears the chip's bar *and* is the best remaining week or
    within the band of it; otherwise hold. The reason is written for the page."""
    bar = CHIP_BARS.get(state.name, 0.0) if bar is None else bar
    now = next((v for v in values if v.gameweek == gameweek), None)
    if now is None:
        return Verdict(state, gameweek, tuple(values), None, None, False,
                       "not evaluated - no value for this gameweek" +
                       ("" if values else " (nothing projected to value)"))
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


def evaluate(conn: sqlite3.Connection, gameweek: int, model_version: str,
             held: HeldSquad) -> list[Verdict]:
    """Every chip's verdict for the gameweek, in FPL's order.

    `held` is the squad the values are summed over - the post-move squad, by the
    owner's choice, so the chip advice and the transfer advice agree - and carries the
    chip payload, the rebuild budget and the club limit. Chips that are played, expired
    or not yet open are reported as such and not valued.
    """
    states = list(held.chips)
    if not states:
        return []
    wall = window_end(states, gameweek) or gameweek
    ids = [p.element_id for p in held.players]
    by_week = squad_projections(conn, held.snapshot_id, model_version, ids, gameweek, wall)
    rebuilds = [s for s in states if s.name in ("freehit", "wildcard")
                and s.evaluable(gameweek)]
    shaped = None
    if rebuilds:
        buyable = market(conn, held.snapshot_id, model_version, gameweek, wall)
        shape: dict[int, int] = {}
        for p in held.players:
            shape[p.element_type] = shape.get(p.element_type, 0) + 1
        shaped = shape == squads.SQUAD_SHAPE
    verdicts = []
    for state in states:
        if not state.evaluable(gameweek):
            verdicts.append(not_evaluated(state, gameweek, ""))
        elif state.name == "bboost":
            verdicts.append(decide(state, bench_boost_values(by_week, held), gameweek))
        elif state.name == "3xc":
            verdicts.append(decide(state, triple_captain_values(by_week, held), gameweek))
        elif state.name in ("freehit", "wildcard") and not shaped:
            verdicts.append(not_evaluated(
                state, gameweek, "the held squad is not FPL-shaped (2-5-5-3), so a "
                                 "rebuild cannot be compared against it"))
        elif state.name == "freehit":
            verdicts.append(decide(state, rebuild_values(
                buyable, held, by_week, 1, wall), gameweek))
        elif state.name == "wildcard":
            verdicts.append(decide(state, rebuild_values(
                buyable, held, by_week, WILDCARD_HORIZON, wall), gameweek))
        else:
            verdicts.append(not_evaluated(state, gameweek, "no rule for this chip"))
    return verdicts


def chips_line(verdicts: list[Verdict], gameweek: int) -> str:
    """The `Chips:` line: one clause per chip, then the wall."""
    if not verdicts:
        return "not evaluated - no chips captured (an authenticated snapshot records them)"
    parts = [f"{v.title} {v.short}" for v in verdicts]
    wall = window_end([v.state for v in verdicts], gameweek)
    tail = f"; set expires after GW{wall} ({wall - gameweek + 1} weeks left)" if wall else ""
    return " · ".join(parts) + tail

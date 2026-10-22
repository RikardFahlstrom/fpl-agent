"""Grade the Instructions against what happened.

For a settled gameweek, the last Instructions record made before its deadline is the advice
that was acted on (`instructions.last_before_deadline`). This scores each part of it against
its alternatives, in the points the players actually scored:

- **captain**: the pick's extra points against every other starter - the best one is what
  hindsight would have chosen, and the gap is what the choice cost;
- **XI**: the recommended eleven against the best legal eleven from the same fifteen;
- **bench order**: the points the automatic substitutions added under the recommended order
  against the best order the same bench could have been in;
- **plan**: the recommended moves and the lineup they made, after the hits they cost,
  against doing nothing and the lineup the model would then have picked.

A player with no `player_gameweek` row scored nothing and played no minutes. That is only
true once the gameweek has finished, which is the only time this runs: `settle` refuses a
gameweek that has not. A gameweek with no stored Instructions says so; it is never given a
grade it did not earn. Nothing here writes, so the graded record is exactly as it was.

The substitution rules are FPL's: a starter who played no minutes is replaced by the first
bench player in order who played, if the formation stays legal (one goalkeeper, at least
three defenders, two midfielders and one forward), a goalkeeper only by the spare
goalkeeper. A captain who played no minutes hands the armband to the vice-captain. The
settled gameweek only is graded - a plan's value runs three weeks, and the other two are
not played.
"""

import itertools
import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import held, instructions, squad as squads, warehouse
from .instructions import Lineup, Slot
from .stance import NONE as NO_STANCE

#: Smallest legal formation by element type (defenders, midfielders, forwards).
MINIMUM = {2: 3, 3: 2, 4: 1}


@dataclass(frozen=True)
class Actual:
    points: int
    minutes: int


def actuals(conn: sqlite3.Connection, gameweek: int,
            element_ids: list[int]) -> dict[int, Actual]:
    """Each player's points and minutes; one with no row scored nothing and did not play."""
    found = {e: Actual(0, 0) for e in element_ids}
    if not element_ids:
        return found
    for row in conn.execute(
            f"SELECT element_id, total_points, minutes FROM player_gameweek "
            f"WHERE round = ? AND element_id IN ({','.join('?' * len(element_ids))})",
            (gameweek, *element_ids)):
        found[row["element_id"]] = Actual(row["total_points"] or 0, row["minutes"] or 0)
    return found


def _formation_ok(xi: list[Slot]) -> bool:
    counts = {t: sum(1 for s in xi if s.element_type == t) for t in (1, 2, 3, 4)}
    return counts[1] == 1 and all(counts[t] >= n for t, n in MINIMUM.items())


def autosubs(xi: tuple[Slot, ...], bench: tuple[Slot, ...],
             got: dict[int, Actual]) -> tuple[list[Slot], list[tuple[Slot, Slot]]]:
    """The eleven that finally played, and who came on for whom, by FPL's rules."""
    playing = list(xi)
    swaps: list[tuple[Slot, Slot]] = []
    for sub in bench:
        if got[sub.element_id].minutes <= 0:
            continue
        for starter in list(playing):
            if got[starter.element_id].minutes > 0:
                continue
            if (sub.element_type == 1) != (starter.element_type == 1):
                continue
            trial = [sub if s is starter else s for s in playing]
            if _formation_ok(trial):
                playing, swaps = trial, swaps + [(starter, sub)]
                break
    return playing, swaps


def _armband(captain: Slot, vice: Slot, got: dict[int, Actual]) -> Slot:
    """Who actually wore it: the vice-captain when the captain did not play."""
    if got[captain.element_id].minutes <= 0 < got[vice.element_id].minutes:
        return vice
    return captain


def lineup_points(found: Lineup, got: dict[int, Actual]) -> int:
    """What a lineup scored: the eleven that played, plus the armband's extra."""
    playing, _ = autosubs(found.xi, found.bench, got)
    wearer = _armband(found.captain, found.vice, got)
    return sum(got[s.element_id].points for s in playing) + got[wearer.element_id].points


@dataclass(frozen=True)
class CaptainGrade:
    chosen: str
    points: int                    # the armband's extra: the points the pick scored
    rank: int                      # 1 = the best starter that week
    of: int
    best: str
    best_points: int
    vice: str
    vice_points: int
    wore_it: str                   # differs from `chosen` when he did not play

    @property
    def regret(self) -> int:
        return self.best_points - self.points


@dataclass(frozen=True)
class XIGrade:
    points: int
    best_points: int
    swaps: tuple[tuple[str, str], ...]     # (in, out): what the best eleven did differently

    @property
    def regret(self) -> int:
        return self.best_points - self.points


@dataclass(frozen=True)
class BenchGrade:
    came_on: tuple[tuple[str, str, int], ...]    # (in, for, points added)
    gained: int
    best_gained: int
    best_order: tuple[str, ...]

    @property
    def regret(self) -> int:
        return self.best_gained - self.gained


@dataclass(frozen=True)
class PlanGrade:
    moves: tuple[tuple[str, int, str, int], ...]   # (in, points, out, points)
    hit_cost: int
    plan_points: int               # the recommended lineup, after the hit
    nothing_points: int            # the lineup the model would have picked unchanged

    @property
    def gain(self) -> int:
        return self.plan_points - self.nothing_points


@dataclass(frozen=True)
class Grade:
    gameweek: int
    record: instructions.Record
    captain: CaptainGrade
    xi: XIGrade
    bench: BenchGrade
    plan: Optional[PlanGrade]
    plan_note: str = ""


def _captain(found: Lineup, got: dict[int, Actual]) -> CaptainGrade:
    ranked = sorted(found.xi, key=lambda s: -got[s.element_id].points)
    wearer = _armband(found.captain, found.vice, got)
    points = got[wearer.element_id].points
    above = sum(1 for s in found.xi if got[s.element_id].points > points)
    best = ranked[0]
    return CaptainGrade(found.captain.name, points, above + 1, len(found.xi), best.name,
                        got[best.element_id].points, found.vice.name,
                        got[found.vice.element_id].points, wearer.name)


def _xi(found: Lineup, got: dict[int, Actual]) -> XIGrade:
    everyone = [*found.xi, *found.bench]
    candidates = [squads.Candidate(s.element_id, s.name, 0, s.element_type, 0,
                                   float(got[s.element_id].points)) for s in everyone]
    best = squads.best_xi(candidates)
    best_ids = {c.element_id for c in best}
    mine = {s.element_id for s in found.xi}
    by_id = {s.element_id: s for s in everyone}
    came = [by_id[e].name for e in sorted(best_ids - mine)]
    left = [by_id[e].name for e in sorted(mine - best_ids)]
    return XIGrade(sum(got[s.element_id].points for s in found.xi),
                   int(sum(c.xp for c in best)), tuple(zip(came, left)))


def _bench(found: Lineup, got: dict[int, Actual]) -> BenchGrade:
    _, swaps = autosubs(found.xi, found.bench, got)
    came_on = tuple((sub.name, starter.name, got[sub.element_id].points)
                    for starter, sub in swaps)
    keeper = [s for s in found.bench if s.element_type == 1]
    outfield = [s for s in found.bench if s.element_type != 1]
    best_gain, best_order = -1, tuple(s.name for s in found.bench)
    for order in itertools.permutations(outfield):
        trial = (*keeper, *order)
        _, trial_swaps = autosubs(found.xi, trial, got)
        gain = sum(got[sub.element_id].points for _, sub in trial_swaps)
        if gain > best_gain:
            best_gain, best_order = gain, tuple(s.name for s in trial)
    return BenchGrade(came_on, sum(p for _, _, p in came_on), max(best_gain, 0), best_order)


def _capture_of(conn: sqlite3.Connection, snapshot_id: int) -> Optional[warehouse.Capture]:
    row = conn.execute("SELECT id, gameweek, captured_at, kind FROM snapshot WHERE id = ?",
                       (snapshot_id,)).fetchone()
    return None if row is None else warehouse.Capture(
        row["id"], row["gameweek"], row["captured_at"], row["kind"])


def _plan(conn: sqlite3.Connection, record: instructions.Record, model_version: str,
          got: dict[int, Actual]) -> tuple[Optional[PlanGrade], str]:
    """The recommended moves and lineup against doing nothing, in points actually scored."""
    given = record.instructions
    if given.lineup is None:
        return None, "no lineup was recommended"
    plan = given.plan
    taken = given.moves
    capture = _capture_of(conn, record.snapshot_id)
    holding = None if capture is None else held.read(conn, capture)
    if holding is None:
        return None, "the squad the advice was made for is not in the warehouse"
    unchanged, why = instructions.lineup(conn, record.gameweek, model_version, holding,
                                         NO_STANCE)
    if unchanged is None:
        return None, f"doing nothing cannot be graded: {why}"
    needed = [m.in_id for m in taken] + [m.out_id for m in taken]
    got = {**got, **actuals(conn, record.gameweek, [e for e in needed if e not in got])}
    hit = plan.chosen.hit_cost if plan is not None and taken and not plan.chip else 0
    swaps = tuple((m.in_name, got[m.in_id].points, m.out_name, got[m.out_id].points)
                  for m in taken)
    return PlanGrade(swaps, hit, lineup_points(given.lineup, got) - hit,
                     lineup_points(unchanged, got)), ""


def grade(conn: sqlite3.Connection, gameweek: int, model_version: str) -> Optional[Grade]:
    """Grade the last pre-deadline Instructions for the gameweek; None when there are none.

    Only for a finished gameweek (the caller's refusal, `settle`'s): before that every
    player's absent row would read as a zero.
    """
    record = instructions.last_before_deadline(conn, gameweek, model_version)
    if record is None or record.instructions.lineup is None:
        return None
    found = record.instructions.lineup
    ids = [s.element_id for s in (*found.xi, *found.bench)]
    got = actuals(conn, gameweek, ids)
    plan, note = _plan(conn, record, model_version, got)
    return Grade(gameweek, record, _captain(found, got), _xi(found, got),
                 _bench(found, got), plan, note)


def render(gameweek: int, model_version: str, graded: Optional[Grade]) -> str:
    """The grade as the lines `settle` prints, or the fact that there is none."""
    if graded is None:
        return (f"\nInstructions for gameweek {gameweek} (model {model_version}): none were "
                f"stored before its deadline, so there is nothing to grade.")
    record, c, x, b = graded.record, graded.captain, graded.xi, graded.bench
    pts = lambda n: f"{n} point{'' if n == 1 else 's'}"
    lines = [f"\nInstructions graded for gameweek {gameweek} (record #{record.id} of "
             f"{record.created_at:%a %d %b %H:%M} UTC, model {model_version}; in points "
             f"actually scored)"]
    pick = f"{c.chosen} scored {pts(c.points)}"
    if c.wore_it != c.chosen:
        pick = (f"{c.chosen} did not play, so the armband went to {c.wore_it}, who scored "
                f"{pts(c.points)}")
    lines.append(f"  Captain  {pick}: ranked {c.rank} of {c.of} starters. Best was "
                 f"{c.best} with {c.best_points}"
                 + (f", so the choice cost {pts(c.regret)}." if c.regret else ", so it was "
                    "the best choice.") + f" Vice-captain {c.vice} scored {c.vice_points}.")
    swap = "; ".join(f"{i} in for {o}" for i, o in x.swaps)
    lines.append(f"  XI       the recommended eleven scored {x.points}; the best legal eleven "
                 f"from the same fifteen scored {x.best_points}"
                 + (f" ({swap}), a regret of {pts(x.regret)}." if x.regret
                    else ", so it was the best eleven."))
    if b.came_on:
        came = "; ".join(f"{i} came on for {o} ({p})" for i, o, p in b.came_on)
        lines.append(f"  Bench    autosubs added {pts(b.gained)} ({came}); the best order "
                     f"would have added {b.best_gained}"
                     + (f" ({' > '.join(b.best_order)})." if b.regret else
                        ", so the order was the best one."))
    else:
        lines.append(f"  Bench    no substitution was needed; the best order would have "
                     f"added {b.best_gained}"
                     + ("." if not b.best_gained else f" ({' > '.join(b.best_order)})."))
    plan = graded.plan
    if plan is None:
        lines.append(f"  Plan     not graded: {graded.plan_note}.")
    elif not plan.moves:
        lines.append(f"  Plan     the advice was no move, so the plan is doing nothing: "
                     f"{plan.plan_points} points.")
    else:
        made = "; ".join(f"{i} ({ip}) in for {o} ({op})" for i, ip, o, op in plan.moves)
        verdict = (f"beat doing nothing by {pts(plan.gain)}" if plan.gain > 0 else
                   f"lost to doing nothing by {pts(-plan.gain)}" if plan.gain < 0
                   else "matched doing nothing")
        lines.append(f"  Plan     {made}: the lineup scored {plan.plan_points} after a "
                     f"{pts(plan.hit_cost)} hit against {plan.nothing_points} for doing "
                     f"nothing - the plan {verdict}.")
    return "\n".join(lines)

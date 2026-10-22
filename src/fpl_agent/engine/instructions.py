"""Instructions: the one thing a run exists to produce.

For the target gameweek, the complete set of things to do (`CONTEXT.md`, *Instructions*).
This module builds them as one value, and renders that value for the terminal and for the
brief's opening block from the same function, so the two cannot disagree.

The first slice is the lineup: the starting eleven that maximises projected points under a
legal formation, the captain with the existing why line, a vice-captain, and the bench in
the order the automatic substitutions will use it. Later slices add the moves, the chip,
and the stance taken for the owner's position in the league; each extends `Instructions`
and `rows`, and nothing else has to change shape.

Pure over stored rows: the projections of the squad's own capture, read through
`chips.captain_picks`, the same record `recommend` and the chip values read.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import chips
from . import squad as squads
from .held import HeldPlayer, HeldSquad

#: Position labels by element type, in the order a lineup is read.
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


@dataclass(frozen=True)
class Slot:
    """One player in the lineup, with the projection that put him there."""
    element_id: int
    name: str
    element_type: int
    xp: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.xp:.1f})"


@dataclass(frozen=True)
class Lineup:
    """The XI, the armband and the bench, each choice carrying its reason."""
    formation: str                          # "3-4-3": defenders, midfielders, forwards
    xi: tuple[Slot, ...]                    # goalkeeper first, then defence to attack
    captain: Slot
    captain_line: str                       # who, the runner-up, where the armband is now
    captain_why: str                        # the fixture and what the xP is made of
    vice: Slot
    vice_reason: str
    bench: tuple[Slot, ...]                 # first substitute first
    bench_reasons: tuple[str, ...]          # one per bench slot


@dataclass(frozen=True)
class Instructions:
    """What to do for one gameweek. `note` says why there is no lineup, when there is not."""
    gameweek: int
    lineup: Optional[Lineup] = None
    note: str = "not evaluated"


def _slot(player: HeldPlayer, xp: float) -> Slot:
    return Slot(player.element_id, player.name, player.element_type, xp)


def _bench_reasons(bench: tuple[Slot, ...]) -> tuple[str, ...]:
    """Why each bench slot sits where it does, in plain words."""
    ordinal = {1: "first", 2: "second", 3: "last"}
    reasons, outfield = [], 0
    for slot in bench:
        if slot.element_type == 1:
            reasons.append("the reserve goalkeeper is always first on the bench and only "
                           "comes on for a goalkeeper")
            continue
        outfield += 1
        if outfield == 1:
            reasons.append(f"best-projected outfield reserve ({slot.xp:.1f} xP): the first "
                           f"to come on when an outfield starter does not play")
        else:
            reasons.append(f"{ordinal.get(outfield, 'next')} outfield reserve to come on "
                           f"({slot.xp:.1f} xP, below the one before)")
    return tuple(reasons)


def lineup(conn: sqlite3.Connection, gameweek: int, model_version: str,
           holding: HeldSquad) -> tuple[Optional[Lineup], str]:
    """The best legal lineup from the held fifteen, or None and the reason there is none."""
    ids = [p.element_id for p in holding.players]
    week = chips.squad_projections(conn, holding.snapshot_id, model_version, ids,
                                   gameweek, gameweek).get(gameweek, {})
    missing = [p.name for p in holding.players if p.element_id not in week]
    if missing:
        # Absent is not zero: a player with no projection row is a projection that was
        # never made, and a lineup built on a guessed zero would be advice built on nothing.
        return None, (f"no projection for {len(missing)} of your {len(ids)} players "
                      f"({', '.join(missing)}); run `fpl-agent project`")
    candidates = [squads.Candidate(p.element_id, p.name, p.team_id, p.element_type,
                                   p.selling_price, week[p.element_id][0])
                  for p in holding.players]
    chosen = squads.best_xi(candidates)
    by_id = {p.element_id: p for p in holding.players}
    chosen_ids = {c.element_id for c in chosen}
    xi = tuple(_slot(by_id[c.element_id], c.xp)
               for c in sorted(chosen, key=lambda c: (c.element_type, -c.xp)))
    reserves = [_slot(p, week[p.element_id][0]) for p in holding.players
                if p.element_id not in chosen_ids]
    bench = tuple(sorted(reserves, key=lambda s: (s.element_type != 1, -s.xp)))
    counts = {t: sum(1 for s in xi if s.element_type == t) for t in (2, 3, 4)}
    formation = f"{counts[2]}-{counts[3]}-{counts[4]}"

    picks = chips.captain_picks(conn, gameweek, model_version, holding,
                                xi=[by_id[s.element_id] for s in xi])
    captain, runner = picks[0], picks[1]
    holder = next((p.name for p in holding.players if (p.multiplier or 1) > 1), None)
    vice = next(s for s in xi if s.element_id == runner.element_id)
    reason = (f"next-best projected starter ({runner.xp:.1f} xP against "
              f"{captain.xp:.1f}): takes the armband if {captain.name} does not play")
    if runner.team == captain.team:
        reason += (f"; both are {captain.team} players, so a postponement would cost "
                   f"you both")
    return Lineup(
        formation=formation, xi=xi,
        captain=next(s for s in xi if s.element_id == captain.element_id),
        captain_line=chips.captain_line(picks, armband=holder), captain_why=chips.captain_why(picks),
        vice=vice, vice_reason=reason,
        bench=bench, bench_reasons=_bench_reasons(bench)), ""


def build(conn: sqlite3.Connection, gameweek: int, model_version: str,
          holding: Optional[HeldSquad], *, locked: Optional[str] = None) -> Instructions:
    """The Instructions for `gameweek`, or what is stopping them.

    A locked gameweek is never advised for, so it is not even read.
    """
    if locked:
        return Instructions(gameweek, note=f"not evaluated - gameweek {gameweek} is locked")
    if holding is None:
        return Instructions(gameweek, note="not evaluated - no squad captured")
    found, why = lineup(conn, gameweek, model_version, holding)
    if found is None:
        return Instructions(gameweek, note=f"not evaluated - {why}")
    return Instructions(gameweek, found)


def rows(instructions: Instructions) -> list[tuple[str, str]]:
    """The labelled lines both outputs print, in their fixed order.

    The same rows whether or not anything changed since the last run, each saying
    "not evaluated" rather than going missing.
    """
    found = instructions.lineup
    if found is None:
        return [("Instructions", f"for GW{instructions.gameweek}: {instructions.note}"),
                ("Starting XI", instructions.note), ("Captain", instructions.note),
                ("Vice-captain", instructions.note), ("Bench", instructions.note)]
    by_type = {t: [s.label for s in found.xi if s.element_type == t] for t in POSITIONS}
    xi = "; ".join(f"{POSITIONS[t]} {', '.join(by_type[t])}" for t in POSITIONS)
    captain = found.captain_line + (f". {found.captain_why}" if found.captain_why else "")
    bench = "; ".join(f"{n}. {s.name} ({s.xp:.1f}) - {why}" for n, (s, why)
                      in enumerate(zip(found.bench, found.bench_reasons), start=1))
    return [("Instructions", f"for GW{instructions.gameweek}, play {found.formation}"),
            ("Starting XI", f"{found.formation} - {xi}"),
            ("Captain", captain),
            ("Vice-captain", f"{found.vice.label} - {found.vice_reason}"),
            ("Bench", bench)]


def render(instructions: Instructions) -> str:
    """The terminal text: the same rows, as plain lines."""
    width = max(len(label) for label, _ in rows(instructions)) + 1
    return "\n".join(f"{label + ':':<{width}} {text}" for label, text in rows(instructions))

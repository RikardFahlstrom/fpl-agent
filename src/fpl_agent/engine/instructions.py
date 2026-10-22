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

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import chips, storage, warehouse
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
    #: Room for the slices still to come; stored with the record so it can be graded.
    moves: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    chip: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """The stored form: plain JSON types, every key always present."""
        return {"gameweek": self.gameweek, "note": self.note,
                "lineup": None if self.lineup is None else asdict(self.lineup),
                "moves": list(self.moves), "chip": self.chip}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Instructions":
        raw = payload.get("lineup")
        lineup = None
        if raw is not None:
            slot = lambda d: Slot(**d)
            lineup = Lineup(
                formation=raw["formation"], xi=tuple(slot(d) for d in raw["xi"]),
                captain=slot(raw["captain"]), captain_line=raw["captain_line"],
                captain_why=raw["captain_why"], vice=slot(raw["vice"]),
                vice_reason=raw["vice_reason"],
                bench=tuple(slot(d) for d in raw["bench"]),
                bench_reasons=tuple(raw["bench_reasons"]))
        return cls(gameweek=payload["gameweek"], lineup=lineup,
                   note=payload.get("note", "not evaluated"),
                   moves=tuple(payload.get("moves") or ()), chip=payload.get("chip"))


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


# --------------------------------------------------------------------------
# Stored at decision time
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    """One stored run: the Instructions as they were, and where they came from."""
    id: int
    created_at: datetime
    gameweek: int
    model_version: str
    snapshot_id: int
    instructions: Instructions


def store(conn: sqlite3.Connection, instructions: Instructions, *, snapshot_id: int,
          model_version: str, now: Optional[datetime] = None
          ) -> tuple[Optional[int], str]:
    """Append this run's Instructions; returns the record id, or None and why not.

    Nothing is stored when there is no lineup to grade, and nothing is ever stored for a
    gameweek that has finished: a record made after the result is not what the model
    believed before it. Records are only ever appended, so an earlier one - including every
    record of a settled gameweek - is never changed.
    """
    if instructions.lineup is None:
        return None, f"nothing to record: {instructions.note}"
    if warehouse.gameweeks(conn, model_version).get(instructions.gameweek).finished:
        return None, (f"gameweek {instructions.gameweek} has finished; a record made after "
                      f"the result is not a decision-time one")
    stamp = None if now is None else now.isoformat()
    return storage.record_instructions(conn, instructions.gameweek, model_version,
                                       snapshot_id, instructions.to_payload(), stamp), ""


def records(conn: sqlite3.Connection, gameweek: int, model_version: str) -> list[Record]:
    """Every record for the gameweek under the version, oldest first."""
    rows = conn.execute(
        "SELECT * FROM instructions WHERE gameweek = ? AND model_version = ? ORDER BY id",
        (gameweek, model_version)).fetchall()
    return [Record(r["id"], storage.parse_utc(r["created_at"]), r["gameweek"],
                   r["model_version"], r["snapshot_id"],
                   Instructions.from_payload(json.loads(r["payload"]))) for r in rows]


def last_before_deadline(conn: sqlite3.Connection, gameweek: int,
                         model_version: str) -> Optional[Record]:
    """The last record made before the gameweek's deadline: the one that was acted on.

    With no deadline derivable it is the latest record. None when there is none, which a
    caller says rather than inventing a grade.
    """
    deadline = warehouse.deadline(conn, gameweek)
    found = [r for r in records(conn, gameweek, model_version)
             if deadline is None or r.created_at <= deadline]
    return found[-1] if found else None

"""Instructions: the one thing a run exists to produce.

For the target gameweek, the complete set of things to do (`CONTEXT.md`, *Instructions*).
This module builds them as one value, and renders that value for the terminal and for the
brief's opening block from the same function, so the two cannot disagree.

The lineup is the starting eleven that maximises projected points under a legal
formation, the captain with the existing why line, a vice-captain, and the bench in the
order the automatic substitutions will use it. The moves are `moves.plan`: how many
transfers to make, if any, beside doing nothing and the best single swap. The chip is
chosen from *whole alternatives*: no chip, bench boost, triple captain, free hit and
wildcard, each with its own best moves, each scored as net expected points against doing
nothing, so a chip is compared with the moves it replaces rather than valued alone. The
chip's own gates still decide whether it may be played (its bar, and being the best
remaining week within the noise band); the highest-scoring alternative that passes them
wins, else the answer is hold, with the margin to the best alternative. The lineup is
built on the squad *after* the chosen moves, since the transfers happen first. All of it is chosen
under a *stance* (`engine/stance`): Protect or Chase, from the owner's league position.

Pure over stored rows: the projections of the squad's own capture, read through
`chips.captain_picks`, the same record `recommend` and the chip values read.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

from . import chips, moves as movesmod, stance as stancemod, storage, warehouse
from . import squad as squads
from .held import HeldPlayer, HeldSquad
from .projection import HorizonMissing
from .stance import Stance
from .warehouse import Capture

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
    captain_note: str = ""                  # what the stance did to the armband, if it did


@dataclass(frozen=True)
class Alternative:
    """One whole way to play the gameweek, scored against doing nothing."""
    name: str                       # "no chip", "bench boost", ...
    chip: Optional[str]             # FPL's chip name; None for no chip
    total: float                    # net xP: the chip's gain plus its moves, after hits
    playable: bool                  # the chip clears its own gates; True for no chip
    reason: str                     # the chip's verdict in the reader's words
    plan: Optional[movesmod.MovePlan]   # its best moves, None where there are none

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "chip": self.chip, "total": self.total,
                "playable": self.playable, "reason": self.reason,
                "plan": None if self.plan is None else self.plan.to_dict()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Alternative":
        plan = raw.get("plan")
        return cls(raw["name"], raw["chip"], raw["total"], raw["playable"], raw["reason"],
                   None if plan is None else movesmod.MovePlan.from_dict(plan))


@dataclass(frozen=True)
class Instructions:
    """What to do for one gameweek. `note` says why there is no lineup, when there is not."""
    gameweek: int
    lineup: Optional[Lineup] = None
    note: str = "not evaluated"
    #: The transfer plan with its alternatives, or None and `plan_note` saying why.
    plan: Optional[movesmod.MovePlan] = None
    plan_note: str = "not evaluated"
    #: The stance the advice was chosen under, and what it did to this week's advice.
    stance: Stance = stancemod.NONE
    effect: str = ""
    #: The chip to play (FPL's name), or None to hold; the alternatives it was chosen from.
    chip: Optional[str] = None
    chip_note: str = "not evaluated"
    alternatives: tuple[Alternative, ...] = ()
    #: Every chip's verdict on the squad after the no-chip moves: the working behind the
    #: Chips line. Not stored; it is recomputed from the warehouse.
    verdicts: tuple[chips.Verdict, ...] = field(default=(), compare=False)

    @property
    def moves(self) -> tuple[movesmod.Move, ...]:
        """The transfers to make: empty when the advice is no move."""
        return () if self.plan is None else self.plan.moves

    def to_payload(self) -> dict[str, Any]:
        """The stored form: plain JSON types, every key always present."""
        return {"gameweek": self.gameweek, "note": self.note,
                "lineup": None if self.lineup is None else asdict(self.lineup),
                "moves": [asdict(m) for m in self.moves],
                "plan": None if self.plan is None else self.plan.to_dict(),
                "plan_note": self.plan_note, "stance": self.stance.to_dict(),
                "effect": self.effect, "chip": self.chip,
                "chip_note": self.chip_note,
                "alternatives": [a.to_dict() for a in self.alternatives]}

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
                bench_reasons=tuple(raw["bench_reasons"]),
                captain_note=raw.get("captain_note", ""))
        plan = payload.get("plan")
        return cls(gameweek=payload["gameweek"], lineup=lineup,
                   note=payload.get("note", "not evaluated"),
                   plan=None if plan is None else movesmod.MovePlan.from_dict(plan),
                   plan_note=payload.get("plan_note", "not evaluated"),
                   stance=(Stance.from_dict(payload["stance"]) if payload.get("stance")
                           else stancemod.NONE),
                   effect=payload.get("effect", ""),
                   chip=payload.get("chip"),
                   chip_note=payload.get("chip_note", "not evaluated"),
                   alternatives=tuple(Alternative.from_dict(a)
                                      for a in payload.get("alternatives") or ()))


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
           holding: HeldSquad, stance: Stance = stancemod.NONE,
           armband: Optional[str] = None) -> tuple[Optional[Lineup], str]:
    """The best legal lineup from the held fifteen, or None and the reason there is none.

    The XI is the best by expected points. The armband follows the stance: while chasing, a
    captain few rivals hold is worth a little more than his points, while protecting, one
    the field holds is. `armband` names who wears it now, read from the squad as captured:
    a transfer hands the new player the old one's slot, and with it a captaincy he does not
    have."""
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
    plain_best = picks[0]
    picks = sorted(picks, key=lambda p: -(p.xp + stance.bonus(p.element_id)))
    captain, runner = picks[0], picks[1]
    captain_note = ""
    if captain is not plain_best:
        giving = plain_best.xp - captain.xp
        goal = ("a differential" if stance.mode == "chase" else "the field's choice")
        captain_note = (f"The stance chose {captain.name} (rivals' effective ownership "
                        f"{stance.eo(captain.element_id):.0%}) over {plain_best.name} "
                        f"({stance.eo(plain_best.element_id):.0%}), giving up "
                        f"{giving:.1f} xP for {goal}.")
    holder = armband
    vice = next(s for s in xi if s.element_id == runner.element_id)
    reason = (f"next-best projected starter ({runner.xp:.1f} xP against "
              f"{captain.xp:.1f}): takes the armband if {captain.name} does not play")
    if runner.team == captain.team:
        reason += (f"; both are {captain.team} players, so a postponement would cost "
                   f"you both")
    return Lineup(
        formation=formation, xi=xi,
        captain=next(s for s in xi if s.element_id == captain.element_id),
        captain_line=chips.captain_line(picks, armband=holder),
        captain_why=chips.captain_why(picks, short=bool(captain_note)),
        vice=vice, vice_reason=reason,
        bench=bench, bench_reasons=_bench_reasons(bench),
        captain_note=captain_note), ""


def after_moves(holding: HeldSquad, plan: Optional[movesmod.MovePlan]) -> HeldSquad:
    """The held squad once the plan's transfers are made."""
    for move in () if plan is None else plan.moves:
        holding = holding.with_move(move.as_swap())
    return holding


def _plan(conn: sqlite3.Connection, holding: HeldSquad, capture: Optional[Capture],
          gameweek: int, bar: float, model_version: str, **options: Any
          ) -> tuple[Optional[movesmod.MovePlan], str]:
    """The transfer plan, or None and why there is none."""
    if capture is None or capture.gameweek != gameweek:
        return None, (f"not planned - transfers are priced from the latest capture, which "
                      f"targets gameweek {capture.gameweek if capture else 'none'}")
    try:
        return movesmod.plan(conn, holding, capture, bar=bar, model_version=model_version,
                             **options), ""
    except HorizonMissing as e:
        return None, f"not evaluated - the horizon is not projected: {e}"
    except LookupError as e:
        return None, f"not evaluated - {e}"
    except movesmod.NotProven as e:
        return None, (f"not evaluated - the solver did not prove its answer optimal, so "
                      f"there is no move advice ({e})")


def _worth(plan: Optional[movesmod.MovePlan]) -> float:
    """What a plan's moves are worth: its score when it clears its bar, else nothing."""
    return plan.chosen.score if plan is not None and plan.recommended else 0.0


def _wildcard_span(conn: sqlite3.Connection, holding: HeldSquad, capture: Capture,
                   model_version: str) -> int:
    """How many weeks a wildcard rebuild is valued over: its horizon, capped at the wall
    of the chip's window and at what has been projected."""
    wall = chips.window_end(list(holding.chips), capture.gameweek)
    projected = conn.execute(
        "SELECT MAX(gameweek) AS last FROM projection WHERE snapshot_id = ? "
        "AND model_version = ?", (capture.id, model_version)).fetchone()["last"]
    span = chips.WILDCARD_HORIZON
    if wall:
        span = min(span, wall - capture.gameweek + 1)
    if projected:
        span = min(span, projected - capture.gameweek + 1)
    return max(1, span)


def _verdict(verdicts: list[chips.Verdict], name: str) -> Optional[chips.Verdict]:
    return next((v for v in verdicts if v.state.name == name), None)


def _alternatives(conn: sqlite3.Connection, gameweek: int, model_version: str,
                  holding: HeldSquad, capture: Capture, bar: float,
                  none_plan: Optional[movesmod.MovePlan], none_verdicts: list[chips.Verdict],
                  stance: Stance) -> list[Alternative]:
    """Every chip that can be valued, with its best moves, scored against doing nothing.

    A chip that cannot be evaluated - played, not yet open, expired - has no alternative:
    it is reported as it is by the Chips line and is never given a value.
    """
    base = _worth(none_plan)
    found = [Alternative("no chip", None, round(base, 2), True,
                         "moves alone" if none_plan is not None and none_plan.recommended
                         else "no move worth making", none_plan)]
    for verdict in none_verdicts:
        if not verdict.evaluated:
            continue
        name = verdict.state.name
        plan, own, why = none_plan, verdict, ""
        if name == "bboost":
            # With the bench counting in full this week the best moves differ, so they are
            # searched again - and the chip's value is read on the squad they leave.
            plan, why = _plan(conn, holding, capture, gameweek, bar, model_version, hit_cap=None,
                              bench_boost=True, stance=stance)
            if plan is not None:
                after = chips.evaluate(conn, gameweek, model_version,
                                       after_moves(holding, plan))
                own = _verdict(after, name) or verdict
            total = own.now.value + _worth(plan)
        elif name == "3xc":
            # The captain is who the stance would pick, so its adjustment rides on the chip.
            picks = chips.captain_picks(conn, gameweek, model_version, after_moves(
                holding, none_plan))
            pull = stance.bonus(picks[0].element_id) if picks else 0.0
            total = verdict.now.value + pull + base
        elif name in ("freehit", "wildcard"):
            # A rebuild: hits are free and the moves are the rebuild itself. A free hit's
            # squad is for this week only; a wildcard's is kept.
            weeks = 1 if name == "freehit" else _wildcard_span(
                conn, holding, capture, model_version)
            plan, why = _plan(conn, holding, capture, gameweek, 0.0, model_version, weeks=weeks,
                              hit_cap=None, chip=name, stance=stance)
            total = verdict.now.value
        else:
            continue
        playable = own.play_now and (plan is not None or name == "3xc")
        reason = own.reason if plan is not None or name == "3xc" else (
            f"its moves could not be planned: {why}")
        found.append(Alternative(own.title, name, round(total, 2), playable, reason, plan))
    return found


def _choose(alternatives: list[Alternative]) -> tuple[Alternative, str]:
    """The alternative to play, or no chip, and the sentence that says why."""
    none = alternatives[0]
    contenders = [a for a in alternatives[1:] if a.playable and a.total > none.total]
    if contenders:
        best = max(contenders, key=lambda a: a.total)
        rivals = [a for a in alternatives if a is not best]
        runner = max(rivals, key=lambda a: a.total)
        return best, (f"play {best.name}: +{best.total:.1f} xP this week with its moves, "
                      f"{best.total - runner.total:.1f} ahead of the best alternative "
                      f"({runner.name}, +{runner.total:.1f}); {best.reason}")
    chips_only = alternatives[1:]
    if not chips_only:
        return none, "hold - no chip can be valued this gameweek"
    runner = max(chips_only, key=lambda a: a.total)
    gap = runner.total - none.total
    return none, (f"hold - the best alternative, {runner.name}, scores +{runner.total:.1f} "
                  f"xP against +{none.total:.1f} with no chip ({abs(gap):.1f} "
                  f"{'ahead' if gap > 0 else 'behind'}) but is not played: "
                  f"{runner.reason.removeprefix('hold: ')}")


def _effect(conn: sqlite3.Connection, holding: HeldSquad, capture: Optional[Capture],
            gameweek: int, bar: float, model_version: str, stance: Stance, with_stance: Optional[movesmod.MovePlan],
            lineup_found: Optional[Lineup]) -> str:
    """What the stance did to this gameweek's advice, in plain English.

    Found by asking the same question without it: the moves it would have chosen, and
    whether the armband moved. Empty when there is no live stance to have had an effect.
    """
    if not (stance.applied and stance.weight and with_stance is not None):
        return ""
    plain, _ = _plan(conn, holding, capture, gameweek, bar, model_version,
                     stance=Stance("none", stance.weight, applied=False))
    parts = []
    if plain is not None and {m.in_id for m in plain.moves} != {m.in_id for m in with_stance.moves}:
        said = lambda p: (p.chosen.text + f" (+{p.chosen.net:.1f} xP)" if p.recommended
                          else "no move")
        parts.append(f"on moves, without it the advice would be {said(plain)}; with it, "
                     f"{said(with_stance)}")
    if lineup_found is not None and lineup_found.captain_note:
        parts.append(lineup_found.captain_note.rstrip("."))
    return "; ".join(parts) if parts else "none this gameweek: the same moves and captain"


def build(conn: sqlite3.Connection, gameweek: int, model_version: str,
          holding: Optional[HeldSquad], *, capture: Optional[Capture] = None,
          bar: float = 2.0, locked: Optional[str] = None,
          stance: Optional[Stance] = None) -> Instructions:
    """The Instructions for `gameweek`, or what is stopping them.

    A locked gameweek is never advised for, so it is not even read. `stance` is read from
    the warehouse unless one is passed.
    """
    if locked:
        why = f"not evaluated - gameweek {gameweek} is locked"
        return Instructions(gameweek, note=why, plan_note=why, chip_note=why)
    if holding is None:
        why = "not evaluated - no squad captured"
        return Instructions(gameweek, note=why, plan_note=why, chip_note=why)
    if stance is None:
        stance = stancemod.read(conn, holding)
    none_plan, plan_note = _plan(conn, holding, capture, gameweek, bar, model_version,
                                 stance=stance)
    alternatives: list[Alternative] = []
    chosen = None
    verdicts = chips.evaluate(conn, gameweek, model_version,
                              after_moves(holding, none_plan))
    if not verdicts:
        chip_note = "not evaluated - no chips captured (an authenticated snapshot records them)"
    elif none_plan is None:
        # The chips can still be valued on the squad as held, but not compared with moves
        # that could not be planned.
        chip_note = f"not compared - {plan_note}; each chip is valued on the squad as held"
    else:
        alternatives = _alternatives(conn, gameweek, model_version, holding, capture,
                                     bar, none_plan, verdicts, stance)
        chosen, chip_note = _choose(alternatives)
    plan = none_plan if chosen is None else chosen.plan
    chip = None if chosen is None else chosen.chip
    wearing = next((p.name for p in holding.players if (p.multiplier or 1) > 1), None)
    found, why = lineup(conn, gameweek, model_version, after_moves(holding, plan), stance,
                        armband=wearing)
    return Instructions(
        gameweek, found, note="not evaluated" if found else f"not evaluated - {why}",
        plan=plan, plan_note=plan_note, stance=stance,
        effect=_effect(conn, holding, capture, gameweek, bar, model_version, stance,
                       none_plan, found),
        chip=chip, chip_note=chip_note, alternatives=tuple(alternatives),
        verdicts=tuple(verdicts))


def _hits(option: movesmod.Option, plan: movesmod.MovePlan) -> str:
    if plan.chip:
        return f"no hit ({plan.chip})"
    paid = option.hit_cost // plan.hit_price if plan.hit_price else 0
    return ("no hit" if not paid else
            f"after {'a' if paid == 1 else paid} {plan.hit_price}-point "
            f"hit{'' if paid == 1 else 's'}")


def moves_text(instructions: "Instructions") -> str:
    """The Moves line: the plan, or no move with the margin, beside its alternatives."""
    plan = instructions.plan
    if plan is None:
        return instructions.plan_note
    terms = (f"{plan.free_transfers} free transfer{'' if plan.free_transfers == 1 else 's'}, "
             + (f"no hits while the {plan.chip} is played" if plan.chip else
                "no hit cap while a chip is played" if plan.hit_cap is None else
                f"hit cap {plan.hit_cap}"))
    if plan.recommended:
        o = plan.chosen
        weeks = "this gameweek" if plan.weeks == 1 else f"{plan.weeks} gameweeks"
        lead = ("free hit squad for this gameweek only, reverting after it - "
                if plan.chip == "freehit" else
                f"{o.transfers} transfer{'' if o.transfers == 1 else 's'} - ")
        text = (f"{lead}{o.text}: +{o.gross:.1f} xP over {weeks}, net {o.net:+.1f} "
                f"{_hits(o, plan)} ({terms})")
        single = plan.single
        beside = ("best single swap "
                  + (f"{single.text} net {single.net:+.1f}" if single else "none"))
        return f"{text}. Beside it: do nothing 0.0; {beside}"
    best = plan.best_move
    if best is None or best.net <= 0:
        return (f"no move - no transfer improves the squad enough to pay for itself "
                f"({terms})")
    return (f"no move - the best plan, {best.text}, nets {best.net:+.1f} xP "
            f"{_hits(best, plan)}, {plan.bar - best.net:.1f} under the {plan.bar:.1f} bar "
            f"({terms})")


def stance_text(instructions: "Instructions") -> str:
    """The Stance line: the mode, the standing, and the effect on this gameweek."""
    text = instructions.stance.line
    if instructions.effect:
        text += f". Effect: {instructions.effect}"
    return text


def chip_text(instructions: "Instructions") -> str:
    """The Chip line: play one, or hold, and what it was compared with."""
    text = instructions.chip_note
    if instructions.alternatives:
        compared = " · ".join(f"{a.name} {a.total:+.1f}" for a in instructions.alternatives)
        text += f". Compared, as net xP against doing nothing: {compared}"
    return text


def rows(instructions: Instructions) -> list[tuple[str, str]]:
    """The labelled lines both outputs print, in their fixed order.

    The same rows whether or not anything changed since the last run, each saying
    "not evaluated" rather than going missing.
    """
    found = instructions.lineup
    if found is None:
        return [("Instructions", f"for GW{instructions.gameweek}: {instructions.note}"),
                ("Stance", stance_text(instructions)),
                ("Chip", chip_text(instructions)),
                ("Moves", moves_text(instructions)),
                ("Starting XI", instructions.note), ("Captain", instructions.note),
                ("Vice-captain", instructions.note), ("Bench", instructions.note)]
    by_type = {t: [s.label for s in found.xi if s.element_type == t] for t in POSITIONS}
    xi = "; ".join(f"{POSITIONS[t]} {', '.join(by_type[t])}" for t in POSITIONS)
    captain = found.captain_line + (f". {found.captain_why}" if found.captain_why else "")
    if found.captain_note:
        captain += f" {found.captain_note}"
    bench = "; ".join(f"{n}. {s.name} ({s.xp:.1f}) - {why}" for n, (s, why)
                      in enumerate(zip(found.bench, found.bench_reasons), start=1))
    return [("Instructions", f"for GW{instructions.gameweek}, play {found.formation}"),
            ("Stance", stance_text(instructions)),
            ("Chip", chip_text(instructions)),
            ("Moves", moves_text(instructions)),
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

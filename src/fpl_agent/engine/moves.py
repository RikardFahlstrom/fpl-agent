"""Multi-move transfer planning: how many transfers, which ones, and whether to bother.

`recommend` ranks single swaps, each priced as the next transfer you would make. This
chooses a *plan*: zero or more transfers over the horizon, netting out the points hits
the extra ones cost, spending the banked free transfers first and honouring the club
limit. Two coordinated moves can beat the best single swap - a defender and a striker
from the same fixture run, or one upgrade that only fits the budget if a second player is
sold - and a ranking of single swaps cannot see that.

The search is `squad.best_squad`, the exact MILP, run once per transfer count with
`max_changes`: for each k, the best legal squad reachable with at most k players changed,
over the held squad's own budget (selling prices, not purchase prices). Every option is
then scored the same way, as the squad's objective over the horizon - its best legal XI
plus the discounted bench - minus the held squad's, minus the hits. The options always
include **do nothing** and **the best single swap**, so the value of the extra moves is
visible rather than asserted.

A plan is recommended only if its net gain over doing nothing clears the bar (the one the
brief already calls a move worth making). Under it the answer is "no move", with the
margin. The solver either proves its answer optimal or says it did not (`NotProven`): a
partial answer is never presented as advice.

An active transfer chip suspends hits, so the cap is meaningless under it and the plan is
the best squad with no limit on changes.
"""

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Optional

from . import held, squad as squads
from .projection import MODEL_VERSION, stored_horizon
from .stance import Stance
from .warehouse import Capture

#: Paid transfers a plan may make beyond the free ones. A chip lifts it.
HIT_CAP = 2
#: FPL's standing price of a transfer beyond the free ones, when a capture recorded none.
DEFAULT_HIT = 4


class NotProven(RuntimeError):
    """The solver did not establish that its answer is optimal, so there is no advice."""


@dataclass(frozen=True)
class Move:
    """One player out and one in, with the horizon points that justify the swap."""
    out_id: int
    out_name: str
    out_price: int          # tenths of a million: what selling him returns
    out_xp: float
    in_id: int
    in_name: str
    in_team_id: int
    in_cost: int            # tenths of a million: what buying him costs now
    in_xp: float

    def as_swap(self) -> dict[str, Any]:
        """The shape `HeldSquad.with_move` takes."""
        return {"out": {"element_id": self.out_id},
                "in": {"element_id": self.in_id, "name": self.in_name,
                       "team_id": self.in_team_id, "now_cost": self.in_cost}}

    @property
    def text(self) -> str:
        return f"{self.in_name} in for {self.out_name}"


@dataclass(frozen=True)
class Option:
    """One way to spend the transfers, scored the same way as every other."""
    transfers: int
    hit_cost: int           # points, for the transfers beyond the free ones
    gross: float            # expected-points gain over the held squad, before hits
    net: float              # gross less hit_cost: plain expected points
    score: float            # what was maximised: net, plus the stance's adjustment
    moves: tuple[Move, ...]

    @property
    def text(self) -> str:
        return "; ".join(m.text for m in self.moves) or "do nothing"


@dataclass(frozen=True)
class MovePlan:
    free_transfers: int
    hit_price: int
    hit_cap: Optional[int]          # None under a transfer chip: hits do not apply
    bar: float
    weeks: int
    chip: Optional[str]
    options: tuple[Option, ...]     # by transfers ascending; the first is do nothing
    chosen: Option                  # the best by net, do nothing included
    single: Optional[Option]        # the best option that makes exactly one transfer

    @property
    def recommended(self) -> bool:
        """Whether to move at all: only a plan that clears the bar over doing nothing."""
        return self.chosen.transfers > 0 and self.chosen.score >= self.bar

    @property
    def best_move(self) -> Optional[Option]:
        """The best option that makes any transfer, whether or not it clears the bar."""
        moving = [o for o in self.options if o.transfers]
        return max(moving, key=lambda o: o.score) if moving else None

    @property
    def moves(self) -> tuple[Move, ...]:
        return self.chosen.moves if self.recommended else ()

    def to_dict(self) -> dict[str, Any]:
        return {"free_transfers": self.free_transfers, "hit_price": self.hit_price,
                "hit_cap": self.hit_cap, "bar": self.bar, "weeks": self.weeks,
                "chip": self.chip, "recommended": self.recommended,
                "options": [asdict(o) for o in self.options],
                "chosen": asdict(self.chosen),
                "single": None if self.single is None else asdict(self.single)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MovePlan":
        def option(d: dict[str, Any]) -> Option:
            return Option(d["transfers"], d["hit_cost"], d["gross"], d["net"],
                          d.get("score", d["net"]), tuple(Move(**m) for m in d["moves"]))
        return cls(raw["free_transfers"], raw["hit_price"], raw["hit_cap"], raw["bar"],
                   raw["weeks"], raw["chip"], tuple(option(o) for o in raw["options"]),
                   option(raw["chosen"]),
                   None if raw["single"] is None else option(raw["single"]))


def _pair(held_out: list[squads.Candidate], bought: list[squads.Candidate]
          ) -> tuple[Move, ...]:
    """Match each player sold to one bought of the same position, for the reader.

    The solver chooses a squad, not pairings. The best incoming player is shown against
    the weakest outgoing one, which is how a person would read the swap.
    """
    moves = []
    for element_type in squads.SQUAD_SHAPE:
        outs = sorted((c for c in held_out if c.element_type == element_type),
                      key=lambda c: c.xp)
        ins = sorted((c for c in bought if c.element_type == element_type),
                     key=lambda c: -c.xp)
        moves += [Move(o.element_id, o.name, o.cost, round(o.xp, 2), i.element_id, i.name,
                       i.team_id, i.cost, round(i.xp, 2)) for o, i in zip(outs, ins)]
    return tuple(moves)


def plan(conn: sqlite3.Connection, holding: held.HeldSquad, capture: Capture, *,
         bar: float, weeks: int = 3, hit_cap: Optional[int] = HIT_CAP,
         chip: Optional[str] = None, bench_boost: bool = False,
         stance: Optional[Stance] = None, model_version: str = MODEL_VERSION) -> MovePlan:
    """The best transfer plan for the capture's gameweek.

    `hit_cap` None is no cap, for a plan made while a chip is being played. `chip` names a
    transfer chip being played in the plan (free hit, wildcard): hits are free and the
    squad is rebuilt without a limit on changes, as for one already active on the capture.
    `bench_boost` values the bench in full for the target gameweek, which is what a bench
    boost does and why the best moves under it differ. `stance` adds its adjustment to
    every player's points, so the plan prefers differentials while chasing and the field's
    players while protecting; the option's `net` stays plain expected points and `score` is
    what the choice was made on.

    Raises `HorizonMissing` when the horizon was never projected, `LookupError` when a
    held player has no projection, and `NotProven` when the solver could not prove it.
    """
    totals = stored_horizon(conn, capture.id, capture.gameweek, weeks, model_version)
    rows = conn.execute(
        f"""SELECT ps.element_id, ps.now_cost, p.web_name, p.element_type, p.team_id
            FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
            WHERE ps.snapshot_id = ?
              AND ps.status IN ({",".join("?" * len(held.BUYABLE))})""",
        (capture.id, *held.BUYABLE)).fetchall()

    owned = {p.element_id for p in holding.players}
    shape: dict[int, int] = {}
    for p in holding.players:
        shape[p.element_type] = shape.get(p.element_type, 0) + 1
    if shape != squads.SQUAD_SHAPE:
        # Without this the solver finds nothing, and "nothing found" reads as "no move".
        raise LookupError("the held squad is not FPL-shaped (2-5-5-3), so transfers "
                          "cannot be planned against it")
    missing = [p.name for p in holding.players if p.element_id not in totals]
    if missing:
        raise LookupError(f"no projection over the horizon for {', '.join(missing)}")
    # Held players cost what selling them returns; everyone else costs what the market
    # asks. A player the market no longer offers (injured, suspended) can still be kept.
    this_week: dict[int, float] = {}
    if bench_boost:
        this_week = {r["element_id"]: r["expected_points"] for r in conn.execute(
            "SELECT element_id, expected_points FROM projection WHERE snapshot_id = ? "
            "AND gameweek = ? AND model_version = ?",
            (capture.id, capture.gameweek, model_version))}
    def candidate(element_id, name, team_id, element_type, cost, adjusted):
        xp = totals[element_id] + (stance.bonus(element_id, weeks) if adjusted and stance
                                   else 0.0)
        return squads.Candidate(element_id, name, team_id, element_type, cost, xp,
                                this_week.get(element_id, 0.0) * (1 - held.BENCH_VALUE))

    def players(adjusted):
        return ([candidate(p.element_id, p.name, p.team_id, p.element_type,
                           p.selling_price, adjusted) for p in holding.players]
                + [candidate(r["element_id"], r["web_name"], r["team_id"],
                             r["element_type"], r["now_cost"], adjusted)
                   for r in rows if r["element_id"] not in owned
                   and r["element_id"] in totals])

    pool = players(True)
    base = pool[:len(holding.players)]
    plain = {c.element_id: c for c in players(False)}

    def squad_of(chosen):
        return squads.Squad(tuple(chosen), squads.best_xi(chosen), sum(c.cost for c in chosen))

    now = squad_of(base)
    now_plain = squad_of([plain[c.element_id] for c in base])

    free = holding.free_transfers or 0
    price = DEFAULT_HIT if holding.transfer_cost is None else int(holding.transfer_cost)
    chip = chip or holding.active_transfer_chip
    cap = None if chip else hit_cap

    def option(found: squads.Squad) -> Optional[Option]:
        bought = [c for c in found.players if c.element_id not in owned]
        if not bought:
            return None
        sold = [c for c in base if c.element_id not in {p.element_id for p in found.players}]
        paid = 0 if chip else max(0, len(bought) - free)
        adjusted = found.objective - now.objective
        if adjusted <= 1e-9:
            return None         # a swap that changes nothing is not an option
        gross = squad_of([plain[c.element_id] for c in found.players]).objective \
            - now_plain.objective
        return Option(len(bought), paid * price, round(gross, 2),
                      round(gross - paid * price, 2), round(adjusted - paid * price, 2),
                      _pair([plain[c.element_id] for c in sold],
                            [plain[c.element_id] for c in bought]))

    found: dict[int, Option] = {}
    limits = [None] if chip else range(1, 16 if cap is None else min(15, free + cap) + 1)
    for limit in limits:
        try:
            best = squads.best_squad(pool, holding.budget, holding.team_limit,
                                     base=owned, max_changes=limit)
        except RuntimeError as e:
            raise NotProven(str(e)) from e
        if best is None:
            continue
        choice = option(best)
        if choice and (choice.transfers not in found
                       or choice.score + choice.hit_cost
                       > found[choice.transfers].score + found[choice.transfers].hit_cost):
            found[choice.transfers] = choice
    options = (Option(0, 0, 0.0, 0.0, 0.0, ()), *(found[k] for k in sorted(found)))
    chosen = max(options, key=lambda o: (o.score, -o.transfers))
    single = found.get(1)
    return MovePlan(free, price, cap, bar, weeks, chip, options, chosen, single)

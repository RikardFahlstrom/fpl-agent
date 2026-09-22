"""The best legal squad for a budget: what a free hit or a wildcard would buy.

A pure function of values. Candidates arrive with a cost and an objective (projected
points over whatever weeks the caller cares about); the answer is fifteen players that
respect FPL's shape (2 GKP, 5 DEF, 5 MID, 3 FWD), the club limit and the budget, with
the best legal XI picked from them.

**Greedy, then swaps - no solver.** The repo keeps three dependencies on purpose and the
problem is fifteen slots from a few hundred candidates, most of them dominated. The
search starts from the cheapest legal squad (so it is feasible from the first step),
then repeatedly makes the single same-position swap that raises the objective most
while staying legal, until no swap does. That lands within the projection's own error
of the optimum on this size of problem; if it ever visibly does not, an exact solver
can replace `best_squad` behind the same signature without touching a caller.

The objective is the XI's points plus a fraction of the bench's - `BENCH_VALUE`, the
same discount `recommend` applies to a bench upgrade - so a squad is not built with a
£4.0m bench that never plays, nor with fifteen starters it cannot field.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

#: FPL's squad shape by element type (1 GKP, 2 DEF, 3 MID, 4 FWD).
SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}
#: Legal XI formations: (DEF, MID, FWD) with one GKP.
FORMATIONS = [(d, m, f) for d in (3, 4, 5) for m in (2, 3, 4, 5) for f in (1, 2, 3)
              if d + m + f == 10]
DEFAULT_TEAM_LIMIT = 3
#: A bench player only scores through an automatic substitution.
BENCH_VALUE = 0.15


@dataclass(frozen=True)
class Candidate:
    element_id: int
    name: str
    team_id: int
    element_type: int       # 1-4
    cost: int               # tenths of a million, what buying him costs
    xp: float               # the objective: projected points over the caller's weeks


@dataclass(frozen=True)
class Squad:
    players: tuple[Candidate, ...]
    xi: tuple[Candidate, ...]
    cost: int

    @property
    def xi_xp(self) -> float:
        return sum(p.xp for p in self.xi)

    @property
    def bench(self) -> tuple[Candidate, ...]:
        chosen = {p.element_id for p in self.xi}
        return tuple(p for p in self.players if p.element_id not in chosen)

    @property
    def objective(self) -> float:
        return self.xi_xp + BENCH_VALUE * sum(p.xp for p in self.bench)


def best_xi(players: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """The highest-scoring legal XI from a squad, trying every formation."""
    by_type: dict[int, list[Candidate]] = {t: [] for t in SQUAD_SHAPE}
    for p in players:
        by_type.setdefault(p.element_type, []).append(p)
    for group in by_type.values():
        group.sort(key=lambda p: -p.xp)
    best: tuple[Candidate, ...] = ()
    best_xp = -1.0
    for d, m, f in FORMATIONS:
        if len(by_type[1]) < 1 or len(by_type[2]) < d or len(by_type[3]) < m \
                or len(by_type[4]) < f:
            continue
        xi = tuple(by_type[1][:1] + by_type[2][:d] + by_type[3][:m] + by_type[4][:f])
        xp = sum(p.xp for p in xi)
        if xp > best_xp:
            best, best_xp = xi, xp
    return best


def legal(players: Iterable[Candidate], budget: int,
          team_limit: int = DEFAULT_TEAM_LIMIT) -> bool:
    players = list(players)
    counts: dict[int, int] = {}
    clubs: dict[int, int] = {}
    for p in players:
        counts[p.element_type] = counts.get(p.element_type, 0) + 1
        clubs[p.team_id] = clubs.get(p.team_id, 0) + 1
    return (counts == SQUAD_SHAPE and max(clubs.values(), default=0) <= team_limit
            and sum(p.cost for p in players) <= budget
            and len({p.element_id for p in players}) == len(players))


def _make(players: list[Candidate]) -> Squad:
    return Squad(tuple(players), best_xi(players), sum(p.cost for p in players))


def _prune(candidates: Iterable[Candidate], team_limit: int) -> list[Candidate]:
    """Drop every candidate another of the same position and club-agnostic dominates:
    no cheaper-or-equal player with more-or-equal points. Dominated players can still
    matter for the club limit, so the pruning keeps the first `team_limit` per club per
    position regardless."""
    by_type: dict[int, list[Candidate]] = {}
    for c in candidates:
        if c.element_type in SQUAD_SHAPE:
            by_type.setdefault(c.element_type, []).append(c)
    kept = []
    for group in by_type.values():
        group.sort(key=lambda c: (c.cost, -c.xp))
        best_xp_so_far = -1.0
        per_club: dict[int, int] = {}
        for c in group:
            per_club[c.team_id] = per_club.get(c.team_id, 0) + 1
            if c.xp > best_xp_so_far or per_club[c.team_id] <= team_limit:
                kept.append(c)
                best_xp_so_far = max(best_xp_so_far, c.xp)
    return kept


def cheapest_legal(candidates: list[Candidate], budget: int,
                   team_limit: int) -> Optional[list[Candidate]]:
    """A feasible start: the cheapest fifteen that fit the shape and the club limit."""
    chosen: list[Candidate] = []
    clubs: dict[int, int] = {}
    for element_type, needed in SQUAD_SHAPE.items():
        pool = sorted((c for c in candidates if c.element_type == element_type),
                      key=lambda c: (c.cost, -c.xp))
        taken = 0
        for c in pool:
            if clubs.get(c.team_id, 0) >= team_limit:
                continue
            chosen.append(c)
            clubs[c.team_id] = clubs.get(c.team_id, 0) + 1
            taken += 1
            if taken == needed:
                break
        if taken < needed:
            return None
    return chosen if sum(c.cost for c in chosen) <= budget else None


def best_squad(candidates: Iterable[Candidate], budget: int,
               team_limit: int = DEFAULT_TEAM_LIMIT,
               max_rounds: int = 200) -> Optional[Squad]:
    """The best legal squad the budget buys, or None when no legal squad is affordable.

    Cheapest legal squad first, then best-improvement swaps until none improves.
    """
    pool = _prune(candidates, team_limit)
    start = cheapest_legal(pool, budget, team_limit)
    if start is None:
        return None
    current = _make(start)
    for _ in range(max_rounds):
        held = {p.element_id for p in current.players}
        clubs: dict[int, int] = {}
        for p in current.players:
            clubs[p.team_id] = clubs.get(p.team_id, 0) + 1
        best_gain, best_swap = 1e-9, None
        for out in current.players:
            room = budget - current.cost + out.cost
            for inc in pool:
                if inc.element_id in held or inc.element_type != out.element_type:
                    continue
                if inc.cost > room:
                    continue
                after = clubs.get(inc.team_id, 0) + (0 if inc.team_id == out.team_id else 1)
                if after > team_limit:
                    continue
                trial = _make([inc if p is out else p for p in current.players])
                gain = trial.objective - current.objective
                if gain > best_gain:
                    best_gain, best_swap = gain, trial
        if best_swap is None:
            break
        current = best_swap
    return current

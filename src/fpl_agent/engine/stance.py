"""The stance Instructions are chosen under: protect a lead, or chase one.

Instructions are chosen *for the owner's position in the league*, not for expected points
alone (`CONTEXT.md`, *Instructions*). Standing in the first configured league sets a mode:

- **Protect** when first. Stay close to the field: a player most rivals hold is one whose
  haul does not hurt, and a differential is variance against a lead already held.
- **Chase** otherwise. Prefer players few rivals hold, because only a player the field
  lacks can move the owner past it, and pull harder the further behind: the pull scales
  with the gap to first, reaching full strength at `FULL_PULL_GAP` points.

It is one adjustment and one number. Each player is worth his expected points plus
`weight * pull * (1 - effective ownership)` per gameweek: positive for a differential while
chasing, negative while protecting, nothing for a player every rival holds. The effective
ownership is the rivals' (a captain counts twice), so the adjustment is about the league
actually being raced. The same function adjusts the moves, the captain and the chip.

`RANK_WEIGHT` is small because it is uncalibrated: it is a stated assumption, to be fitted
once enough gameweeks have been graded to say what a differential is worth. At zero the
advice is exactly the plain expected-points advice.

Stale or missing ownership is never used: the stance still says where the owner stands, and
says that it was not applied and why.
"""

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .. import config
from . import recommend
from .held import HeldSquad

#: Expected points per player per gameweek that full pull is worth. Uncalibrated.
RANK_WEIGHT = 0.2
#: Points behind the leader at which a chase pulls at full strength.
FULL_PULL_GAP = 100


@dataclass(frozen=True)
class Stance:
    mode: str                       # "protect" | "chase" | "none"
    weight: float = RANK_WEIGHT
    league: Optional[str] = None
    rank: Optional[int] = None
    field: Optional[int] = None     # how many in the league
    gap: Optional[int] = None       # points behind the leader; 0 when leading
    pull: float = 0.0               # -1 protect, 0..1 chase
    applied: bool = False           # ownership was usable, so the adjustment is live
    reason: str = ""                # why there is no stance, or why it was not applied
    #: element id -> the rivals' effective ownership, 0..1+. Empty unless applied.
    ownership: Optional[dict[int, float]] = None

    def bonus(self, element_id: int, weeks: int = 1) -> float:
        """What a player is worth beyond his expected points, over `weeks` gameweeks."""
        if not self.applied or not self.weight:
            return 0.0
        eo = min(1.0, (self.ownership or {}).get(element_id, 0.0))
        return self.weight * self.pull * (1.0 - eo) * weeks

    def eo(self, element_id: int) -> Optional[float]:
        if not self.applied:
            return None
        return min(1.0, (self.ownership or {}).get(element_id, 0.0))

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw.pop("ownership")
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Stance":
        return cls(**raw)

    @property
    def line(self) -> str:
        """The stance in plain English: the mode, the standing, and what it does."""
        if self.mode == "none":
            return f"none - {self.reason}"
        where = (f"{self.rank} of {self.field} in {self.league}"
                 if self.field else f"{self.rank} in {self.league}")
        if self.mode == "protect":
            text = (f"Protect - first ({where}), so the advice stays close to what the "
                    f"field holds and avoids differentials")
        else:
            text = (f"Chase - {where}, {self.gap} point{'' if self.gap == 1 else 's'} behind "
                    f"the leader, so the advice leans toward players few rivals hold "
                    f"(pull {self.pull:.0%} of full)")
        if not self.applied:
            return f"{text}; not applied - {self.reason}"
        if not self.weight:
            return f"{text}; the weight is zero, so it changes nothing"
        return f"{text}; {self.weight:g} xP per player per gameweek at full pull"


NONE = Stance("none", reason="not evaluated")


def read(conn: sqlite3.Connection, holding: Optional[HeldSquad], *,
         weight: float = RANK_WEIGHT) -> Stance:
    """The owner's stance from the first configured league's latest finished standings."""
    leagues = config.rival_leagues()
    if not leagues:
        return Stance("none", weight, reason="no league is configured (rivals.leagues), so "
                                             "there is no field to be ahead of or behind")
    league_id = leagues[0]
    if holding is None or holding.entry_id is None:
        return Stance("none", weight, reason="no squad captured, so the owner's entry is "
                                             "not known")
    name = conn.execute("SELECT name FROM league WHERE id = ?", (league_id,)).fetchone()
    gameweek = conn.execute(
        "SELECT MAX(gameweek) AS gw FROM league_standing WHERE league_id = ? AND finished = 1",
        (league_id,)).fetchone()["gw"]
    if name is None or gameweek is None:
        return Stance("none", weight, reason=f"no finished-round standings are held for "
                                             f"league {league_id}")
    rows = conn.execute(
        "SELECT entry_id, rank, total_points FROM league_standing "
        "WHERE league_id = ? AND gameweek = ? AND finished = 1 ORDER BY rank",
        (league_id, gameweek)).fetchall()
    own = next((r for r in rows if r["entry_id"] == holding.entry_id), None)
    leader = rows[0] if rows else None
    if own is None or own["rank"] is None or own["total_points"] is None or leader is None:
        return Stance("none", weight, reason=f"the owner is not in the gameweek {gameweek} "
                                             f"standings of league {league_id}")
    gap = max(0, leader["total_points"] - own["total_points"])
    mode = "protect" if own["rank"] == 1 else "chase"
    pull = -1.0 if mode == "protect" else min(1.0, gap / FULL_PULL_GAP)

    source, owned = recommend.ownership_source(conn)
    base = dict(mode=mode, weight=weight, league=name["name"], rank=own["rank"],
                field=len(rows), gap=gap, pull=pull)
    if not source.fresh:
        return Stance(**base, applied=False,
                      reason=f"ownership is not usable ({source.reason})")
    return Stance(**base, applied=True,
                  ownership={e: v["effective_ownership"] for e, v in owned.items()})

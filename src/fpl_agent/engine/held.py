"""The held squad: the fifteen you own and the rules they are held under, read once.

One value from one capture - each player's squad position, element type, club, selling
price and armband multiplier; the bank, free transfers and the hit; the chip payload;
and the club limit FPL publishes in `game_config`. The transfer ranking and the chip
evaluation both take this rather than each querying `my_squad`, `my_state` and
`game_config` for themselves, which is how the chip rebuilds came to ignore the club
limit the transfers already honoured.

The club limit is the same kind of rule as the scoring weights: FPL publishes it and can
change it between seasons, so it is read from `game_config` - here, and nowhere else.
"""

import json
import sqlite3
from dataclasses import dataclass, replace
from typing import Any, Optional

from . import squad as squads
from .warehouse import Capture


@dataclass(frozen=True)
class HeldPlayer:
    element_id: int
    name: str
    team_id: int
    element_type: int       # 1-4
    position: int           # squad slot, 1-15; 1-11 start
    selling_price: int      # tenths of a million
    multiplier: Optional[int]   # FPL's: 0 on the bench, 1 starting, 2 or 3 captained


@dataclass(frozen=True)
class HeldSquad:
    snapshot_id: int
    players: tuple[HeldPlayer, ...]      # in squad position order
    bank: int
    free_transfers: Optional[int]        # None when the capture did not record it
    transfer_cost: Optional[int]
    chips: Optional[str]                 # FPL's `my-team` chip payload, verbatim
    team_limit: int                      # FPL's `squad_team_limit`

    @property
    def budget(self) -> int:
        """Bank plus what the held players sell for - never squad value, which is
        purchase-based and overstates it. What a rebuild can spend."""
        return self.bank + sum(p.selling_price for p in self.players)

    def club_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for p in self.players:
            counts[p.team_id] = counts.get(p.team_id, 0) + 1
        return counts

    def with_move(self, move: Optional[dict[str, Any]]) -> "HeldSquad":
        """The squad after a recommended transfer: the incoming player takes the
        outgoing one's slot, bought at today's price, and the bank pays the difference.
        No move, or a move for a player not held, changes nothing.

        `move` is a `recommend.recommend` entry. Swaps are like-for-like, so the
        incoming player keeps the outgoing one's element type.
        """
        if not move:
            return self
        out_id, incoming = move["out"]["element_id"], move["in"]
        out = next((p for p in self.players if p.element_id == out_id), None)
        if out is None:
            return self
        bought = replace(out, element_id=incoming["element_id"], name=incoming["name"],
                         team_id=incoming["team_id"], selling_price=incoming["now_cost"])
        return replace(
            self, players=tuple(bought if p is out else p for p in self.players),
            bank=self.bank + out.selling_price - incoming["now_cost"])


def team_limit(conn: sqlite3.Connection) -> int:
    """FPL's club limit from the latest `game_config`, or the long-standing 3 when none
    was captured or it does not say."""
    row = conn.execute(
        "SELECT rules FROM game_config ORDER BY captured_at DESC LIMIT 1").fetchone()
    if not row:
        return squads.DEFAULT_TEAM_LIMIT
    return int(json.loads(row["rules"]).get("squad_team_limit", squads.DEFAULT_TEAM_LIMIT))


def read(conn: sqlite3.Connection, capture: Capture) -> Optional[HeldSquad]:
    """The capture's held squad, or None when it logged no squad (a market-only capture)."""
    rows = conn.execute(
        """SELECT ms.element_id, ms.position, ms.multiplier, ms.selling_price,
                  p.web_name, p.team_id, p.element_type
           FROM my_squad ms JOIN player p ON p.element_id = ms.element_id
           WHERE ms.snapshot_id = ? ORDER BY ms.position""", (capture.id,)).fetchall()
    if not rows:
        return None
    state = conn.execute("SELECT * FROM my_state WHERE snapshot_id = ?",
                         (capture.id,)).fetchone()
    return HeldSquad(
        snapshot_id=capture.id,
        players=tuple(HeldPlayer(r["element_id"], r["web_name"], r["team_id"],
                                 r["element_type"], r["position"],
                                 r["selling_price"] or 0, r["multiplier"])
                      for r in rows),
        bank=(state["bank"] if state and state["bank"] is not None else 0),
        free_transfers=state["free_transfers"] if state else None,
        transfer_cost=state["transfer_cost"] if state else None,
        chips=state["chips"] if state else None,
        team_limit=team_limit(conn))

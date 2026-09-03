"""Price movement and the affordability window.

FPL publishes a first-party price forecast in `price_change_projections`: an entry per
day ahead, each carrying a projected percent and a likelihood. Prices resolve nightly,
on a clock independent of the gameweek deadline - which is why a transfer can be urgent
days before the deadline, or not urgent at all despite the deadline being close.

The window closes from both ends. The target rises out of reach, and separately a held
player falling shrinks the budget available to buy anyone. `transfers_sell_on_fee` means
a rise in a player you already own returns only half the profit, so the budget grows more
slowly than the market moves.

The `likelihood` scale (-5..+5) is undocumented; it was observed empirically. Thresholds
here are named constants so they can be corrected once realised changes have been
compared against forecasts.
"""

import json
import sqlite3
from dataclasses import dataclass, asdict
from typing import Any, Optional

# Observed range is -5..+5. A rise looks near-certain at 4-5 and probable at 3.
LIKELY_RISE = 3
NEAR_CERTAIN_RISE = 4
LIKELY_FALL = -3
# Price moves in 0.1m steps, held here in FPL's integer tenths.
PRICE_STEP = 1


@dataclass
class PriceOutlook:
    element_id: int
    web_name: str
    now_cost: int
    percent: float
    likelihood: Optional[int]
    locked: bool
    net_transfers: int

    @property
    def rising(self) -> bool:
        return self.likelihood is not None and self.likelihood >= LIKELY_RISE

    @property
    def falling(self) -> bool:
        return self.likelihood is not None and self.likelihood <= LIKELY_FALL

    @property
    def cost_after_change(self) -> int:
        """Price once the forecast change lands."""
        if self.locked:
            return self.now_cost
        if self.rising:
            return self.now_cost + PRICE_STEP
        if self.falling:
            return self.now_cost - PRICE_STEP
        return self.now_cost


def _first_projection(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not entries:
        return None
    # offset 0 is today's tick; that is the one that can close a window tonight.
    return min(entries, key=lambda e: e.get("offset", 0))


def price_outlooks(conn: sqlite3.Connection,
                   snapshot_id: Optional[int] = None) -> dict[int, PriceOutlook]:
    """Price outlook per player, from the most recent snapshot unless told otherwise."""
    if snapshot_id is None:
        row = conn.execute("SELECT MAX(id) AS id FROM snapshot").fetchone()
        snapshot_id = row["id"] if row else None
    if snapshot_id is None:
        return {}

    outlooks: dict[int, PriceOutlook] = {}
    query = """
        SELECT ps.element_id, p.web_name, ps.now_cost, ps.price_change_percent,
               ps.price_change_projections, ps.price_change_locked_until,
               ps.transfers_in_event, ps.transfers_out_event
        FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
        WHERE ps.snapshot_id = ?
    """
    for row in conn.execute(query, (snapshot_id,)):
        projection = _first_projection(row["price_change_projections"])
        likelihood = projection.get("likelihood") if projection else None
        outlooks[row["element_id"]] = PriceOutlook(
            element_id=row["element_id"],
            web_name=row["web_name"],
            now_cost=row["now_cost"] or 0,
            percent=row["price_change_percent"] or 0.0,
            likelihood=likelihood,
            locked=bool(row["price_change_locked_until"]),
            net_transfers=(row["transfers_in_event"] or 0) - (row["transfers_out_event"] or 0),
        )
    return outlooks


@dataclass
class Affordability:
    budget: int              # bank + selling price of the player going out
    cost: int                # target's price now
    margin: int              # budget - cost, in tenths; negative means unaffordable
    margin_after_change: int # the same once the forecast price change lands
    urgency: str             # none | soon | tonight | missed
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess(budget: int, target: PriceOutlook,
           holding: Optional[PriceOutlook] = None) -> Affordability:
    """Whether the target is affordable, and whether that is about to change.

    `budget` is bank plus the selling price of whoever is being sold. `holding` is that
    player, whose own fall would shrink the budget before the transfer is made.
    """
    margin = budget - target.now_cost
    budget_after = budget
    if holding is not None and holding.falling and not holding.locked:
        # Selling price drops with the player's price, so waiting costs budget too.
        budget_after -= PRICE_STEP
    margin_after = budget_after - target.cost_after_change

    if margin < 0:
        return Affordability(budget, target.now_cost, margin, margin_after, "missed",
                             f"{target.web_name} already costs more than the budget")

    if target.locked:
        return Affordability(budget, target.now_cost, margin, margin_after, "none",
                             f"{target.web_name} has already moved today and is locked")

    if margin_after < 0:
        squeeze = []
        if target.rising:
            squeeze.append(f"{target.web_name} is rising (likelihood {target.likelihood}, "
                           f"{target.percent:.0f}% of the way)")
        if holding is not None and holding.falling:
            squeeze.append(f"{holding.web_name} is falling, shrinking the budget")
        urgency = "tonight" if target.likelihood is not None and \
            target.likelihood >= NEAR_CERTAIN_RISE else "soon"
        return Affordability(budget, target.now_cost, margin, margin_after, urgency,
                             "; ".join(squeeze) + " - the window closes at the next price tick")

    if target.rising:
        return Affordability(budget, target.now_cost, margin, margin_after, "soon",
                             f"{target.web_name} is rising but stays affordable "
                             f"(margin £{margin_after / 10:.1f}m after)")

    return Affordability(budget, target.now_cost, margin, margin_after, "none",
                         "no price pressure")

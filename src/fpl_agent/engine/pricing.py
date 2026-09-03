"""Price movement and the affordability window.

FPL publishes a first-party price forecast in `price_change_projections`: an entry per
day ahead, each carrying a projected percent and a likelihood. Prices resolve nightly,
on a clock independent of the gameweek deadline - which is why a transfer can be urgent
days before the deadline, or not urgent at all despite the deadline being close.

The window closes from both ends. The target rises out of reach, and separately a held
player falling shrinks the budget available to buy anyone. `transfers_sell_on_fee` means
a rise in a player you already own returns only half the profit, so the budget grows more
slowly than the market moves.

FPL documents the rule on its own Price Changes page:

    Progress shows how far a player has currently moved towards a price change.
    Predicted Progress estimates where they will be by the time of the next update.
    When Predicted Progress exceeds 100%, the player is considered Very Likely to
    rise or fall.

So the signal is `projected_percent` crossing 100, not the `likelihood` field.
`likelihood` turns out to be a derived ordinal band of the same number, confirmed
against a full snapshot:

    likelihood  +-5   |projected| >= 100      "Very Likely"
    likelihood  +-4   95.0 .. 99.4
    likelihood  +-3   40.0 .. 94.7
    likelihood  +-2   20.0 .. 39.7
    likelihood  +-1    0.1 .. 19.6
    likelihood    0   exactly 0

Driving the logic off `projected_percent` therefore follows FPL's documented rule
directly, rather than off a derived field whose banding could be re-cut at any time.

FPL is explicit that these are "a guide, not a guarantee", and that team news and the
gameweek deadline matter too - which is why a closing price window is reported next to
the projection rather than folded into it.
"""

import json
import sqlite3
from dataclasses import dataclass, asdict
from typing import Any, Optional

# FPL's documented rule: Predicted Progress over 100% is "Very Likely" to change.
VERY_LIKELY_PROGRESS = 100.0
# Below that, a change is plausible at the following tick rather than this one. 95 is
# where FPL's own banding puts the next step down (likelihood +-4).
APPROACHING_PROGRESS = 95.0
# Price moves in 0.1m steps, held here in FPL's integer tenths.
PRICE_STEP = 1


@dataclass
class PriceOutlook:
    element_id: int
    web_name: str
    now_cost: int
    percent: float               # Progress: how far the player has already moved
    projected_percent: float     # Predicted Progress at the next update
    likelihood: Optional[int]
    locked: bool
    net_transfers: int

    @property
    def status(self) -> str:
        """FPL's own wording for where this player sits."""
        if self.locked:
            return "locked"
        if self.projected_percent >= VERY_LIKELY_PROGRESS:
            return "very likely to rise"
        if self.projected_percent <= -VERY_LIKELY_PROGRESS:
            return "very likely to fall"
        if self.projected_percent >= APPROACHING_PROGRESS:
            return "approaching a rise"
        if self.projected_percent <= -APPROACHING_PROGRESS:
            return "approaching a fall"
        return "stable"

    @property
    def rising(self) -> bool:
        """Very Likely to rise at the next update, by FPL's documented threshold."""
        return not self.locked and self.projected_percent >= VERY_LIKELY_PROGRESS

    @property
    def falling(self) -> bool:
        return not self.locked and self.projected_percent <= -VERY_LIKELY_PROGRESS

    @property
    def approaching_rise(self) -> bool:
        """Close enough to matter at the update after this one."""
        return (not self.locked
                and APPROACHING_PROGRESS <= self.projected_percent < VERY_LIKELY_PROGRESS)

    @property
    def cost_after_change(self) -> int:
        """Price once the forecast change lands.

        Only a Very Likely change moves the price. Treating a player 40% of the way
        through as a rise would predict changes that mostly do not happen.
        """
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
        try:
            projected = float(projection["projected_percent"]) if projection else 0.0
        except (TypeError, ValueError, KeyError):
            projected = 0.0
        outlooks[row["element_id"]] = PriceOutlook(
            element_id=row["element_id"],
            web_name=row["web_name"],
            now_cost=row["now_cost"] or 0,
            percent=row["price_change_percent"] or 0.0,
            projected_percent=projected,
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
            squeeze.append(f"{target.web_name} is {target.status} "
                           f"({target.projected_percent:.0f}% predicted progress, "
                           f"{target.percent:.0f}% so far)")
        if holding is not None and holding.falling:
            squeeze.append(f"{holding.web_name} is {holding.status}, shrinking the budget")
        return Affordability(budget, target.now_cost, margin, margin_after, "tonight",
                             "; ".join(squeeze) + " - the window closes at the next update")

    if target.rising:
        return Affordability(budget, target.now_cost, margin, margin_after, "soon",
                             f"{target.web_name} is {target.status} but stays affordable "
                             f"(margin £{margin_after / 10:.1f}m after)")

    if target.approaching_rise:
        return Affordability(budget, target.now_cost, margin, margin_after, "soon",
                             f"{target.web_name} is {target.status} "
                             f"({target.projected_percent:.0f}% predicted progress) - "
                             f"not tonight, but watch the next update")

    return Affordability(budget, target.now_cost, margin, margin_after, "none",
                         "no price pressure")

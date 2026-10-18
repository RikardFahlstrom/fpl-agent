"""Hedging against the field: what selling one, two or three players buys.

`recommend` prices single like-for-like swaps, so it can never say "sell a midfielder
and a forward to afford the premium striker your whole league owns". This asks that
directly. For every player your leagues hold that you do not, and for one, two and
three transfers, it solves for the best legal squad within reach of the one you have,
with that player in it.

The field is the rivals' latest captured picks, taken as the picks they will keep:
nobody sells a fit player. A rival's player who is injured, suspended or otherwise
not `held.BUYABLE` is assumed sold, so he counts as owned by nobody.

Two numbers describe each option, kept apart as `recommend` keeps gain and urgency
apart. The gain is expected points over the horizon, captaincy included, net of the
points hit. The distance is how far your side is from the field's: the sum over
players of how many multipliers (1 starting, 2 captained) you hold him at against how
many the field does on average. A hedge is an option that shrinks the distance, and
what it costs in gain is what the hedge costs.

    fpl-agent hedge                  # every template player you do not own
    fpl-agent hedge --player Haaland
"""

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import config
from . import held, recommend, storage, warehouse
from . import squad as squads
from .projection import HORIZON_GAMEWEEKS, HorizonMissing, stored_horizon

logger = logging.getLogger("fpl_hedge")

MAX_TRANSFERS = 3
# Template players ranked by what the field gets from them (its multipliers times their
# projection); a template goalkeeper on your bench is not the gap that is costing you.
MAX_TARGETS = 3
# The target of the search with nobody forced in.
ANY_PLAYER = "best available"

# Two captains this close in expected points are a coin flip for the model, so the
# league decides: behind, take the one fewer rivals are captaining.
CAPTAIN_MARGIN = 0.5


@dataclass(frozen=True)
class Standing:
    """Where you sit in the league the rivals were captured from."""
    rank: int
    of: int
    behind: int          # points to the leader; 0 when leading


@dataclass(frozen=True)
class Option:
    target: str
    rivals_holding: str                    # "4 of 5", the target's owners in the field
    field_multiplier: float                # his average multiplier across the field
    changes: int                           # transfers actually made
    out: tuple[tuple[str, int], ...]       # (name, selling price) of each sale
    into: tuple[tuple[str, int], ...]      # (name, price) of each purchase
    bank_after: int
    hit: int
    gain: float                            # net xP over the horizon, captaincy in, after hit
    distance_before: float
    distance_after: float
    captain: str
    captain_why: str


@dataclass(frozen=True)
class Report:
    gameweek: int
    horizon: int
    free_transfers: Optional[int]
    standing: Optional[Standing]
    ownership_gameweek: Optional[int]
    unavailable: Optional[str]             # why no hedge could be computed, in words
    captain_now: str = ""
    options: tuple[Option, ...] = ()
    targets: tuple[str, ...] = field(default_factory=tuple)

    @property
    def recommended(self) -> Optional[Option]:
        """The option with the best net gain: the cheapest way to hold the field's
        player, by the points it costs. No risk weight is invented to trade it off."""
        return max(self.options, key=lambda o: o.gain, default=None)


def standing(conn: sqlite3.Connection, entry_id: Optional[int],
             league_ids: Optional[list[int]]) -> Optional[Standing]:
    """Your place in the latest captured table of the configured leagues, or of the
    first league known when none is configured. None when you were never in one."""
    if entry_id is None:
        return None
    scope, params = "", []
    if league_ids:
        scope = f"WHERE league_id IN ({','.join('?' * len(league_ids))})"
        params = list(league_ids)
    row = conn.execute(
        f"""SELECT league_id, MAX(gameweek) AS gameweek FROM league_standing {scope}
            GROUP BY league_id ORDER BY league_id LIMIT 1""", params).fetchone()
    if row is None:
        return None
    table = conn.execute(
        """SELECT entry_id, total_points FROM league_standing
           WHERE league_id = ? AND gameweek = ? ORDER BY total_points DESC""",
        (row["league_id"], row["gameweek"])).fetchall()
    mine = next((i for i, r in enumerate(table) if r["entry_id"] == entry_id), None)
    if mine is None:
        return None
    return Standing(mine + 1, len(table),
                    max(0, table[0]["total_points"] - table[mine]["total_points"]))


def captain_choice(week_xp: dict[int, float], captain_share: dict[int, float],
                   behind: bool) -> tuple[int, str]:
    """The armband for the gameweek: the best expected points, unless it is a coin
    flip and you are behind, when it goes to whoever fewer rivals are captaining.

    Matching a field that captains one player cannot close a gap to it; only a
    different armband can, and at a margin this thin it costs the model nothing.
    """
    ranked = sorted(week_xp, key=lambda e: -week_xp[e])
    best = ranked[0]
    if behind:
        close = [e for e in ranked if week_xp[best] - week_xp[e] <= CAPTAIN_MARGIN]
        pick = min(close, key=lambda e: (captain_share.get(e, 0.0), -week_xp[e]))
        if pick != best:
            return pick, (f"within {CAPTAIN_MARGIN} xP of the best and you are behind, "
                          f"so the armband avoids the field's "
                          f"({captain_share.get(best, 0.0):.0%} captain the other)")
    return best, "best expected points"


def distance(squad: squads.Squad, captain: int, field_eo: dict[int, float]) -> float:
    """The L1 distance between your multipliers and the field's average ones."""
    mine = {p.element_id: 1.0 for p in squad.xi}
    mine[captain] = 2.0
    return sum(abs(mine.get(e, 0.0) - field_eo.get(e, 0.0))
               for e in set(mine) | set(field_eo))


def hit_for(changes: int, free: Optional[int], per_transfer: int, chip: Optional[str]) -> int:
    if chip:
        return 0
    return per_transfer * max(0, changes - (free or 0))


def hedge(conn: sqlite3.Connection, weeks: int = HORIZON_GAMEWEEKS,
          max_transfers: int = MAX_TRANSFERS,
          players: Optional[list[str]] = None,
          max_targets: int = MAX_TARGETS) -> Report:
    """Every hedge worth showing: per target player, the best squad within one, two
    and three transfers. Reads stored projections; it writes nothing.
    """
    target = warehouse.require_target(conn)
    capture = target.capture
    holding = held.read(conn, capture)
    if holding is None:
        raise LookupError(
            "no squad captured; an authenticated snapshot is needed to hedge")
    league_ids = config.rival_leagues()
    place = standing(conn, holding.entry_id, league_ids)
    context = recommend.transfer_context(conn)
    free, chip = context["free_transfers"], context["chip"]
    per_transfer = (context["transfer_cost"] if context["transfer_cost"] is not None
                    else recommend.DEFAULT_TRANSFER_COST)

    def report(why: Optional[str], **kw) -> Report:
        return Report(capture.gameweek, weeks, free, place, kw.pop("gw", None), why, **kw)

    source, ownership = recommend.ownership_source(conn)
    if not ownership:
        return report(source.reason or "no rival picks are captured")

    totals = stored_horizon(conn, capture.id, capture.gameweek, weeks)
    weekly = {gw: stored_horizon(conn, capture.id, gw, 1)
              for gw in range(capture.gameweek, capture.gameweek + weeks)}

    market = conn.execute(
        """SELECT ps.element_id, ps.now_cost, p.web_name, p.element_type, p.team_id,
                   ps.status
            FROM player_snapshot ps JOIN player p ON p.element_id = ps.element_id
            WHERE ps.snapshot_id = ?""", (capture.id,)).fetchall()
    status = {r["element_id"]: r["status"] for r in market}
    owned = {p.element_id for p in holding.players}

    # Expected multipliers of the field: its picks, minus whoever it must sell.
    field_eo = {e: v["effective_ownership"] for e, v in ownership.items()
                if status.get(e) in held.BUYABLE}
    captain_share = {e: v["effective_ownership"] - v["ownership"]
                     for e, v in ownership.items()}

    pool = [squads.Candidate(p.element_id, p.name, p.team_id, p.element_type,
                             p.selling_price, totals.get(p.element_id, 0.0))
            for p in holding.players]
    pool += [squads.Candidate(r["element_id"], r["web_name"], r["team_id"],
                              r["element_type"], r["now_cost"],
                              totals.get(r["element_id"], 0.0))
             for r in market if r["element_id"] not in owned
             and r["status"] in held.BUYABLE and r["element_id"] in totals]
    names = {c.element_id: c.name for c in pool}
    behind = bool(place and place.behind)

    def lineup_value(squad: squads.Squad) -> tuple[float, int, str]:
        """Objective with the armband in, the armband holder, and why."""
        xi_ids = [p.element_id for p in squad.xi]
        first = {e: weekly[capture.gameweek].get(e, 0.0) for e in xi_ids}
        armband, why = captain_choice(first, captain_share, behind)
        bonus = sum(max(weekly[gw].get(e, 0.0) for e in xi_ids) for gw in weekly)
        return squad.objective + bonus, armband, why

    mine = [c for c in pool if c.element_id in owned]
    base = squads.Squad(tuple(mine), squads.best_xi(mine), sum(c.cost for c in mine))
    base_value, base_captain, _ = lineup_value(base)
    base_distance = distance(base, base_captain, field_eo)

    if players:
        wanted = {n.casefold() for n in players}
        targets = [c for c in pool if c.element_id not in owned
                   and c.name.casefold() in wanted]
        missing = wanted - {c.name.casefold() for c in targets}
        if missing:
            return report(f"no buyable player named {', '.join(sorted(missing))}",
                          gw=source.gameweek)
    else:
        targets = sorted((c for c in pool if c.element_id not in owned
                          and field_eo.get(c.element_id, 0.0) >= recommend.TEMPLATE_EO),
                         key=lambda c: -field_eo[c.element_id] * c.xp)[:max_targets]

    # Named players are the question; otherwise the template players are, and so is
    # the whole market with nobody forced in (None), which says what the best squad
    # within reach is whoever the field owns.
    searches = list(targets) if players else [*targets, None]
    options, seen = [], set()
    for cand in searches:
        for k in range(1, max_transfers + 1):
            best = squads.best_squad(pool, holding.budget, holding.team_limit,
                                     base=owned, max_changes=k,
                                     required={cand.element_id} if cand else ())
            if best is None:
                continue
            bought = [p for p in best.players if p.element_id not in owned]
            sold = [p for p in holding.players
                    if p.element_id not in {q.element_id for q in best.players}]
            key = (cand.element_id if cand else None,
                   frozenset(p.element_id for p in bought))
            if key in seen:         # a looser limit found nothing a tighter one had not
                continue
            seen.add(key)
            value, armband, why = lineup_value(best)
            hit = hit_for(len(bought), free, per_transfer, chip)
            entry = cand and ownership.get(cand.element_id)
            options.append(Option(
                target=cand.name if cand else ANY_PLAYER,
                rivals_holding=(f"{entry['owned_by']} of {entry['managers']}"
                                if entry else "0"),
                field_multiplier=round(field_eo.get(cand.element_id, 0.0), 2)
                if cand else 0.0,
                changes=len(bought),
                out=tuple((p.name, p.selling_price) for p in sold),
                into=tuple((p.name, p.cost) for p in bought),
                bank_after=holding.budget - best.cost, hit=hit,
                gain=round(value - base_value - hit, 2),
                distance_before=round(base_distance, 2),
                distance_after=round(distance(best, armband, field_eo), 2),
                captain=names[armband], captain_why=why))

    return Report(capture.gameweek, weeks, free, place, source.gameweek, None,
                  captain_now=names.get(base_captain, "?"), options=tuple(options),
                  targets=tuple(c.name for c in targets))


def _money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def render(report: Report) -> str:
    lines = [f"Hedge for gameweek {report.gameweek} "
             f"({report.horizon}-gameweek horizon, "
             f"{report.free_transfers if report.free_transfers is not None else '?'} "
             f"free transfers)"]
    if report.standing:
        s = report.standing
        lines.append(f"League: {s.rank} of {s.of}, "
                     f"{'leading' if not s.behind else f'{s.behind} points behind the leader'}")
    if report.unavailable:
        lines.append(f"No hedge: {report.unavailable}.")
        return "\n".join(lines)
    lines.append(f"Field: rivals' picks for gameweek {report.ownership_gameweek}, "
                 f"assumed kept unless the player is out.")
    if not report.options:
        lines.append("No template player is missing from your squad, or none is "
                     "reachable within the transfer limit.")
        return "\n".join(lines)
    for o in report.options:
        lines.append("")
        who = (f"{o.target} (rivals holding {o.rivals_holding}, field multiplier "
               f"{o.field_multiplier:.1f}x, yours 0x)" if o.target != ANY_PLAYER
               else "Best squad whoever the field owns")
        lines.append(f"{who}: {o.changes} transfer{'s' if o.changes > 1 else ''}"
                     f"{f', hit -{o.hit}' if o.hit else ', no hit'}")
        lines.append("  out: " + ", ".join(f"{n} {_money(c)}" for n, c in o.out))
        lines.append("  in:  " + ", ".join(f"{n} {_money(c)}" for n, c in o.into)
                     + f"   (bank {_money(o.bank_after)})")
        lines.append(f"  net {o.gain:+.2f} xP over {report.horizon} gameweeks; distance "
                     f"from the field {o.distance_before:.1f} -> {o.distance_after:.1f}")
        lines.append(f"  captain {o.captain} ({o.captain_why})")
    best = report.recommended
    lines.append("")
    lines.append(f"Recommended: {best.target} via {best.changes} transfer"
                 f"{'s' if best.changes > 1 else ''} - "
                 f"{'gains' if best.gain >= 0 else 'costs'} {abs(best.gain):.2f} xP, "
                 f"closes the distance {best.distance_before:.1f} -> "
                 f"{best.distance_after:.1f}. Captain {best.captain}.")
    lines.append(f"Today's captain: {report.captain_now}. Nothing here is executed; "
                 f"`make record` only after you make the transfers.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show what selling 1-3 players buys against the field.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--weeks", type=int, default=HORIZON_GAMEWEEKS)
    parser.add_argument("--max-transfers", type=int, default=MAX_TRANSFERS)
    parser.add_argument("--targets", type=int, default=MAX_TARGETS,
                        help="how many template players to price (default %(default)s)")
    parser.add_argument("--player", action="append",
                        help="hedge into this player (web name); default: every "
                             "template player you do not own")
    args = parser.parse_args(argv)

    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    conn = storage.connect(args.db)
    try:
        try:
            result = hedge(conn, args.weeks, args.max_transfers, args.player,
                           args.targets)
        except HorizonMissing as missing:
            print(f"\nCannot hedge: {missing}", file=sys.stderr)
            return 1
        print(render(result))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

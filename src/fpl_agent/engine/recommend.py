"""Transfer recommendations, and the decision log they are written to.

A recommendation carries two independent numbers. Expected-points gain over the horizon
says whether the move is worth making; price urgency says whether the chance to make it
is about to disappear. They are kept apart deliberately - a modest upgrade whose window
closes tonight is a different situation from a big upgrade you can take your time over,
and one score cannot express both.

The gain is quoted twice, gross and net of the points hit the move would cost. Both are
needed: a net of -2.8 that is +1.2 gross is a real upgrade you cannot afford this week,
while +1.2 gross under an active wildcard is simply +1.2. Ranking is on the net, because
a move that does not survive its own hit is not a recommendation.

Decisions are written to the `decision` table and exported to logs/actions.jsonl, which
is the version-controlled record. Appending there produces a pure-addition diff. The
file and its directory are created by the first --record run; until one has happened
there is nothing to commit, which is why neither is in the checkout.

    fpl-agent recommend                 # show recommendations
    fpl-agent recommend --record        # and log the top one as a decision
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from . import pricing, rivals, storage
from .projection import (HORIZON_GAMEWEEKS, MODEL_VERSION, HorizonMissing,
                         stored_horizon)
from .scoring import POSITIONS

logger = logging.getLogger("fpl_recommend")

ACTIONS_LOG = Path("logs/actions.jsonl")
DEFAULT_TEAM_LIMIT = 3

# Effective ownership *within your leagues*, not globally. Above the template line a
# player is owned by so much of your field that not owning him is itself the risk;
# below the differential line he is where an edge can come from.
TEMPLATE_EO = 0.50
DIFFERENTIAL_EO = 0.15

# Squad positions 1-11 start; 12-15 are the bench.
STARTING_XI = 11
# A bench player only scores through an automatic substitution, so improving a bench
# slot is worth a fraction of the same upgrade in the XI. Without this the recommender
# happily proposes a large "gain" on a reserve goalkeeper, which returns nothing.
# A stated assumption, to be fitted once outcomes exist.
BENCH_VALUE = 0.15

# FPL's standing hit, used only when a snapshot recorded no `transfers.cost`.
DEFAULT_TRANSFER_COST = 4


def ownership_profile(effective_ownership: Optional[float]) -> str:
    """Classify a player by how much of your league owns him.

    None means rivals have never been captured. It does not mean nobody owns him:
    once rival squads exist, a player absent from every one of them is owned by 0%,
    which is the strongest differential there is - not missing data.
    """
    if effective_ownership is None:
        return "unknown"
    if effective_ownership >= TEMPLATE_EO:
        return "template"
    if effective_ownership <= DIFFERENTIAL_EO:
        return "differential"
    return "balanced"


def _squad(conn: sqlite3.Connection, snapshot_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT ms.*, p.web_name, p.element_type, p.team_id
           FROM my_squad ms JOIN player p ON p.element_id = ms.element_id
           WHERE ms.snapshot_id = ? ORDER BY ms.position""",
        (snapshot_id,),
    ).fetchall()


def _state(conn: sqlite3.Connection, snapshot_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM my_state WHERE snapshot_id = ?", (snapshot_id,)).fetchone()


def _team_limit(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT rules FROM game_config ORDER BY captured_at DESC LIMIT 1").fetchone()
    if not row:
        return DEFAULT_TEAM_LIMIT
    return int(json.loads(row["rules"]).get("squad_team_limit", DEFAULT_TEAM_LIMIT))


def active_transfer_chip(chips_json: Optional[str]) -> Optional[str]:
    """Name the transfer chip in play, if any.

    `my_state.chips` is FPL's own `my-team` payload, stored verbatim: a list of objects
    carrying `name` ("wildcard", "freehit", "bboost", "3xc"), `chip_type` ("transfer" or
    "team") and `status_for_entry`, which is one of "unavailable", "available", "active"
    or "played". "active" - not "played" - is the state during the gameweek the chip is
    being used in, so that is the value to match.

    Only a *transfer* chip suspends hits. An active bench boost or triple captain is a
    team chip: it changes what the squad scores, not what a move costs. Keying on
    `chip_type` rather than on the two known names keeps that distinction if FPL adds
    another chip, with the names as a fallback for payloads that omit the type.
    """
    if not chips_json:
        return None
    try:
        chips = json.loads(chips_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(chips, list):
        return None
    for chip in chips:
        if not isinstance(chip, dict) or chip.get("status_for_entry") != "active":
            continue
        if chip.get("chip_type") == "transfer" or chip.get("name") in ("wildcard", "freehit"):
            return chip.get("name")
    return None


def transfer_price(free_transfers: Optional[int], transfer_cost: Optional[int],
                   chip: Optional[str]) -> int:
    """What the next transfer costs in points, given the state of the squad.

    Every recommendation in the list is priced as *the next transfer you would make*,
    never as the nth move of a plan. The list is a ranked set of mutually exclusive
    alternatives - you take one of them - so with one free transfer every option on it
    is free, and with none every option carries the full hit. Charging option 2 as
    though option 1 had already been taken would bill a hit nobody is going to pay and
    understate every alternative below the first. Multi-transfer planning, where the
    running cost genuinely accumulates, is a different tool and out of scope here.

    `free_transfers` is `limit - made` as the snapshot stored it. Banking is not
    modelled: this season's rolling allowance is already reflected in `limit`.

    Unknown free transfers are priced as none. A snapshot that failed to record
    `transfers.limit` is not evidence that a free transfer exists, and assuming one you
    may not have is exactly how a net -2.8 move gets ranked first.
    """
    if chip:
        return 0
    if free_transfers and free_transfers > 0:
        return 0
    return DEFAULT_TRANSFER_COST if transfer_cost is None else int(transfer_cost)


def transfer_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """The transfer budget the latest snapshot's recommendations are priced against."""
    snapshot = conn.execute(
        "SELECT id FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not snapshot:
        raise LookupError("no snapshot captured yet")
    state = _state(conn, snapshot["id"])
    free = state["free_transfers"] if state else None
    cost = state["transfer_cost"] if state else None
    chip = active_transfer_chip(state["chips"] if state else None)
    return {"free_transfers": free, "transfer_cost": cost, "chip": chip,
            "hit_cost": transfer_price(free, cost, chip)}


def recommend(conn: sqlite3.Connection, weeks: int = HORIZON_GAMEWEEKS,
              limit: int = 10) -> list[dict[str, Any]]:
    """Rank transfers by net gain over the horizon, flagging closing windows.

    Net means after the points hit the move would cost. See `transfer_price` for why
    every option on the list is charged the same hit rather than the first being free.

    Reads the projections `project` stored; it does not run them. Ranking is a read of
    the warehouse, not a write to it, so `recommend` can be run twice without leaving a
    second set of rows behind under whatever MODEL_VERSION the code has moved on to.
    An unprojected horizon is an error, not a cue to project - see `stored_horizon`.

    Candidates include the *doubtful* as well as the fully available. FPL's `d` status
    always comes with a `chance_of_playing_next_round`, and `projection.availability`
    already scales the whole projection by exactly that percentage: a 75% doubt is
    worth 75% of his points before he reaches this list. Filtering him out as well
    charged the same doubt twice and then rounded it to zero - the same double-count
    that was taking predicted starters out of the XI in `lineups`. Statuses with no
    percentage to scale by (`i`, `s`, `u`, `n` - injured, suspended, unavailable, on
    loan) project to zero anyway and are excluded outright, so the whitelist stays
    explicit rather than trusting a status code FPL has not invented yet.
    """
    snapshot = conn.execute(
        "SELECT id, gameweek FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not snapshot:
        raise LookupError("no snapshot captured yet")

    if snapshot["gameweek"] is None:
        raise LookupError("no target gameweek on the latest snapshot; the season may be over")

    squad = _squad(conn, snapshot["id"])
    if not squad:
        raise LookupError(
            "no squad captured; an authenticated snapshot is needed to recommend transfers")
    state = _state(conn, snapshot["id"])
    bank = (state["bank"] if state and state["bank"] is not None else 0)

    context = transfer_context(conn)
    hit_cost, chip = context["hit_cost"], context["chip"]

    totals = stored_horizon(conn, snapshot["id"], snapshot["gameweek"], weeks)
    outlooks = pricing.price_outlooks(conn, snapshot["id"])
    team_limit = _team_limit(conn)

    # Ownership comes from the most recent gameweek rivals were captured for; squads are
    # only public once a gameweek has started, so this necessarily lags the target one.
    row = conn.execute("SELECT MAX(gameweek) AS gw FROM rival_squad").fetchone()
    ownership = (
        rivals.league_ownership(conn, row["gw"], rivals.configured_league_ids())
        if row and row["gw"] else {}
    )

    owned = {row["element_id"] for row in squad}
    club_counts: dict[int, int] = {}
    for row in squad:
        club_counts[row["team_id"]] = club_counts.get(row["team_id"], 0) + 1

    # 'a' available, 'd' doubtful. See the docstring for why the doubtful belong here:
    # their projection has already been cut by FPL's own percentage.
    candidates = conn.execute(
        """SELECT ps.element_id, ps.now_cost, p.web_name, p.element_type, p.team_id,
                  t.short_name AS team, ps.status,
                  ps.chance_of_playing_next_round AS chance
           FROM player_snapshot ps
           JOIN player p ON p.element_id = ps.element_id
           JOIN team t ON t.id = p.team_id
           WHERE ps.snapshot_id = ? AND ps.status IN ('a', 'd')""",
        (snapshot["id"],),
    ).fetchall()

    recommendations = []
    for out_row in squad:
        starts = (out_row["position"] or 99) <= STARTING_XI
        slot_value = 1.0 if starts else BENCH_VALUE
        out_xp = totals.get(out_row["element_id"], 0.0)
        selling = out_row["selling_price"] or 0
        budget = bank + selling
        out_hold = outlooks.get(out_row["element_id"])

        for cand in candidates:
            if cand["element_id"] in owned:
                continue
            if cand["element_type"] != out_row["element_type"]:
                continue          # squad structure is fixed; swaps are like-for-like
            # The club limit counts the squad after the swap.
            after = club_counts.get(cand["team_id"], 0) + (
                -1 if cand["team_id"] == out_row["team_id"] else 0)
            if after >= team_limit:
                continue

            target = outlooks.get(cand["element_id"])
            if target is None:
                continue
            affordability = pricing.assess(budget, target, holding=out_hold)
            if affordability.margin < 0:
                continue

            raw_gain = totals.get(cand["element_id"], 0.0) - out_xp
            if raw_gain <= 0:
                continue
            # What the swap is actually worth, given the slot it lands in.
            gain = raw_gain * slot_value
            # ...and what is left of it once the move has paid for itself.
            net = gain - hit_cost
            if net <= 0:
                continue

            # With rivals captured, absence from every squad is 0% ownership - the
            # strongest differential - rather than an absence of information.
            def eo(element_id: int) -> Optional[float]:
                if not ownership:
                    return None
                entry = ownership.get(element_id)
                return entry["effective_ownership"] if entry else 0.0

            in_eo = eo(cand["element_id"])
            out_eo = eo(out_row["element_id"])

            recommendations.append({
                "gameweek": snapshot["gameweek"],
                "horizon": weeks,
                "out": {"element_id": out_row["element_id"], "name": out_row["web_name"],
                        "selling_price": selling, "xp": round(out_xp, 2),
                        "slot": "xi" if starts else "bench",
                        "league_eo": round(out_eo, 3) if out_eo is not None else None,
                        "profile": ownership_profile(out_eo)},
                "in": {"element_id": cand["element_id"], "name": cand["web_name"],
                       "team": cand["team"], "now_cost": cand["now_cost"],
                       "xp": round(totals.get(cand["element_id"], 0.0), 2),
                       # A doubt already discounted the xP above; it is carried through
                       # so the reader is told, not so it can be charged again.
                       "status": cand["status"], "chance": cand["chance"],
                       "league_eo": round(in_eo, 3) if in_eo is not None else None,
                       "profile": ownership_profile(in_eo)},
                "xp_delta": round(gain, 2),
                "raw_xp_delta": round(raw_gain, 2),
                "net_xp_delta": round(net, 2),
                "hit_cost": hit_cost,
                "free_transfers": context["free_transfers"],
                "chip": chip,
                "urgency": affordability.urgency,
                "affordability": affordability.as_dict(),
            })

    # Best net gain first; among comparable gains a closing window breaks the tie.
    # The hit is the same for every option, so it cannot reorder the list - what it
    # does is drop the moves that do not clear it, above.
    urgency_rank = {"tonight": 0, "soon": 1, "none": 2, "missed": 3}
    recommendations.sort(
        key=lambda r: (-r["net_xp_delta"], urgency_rank.get(r["urgency"], 9)))
    return recommendations[:limit]


def banner(context: dict[str, Any]) -> str:
    """What the whole list is priced against, in one line.

    It comes first and it is not decoration. Under an active transfer chip nothing
    below is charged a hit and single like-for-like swaps say much less than usual,
    because a wildcard rebuilds the squad rather than replacing one player. With no
    free transfer every line has already been docked four points. A reader who sees
    the ranking without this line is reading a different recommendation.
    """
    if context["chip"]:
        return (f"{context['chip'].upper()} ACTIVE this gameweek: transfers cost "
                f"nothing, so no hit is charged below. Bear in mind these are single "
                f"like-for-like swaps ranked one at a time - a wildcard rebuilds the "
                f"whole squad, which this tool does not plan.")
    if context["hit_cost"]:
        why = ("no free transfers left"
               if context["free_transfers"] == 0
               else "free transfers not recorded in this snapshot, so assumed none")
        return (f"{why}: every move below is charged a {context['hit_cost']}-point "
                f"hit and ranked on the net gain.")
    count = context["free_transfers"]
    return (f"{count} free transfer{'' if count == 1 else 's'}: no hit. Each "
            f"option is priced as the one move you make, not as a running plan.")


def render(context: dict[str, Any], recommendations: list[dict[str, Any]],
           weeks: int = HORIZON_GAMEWEEKS) -> str:
    """The banner and the ranked list, as text.

    One renderer, because there is one recommendation. The CLI prints this and the
    MCP tool returns it; neither formats a list of its own, so the two interfaces
    cannot drift into giving the same warehouse two different answers.
    """
    lines = ["", banner(context)]

    if not recommendations:
        tail = (f" that survives a {context['hit_cost']}-point hit"
                if context["hit_cost"] else "")
        lines.append(
            f"No transfer improves the squad over the horizon within budget{tail}.")
        return "\n".join(lines)

    lines += ["", f"Transfer candidates over the next {weeks} gameweeks", ""]
    for i, r in enumerate(recommendations, 1):
        flag = {"tonight": "ACT TONIGHT", "soon": "watch price",
                "none": "", "missed": "out of reach"}[r["urgency"]]
        bench = "  [bench slot]" if r["out"]["slot"] == "bench" else ""
        lines.append(f"{i}. {r['in']['name']} ({r['in']['team']}) "
                     f"£{r['in']['now_cost'] / 10:.1f}m  for  {r['out']['name']} "
                     f"£{r['out']['selling_price'] / 10:.1f}m{bench}")
        worth = (f"+{r['xp_delta']} xP over {weeks}gw "
                 f"({r['out']['xp']} -> {r['in']['xp']})")
        if r["out"]["slot"] == "bench":
            worth += (f", discounted from +{r['raw_xp_delta']} because the bench "
                      f"only scores through substitutions")
        if r["hit_cost"]:
            worth += (f"; net +{r['net_xp_delta']} after the "
                      f"{r['hit_cost']}-point hit")
        lines.append(f"   {worth}" + (f"   [{flag}]" if flag else ""))
        if r["urgency"] in ("tonight", "soon"):
            lines.append(f"   {r['affordability']['reason']}")

        notes = []
        if r["in"].get("status") == "d":
            notes.append(f"doubtful: {r['in']['chance']}% chance of playing, "
                         f"already priced into the xP above")
        if r["in"]["league_eo"] is not None:
            notes.append(f"in: {r['in']['profile']} "
                         f"({r['in']['league_eo'] * 100:.0f}% EO in your leagues)")
        if r["out"]["profile"] == "template":
            notes.append(f"selling {r['out']['name']}, owned by "
                         f"{r['out']['league_eo'] * 100:.0f}% of your leagues - "
                         f"a haul costs you ground")
        if notes:
            lines.append(f"   {' | '.join(notes)}")
    return "\n".join(lines)


def record_decision(conn: sqlite3.Connection, recommendation: dict[str, Any],
                    kind: str = "transfer", status: str = "proposed") -> int:
    # The `xp_delta` column keeps its meaning - gross gain - so existing rows stay
    # comparable; the net and the hit ride along in the payload JSON and are spelled
    # out in the rationale, which is the part a human reads back.
    # A recommendation with no `hit_cost` key was not priced at all; say nothing rather
    # than assert a free transfer that was never established.
    hit = recommendation.get("hit_cost")
    if recommendation.get("chip"):
        cost_note = f" (no hit: {recommendation['chip']} active)"
    elif hit:
        cost_note = f" (net {recommendation.get('net_xp_delta')} after a {hit}-point hit)"
    elif hit == 0:
        cost_note = " (free transfer)"
    else:
        cost_note = ""
    rationale = (
        f"{recommendation['in']['name']} over {recommendation['out']['name']}: "
        f"+{recommendation['xp_delta']} xP over {recommendation['horizon']} gameweeks"
        f"{cost_note}; {recommendation['affordability']['reason']}"
    )
    cur = conn.execute(
        """INSERT INTO decision (created_at, gameweek, model_version, kind, payload,
                                 rationale, urgency, xp_delta, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), recommendation["gameweek"],
         MODEL_VERSION, kind, json.dumps(recommendation, sort_keys=True), rationale,
         recommendation["urgency"], recommendation["xp_delta"], status),
    )
    conn.commit()
    return cur.lastrowid


def export_actions(conn: sqlite3.Connection, path: Path = ACTIONS_LOG) -> int:
    """Write the decision log to JSONL.

    Decisions are only ever appended, so rewriting the file still produces an
    addition-only diff, and the file stays a faithful export of the table.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute("SELECT * FROM decision ORDER BY id").fetchall()
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps({
                "id": row["id"], "ts": row["created_at"], "gw": row["gameweek"],
                "model_version": row["model_version"], "kind": row["kind"],
                "urgency": row["urgency"], "xp_delta": row["xp_delta"],
                "status": row["status"], "rationale": row["rationale"],
                "payload": json.loads(row["payload"]),
            }, sort_keys=True) + "\n")
    return len(rows)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Recommend transfers for the horizon.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--weeks", type=int, default=HORIZON_GAMEWEEKS)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--record", action="store_true",
                        help="log the top recommendation as a proposed decision")
    parser.add_argument("--actions-log", type=Path, default=ACTIONS_LOG)
    args = parser.parse_args(argv)

    config.load()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    conn = storage.connect(args.db)
    try:
        context = transfer_context(conn)
        try:
            recommendations = recommend(conn, args.weeks, args.top)
        except HorizonMissing as missing:
            # Not a crash to fix: the run order was wrong. Say which command was skipped.
            print(f"\nCannot recommend: {missing}", file=sys.stderr)
            return 1

        print(render(context, recommendations, args.weeks))
        if not recommendations:
            return 0

        if args.record:
            decision_id = record_decision(conn, recommendations[0])
            written = export_actions(conn, args.actions_log)
            print(f"\nrecorded decision {decision_id}; "
                  f"{written} decisions in {args.actions_log}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

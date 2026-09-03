"""Transfer recommendations, and the decision log they are written to.

A recommendation carries two independent numbers. Expected-points gain over the horizon
says whether the move is worth making; price urgency says whether the chance to make it
is about to disappear. They are kept apart deliberately - a modest upgrade whose window
closes tonight is a different situation from a big upgrade you can take your time over,
and one score cannot express both.

Decisions are written to the `decision` table and exported to logs/actions.jsonl, which
is the version-controlled record. Appending there produces a pure-addition diff.

    python -m fpl_agent.recommend                 # show recommendations
    python -m fpl_agent.recommend --record        # and log the top one as a decision
"""

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import pricing, storage
from .projection import HORIZON_GAMEWEEKS, MODEL_VERSION, project_horizon
from .scoring import POSITIONS

logger = logging.getLogger("fpl_recommend")

ACTIONS_LOG = Path("logs/actions.jsonl")
DEFAULT_TEAM_LIMIT = 3


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


def recommend(conn: sqlite3.Connection, weeks: int = HORIZON_GAMEWEEKS,
              limit: int = 10) -> list[dict[str, Any]]:
    """Rank transfers by projected gain over the horizon, flagging closing windows."""
    snapshot = conn.execute(
        "SELECT id, gameweek FROM snapshot ORDER BY id DESC LIMIT 1").fetchone()
    if not snapshot:
        raise LookupError("no snapshot captured yet")

    squad = _squad(conn, snapshot["id"])
    if not squad:
        raise LookupError(
            "no squad captured; an authenticated snapshot is needed to recommend transfers")
    state = _state(conn, snapshot["id"])
    bank = (state["bank"] if state and state["bank"] is not None else 0)

    totals = project_horizon(conn, snapshot["gameweek"], weeks)
    outlooks = pricing.price_outlooks(conn, snapshot["id"])
    team_limit = _team_limit(conn)

    owned = {row["element_id"] for row in squad}
    club_counts: dict[int, int] = {}
    for row in squad:
        club_counts[row["team_id"]] = club_counts.get(row["team_id"], 0) + 1

    candidates = conn.execute(
        """SELECT ps.element_id, ps.now_cost, p.web_name, p.element_type, p.team_id,
                  t.short_name AS team
           FROM player_snapshot ps
           JOIN player p ON p.element_id = ps.element_id
           JOIN team t ON t.id = p.team_id
           WHERE ps.snapshot_id = ? AND ps.status = 'a'""",
        (snapshot["id"],),
    ).fetchall()

    recommendations = []
    for out_row in squad:
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

            gain = totals.get(cand["element_id"], 0.0) - out_xp
            if gain <= 0:
                continue

            recommendations.append({
                "gameweek": snapshot["gameweek"],
                "horizon": weeks,
                "out": {"element_id": out_row["element_id"], "name": out_row["web_name"],
                        "selling_price": selling, "xp": round(out_xp, 2)},
                "in": {"element_id": cand["element_id"], "name": cand["web_name"],
                       "team": cand["team"], "now_cost": cand["now_cost"],
                       "xp": round(totals.get(cand["element_id"], 0.0), 2)},
                "xp_delta": round(gain, 2),
                "urgency": affordability.urgency,
                "affordability": affordability.as_dict(),
            })

    # Best gain first; among comparable gains a closing window breaks the tie.
    urgency_rank = {"tonight": 0, "soon": 1, "none": 2, "missed": 3}
    recommendations.sort(
        key=lambda r: (-r["xp_delta"], urgency_rank.get(r["urgency"], 9)))
    return recommendations[:limit]


def record_decision(conn: sqlite3.Connection, recommendation: dict[str, Any],
                    kind: str = "transfer", status: str = "proposed") -> int:
    rationale = (
        f"{recommendation['in']['name']} over {recommendation['out']['name']}: "
        f"+{recommendation['xp_delta']} xP over {recommendation['horizon']} gameweeks; "
        f"{recommendation['affordability']['reason']}"
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

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    conn = storage.connect(args.db)
    try:
        recommendations = recommend(conn, args.weeks, args.top)
        if not recommendations:
            print("No transfer improves the squad over the horizon within budget.")
            return 0

        print(f"\nTransfer candidates over the next {args.weeks} gameweeks\n")
        for i, r in enumerate(recommendations, 1):
            flag = {"tonight": "ACT TONIGHT", "soon": "watch price",
                    "none": "", "missed": "out of reach"}[r["urgency"]]
            print(f"{i}. {r['in']['name']} ({r['in']['team']}) "
                  f"£{r['in']['now_cost'] / 10:.1f}m  for  {r['out']['name']} "
                  f"£{r['out']['selling_price'] / 10:.1f}m")
            print(f"   +{r['xp_delta']} xP over {args.weeks}gw "
                  f"({r['out']['xp']} -> {r['in']['xp']})"
                  + (f"   [{flag}]" if flag else ""))
            if r["urgency"] in ("tonight", "soon"):
                print(f"   {r['affordability']['reason']}")

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

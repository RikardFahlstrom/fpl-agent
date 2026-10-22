"""Record what you actually did: the moves, the captain and the chip.

Recording is a claim about what the owner did, never a step in a plan, so nothing is
recorded unless this command is run and it records exactly what it is told:

    fpl-agent record --move 1 --move 3      # made moves 1 and 3 of the plan, not move 2
    fpl-agent record --all                  # made the whole plan, and says so
    fpl-agent record --captain Haaland      # the armband went on him
    fpl-agent record --chip bboost          # played this chip (`none`: held)

Moves are numbered as the stored Instructions list them. A bare `record` records nothing
and says how to; there is no default that assumes the plan was followed. The captain and
the chip are stored as their own rows with the recommended ones beside them, so a
deviation - the plan said hold, the owner played the chip; the plan said one captain, the
armband went elsewhere - can be graded against what was actually done.

Rows are appended to the `decision` table and exported to `logs/actions.jsonl`, which stays
append-only. Each carries the id of the Instructions record it was read against.
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
from . import held, instructions, recommend, storage, warehouse
from .projection import MODEL_VERSION

logger = logging.getLogger("fpl_record")

CHIPS = ("bboost", "3xc", "freehit", "wildcard", "none")

EXIT_OK = 0
EXIT_NOTHING_ASKED = 2
EXIT_CANNOT = 1


class CannotRecord(Exception):
    """The claim cannot be recorded as stated; nothing is written."""


def _insert(conn: sqlite3.Connection, gameweek: int, kind: str, payload: dict[str, Any],
            rationale: str, xp_delta: float = 0.0) -> int:
    cur = conn.execute(
        """INSERT INTO decision (created_at, gameweek, model_version, kind, payload,
                                 rationale, urgency, xp_delta, status)
           VALUES (?, ?, ?, ?, ?, ?, 'none', ?, 'made')""",
        (datetime.now(timezone.utc).isoformat(), gameweek, MODEL_VERSION, kind,
         json.dumps(payload, sort_keys=True), rationale, xp_delta))
    return cur.lastrowid


def _move_decision(record: instructions.Record, index: int) -> dict[str, Any]:
    """One planned move as the dict `recommend.record_decision` writes."""
    plan = record.instructions.plan
    move = record.instructions.moves[index - 1]
    total = len(record.instructions.moves)
    return {
        "gameweek": record.gameweek, "horizon": plan.weeks,
        "out": {"element_id": move.out_id, "name": move.out_name,
                "selling_price": move.out_price, "xp": move.out_xp},
        "in": {"element_id": move.in_id, "name": move.in_name, "team_id": move.in_team_id,
               "now_cost": move.in_cost, "xp": move.in_xp},
        "xp_delta": round(move.in_xp - move.out_xp, 2), "urgency": "none",
        "affordability": {"reason": f"made from Instructions #{record.id}, move {index} "
                                    f"of {total}"},
        "instructions_id": record.id, "move": index, "moves_in_plan": total,
        "plan_net_xp": plan.chosen.net, "plan_hit_cost": plan.chosen.hit_cost,
        "chip": plan.chip,
    }


def _resolve_captain(name: str, holding: held.HeldSquad,
                     record: instructions.Record) -> tuple[int, str]:
    """The player named, among those the owner can have captained: the captured squad and
    the players the plan brings in. Exact (any case) or a single prefix; else refused."""
    candidates = {p.element_id: p.name for p in holding.players}
    candidates.update({m.in_id: m.in_name for m in record.instructions.moves})
    wanted = name.strip().lower()
    exact = [(e, n) for e, n in candidates.items() if n.lower() == wanted]
    found = exact or [(e, n) for e, n in candidates.items() if n.lower().startswith(wanted)]
    if len(found) != 1:
        raise CannotRecord(
            f"{name!r} is " + ("ambiguous" if found else "not in the squad")
            + f" (squad: {', '.join(sorted(candidates.values()))})")
    return found[0]


def record(conn: sqlite3.Connection, *, moves: list[int], everything: bool,
           captain: Optional[str], chip: Optional[str]) -> list[str]:
    """Write the claims and return a line per row written. Writes nothing if any claim fails.

    Every claim is checked before the first row is written, so a typo in the third cannot
    leave the first two recorded.
    """
    target = warehouse.require_target(conn)
    stored = instructions.records(conn, target.gameweek, MODEL_VERSION)
    if not stored:
        raise CannotRecord(f"no Instructions are stored for gameweek {target.gameweek}; "
                           f"run `make now` first, so there is a plan to record against")
    latest = stored[-1]
    plan = latest.instructions.moves
    if everything and moves:
        raise CannotRecord("--all and --move say different things; give one")
    if everything:
        if not plan:
            raise CannotRecord("the plan has no moves, so there is no plan to say you made")
        moves = list(range(1, len(plan) + 1))
    for index in moves:
        if not 1 <= index <= len(plan):
            raise CannotRecord(f"move {index} is not in the plan, which has "
                               f"{len(plan)} move{'' if len(plan) == 1 else 's'}")
    if moves and latest.instructions.plan.chip == "freehit":
        raise CannotRecord("the plan's moves are a free hit squad, which reverts after the "
                           "gameweek; record the chip, not the moves")
    if len(set(moves)) != len(moves):
        raise CannotRecord("a move was named twice")
    holding = held.read(conn, target.capture)
    captain_pick = None
    if captain:
        if holding is None:
            raise CannotRecord("no squad captured, so the captain cannot be checked")
        captain_pick = _resolve_captain(captain, holding, latest)

    written = []
    for index in sorted(moves):
        decision_id = recommend.record_decision(conn, _move_decision(latest, index))
        move = plan[index - 1]
        written.append(f"decision {decision_id}: {move.text} (move {index} of {len(plan)})")
    lineup = latest.instructions.lineup
    if captain_pick:
        recommended = None if lineup is None else lineup.captain
        payload = {"instructions_id": latest.id,
                   "actual": {"element_id": captain_pick[0], "name": captain_pick[1]},
                   "recommended": None if recommended is None else {
                       "element_id": recommended.element_id, "name": recommended.name}}
        same = recommended is not None and recommended.element_id == captain_pick[0]
        rationale = (f"captain played: {captain_pick[1]}; recommended: "
                     f"{recommended.name if recommended else 'none'}"
                     + ("" if same or recommended is None else " (a deviation)"))
        written.append(f"decision {_insert(conn, target.gameweek, 'captain', payload, rationale)}: "
                       f"{rationale}")
    if chip is not None:
        actual = None if chip == "none" else chip
        payload = {"instructions_id": latest.id, "actual": actual,
                   "recommended": latest.instructions.chip}
        said = lambda c: c or "none"
        rationale = (f"chip played: {said(actual)}; recommended: "
                     f"{said(latest.instructions.chip)}"
                     + ("" if actual == latest.instructions.chip else " (a deviation)"))
        written.append(f"decision {_insert(conn, target.gameweek, 'chip', payload, rationale)}: "
                       f"{rationale}")
    conn.commit()
    return written


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record what you actually did this gameweek. A claim, never a step in "
                    "a plan: nothing is recorded that is not named here.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--move", type=int, action="append", default=[], metavar="N",
                        help="you made move N of the plan (repeat for several)")
    parser.add_argument("--all", dest="everything", action="store_true",
                        help="you made every move of the plan")
    parser.add_argument("--captain", metavar="NAME", help="who you actually captained")
    parser.add_argument("--chip", choices=CHIPS,
                        help="the chip you actually played, or `none`")
    parser.add_argument("--actions-log", type=Path, default=recommend.ACTIONS_LOG)
    args = parser.parse_args(argv)

    if not (args.move or args.everything or args.captain or args.chip):
        print("nothing recorded: say what you did - `--move N` for each move you made, "
              "`--all` if you made the whole plan, `--captain NAME`, `--chip CHIP|none`.",
              file=sys.stderr)
        return EXIT_NOTHING_ASKED

    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    conn = storage.connect(args.db)
    try:
        try:
            lines = record(conn, moves=args.move, everything=args.everything,
                           captain=args.captain, chip=args.chip)
        except (CannotRecord, LookupError) as e:
            print(f"nothing recorded: {e}", file=sys.stderr)
            return EXIT_CANNOT
        total = recommend.export_actions(conn, args.actions_log)
        print("\n".join(lines))
        print(f"{total} decisions in {args.actions_log}")
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

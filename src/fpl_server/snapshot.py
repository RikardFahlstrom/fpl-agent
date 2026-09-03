"""Capture FPL state into the warehouse.

Run daily. Price changes resolve nightly (~01:30 UK), so a weekly capture would miss the
price dynamics entirely - and none of it can be recovered after the fact.

    python -m fpl_server.snapshot                 # capture, skip if already done today
    python -m fpl_server.snapshot --force         # capture regardless
    python -m fpl_server.snapshot --backfill      # also pull per-gameweek actuals
    python -m fpl_server.snapshot --backfill-only # actuals only, no new snapshot
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from . import storage
from .client import FPLClient
from .state import store

logger = logging.getLogger("fpl_snapshot")

# element-summary is one request per player; this bounds concurrency so a backfill of
# ~650 players does not open 650 sockets against the FPL API at once.
BACKFILL_CONCURRENCY = 8


async def capture(conn, client: FPLClient, *, kind: str = "manual") -> int:
    """Capture one point-in-time snapshot. Returns the snapshot id."""
    bootstrap = await client.get_bootstrap_data()
    fixtures = await client.get_fixtures()

    snapshot_id = storage.create_snapshot(conn, bootstrap, kind=kind)
    storage.upsert_teams(conn, bootstrap)
    storage.upsert_players(conn, bootstrap)
    players = storage.record_player_snapshot(conn, snapshot_id, bootstrap)
    storage.record_game_config(conn, bootstrap)
    fixture_count = storage.upsert_fixtures(conn, fixtures)

    logger.info(
        "snapshot %s: %s players, %s fixtures, gameweek %s",
        snapshot_id, players, fixture_count, storage.target_gameweek(bootstrap),
    )

    # The authenticated squad is optional: an unauthenticated run still captures the
    # market, which is the part that cannot be recovered later.
    entry_id = store.get_user_entry_id(client) if client.user_info else None
    if entry_id:
        try:
            my_team = await client.get_my_team(entry_id)
            picks = storage.record_my_team(conn, snapshot_id, entry_id, my_team)
            logger.info("captured own squad: %s picks", picks)
        except Exception as e:
            logger.warning("could not capture own squad: %s", e)
    else:
        logger.info("no authenticated session; market captured without own squad")

    conn.commit()
    return snapshot_id


async def backfill_actuals(conn, client: FPLClient, element_ids: list[int]) -> int:
    """Pull per-gameweek actuals for the given players into player_gameweek.

    Safe to re-run: rows are keyed on (element_id, round) and replaced, so a settled
    gameweek converges rather than duplicating.
    """
    semaphore = asyncio.Semaphore(BACKFILL_CONCURRENCY)
    total = 0

    async def fetch(element_id: int) -> list[dict]:
        async with semaphore:
            try:
                summary = await client.get_element_summary(element_id)
                return summary.get("history") or []
            except Exception as e:
                logger.warning("element-summary %s failed: %s", element_id, e)
                return []

    results = await asyncio.gather(*(fetch(eid) for eid in element_ids))
    for history in results:
        if history:
            total += storage.record_player_gameweeks(conn, history)

    conn.commit()
    logger.info("backfilled %s player-gameweek rows", total)
    return total


async def _run(args) -> int:
    conn = storage.connect(args.db)
    client = FPLClient(store=store)
    try:
        if not args.backfill_only:
            if storage.snapshot_taken_today(conn) and not args.force:
                logger.info("a snapshot already exists for today; use --force to add another")
            else:
                await capture(conn, client, kind=args.kind)

        if args.backfill or args.backfill_only:
            element_ids = [r["element_id"] for r in
                           conn.execute("SELECT element_id FROM player ORDER BY element_id")]
            if not element_ids:
                logger.error("no players known yet; run a snapshot first")
                return 1
            await backfill_actuals(conn, client, element_ids)

        for label, query in [
            ("snapshots", "SELECT COUNT(*) FROM snapshot"),
            ("player_snapshot rows", "SELECT COUNT(*) FROM player_snapshot"),
            ("player_gameweek rows", "SELECT COUNT(*) FROM player_gameweek"),
            ("fixtures", "SELECT COUNT(*) FROM fixture"),
        ]:
            logger.info("%-22s %s", label, conn.execute(query).fetchone()[0])
        return 0
    finally:
        await client.close()
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture FPL state into the warehouse.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--force", action="store_true",
                        help="snapshot even if one was already taken today")
    parser.add_argument("--backfill", action="store_true",
                        help="also pull per-gameweek actuals for every known player")
    parser.add_argument("--backfill-only", action="store_true",
                        help="pull actuals without taking a new snapshot")
    parser.add_argument("--kind", default="manual", help="label for this snapshot")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

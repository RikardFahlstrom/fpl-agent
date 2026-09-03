"""Capture FPL state into the warehouse.

Run daily. Price changes resolve nightly (~01:30 UK), so a weekly capture would miss the
price dynamics entirely - and none of it can be recovered after the fact.

A snapshot without a session captures the market but not your squad, and selling
prices, bank and free transfers exist in no public endpoint - so that half is lost for
good, silently, unless something checks first. Capture therefore refuses to run
half-blind unless explicitly told to.

    python -m fpl_agent.snapshot                  # capture, skip if already done today
    python -m fpl_agent.snapshot --force          # capture regardless
    python -m fpl_agent.snapshot --allow-partial  # market only, knowingly without a squad
    python -m fpl_agent.snapshot --backfill       # also pull per-gameweek actuals
    python -m fpl_agent.snapshot --backfill-only  # actuals only, no new snapshot
"""

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import config
from . import lineups, storage
from ..client import FPLClient
from ..headless_auth import authenticated_client, cache_path, env_flag
from ..rotowire_scraper import RotoWireLineupScraper
from ..sessions import sessions

logger = logging.getLogger("fpl_snapshot")

# element-summary is one request per player; this bounds concurrency so a backfill of
# ~650 players does not open 650 sockets against the FPL API at once.
BACKFILL_CONCURRENCY = 8

# Above this share of failed element-summary calls the warehouse is half-fetched and must
# not be graded against. A handful of players failing is ordinary FPL API flakiness; a
# twentieth of the league missing means the rows settle would read as zeros are simply
# absent, and no run should continue as if they were.
MAX_BACKFILL_FAILURE_RATE = 0.05


@dataclass
class BackfillResult:
    """What a backfill actually fetched, as distinct from what it attempted.

    The FPL API refuses element-summary requests intermittently. The original backfill
    caught every per-player failure, logged a warning and returned normally, so a run in
    which all 652 calls failed was indistinguishable from a clean one. Settle then saw a
    finished gameweek, turned every absent player_gameweek row into a zero via COALESCE,
    and reported a confident bias of about +1.5 against actuals that were never fetched -
    which `--learn` writes to a learning file as fact. The caller cannot refuse that
    without being told how much of the league is missing.
    """
    rows: int
    attempted: int
    failed: int

    @property
    def failure_rate(self) -> float:
        return self.failed / self.attempted if self.attempted else 0.0


@dataclass
class Readiness:
    """Whether a snapshot will capture everything, and what is missing if not."""
    complete: bool
    missing: list[str]
    detail: list[str]


def auth_readiness() -> Readiness:
    """Check the configuration a full snapshot needs, before spending a request on it.

    A cached session is enough on its own: credentials are only needed to create one.
    """
    missing: list[str] = []
    detail: list[str] = []

    cached = cache_path()
    has_cache = cached.exists()
    detail.append(f"token cache {'found' if has_cache else 'absent'} at {cached}")

    if not env_flag("FPL_AUTO_LOGIN"):
        missing.append("FPL_AUTO_LOGIN")
        detail.append("FPL_AUTO_LOGIN is not set, so no session is established at startup")

    if not has_cache:
        for name in ("FPL_EMAIL", "FPL_PASSWORD"):
            if not os.environ.get(name, "").strip():
                missing.append(name)
        if "FPL_EMAIL" in missing or "FPL_PASSWORD" in missing:
            detail.append("no cached session and no credentials to create one")

    return Readiness(complete=not missing, missing=missing, detail=detail)


def report_readiness(readiness: Readiness) -> None:
    for line in readiness.detail:
        logger.info("  %s", line)
    if readiness.complete:
        logger.info("auth configured; will try to establish a session before capturing")
        return
    logger.warning("auth incomplete - missing: %s", ", ".join(readiness.missing))
    logger.warning(
        "  a snapshot now records the market but NOT your squad. Selling prices, bank, "
        "free transfers and chips exist in no public endpoint, so that data is lost for "
        "this point in time and cannot be recovered later.")
    logger.warning(
        "  fix: export FPL_AUTO_LOGIN=true with FPL_EMAIL and FPL_PASSWORD, or log in "
        "once through the local browser flow to create the token cache.")
    logger.warning("  to capture the market anyway, re-run with --allow-partial")


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
    entry_id = sessions.get_user_entry_id(client) if client.user_info else None
    if entry_id:
        try:
            my_team = await client.get_my_team(entry_id)
            picks = storage.record_my_team(conn, snapshot_id, entry_id, my_team)
            logger.info("captured own squad: %s picks", picks)
        except Exception as e:
            logger.warning("could not capture own squad: %s", e)
    else:
        logger.info("no authenticated session; market captured without own squad")

    # Predicted lineups, for the gameweek being captured. Minutes dominate scoring and
    # FPL's own flag only reports injuries, never rotation.
    gameweek = storage.target_gameweek(bootstrap)
    if gameweek is not None:
        try:
            matches = await RotoWireLineupScraper().scrape_match_lineups()
            if matches:
                stored, unresolved = lineups.record_lineups(
                    conn, snapshot_id, gameweek, matches, bootstrap)
                logger.info("captured %s lineup entries (%s unresolved)",
                            stored, len(unresolved))
        except Exception as e:
            # A third-party page being down must not cost the market snapshot.
            logger.warning("could not capture lineups: %s", e)

    conn.commit()
    return snapshot_id


async def backfill_actuals(conn, client: FPLClient, element_ids: list[int]) -> BackfillResult:
    """Pull per-gameweek actuals for the given players into player_gameweek.

    Safe to re-run: rows are keyed on (element_id, round) and replaced, so a settled
    gameweek converges rather than duplicating.

    Returns what was fetched *and* what was lost. Rows that did arrive are kept - the
    partial warehouse is still better than nothing and the next run converges on it -
    but the caller is told the failure count so it can refuse to grade against it.
    """
    semaphore = asyncio.Semaphore(BACKFILL_CONCURRENCY)
    total = 0

    async def fetch(element_id: int) -> Optional[list[dict]]:
        """None means the request failed; [] means the player genuinely has no history.

        Collapsing those two was the bug. A player who has not appeared yet and a player
        the API refused to answer for look identical once both become an empty list, so a
        total outage reads as a league that has played no football - and every one of
        those absences later becomes a zero the calibration believes.
        """
        async with semaphore:
            try:
                summary = await client.get_element_summary(element_id)
                return summary.get("history") or []
            except Exception as e:
                logger.warning("element-summary %s failed: %s", element_id, e)
                return None

    results = await asyncio.gather(*(fetch(eid) for eid in element_ids))
    failed = 0
    for history in results:
        if history is None:
            failed += 1
        elif history:
            total += storage.record_player_gameweeks(conn, history)

    conn.commit()
    if failed:
        logger.warning(
            "backfilled %s player-gameweek rows; %s of %s players failed to fetch",
            total, failed, len(element_ids))
    else:
        logger.info("backfilled %s player-gameweek rows from %s players",
                    total, len(element_ids))
    return BackfillResult(rows=total, attempted=len(element_ids), failed=failed)


async def _run(args) -> int:
    conn = storage.connect(args.db)
    client, authenticated = await authenticated_client()
    exit_code = 0
    try:
        if not args.backfill_only:
            readiness = auth_readiness()
            report_readiness(readiness)
            if not readiness.complete and not args.allow_partial:
                logger.error("refusing to take a partial snapshot; see above")
                return 2
            # Configured but the login failed: the squad was promised and cannot be
            # delivered, so do not quietly record half a snapshot.
            if readiness.complete and not authenticated and not args.allow_partial:
                logger.error(
                    "auth is configured but no session could be established, so the "
                    "squad cannot be captured. Fix the login, or re-run with "
                    "--allow-partial to record the market alone.")
                return 3

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
            result = await backfill_actuals(conn, client, element_ids)
            # A nightly job that half-fetches the league must fail where someone can see
            # it. Exiting zero here leaves a warehouse that looks complete and settles to
            # a confident bias built out of players the API never answered for.
            if result.failure_rate > MAX_BACKFILL_FAILURE_RATE:
                logger.error(
                    "backfill lost %s of %s players (%.1f%%, tolerated %.0f%%); the "
                    "actuals are incomplete and must not be graded against. Re-run once "
                    "the FPL API is answering.",
                    result.failed, result.attempted, 100 * result.failure_rate,
                    100 * MAX_BACKFILL_FAILURE_RATE)
                exit_code = 5

        for label, query in [
            ("snapshots", "SELECT COUNT(*) FROM snapshot"),
            ("player_snapshot rows", "SELECT COUNT(*) FROM player_snapshot"),
            ("player_gameweek rows", "SELECT COUNT(*) FROM player_gameweek"),
            ("fixtures", "SELECT COUNT(*) FROM fixture"),
        ]:
            logger.info("%-22s %s", label, conn.execute(query).fetchone()[0])
        return exit_code
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
    parser.add_argument("--allow-partial", action="store_true",
                        help="capture the market even though the squad cannot be captured")
    parser.add_argument("--kind", default="manual", help="label for this snapshot")
    args = parser.parse_args(argv)

    config.load()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

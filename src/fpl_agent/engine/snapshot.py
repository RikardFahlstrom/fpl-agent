"""Capture FPL state into the warehouse.

Run daily. Price changes resolve nightly (~01:30 UK), so a weekly capture would miss the
price dynamics entirely - and none of it can be recovered after the fact.

A snapshot without a session captures the market but not your squad, and selling
prices, bank and free transfers exist in no public endpoint - so that half is lost for
good, silently, unless something checks first. Capture therefore refuses to run
half-blind unless explicitly told to.

    fpl-agent snapshot                  # capture, skip if already done today
    fpl-agent snapshot --force          # capture regardless
    fpl-agent snapshot --allow-partial  # market only, knowingly without a squad
    fpl-agent snapshot --backfill       # also pull per-gameweek actuals
    fpl-agent snapshot --backfill-only  # actuals only, no new snapshot

The default skip is a guard for a hand-run repeat, not the behaviour the scheduled
callers want: both `make snapshot` and `deploy/fpl-cron.sh` pass --force deliberately.
Prices resolve nightly and predicted lineups firm up through matchday, so a second
capture on the same day is a different market, not a duplicate of the first.

Exit codes, for whoever is reading a scheduled run that failed: 2 auth is not configured,
3 it is configured but no session was established, 4 a session was established and the
squad still was not captured, 5 too much of the backfill failed to be graded against.
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
from .actuals import MAX_BACKFILL_FAILURE_RATE, backfill_actuals
from ..client import FPLClient
from ..headless_auth import authenticated_client, cache_path, env_flag
from ..rotowire_scraper import RotoWireLineupScraper
from ..sessions import sessions

logger = logging.getLogger("fpl_snapshot")

# An FPL squad is exactly 15 players: 11 on the pitch and 4 on the bench. `my-team/`
# answers with all fifteen or it has not answered, so any other count means the squad
# half of the snapshot is incomplete rather than merely small.
SQUAD_SIZE = 15


@dataclass
class CaptureResult:
    """What a snapshot actually put in the warehouse, read back out of it.

    Counted with queries against the snapshot id rather than taken from the return values
    of the writers: `capture` swallows a failing `my-team/` so the market half survives,
    which means the only honest answer to "was the squad captured" is to go and look. A
    403 on `my-team/` (FPL returns one during maintenance) used to leave a warning in the
    log, no `my_squad` rows, and exit 0 - and `recommend` then failed with "no squad
    captured" hours later at the deadline.
    """
    snapshot_id: int
    gameweek: Optional[int]
    players: int
    fixtures: int
    squad_rows: int
    lineup_rows: int
    # The gameweeks the lineups were filed under. Plural, and not assumed equal to
    # `gameweek`: a match is filed under the event whose fixture list holds its pair of
    # clubs, which between a deadline and that round's last kickoff is the round being
    # played rather than the one bootstrap has already moved on to.
    lineup_gameweeks: list[int]


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


async def capture(conn, client: FPLClient, *, kind: str = "manual") -> CaptureResult:
    """Capture one point-in-time snapshot. Returns what landed in the warehouse."""
    bootstrap = await client.get_bootstrap_data()
    fixtures = await client.get_fixtures()

    snapshot_id = storage.create_snapshot(conn, bootstrap, kind=kind)
    storage.upsert_teams(conn, bootstrap)
    storage.upsert_players(conn, bootstrap)
    storage.record_player_snapshot(conn, snapshot_id, bootstrap)
    storage.record_game_config(conn, bootstrap)
    fixture_count = storage.upsert_fixtures(conn, fixtures)

    # The authenticated squad is optional: an unauthenticated run still captures the
    # market, which is the part that cannot be recovered later.
    entry_id = sessions.get_user_entry_id(client) if client.user_info else None
    if entry_id:
        try:
            my_team = await client.get_my_team(entry_id)
            picks = storage.record_my_team(conn, snapshot_id, entry_id, my_team)
            logger.info("captured own squad: %s picks", picks)
        except Exception as e:
            # Kept a warning on purpose: the market half is the irrecoverable one and
            # must still be committed. The caller decides what a missing squad costs.
            logger.warning("could not capture own squad: %s", e)
    else:
        logger.info("no authenticated session; market captured without own squad")

    # Predicted lineups, for the gameweek being captured. Minutes dominate scoring and
    # FPL's own flag only reports injuries, never rotation.
    gameweek = storage.target_gameweek(bootstrap)
    lineup_gameweeks: list[int] = []
    if gameweek is not None:
        try:
            matches = await RotoWireLineupScraper().scrape_match_lineups()
            if matches:
                # The fixtures just fetched are what says which round a scraped match is
                # in; `gameweek` is bootstrap's `is_next`, which has already advanced by
                # the time the round it names is being played.
                captured = lineups.record_lineups(
                    conn, snapshot_id, gameweek, matches, bootstrap,
                    fixture_events=lineups.fixture_events(fixtures, bootstrap))
                lineup_gameweeks = captured.gameweeks
                logger.info("captured %s lineup entries (%s unresolved)",
                            captured.rows, len(captured.unresolved))
        except Exception as e:
            # A third-party page being down must not cost the market snapshot. It stays a
            # warning, but the summary reports the zero rows rather than staying silent.
            logger.warning("could not capture lineups: %s", e)

    conn.commit()

    def count(table: str) -> int:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()[0]

    return CaptureResult(
        snapshot_id=snapshot_id,
        gameweek=gameweek,
        players=count("player_snapshot"),
        fixtures=fixture_count,
        squad_rows=count("my_squad"),
        lineup_rows=count("predicted_lineup"),
        lineup_gameweeks=lineup_gameweeks,
    )


def summarise(result: CaptureResult, *, squad_expected: bool) -> str:
    """One line for whoever reads the nightly job's log at 03:00 with no other context."""
    squad = (f"{result.squad_rows} of {SQUAD_SIZE} squad rows" if squad_expected
             else f"{result.squad_rows} squad rows (no session; market only)")
    # Named individually, not summarised: two gameweeks here means the scrape straddled a
    # deadline, and that is worth seeing at 03:00 rather than inferring later.
    if not result.lineup_gameweeks:
        filed = "filed under no gameweek"
    else:
        plural = "s" if len(result.lineup_gameweeks) > 1 else ""
        filed = (f"filed under gameweek{plural} "
                 + ", ".join(str(gw) for gw in result.lineup_gameweeks))
    return (f"snapshot {result.snapshot_id} for gameweek {result.gameweek}: "
            f"{result.players} players, {result.fixtures} fixtures, {squad}, "
            f"{result.lineup_rows} lineup rows {filed}")


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
                result = await capture(conn, client, kind=args.kind)
                # The preflight above promised the squad exactly when auth was configured
                # *and* a session was established; gate on the same pair, so what is
                # checked here cannot disagree with what was promised there.
                squad_expected = readiness.complete and authenticated
                logger.info("%s", summarise(result, squad_expected=squad_expected))
                if (squad_expected and result.squad_rows != SQUAD_SIZE
                        and not args.allow_partial):
                    logger.error(
                        "squad missing from snapshot %s: %s of %s my_squad rows were "
                        "recorded although a session was established. Selling prices, "
                        "bank and free transfers for this moment exist in no public "
                        "endpoint, so they are gone; `recommend` will fail with \"no "
                        "squad captured\" at the deadline. The market half has been "
                        "kept - re-run once my-team/ answers, or use --allow-partial "
                        "for a deliberate market-only run.",
                        result.snapshot_id, result.squad_rows, SQUAD_SIZE)
                    exit_code = 4

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
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="record the market alone and exit 0 without a squad; use it for a "
             "deliberate market-only run, e.g. before the account exists or while "
             "my-team/ is down and the price data still matters")
    parser.add_argument("--kind", default="manual", help="label for this snapshot")
    args = parser.parse_args(argv)

    config.load()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Pull what players actually scored into `player_gameweek`.

Its own module because it has two callers with opposite intentions and belongs to
neither. `snapshot` fetches actuals as housekeeping alongside the market; `settle` fetches
them because it is about to grade against them, and refuses to continue when too few
arrive. Living in `snapshot` made the second caller import the first for a function that
was never about snapshots, and put the honesty guard - `MAX_BACKFILL_FAILURE_RATE` - in a
file whose subject is something else.

The distinction this module exists to preserve is between a player who has not played and
a request that failed. Collapsing them was the bug: both become an empty list, so a total
API outage reads as a league that has played no football, and every one of those absences
later becomes a zero the calibration believes. `CLAUDE.md`: absence of a row is data, but
only once you know the row was actually asked for.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from ..client import FPLClient
from . import storage

logger = logging.getLogger("fpl_actuals")

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


async def backfill_actuals(conn, client: FPLClient,
                           element_ids: list[int]) -> BackfillResult:
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

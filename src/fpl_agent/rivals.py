"""Rival squads, and ownership measured against the people you actually play.

Global ownership is the wrong denominator. A player owned by 40% of the world but by
everyone in your six-person league is not a differential - skipping him is a risk, not
an edge. What matters is ownership *within your leagues*.

Rival squads are public once a gameweek has started, so this reads the same endpoint the
FPL site uses. Only classic private leagues are captured: FPL's `league_type` is 'x' for
those and 's' for the global ones, and the global ones are unusable here - "Overall" has
around 9.9 million entries.

    python -m fpl_agent.rivals --gameweek 2
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import storage
from .client import FPLClient
from .state import store

logger = logging.getLogger("fpl_rivals")

# FPL marks private/classic leagues 'x' and its own global ones 's'.
PRIVATE_LEAGUE_TYPE = "x"
# One standings page. Leagues larger than this are skipped by default: past a few dozen
# rivals the ownership signal stops being about anyone you are actually racing.
DEFAULT_MAX_RIVALS = 50
RIVAL_CONCURRENCY = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def capturable_leagues(user_info: dict, max_rivals: int = DEFAULT_MAX_RIVALS,
                       include_global: bool = False) -> list[dict]:
    """The user's leagues worth capturing: private, and small enough to mean something."""
    leagues = (user_info.get("leagues") or {}).get("classic") or []
    keep = []
    for league in leagues:
        if not include_global and league.get("league_type") != PRIVATE_LEAGUE_TYPE:
            continue
        count = league.get("rank_count")
        if count is not None and count > max_rivals:
            logger.info("skipping %s (%s entries, above the %s cap)",
                        league.get("name"), count, max_rivals)
            continue
        keep.append(league)
    return keep


async def capture_league(conn: sqlite3.Connection, client: FPLClient, league: dict,
                         gameweek: int, max_rivals: int = DEFAULT_MAX_RIVALS,
                         own_entry: Optional[int] = None) -> int:
    """Record a league's members and their squads for the gameweek."""
    league_id = league["id"]
    conn.execute(
        "INSERT OR REPLACE INTO league VALUES (?, ?, ?, ?, ?)",
        (league_id, league.get("name"), league.get("league_type"),
         league.get("rank_count"), _now()),
    )

    standings = await client.get_league_standings(league_id)
    results = (standings.get("standings") or {}).get("results") or []
    results = [r for r in results if r.get("entry") != own_entry][:max_rivals]
    if not results:
        return 0

    conn.executemany(
        "INSERT OR REPLACE INTO rival VALUES (?, ?, ?, ?, ?, ?)",
        [(r["entry"], league_id, r.get("player_name"), r.get("entry_name"),
          r.get("rank"), r.get("total")) for r in results],
    )

    semaphore = asyncio.Semaphore(RIVAL_CONCURRENCY)

    async def picks(entry_id: int) -> tuple[int, list[dict]]:
        async with semaphore:
            try:
                data = await client.get_manager_gameweek_picks(entry_id, gameweek)
                return entry_id, data.get("picks") or []
            except Exception as e:
                # A manager who joined late has no picks for an early gameweek.
                logger.debug("picks for %s gw%s unavailable: %s", entry_id, gameweek, e)
                return entry_id, []

    captured = 0
    for entry_id, squad in await asyncio.gather(
            *(picks(r["entry"]) for r in results)):
        if not squad:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO rival_squad VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(entry_id, gameweek, p["element"], p.get("position"), p.get("multiplier"),
              1 if p.get("is_captain") else 0, 1 if p.get("is_vice_captain") else 0)
             for p in squad],
        )
        captured += 1

    conn.commit()
    logger.info("league %s (%s): %s rivals, %s squads for gw%s",
                league_id, league.get("name"), len(results), captured, gameweek)
    return captured


def league_ownership(conn: sqlite3.Connection, gameweek: int) -> dict[int, dict[str, float]]:
    """Ownership and effective ownership across all captured rivals for a gameweek.

    Effective ownership counts a captain twice, because that is how much of the field's
    score a player actually drives: EO = (owners + captains) / managers.
    """
    total = conn.execute(
        "SELECT COUNT(DISTINCT entry_id) AS n FROM rival_squad WHERE gameweek = ?",
        (gameweek,),
    ).fetchone()["n"]
    if not total:
        return {}

    ownership: dict[int, dict[str, float]] = {}
    for row in conn.execute(
        """SELECT element_id,
                  COUNT(DISTINCT entry_id) AS owners,
                  SUM(is_captain) AS captains
           FROM rival_squad WHERE gameweek = ? GROUP BY element_id""",
        (gameweek,),
    ):
        owners = row["owners"] or 0
        captains = row["captains"] or 0
        ownership[row["element_id"]] = {
            "owned_by": owners,
            "managers": total,
            "ownership": owners / total,
            "effective_ownership": (owners + captains) / total,
        }
    return ownership


async def _run(args) -> int:
    conn = storage.connect(args.db)
    client = FPLClient(store=store)
    try:
        if not client.user_info:
            logger.error(
                "no authenticated session: league membership comes from /me/. "
                "Capture a snapshot with FPL_AUTO_LOGIN set, or pass --league.")
            return 1
        own_entry = store.get_user_entry_id(client)

        gameweek = args.gameweek
        if gameweek is None:
            row = conn.execute("SELECT MAX(round) AS r FROM player_gameweek").fetchone()
            gameweek = row["r"] if row else None
        if gameweek is None:
            logger.error("no completed gameweek to capture; run a backfill first")
            return 1

        leagues = capturable_leagues(client.user_info, args.max_rivals, args.include_global)
        if args.league:
            leagues = [lg for lg in leagues if lg["id"] in set(args.league)]
        if not leagues:
            logger.error("no capturable leagues; all are global or above the rival cap")
            return 1

        for league in leagues:
            await capture_league(conn, client, league, gameweek, args.max_rivals, own_entry)

        ownership = league_ownership(conn, gameweek)
        logger.info("%s players owned across your leagues in gw%s", len(ownership), gameweek)
        return 0
    finally:
        await client.close()
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture rival squads from your leagues.")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    parser.add_argument("--gameweek", type=int, default=None)
    parser.add_argument("--max-rivals", type=int, default=DEFAULT_MAX_RIVALS)
    parser.add_argument("--league", type=int, action="append",
                        help="restrict to these league ids")
    parser.add_argument("--include-global", action="store_true",
                        help="also consider FPL's global leagues (usually far too large)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

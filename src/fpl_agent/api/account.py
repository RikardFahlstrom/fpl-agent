"""Who an authenticated client belongs to, and the leagues they are in.

Both are read off the client itself: `/me/` is fetched at login and kept as
`client.user_info`; the leagues are fetched once on demand and kept as `client.leagues`.
There is one client per process, so there is nothing else to key a cache by.
"""
# Derived from lewis-king/fpl-mcp-server (MIT); see LICENSE-THIRD-PARTY.

import logging
from typing import List, Optional

from .client import FPLClient

logger = logging.getLogger("fpl_account")


def entry_id(client: FPLClient) -> Optional[int]:
    """The manager's entry id, or None when the client has no authenticated identity."""
    if not client.user_info:
        return None
    # /me/ returns {"player": null} when unauthenticated, and a default only applies
    # to a missing key, not a null one.
    return (client.user_info.get('player') or {}).get('entry')


async def leagues(client: FPLClient) -> List[dict]:
    """The manager's classic leagues.

    /me/ returns only the player and their watchlist - league membership is not in
    it - so this reads entry/{id}/ instead. Reading it from user_info silently
    yielded an empty list, which made every league lookup report "league not found"
    for leagues the user is actually in.
    """
    entry = entry_id(client)
    if not entry:
        return []
    if client.leagues is not None:
        return client.leagues
    try:
        manager = await client.get_manager_entry(entry)
    except Exception as e:
        logger.error(f"Could not fetch leagues for entry {entry}: {e}")
        return []
    client.leagues = (manager.get("leagues") or {}).get("classic") or []
    return client.leagues

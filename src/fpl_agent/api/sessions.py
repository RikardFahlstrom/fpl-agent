"""Authenticated sessions and the leagues they belong to.

Holds who is logged in and the leagues that can be looked up on their behalf.
"""
# Derived from lewis-king/fpl-mcp-server (MIT); see LICENSE-THIRD-PARTY.

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .client import FPLClient

logger = logging.getLogger("fpl_sessions")


@dataclass
class PendingLogin:
    created_at: float
    status: str = "pending"  # pending, success, failed
    session_id: Optional[str] = None
    error: Optional[str] = None


class SessionRegistry:
    """Pending logins, authenticated clients, and the user's leagues."""

    def __init__(self):
        # request_id (from the login URL) -> status
        self.pending_logins: Dict[str, PendingLogin] = {}
        # session_id -> authenticated client
        self.active_sessions: Dict[str, FPLClient] = {}
        # The session established with no human present.
        self.active_session_id: Optional[str] = None
        # Classic leagues per entry id: /me/ does not carry them.
        self.league_cache: Dict[int, List[dict]] = {}

    def create_login_request(self, request_id: str):
        self.pending_logins[request_id] = PendingLogin(created_at=time.time())

    async def set_login_success(self, request_id: str, session_id: str, client: FPLClient):
        """Record a successful login and fetch who it belongs to."""
        self.active_sessions[session_id] = client
        
        # Fetch user info after successful login and store it in the client
        try:
            user_data = await client.get_me()
            client.user_info = user_data  # Store the user info in the client
            entry_id = user_data.get('player', {}).get('entry')
            logger.info(f"Fetched and stored user info for session {session_id}: entry_id={entry_id}")
        except Exception as e:
            logger.error(f"Failed to fetch user info after login: {e}")
        finally:
            # Dispose this loop's connection pool; the client lazily opens another on
            # first use, so a caller on a different loop is safe.
            await client.close()
        
        if request_id in self.pending_logins:
            self.pending_logins[request_id].status = "success"
            self.pending_logins[request_id].session_id = session_id

    def set_login_failure(self, request_id: str, error: str):
        if request_id in self.pending_logins:
            self.pending_logins[request_id].status = "failed"
            self.pending_logins[request_id].error = error

    def get_client(self, session_id: str) -> Optional[FPLClient]:
        return self.active_sessions.get(session_id)

    def get_user_entry_id(self, client: FPLClient) -> Optional[int]:
        """
        Get the user's entry ID from their stored user info.
        
        Args:
            client: The authenticated FPL client
            
        Returns:
            The user's entry ID or None if not available
        """
        if not client.user_info:
            return None
        # /me/ returns {"player": null} when unauthenticated, and a default only applies
        # to a missing key, not a null one.
        return (client.user_info.get('player') or {}).get('entry')

    async def get_user_leagues(self, client: FPLClient) -> List[dict]:
        """The user's classic leagues.

        /me/ returns only the player and their watchlist - league membership is not in
        it - so this reads entry/{id}/ instead. Reading it from user_info silently
        yielded an empty list, which made every league tool report "league not found"
        for leagues the user is actually in.
        """
        entry_id = self.get_user_entry_id(client)
        if not entry_id:
            return []
        if entry_id in self.league_cache:
            return self.league_cache[entry_id]
        try:
            entry = await client.get_manager_entry(entry_id)
        except Exception as e:
            logger.error(f"Could not fetch leagues for entry {entry_id}: {e}")
            return []
        leagues = (entry.get("leagues") or {}).get("classic") or []
        self.league_cache[entry_id] = leagues
        return leagues

# Global instance
sessions = SessionRegistry()

"""Authenticated sessions and the leagues they belong to.

Split out of the old SessionStore. This half holds who is logged in and what can be
looked up on their behalf; the reference data lives in `reference.py`.
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .client import FPLClient
from .reference import normalize_name

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
        # session_id (given to the model) -> authenticated client
        self.active_sessions: Dict[str, FPLClient] = {}
        # A session established with no human present, which tools fall back to.
        self.active_session_id: Optional[str] = None
        # Classic leagues per entry id: /me/ does not carry them.
        self.league_cache: Dict[int, List[dict]] = {}

    def _normalize_name(self, name: str) -> str:
        return normalize_name(name)

    def create_login_request(self, request_id: str):
        self.pending_logins[request_id] = PendingLogin(created_at=time.time())

    async def set_login_success(self, request_id: str, session_id: str, client: FPLClient):
        """Set login success, retaining only loop-safe client state for MCP use."""
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
            # Web authentication and MCP tools run on different event loops. Dispose
            # the web loop's connection pool; the client lazily creates a new one in MCP.
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

    async def find_league_by_name(self, client: FPLClient, league_name: str) -> Optional[dict]:
        """
        Find a league by name from the user's leagues.
        
        Args:
            client: The authenticated FPL client
            league_name: The name of the league to find
            
        Returns:
            League dict with 'id' and 'name' if found, None otherwise
        """
        classic_leagues = await self.get_user_leagues(client)
        if not classic_leagues:
            return None
        
        # Normalize search name
        normalized_search = self._normalize_name(league_name)
        
        # Try exact match first
        for league in classic_leagues:
            if self._normalize_name(league.get('name', '')) == normalized_search:
                return {
                    'id': league.get('id'),
                    'name': league.get('name')
                }
        
        # Try substring match
        for league in classic_leagues:
            league_norm = self._normalize_name(league.get('name', ''))
            if normalized_search in league_norm or league_norm in normalized_search:
                return {
                    'id': league.get('id'),
                    'name': league.get('name')
                }
        
        return None

    async def find_manager_by_name(self, client: FPLClient, league_id: int, manager_name: str) -> Optional[dict]:
        """
        Find a manager by name in a league's standings.
        
        Args:
            client: The authenticated FPL client
            league_id: The league ID to search in
            manager_name: The manager's name to find
            
        Returns:
            Manager dict with 'entry', 'entry_name', 'player_name' if found, None otherwise
        """
        try:
            standings = await client.get_league_standings(league_id)
            results = (standings.get('standings') or {}).get('results') or []
            
            # Normalize search name
            normalized_search = self._normalize_name(manager_name)
            
            def _as_match(result: dict) -> dict:
                return {
                    'entry': result.get('entry'),
                    'entry_name': result.get('entry_name'),
                    'player_name': result.get('player_name')
                }
            
            # Search through standings
            for result in results:
                # Try matching against player_name (manager name), then entry_name (team name)
                if self._normalize_name(result.get('player_name', '')) == normalized_search:
                    return _as_match(result)
                
                if self._normalize_name(result.get('entry_name', '')) == normalized_search:
                    return _as_match(result)
            
            # Try substring matches
            for result in results:
                player_norm = self._normalize_name(result.get('player_name', ''))
                entry_norm = self._normalize_name(result.get('entry_name', ''))
                
                if (normalized_search in player_norm or player_norm in normalized_search or
                    normalized_search in entry_norm or entry_norm in normalized_search):
                    return _as_match(result)
            
            return None
            
        except Exception as e:
            logger.error(f"Error finding manager by name: {e}")
            return None


# Global instance
sessions = SessionRegistry()

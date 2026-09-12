# Derived from lewis-king/fpl-mcp-server (MIT); see LICENSE-THIRD-PARTY.
import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx


logger = logging.getLogger("fpl_client")

class FPLClient:
    BASE_URL = "https://fantasy.premierleague.com/api/"
    
    def __init__(self):
        self.session: httpx.AsyncClient | None = None
        self._session_loop: asyncio.AbstractEventLoop | None = None
        self.api_token = None
        self.team_id: Optional[int] = None
        self.user_info: Optional[Dict[str, Any]] = None  # Store user info from /me
        self.leagues: Optional[List[Dict[str, Any]]] = None  # entry/{id}/ classic leagues, once fetched
        self._reauth_hook = None

    def set_reauth_hook(self, hook) -> None:
        """Register an async callable used to recover from an expired token.

        Injected rather than imported so this module stays free of any
        dependency on the authentication path. The hook takes this client,
        refreshes its token in place, and returns True on success.
        """
        self._reauth_hook = hook

    def set_api_token(self, token: str):
        if not token.startswith("Bearer "):
            token = f"Bearer {token}"
        self.api_token = token

    def _get_session(self) -> httpx.AsyncClient:
        current_loop = asyncio.get_running_loop()
        if self.session is not None and self._session_loop is not current_loop:
            raise RuntimeError(
                "FPLClient crossed an asyncio event-loop boundary without being closed."
            )
        if self.session is None:
            self.session = httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30.0,
            )
            self._session_loop = current_loop
        return self.session
        
    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict = None,
        params: dict = None,
        allow_reauth: bool = True,
    ) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        session = self._get_session()
        headers = {}
        if self.api_token:
            headers['x-api-authorization'] = self.api_token
            headers['Authorization'] = self.api_token

        if method == "GET":
            response = await session.get(url, headers=headers, params=params)
        else:
            response = await session.post(url, json=data, headers=headers)

        # Every authenticated call funnels through here, so recovering from an
        # expired token in this one place covers every caller. Retry only once, so a
        # persistently rejected token surfaces as an error instead of looping.
        if response.status_code == 401 and allow_reauth and self._reauth_hook:
            logger.info("FPL rejected the token; attempting to re-authenticate.")
            if await self._reauth_hook(self):
                return await self._request(
                    method, endpoint, data, params, allow_reauth=False
                )

        response.raise_for_status()
        return response.json()

    async def get_bootstrap_data(self) -> Dict[str, Any]:
        """Fetch fresh bootstrap data from API"""
        return await self._request("GET", "bootstrap-static/")
    
    async def get_fixtures(self) -> List[Dict[str, Any]]:
        """Fetch fixtures data from API"""
        return await self._request("GET", "fixtures/")
    
    async def get_element_summary(self, player_id: int) -> Dict[str, Any]:
        """
        Fetch detailed player summary including fixtures, history, and past seasons.
        
        Args:
            player_id: The FPL player ID (element ID)
            
        Returns:
            Dictionary containing fixtures, history, and history_past
        """
        return await self._request("GET", f"element-summary/{player_id}/")
    
    async def get_manager_entry(self, team_id: int) -> Dict[str, Any]:
        """
        Fetch FPL manager/team entry information.
        
        Args:
            team_id: The FPL manager's team ID (entry ID)
            
        Returns:
            Dictionary containing manager details, leagues, and team information
        """
        return await self._request("GET", f"entry/{team_id}/")
    
    async def get_league_standings(
        self,
        league_id: int,
        page_standings: int = 1,
        page_new_entries: int = 1,
        phase: int = 1
    ) -> Dict[str, Any]:
        """
        Fetch league standings for a classic league.
        
        Args:
            league_id: The league ID
            page_standings: Page number for standings (default: 1)
            page_new_entries: Page number for new entries (default: 1)
            phase: Phase/season number (default: 1)
            
        Returns:
            Dictionary containing league info and standings with entries
        """
        params = {
            'page_standings': page_standings,
            'page_new_entries': page_new_entries,
            'phase': phase
        }
        return await self._request(
            "GET",
            f"leagues-classic/{league_id}/standings/",
            params=params
        )
    
    async def get_manager_gameweek_picks(self, team_id: int, gameweek: int) -> Dict[str, Any]:
        """
        Fetch a manager's team picks for a specific gameweek.
        
        Args:
            team_id: The FPL manager's team ID (entry ID)
            gameweek: The gameweek number (event ID)
            
        Returns:
            Dictionary containing picks, automatic subs, and entry history for the gameweek
        """
        return await self._request("GET", f"entry/{team_id}/event/{gameweek}/picks/")
    
    async def get_me(self) -> Dict[str, Any]:
        """
        Fetch current user's information including their entry ID.
        This is called after authentication to get the user's team ID.
        
        Returns:
            Dictionary containing player info with entry ID
        """
        return await self._request("GET", "me/")

    async def get_my_team(self, team_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"my-team/{team_id}/")

    async def close(self):
        if self.session is not None:
            await self.session.aclose()
            self.session = None
            self._session_loop = None

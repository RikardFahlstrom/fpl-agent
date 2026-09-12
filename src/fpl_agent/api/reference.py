"""Cached FPL reference data: squads, teams, fixtures and name lookup.

Split out of the old SessionStore, which did five unrelated jobs in one object and was
the coupling point between the engine and the MCP server. This half holds only what is
read *about* the game, and no session state, so the engine can use it without dragging in
authentication and the server can use it without dragging in the warehouse.
"""

import logging
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from .client import FPLClient
from .models import BootstrapData, ElementData, EventData, FixtureData

logger = logging.getLogger("fpl_reference")


def normalize_name(name: str) -> str:
    """Lowercase and collapse whitespace, for name matching."""
    return " ".join(name.lower().strip().split())


class ReferenceData:
    """Bootstrap and fixture data, loaded once and indexed for lookup."""

    def __init__(self):
        self.bootstrap_data: Optional[BootstrapData] = None
        self.fixtures_data: Optional[List[FixtureData]] = None
        # Normalised name -> player ids, since names are not unique.
        self.player_name_map: Dict[str, List[int]] = {}
        self.player_id_map: Dict[int, ElementData] = {}

    def _normalize_name(self, name: str) -> str:
        return normalize_name(name)

    async def ensure_bootstrap_data(self, client: FPLClient):
        """Ensure bootstrap data is loaded, fetching from API if needed"""
        if self.bootstrap_data is None:
            try:
                logger.info("Fetching bootstrap data from API...")
                raw_data = await client.get_bootstrap_data()
                self.bootstrap_data = BootstrapData(**raw_data)
                self._build_player_indices()
                logger.info(f"Loaded {len(self.bootstrap_data.elements)} players from API")
            except Exception as e:
                logger.error(f"Failed to load bootstrap data: {e}")
                raise

    async def ensure_fixtures_data(self, client: FPLClient):
        """Ensure fixtures data is loaded, fetching from API if needed"""
        if self.fixtures_data is None:
            try:
                logger.info("Fetching fixtures data from API...")
                raw_data = await client.get_fixtures()
                self.fixtures_data = [FixtureData(**fixture) for fixture in raw_data]
                logger.info(f"Loaded {len(self.fixtures_data)} fixtures from API")
            except Exception as e:
                logger.error(f"Failed to load fixtures data: {e}")
                raise

    def _build_player_indices(self):
        """Build player name and ID indices from bootstrap data"""
        if not self.bootstrap_data:
            return
        
        # Enrich elements with team names for faster lookups
        team_map = {t.id: t.name for t in self.bootstrap_data.teams}
        position_map = {t.id: t.singular_name_short for t in self.bootstrap_data.element_types}
        
        # Build player name index and ID map
        self.player_name_map.clear()
        self.player_id_map.clear()
        
        for element in self.bootstrap_data.elements:
            # Add team_name and position to each element
            element.team_name = team_map.get(element.team, "Unknown")
            element.position = position_map.get(element.element_type, "UNK")
            
            # Store in ID map
            self.player_id_map[element.id] = element
            
            # Build name index with multiple keys for flexible matching
            # 1. Web name (most common)
            web_key = self._normalize_name(element.web_name)
            if web_key not in self.player_name_map:
                self.player_name_map[web_key] = []
            self.player_name_map[web_key].append(element.id)
            
            # 2. Full name (first + second)
            full_key = self._normalize_name(f"{element.first_name} {element.second_name}")
            if full_key not in self.player_name_map:
                self.player_name_map[full_key] = []
            if element.id not in self.player_name_map[full_key]:
                self.player_name_map[full_key].append(element.id)
            
            # 3. Second name only (surname)
            surname_key = self._normalize_name(element.second_name)
            if surname_key not in self.player_name_map:
                self.player_name_map[surname_key] = []
            if element.id not in self.player_name_map[surname_key]:
                self.player_name_map[surname_key].append(element.id)
            
            # 4. First name + web name (for cases like "Mohamed Salah")
            if element.first_name and element.web_name != element.second_name:
                first_web_key = self._normalize_name(f"{element.first_name} {element.web_name}")
                if first_web_key not in self.player_name_map:
                    self.player_name_map[first_web_key] = []
                if element.id not in self.player_name_map[first_web_key]:
                    self.player_name_map[first_web_key].append(element.id)
        
        if self.bootstrap_data:
            logger.info(
                f"Built player indices: {len(self.bootstrap_data.elements)} players, "
                f"{len(self.bootstrap_data.teams)} teams, "
                f"{len(self.bootstrap_data.events)} gameweeks. "
                f"Name index has {len(self.player_name_map)} keys."
            )

    def get_team_by_id(self, team_id: int) -> Optional[dict]:
        """Get team information by ID"""
        if not self.bootstrap_data:
            return None
        
        team = next((t for t in self.bootstrap_data.teams if t.id == team_id), None)
        if not team:
            return None
        
        return {
            'id': team.id,
            'name': team.name,
            'short_name': team.short_name,
            'strength': getattr(team, 'strength', None),
            'strength_overall_home': getattr(team, 'strength_overall_home', None),
            'strength_overall_away': getattr(team, 'strength_overall_away', None),
            'strength_attack_home': getattr(team, 'strength_attack_home', None),
            'strength_attack_away': getattr(team, 'strength_attack_away', None),
            'strength_defence_home': getattr(team, 'strength_defence_home', None),
            'strength_defence_away': getattr(team, 'strength_defence_away', None),
        }

    def get_all_teams(self) -> list:
        """Get all teams with their information"""
        if not self.bootstrap_data:
            return []
        
        return [
            {
                'id': t.id,
                'name': t.name,
                'short_name': t.short_name,
                'strength': getattr(t, 'strength', None),
                'strength_overall_home': getattr(t, 'strength_overall_home', None),
                'strength_overall_away': getattr(t, 'strength_overall_away', None),
            }
            for t in self.bootstrap_data.teams
        ]

    def find_players_by_name(self, name_query: str, fuzzy: bool = True) -> List[Tuple[ElementData, float]]:
        """
        Find players by name with intelligent matching.
        Returns list of (player, similarity_score) tuples sorted by relevance.
        
        Args:
            name_query: The name to search for
            fuzzy: Whether to use fuzzy matching for close matches
        
        Returns:
            List of (ElementData, similarity_score) tuples, sorted by score descending
        """
        if not self.bootstrap_data:
            return []
        
        normalized_query = self._normalize_name(name_query)
        results: Dict[int, float] = {}  # player_id -> best similarity score
        
        # 1. Exact match
        if normalized_query in self.player_name_map:
            for player_id in self.player_name_map[normalized_query]:
                results[player_id] = 1.0
        
        # 2. Substring match (contains)
        if not results:
            for name_key, player_ids in self.player_name_map.items():
                if normalized_query in name_key or name_key in normalized_query:
                    # Calculate similarity based on length ratio
                    similarity = min(len(normalized_query), len(name_key)) / max(len(normalized_query), len(name_key))
                    for player_id in player_ids:
                        if player_id not in results or similarity > results[player_id]:
                            results[player_id] = similarity * 0.9  # Slightly lower than exact
        
        # 3. Fuzzy matching (if enabled and no good matches yet)
        if fuzzy and (not results or max(results.values()) < 0.7):
            for name_key, player_ids in self.player_name_map.items():
                similarity = SequenceMatcher(None, normalized_query, name_key).ratio()
                if similarity >= 0.6:  # Threshold for fuzzy matches
                    for player_id in player_ids:
                        if player_id not in results or similarity > results[player_id]:
                            results[player_id] = similarity * 0.8  # Lower than substring
        
        # Convert to list of tuples and sort by score
        player_matches = [
            (self.player_id_map[player_id], score)
            for player_id, score in results.items()
        ]
        player_matches.sort(key=lambda x: x[1], reverse=True)
        
        return player_matches

    def upcoming_fixtures(self, team_id: int, *, from_gameweek: int, limit: int) -> List[FixtureData]:
        """A team's next unplayed fixtures, earliest first.

        Counts fixtures rather than gameweeks, so a finished current gameweek,
        a blank or a double does not distort the horizon.
        """
        if not self.fixtures_data:
            return []
        upcoming = [
            f for f in self.fixtures_data
            if (f.team_h == team_id or f.team_a == team_id)
            and f.event and f.event >= from_gameweek
            and not f.finished
        ]
        return sorted(upcoming, key=lambda f: f.event)[:limit]

    def get_player_by_id(self, player_id: int) -> Optional[ElementData]:
        """Get a player by their ID"""
        return self.player_id_map.get(player_id)

    def get_current_gameweek(self) -> Optional[EventData]:
        """Get the current gameweek event"""
        if not self.bootstrap_data or not self.bootstrap_data.events:
            return None
        
        # First check for is_current flag
        for event in self.bootstrap_data.events:
            if event.is_current:
                return event
        
        # Fallback to is_next if current deadline has passed
        for event in self.bootstrap_data.events:
            if event.is_next:
                return event
        
        # Last resort: first unfinished gameweek
        for event in self.bootstrap_data.events:
            if not event.finished:
                return event
        
        return None

    def rehydrate_player_names(self, element_ids: list[int]) -> dict[int, dict]:
        """
        Rehydrate player element IDs to full player information.
        
        Args:
            element_ids: List of player element IDs
            
        Returns:
            Dictionary mapping element_id -> player info dict
        """
        result = {}
        for element_id in element_ids:
            player = self.get_player_by_id(element_id)
            if player:
                result[element_id] = {
                    'id': player.id,
                    'web_name': player.web_name,
                    'full_name': f"{player.first_name} {player.second_name}",
                    'team': player.team_name,
                    'position': player.position,
                    'price': player.now_cost / 10,
                    'form': player.form,
                    'points_per_game': player.points_per_game,
                    'total_points': getattr(player, 'total_points', 0),
                    'status': player.status,
                    'news': player.news
                }
        return result

    def get_player_name(self, element_id: int) -> str:
        """
        Get a player's web name by their element ID.
        
        Args:
            element_id: The player's element ID
            
        Returns:
            Player's web name or "Unknown Player (ID: {element_id})"
        """
        player = self.get_player_by_id(element_id)
        if player:
            return player.web_name
        return f"Unknown Player (ID: {element_id})"

    def enrich_gameweek_history(self, history: list[dict]) -> list[dict]:
        """
        Enrich gameweek history data with friendly names for teams.
        Adds 'opponent_team_name' and 'opponent_team_short' fields.
        
        Args:
            history: List of gameweek history dicts from element-summary
            
        Returns:
            Enriched history with team names added
        """
        if not self.bootstrap_data:
            return history
        
        enriched = []
        for gw in history:
            enriched_gw = gw.copy()
            
            # Add opponent team names
            opponent_id = gw.get('opponent_team')
            if opponent_id:
                opponent = self.get_team_by_id(opponent_id)
                if opponent:
                    enriched_gw['opponent_team_name'] = opponent['name']
                    enriched_gw['opponent_team_short'] = opponent['short_name']
            
            enriched.append(enriched_gw)
        
        return enriched

    def enrich_fixtures(self, fixtures: list) -> list:
        """
        Enrich fixture data with friendly team names.
        Adds 'team_h_name', 'team_h_short', 'team_a_name', 'team_a_short' fields.
        
        Args:
            fixtures: List of FixtureData objects or fixture dicts
            
        Returns:
            List of enriched fixture dicts
        """
        if not self.bootstrap_data:
            return fixtures
        
        enriched = []
        for fixture in fixtures:
            # Convert to dict if it's a FixtureData object
            if hasattr(fixture, 'model_dump'):
                fixture_dict = fixture.model_dump()
            elif hasattr(fixture, '__dict__'):
                fixture_dict = fixture.__dict__.copy()
            else:
                fixture_dict = fixture.copy() if isinstance(fixture, dict) else {}
            
            # Add home team names
            team_h_id = fixture_dict.get('team_h') if isinstance(fixture_dict, dict) else getattr(fixture, 'team_h', None)
            if team_h_id:
                team_h = self.get_team_by_id(team_h_id)
                if team_h:
                    fixture_dict['team_h_name'] = team_h['name']
                    fixture_dict['team_h_short'] = team_h['short_name']
            
            # Add away team names
            team_a_id = fixture_dict.get('team_a') if isinstance(fixture_dict, dict) else getattr(fixture, 'team_a', None)
            if team_a_id:
                team_a = self.get_team_by_id(team_a_id)
                if team_a:
                    fixture_dict['team_a_name'] = team_a['name']
                    fixture_dict['team_a_short'] = team_a['short_name']
            
            enriched.append(fixture_dict)
        
        return enriched


# Global instance
reference = ReferenceData()

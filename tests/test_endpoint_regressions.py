"""Regression tests for the FPL API layer: client, reference data and sessions.

Each test pins one previously-broken path. All fixtures are local: no test
here reaches the FPL API.
"""
import unittest

from fpl_agent.client import FPLClient
from fpl_agent.models import BootstrapData
from fpl_agent.reference import ReferenceData, reference
from fpl_agent.sessions import SessionRegistry


def _event(event_id: int, *, current=False, next_=False, finished=False, deadline=None):
    return {
        "id": event_id,
        "name": f"Gameweek {event_id}",
        "deadline_time": deadline or "2026-08-28T17:30:00Z",
        "finished": finished,
        "data_checked": finished,
        "deadline_time_epoch": 1756400000 + event_id,
        "is_previous": False,
        "is_current": current,
        "is_next": next_,
        "can_enter": True,
        "released": True,
    }


def _element(element_id: int, team: int, **overrides):
    element = {
        "id": element_id,
        "web_name": f"Player{element_id}",
        "first_name": "First",
        "second_name": f"Last{element_id}",
        "team": team,
        "element_type": 3,
        "now_cost": 50,
        "form": "3.0",
        "points_per_game": "3.0",
        "news": "",
        "status": "a",
        "total_points": 30,
        "minutes": 540,
    }
    element.update(overrides)
    return element


def _fixture(fixture_id: int, event: int, team_h: int, team_a: int, finished: bool):
    return {
        "code": 1000 + fixture_id,
        "event": event,
        "finished": finished,
        "finished_provisional": finished,
        "id": fixture_id,
        "kickoff_time": "2026-09-04T14:00:00Z",
        "minutes": 90 if finished else 0,
        "provisional_start_time": False,
        "started": finished,
        "team_a": team_a,
        "team_h": team_h,
        "stats": [],
        "team_h_difficulty": 3,
        "team_a_difficulty": 2,
        "pulse_id": 2000 + fixture_id,
    }


def _bootstrap():
    return {
        "elements": [_element(1, 1), _element(2, 2)],
        "teams": [
            {"id": 1, "name": "Test United", "short_name": "TSU", "strength": 4},
            {"id": 2, "name": "Test City", "short_name": "TSC", "strength": 3},
        ],
        "element_types": [
            {"id": 3, "singular_name_short": "MID", "plural_name_short": "MID"},
        ],
        "events": [
            _event(1, finished=True),
            _event(2, current=True, finished=True),
            _event(3, next_=True),
        ],
    }


class _FakeClient:
    """Stands in for FPLClient without touching the network."""

    user_info = {"player": {"entry": 99}}

    def __init__(self, standings=None, history=None):
        self.bootstrap_calls = 0
        self._standings = standings or {}
        self._history = history if history is not None else []

    async def get_bootstrap_data(self):
        self.bootstrap_calls += 1
        return _bootstrap()

    async def get_fixtures(self):
        return [_fixture(1, 2, 1, 2, True), _fixture(2, 3, 2, 1, False)]

    async def get_league_standings(self, league_id, **kwargs):
        return self._standings

    async def get_element_summary(self, player_id):
        return {"history": list(self._history), "fixtures": [], "history_past": []}

    async def get_players(self):
        return await FPLClient(reference=reference).get_players()


class BootstrapLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_bootstrap_data_uses_the_real_client_method(self):
        """ensure_bootstrap_data called a non-existent get_bootstrap_static()."""
        isolated = ReferenceData()
        client = _FakeClient()

        await isolated.ensure_bootstrap_data(client)

        self.assertEqual(client.bootstrap_calls, 1)
        self.assertIsNotNone(isolated.bootstrap_data)
        self.assertEqual(len(isolated.bootstrap_data.elements), 2)
        self.assertIn("player1", isolated.player_name_map)

    async def test_ensure_bootstrap_data_is_cached(self):
        isolated = ReferenceData()
        client = _FakeClient()

        await isolated.ensure_bootstrap_data(client)
        await isolated.ensure_bootstrap_data(client)

        self.assertEqual(client.bootstrap_calls, 1)


class EnrichmentTests(unittest.TestCase):
    def test_enrich_gameweek_history_returns_the_enriched_rows(self):
        """The function built `enriched` then fell off the end, returning None."""
        isolated = ReferenceData()
        isolated.bootstrap_data = BootstrapData(**_bootstrap())
        isolated._build_player_indices()

        enriched = isolated.enrich_gameweek_history(
            [{"opponent_team": 2, "total_points": 6, "minutes": 90}]
        )

        self.assertIsNotNone(enriched)
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["opponent_team_name"], "Test City")
        self.assertEqual(enriched[0]["opponent_team_short"], "TSC")
        self.assertEqual(enriched[0]["total_points"], 6)


class LeagueStandingsTests(unittest.IsolatedAsyncioTestCase):
    def _standings(self):
        return {"standings": {"results": [
            {"entry": 4242, "entry_name": "Team Alpha", "player_name": "Alex Manager"},
        ]}}

    async def test_find_manager_by_name_reads_the_dict_payload(self):
        """The store treated the standings dict as a pydantic model and always returned None."""
        isolated = SessionRegistry()
        client = _FakeClient(standings=self._standings())

        match = await isolated.find_manager_by_name(client, 314, "Alex Manager")

        self.assertIsNotNone(match)
        self.assertEqual(match["entry"], 4242)
        self.assertEqual(match["entry_name"], "Team Alpha")

    async def test_find_manager_by_name_matches_team_name_and_substrings(self):
        isolated = SessionRegistry()
        client = _FakeClient(standings=self._standings())

        self.assertEqual((await isolated.find_manager_by_name(client, 314, "Team Alpha"))["entry"], 4242)
        self.assertEqual((await isolated.find_manager_by_name(client, 314, "Alex"))["entry"], 4242)
        self.assertIsNone(await isolated.find_manager_by_name(client, 314, "Nobody Here"))


class PlayerModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_players_exposes_fields_the_recommendation_tools_read(self):
        """recommend_transfers/recommend_chip_strategy read .status and .minutes off Player."""
        isolated = ReferenceData()
        isolated.bootstrap_data = BootstrapData(**_bootstrap())
        isolated._build_player_indices()
        client = FPLClient(reference=isolated)

        players = await client.get_players()

        self.assertEqual(len(players), 2)
        for player in players:
            self.assertEqual(player.status, "a")
            self.assertEqual(player.minutes, 540)
            self.assertEqual(player.total_points, 30)


if __name__ == "__main__":
    unittest.main()

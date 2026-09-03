"""Regression tests for the MCP endpoint bugs found by auditing every tool/resource/prompt.

Each test pins one previously-broken endpoint path. All fixtures are local: no test
here reaches the FPL API.
"""
import unittest
from datetime import datetime, timedelta, timezone

from fpl_agent.mcp import prompts, tools  # noqa: F401  (registers prompts)
from fpl_agent.client import FPLClient
from fpl_agent.mcp.tools import mcp
from fpl_agent.models import BootstrapData, FixtureData
from fpl_agent.state import SessionStore, store


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
        return await FPLClient(store=store).get_players()


class _StoreFixture(unittest.IsolatedAsyncioTestCase):
    """Loads the shared store with local data and restores it afterwards."""

    def setUp(self):
        self._saved = (store.bootstrap_data, store.fixtures_data,
                       dict(store.player_name_map), dict(store.player_id_map),
                       tools.get_active_session())
        store.bootstrap_data = BootstrapData(**_bootstrap())
        store._build_player_indices()
        store.fixtures_data = [FixtureData(**f) for f in
                               [_fixture(1, 2, 1, 2, True), _fixture(2, 3, 2, 1, False),
                                _fixture(3, 4, 1, 2, False), _fixture(4, 5, 2, 1, False)]]

    def tearDown(self):
        (store.bootstrap_data, store.fixtures_data,
         store.player_name_map, store.player_id_map, session) = self._saved
        tools.set_active_session(session)

    def activate(self, client):
        store.active_sessions["regression"] = client
        tools.set_active_session("regression")
        self.addCleanup(store.active_sessions.pop, "regression", None)

    async def call_tool(self, name, args=None):
        result = await mcp.call_tool(name, args or {})
        content = result[0] if isinstance(result, tuple) else result
        return "".join(getattr(c, "text", str(c))
                       for c in (content if isinstance(content, list) else [content]))


class BootstrapLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_bootstrap_data_uses_the_real_client_method(self):
        """ensure_bootstrap_data called a non-existent get_bootstrap_static()."""
        isolated = SessionStore()
        client = _FakeClient()

        await isolated.ensure_bootstrap_data(client)

        self.assertEqual(client.bootstrap_calls, 1)
        self.assertIsNotNone(isolated.bootstrap_data)
        self.assertEqual(len(isolated.bootstrap_data.elements), 2)
        self.assertIn("player1", isolated.player_name_map)

    async def test_ensure_bootstrap_data_is_cached(self):
        isolated = SessionStore()
        client = _FakeClient()

        await isolated.ensure_bootstrap_data(client)
        await isolated.ensure_bootstrap_data(client)

        self.assertEqual(client.bootstrap_calls, 1)


class EnrichmentTests(unittest.TestCase):
    def test_enrich_gameweek_history_returns_the_enriched_rows(self):
        """The function built `enriched` then fell off the end, returning None."""
        isolated = SessionStore()
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
        isolated = SessionStore()
        client = _FakeClient(standings=self._standings())

        match = await isolated.find_manager_by_name(client, 314, "Alex Manager")

        self.assertIsNotNone(match)
        self.assertEqual(match["entry"], 4242)
        self.assertEqual(match["entry_name"], "Team Alpha")

    async def test_find_manager_by_name_matches_team_name_and_substrings(self):
        isolated = SessionStore()
        client = _FakeClient(standings=self._standings())

        self.assertEqual((await isolated.find_manager_by_name(client, 314, "Team Alpha"))["entry"], 4242)
        self.assertEqual((await isolated.find_manager_by_name(client, 314, "Alex"))["entry"], 4242)
        self.assertIsNone(await isolated.find_manager_by_name(client, 314, "Nobody Here"))


class PlayerModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_players_exposes_fields_the_recommendation_tools_read(self):
        """recommend_transfers/recommend_chip_strategy read .status and .minutes off Player."""
        isolated = SessionStore()
        isolated.bootstrap_data = BootstrapData(**_bootstrap())
        isolated._build_player_indices()
        client = FPLClient(store=isolated)

        players = await client.get_players()

        self.assertEqual(len(players), 2)
        for player in players:
            self.assertEqual(player.status, "a")
            self.assertEqual(player.minutes, 540)
            self.assertEqual(player.total_points, 30)


class CurrentGameweekTests(_StoreFixture):
    async def test_deadline_comparison_is_timezone_aware(self):
        """utcnow() is naive; the deadline is aware, so the compare raised TypeError."""
        future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bootstrap = _bootstrap()
        bootstrap["events"] = [_event(2, current=True, deadline=future), _event(3, next_=True)]
        store.bootstrap_data = BootstrapData(**bootstrap)
        store._build_player_indices()
        self.activate(_FakeClient())

        output = await self.call_tool("get_current_gameweek")

        self.assertNotIn("offset-naive", output)
        self.assertIn("Current Gameweek: Gameweek 2", output)

    async def test_falls_through_to_next_gameweek_after_the_deadline(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bootstrap = _bootstrap()
        bootstrap["events"] = [_event(2, current=True, deadline=past), _event(3, next_=True)]
        store.bootstrap_data = BootstrapData(**bootstrap)
        store._build_player_indices()
        self.activate(_FakeClient())

        output = await self.call_tool("get_current_gameweek")

        self.assertNotIn("offset-naive", output)
        self.assertIn("Upcoming Gameweek: Gameweek 3", output)


class FixtureWindowTests(_StoreFixture):
    async def test_requesting_n_gameweeks_returns_n_upcoming_fixtures(self):
        """The window started at the finished current GW, so N returned N-1 fixtures."""
        self.activate(_FakeClient())

        output = await self.call_tool(
            "analyze_team_fixtures", {"team_name": "Test United", "num_gameweeks": 2}
        )

        self.assertIn("Next 2 Fixtures", output)
        self.assertIn("GW3", output)
        self.assertIn("GW4", output)


    async def test_transfer_candidate_fixtures_skip_the_finished_gameweek(self):
        """recommend_transfers listed the current GW even once it had been played."""
        bootstrap = _bootstrap()
        bootstrap["elements"] = [_element(1, 1, status="i", news="Knock")]
        store.bootstrap_data = BootstrapData(**bootstrap)
        store._build_player_indices()

        class _InjuredSquadClient(_FakeClient):
            async def get_my_team(self, entry_id):
                return {
                    "picks": [{"element": 1, "position": 1, "multiplier": 1,
                               "is_captain": False, "is_vice_captain": False,
                               "selling_price": 50, "purchase_price": 50}],
                    "chips": [],
                    "transfers": {"bank": 5, "value": 1000, "limit": 1,
                                  "made": 0, "cost": 4},
                }

        self.activate(_InjuredSquadClient())

        output = await self.call_tool("recommend_transfers")

        self.assertIn("Next fixtures:", output)
        # GW2 is the current gameweek but is already finished, so it must not appear
        self.assertNotIn("GW2(", output)
        self.assertIn("GW3(", output)


class SquadAnalysisTests(_StoreFixture):
    async def test_player_without_history_does_not_raise_keyerror(self):
        """The 'No data' branch omitted transfers_balance, which the formatter indexed."""
        class _NoHistoryClient(_FakeClient):
            async def get_my_team(self, entry_id):
                return {
                    "picks": [{"element": 1, "position": 1, "multiplier": 1,
                               "is_captain": True, "is_vice_captain": False,
                               "selling_price": 50, "purchase_price": 50}],
                    "chips": [],
                    "transfers": {"bank": 5, "value": 1000, "limit": 1,
                                  "made": 0, "cost": 4},
                }

        self.activate(_NoHistoryClient(history=[]))

        output = await self.call_tool("analyze_squad_recent_performance", {"num_gameweeks": 3})

        self.assertNotIn("transfers_balance", output)
        self.assertNotIn("Error analyzing squad performance", output)
        self.assertIn("Squad Performance Analysis", output)


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_compare_players_prompt_renders(self):
        """*args made the prompt impossible to satisfy through FastMCP."""
        result = await mcp.get_prompt("compare_players", {"player_names": "Haaland, Palmer"})
        text = " ".join(m.content.text for m in result.messages)

        self.assertIn("Haaland, Palmer", text)
        self.assertIn("(2 players)", text)

    async def test_compare_players_prompt_renders_without_arguments(self):
        result = await mcp.get_prompt("compare_players", {})
        text = " ".join(m.content.text for m in result.messages)

        self.assertIn("{player1}", text)

    async def test_compare_managers_prompt_renders(self):
        result = await mcp.get_prompt(
            "compare_managers",
            {"league_name": "Work League", "gameweek": "5", "manager_names": "Ana, Bo"},
        )
        text = " ".join(m.content.text for m in result.messages)

        self.assertIn("Work League", text)
        self.assertIn("Ana, Bo", text)
        self.assertIn("2 managers", text)


if __name__ == "__main__":
    unittest.main()

"""The MCP surface: what it exposes, and that it does not derive its own answers.

The tool these tests exist for is `recommend_transfers`. It used to re-derive transfer
advice from the live API - no points hit, no projections, no idea a wildcard was active
- so an MCP client was told to make moves the CLI would refuse. It is now a thin adapter
over `engine/recommend.py`, and `test_mcp_recommend_is_the_engines_answer` is the whole
point of the exercise: the tool's output must be the engine's output, character for
character, or there are two recommenders again.
"""

import ast
import asyncio
import pathlib
import re
import tempfile
import unittest

from pydantic import AnyUrl

from fpl_agent.client import FPLClient
from fpl_agent.engine import recommend as engine
from fpl_agent.engine import storage
from fpl_agent.models import BootstrapData
from fpl_agent.reference import reference
from fpl_agent.sessions import sessions
from fpl_agent.mcp import prompts as mcp_prompts  # noqa: F401  (registers prompts)
from fpl_agent.mcp import resources as mcp_resources  # noqa: F401  (registers them)
from fpl_agent.mcp.tools import get_active_session, mcp, set_active_session
from fpl_agent.mcp.tools import warehouse
from test_recommend import SeedMixin, chip

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "fpl_agent"


def _element(element_id, web_name, first, second, team=1):
    return {"id": element_id, "web_name": web_name, "first_name": first,
            "second_name": second, "team": team, "element_type": 3, "now_cost": 95,
            "form": "10.0", "points_per_game": "10.0", "news": "", "status": "a",
            "total_points": 20, "minutes": 157}


def _lookup_bootstrap():
    """Names chosen for the two cases the lookup tools are supposed to separate.

    "Saka" matches one player exactly and three by substring; "Silva" matches two
    equally well, which is the tie `get_player_details` used to break by guessing.
    """
    return {
        "elements": [
            _element(1, "Saka", "Bukayo", "Saka"),
            _element(2, "Sakamoto", "Tatsuhiro", "Sakamoto", team=2),
            _element(3, "Wan-Bissaka", "Aaron", "Wan-Bissaka", team=2),
            _element(4, "B.Silva", "Bernardo", "Silva"),
            _element(5, "M.Silva", "Marcos", "Silva", team=2),
        ],
        "teams": [
            {"id": 1, "name": "Arsenal", "short_name": "ARS", "strength": 4},
            {"id": 2, "name": "Test City", "short_name": "TSC", "strength": 3},
        ],
        "element_types": [
            {"id": 3, "singular_name_short": "MID", "plural_name_short": "MID"},
        ],
        "events": [{"id": 3, "name": "Gameweek 3", "deadline_time": "2025-09-13T10:00:00Z",
                    "finished": False, "data_checked": False,
                    "deadline_time_epoch": 1757757600, "is_previous": False,
                    "is_current": True, "is_next": False, "can_enter": False,
                    "released": True}],
    }


def _decorators(node: ast.AST) -> set[str]:
    """The dotted names of a definition's decorators, calls unwrapped."""
    names = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        names.add(".".join(reversed(parts)))
    return names


def _definitions(name: str) -> list[tuple[pathlib.Path, set[str]]]:
    """Every top-level def of `name` under src/, with its decorators."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == name:
                found.append((path, _decorators(node)))
    return found


class SurfaceTests(unittest.TestCase):
    """What the registry advertises. Recount here, never by hand in the README."""

    def test_only_one_recommend_transfers_tool(self) -> None:
        """The acceptance criterion for review item 15.

        A second implementation is not always a second MCP tool - the one that was
        deleted lived in `squad.py` and would have passed a check on the registry
        alone, because both registered under the same name and the later import won.
        So this reads the source: exactly one function named `recommend_transfers`
        may carry `@mcp.tool()`.
        """
        tools = [path for path, decorators in _definitions("recommend_transfers")
                 if "mcp.tool" in decorators]
        self.assertEqual([SRC / "mcp" / "tools" / "warehouse.py"], tools)

    def test_the_prompt_points_at_the_tool_rather_than_re_deriving(self) -> None:
        """A prompt template is allowed to stay; a second ranking method is not.

        The old prompt restated the deleted heuristic's scoring table verbatim
        (+100 injured, +50 DNP, ...), which is a second recommender written in
        English. It now names the tool instead.
        """
        prompts = [path for path, decorators in _definitions("recommend_transfers")
                   if "mcp.prompt" in decorators]
        self.assertEqual([SRC / "mcp" / "prompts.py"], prompts)
        body = mcp_prompts.recommend_transfers()
        self.assertIn("`recommend_transfers` tool", body)
        for scoring in ("+100", "+50", "Priority Score", "priority score"):
            self.assertNotIn(scoring, body)

    def test_registered_tool_names(self) -> None:
        """The exact surface, so a deletion or a rename cannot pass unnoticed.

        This is also the evidence behind the pruning: had anything still referenced
        a removed tool, its module would fail to import and this list would not
        build at all.
        """
        names = sorted(tool.name for tool in asyncio.run(mcp.list_tools()))
        self.assertEqual([
            "analyze_squad_recent_performance",
            "analyze_team_fixtures",
            "begin_web_login",
            "check_login_status",
            "check_player_availability",
            "compare_managers",
            "compare_players",
            "find_player",
            "get_auth_status",
            "get_authenticated_schema_diagnostics",
            "get_current_gameweek",
            "get_fixtures_for_gameweek",
            "get_gameweek_info",
            "get_injury_and_lineup_predictions",
            "get_league_standings",
            "get_manager_gameweek_team",
            "get_manager_snapshot",
            "get_my_info",
            "get_my_performance",
            "get_my_squad",
            "get_player_summary",
            "get_team_info",
            "get_top_players",
            "list_all_gameweeks",
            "list_all_teams",
            "login_to_fpl",
            "make_transfers",
            "poll_web_login",
            "recommend_chip_strategy",
            "recommend_transfers",
            "search_players",
            "search_players_by_team",
        ], names)

    def test_registered_prompt_names(self) -> None:
        names = sorted(prompt.name for prompt in asyncio.run(mcp.list_prompts()))
        self.assertEqual([
            "analyze_squad_performance",
            "analyze_team_fixtures",
            "compare_managers",
            "compare_players",
            "find_league_differentials",
            "recommend_chip_strategy",
            "recommend_transfers",
        ], names)

    def test_registered_resource_count(self) -> None:
        """Resources delegate to tools, so a deleted tool breaks them at import.

        Both lists are counted because the README quotes their sum.
        """
        fixed = asyncio.run(mcp.list_resources())
        templates = asyncio.run(mcp.list_resource_templates())
        self.assertEqual(8, len(fixed))
        self.assertEqual(9, len(templates))

    def test_every_resource_uri_a_prompt_names_actually_resolves(self) -> None:
        """A prompt sending the model to a URI the server does not serve is dead.

        Three of them were: `.../standings` without its page, `.../fixtures` without
        its gameweek count, and a `?num_gameweeks=` query string on a path template.
        Nothing raises when a prompt is rendered, so only reading them against the
        registry finds it.
        """
        registered = {str(resource.uri)
                      for resource in asyncio.run(mcp.list_resources())}
        patterns = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template.uriTemplate) + "$")
                    for template in asyncio.run(mcp.list_resource_templates())]

        source = (SRC / "mcp" / "prompts.py").read_text()
        named = {uri.replace("{{", "{").replace("}}", "}")
                 for uri in re.findall(r"fpl://[a-zA-Z0-9{}_/-]*", source)}
        self.assertTrue(named, "the prompts should still point at resources")

        for uri in sorted(named):
            concrete = re.sub(r"\{[^}]+\}", "X", uri)
            with self.subTest(uri=uri):
                self.assertTrue(
                    concrete in registered or any(p.match(concrete) for p in patterns),
                    f"{uri} is not a resource this server serves")

    def test_readme_quotes_the_registry(self) -> None:
        """The counts in the README are the counts the server advertises."""
        readme = (SRC.parent.parent / "README.md").read_text()
        tools = len(asyncio.run(mcp.list_tools()))
        resources = (len(asyncio.run(mcp.list_resources()))
                     + len(asyncio.run(mcp.list_resource_templates())))
        prompts = len(asyncio.run(mcp.list_prompts()))
        self.assertIn(
            f"exposes {tools} tools, {resources} resources", readme)
        self.assertIn(f"and {prompts} prompts", readme)


class RecommendTransfersToolTests(SeedMixin, unittest.TestCase):
    """The adapter. Every assertion here is about *not* having a second opinion."""

    def setUp(self) -> None:
        # No session, and none is restored. A stored recommendation is a read of the
        # warehouse; requiring a login to see it would guard nothing.
        set_active_session(None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = pathlib.Path(directory.name) / "fpl.db"
        original = warehouse.DB_PATH
        self.addCleanup(lambda: setattr(warehouse, "DB_PATH", original))
        warehouse.DB_PATH = self.db

    def _warehouse(self, **kwargs) -> None:
        """Seed the warehouse on disk and close it.

        The commit is not decoration: `_seed` leaves an open transaction, and the
        tool opens the file separately and read-only, so anything uncommitted is
        simply not there - which is how a fixture ends up testing "no snapshot"
        while claiming to test something else.
        """
        conn = self._seed(db=self.db, **kwargs)
        conn.commit()
        conn.close()

    def _call(self, **kwargs) -> str:
        """Through the registry, as a client would reach it."""
        blocks = asyncio.run(mcp.call_tool("recommend_transfers", kwargs))
        content = blocks[0] if isinstance(blocks, tuple) else blocks
        return "\n".join(block.text for block in content)

    def test_mcp_recommend_is_the_engines_answer(self) -> None:
        """One logic, two interfaces: the tool renders what the engine ranked.

        Not "agrees roughly" - the same string. Anything the tool computed itself
        would show up here as a difference.
        """
        conn = self._seed(bank=10, squad_ids=(1,), db=self.db)
        conn.commit()
        context = engine.transfer_context(conn)
        expected = engine.render(context, engine.recommend(conn, 3, 8), 3)
        conn.close()

        self.assertEqual(expected.lstrip("\n"), self._call())

    def test_wildcard_banner_reaches_the_client(self) -> None:
        """Under a wildcard the ranking means much less, and the tool must say so."""
        self._warehouse(squad_ids=(1,), chips=[chip("wildcard", "active")])

        output = self._call()
        self.assertIn("WILDCARD ACTIVE this gameweek", output)
        self.assertIn("a wildcard rebuilds the whole squad", output)

    def test_the_hit_is_charged_and_shown(self) -> None:
        """The deleted tool never charged one; this is what made it wrong.

        With the free transfer already spent, every option is docked four points and
        ranked on what is left.
        """
        self._warehouse(limit=1, made=1, cost=4, squad_ids=(1,))

        output = self._call()
        self.assertIn("no free transfers left", output)
        self.assertIn("charged a 4-point hit", output)
        self.assertIn("net +", output)

    def test_a_move_that_does_not_clear_its_hit_is_not_offered(self) -> None:
        """The engine drops it; the adapter must not resurrect it."""
        self._warehouse(limit=1, made=1, cost=100, squad_ids=(1,))

        self.assertIn("No transfer improves the squad", self._call())

    def test_no_session_is_required(self) -> None:
        """The warehouse is on disk. Nothing here needs an FPL login."""
        self._warehouse(squad_ids=(1,))

        output = self._call()
        self.assertNotIn("Not authenticated", output)
        self.assertIn("Transfer candidates over the next 3 gameweeks", output)

    def test_unprojected_horizon_is_a_readable_message(self) -> None:
        """`recommend` raises rather than projecting. The client gets prose."""
        self._warehouse(squad_ids=(1,), project=False)

        output = self._call()
        self.assertIn("Cannot recommend: no projections stored", output)
        self.assertIn("make deadline", output)
        self.assertNotIn("Traceback", output)

    def test_missing_warehouse_says_so_without_creating_one(self) -> None:
        """Opening read-only is the point: no empty warehouse that looks healthy."""
        warehouse.DB_PATH = self.db.parent / "absent.db"

        output = self._call()
        self.assertIn("No warehouse at", output)
        self.assertIn("make deadline", output)
        self.assertFalse(warehouse.DB_PATH.exists())

    def test_reading_leaves_the_warehouse_untouched(self) -> None:
        self._warehouse(squad_ids=(1,))
        before = self.db.read_bytes()

        self._call()

        self.assertEqual(before, self.db.read_bytes())

    def test_no_squad_captured_is_a_readable_message(self) -> None:
        """An unauthenticated snapshot has no squad to recommend against."""
        conn = storage.connect(self.db)
        conn.execute(
            "INSERT INTO snapshot (captured_at, gameweek) VALUES ('2025-01-01', 3)")
        conn.commit()
        conn.close()

        output = self._call()
        self.assertIn("Cannot recommend:", output)
        self.assertNotIn("Traceback", output)


class PlayerLookupTests(unittest.TestCase):
    """The two surviving name lookups, and why they are two and not one.

    `get_player_details` was the third. It ran the identical
    `reference.find_players_by_name` call and either returned find_player's card,
    told the caller to use find_player, or - among several equally good matches -
    picked one and presented the guess as an answer.
    """

    def setUp(self) -> None:
        self._saved = (reference.bootstrap_data, dict(reference.player_name_map),
                       dict(reference.player_id_map), get_active_session())
        reference.bootstrap_data = BootstrapData(**_lookup_bootstrap())
        reference._build_player_indices()
        client = FPLClient(reference=reference)
        client.user_info = {"player": {"entry": 1}}
        sessions.active_sessions["lookup"] = client
        set_active_session("lookup")
        self.addCleanup(sessions.active_sessions.pop, "lookup", None)

    def tearDown(self) -> None:
        (reference.bootstrap_data, reference.player_name_map,
         reference.player_id_map, session) = self._saved
        set_active_session(session)

    def _call(self, name, **kwargs) -> str:
        blocks = asyncio.run(mcp.call_tool(name, kwargs))
        content = blocks[0] if isinstance(blocks, tuple) else blocks
        return "".join(block.text for block in content)

    def test_find_player_answers_the_name_that_get_player_details_answered(self) -> None:
        """Its whole confident branch was this call and this formatter."""
        output = self._call("find_player", player_name="Saka")

        self.assertIn("**Saka** (Bukayo Saka)", output)
        self.assertIn("Position: MID", output)

    def test_find_player_lists_candidates_where_details_would_have_guessed(self) -> None:
        """`get_player_details` returned one of these and called it the answer."""
        output = self._call("find_player", player_name="Silva")

        self.assertIn("players matching", output)
        self.assertIn("Bernardo Silva", output)
        self.assertIn("Marcos Silva", output)

    def test_search_players_finds_what_find_player_short_circuits_past(self) -> None:
        """Why search_players survived the pruning.

        `find_players_by_name` returns on an exact hit and never runs its substring
        pass, so a query that names one player exactly can never surface the others
        who contain it. This is the counterexample, and it is why these two tools
        are not the same tool under two names.
        """
        found = self._call("find_player", player_name="Saka")
        searched = self._call("search_players", name_query="Saka")

        self.assertNotIn("Sakamoto", found)
        self.assertIn("Sakamoto", searched)
        self.assertIn("Wan-Bissaka", searched)

    def test_the_player_resource_is_the_find_player_tool(self) -> None:
        """Delegated, not reimplemented - resources.py says so in its own docstring.

        "Silva" is the name that catches a reimplementation: the inlined copy only
        disambiguated when the top score fell below 0.95, so two players scoring 1.0
        left it returning the first as though it were the answer, while the tool
        listed both. Asserting on an unambiguous name would pass either way, which is
        exactly how the two had drifted apart unnoticed.
        """
        for name in ("Saka", "Silva"):
            with self.subTest(player=name):
                resource = asyncio.run(mcp.read_resource(AnyUrl(f"fpl://player/{name}")))
                text = "".join(item.content for item in resource)

                self.assertEqual(self._call("find_player", player_name=name), text)


if __name__ == "__main__":
    unittest.main()

"""Rival capture and league-relative ownership."""
import os
import unittest

from fpl_agent.engine import rivals, storage
from fpl_agent.sessions import SessionRegistry


def storage_store():
    return SessionRegistry()
from fpl_agent.engine.recommend import (
    DIFFERENTIAL_EO, TEMPLATE_EO, ownership_profile,
)


def league(league_id=1, name="Mini", league_type="x", rank_count=6):
    return {"id": league_id, "name": name, "league_type": league_type,
            "rank_count": rank_count}


class CapturableLeagueTests(unittest.TestCase):
    def test_global_leagues_are_skipped(self):
        """FPL's own leagues are type 's' - Overall has ~9.9 million entries."""
        leagues = [league(314, "Overall", "s", 9904802),
                   league(226, "Sweden", "s", 165632),
                   league(18891, "Copperminers", "x", 6)]
        kept = rivals.capturable_leagues(leagues)
        self.assertEqual([lg["id"] for lg in kept], [18891])

    def test_large_private_leagues_are_skipped(self):
        leagues = [league(1, "Big", "x", 11143), league(2, "Small", "x", 8)]
        self.assertEqual([lg["id"] for lg in rivals.capturable_leagues(leagues)], [2])

    def test_the_cap_is_adjustable(self):
        leagues = [league(1, "Mid", "x", 120)]
        self.assertEqual(rivals.capturable_leagues(leagues, max_rivals=50), [])
        self.assertEqual(len(rivals.capturable_leagues(leagues, max_rivals=200)), 1)

    def test_global_can_be_forced(self):
        leagues = [league(314, "Overall", "s", 20)]
        self.assertEqual(len(rivals.capturable_leagues(leagues, include_global=True)), 1)

    def test_leagues_without_a_count_are_kept(self):
        leagues = [league(1, "Unknown size", "x", None)]
        self.assertEqual(len(rivals.capturable_leagues(leagues)), 1)


class LeagueSourceTests(unittest.IsolatedAsyncioTestCase):
    """Regression: /me/ does not carry league membership.

    It returns only `player` and `watched`. Reading leagues from it silently yielded an
    empty list, so every league tool reported "not found" for leagues the user is in.
    """

    class _Client:
        user_info = {"player": {"entry": 8884192}, "watched": []}   # a real /me/ shape

        def __init__(self):
            self.entry_calls = 0

        async def get_manager_entry(self, entry_id):
            self.entry_calls += 1
            return {"leagues": {"classic": [
                {"id": 920863, "name": "The inner", "league_type": "x", "rank_count": 6},
                {"id": 314, "name": "Overall", "league_type": "s", "rank_count": 9904802},
            ]}}

    async def test_leagues_come_from_the_entry_endpoint(self):
        isolated = storage_store()
        client = self._Client()
        leagues = await isolated.get_user_leagues(client)
        self.assertEqual([lg["id"] for lg in leagues], [920863, 314])
        self.assertEqual(client.entry_calls, 1)

    async def test_the_result_is_cached_per_entry(self):
        isolated = storage_store()
        client = self._Client()
        await isolated.get_user_leagues(client)
        await isolated.get_user_leagues(client)
        self.assertEqual(client.entry_calls, 1, "entry/{id}/ should not be refetched")

    async def test_find_league_by_name_uses_it(self):
        isolated = storage_store()
        found = await isolated.find_league_by_name(self._Client(), "The inner")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 920863)

    async def test_no_entry_id_yields_no_leagues(self):
        class _Anonymous:
            user_info = {"player": None, "watched": []}
        self.assertEqual(await storage_store().get_user_leagues(_Anonymous()), [])


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _squads(self, squads: dict[int, list[tuple[int, bool]]], gameweek=2):
        rows = []
        for entry_id, picks in squads.items():
            for position, (element_id, is_captain) in enumerate(picks, start=1):
                rows.append((entry_id, gameweek, element_id, position, 2 if is_captain else 1,
                             1 if is_captain else 0, 0))
        self.conn.executemany(
            "INSERT OR REPLACE INTO rival_squad VALUES (?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def test_effective_ownership_counts_the_captain_twice(self):
        """Haaland owned by all six and captained by three is 100% owned, 150% EO."""
        self._squads({
            1: [(100, True)], 2: [(100, True)], 3: [(100, True)],
            4: [(100, False)], 5: [(100, False)], 6: [(100, False)],
        })
        own = rivals.league_ownership(self.conn, 2)[100]
        self.assertEqual(own["managers"], 6)
        self.assertEqual(own["owned_by"], 6)
        self.assertAlmostEqual(own["ownership"], 1.0)
        self.assertAlmostEqual(own["effective_ownership"], 1.5)

    def test_partial_ownership(self):
        self._squads({1: [(7, False)], 2: [(7, False)], 3: [(9, False)], 4: [(9, False)]})
        own = rivals.league_ownership(self.conn, 2)
        self.assertAlmostEqual(own[7]["ownership"], 0.5)
        self.assertAlmostEqual(own[9]["effective_ownership"], 0.5)

    def test_no_rivals_captured_yields_nothing(self):
        self.assertEqual(rivals.league_ownership(self.conn, 2), {})

    def test_ownership_is_scoped_to_the_gameweek(self):
        self._squads({1: [(7, False)]}, gameweek=1)
        self._squads({1: [(8, False)]}, gameweek=2)
        self.assertIn(7, rivals.league_ownership(self.conn, 1))
        self.assertNotIn(7, rivals.league_ownership(self.conn, 2))


class ConfiguredLeagueTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(rivals.RIVAL_LEAGUES_ENV)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop(rivals.RIVAL_LEAGUES_ENV, None)
        else:
            os.environ[rivals.RIVAL_LEAGUES_ENV] = self._saved

    def _set(self, value):
        os.environ[rivals.RIVAL_LEAGUES_ENV] = value

    def test_unset_means_every_capturable_league(self):
        os.environ.pop(rivals.RIVAL_LEAGUES_ENV, None)
        self.assertIsNone(rivals.configured_league_ids())

    def test_single_and_multiple_ids(self):
        self._set("920863")
        self.assertEqual(rivals.configured_league_ids(), [920863])
        self._set("920863, 18891")
        self.assertEqual(rivals.configured_league_ids(), [920863, 18891])

    def test_junk_is_ignored_rather_than_fatal(self):
        self._set("920863,not-an-id,")
        self.assertEqual(rivals.configured_league_ids(), [920863])
        self._set("   ")
        self.assertIsNone(rivals.configured_league_ids())


class ScopedOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        # Two leagues. Entry 1 is in both; entries 2 and 3 sit in one each.
        self.conn.executemany(
            "INSERT OR REPLACE INTO league VALUES (?,?,?,?,?)",
            [(100, "Wanted", "x", 2, "now"), (200, "Other", "x", 2, "now")])
        self.conn.executemany(
            "INSERT OR REPLACE INTO rival VALUES (?,?,?,?,?,?)",
            [(1, 100, "A", "TA", 1, 10), (2, 100, "B", "TB", 2, 9),
             (1, 200, "A", "TA", 1, 10), (3, 200, "C", "TC", 2, 8)])
        self.conn.executemany(
            "INSERT OR REPLACE INTO rival_squad VALUES (?,?,?,?,?,?,?)",
            [(1, 2, 500, 1, 1, 0, 0),      # owned in both leagues
             (2, 2, 600, 1, 1, 0, 0),      # only in league 100
             (3, 2, 700, 1, 1, 0, 0)])     # only in league 200
        self.conn.commit()

    def test_scoping_changes_the_denominator_and_the_field(self):
        wanted = rivals.league_ownership(self.conn, 2, [100])
        self.assertEqual(wanted[500]["managers"], 2)       # entries 1 and 2 only
        self.assertIn(600, wanted)
        self.assertNotIn(700, wanted, "a player only owned in another league is out of scope")

    def test_unscoped_counts_everyone_captured(self):
        everyone = rivals.league_ownership(self.conn, 2)
        self.assertEqual(everyone[500]["managers"], 3)
        self.assertIn(700, everyone)

    def test_scoping_to_a_league_with_no_squads_is_empty(self):
        self.assertEqual(rivals.league_ownership(self.conn, 2, [999]), {})


class ProfileTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(ownership_profile(TEMPLATE_EO), "template")
        self.assertEqual(ownership_profile(1.5), "template")
        self.assertEqual(ownership_profile(DIFFERENTIAL_EO), "differential")
        self.assertEqual(ownership_profile(0.0), "differential")
        self.assertEqual(ownership_profile(0.3), "balanced")

    def test_unknown_only_when_rivals_were_never_captured(self):
        """A player nobody owns is a differential at 0%, not missing information."""
        self.assertEqual(ownership_profile(None), "unknown")
        self.assertEqual(ownership_profile(0.0), "differential")


if __name__ == "__main__":
    unittest.main()


def standings(*rows):
    """A league standings page as the API returns it: (entry, name, team, rank, total)."""
    return {"standings": {"results": [
        {"entry": e, "player_name": n, "entry_name": t, "rank": r, "total": p}
        for e, n, t, r, p in rows]}}


class _StandingsClient:
    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.picks_calls = 0

    async def get_league_standings(self, league_id):
        return self.pages[league_id]

    async def get_manager_gameweek_picks(self, entry_id, gameweek):
        self.picks_calls += 1
        return {"picks": []}


class StandingsRefreshTests(unittest.IsolatedAsyncioTestCase):
    """The cheap half of `rivals`: the table, refreshed without touching the picks."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO league VALUES (?,?,?,?,?)",
                          (100, "Mini", "x", 6, "2026-09-05T02:30:00+00:00"))
        self.conn.executemany(
            "INSERT INTO rival VALUES (?,?,?,?,?,?)",
            [(1, 100, "Emil", "E FC", 1, 188), (2, 100, "Mattias", "M FC", 3, 186),
             (3, 100, "Christoffer", "C FC", 2, 186)])
        self.conn.executemany(
            "INSERT INTO rival_squad VALUES (?,?,?,?,?,?,?)",
            [(1, 3, 500, 1, 2, 1, 0), (2, 3, 500, 1, 1, 0, 0), (3, 3, 600, 1, 1, 0, 0)])
        self.conn.commit()

    async def test_ranks_and_totals_move_and_the_picks_do_not(self):
        """The GW4 table from issue #49: ranks 2 and 3 transposed, every total moved."""
        before = self.conn.execute("SELECT * FROM rival_squad ORDER BY 1,2,3").fetchall()
        client = _StandingsClient({100: standings(
            (1, "Emil", "E FC", 1, 210), (2, "Mattias", "M FC", 2, 194),
            (3, "Christoffer", "C FC", 3, 191), (9, "Me", "Own FC", 6, 140))})
        await rivals.refresh_standings(self.conn, client, league(100), own_entry=9)
        rows = {r["entry_id"]: (r["rank"], r["total_points"]) for r in
                self.conn.execute("SELECT * FROM rival WHERE league_id = 100")}
        self.assertEqual(rows, {1: (1, 210), 2: (2, 194), 3: (3, 191)})
        captured = self.conn.execute("SELECT captured_at FROM league").fetchone()[0]
        self.assertGreater(captured, "2026-09-05T02:30:00+00:00")
        after = self.conn.execute("SELECT * FROM rival_squad ORDER BY 1,2,3").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        self.assertEqual(client.picks_calls, 0)

    async def test_leagues_come_from_the_warehouse_and_the_own_entry_from_my_state(self):
        """No login: the ids were recorded by the first full run, the entry by snapshot."""
        self.conn.execute("INSERT INTO snapshot (id, captured_at) VALUES (1, 'now')")
        self.conn.execute("INSERT INTO my_state (snapshot_id, entry_id) VALUES (1, 9)")
        self.conn.commit()
        client = _StandingsClient({100: standings(
            (1, "Emil", "E FC", 1, 210), (9, "Me", "Own FC", 6, 140))})
        refreshed = await rivals.refresh_known_standings(self.conn, client)
        self.assertEqual(refreshed, 1)
        entries = {r[0] for r in self.conn.execute("SELECT entry_id FROM rival")}
        self.assertNotIn(9, entries, "your own entry is not a rival")
        self.assertIn(1, entries)

    async def test_wanted_ids_narrow_the_warehouse_set(self):
        self.conn.execute("INSERT INTO league VALUES (?,?,?,?,?)", (200, "Other", "x", 4, "old"))
        self.conn.commit()
        client = _StandingsClient({100: standings((1, "Emil", "E FC", 1, 210)),
                                   200: standings((7, "X", "X FC", 1, 1))})
        self.assertEqual(await rivals.refresh_known_standings(self.conn, client, [200]), 1)
        ages = dict(self.conn.execute("SELECT id, captured_at FROM league"))
        self.assertEqual(ages[100], "2026-09-05T02:30:00+00:00", "league 100 was not asked for")
        self.assertNotEqual(ages[200], "old")

    async def test_without_a_recorded_entry_nobody_is_excluded_and_it_says_so(self):
        client = _StandingsClient({100: standings(
            (1, "Emil", "E FC", 1, 210), (9, "Me", "Own FC", 6, 140))})
        with self.assertLogs("fpl_rivals", level="WARNING") as logs:
            await rivals.refresh_known_standings(self.conn, client)
        self.assertIn(9, {r[0] for r in self.conn.execute("SELECT entry_id FROM rival")})
        self.assertTrue(any("own entry" in line for line in logs.output), logs.output)

    async def test_no_known_league_refreshes_nothing(self):
        self.conn.execute("DELETE FROM rival")
        self.conn.execute("DELETE FROM league")
        self.conn.commit()
        self.assertEqual(await rivals.refresh_known_standings(self.conn, _StandingsClient({})), 0)

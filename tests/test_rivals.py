"""Rival capture and league-relative ownership."""
import unittest

from fpl_agent import rivals, storage
from fpl_agent.recommend import (
    DIFFERENTIAL_EO, TEMPLATE_EO, ownership_profile,
)


def league(league_id=1, name="Mini", league_type="x", rank_count=6):
    return {"id": league_id, "name": name, "league_type": league_type,
            "rank_count": rank_count}


class CapturableLeagueTests(unittest.TestCase):
    def test_global_leagues_are_skipped(self):
        """FPL's own leagues are type 's' - Overall has ~9.9 million entries."""
        user = {"leagues": {"classic": [
            league(314, "Overall", "s", 9904802),
            league(226, "Sweden", "s", 165632),
            league(18891, "Copperminers", "x", 6),
        ]}}
        kept = rivals.capturable_leagues(user)
        self.assertEqual([lg["id"] for lg in kept], [18891])

    def test_large_private_leagues_are_skipped(self):
        user = {"leagues": {"classic": [league(1, "Big", "x", 11143), league(2, "Small", "x", 8)]}}
        self.assertEqual([lg["id"] for lg in rivals.capturable_leagues(user)], [2])

    def test_the_cap_is_adjustable(self):
        user = {"leagues": {"classic": [league(1, "Mid", "x", 120)]}}
        self.assertEqual(rivals.capturable_leagues(user, max_rivals=50), [])
        self.assertEqual(len(rivals.capturable_leagues(user, max_rivals=200)), 1)

    def test_global_can_be_forced(self):
        user = {"leagues": {"classic": [league(314, "Overall", "s", 20)]}}
        self.assertEqual(len(rivals.capturable_leagues(user, include_global=True)), 1)

    def test_leagues_without_a_count_are_kept(self):
        user = {"leagues": {"classic": [league(1, "Unknown size", "x", None)]}}
        self.assertEqual(len(rivals.capturable_leagues(user)), 1)


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

"""Which capture a reader means. Offline: every warehouse here is built in memory.

These are the rules the callers used to each state for themselves - `brief`, `status`,
`lineups`, `recommend`, `projection`, `pricing`, `settle`, `rivals`. Their own tests
check what they say about the answer; this file checks the answer.
"""
import unittest

from fpl_agent.engine import storage, warehouse
from fpl_agent.engine.projection import MODEL_VERSION
from test_status import WarehouseBuilder


class WarehouseTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.w = WarehouseBuilder(self.conn)


class LatestTests(WarehouseTestCase):
    def test_an_empty_warehouse_has_no_capture(self):
        self.assertIsNone(warehouse.latest(self.conn))

    def test_the_highest_id_wins_whatever_it_targets(self):
        self.w.snapshot(gameweek=4)
        newest = self.w.snapshot(gameweek=3)
        capture = warehouse.latest(self.conn)
        self.assertEqual(capture.id, newest)
        self.assertEqual(capture.gameweek, 3)

    def test_the_value_carries_the_row(self):
        self.w.snapshot(gameweek=3, captured_at="2026-09-04T11:29:31+00:00")
        capture = warehouse.latest(self.conn)
        self.assertEqual(capture.captured_at, "2026-09-04T11:29:31+00:00")
        self.assertEqual(capture.kind, "test")

    def test_a_capture_with_no_target_is_still_the_latest(self):
        """The season may be over; that is the caller's call, not a missing capture."""
        self.w.snapshot(gameweek=None)
        self.assertIsNone(warehouse.latest(self.conn).gameweek)


class WithLineupsTests(WarehouseTestCase):
    def test_no_lineups_for_the_gameweek_is_none(self):
        newest = self.w.snapshot(gameweek=3)
        self.w.lineups(newest, 2)
        self.assertIsNone(warehouse.with_lineups(self.conn, 3))

    def test_an_older_capture_holding_the_lineups_wins_over_a_newer_one_without(self):
        """RotoWire publishes near matchday; a later capture legitimately holds none."""
        older = self.w.snapshot(gameweek=3)
        self.w.lineups(older, 3)
        self.w.snapshot(gameweek=3)
        self.assertEqual(warehouse.with_lineups(self.conn, 3).id, older)

    def test_the_most_recent_holder_wins_when_several_hold_them(self):
        self.w.lineups(self.w.snapshot(gameweek=3), 3)
        newer = self.w.snapshot(gameweek=3)
        self.w.lineups(newer, 3)
        self.assertEqual(warehouse.with_lineups(self.conn, 3).id, newer)

    def test_the_holder_need_not_target_the_gameweek(self):
        """Filing is per fixture: a capture between the gameweek 2 deadline and that
        round's last kickoff targets 3 and files 2's lineups."""
        straddling = self.w.snapshot(gameweek=3)
        self.w.lineups(straddling, 2)
        capture = warehouse.with_lineups(self.conn, 2)
        self.assertEqual(capture.id, straddling)
        self.assertEqual(capture.gameweek, 3)


class WithSquadTests(WarehouseTestCase):
    def test_no_capture_ever_logged_in_is_none(self):
        self.w.snapshot(gameweek=3)
        self.assertIsNone(warehouse.with_squad(self.conn))

    def test_a_market_only_capture_after_a_logged_in_one_does_not_hide_it(self):
        logged_in = self.w.snapshot(gameweek=3)
        self.w.squad(logged_in)
        self.w.state(logged_in)
        self.w.snapshot(gameweek=3)
        self.assertEqual(warehouse.with_squad(self.conn).id, logged_in)


class ProjectedTests(WarehouseTestCase):
    def test_nothing_projected_is_none(self):
        self.w.snapshot(gameweek=3)
        self.assertIsNone(warehouse.projected(self.conn, 3, MODEL_VERSION))

    def test_a_horizon_row_from_a_capture_targeting_an_earlier_round_does_not_count(self):
        """Projected *for* gameweek 3 from a capture targeting 2 is what the model said a
        week early, not what it believed at decision time."""
        early = self.w.snapshot(gameweek=2)
        self.w.projections(early, 3)
        self.assertIsNone(warehouse.projected(self.conn, 3, MODEL_VERSION))

    def test_the_model_version_must_match(self):
        capture = self.w.snapshot(gameweek=3)
        self.w.projections(capture, 3, model_version="0.0.1-old")
        self.assertIsNone(warehouse.projected(self.conn, 3, MODEL_VERSION))
        self.assertEqual(warehouse.projected(self.conn, 3, "0.0.1-old").id, capture)

    def test_the_most_recent_projected_capture_targeting_the_round_wins(self):
        self.w.projections(self.w.snapshot(gameweek=3), 3)
        later = self.w.snapshot(gameweek=3)
        self.w.projections(later, 3)
        self.w.snapshot(gameweek=3)  # captured but never projected
        self.assertEqual(warehouse.projected(self.conn, 3, MODEL_VERSION).id, later)


if __name__ == "__main__":
    unittest.main()

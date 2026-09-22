"""Which capture a reader means, and what each gameweek holds. Offline: every warehouse
here is built in memory.

These are the rules the callers used to each state for themselves - `brief`, `status`,
`lineups`, `recommend`, `projection`, `pricing`, `settle`, `rivals`, `schedule`. Their
own tests check what they say about the answer; this file checks the answer.
"""
import unittest
from datetime import datetime, timezone

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


class DeadlineTests(WarehouseTestCase):
    """When a gameweek locks, derived from its fixtures - the warehouse keeps no
    `deadline_time`."""

    def kickoff(self, fixture_id, gameweek, stamp, finished=False):
        storage.upsert_fixtures(self.conn, [
            {"id": fixture_id, "event": gameweek, "kickoff_time": stamp,
             "finished": finished}])

    def test_the_deadline_is_ninety_minutes_before_the_first_kickoff(self):
        self.kickoff(300, 3, "2026-09-06T14:00:00Z")
        self.kickoff(301, 3, "2026-09-05T15:00:00Z")
        self.assertEqual(warehouse.deadline(self.conn, 3),
                         datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc))

    def test_played_fixtures_do_not_move_it(self):
        """The bug this rule replaced: the earliest *unplayed* kickoff walked forward
        through a gameweek under way, putting a deadline before every remaining match."""
        self.kickoff(300, 3, "2026-09-05T15:00:00Z", finished=True)
        self.kickoff(301, 3, "2026-09-07T20:00:00Z")
        self.assertEqual(warehouse.deadline(self.conn, 3),
                         datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc))

    def test_no_fixtures_means_no_deadline_rather_than_a_guess(self):
        """Absence of fixtures is absence of evidence, the rule settle already follows."""
        self.assertIsNone(warehouse.deadline(self.conn, 3))

    def test_a_trailing_z_is_read_as_utc(self):
        self.kickoff(300, 3, "2026-09-05T15:00:00Z")
        self.assertEqual(warehouse.deadline(self.conn, 3).tzinfo, timezone.utc)


class TargetTests(WarehouseTestCase):
    """The gameweek decisions are made for, and when it locks, in one read."""

    def test_an_empty_warehouse_has_no_target(self):
        self.assertIsNone(warehouse.target(self.conn))

    def test_it_is_the_latest_captures_gameweek_and_that_gameweeks_deadline(self):
        storage.upsert_fixtures(self.conn, [
            {"id": 300, "event": 3, "kickoff_time": "2026-09-05T15:00:00Z"},
            {"id": 400, "event": 4, "kickoff_time": "2026-09-12T15:00:00Z"}])
        self.w.snapshot(gameweek=3)
        newest = self.w.snapshot(gameweek=4)
        target = warehouse.target(self.conn)
        self.assertEqual(target.capture.id, newest)
        self.assertEqual(target.gameweek, 4)
        self.assertEqual(target.deadline,
                         datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc))

    def test_a_capture_with_no_target_gameweek_has_no_deadline(self):
        self.w.snapshot(gameweek=None)
        self.assertIsNone(warehouse.target(self.conn).deadline)


class RequireTargetTests(WarehouseTestCase):
    """The gameweek a command works on, or one wording of why there is none."""

    def test_nothing_captured_is_refused(self):
        with self.assertRaisesRegex(LookupError, "no snapshot captured yet"):
            warehouse.require_target(self.conn)

    def test_a_capture_with_no_target_gameweek_is_refused(self):
        self.w.snapshot(gameweek=None)
        with self.assertRaisesRegex(LookupError, "the season may be over"):
            warehouse.require_target(self.conn)

    def test_unnamed_it_is_the_target(self):
        self.w.snapshot(gameweek=3)
        self.assertEqual(warehouse.require_target(self.conn), warehouse.target(self.conn))

    def test_a_named_gameweek_is_carried_with_its_own_deadline_not_swapped(self):
        storage.upsert_fixtures(self.conn, [
            {"id": 300, "event": 3, "kickoff_time": "2026-09-05T15:00:00Z"},
            {"id": 400, "event": 4, "kickoff_time": "2026-09-12T15:00:00Z"}])
        newest = self.w.snapshot(gameweek=3)
        target = warehouse.require_target(self.conn, 4)
        self.assertEqual(target.capture.id, newest)
        self.assertEqual(target.gameweek, 4)
        self.assertEqual(target.deadline,
                         datetime(2026, 9, 12, 13, 30, tzinfo=timezone.utc))


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


class LedgerTestCase(WarehouseTestCase):
    def ledger(self, model_version=MODEL_VERSION):
        return warehouse.gameweeks(self.conn, model_version)


class FinishedTests(LedgerTestCase):
    def test_an_empty_warehouse_has_no_rounds(self):
        self.assertEqual(self.ledger().rounds, ())
        self.assertEqual(self.ledger().finished(), [])

    def test_every_fixture_played_is_finished(self):
        self.w.fixtures(1)
        self.w.fixtures(2)
        self.assertEqual(self.ledger().finished(), [1, 2])

    def test_no_fixtures_recorded_is_not_finished(self):
        """Absence of fixtures is absence of evidence, not a completed gameweek."""
        self.w.actuals(9)
        self.assertFalse(self.ledger().get(9).finished)
        self.assertFalse(self.ledger().get(10).finished)
        self.assertNotIn(9, self.ledger().finished())

    def test_one_unplayed_fixture_is_not_finished(self):
        self.w.fixtures(4, finished=True)
        self.w.fixtures(4, finished=False, count=1)  # ids collide, so this replaces one
        entry = self.ledger().get(4)
        self.assertEqual((entry.fixtures, entry.played), (2, 1))
        self.assertFalse(entry.finished)
        self.assertEqual(self.ledger().finished(), [])

    def test_rounds_are_ascending_whatever_order_they_were_written(self):
        self.w.fixtures(3)
        self.w.fixtures(1)
        self.w.fixtures(2, finished=False)
        self.assertEqual([g.round for g in self.ledger().rounds], [1, 2, 3])


class HasActualsTests(LedgerTestCase):
    def test_zero_rows_never_passes_whatever_the_fixtures_say(self):
        self.w.fixtures(2, finished=True, count=0)
        self.assertFalse(self.ledger().get(2).has_actuals)

    def test_a_handful_of_rows_is_not_a_fetched_round(self):
        """Half a backfill is still a backfill that failed."""
        self.w.fixtures(2, finished=True)
        self.w.actuals(2, players=5)
        self.assertFalse(self.ledger().get(2).has_actuals)

    def test_eleven_a_side_per_played_fixture_is_the_floor(self):
        self.w.fixtures(2, finished=True, count=2)
        self.w.actuals(2, players=43)
        self.assertFalse(self.ledger().get(2).has_actuals)
        self.w.actuals(2, players=44)
        self.assertTrue(self.ledger().get(2).has_actuals)

    def test_backfilled_through_is_the_highest_round_with_any_row(self):
        self.assertIsNone(self.ledger().backfilled_through())
        self.w.actuals(1)
        self.w.actuals(3, players=1)
        self.assertEqual(self.ledger().backfilled_through(), 3)


class SettleableTests(LedgerTestCase):
    def _projected_round(self, gameweek, finished=True, model_version=MODEL_VERSION):
        snapshot_id = self.w.snapshot(gameweek=gameweek)
        self.w.projections(snapshot_id, gameweek, model_version=model_version)
        self.w.fixtures(gameweek, finished=finished)
        return snapshot_id

    def test_finished_projected_and_ungraded_is_offered_oldest_first(self):
        self._projected_round(4)
        self._projected_round(3)
        self.assertEqual(self.ledger().settleable(), [3, 4])

    def test_an_unfinished_round_is_never_offered(self):
        self._projected_round(3, finished=False)
        self.assertTrue(self.ledger().get(3).projected)
        self.assertEqual(self.ledger().settleable(), [])

    def test_a_horizon_row_from_an_earlier_targeting_capture_does_not_count(self):
        """Projected *for* gameweek 3 from a capture targeting 2: not the decision-time
        record, so gameweek 3 is not projected as far as grading is concerned."""
        capture = self.w.snapshot(gameweek=2)
        self.w.projections(capture, 3)
        self.w.fixtures(3)
        self.assertFalse(self.ledger().get(3).projected)
        self.assertEqual(self.ledger().settleable(), [])

    def test_a_graded_round_is_not_offered_again(self):
        self.w.graded_gameweek(3)
        self.w.fixtures(3)
        self.assertTrue(self.ledger().get(3).graded)
        self.assertEqual(self.ledger().settleable(), [])

    def test_graded_under_another_version_is_still_settleable_under_this_one(self):
        self.w.graded_gameweek(3, model_version="0.0.1")
        self._projected_round(3)
        self.assertEqual(self.ledger().settleable(), [3])
        self.assertEqual(self.ledger("0.0.1").settleable(), [])

    def test_the_version_must_match_to_count_as_projected(self):
        self._projected_round(3, model_version="0.0.1")
        self.assertFalse(self.ledger().get(3).projected)
        self.assertTrue(self.ledger("0.0.1").get(3).projected)

    def test_a_round_nothing_is_known_about_is_an_empty_entry(self):
        entry = self.ledger().get(30)
        self.assertEqual((entry.fixtures, entry.played, entry.actuals), (0, 0, 0))
        self.assertFalse(entry.projected or entry.graded or entry.finished)


if __name__ == "__main__":
    unittest.main()

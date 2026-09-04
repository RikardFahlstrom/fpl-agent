"""The gameweek brief and the four triggers.

Offline: every warehouse here is built in memory and every file is written to a temporary
directory. Nothing in this file may touch `data/fpl.db`, the real `logs/`, or the token
cache - the status check the brief calls is pointed at a temporary cache through
FPL_TOKEN_CACHE, which is the knob `headless_auth.cache_path` reads in production.

The tests that matter most are the ones about *not* firing. A notifier is only worth
having if silence means "checked and declined", so most of what follows drives the
warehouse into a state where something nearly fires and then asserts that it did not, and
why.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fpl_agent.engine import brief, storage
from fpl_agent.engine import status as status_module
from fpl_agent.engine.projection import MODEL_VERSION

# The default warehouse kicks off at 09:00 on 5 September, so the derived deadline is
# 07:30 that morning - 19.5 hours after NOW, inside the 24-hour window trigger 3 watches.
# Tests that want the deadline outside the window move `now`, not the fixtures.
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
GAMEWEEK = 3


class Warehouse:
    """A warehouse the brief reads clean, with one lever per state worth testing.

    Built rather than fixtured because every trigger is a statement about a *combination*
    of rows - a deadline, a chip, a squad and a projected horizon - and a fixture file
    hides which of them a given test is actually moving.
    """

    def __init__(self, conn):
        self.conn = conn
        self.snapshot_id = None

    # -- the pieces -------------------------------------------------------
    # 20 clubs, so `recommend`'s three-per-club limit is never the reason a candidate is
    # dropped. With three clubs every swap was refused and every "does the threshold
    # work" test passed for the wrong reason.
    CLUBS = 20

    def teams(self):
        for i in range(1, self.CLUBS + 1):
            self.conn.execute("INSERT OR REPLACE INTO team VALUES (?,?,?,'{}')",
                              (i, f"Club {i}", f"C{i:02d}"))

    def players(self, n=40):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,?,'t','t')",
                (element_id, f"P{element_id}", "First", f"Last{element_id}",
                 1 + (element_id - 1) % self.CLUBS, 1 + (element_id - 1) % 4))

    def game_config(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO game_config VALUES ('t','{}',?,'{}','[]')",
            ('{"squad_team_limit": 3}',))

    def snapshot(self, gameweek=GAMEWEEK, captured_at="2026-09-04T09:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO snapshot (captured_at, gameweek, kind) VALUES (?,?,'test')",
            (captured_at, gameweek))
        self.snapshot_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]
        return self.snapshot_id

    def player_snapshots(self, n=40, status="a", chance=None, news=""):
        for element_id in range(1, n + 1):
            self.conn.execute(
                """INSERT OR REPLACE INTO player_snapshot
                   (snapshot_id, element_id, now_cost, status,
                    chance_of_playing_next_round, news, price_change_percent,
                    price_change_projections, transfers_in_event, transfers_out_event,
                    raw)
                   VALUES (?,?,?,?,?,?,0.0,'[]',0,0,'{}')""",
                (self.snapshot_id, element_id, 50, status, chance, news))

    def flag(self, element_id, status="i", chance=None, news="Knee injury"):
        self.conn.execute(
            """UPDATE player_snapshot SET status = ?, chance_of_playing_next_round = ?,
               news = ? WHERE snapshot_id = ? AND element_id = ?""",
            (status, chance, news, self.snapshot_id, element_id))

    def falling(self, element_id, projected=-120.0):
        self.conn.execute(
            """UPDATE player_snapshot SET price_change_projections = ?
               WHERE snapshot_id = ? AND element_id = ?""",
            (f'[{{"offset": 0, "projected_percent": {projected}, "likelihood": -5}}]',
             self.snapshot_id, element_id))

    def squad(self, element_ids=None):
        """15 players, positions 1-15. Positions 1-11 are the XI."""
        element_ids = element_ids or list(range(1, 16))
        for position, element_id in enumerate(element_ids, start=1):
            self.conn.execute(
                "INSERT OR REPLACE INTO my_squad VALUES (?,?,?,1,0,0,50,50)",
                (self.snapshot_id, element_id, position))

    def state(self, bank=10, free_transfers=1, transfer_cost=4, chips="[]"):
        self.conn.execute(
            "INSERT OR REPLACE INTO my_state VALUES (?,1,?,1000,?,?,?)",
            (self.snapshot_id, bank, free_transfers, transfer_cost, chips))

    def wildcard(self):
        self.state(chips='[{"chip_type": "transfer", "name": "wildcard", '
                         '"status_for_entry": "active"}]')

    def projections(self, gameweek=GAMEWEEK, weeks=3, n=40, better=(),
                    model_version=MODEL_VERSION):
        """Everyone projects 1.0 a gameweek; `better` players project 2.0.

        Over a three-gameweek horizon that is a 3.0 gross gain for swapping any ordinary
        player for a `better` one - comfortably over the 2.0 bar, which is what makes the
        threshold tests about the threshold rather than about the arithmetic.
        """
        for gw in range(gameweek, gameweek + weeks):
            for element_id in range(1, n + 1):
                self.conn.execute(
                    """INSERT OR REPLACE INTO projection
                       (snapshot_id, gameweek, element_id, model_version,
                        expected_points, p_start, expected_minutes, fixture_count,
                        components, created_at)
                       VALUES (?,?,?,?,?,0.9,80,1,'{}','t')""",
                    (self.snapshot_id, gw, element_id, model_version,
                     2.0 if element_id in better else 1.0))

    def fixtures(self, gameweek=GAMEWEEK, kickoff="2026-09-05T09:00:00Z",
                 finished=False, count=2):
        for i in range(count):
            self.conn.execute(
                "INSERT OR REPLACE INTO fixture VALUES (?,?,1,2,3,3,NULL,NULL,?,?,'{}')",
                (gameweek * 100 + i, gameweek, kickoff, 1 if finished else 0))

    def lineups(self, gameweek=GAMEWEEK, n=40, snapshot_id=None):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO predicted_lineup "
                "VALUES (?,?,?,'ARS','rotowire','M',1,NULL,0)",
                (snapshot_id or self.snapshot_id, gameweek, element_id))

    def lineup_out(self, element_id, gameweek=GAMEWEEK, code="OUT"):
        self.conn.execute(
            "INSERT OR REPLACE INTO predicted_lineup "
            "VALUES (?,?,?,'ARS','rotowire','M',0,?,0)",
            (self.snapshot_id, gameweek, element_id, code))

    # -- the whole thing --------------------------------------------------
    def healthy(self, better=(16, 17, 18)):
        """A warehouse `status` calls clean and `recommend` can rank."""
        self.teams()
        self.players()
        self.game_config()
        self.snapshot()
        self.player_snapshots()
        self.squad()
        self.state()
        self.projections(better=better)
        self.fixtures()
        self.lineups()
        self.conn.execute(
            "INSERT OR REPLACE INTO rival_squad VALUES (99, 2, 1, 1, 1, 0, 0)")
        self.conn.commit()
        return self


class BriefTestCase(unittest.TestCase):
    """A fresh in-memory warehouse and a token cache that is never the real one."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.warehouse = Warehouse(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        patch = mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(Path(self.tmp.name) / "token.json"),
             "FPL_RIVAL_LEAGUES": ""},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def evaluate(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return brief.evaluate(self.conn, GAMEWEEK, **kwargs)

    def fired(self, evaluation):
        return [t.name for t in evaluation.triggers]


# --------------------------------------------------------------------------
# The seam: names, shapes, and the rule that an action is never empty
# --------------------------------------------------------------------------

class TriggerShapeTests(unittest.TestCase):

    def make(self, **kwargs):
        fields = dict(name="t", headline="h", detail="d", action="do this",
                      fingerprint="f")
        fields.update(kwargs)
        return brief.Trigger(**fields)

    def test_a_trigger_with_no_action_is_refused(self):
        """The review's stated cure for notification spam, enforced rather than hoped."""
        for empty in ("", "   ", None):
            with self.subTest(action=empty):
                with self.assertRaises(ValueError):
                    self.make(action=empty)

    def test_a_trigger_with_no_fingerprint_is_refused(self):
        with self.assertRaises(ValueError):
            self.make(fingerprint="")

    def test_a_headline_that_will_not_fit_a_lock_screen_is_refused(self):
        with self.assertRaises(ValueError):
            self.make(headline="x" * (brief.HEADLINE_MAX + 1))
        with self.assertRaises(ValueError):
            self.make(headline="two\nlines")

    def test_headline_collapses_and_truncates_rather_than_raising(self):
        """Construction sites go through `headline`, so a long player name cannot crash
        a scheduled brief."""
        self.assertEqual(brief.headline("a  b\nc"), "a b c")
        long = brief.headline("x" * 500)
        self.assertLessEqual(len(long), brief.HEADLINE_MAX)
        self.make(headline=long)  # and the result is acceptable to the constructor

    def test_triggers_are_hashable_and_comparable(self):
        """The notifier will put these in sets and dicts keyed by fingerprint."""
        self.assertEqual(self.make(), self.make())
        self.assertEqual(len({self.make(), self.make()}), 1)

    def test_the_four_names_are_the_documented_set(self):
        self.assertEqual(
            brief.TRIGGER_NAMES,
            ("status_failed", "squad_player_unavailable", "deadline_with_move",
             "move_worth_making"))


class BriefPathTests(unittest.TestCase):

    def test_gwnn_is_zero_padded_so_a_listing_sorts(self):
        self.assertEqual(brief.brief_path(3), Path("logs/gw03.md"))
        self.assertEqual(brief.brief_path(10), Path("logs/gw10.md"))

    def test_the_root_is_overridable(self):
        self.assertEqual(brief.brief_path(3, Path("/tmp/x")), Path("/tmp/x/gw03.md"))


class ThresholdTests(unittest.TestCase):

    def test_the_default_is_the_stated_constant(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(brief.worth_making_threshold(), brief.WORTH_MAKING_NET_XP)

    def test_the_environment_overrides_it(self):
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "3.5"}):
            self.assertEqual(brief.worth_making_threshold(), 3.5)

    def test_an_unreadable_override_falls_back_rather_than_failing_the_run(self):
        """A brief that dies on a typo in the ini tells the owner nothing at all."""
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "soon"}):
            self.assertEqual(brief.worth_making_threshold(), brief.WORTH_MAKING_NET_XP)


# --------------------------------------------------------------------------
# The deadline, which the warehouse does not store
# --------------------------------------------------------------------------

class DeadlineTests(BriefTestCase):

    def test_the_deadline_is_ninety_minutes_before_the_first_kickoff(self):
        self.warehouse.fixtures(kickoff="2026-09-05T15:00:00Z")
        self.warehouse.fixtures(gameweek=GAMEWEEK, kickoff="2026-09-06T14:00:00Z",
                                count=1)
        self.assertEqual(
            brief.gameweek_deadline(self.conn, GAMEWEEK),
            datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc))

    def test_no_fixtures_means_no_deadline_rather_than_a_guess(self):
        """Absence of fixtures is absence of evidence, the rule settle already follows."""
        self.assertIsNone(brief.gameweek_deadline(self.conn, GAMEWEEK))

    def test_a_trailing_z_is_read_as_utc(self):
        self.warehouse.fixtures(kickoff="2026-09-05T15:00:00Z", count=1)
        self.assertEqual(brief.gameweek_deadline(self.conn, GAMEWEEK).tzinfo,
                         timezone.utc)


# --------------------------------------------------------------------------
# Trigger 1: status_failed
# --------------------------------------------------------------------------

class StatusFailedTests(BriefTestCase):

    def test_it_does_not_fire_on_a_healthy_warehouse(self):
        self.warehouse.healthy()
        evaluation = self.evaluate()
        self.assertNotIn("status_failed", self.fired(evaluation))
        self.assertIn("passed", evaluation.silent["status_failed"])

    def test_a_warn_is_not_an_inconsistency(self):
        """WARN exists for states a healthy warehouse passes through; firing on them is
        how a notification becomes something people mute."""
        self.warehouse.healthy()
        self.conn.execute("DELETE FROM rival_squad")     # -> rivals warns
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertNotIn("status_failed", self.fired(evaluation))
        self.assertIn("rivals", evaluation.silent["status_failed"])

    def test_a_missing_squad_fires(self):
        self.warehouse.healthy()
        self.conn.execute("DELETE FROM my_squad")
        self.conn.commit()
        triggers = [t for t in self.evaluate().triggers if t.name == "status_failed"]
        self.assertEqual(len(triggers), 1)
        self.assertIn("squad", triggers[0].headline)
        self.assertTrue(triggers[0].action.strip())

    def test_the_fingerprint_is_the_failing_labels_not_their_details(self):
        """An hourly job re-evaluates the same broken squad dozens of times. A squad that
        goes from 0 rows to 14 is still the same fault, and must not be sent twice."""
        self.warehouse.healthy()
        self.conn.execute("DELETE FROM my_squad")
        self.conn.commit()
        first = self.evaluate().triggers[0].fingerprint

        self.warehouse.squad(element_ids=list(range(1, 15)))   # 14 of 15: still FAIL
        self.conn.commit()
        second = self.evaluate().triggers[0].fingerprint
        self.assertEqual(first, second)

    def test_the_fingerprint_moves_when_a_different_check_fails(self):
        self.warehouse.healthy()
        self.conn.execute("DELETE FROM my_squad")
        self.conn.commit()
        squad_only = self.evaluate().triggers[0].fingerprint

        self.conn.execute("DELETE FROM projection")
        self.conn.commit()
        both = self.evaluate().triggers[0].fingerprint
        self.assertNotEqual(squad_only, both)


# --------------------------------------------------------------------------
# Trigger 2: squad_player_unavailable
# --------------------------------------------------------------------------

class SquadPlayerUnavailableTests(BriefTestCase):

    def test_a_clean_squad_does_not_fire_and_says_what_it_checked(self):
        self.warehouse.healthy()
        evaluation = self.evaluate()
        self.assertNotIn("squad_player_unavailable", self.fired(evaluation))
        self.assertIn("all 15 squad players checked",
                      evaluation.silent["squad_player_unavailable"])

    def test_an_injured_player_fires_once_with_an_action(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "i", news="Hamstring")
        self.conn.commit()
        triggers = [t for t in self.evaluate().triggers
                    if t.name == "squad_player_unavailable"]
        self.assertEqual(len(triggers), 1)
        self.assertIn("P4", triggers[0].headline)
        self.assertIn("injured", triggers[0].headline)
        self.assertIn("P4", triggers[0].action)
        self.assertEqual(triggers[0].fingerprint,
                         f"squad_player_unavailable:gw{GAMEWEEK}:p4:i")

    def test_a_suspension_fires(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "s", news="Suspended")
        self.conn.commit()
        triggers = [t for t in self.evaluate().triggers
                    if t.name == "squad_player_unavailable"]
        self.assertEqual(len(triggers), 1)
        self.assertIn("suspended", triggers[0].headline)

    def test_a_doubt_does_not_fire(self):
        """`projection.availability` has already scaled the whole projection by the
        percentage. Pushing it as unavailable charges the same doubt twice."""
        self.warehouse.healthy()
        self.warehouse.flag(4, "d", chance=75, news="Knock - 75% chance")
        self.conn.commit()
        self.assertNotIn("squad_player_unavailable", self.fired(self.evaluate()))

    def test_out_in_the_predicted_lineup_fires_even_when_fpl_says_available(self):
        """The case worth knowing about: FPL's flag only moves when there is news."""
        self.warehouse.healthy()
        self.warehouse.lineup_out(4)
        self.conn.commit()
        triggers = [t for t in self.evaluate().triggers
                    if t.name == "squad_player_unavailable"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0].fingerprint,
                         f"squad_player_unavailable:gw{GAMEWEEK}:p4:lineup-out")

    def test_a_suspension_code_the_scraper_table_misses_still_counts(self):
        """`lineups.UNAVAILABLE` is imported, not restated, so SUS is covered."""
        self.warehouse.healthy()
        self.warehouse.lineup_out(4, code="SUS")
        self.conn.commit()
        self.assertIn("squad_player_unavailable", self.fired(self.evaluate()))

    def test_merely_being_benched_in_the_lineup_does_not_fire(self):
        """Rotation belongs in the projection's start rate, not on a phone."""
        self.warehouse.healthy()
        self.warehouse.lineup_out(4, code=None)
        self.conn.commit()
        self.assertNotIn("squad_player_unavailable", self.fired(self.evaluate()))

    def test_one_trigger_per_player_so_each_can_be_acted_on_separately(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "i")
        self.warehouse.flag(9, "s")
        self.conn.commit()
        triggers = [t for t in self.evaluate().triggers
                    if t.name == "squad_player_unavailable"]
        self.assertEqual(len(triggers), 2)
        self.assertEqual(len({t.fingerprint for t in triggers}), 2)

    def test_an_fpl_flag_outranks_a_lineup_prediction_in_the_fingerprint(self):
        """A doubt confirmed by the club is news, and is reported once more."""
        self.warehouse.healthy()
        self.warehouse.lineup_out(4)
        self.conn.commit()
        predicted = self.evaluate().triggers[0].fingerprint
        self.warehouse.flag(4, "i")
        self.conn.commit()
        confirmed = self.evaluate().triggers[0].fingerprint
        self.assertNotEqual(predicted, confirmed)
        self.assertTrue(confirmed.endswith(":i"))


# --------------------------------------------------------------------------
# Trigger 3: deadline_with_move
# --------------------------------------------------------------------------

class DeadlineWithMoveTests(BriefTestCase):

    def setUp(self):
        super().setUp()
        self.warehouse.healthy()

    def test_it_fires_inside_the_window_with_a_free_transfer_and_a_positive_move(self):
        triggers = [t for t in self.evaluate().triggers
                    if t.name == "deadline_with_move"]
        self.assertEqual(len(triggers), 1)
        self.assertIn("deadline", triggers[0].headline)
        self.assertTrue(triggers[0].action.strip())
        self.assertTrue(triggers[0].fingerprint.startswith(
            f"deadline_with_move:gw{GAMEWEEK}:"))

    def test_a_deadline_that_has_passed_does_not_fire(self):
        evaluation = self.evaluate(now=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc))
        self.assertNotIn("deadline_with_move", self.fired(evaluation))
        self.assertIn("passed", evaluation.silent["deadline_with_move"])

    def test_a_deadline_further_out_than_a_day_does_not_fire(self):
        evaluation = self.evaluate(now=datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc))
        self.assertNotIn("deadline_with_move", self.fired(evaluation))
        self.assertIn("away", evaluation.silent["deadline_with_move"])

    def test_no_free_transfer_does_not_fire(self):
        self.warehouse.state(free_transfers=0)
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertNotIn("deadline_with_move", self.fired(evaluation))
        self.assertIn("no free transfer", evaluation.silent["deadline_with_move"])

    def test_an_unrecorded_free_transfer_count_is_priced_as_none(self):
        """A snapshot that failed to record `transfers.limit` is not evidence that a free
        transfer exists - the rule `recommend.transfer_price` already owns."""
        self.warehouse.state(free_transfers=None)
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertNotIn("deadline_with_move", self.fired(evaluation))
        self.assertIn("no free-transfer count", evaluation.silent["deadline_with_move"])

    def test_no_positive_move_does_not_fire(self):
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertNotIn("deadline_with_move", self.fired(evaluation))

    def test_the_fingerprint_is_the_move_and_not_the_hours_left(self):
        """The whole reason the notifier can exist: an hourly job must not re-send an
        unchanged fact just because the clock moved."""
        early = self.evaluate(now=datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc))
        late = self.evaluate(now=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc))
        self.assertEqual([t.fingerprint for t in early.triggers],
                         [t.fingerprint for t in late.triggers])

    def test_the_fingerprint_moves_when_the_recommended_move_changes(self):
        before = [t.fingerprint for t in self.evaluate().triggers
                  if t.name == "deadline_with_move"]
        # A different player becomes the best target, so the advice is genuinely new.
        self.conn.execute("UPDATE projection SET expected_points = 9.0 "
                          "WHERE element_id = 20")
        self.conn.commit()
        after = [t.fingerprint for t in self.evaluate().triggers
                 if t.name == "deadline_with_move"]
        self.assertNotEqual(before, after)


# --------------------------------------------------------------------------
# Trigger 4: move_worth_making - the one the word "worth" is doing work in
# --------------------------------------------------------------------------

class MoveWorthMakingTests(BriefTestCase):

    def setUp(self):
        super().setUp()
        self.warehouse.healthy()

    def test_a_move_over_the_bar_fires_with_the_net_in_its_headline(self):
        triggers = [t for t in self.evaluate().triggers if t.name == "move_worth_making"]
        self.assertEqual(len(triggers), 1)
        self.assertIn("+3.00", triggers[0].headline)
        self.assertTrue(triggers[0].action.strip())

    def test_it_is_not_gated_on_the_deadline_being_near(self):
        """A move that clears the bar is worth knowing about on a Tuesday."""
        evaluation = self.evaluate(now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc))
        self.assertIn("move_worth_making", self.fired(evaluation))
        self.assertNotIn("deadline_with_move", self.fired(evaluation))

    def test_topping_the_list_is_not_enough(self):
        """The recommender always emits a ranked list; the bar is what makes it advice."""
        self.conn.execute(
            "UPDATE projection SET expected_points = 1.3 WHERE element_id IN (16,17,18)")
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertTrue(evaluation.listing["moves"])          # a list still exists
        self.assertNotIn("move_worth_making", self.fired(evaluation))
        self.assertIn("under the", evaluation.silent["move_worth_making"])

    def test_an_active_wildcard_stops_it_and_says_so(self):
        """Hits cost nothing under a chip, so every candidate reads positive - and the
        tool ranks single swaps while a wildcard rebuilds all fifteen. Wrong shape, not
        merely optimistic."""
        self.warehouse.wildcard()
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertTrue(evaluation.listing["moves"])
        self.assertNotIn("move_worth_making", self.fired(evaluation))
        self.assertIn("wildcard is active", evaluation.silent["move_worth_making"])

    def test_a_free_hit_stops_it_too(self):
        """The gate is on any transfer chip, not on the wildcard by name."""
        self.warehouse.state(chips='[{"chip_type": "transfer", "name": "freehit", '
                                   '"status_for_entry": "active"}]')
        self.conn.commit()
        self.assertNotIn("move_worth_making", self.fired(self.evaluate()))

    def test_a_team_chip_does_not_stop_it(self):
        """A bench boost changes what the squad scores, not what a move costs."""
        self.warehouse.state(chips='[{"chip_type": "team", "name": "bboost", '
                                   '"status_for_entry": "active"}]')
        self.conn.commit()
        self.assertIn("move_worth_making", self.fired(self.evaluate()))

    def test_a_deadline_that_has_gone_stops_it_because_acting_is_impossible(self):
        evaluation = self.evaluate(now=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc))
        self.assertNotIn("move_worth_making", self.fired(evaluation))
        self.assertIn("deadline passed", evaluation.silent["move_worth_making"])

    def test_zero_free_transfers_sends_the_whole_list_net_negative(self):
        """The owner's second real state: every option is charged 4 points, nothing
        survives it, `recommend` returns nothing, and nothing fires."""
        self.warehouse.state(free_transfers=0)
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertEqual(evaluation.listing["moves"], [])
        self.assertEqual(self.fired(evaluation), [])

    def test_the_threshold_is_overridable_from_the_environment(self):
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "5.0"}):
            self.assertNotIn("move_worth_making", self.fired(self.evaluate()))
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "1.0"}):
            self.assertIn("move_worth_making", self.fired(self.evaluate()))

    def test_the_fingerprint_ignores_an_expected_points_float_that_jitters(self):
        """Prices tick and lineups firm up hourly. The move is the fact; its xP is not."""
        before = [t.fingerprint for t in self.evaluate().triggers
                  if t.name == "move_worth_making"]
        self.conn.execute("UPDATE projection SET expected_points = expected_points + 0.01 "
                          "WHERE element_id IN (16,17,18)")
        self.conn.commit()
        after = [t.fingerprint for t in self.evaluate().triggers
                 if t.name == "move_worth_making"]
        self.assertEqual(before, after)
        self.assertTrue(all(f for f in after))

    def test_no_horizon_projected_is_a_sentence_not_a_traceback(self):
        self.conn.execute("DELETE FROM projection WHERE gameweek = ?", (GAMEWEEK + 2,))
        self.conn.commit()
        evaluation = self.evaluate()
        self.assertEqual(evaluation.listing["moves"], [])
        self.assertIn("horizon", evaluation.silent["move_worth_making"])


# --------------------------------------------------------------------------
# Every trigger, together
# --------------------------------------------------------------------------

class AllTriggersTests(BriefTestCase):

    def all_four(self):
        """A warehouse in which every one of the four fires at once."""
        self.warehouse.healthy()
        self.conn.execute("DELETE FROM my_squad WHERE element_id = 15")  # -> status FAIL
        self.warehouse.flag(4, "i")
        self.conn.commit()

    def test_every_fired_trigger_has_an_action_and_a_lock_screen_headline(self):
        self.all_four()
        triggers = self.evaluate().triggers
        self.assertGreaterEqual(len(triggers), 2)
        for trigger in triggers:
            with self.subTest(trigger=trigger.name):
                self.assertTrue(trigger.action.strip())
                self.assertLessEqual(len(trigger.headline), brief.HEADLINE_MAX)
                self.assertNotIn("\n", trigger.headline)
                self.assertIn(trigger.name, brief.TRIGGER_NAMES)

    def test_fingerprints_are_unique_within_a_run(self):
        self.all_four()
        fingerprints = [t.fingerprint for t in self.evaluate().triggers]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))

    def test_every_fingerprint_is_stable_across_runs_at_different_moments(self):
        self.all_four()
        first = [t.fingerprint for t in self.evaluate(
            now=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)).triggers]
        second = [t.fingerprint for t in self.evaluate(
            now=datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)).triggers]
        self.assertEqual(first, second)

    def test_no_fingerprint_carries_a_snapshot_id_or_a_timestamp(self):
        """Both drift on every hourly run, and either would make silence impossible."""
        self.all_four()
        snapshot_id = str(self.warehouse.snapshot_id)
        for trigger in self.evaluate().triggers:
            with self.subTest(trigger=trigger.name):
                self.assertNotIn("2026", trigger.fingerprint)
                self.assertNotIn(f"snapshot{snapshot_id}", trigger.fingerprint)

    def test_evaluate_triggers_is_the_documented_seam(self):
        self.all_four()
        triggers = brief.evaluate_triggers(self.conn, GAMEWEEK, now=NOW)
        self.assertEqual(triggers, self.evaluate().triggers)
        self.assertIsInstance(triggers, list)
        self.assertIsInstance(triggers[0], brief.Trigger)

    def test_every_trigger_either_fires_or_records_why_it_did_not(self):
        """The point of the whole thing: silence has to be accountable."""
        self.warehouse.healthy()
        evaluation = self.evaluate()
        accounted = {t.name for t in evaluation.triggers} | set(evaluation.silent)
        self.assertEqual(accounted, set(brief.TRIGGER_NAMES))


# --------------------------------------------------------------------------
# The written brief
# --------------------------------------------------------------------------

class RenderBriefTests(BriefTestCase):

    def render(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return brief.render_brief(self.conn, GAMEWEEK, **kwargs)

    def test_the_wildcard_banner_comes_before_anything_it_would_change(self):
        """It re-reads every recommendation under it, so a reader who scrolls past it has
        been misled by the layout rather than by the numbers."""
        self.warehouse.healthy()
        self.warehouse.wildcard()
        self.conn.commit()
        text = self.render()
        self.assertIn("WILDCARD ACTIVE", text)
        self.assertLess(text.index("WILDCARD ACTIVE"), text.index("## What needs you"))
        self.assertLess(text.index("WILDCARD ACTIVE"), text.index("Transfers ranked"))

    def test_what_needs_you_comes_before_the_tables(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertLess(text.index("## What needs you"),
                        text.index("## Transfers ranked"))
        self.assertLess(text.index("## What needs you"), text.index("## Warehouse"))

    def test_nothing_firing_still_names_what_was_checked(self):
        """A wildcard week, days before the deadline: the exact state the real warehouse
        was in when this was written, and the one where an unexplained "nothing" would
        be indistinguishable from a brief that checked nothing."""
        self.warehouse.healthy()
        self.warehouse.wildcard()
        self.conn.commit()
        text = self.render(now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc))
        self.assertIn("Nothing needs you", text)
        for name in brief.TRIGGER_NAMES:
            self.assertIn(f"`{name}`", text)

    def test_a_fired_trigger_is_rendered_with_its_action(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "i", news="Hamstring")
        self.conn.commit()
        text = self.render()
        self.assertIn("**Do:**", text)
        self.assertIn("P4", text)

    def test_a_falling_holding_is_in_the_brief_and_is_not_a_trigger(self):
        """The owner's explicit choice: the most frequent signal stays off the phone."""
        self.warehouse.healthy()
        self.warehouse.falling(3)
        self.conn.commit()
        text = self.render()
        section = text.split("## Price watch")[1].split("\n## ")[0]
        self.assertIn("P3", section)
        self.assertIn("not** a notification", " ".join(section.split()))
        self.assertNotIn("price", " ".join(t.name for t in self.evaluate().triggers))

    def test_an_ungraded_warehouse_says_so_rather_than_showing_an_empty_table(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("No gameweek has been graded yet", text)

    def test_a_settled_gameweek_reports_its_calibration(self):
        self.warehouse.healthy()
        self.conn.execute(
            """INSERT INTO outcome VALUES (1, 1, 2, ?, 5.0, 2.0, 3.0, 0.9, 50, 3, 't')""",
            (MODEL_VERSION,))
        self.conn.commit()
        text = self.render()
        self.assertIn("Gameweek 2 under model", text)
        self.assertIn("+3.00", text)

    def test_the_deadline_and_free_transfers_are_stated(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("2026-09-05T07:30", text)
        self.assertIn("Free transfers", text)

    def test_a_passed_deadline_says_transfers_land_next_gameweek(self):
        self.warehouse.healthy()
        text = self.render(now=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc))
        self.assertIn("has passed", text)

    def test_the_status_report_is_carried_verbatim(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("## Warehouse", text)
        for label in ("snapshot", "squad", "projections", "lineups", "actuals",
                      "grading", "rivals", "decisions"):
            self.assertIn(label, text.split("## Warehouse")[1])

    def test_an_empty_warehouse_renders_rather_than_raising(self):
        """A brief that crashes on a fresh clone is a brief nobody can use to find out
        why the clone is empty."""
        text = brief.render_brief(self.conn, GAMEWEEK, now=NOW)
        self.assertIn("# Gameweek 3 brief", text)
        self.assertIn("no snapshot captured", text)

    def test_a_market_only_snapshot_renders_rather_than_raising(self):
        self.warehouse.teams()
        self.warehouse.players()
        self.warehouse.game_config()
        self.warehouse.snapshot()
        self.warehouse.player_snapshots()
        self.warehouse.projections()
        self.warehouse.fixtures()
        self.conn.commit()
        text = self.render()
        self.assertIn("No squad captured", text)
        self.assertIn("no squad captured", text.lower())

    def test_one_evaluation_serves_both_halves_of_the_page(self):
        """Reading the warehouse twice for one page is how its two halves disagree."""
        self.warehouse.healthy()
        evaluation = self.evaluate()
        self.assertEqual(self.render(evaluation=evaluation), self.render())


# --------------------------------------------------------------------------
# Writing the file, and never writing anything else
# --------------------------------------------------------------------------

class WriteBriefTests(BriefTestCase):

    def test_it_creates_the_directory_and_writes_gwnn(self):
        self.warehouse.healthy()
        root = Path(self.tmp.name) / "logs"
        self.assertFalse(root.exists())
        path = brief.write_brief(self.conn, GAMEWEEK, root, now=NOW)
        self.assertEqual(path, root / "gw03.md")
        self.assertIn("# Gameweek 3 brief", path.read_text())

    def test_a_second_run_replaces_the_file_rather_than_appending(self):
        self.warehouse.healthy()
        root = Path(self.tmp.name) / "logs"
        first = brief.write_brief(self.conn, GAMEWEEK, root, now=NOW).read_text()
        second = brief.write_brief(self.conn, GAMEWEEK, root, now=NOW).read_text()
        self.assertEqual(first, second)

    def test_the_brief_writes_nothing_to_the_warehouse(self):
        """Read-only for the same reason `status` is: a brief that could change the
        warehouse is a brief you cannot trust to describe it."""
        self.warehouse.healthy()
        path = Path(self.tmp.name) / "fpl.db"
        disk = storage.connect(path)
        Warehouse(disk).healthy()
        disk.close()

        # A read-only connection raises on any write, so a clean render is the proof.
        conn = status_module.connect_readonly(path)
        try:
            text = brief.render_brief(conn, GAMEWEEK, now=NOW)
        finally:
            conn.close()
        self.assertIn("# Gameweek 3 brief", text)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

class CommandTests(BriefTestCase):

    def setUp(self):
        super().setUp()
        self.db = Path(self.tmp.name) / "fpl.db"
        disk = storage.connect(self.db)
        Warehouse(disk).healthy()
        disk.close()
        self.logs = Path(self.tmp.name) / "logs"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = brief.main(["--db", str(self.db), "--logs", str(self.logs), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_the_brief_for_the_latest_snapshots_target(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue((self.logs / "gw03.md").exists())
        self.assertIn("gw03.md", out)

    def test_gameweek_is_overridable(self):
        code, _, _ = self.run_cli("--gameweek", "4")
        self.assertEqual(code, 0)
        self.assertTrue((self.logs / "gw04.md").exists())

    def test_dry_run_prints_and_writes_nothing(self):
        code, out, err = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.logs.exists())
        self.assertIn("# Gameweek 3 brief", out)
        self.assertIn("DID NOT FIRE", err)

    def test_dry_run_explains_every_trigger_that_stayed_silent(self):
        _, _, err = self.run_cli("--dry-run")
        for name in brief.TRIGGER_NAMES:
            self.assertIn(name, err)

    def test_a_missing_warehouse_is_a_sentence_and_exit_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = brief.main(["--db", str(Path(self.tmp.name) / "nope.db")])
        self.assertEqual(code, brief.EXIT_UNREADABLE)
        self.assertIn("no warehouse", err.getvalue())

    def test_the_command_is_registered_on_the_dispatcher(self):
        from fpl_agent import cli
        self.assertIn("brief", cli.COMMANDS)
        self.assertEqual(cli.COMMANDS["brief"][0], "fpl_agent.engine.brief")


if __name__ == "__main__":
    unittest.main()

"""Settle grades the Instructions: captain, XI, bench order and plan, in points scored.

On the brief's hand-built warehouse. The advice is built and stored before the deadline,
the gameweek is then finished and every player given the points a test chose, so each grade
is checked against a number worked out by hand.
"""

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fpl_agent.engine import grading, held, instructions, settle, storage, warehouse
from fpl_agent.engine.projection import MODEL_VERSION

from test_brief import GAMEWEEK
from test_instructions import SHAPED, InstructionsTestCase

#: Before the deadline (07:30 UTC on 5 September), so it is the record that was acted on.
BEFORE = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


class GradingTestCase(InstructionsTestCase):

    def setUp(self):
        super().setUp()
        # Two unowned midfielders worth +3.0 each; two free transfers to spend on them.
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.execute("UPDATE projection SET expected_points = 2.0 "
                          "WHERE element_id IN (23, 27)")
        self.w.state(free_transfers=2)
        self.conn.commit()
        capture = warehouse.latest(self.conn)
        self.built = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION,
                                        held.read(self.conn, capture), capture=capture)
        self.record_id, _ = instructions.store(
            self.conn, self.built, snapshot_id=capture.id, model_version=MODEL_VERSION,
            now=BEFORE)
        self.conn.commit()
        self.lineup = self.built.lineup
        self.everyone = [s.element_id for s in (*self.lineup.xi, *self.lineup.bench)]
        self.play_all(points=1)

    def play_all(self, points=1, minutes=90):
        """Everyone in the recommended fifteen and both incoming players, one point each."""
        for element_id in {*self.everyone, 23, 27, *SHAPED}:
            self.score(element_id, points, minutes)

    def score(self, element_id, points, minutes=90):
        self.conn.execute(
            "INSERT OR REPLACE INTO player_gameweek (element_id, round, total_points, "
            "minutes, raw) VALUES (?, ?, ?, ?, '{}')", (element_id, GAMEWEEK, points, minutes))
        self.conn.commit()

    def by_name(self, name):
        return next(s for s in (*self.lineup.xi, *self.lineup.bench) if s.name == name)

    def grade(self) -> grading.Grade:
        graded = grading.grade(self.conn, GAMEWEEK, MODEL_VERSION)
        self.assertIsNotNone(graded)
        return graded


class CaptainTests(GradingTestCase):

    def test_the_captain_is_ranked_against_the_other_starters(self):
        other = next(s for s in self.lineup.xi if s is not self.lineup.captain
                     and s.element_id != self.lineup.vice.element_id)
        self.score(self.lineup.captain.element_id, 5)
        self.score(other.element_id, 11)
        grade = self.grade().captain
        self.assertEqual((grade.chosen, grade.points, grade.rank, grade.of),
                         (self.lineup.captain.name, 5, 2, 11))
        self.assertEqual((grade.best, grade.best_points, grade.regret), (other.name, 11, 6))

    def test_the_best_captain_has_no_regret(self):
        self.score(self.lineup.captain.element_id, 12)
        grade = self.grade().captain
        self.assertEqual((grade.rank, grade.regret), (1, 0))

    def test_a_captain_who_did_not_play_hands_the_armband_to_the_vice(self):
        self.score(self.lineup.captain.element_id, 0, minutes=0)
        self.score(self.lineup.vice.element_id, 7)
        grade = self.grade().captain
        self.assertEqual(grade.wore_it, self.lineup.vice.name)
        self.assertEqual(grade.points, 7)


class XITests(GradingTestCase):

    def test_the_best_legal_eleven_from_the_same_fifteen_is_the_yardstick(self):
        benched = next(s for s in self.lineup.bench if s.element_type == 3)
        starter = next(s for s in self.lineup.xi if s.element_type == 3
                       and s.element_id not in (self.lineup.captain.element_id,))
        self.score(benched.element_id, 15)
        grade = self.grade().xi
        self.assertEqual(grade.points, 11)
        self.assertEqual(grade.best_points, 11 - 1 + 15)
        self.assertEqual(grade.regret, 14)
        self.assertEqual(len(grade.swaps), 1)
        self.assertEqual(grade.swaps[0][0], benched.name)
        self.assertIn(grade.swaps[0][1], {s.name for s in self.lineup.xi})
        del starter

    def test_a_best_eleven_is_said_to_be_best(self):
        text = grading.render(GAMEWEEK, MODEL_VERSION, self.grade())
        self.assertIn("the best eleven", text)


class BenchTests(GradingTestCase):

    def outfield_bench(self):
        return [s for s in self.lineup.bench if s.element_type != 1]

    def test_a_substitution_follows_the_bench_order(self):
        first, second, _ = self.outfield_bench()
        starter = next(s for s in self.lineup.xi if s.element_type == 3
                       and s.element_id != self.lineup.captain.element_id)
        self.score(starter.element_id, 0, minutes=0)
        self.score(first.element_id, 2)
        self.score(second.element_id, 9)
        grade = self.grade().bench
        came = [(i, o) for i, o, _ in grade.came_on]
        self.assertEqual(came, [(first.name, starter.name)])
        self.assertEqual(grade.gained, 2)

    def test_the_best_order_is_found_and_the_regret_is_the_difference(self):
        first, second, _ = self.outfield_bench()
        starter = next(s for s in self.lineup.xi if s.element_type == 3
                       and s.element_id != self.lineup.captain.element_id)
        self.score(starter.element_id, 0, minutes=0)
        self.score(first.element_id, 2)
        self.score(second.element_id, 9)
        grade = self.grade().bench
        self.assertEqual((grade.best_gained, grade.regret), (9, 7))
        self.assertEqual(grade.best_order[1], second.name)

    def test_a_sub_who_would_break_the_formation_is_skipped(self):
        # Only three defenders start in some formations; a midfielder cannot replace one.
        defenders = [s for s in self.lineup.xi if s.element_type == 2]
        count = len(defenders)
        if count > 3:
            self.skipTest("this lineup has spare defenders")
        self.score(defenders[0].element_id, 0, minutes=0)
        grade = self.grade().bench
        for sub, _, _ in grade.came_on:
            self.assertEqual(self.by_name(sub).element_type, 2)

    def test_a_goalkeeper_is_only_replaced_by_the_spare_goalkeeper(self):
        keeper = next(s for s in self.lineup.xi if s.element_type == 1)
        spare = next(s for s in self.lineup.bench if s.element_type == 1)
        self.score(keeper.element_id, 0, minutes=0)
        self.score(spare.element_id, 6)
        grade = self.grade().bench
        self.assertEqual([(i, o) for i, o, _ in grade.came_on], [(spare.name, keeper.name)])
        self.assertEqual(grade.gained, 6)


class PlanTests(GradingTestCase):

    def test_the_plan_is_graded_against_doing_nothing(self):
        moves = self.built.moves
        self.assertEqual(len(moves), 2)
        for move in moves:
            self.score(move.in_id, 10)
        graded = self.grade().plan
        self.assertEqual(len(graded.moves), 2)
        # Both arrivals start in place of one-pointers (+18); the best of them is also the
        # captain, whose 10 doubles where doing nothing's captain would add only 1 (+9).
        self.assertEqual(graded.gain, 27)
        self.assertEqual(graded.hit_cost, 0)

    def test_a_hit_is_charged_against_the_plan(self):
        self.w.state(free_transfers=0)
        self.conn.execute("UPDATE projection SET expected_points = 4.0 "
                          "WHERE element_id IN (23, 27)")
        self.conn.commit()
        capture = warehouse.latest(self.conn)
        built = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION,
                                   held.read(self.conn, capture), capture=capture)
        self.conn.execute("DELETE FROM instructions")
        instructions.store(self.conn, built, snapshot_id=capture.id,
                           model_version=MODEL_VERSION, now=BEFORE)
        for move in built.moves:
            self.score(move.in_id, 1)
        graded = self.grade().plan
        self.assertEqual(graded.hit_cost, 4 * len(built.moves))
        self.assertEqual(graded.plan_points - graded.nothing_points, -graded.hit_cost)

    def test_a_plan_of_no_move_is_doing_nothing(self):
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.commit()
        capture = warehouse.latest(self.conn)
        built = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION,
                                   held.read(self.conn, capture), capture=capture)
        self.conn.execute("DELETE FROM instructions")
        instructions.store(self.conn, built, snapshot_id=capture.id,
                           model_version=MODEL_VERSION, now=BEFORE)
        graded = self.grade()
        self.assertEqual(graded.plan.moves, ())
        self.assertIn("no move", grading.render(GAMEWEEK, MODEL_VERSION, graded))


class NoRecordTests(GradingTestCase):

    def test_a_gameweek_with_no_instructions_says_so_rather_than_inventing_a_grade(self):
        self.conn.execute("DELETE FROM instructions")
        self.conn.commit()
        self.assertIsNone(grading.grade(self.conn, GAMEWEEK, MODEL_VERSION))
        text = grading.render(GAMEWEEK, MODEL_VERSION, None)
        self.assertIn("none were stored before its deadline", text)

    def test_only_a_record_made_before_the_deadline_is_graded(self):
        self.conn.execute("DELETE FROM instructions")
        instructions.store(
            self.conn, self.built, snapshot_id=1, model_version=MODEL_VERSION,
            now=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))      # after the lock
        self.conn.commit()
        self.assertIsNone(grading.grade(self.conn, GAMEWEEK, MODEL_VERSION))

    def test_the_last_record_before_the_deadline_is_the_one_graded(self):
        later, _ = instructions.store(
            self.conn, self.built, snapshot_id=1, model_version=MODEL_VERSION,
            now=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc))
        self.conn.commit()
        self.assertEqual(self.grade().record.id, later)

    def test_other_model_versions_are_not_graded(self):
        self.assertIsNone(grading.grade(self.conn, GAMEWEEK, "0.0.0-other"))


class ReadOnlyTests(GradingTestCase):

    def dump(self):
        return self.conn.execute("SELECT * FROM instructions ORDER BY id").fetchall()

    def test_grading_does_not_alter_the_graded_record(self):
        before = [tuple(row) for row in self.dump()]
        grading.render(GAMEWEEK, MODEL_VERSION, self.grade())
        self.assertEqual([tuple(row) for row in self.dump()], before)

    def test_it_works_over_a_read_only_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fpl.db"
            disk = storage.connect(path)
            self.conn.backup(disk)
            disk.close()
            conn = storage.connect_readonly(path)
            try:
                self.assertIsNotNone(grading.grade(conn, GAMEWEEK, MODEL_VERSION))
            finally:
                conn.close()


class RenderTests(GradingTestCase):

    def test_it_prints_how_each_part_fared(self):
        text = grading.render(GAMEWEEK, MODEL_VERSION, self.grade())
        for label in ("Captain", "XI", "Bench", "Plan"):
            self.assertIn(f"  {label} ", text)
        self.assertIn(f"record #{self.record_id}", text)
        self.assertIn("doing nothing", text)

    def test_settle_prints_the_grade_after_the_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fpl.db"
            disk = storage.connect(path)
            self.conn.execute("UPDATE fixture SET finished = 1 WHERE event = ?", (GAMEWEEK,))
            self.w.players(44)
            for element_id in range(1, 45):
                self.conn.execute(
                    "INSERT OR IGNORE INTO player_gameweek (element_id, round, total_points,"
                    " minutes, raw) VALUES (?, ?, 1, 90, '{}')", (element_id, GAMEWEEK))
            self.conn.commit()
            self.conn.backup(disk)
            disk.close()

            class Stub:
                async def get_fixtures(self):
                    return []

                async def close(self):
                    pass

            args = SimpleNamespace(db=path, gameweek=GAMEWEEK, model_version=MODEL_VERSION,
                                   learn=False, learnings=Path(tmp) / "learnings",
                                   no_backfill=True)
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(settle, "FPLClient", Stub):
                code = asyncio.run(settle._run(args))
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("Calibration for gameweek 3", text)
        self.assertIn("Instructions graded for gameweek 3", text)
        self.assertLess(text.index("Calibration for"), text.index("Instructions graded"))


if __name__ == "__main__":
    unittest.main()

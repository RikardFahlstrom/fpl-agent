"""Per-move recording: exactly what was claimed, and nothing that was not.

On the brief's hand-built warehouse with a two-move plan stored, so "I made move 2 only"
is a real subset of something.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from fpl_agent.engine import held, instructions, recording, storage, warehouse
from fpl_agent.engine.projection import MODEL_VERSION

from test_brief import GAMEWEEK
from test_instructions import InstructionsTestCase


class RecordingTestCase(InstructionsTestCase):

    def setUp(self):
        super().setUp()
        # Two unowned midfielders worth +3.0 each, and two free transfers to spend on them.
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.execute("UPDATE projection SET expected_points = 2.0 "
                          "WHERE element_id IN (23, 27)")
        self.w.state(free_transfers=2)
        self.conn.commit()
        capture = warehouse.latest(self.conn)
        self.built = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION,
                                        held.read(self.conn, capture), capture=capture)
        self.record_id, _ = instructions.store(
            self.conn, self.built, snapshot_id=capture.id, model_version=MODEL_VERSION)
        self.conn.commit()
        self.assertEqual(len(self.built.moves), 2)

    def decisions(self, kind=None):
        sql = "SELECT * FROM decision" + (" WHERE kind = ?" if kind else "") + " ORDER BY id"
        return self.conn.execute(sql, (kind,) if kind else ()).fetchall()

    def record(self, **kwargs):
        kwargs.setdefault("moves", [])
        kwargs.setdefault("everything", False)
        kwargs.setdefault("captain", None)
        kwargs.setdefault("chip", None)
        return recording.record(self.conn, **kwargs)


class MoveTests(RecordingTestCase):

    def test_a_subset_of_the_plan_stores_exactly_those_moves(self):
        self.record(moves=[2])
        [row] = self.decisions("transfer")
        payload = json.loads(row["payload"])
        second = self.built.moves[1]
        self.assertEqual((payload["in"]["name"], payload["out"]["name"]),
                         (second.in_name, second.out_name))
        self.assertEqual((payload["move"], payload["moves_in_plan"]), (2, 2))
        self.assertEqual(payload["instructions_id"], self.record_id)
        self.assertEqual(row["status"], "made")

    def test_the_whole_plan_only_when_it_is_said_to_be_the_whole_plan(self):
        self.record(everything=True)
        names = [json.loads(r["payload"])["in"]["name"] for r in self.decisions("transfer")]
        self.assertEqual(sorted(names), sorted(m.in_name for m in self.built.moves))

    def test_naming_no_move_records_no_move(self):
        self.record(captain="P12")
        self.assertEqual(self.decisions("transfer"), [])

    def test_a_move_that_is_not_in_the_plan_is_refused_and_nothing_is_written(self):
        with self.assertRaises(recording.CannotRecord):
            self.record(moves=[3])
        self.assertEqual(self.decisions(), [])

    def test_all_and_a_subset_together_are_refused(self):
        with self.assertRaises(recording.CannotRecord):
            self.record(moves=[1], everything=True)
        self.assertEqual(self.decisions(), [])

    def test_a_bad_claim_after_a_good_one_leaves_nothing_written(self):
        with self.assertRaises(recording.CannotRecord):
            self.record(moves=[1], captain="Nobody")
        self.assertEqual(self.decisions(), [])

    def test_all_with_no_moves_in_the_plan_is_refused(self):
        self.conn.execute("DELETE FROM instructions")
        flat = instructions.Instructions(GAMEWEEK, lineup=self.built.lineup,
                                         plan=None)
        instructions.store(self.conn, flat, snapshot_id=1, model_version=MODEL_VERSION)
        with self.assertRaises(recording.CannotRecord):
            self.record(everything=True)

    def test_no_stored_instructions_means_nothing_to_record_against(self):
        self.conn.execute("DELETE FROM instructions")
        with self.assertRaises(recording.CannotRecord) as raised:
            self.record(moves=[1])
        self.assertIn("run `make now` first", str(raised.exception))


class CaptainAndChipTests(RecordingTestCase):

    def test_the_actual_captain_is_stored_apart_from_the_recommended_one(self):
        recommended = self.built.lineup.captain
        other = next(s for s in self.built.lineup.xi if s.element_id != recommended.element_id)
        self.record(captain=other.name)
        [row] = self.decisions("captain")
        payload = json.loads(row["payload"])
        self.assertEqual(payload["actual"]["name"], other.name)
        self.assertEqual(payload["recommended"]["name"], recommended.name)
        self.assertIn("a deviation", row["rationale"])

    def test_following_the_recommendation_is_not_called_a_deviation(self):
        self.record(captain=self.built.lineup.captain.name)
        [row] = self.decisions("captain")
        self.assertNotIn("deviation", row["rationale"])

    def test_a_captain_the_squad_does_not_hold_is_refused(self):
        with self.assertRaises(recording.CannotRecord):
            self.record(captain="Haaland")

    def test_a_player_the_plan_brings_in_can_be_the_captain(self):
        self.record(captain=self.built.moves[0].in_name)
        self.assertEqual(len(self.decisions("captain")), 1)

    def test_the_actual_chip_is_stored_beside_the_recommended_one(self):
        self.record(chip="bboost")
        [row] = self.decisions("chip")
        payload = json.loads(row["payload"])
        self.assertEqual((payload["actual"], payload["recommended"]),
                         ("bboost", self.built.chip))
        self.assertIn("a deviation", row["rationale"])

    def test_none_records_that_no_chip_was_played(self):
        self.record(chip="none")
        [row] = self.decisions("chip")
        self.assertIsNone(json.loads(row["payload"])["actual"])
        self.assertNotIn("deviation", row["rationale"])    # nothing recommended either


class ClaimTests(RecordingTestCase):

    def test_building_and_storing_instructions_records_no_decision(self):
        self.assertEqual(self.decisions(), [])

    def test_a_free_hit_plans_moves_are_not_transfers(self):
        plan = self.built.plan
        fh = instructions.Instructions(
            GAMEWEEK, lineup=self.built.lineup,
            plan=type(plan)(plan.free_transfers, plan.hit_price, None, plan.bar, 1,
                            "freehit", plan.options, plan.chosen, plan.single))
        self.conn.execute("DELETE FROM instructions")
        instructions.store(self.conn, fh, snapshot_id=1, model_version=MODEL_VERSION)
        with self.assertRaises(recording.CannotRecord) as raised:
            self.record(moves=[1])
        self.assertIn("free hit", str(raised.exception))


class CommandTests(RecordingTestCase):

    def setUp(self):
        super().setUp()
        self.db = Path(self.tmp.name) / "fpl.db"
        disk = storage.connect(self.db)
        self.conn.backup(disk)
        disk.close()
        self.log = Path(self.tmp.name) / "actions.jsonl"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = recording.main(["--db", str(self.db), "--actions-log", str(self.log),
                                   *argv])
        return code, out.getvalue(), err.getvalue()

    def count(self):
        conn = storage.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM decision").fetchone()[0]
        finally:
            conn.close()

    def test_a_bare_record_records_nothing_and_says_how(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, recording.EXIT_NOTHING_ASKED)
        self.assertIn("--move N", err)
        self.assertIn("--all", err)
        self.assertEqual(self.count(), 0)
        self.assertFalse(self.log.exists())

    def test_the_command_records_what_it_is_told_and_exports_the_log(self):
        code, out, _ = self.run_cli("--move", "1", "--chip", "none")
        self.assertEqual(code, 0)
        self.assertEqual(self.count(), 2)
        self.assertIn("move 1 of 2", out)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)

    def test_the_action_log_only_ever_grows_at_the_end(self):
        self.run_cli("--move", "1")
        before = self.log.read_text().splitlines()
        self.run_cli("--move", "2", "--chip", "none")
        after = self.log.read_text().splitlines()
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(len(after), len(before) + 2)

    def test_a_refused_claim_exits_one_and_writes_nothing(self):
        code, _, err = self.run_cli("--move", "9")
        self.assertEqual(code, recording.EXIT_CANNOT)
        self.assertIn("nothing recorded", err)
        self.assertEqual(self.count(), 0)

    def test_the_command_is_registered_and_recommend_no_longer_records(self):
        from fpl_agent import cli
        self.assertEqual(cli.COMMANDS["record"][0], "fpl_agent.engine.recording")
        from fpl_agent.engine import recommend
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                recommend.main(["--record"])


if __name__ == "__main__":
    unittest.main()

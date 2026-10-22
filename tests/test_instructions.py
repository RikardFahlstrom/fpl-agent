"""The lineup slice of Instructions: XI, captain, vice-captain, bench order.

Offline, on the brief's hand-built warehouse. The XI is checked against an independent
brute force over every legal eleven, not against the code that picked it.
"""

import itertools
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fpl_agent.engine import brief, held, instructions, storage, warehouse
from fpl_agent.engine.projection import MODEL_VERSION

from test_brief import GAMEWEEK, NOW, Warehouse

#: Two keepers, five defenders, five midfielders, three forwards (types cycle 1-4 by id).
SHAPED = [1, 5, 2, 6, 10, 14, 18, 3, 7, 11, 15, 19, 4, 8, 12]
LEGAL = {(3, 4, 3), (3, 5, 2), (4, 3, 3), (4, 4, 2), (4, 5, 1), (5, 2, 3), (5, 3, 2),
         (5, 4, 1)}


class InstructionsTestCase(unittest.TestCase):

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {
            "FPL_TOKEN_CACHE": str(Path(self.tmp.name) / "token.json"),
            "FPL_RIVAL_LEAGUES": ""})
        patch.start()
        self.addCleanup(patch.stop)
        self.w = Warehouse(self.conn).healthy()
        self.conn.execute("DELETE FROM my_squad")
        self.w.squad(SHAPED)
        # Everyone distinct: the element id over ten, so a higher id projects higher.
        self.conn.execute("UPDATE projection SET expected_points = element_id / 10.0")
        self.conn.commit()

    def holding(self) -> held.HeldSquad:
        return held.read(self.conn, warehouse.latest(self.conn))

    def build(self, **kwargs) -> instructions.Instructions:
        return instructions.build(self.conn, GAMEWEEK, MODEL_VERSION, self.holding(), **kwargs)

    def lineup(self) -> instructions.Lineup:
        found = self.build().lineup
        self.assertIsNotNone(found)
        return found


class XITests(InstructionsTestCase):

    def test_the_xi_is_the_best_legal_eleven_by_brute_force(self):
        xp = {e: e / 10.0 for e in SHAPED}
        kind = {e: 1 + (e - 1) % 4 for e in SHAPED}
        best = 0.0
        for eleven in itertools.combinations(SHAPED, 11):
            shape = tuple(sum(1 for e in eleven if kind[e] == t) for t in (2, 3, 4))
            if sum(1 for e in eleven if kind[e] == 1) == 1 and shape in LEGAL:
                best = max(best, sum(xp[e] for e in eleven))
        xi = self.lineup().xi
        self.assertEqual(len(xi), 11)
        self.assertAlmostEqual(sum(s.xp for s in xi), best)

    def test_the_formation_is_legal_and_named(self):
        found = self.lineup()
        shape = tuple(sum(1 for s in found.xi if s.element_type == t) for t in (2, 3, 4))
        self.assertIn(shape, LEGAL)
        self.assertEqual(found.formation, "-".join(map(str, shape)))
        self.assertEqual(sum(1 for s in found.xi if s.element_type == 1), 1)

    def test_the_best_keeper_starts_and_the_other_is_first_on_the_bench(self):
        found = self.lineup()
        self.assertEqual([s.name for s in found.xi if s.element_type == 1], ["P5"])
        self.assertEqual(found.bench[0].name, "P1")

    def test_a_strong_bench_player_displaces_a_weaker_starter(self):
        # P14 starts the captured XI (position 6) but P18 projects higher.
        self.assertIn("P18", [s.name for s in self.lineup().xi])


class ArmbandTests(InstructionsTestCase):

    def test_captain_and_vice_are_the_two_best_starters(self):
        found = self.lineup()
        ranked = sorted(found.xi, key=lambda s: -s.xp)
        self.assertEqual(found.captain.name, ranked[0].name)
        self.assertEqual(found.vice.name, ranked[1].name)
        self.assertNotEqual(found.captain.element_id, found.vice.element_id)

    def test_the_captain_carries_the_existing_why_line(self):
        found = self.lineup()
        self.assertIn(found.captain.name, found.captain_line)
        self.assertIn("xP", found.captain_line)

    def test_the_vice_has_a_plain_english_reason(self):
        reason = self.lineup().vice_reason
        self.assertIn("takes the armband if", reason)
        self.assertIn(self.lineup().captain.name, reason)

    def test_an_armband_on_a_benched_player_is_said_not_hidden(self):
        # The captured armband sits on P1, the keeper the lineup leaves out.
        self.conn.execute("UPDATE my_squad SET multiplier = 2 WHERE element_id = 1")
        self.conn.commit()
        self.assertIn("armband is on P1, who is not in this XI", self.lineup().captain_line)


class BenchTests(InstructionsTestCase):

    def test_the_bench_is_the_keeper_then_outfield_best_first(self):
        bench = self.lineup().bench
        self.assertEqual(len(bench), 4)
        self.assertEqual(bench[0].element_type, 1)
        outfield = [s.xp for s in bench[1:]]
        self.assertEqual(outfield, sorted(outfield, reverse=True))

    def test_every_bench_slot_has_a_reason(self):
        found = self.lineup()
        self.assertEqual(len(found.bench_reasons), 4)
        self.assertTrue(all(found.bench_reasons))
        self.assertIn("goalkeeper", found.bench_reasons[0])
        self.assertIn("first to come on", found.bench_reasons[1])


class NoLineupTests(InstructionsTestCase):

    def test_a_player_with_no_projection_means_no_lineup_not_a_zero(self):
        self.conn.execute("DELETE FROM projection WHERE element_id = 12")
        self.conn.commit()
        found = self.build()
        self.assertIsNone(found.lineup)
        self.assertIn("no projection for 1 of your 15 players (P12)", found.note)

    def test_no_squad_says_so(self):
        found = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION, None)
        self.assertEqual(found.note, "not evaluated - no squad captured")

    def test_a_locked_gameweek_is_not_read(self):
        found = self.build(locked="locked")
        self.assertIsNone(found.lineup)
        self.assertIn("gameweek 3 is locked", found.note)


class StoreTests(InstructionsTestCase):
    """One record per run, appended, tagged and never changed."""

    def store(self, **kwargs):
        kwargs.setdefault("snapshot_id", warehouse.latest(self.conn).id)
        kwargs.setdefault("model_version", MODEL_VERSION)
        return instructions.store(self.conn, self.build(), **kwargs)

    def test_a_run_adds_a_record_and_a_second_run_adds_another(self):
        first, _ = self.store()
        second, _ = self.store()
        self.assertNotEqual(first, second)
        found = instructions.records(self.conn, GAMEWEEK, MODEL_VERSION)
        self.assertEqual([r.id for r in found], [first, second])

    def test_the_record_round_trips_and_has_room_for_moves_and_chip(self):
        self.store()
        [record] = instructions.records(self.conn, GAMEWEEK, MODEL_VERSION)
        self.assertEqual(record.instructions, self.build())
        payload = self.build().to_payload()
        self.assertEqual(payload["moves"], [])
        self.assertIsNone(payload["chip"])
        self.assertEqual(record.snapshot_id, warehouse.latest(self.conn).id)

    def test_the_last_record_before_the_deadline_is_identifiable(self):
        # The deadline is 07:30 UTC on 5 September.
        self.store(now=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc))
        before, _ = self.store(now=datetime(2026, 9, 5, 7, 0, tzinfo=timezone.utc))
        self.store(now=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc))   # after the lock
        found = instructions.last_before_deadline(self.conn, GAMEWEEK, MODEL_VERSION)
        self.assertEqual(found.id, before)

    def test_no_record_means_none_not_a_made_up_one(self):
        self.assertIsNone(instructions.last_before_deadline(self.conn, GAMEWEEK, MODEL_VERSION))

    def test_a_finished_gameweek_gets_no_new_record_and_keeps_its_old_ones(self):
        before, _ = self.store()
        self.conn.execute("UPDATE fixture SET finished = 1 WHERE event = ?", (GAMEWEEK,))
        self.conn.commit()
        again, why = self.store()
        self.assertIsNone(again)
        self.assertIn("has finished", why)
        self.assertEqual([r.id for r in instructions.records(
            self.conn, GAMEWEEK, MODEL_VERSION)], [before])

    def test_two_model_versions_sit_side_by_side(self):
        self.store()
        self.store(model_version="0.0.0-other")
        self.assertEqual(len(instructions.records(self.conn, GAMEWEEK, MODEL_VERSION)), 1)
        self.assertEqual(len(instructions.records(self.conn, GAMEWEEK, "0.0.0-other")), 1)

    def test_nothing_is_stored_when_there_is_no_lineup(self):
        record, why = instructions.store(
            self.conn, instructions.Instructions(GAMEWEEK, note="not evaluated - x"),
            snapshot_id=1, model_version=MODEL_VERSION)
        self.assertIsNone(record)
        self.assertIn("nothing to record", why)

    def test_the_command_records_each_run_and_a_dry_run_records_nothing(self):
        import io
        from contextlib import redirect_stdout
        disk = Path(self.tmp.name) / "fpl.db"
        copy = storage.connect(disk)
        self.conn.backup(copy)
        copy.close()

        real = brief.evaluate

        def at_now(conn, gameweek, **kwargs):
            return real(conn, gameweek, now=NOW,
                        learnings_dir=Path(self.tmp.name) / "learnings")

        def run(*extra):
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(brief, "evaluate", at_now):
                brief.main(["--db", str(disk), "--logs", str(Path(self.tmp.name) / "logs"),
                            *extra])
            return out.getvalue()

        def count():
            conn = storage.connect(disk)
            try:
                return conn.execute("SELECT COUNT(*) FROM instructions").fetchone()[0]
            finally:
                conn.close()

        run("--dry-run")
        self.assertEqual(count(), 0)
        self.assertIn("recorded Instructions #1", run())
        run()
        self.assertEqual(count(), 2)


class ChipChoiceTests(InstructionsTestCase):
    """Chips as whole alternatives: no chip, each chip with its own best moves."""

    def setUp(self):
        super().setUp()
        import json
        # The set's wall is GW5, every chip open and unplayed.
        chips_json = json.dumps([
            {"name": name, "chip_type": kind, "status_for_entry": "available",
             "start_event": 1, "stop_event": GAMEWEEK + 2, "played_by_entry": []}
            for name, kind in (("bboost", "team"), ("3xc", "team"),
                               ("freehit", "transfer"), ("wildcard", "transfer"))])
        self.w.state(chips=chips_json)
        # Flat: everyone 1.0 a week, so nothing is worth a move or a chip until a test says.
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.commit()

    def flat_but(self, element_ids, xp, weeks=None):
        for gw in weeks or (GAMEWEEK, GAMEWEEK + 1, GAMEWEEK + 2):
            self.conn.execute(
                "UPDATE projection SET expected_points = ? WHERE gameweek = ? "
                "AND element_id IN (%s)" % ",".join("?" * len(element_ids)),
                (xp, gw, *element_ids))
        self.conn.commit()

    def build(self):
        capture = warehouse.latest(self.conn)
        return instructions.build(self.conn, GAMEWEEK, MODEL_VERSION, self.holding(),
                                  capture=capture)

    def test_a_flat_week_holds_and_names_the_best_alternative_with_its_margin(self):
        built = self.build()
        self.assertIsNone(built.chip)
        self.assertTrue(built.chip_note.startswith("hold - the best alternative"))
        self.assertIn("is not played", built.chip_note)
        self.assertEqual([a.name for a in built.alternatives],
                         ["no chip", "bench boost", "triple captain", "free hit",
                          "wildcard"])

    def test_every_alternative_is_scored_against_doing_nothing(self):
        built = self.build()
        none = built.alternatives[0]
        self.assertEqual((none.name, none.total), ("no chip", 0.0))
        text = instructions.chip_text(built)
        for alternative in built.alternatives:
            self.assertIn(f"{alternative.name} {alternative.total:+.1f}", text)

    def test_a_strong_bench_plays_the_bench_boost(self):
        # Everyone projects 4.5 this week: the bench alone is worth 18 against a 15 bar.
        self.flat_but([e for e in SHAPED], 4.5, weeks=(GAMEWEEK,))
        built = self.build()
        self.assertEqual(built.chip, "bboost")
        self.assertTrue(built.chip_note.startswith("play bench boost"))
        self.assertIn("ahead of the best alternative", built.chip_note)

    def test_a_big_enough_gap_in_the_market_plays_the_wildcard_with_its_rebuild_moves(self):
        unowned = [23, 27, 31, 22, 26, 30, 20, 24]
        self.flat_but(unowned, 6.0)
        built = self.build()
        self.assertEqual(built.chip, "wildcard")
        self.assertEqual(built.plan.chip, "wildcard")
        self.assertGreater(len(built.moves), 4)         # past what a capped plan would make
        self.assertEqual(built.alternatives[0].plan.hit_cap, 2)

    def test_the_moves_shown_are_the_chosen_alternatives_moves(self):
        self.flat_but([23, 27, 31, 22, 26, 30, 20, 24], 6.0)
        built = self.build()
        chosen = next(a for a in built.alternatives if a.chip == built.chip)
        self.assertEqual(built.plan, chosen.plan)
        self.assertEqual(built.moves, chosen.plan.moves)

    def test_a_chip_that_is_not_evaluable_is_reported_as_it_is_and_never_valued(self):
        import json
        self.w.state(chips=json.dumps([
            {"name": "wildcard", "chip_type": "transfer", "status_for_entry": "played",
             "start_event": 1, "stop_event": 19, "played_by_entry": [GAMEWEEK - 1]},
            {"name": "bboost", "chip_type": "team", "status_for_entry": "available",
             "start_event": 1, "stop_event": GAMEWEEK + 2, "played_by_entry": []}]))
        self.conn.commit()
        built = self.build()
        self.assertEqual([a.name for a in built.alternatives], ["no chip", "bench boost"])
        wildcard = next(v for v in built.verdicts if v.state.name == "wildcard")
        self.assertFalse(wildcard.evaluated)
        self.assertIn("played", wildcard.reason)

    def test_a_chip_whose_moves_cannot_be_planned_is_not_played(self):
        self.flat_but([e for e in SHAPED], 4.5, weeks=(GAMEWEEK,))
        from fpl_agent.engine import moves as movesmod
        real = movesmod.plan

        def failing(conn, holding, capture, **kwargs):
            if kwargs.get("bench_boost"):
                raise movesmod.NotProven("time limit")
            return real(conn, holding, capture, **kwargs)

        with mock.patch.object(movesmod, "plan", failing):
            built = self.build()
        self.assertIsNone(built.chip)
        bench = next(a for a in built.alternatives if a.chip == "bboost")
        self.assertFalse(bench.playable)
        self.assertIn("could not be planned", bench.reason)

    def test_with_no_chips_captured_it_says_so_not_hold(self):
        self.w.state(chips="[]")
        self.conn.commit()
        self.assertIn("no chips captured", self.build().chip_note)

    def test_the_decision_is_stored_with_the_alternatives(self):
        self.flat_but([e for e in SHAPED], 4.5, weeks=(GAMEWEEK,))
        built = self.build()
        again = instructions.Instructions.from_payload(built.to_payload())
        self.assertEqual(again.chip, "bboost")
        self.assertEqual(again.alternatives, built.alternatives)
        self.assertEqual(again, built)


class OneValueTests(InstructionsTestCase):
    """The terminal and the brief render the same rows from the same value."""

    def evaluate(self, **kwargs):
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("learnings_dir", Path(self.tmp.name) / "learnings")
        return brief.evaluate(self.conn, GAMEWEEK, **kwargs)

    def test_the_brief_block_opens_with_the_instructions_rows(self):
        evaluation = self.evaluate()
        block = brief.render_block(evaluation, markdown=False)
        rows = instructions.rows(evaluation.instructions)
        self.assertEqual([line.split(":")[0] for line in block[:7]],
                         [label for label, _ in rows])
        self.assertEqual([label for label, _ in rows],
                         ["Instructions", "Chip", "Moves", "Starting XI", "Captain",
                          "Vice-captain", "Bench"])

    def test_the_terminal_text_carries_every_row_the_brief_does(self):
        evaluation = self.evaluate()
        terminal = instructions.render(evaluation.instructions)
        page = brief.render_brief(evaluation)
        for label, text in instructions.rows(evaluation.instructions):
            self.assertIn(label + ":", terminal)
            self.assertIn(text, terminal)
            self.assertIn(f"- **{label}:** {text}", page.replace("GW3 (deadline 5 Sep 09:30)", "GW3"))

    def test_the_same_lines_open_the_block_whether_or_not_there_is_a_lineup(self):
        with_lineup = [l.split(":")[0] for l in brief.render_block(self.evaluate())[:7]]
        self.conn.execute("DELETE FROM projection WHERE element_id = 12")
        self.conn.commit()
        without = [l.split(":")[0] for l in brief.render_block(self.evaluate())[:7]]
        self.assertEqual(with_lineup, without)

    def test_a_lineup_that_cannot_be_built_still_names_every_row(self):
        text = instructions.render(instructions.Instructions(3, note="not evaluated - x"))
        for label in ("Instructions", "Chip", "Moves", "Starting XI", "Captain", "Vice-captain", "Bench"):
            self.assertIn(label + ":", text)

    def test_the_command_prints_the_instructions_before_writing_the_brief(self):
        import io
        from contextlib import redirect_stdout
        disk = Path(self.tmp.name) / "fpl.db"
        copy = storage.connect(disk)
        self.conn.backup(copy)
        copy.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = brief.main(["--db", str(disk), "--logs", str(Path(self.tmp.name) / "logs")])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertLess(text.index("Starting XI:"), text.index("wrote "))


if __name__ == "__main__":
    unittest.main()

"""Chips: what FPL says you hold, the window they define, and the captain pick.

Tested through the seeded warehouse the brief tests use, plus the pure functions over
values (`chip_states`, `window_end`, `captain_line`) with hand-built inputs.
"""

import json
import unittest

from fpl_agent.engine import chips, projection, storage
from fpl_agent.engine.projection import MODEL_VERSION

from test_brief import GAMEWEEK, Warehouse
from test_projection import SeedMixin

# A real payload shape: two sets, first set to GW19, wildcard played in GW3.
FIRST_SET = [
    {"chip_type": "team", "name": "bboost", "played_by_entry": [], "start_event": 1,
     "status_for_entry": "available", "stop_event": 19},
    {"chip_type": "team", "name": "3xc", "played_by_entry": [], "start_event": 1,
     "status_for_entry": "available", "stop_event": 19},
    {"chip_type": "transfer", "name": "wildcard", "played_by_entry": [3],
     "start_event": 2, "status_for_entry": "played", "stop_event": 19},
    {"chip_type": "transfer", "name": "freehit", "played_by_entry": [],
     "start_event": 2, "status_for_entry": "available", "stop_event": 19},
]
SECOND_SET = [dict(c, played_by_entry=[], start_event=20, stop_event=38,
                   status_for_entry="available") for c in FIRST_SET]


class ChipStateTests(unittest.TestCase):

    def test_the_payload_is_read_as_states_with_their_windows(self):
        states = {s.name: s for s in chips.chip_states(json.dumps(FIRST_SET))}
        self.assertEqual(set(states), {"bboost", "3xc", "wildcard", "freehit"})
        self.assertEqual(states["wildcard"].status, "played")
        self.assertEqual(states["wildcard"].played_in, 3)
        self.assertEqual((states["bboost"].start_event, states["bboost"].stop_event), (1, 19))

    def test_only_an_available_chip_inside_its_window_is_evaluable(self):
        first = {s.name: s for s in chips.chip_states(json.dumps(FIRST_SET))}
        self.assertTrue(first["bboost"].evaluable(5))
        self.assertFalse(first["wildcard"].evaluable(5))           # played
        self.assertFalse(first["bboost"].evaluable(20))            # expired
        second = chips.chip_states(json.dumps(SECOND_SET))[0]
        self.assertFalse(second.evaluable(5))                      # not open yet
        self.assertTrue(second.evaluable(20))

    def test_states_are_described_in_the_readers_words(self):
        first = {s.name: s for s in chips.chip_states(json.dumps(FIRST_SET))}
        self.assertEqual(first["wildcard"].describe(5), "played in GW3")
        self.assertEqual(first["bboost"].describe(5), "available")
        self.assertEqual(first["bboost"].describe(20), "expired")
        second = chips.chip_states(json.dumps(SECOND_SET))[0]
        self.assertEqual(second.describe(5), "not until GW20")

    def test_the_window_end_is_the_wall_of_the_set_in_play(self):
        both = chips.chip_states(json.dumps(FIRST_SET + SECOND_SET))
        self.assertEqual(chips.window_end(both, 5), 19)
        self.assertEqual(chips.window_end(both, 20), 38)
        self.assertEqual(chips.window_end(both, 19), 19)
        # a set with every chip played still defines the range
        played = [chips.ChipState("bboost", "played", 1, 19, 4)]
        self.assertEqual(chips.window_end(played, 5), 19)

    def test_garbage_and_nothing_read_as_no_chips(self):
        self.assertEqual(chips.chip_states(None), [])
        self.assertEqual(chips.chip_states("not json"), [])
        self.assertEqual(chips.chip_states('{"name": "bboost"}'), [])
        self.assertIsNone(chips.window_end([], 5))


class ChipWindowProjectionTests(SeedMixin, unittest.TestCase):
    """`project --chips` fills every week to the wall, once, and only with a squad."""

    def _with_chips(self, conn, stop=8):
        payload = [dict(c, stop_event=stop) for c in FIRST_SET]
        storage.record_my_team(conn, 1, 1, {
            "picks": [{"element": 1, "position": 1, "multiplier": 2, "is_captain": True,
                       "is_vice_captain": False, "selling_price": 90,
                       "purchase_price": 90}],
            "transfers": {"bank": 0, "value": 90, "limit": 1, "made": 0, "cost": 4},
            "chips": payload})
        storage.upsert_fixtures(conn, [{
            "id": 10 + gw, "event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
            "team_a_difficulty": 3, "team_h_score": None, "team_a_score": None,
            "kickoff_time": None, "finished": False} for gw in range(4, 39)])
        conn.commit()

    def test_it_projects_from_the_target_to_the_walls_gameweek(self):
        conn = self._seed()
        self.addCleanup(conn.close)
        self._with_chips(conn, stop=8)
        self.assertEqual(projection.project_chip_window(conn), (3, 8))
        weeks = [r[0] for r in conn.execute(
            "SELECT DISTINCT gameweek FROM projection ORDER BY gameweek")]
        self.assertEqual(weeks, [3, 4, 5, 6, 7, 8])

    def test_weeks_the_horizon_already_wrote_are_not_redone(self):
        conn = self._seed()
        self.addCleanup(conn.close)
        self._with_chips(conn, stop=6)
        projection.project_horizon(conn, 3, weeks=3)
        before = {r[0]: r[1] for r in conn.execute(
            "SELECT gameweek, id FROM projection WHERE element_id = 1")}
        projection.project_chip_window(conn)
        after = {r[0]: r[1] for r in conn.execute(
            "SELECT gameweek, id FROM projection WHERE element_id = 1")}
        self.assertEqual({gw: after[gw] for gw in before}, before)    # same row ids
        self.assertEqual(sorted(after), [3, 4, 5, 6])

    def test_no_squad_means_no_chips_and_nothing_projected(self):
        conn = self._seed()
        self.addCleanup(conn.close)
        self.assertIsNone(projection.project_chip_window(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM projection").fetchone()[0], 0)

    def test_the_wall_is_capped_by_the_last_fixture_recorded(self):
        conn = self._seed()
        self.addCleanup(conn.close)
        self._with_chips(conn, stop=38)
        conn.execute("DELETE FROM fixture WHERE event > 6")
        conn.commit()
        self.assertEqual(projection.project_chip_window(conn), (3, 6))


class CaptainTests(unittest.TestCase):

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.warehouse = Warehouse(self.conn).healthy(better=(4, 7))
        self.conn.commit()

    def picks(self):
        return chips.captain_picks(self.conn, self.warehouse.snapshot_id, GAMEWEEK,
                                   MODEL_VERSION)

    def test_the_xi_is_ranked_by_the_gameweeks_projection(self):
        picks = self.picks()
        self.assertEqual([p.element_id for p in picks[:2]], [4, 7])
        self.assertEqual(len(picks), 11)                    # the bench is not a captain
        self.assertEqual(picks[0].xp, 2.0)

    def test_the_line_names_the_pick_the_runner_up_and_where_the_armband_is(self):
        self.conn.execute("UPDATE my_squad SET multiplier = 2 WHERE element_id = 1")
        self.conn.commit()
        line = chips.captain_line(self.picks())
        self.assertIn("P4 (C04), 2.0 xP; next P7 2.0", line)
        self.assertIn("armband is on P1 (1.0)", line)

    def test_the_line_says_when_the_armband_is_already_right(self):
        self.conn.execute("UPDATE my_squad SET multiplier = 2 WHERE element_id = 4")
        self.conn.commit()
        self.assertIn("armband already on him", chips.captain_line(self.picks()))

    def test_no_projection_is_not_evaluated_rather_than_a_guess(self):
        self.conn.execute("DELETE FROM projection")
        self.conn.commit()
        self.assertEqual(self.picks(), [])
        self.assertIn("not evaluated", chips.captain_line([]))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------
# Values and verdicts
# --------------------------------------------------------------------------

def value(gw, v, note="x"):
    return chips.ChipValue(gw, v, note)


BB = chips.ChipState("bboost", "available", 1, 19, None)
TC = chips.ChipState("3xc", "available", 1, 19, None)


class DecideTests(unittest.TestCase):
    """Play now needs both gates: the bar, and best-of-window within the band."""

    def test_under_the_bar_is_hold_even_when_this_week_is_the_best(self):
        v = chips.decide(BB, [value(5, 11.7), value(6, 9.8), value(7, 8.9)], 5)
        self.assertFalse(v.play_now)
        self.assertIn("under the 15 bar", v.reason)
        self.assertIn("no week to GW7 clears it either", v.reason)
        self.assertEqual(v.short, "hold (+11.7, bar 15)")

    def test_a_later_week_over_the_bar_is_named_as_the_hold_target(self):
        v = chips.decide(BB, [value(5, 11.7), value(6, 9.8), value(9, 16.2, "double")], 5)
        self.assertFalse(v.play_now)
        self.assertIn("GW9 clears it (+16.2, double)", v.reason)
        self.assertEqual(v.short, "hold for GW9 (+16.2 vs +11.7 now)")

    def test_over_the_bar_and_the_best_week_is_play_now(self):
        v = chips.decide(BB, [value(5, 16.0, "bench"), value(6, 9.8), value(7, 8.9)], 5)
        self.assertTrue(v.play_now)
        self.assertIn("play now: +16.0 (bench) clears the 15 bar, the best of the 3 weeks",
                      v.reason)
        self.assertEqual(v.short, "PLAY NOW (+16.0, bar 15)")

    def test_over_the_bar_and_within_the_band_of_the_best_is_play_now(self):
        # 16.0 is within 15% of 18.0: ties go to now, because the set has a wall.
        v = chips.decide(BB, [value(5, 16.0), value(6, 18.0), value(7, 8.9)], 5)
        self.assertTrue(v.play_now)
        self.assertIn("within 15% of GW6 (+18.0)", v.reason)

    def test_over_the_bar_but_a_clearly_better_week_is_hold(self):
        v = chips.decide(BB, [value(5, 16.0), value(6, 22.0, "double"), value(7, 8.9)], 5)
        self.assertFalse(v.play_now)
        self.assertIn("hold for GW6: +22.0 (double) against +16.0 now", v.reason)

    def test_no_projection_for_this_week_is_not_evaluated(self):
        v = chips.decide(BB, [value(6, 9.8)], 5)
        self.assertFalse(v.evaluated)
        self.assertIn("not evaluated", v.reason)

    def test_the_bar_is_per_chip(self):
        self.assertTrue(chips.decide(TC, [value(5, 9.5, "Haaland, 2 fixtures")], 5).play_now)
        self.assertFalse(chips.decide(BB, [value(5, 9.5)], 5).play_now)


class ValueTests(unittest.TestCase):

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.warehouse = Warehouse(self.conn).healthy()
        self.conn.commit()
        self.squad = [dict(r) for r in self.conn.execute(
            "SELECT element_id, position, multiplier FROM my_squad")]

    def project(self, gameweek, element_id, xp, fixtures=1):
        self.conn.execute(
            """INSERT OR REPLACE INTO projection
               (snapshot_id, gameweek, element_id, model_version, expected_points,
                p_start, expected_minutes, fixture_count, components, created_at)
               VALUES (?,?,?,?,?,0.9,80,?,'{}','now')""",
            (self.warehouse.snapshot_id, gameweek, element_id, MODEL_VERSION, xp, fixtures))
        self.conn.commit()

    def by_week(self, first=GAMEWEEK, last=GAMEWEEK + 2):
        return chips.squad_projections(self.conn, self.warehouse.snapshot_id,
                                       MODEL_VERSION, [p["element_id"] for p in self.squad],
                                       first, last)

    def test_bench_boost_is_the_bench_summed_week_by_week(self):
        for e in (12, 13, 14, 15):
            self.project(GAMEWEEK + 1, e, 4.0)
        values = chips.bench_boost_values(self.by_week(), self.squad)
        self.assertEqual([(v.gameweek, v.value) for v in values],
                         [(3, 4.0), (4, 16.0), (5, 4.0)])
        self.assertEqual(values[0].note, "P12, P13, P14, P15")

    def test_triple_captain_is_the_best_xi_player_with_his_fixtures(self):
        self.project(GAMEWEEK + 1, 7, 9.6, fixtures=2)
        self.project(GAMEWEEK + 1, 14, 20.0)        # bench: never the captain
        values = chips.triple_captain_values(self.by_week(), self.squad)
        week = next(v for v in values if v.gameweek == GAMEWEEK + 1)
        self.assertEqual((week.value, week.note), (9.6, "P7, 2 fixtures"))

    def test_the_post_move_squad_puts_the_incoming_player_in_the_outgoing_slot(self):
        move = {"out": {"element_id": 1}, "in": {"element_id": 17}}
        after = chips.apply_move(self.squad, move)
        self.assertEqual(after[0]["element_id"], 17)
        self.assertEqual(after[0]["position"], 1)
        self.assertEqual(chips.apply_move(self.squad, None), self.squad)
        stranger = {"out": {"element_id": 99}, "in": {"element_id": 17}}
        self.assertEqual(chips.apply_move(self.squad, stranger), self.squad)

    def test_evaluate_reports_every_chip_in_fpls_order(self):
        self.warehouse.state(chips=json.dumps(FIRST_SET))
        self.conn.commit()
        verdicts = chips.evaluate(self.conn, self.warehouse.snapshot_id, GAMEWEEK,
                                  MODEL_VERSION, self.squad)
        self.assertEqual([v.state.name for v in verdicts],
                         ["bboost", "3xc", "wildcard", "freehit"])
        self.assertTrue(verdicts[0].evaluated)
        self.assertEqual(verdicts[2].reason, "played in GW3")
        self.assertIn("not FPL-shaped", verdicts[3].reason)      # 4 GKP in this fixture
        line = chips.chips_line(verdicts, GAMEWEEK)
        self.assertIn("bench boost hold (+4.0, bar 15)", line)
        self.assertIn("wildcard played in GW3 · free hit not evaluated", line)
        self.assertIn("set expires after GW19 (17 weeks left)", line)

    def test_no_chips_captured_is_said_not_guessed(self):
        self.assertEqual(chips.evaluate(self.conn, self.warehouse.snapshot_id, GAMEWEEK,
                                        MODEL_VERSION, self.squad), [])
        self.assertIn("no chips captured", chips.chips_line([], GAMEWEEK))


# --------------------------------------------------------------------------
# Through the brief: the trigger, the line, the section, the record
# --------------------------------------------------------------------------

from datetime import datetime, timezone
from pathlib import Path
import tempfile

from fpl_agent.engine import brief, recommend
from test_brief import NOW


class ChipTriggerTests(unittest.TestCase):

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.warehouse = Warehouse(self.conn).healthy()
        self.warehouse.state(chips=json.dumps(FIRST_SET))
        self.conn.commit()

    def bench(self, xp, gameweek=GAMEWEEK):
        for e in (12, 13, 14, 15):
            self.conn.execute(
                "UPDATE projection SET expected_points = ? WHERE element_id = ? "
                "AND gameweek = ?", (xp, e, gameweek))
        self.conn.commit()

    def evaluate(self):
        return brief.evaluate(self.conn, GAMEWEEK, now=NOW, include_token=False)

    def render(self, evaluation=None):
        return brief.render_brief(self.conn, GAMEWEEK, now=NOW, evaluation=evaluation,
                                  include_token=False, notifications_configured=False,
                                  learnings_dir=Path(self.tmp.name))

    def test_a_bench_over_the_bar_fires_with_the_one_action(self):
        self.bench(4.5)                         # 18 > 15, and the best of the horizon
        evaluation = self.evaluate()
        [trigger] = [t for t in evaluation.triggers if t.name == "chip_worth_playing"]
        self.assertEqual(trigger.headline,
                         "Play your bench boost this gameweek: +18.0 (P12, P13, P14, P15)")
        self.assertIn("clears the 15 bar", trigger.detail)
        self.assertIn("Best other week: GW4 (+4.0", trigger.detail)
        self.assertIn("Deadline Sat 05 Sep 07:30 UTC", trigger.detail)
        self.assertIn("Play the bench boost before Sat 05 Sep 07:30 UTC, or record why not "
                      "with `fpl-agent recommend --record --chip bboost`", trigger.action)
        self.assertEqual(trigger.fingerprint, "chip_worth_playing:gw3:bboost")

    def test_the_fingerprint_is_the_chip_and_the_week_not_the_value(self):
        self.bench(4.5)
        first = self.evaluate().triggers[-1].fingerprint
        self.bench(4.8)
        self.assertEqual(self.evaluate().triggers[-1].fingerprint, first)

    def test_a_bench_under_the_bar_is_silent_and_says_the_numbers(self):
        evaluation = self.evaluate()
        self.assertNotIn("chip_worth_playing", [t.name for t in evaluation.triggers])
        self.assertIn("bench boost hold: +4.0 now", evaluation.silent["chip_worth_playing"])
        self.assertIn("under the 15 bar", evaluation.silent["chip_worth_playing"])

    def test_the_block_and_the_section_carry_the_verdicts(self):
        self.bench(4.5)
        text = self.render()
        self.assertIn("- **Chips:** bench boost PLAY NOW (+18.0, bar 15) · triple captain "
                      "hold (+2.0, bar 9) · wildcard played in GW3 · free hit not "
                      "evaluated; set expires after GW19 (17 weeks left)", text)
        self.assertIn("## Chips", text)
        self.assertIn("| GW3 * | +18.0 (P12, P13, P14, P15) | +2.0 (P17) |", text)
        self.assertIn("| GW4 | +4.0 | +2.0 |", text)         # names only when they change
        self.assertIn("The set expires after GW19; weeks GW6-GW19 are not projected yet "
                      "(`project --chips`)", text)
        self.assertIn("Only the next gameweek has predicted lineups", text)
        self.assertIn("fired, not delivered: Deadline near with a free transfer unused, A move worth making, A chip worth playing", text)

    def test_values_are_summed_over_the_post_move_squad(self):
        # The recommended move brings P17 (2.0) in for P1; P1 is in the XI, so the
        # captain pick for triple captain moves with it.
        evaluation = self.evaluate()
        tc = next(v for v in evaluation.chips if v.state.name == "3xc")
        self.assertEqual(tc.now.note, "P17")

    def test_a_passed_deadline_never_fires_a_chip(self):
        self.bench(4.5)
        late = brief.evaluate(self.conn, GAMEWEEK,
                              now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
                              include_token=False)
        self.assertNotIn("chip_worth_playing", [t.name for t in late.triggers])
        self.assertIn("deadline has passed", late.silent["chip_worth_playing"])

    def test_recording_a_chip_writes_a_chip_decision_with_the_values(self):
        self.bench(4.5)
        evaluation = self.evaluate()
        verdict = next(v for v in evaluation.chips if v.state.name == "bboost")
        recommend.record_chip(self.conn, verdict)
        row = self.conn.execute("SELECT * FROM decision").fetchone()
        self.assertEqual(row["kind"], "chip")
        self.assertEqual(row["gameweek"], GAMEWEEK)
        self.assertEqual(row["status"], "made")
        self.assertIn("bench boost played in gameweek 3: play now: +18.0", row["rationale"])
        payload = json.loads(row["payload"])
        self.assertEqual(payload["chip"], "bboost")
        self.assertEqual(payload["values"][0], {"gameweek": 3, "value": 18.0,
                                                "note": "P12, P13, P14, P15"})


class RebuildTests(unittest.TestCase):
    """Free hit and wildcard: the best legal fifteen against the held one."""

    #: An FPL-shaped squad from the test market (types cycle 1-4 by id).
    SHAPED = [1, 5, 2, 6, 10, 14, 18, 3, 7, 11, 15, 19, 4, 8, 12]

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.warehouse = Warehouse(self.conn).healthy()
        self.conn.execute("DELETE FROM my_squad")
        self.warehouse.squad(self.SHAPED)
        wall = [dict(c, stop_event=GAMEWEEK + 2) for c in FIRST_SET]
        for c in wall:
            c["status_for_entry"] = "available"
            c["played_by_entry"] = []
        self.warehouse.state(chips=json.dumps(wall))
        self.conn.commit()

    def project(self, gameweek, element_id, xp):
        self.conn.execute(
            "UPDATE projection SET expected_points = ? WHERE element_id = ? "
            "AND gameweek = ? AND snapshot_id = ?",
            (xp, element_id, gameweek, self.warehouse.snapshot_id))
        self.conn.commit()

    def evaluate(self):
        return brief.evaluate(self.conn, GAMEWEEK, now=NOW, include_token=False)

    def verdict(self, name):
        return next(v for v in self.evaluate().chips if v.state.name == name)

    def test_a_flat_market_is_worth_little_and_under_the_bar(self):
        fh = self.verdict("freehit")
        self.assertTrue(fh.evaluated)
        self.assertFalse(fh.play_now)
        self.assertIn("under the 8 bar", fh.reason)
        self.assertEqual([v.gameweek for v in fh.values], [3, 4, 5])

    def test_a_week_where_the_market_leaves_the_squad_behind_fires_the_free_hit(self):
        # Three unowned midfielders worth 8 each in GW4 only: a free hit buys them.
        for e in (23, 27, 31):
            self.project(GAMEWEEK + 1, e, 8.0)
        fh = self.verdict("freehit")
        self.assertFalse(fh.play_now)
        self.assertIn("GW4 clears it", fh.reason)
        week = next(v for v in fh.values if v.gameweek == GAMEWEEK + 1)
        self.assertGreater(week.value, 8.0)
        self.assertIn("P27", week.note)      # P23 is already in via the recommended move
        self.assertEqual(len(week.squad), 15)

    def test_a_free_hit_that_clears_the_bar_this_week_fires_and_shows_the_squad(self):
        for e in (23, 27, 31):
            self.project(GAMEWEEK, e, 8.0)
        evaluation = self.evaluate()
        fh = next(v for v in evaluation.chips if v.state.name == "freehit")
        self.assertTrue(fh.play_now)
        names = [t.headline for t in evaluation.triggers if t.name == "chip_worth_playing"]
        self.assertTrue(any(h.startswith("Play your free hit this gameweek") for h in names))
        text = brief.render_brief(self.conn, GAMEWEEK, now=NOW, evaluation=evaluation,
                                  include_token=False, notifications_configured=False,
                                  learnings_dir=Path(self.tmp.name))
        self.assertIn("The free hit squad for GW3 (XI first, then bench): ", text)
        self.assertIn("P23 £5.0m 8.0; P27 £5.0m 8.0; P31 £5.0m 8.0", text)

    def test_the_wildcard_is_valued_over_its_horizon_capped_at_the_wall(self):
        # The wall is GW5, so a GW3 wildcard spans 3-5 and a GW5 one just 5.
        for e in (23, 27, 31):
            for gw in (GAMEWEEK, GAMEWEEK + 1, GAMEWEEK + 2):
                self.project(gw, e, 8.0)
        wc = self.verdict("wildcard")
        self.assertTrue(wc.play_now)
        self.assertIn("clears the 15 bar", wc.reason)
        by_week = {v.gameweek: v.value for v in wc.values}
        self.assertGreater(by_week[3], by_week[5])       # three weeks of gain vs one
        text = brief.render_brief(self.conn, GAMEWEEK, now=NOW, include_token=False,
                                  notifications_configured=False,
                                  learnings_dir=Path(self.tmp.name))
        self.assertIn("moot if you play the wildcard (see Chips)", text)

    def test_the_budget_is_bank_plus_selling_prices(self):
        self.assertEqual(chips.rebuild_budget(self.conn, self.warehouse.snapshot_id),
                         10 + 15 * 50)

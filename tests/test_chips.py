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

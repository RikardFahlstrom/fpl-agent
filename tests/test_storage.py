"""Tests for the SQLite warehouse. All local: nothing here touches the FPL API."""
import json
import sqlite3
import unittest

from fpl_agent.engine import storage


def _element(element_id=1, **overrides):
    element = {
        "id": element_id, "web_name": f"Player{element_id}", "first_name": "First",
        "second_name": f"Last{element_id}", "team": 1, "element_type": 3,
        "now_cost": 56, "form": "5.5", "points_per_game": "4.2", "total_points": 42,
        "minutes": 540, "status": "a", "chance_of_playing_next_round": None,
        "selected_by_percent": "12.3", "expected_goals_per_90": "0.41",
        "expected_assists_per_90": "0.22", "expected_goals_conceded_per_90": "1.1",
        "starts_per_90": "0.9", "penalties_order": 1, "news": "",
        "price_change_percent": "92.6", "price_change_locked_until": None,
        "price_change_projections": [
            {"offset": 0, "projected_percent": "100.7", "likelihood": 5},
            {"offset": 1, "projected_percent": "104.2", "likelihood": 5},
        ],
        "transfers_in_event": 268143, "transfers_out_event": 1200,
    }
    element.update(overrides)
    return element


def _bootstrap(**overrides):
    data = {
        "elements": [_element(1), _element(2, now_cost=45, price_change_percent="-98.1")],
        "teams": [{"id": 1, "name": "Test United", "short_name": "TSU", "strength": 4}],
        "element_types": [{"id": 3, "singular_name_short": "MID"}],
        "events": [
            {"id": 2, "is_current": True, "is_next": False, "finished": True},
            {"id": 3, "is_current": False, "is_next": True, "finished": False},
        ],
        "game_settings": {"squad_total_spend": 1000, "transfers_sell_on_fee": 0.5},
        "game_config": {
            "scoring": {"goals_scored": {"DEF": 6, "MID": 5}, "assists": 3},
            "rules": {"squad_team_limit": 3},
        },
        "chips": [{"name": "wildcard", "start_event": 2, "stop_event": 19}],
    }
    data.update(overrides)
    return data


def _fixture(fixture_id=1, event=3, finished=False):
    return {"id": fixture_id, "event": event, "team_h": 1, "team_a": 2,
            "team_h_difficulty": 3, "team_a_difficulty": 2, "team_h_score": None,
            "team_a_score": None, "kickoff_time": "2026-09-04T14:00:00Z",
            "finished": finished}


def _history(element_id=1, rnd=1, points=8):
    return {"element": element_id, "round": rnd, "fixture": 1, "opponent_team": 2,
            "was_home": True, "minutes": 90, "total_points": points, "goals_scored": 1,
            "assists": 0, "clean_sheets": 1, "goals_conceded": 0, "bonus": 2, "bps": 30,
            "starts": 1, "expected_goals": "0.55", "expected_assists": "0.12",
            "expected_goal_involvements": "0.67", "expected_goals_conceded": "0.80",
            "defensive_contribution": 4, "value": 56, "selected": 900000,
            "transfers_balance": 12000}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_schema_is_created_and_reentrant(self):
        storage.connect(":memory:")  # second call must not fail on existing objects
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"snapshot", "player", "player_snapshot", "player_gameweek",
             "fixture", "game_config", "my_squad", "my_state", "team"}, tables)

    def test_target_gameweek_prefers_next_then_current(self):
        self.assertEqual(storage.target_gameweek(_bootstrap()), 3)
        only_current = _bootstrap(events=[{"id": 7, "is_current": True, "is_next": False,
                                           "finished": False}])
        self.assertEqual(storage.target_gameweek(only_current), 7)
        self.assertIsNone(storage.target_gameweek(_bootstrap(events=[])))

    def test_snapshot_records_typed_and_raw_columns(self):
        boot = _bootstrap()
        snapshot_id = storage.create_snapshot(self.conn, boot)
        storage.upsert_teams(self.conn, boot)
        storage.upsert_players(self.conn, boot)
        written = storage.record_player_snapshot(self.conn, snapshot_id, boot)

        self.assertEqual(written, 2)
        row = self.conn.execute(
            "SELECT * FROM player_snapshot WHERE element_id = 1").fetchone()
        # strings from the API become real numbers
        self.assertEqual(row["now_cost"], 56)
        self.assertAlmostEqual(row["form"], 5.5)
        self.assertAlmostEqual(row["expected_goals_per_90"], 0.41)
        self.assertAlmostEqual(row["price_change_percent"], 92.6)
        # the price forecast survives intact
        projections = json.loads(row["price_change_projections"])
        self.assertEqual(projections[0]["likelihood"], 5)
        # and nothing is lost
        self.assertEqual(json.loads(row["raw"])["web_name"], "Player1")

    def test_unmodelled_api_fields_survive_in_raw(self):
        """A new FPL field must not need a migration to be retained."""
        boot = _bootstrap(elements=[_element(1, some_new_stat_2027=17)])
        snapshot_id = storage.create_snapshot(self.conn, boot)
        storage.upsert_players(self.conn, boot)
        storage.record_player_snapshot(self.conn, snapshot_id, boot)

        raw = json.loads(self.conn.execute(
            "SELECT raw FROM player_snapshot WHERE element_id = 1").fetchone()["raw"])
        self.assertEqual(raw["some_new_stat_2027"], 17)

    def test_missing_numerics_become_null_not_zero(self):
        """An absent value must not be recorded as a real zero - it would skew any model."""
        boot = _bootstrap(elements=[_element(1, form="", expected_goals_per_90=None,
                                             chance_of_playing_next_round=None)])
        snapshot_id = storage.create_snapshot(self.conn, boot)
        storage.upsert_players(self.conn, boot)
        storage.record_player_snapshot(self.conn, snapshot_id, boot)

        row = self.conn.execute(
            "SELECT * FROM player_snapshot WHERE element_id = 1").fetchone()
        self.assertIsNone(row["form"])
        self.assertIsNone(row["expected_goals_per_90"])
        self.assertIsNone(row["chance_of_playing_next_round"])

    def test_consecutive_snapshots_preserve_price_history(self):
        """The whole point of the warehouse: yesterday's price is still there tomorrow."""
        boot = _bootstrap()
        first = storage.create_snapshot(self.conn, boot)
        storage.upsert_players(self.conn, boot)
        storage.record_player_snapshot(self.conn, first, boot)

        risen = _bootstrap(elements=[_element(1, now_cost=57, price_change_percent="4.1")])
        second = storage.create_snapshot(self.conn, risen)
        storage.record_player_snapshot(self.conn, second, risen)

        costs = [r["now_cost"] for r in self.conn.execute(
            "SELECT now_cost FROM player_snapshot WHERE element_id = 1 ORDER BY snapshot_id")]
        self.assertEqual(costs, [56, 57])

    def test_game_config_scoring_is_stored(self):
        storage.record_game_config(self.conn, _bootstrap())
        row = self.conn.execute("SELECT * FROM game_config").fetchone()
        self.assertEqual(json.loads(row["scoring"])["goals_scored"]["MID"], 5)
        self.assertEqual(json.loads(row["rules"])["squad_team_limit"], 3)
        self.assertEqual(json.loads(row["settings"])["transfers_sell_on_fee"], 0.5)
        self.assertEqual(json.loads(row["chips"])[0]["name"], "wildcard")

    def test_fixtures_upsert_on_rerun(self):
        self.assertEqual(storage.upsert_fixtures(self.conn, [_fixture()]), 1)
        storage.upsert_fixtures(self.conn, [dict(_fixture(finished=True),
                                                 team_h_score=2, team_a_score=1)])
        rows = self.conn.execute("SELECT * FROM fixture").fetchall()
        self.assertEqual(len(rows), 1)          # updated, not duplicated
        self.assertEqual(rows[0]["team_h_score"], 2)
        self.assertEqual(rows[0]["finished"], 1)

    def test_player_gameweeks_are_idempotent(self):
        boot = _bootstrap()
        storage.upsert_players(self.conn, boot)
        storage.record_player_gameweeks(self.conn, [_history(1, 1, 8), _history(1, 2, 5)])
        storage.record_player_gameweeks(self.conn, [_history(1, 1, 8)])  # re-run

        rows = self.conn.execute(
            "SELECT round, total_points FROM player_gameweek ORDER BY round").fetchall()
        self.assertEqual([(r["round"], r["total_points"]) for r in rows], [(1, 8), (2, 5)])

    def test_my_team_records_selling_price_and_free_transfers(self):
        boot = _bootstrap()
        snapshot_id = storage.create_snapshot(self.conn, boot)
        storage.upsert_players(self.conn, boot)
        picks = storage.record_my_team(self.conn, snapshot_id, 431892, {
            "picks": [{"element": 1, "position": 1, "multiplier": 2, "is_captain": True,
                       "is_vice_captain": False, "selling_price": 57, "purchase_price": 55}],
            "transfers": {"bank": 5, "value": 1004, "limit": 2, "made": 1, "cost": 4},
            "chips": [{"name": "bboost"}],
        })

        self.assertEqual(picks, 1)
        squad = self.conn.execute("SELECT * FROM my_squad").fetchone()
        self.assertEqual(squad["selling_price"], 57)     # exists nowhere public
        self.assertEqual(squad["purchase_price"], 55)
        self.assertEqual(squad["is_captain"], 1)
        state = self.conn.execute("SELECT * FROM my_state").fetchone()
        self.assertEqual(state["free_transfers"], 1)     # limit 2 - made 1
        self.assertEqual(state["bank"], 5)

    def test_preseason_null_transfer_state_is_preserved(self):
        boot = _bootstrap()
        snapshot_id = storage.create_snapshot(self.conn, boot)
        storage.record_my_team(self.conn, snapshot_id, 1, {
            "picks": [], "chips": None,
            "transfers": {"bank": None, "value": None, "limit": None,
                          "made": None, "cost": None}})
        state = self.conn.execute("SELECT * FROM my_state").fetchone()
        self.assertIsNone(state["free_transfers"])
        self.assertIsNone(state["bank"])

    def test_snapshot_taken_today(self):
        self.assertFalse(storage.snapshot_taken_today(self.conn))
        storage.create_snapshot(self.conn, _bootstrap())
        self.assertTrue(storage.snapshot_taken_today(self.conn))


if __name__ == "__main__":
    unittest.main()

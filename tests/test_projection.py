"""Projection tests. Offline: every input is constructed locally."""
import json
import math
import unittest

from fpl_agent import projection, storage
from fpl_agent.projection import (
    availability, clean_sheet_probability, project_player, shrink, start_rate,
)
from fpl_agent.scoring import Scoring
from test_scoring import WEIGHTS


def snap(**overrides):
    """A player_snapshot row; project_player only subscripts it, so a dict will do."""
    row = {"element_id": 1, "minutes": 900, "status": "a",
           "chance_of_playing_next_round": None, "expected_goals_per_90": 0.5,
           "expected_assists_per_90": 0.3, "expected_goals_conceded_per_90": 1.2}
    row.update(overrides)
    return row


HISTORY = {"appearances": 10.0, "starts": 10.0, "bonus": 5.0, "minutes": 900.0,
           "dc_rate": 0.2, "yellow_per_90": 0.2}
PRIORS = {"xg90": 0.10, "xa90": 0.08}
ONE_FIXTURE = [{"difficulty": 3, "home": True}]


class AvailabilityTests(unittest.TestCase):
    def test_explicit_chance_wins_over_status(self):
        self.assertEqual(availability(snap(chance_of_playing_next_round=75)), 0.75)
        self.assertEqual(availability(snap(status="d", chance_of_playing_next_round=25)), 0.25)

    def test_non_available_status_without_a_chance_is_zero(self):
        self.assertEqual(availability(snap(status="i")), 0.0)
        self.assertEqual(availability(snap(status="s")), 0.0)

    def test_fit_player_is_fully_available(self):
        self.assertEqual(availability(snap()), 1.0)

    def test_start_rate_uses_history_then_falls_back(self):
        self.assertEqual(start_rate({"appearances": 4.0, "starts": 2.0}), 0.5)
        self.assertEqual(start_rate({}), projection.BASE_START_PROB)

    def test_no_appearances_once_the_season_has_started_is_evidence(self):
        """A fit player nobody has picked in two gameweeks is not an 85% starter.

        Regression: reserve goalkeepers projected 12.54 xP over three gameweeks and
        ranked among the best value in the game.
        """
        self.assertEqual(start_rate({}, season_started=True),
                         projection.UNUSED_START_PROB)
        self.assertLess(projection.UNUSED_START_PROB, projection.BASE_START_PROB)

    def test_a_player_with_appearances_is_unaffected_by_the_season_flag(self):
        history = {"appearances": 2.0, "starts": 2.0}
        self.assertEqual(start_rate(history, season_started=True), 1.0)
        self.assertEqual(start_rate(history, season_started=False), 1.0)


class ShrinkageTests(unittest.TestCase):
    def test_no_evidence_returns_the_prior(self):
        self.assertEqual(shrink(rate=9.9, minutes=0, prior=0.2), 0.2)

    def test_thin_evidence_is_pulled_toward_the_prior(self):
        """2 goals in 63 minutes reads as xG90 2.0; it must not be taken at face value."""
        pulled = shrink(rate=2.0, minutes=63, prior=0.1)
        self.assertLess(pulled, 0.5)
        self.assertGreater(pulled, 0.1)

    def test_ample_evidence_keeps_the_observed_rate(self):
        self.assertAlmostEqual(shrink(rate=0.8, minutes=3000, prior=0.1), 0.74, places=1)

    def test_clean_sheet_probability_is_poisson_zero(self):
        self.assertAlmostEqual(clean_sheet_probability(0.0), 1.0)
        self.assertAlmostEqual(clean_sheet_probability(1.4), math.exp(-1.4))
        self.assertLess(clean_sheet_probability(3.0), clean_sheet_probability(1.0))


class ProjectPlayerTests(unittest.TestCase):
    def setUp(self):
        self.scoring = Scoring(WEIGHTS)

    def project(self, row, position="MID", fixtures=ONE_FIXTURE, history=None,
                team_conceded=1.2):
        return project_player(row, position, fixtures,
                              dict(HISTORY if history is None else history),
                              self.scoring, PRIORS, team_conceded)

    def test_ruled_out_player_projects_exactly_zero(self):
        """Regression: an injured player was collecting bench points and card penalties."""
        result = self.project(snap(status="i"))
        self.assertEqual(result["expected_points"], 0.0)
        self.assertEqual(result["p_start"], 0.0)
        for name, value in result["components"].items():
            self.assertEqual(value, 0.0, f"{name} should be zero for an unavailable player")

    def test_never_played_player_gets_no_free_clean_sheet(self):
        """Regression: xGC90 of 0.0 meant exp(-0) = a certain clean sheet, worth 4 points.

        The conceded rate is a team property and must come from the team, not from a
        player with no minutes behind him.
        """
        result = self.project(snap(minutes=0, expected_goals_conceded_per_90=0.0),
                              position="DEF", history={}, team_conceded=1.3)
        self.assertLess(result["components"]["clean_sheet"], 2.0)

    def test_doubtful_player_is_scaled_not_zeroed(self):
        full = self.project(snap())["expected_points"]
        half = self.project(snap(chance_of_playing_next_round=50))["expected_points"]
        self.assertLess(half, full)
        self.assertGreater(half, 0.0)

    def test_blank_gameweek_projects_zero(self):
        self.assertEqual(self.project(snap(), fixtures=[])["expected_points"], 0.0)

    def test_double_gameweek_roughly_doubles(self):
        single = self.project(snap())["expected_points"]
        double = self.project(snap(), fixtures=ONE_FIXTURE * 2)["expected_points"]
        # expected_points is rounded to 3dp, so compare at 2
        self.assertAlmostEqual(double, single * 2, places=2)

    def test_harder_fixture_lowers_the_projection(self):
        easy = self.project(snap(), fixtures=[{"difficulty": 2, "home": True}])
        hard = self.project(snap(), fixtures=[{"difficulty": 5, "home": True}])
        self.assertGreater(easy["expected_points"], hard["expected_points"])

    def test_components_sum_to_the_total(self):
        result = self.project(snap())
        self.assertAlmostEqual(sum(result["components"].values()),
                               result["expected_points"], places=2)

    def test_scoring_weights_drive_the_result(self):
        """A scoring change must move projections without any code change."""
        base = self.project(snap(), position="DEF")["components"]["goals"]
        self.scoring = Scoring({**WEIGHTS, "goals_scored": {**WEIGHTS["goals_scored"], "DEF": 12}})
        doubled = self.project(snap(), position="DEF")["components"]["goals"]
        # components are rounded to 3dp, so compare at 2
        self.assertAlmostEqual(doubled, base * 2, places=2)

    def test_goalkeepers_never_earn_defensive_contribution(self):
        result = self.project(snap(), position="GKP", history={**HISTORY, "dc_rate": 1.0})
        self.assertEqual(result["components"]["defensive_contribution"], 0.0)


class ProjectGameweekTests(unittest.TestCase):
    def _seed(self):
        conn = storage.connect(":memory:")
        bootstrap = {
            "elements": [{
                "id": 1, "web_name": "Striker", "first_name": "A", "second_name": "B",
                "team": 1, "element_type": 4, "now_cost": 90, "form": "5.0",
                "points_per_game": "5.0", "total_points": 40, "minutes": 900,
                "status": "a", "chance_of_playing_next_round": None,
                "selected_by_percent": "20.0", "expected_goals_per_90": "0.7",
                "expected_assists_per_90": "0.2", "expected_goals_conceded_per_90": "1.1",
                "starts_per_90": "1.0", "penalties_order": 1, "news": "",
                "price_change_percent": "10.0", "price_change_locked_until": None,
                "price_change_projections": [], "transfers_in_event": 100,
                "transfers_out_event": 10,
            }],
            "teams": [{"id": 1, "name": "Test United", "short_name": "TSU"},
                      {"id": 2, "name": "Test City", "short_name": "TSC"}],
            "element_types": [{"id": 4, "singular_name_short": "FWD"}],
            "events": [{"id": 3, "is_current": False, "is_next": True, "finished": False}],
            "game_settings": {},
            "game_config": {"scoring": WEIGHTS, "rules": {}},
            "chips": [],
        }
        snapshot_id = storage.create_snapshot(conn, bootstrap)
        storage.upsert_teams(conn, bootstrap)
        storage.upsert_players(conn, bootstrap)
        storage.record_player_snapshot(conn, snapshot_id, bootstrap)
        storage.record_game_config(conn, bootstrap)
        storage.upsert_fixtures(conn, [{
            "id": 1, "event": 3, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
            "team_a_difficulty": 3, "team_h_score": None, "team_a_score": None,
            "kickoff_time": "2026-09-04T14:00:00Z", "finished": False}])
        return conn

    def test_projects_and_is_rerunnable(self):
        conn = self._seed()
        self.addCleanup(conn.close)

        self.assertEqual(projection.project_gameweek(conn, 3), 1)
        projection.project_gameweek(conn, 3)          # same model, same snapshot

        rows = conn.execute("SELECT * FROM projection").fetchall()
        self.assertEqual(len(rows), 1, "re-running must replace, not duplicate")
        self.assertEqual(rows[0]["gameweek"], 3)
        self.assertGreater(rows[0]["expected_points"], 0)
        self.assertEqual(rows[0]["fixture_count"], 1)
        self.assertIn("goals", json.loads(rows[0]["components"]))

    def test_model_versions_coexist(self):
        """A weight change must be comparable against the previous era, not overwrite it."""
        conn = self._seed()
        self.addCleanup(conn.close)
        projection.project_gameweek(conn, 3, model_version="0.1.0")
        projection.project_gameweek(conn, 3, model_version="0.2.0")
        versions = {r["model_version"] for r in
                    conn.execute("SELECT model_version FROM projection")}
        self.assertEqual(versions, {"0.1.0", "0.2.0"})

    def test_requires_a_snapshot(self):
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        with self.assertRaises(LookupError):
            projection.project_gameweek(conn, 3)


if __name__ == "__main__":
    unittest.main()

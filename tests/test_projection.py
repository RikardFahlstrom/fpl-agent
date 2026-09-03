"""Projection tests. Offline: every input is constructed locally."""
import json
import math
import unittest

from fpl_agent.engine import projection, storage
from fpl_agent.engine.projection import (
    availability, clean_sheet_probability, project_player, shrink, start_rate,
)
from fpl_agent.engine.scoring import Scoring
from test_scoring import WEIGHTS


def snap(**overrides):
    """A player_snapshot row; project_player only subscripts it, so a dict will do."""
    row = {"element_id": 1, "minutes": 900, "status": "a",
           "chance_of_playing_next_round": None, "expected_goals_per_90": 0.5,
           "expected_assists_per_90": 0.3, "expected_goals_conceded_per_90": 1.2}
    row.update(overrides)
    return row


HISTORY = {"appearances": 10.0, "games": 10.0, "starts": 10.0, "bonus": 5.0,
           "minutes": 900.0, "dc_rate": 0.2, "yellow_per_90": 0.2}
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
        # Two starts in four games, against a position that starts half of its games:
        # the shrink has nothing to pull toward, so the observed rate stands.
        self.assertEqual(
            start_rate({"games": 4.0, "starts": 2.0, "start_prior": 0.5}), 0.5)
        self.assertEqual(start_rate({}), projection.BASE_START_PROB)

    def test_no_appearances_once_the_season_has_started_is_evidence(self):
        """A fit player nobody has picked in two gameweeks is not an 85% starter.

        Regression: reserve goalkeepers projected 12.54 xP over three gameweeks and
        ranked among the best value in the game.
        """
        self.assertEqual(start_rate({}, season_started=True),
                         projection.UNUSED_START_PROB)
        self.assertLess(projection.UNUSED_START_PROB, projection.BASE_START_PROB)

    def test_a_published_lineup_overrides_the_historical_start_rate(self):
        """Selection comes from the lineup; fitness still comes from FPL's flag."""
        from fpl_agent.engine.projection import project_player
        from fpl_agent.engine.scoring import Scoring
        from test_scoring import WEIGHTS

        history = {"appearances": 10.0, "games": 10.0, "starts": 10.0, "bonus": 0.0,
                   "minutes": 900.0, "dc_rate": 0.0, "yellow_per_90": 0.0}
        row = {"element_id": 1, "minutes": 900, "status": "a",
               "chance_of_playing_next_round": None, "expected_goals_per_90": 0.5,
               "expected_assists_per_90": 0.3, "expected_goals_conceded_per_90": 1.2}
        args = ("MID", [{"difficulty": 3, "home": True}], history,
                Scoring(WEIGHTS), {"xg90": 0.1, "xa90": 0.08}, 1.2)

        without = project_player(row, *args, True)
        left_out = project_player(row, *args, True, lineup_rate=0.15)
        self.assertAlmostEqual(without["p_start"], start_rate(history, True))
        self.assertGreater(without["p_start"], 0.8, "ten starts in ten games")
        self.assertAlmostEqual(left_out["p_start"], 0.15)
        self.assertLess(left_out["expected_points"], without["expected_points"])

    def test_an_unavailable_player_stays_out_even_if_a_lineup_names_him(self):
        """Fitness and selection are different questions; the flag still applies."""
        from fpl_agent.engine.projection import project_player
        from fpl_agent.engine.scoring import Scoring
        from test_scoring import WEIGHTS

        row = {"element_id": 1, "minutes": 900, "status": "i",
               "chance_of_playing_next_round": 0, "expected_goals_per_90": 0.5,
               "expected_assists_per_90": 0.3, "expected_goals_conceded_per_90": 1.2}
        result = project_player(row, "MID", [{"difficulty": 3, "home": True}], {},
                                Scoring(WEIGHTS), {"xg90": 0.1, "xa90": 0.08}, 1.2,
                                True, 0.90)
        self.assertEqual(result["p_start"], 0.0)
        self.assertEqual(result["expected_points"], 0.0)

    def test_a_player_with_games_is_unaffected_by_the_season_flag(self):
        history = {"games": 2.0, "starts": 2.0, "start_prior": 1 / 3}
        # (2 starts + 3 prior games at 1/3) / (2 + 3) - two out of two is a real record,
        # but not yet proof of a nailed-on starter.
        self.assertEqual(start_rate(history, season_started=True), 0.6)
        self.assertEqual(start_rate(history, season_started=False), 0.6)

    def test_a_benching_counts_against_the_start_rate(self):
        """One start and one unused-substitute appearance is not a certain starter.

        Regression: the denominator was games *played*, so the benching vanished and the
        rate came out 1.0. Onyeka (COV) went into GW4 at p_start 1.00 on that record.
        """
        history = {"appearances": 1.0, "games": 2.0, "starts": 1.0, "start_prior": 1 / 3}
        self.assertEqual(start_rate(history, season_started=True), 0.4)

    def test_a_long_record_of_starts_survives_the_shrink(self):
        """Thirty starts in thirty games is evidence enough to be taken at face value."""
        history = {"games": 30.0, "starts": 30.0, "start_prior": 0.34}
        self.assertAlmostEqual(start_rate(history, season_started=True), 0.94)

    def test_a_player_who_has_never_started_is_not_shrunk_upward(self):
        """The reserve keeper must not be rescued by the prior.

        His position starts 0.29 of its games, so shrinking a 0-for-1 record toward it
        would report 0.22 - twice UNUSED_START_PROB, and the 0.2.0 bug back in a new
        coat. Never being picked is evidence, not thin evidence.
        """
        history = {"appearances": 0.0, "games": 1.0, "starts": 0.0, "start_prior": 0.29}
        would_have_been = (0.29 * projection.APPEARANCE_PRIOR) / (1 + projection.APPEARANCE_PRIOR)
        self.assertGreater(would_have_been, 0.2)
        self.assertEqual(start_rate(history, season_started=True),
                         projection.UNUSED_START_PROB)


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


class PlayerHistoryTests(unittest.TestCase):
    """The two denominators, read off real rows.

    element-summary carries a row per team fixture whether the player played or not -
    614 of the 1236 rows in the live warehouse are at zero minutes - and grouping them
    away with `WHERE minutes > 0` threw away every benching.
    """

    def _history(self, squad):
        """squad: {element_id: [(minutes, starts), ...]}, all midfielders."""
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        storage.upsert_players(conn, {"elements": [
            {"id": element_id, "web_name": f"P{element_id}", "team": 1, "element_type": 3}
            for element_id in squad]})
        storage.record_player_gameweeks(conn, [
            {"element": element_id, "round": round_number, "minutes": minutes,
             "starts": starts, "bonus": 0, "defensive_contribution": 0}
            for element_id, rows in squad.items()
            for round_number, (minutes, starts) in enumerate(rows, 1)])
        return projection._player_history(conn)

    def _cohort(self):
        # Four of these six midfielders started one of their two games and one started
        # none: 4 starts in 12 games, a positional prior of exactly 1/3, close to the
        # 0.34 the live warehouse shows for midfielders.
        return self._history({1: [(90, 1), (0, 0)], 2: [(90, 1), (0, 0)],
                              3: [(90, 1), (0, 0)], 4: [(90, 1), (0, 0)],
                              5: [(0, 0), (0, 0)], 6: [(0, 0), (0, 0)]})

    def test_the_benched_game_is_counted_and_the_start_rate_halves(self):
        """One start, one benching: 0.4, not the 1.0 the played-games denominator gave."""
        entry = self._cohort()[1]
        self.assertEqual(entry["games"], 2.0)
        self.assertEqual(entry["starts"], 1.0)
        self.assertAlmostEqual(entry["start_prior"], 1 / 3)
        self.assertAlmostEqual(start_rate(entry, season_started=True), 0.4)

    def test_per_appearance_rates_keep_the_played_games_denominator(self):
        """Bonus and cards can only be earned on the pitch, so the benching is not one."""
        entry = self._cohort()[1]
        self.assertEqual(entry["appearances"], 1.0)
        self.assertEqual(entry["minutes"], 90.0)

    def test_a_player_who_has_only_been_benched_stays_at_the_unused_rate(self):
        """He is in `history` now, where he used to be absent; the answer must not move."""
        entry = self._cohort()[5]
        self.assertEqual(entry["games"], 2.0)
        self.assertEqual(entry["starts"], 0.0)
        self.assertEqual(entry["appearances"], 0.0)
        self.assertEqual(start_rate(entry, season_started=True),
                         projection.UNUSED_START_PROB)

    def test_the_prior_is_positional_not_league_wide(self):
        """Goalkeepers rotate less than forwards; one league number would blur both."""
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        storage.upsert_players(conn, {"elements": [
            {"id": 1, "web_name": "Keeper", "team": 1, "element_type": 1},
            {"id": 2, "web_name": "Sub keeper", "team": 1, "element_type": 1},
            {"id": 3, "web_name": "Forward", "team": 1, "element_type": 4}]})
        storage.record_player_gameweeks(conn, [
            {"element": 1, "round": 1, "minutes": 90, "starts": 1},
            {"element": 1, "round": 2, "minutes": 90, "starts": 1},
            {"element": 2, "round": 1, "minutes": 0, "starts": 0},
            {"element": 2, "round": 2, "minutes": 0, "starts": 0},
            {"element": 3, "round": 1, "minutes": 60, "starts": 1},
            {"element": 3, "round": 2, "minutes": 20, "starts": 0}])
        history = projection._player_history(conn)
        self.assertAlmostEqual(history[1]["start_prior"], 0.5)   # 2 starts / 4 GKP games
        self.assertAlmostEqual(history[3]["start_prior"], 0.5)   # 1 start / 2 FWD games


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

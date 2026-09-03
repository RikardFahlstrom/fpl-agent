"""Scoring tests. The reconstruction cases are real gameweeks from the 2026/27 season."""
import unittest

from fpl_agent.engine.scoring import DC_THRESHOLDS, Scoring

WEIGHTS = {
    "long_play": 2, "short_play": 1, "saves": 1, "assists": 3, "bonus": 1,
    "goals_scored": {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
    "clean_sheets": {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
    "goals_conceded": {"GKP": -1, "DEF": -1, "MID": 0, "FWD": 0},
    "defensive_contribution": {"GKP": 0, "DEF": 2, "MID": 2, "FWD": 2},
    "penalties_saved": 5, "penalties_missed": -2, "yellow_cards": -1,
    "red_cards": -3, "own_goals": -2,
}


def line(**overrides):
    base = {"minutes": 90, "goals_scored": 0, "assists": 0, "clean_sheets": 0,
            "goals_conceded": 0, "bonus": 0, "saves": 0, "yellow_cards": 0,
            "red_cards": 0, "own_goals": 0, "penalties_missed": 0,
            "penalties_saved": 0, "defensive_contribution": 0}
    base.update(overrides)
    return base


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.s = Scoring(WEIGHTS)

    def test_reconstructs_real_gameweeks(self):
        """Actual 2026/27 rows: if these drift, the weights or the rules moved."""
        cases = [
            # B.Fernandes MID: 3 goals, 1 assist, 3 bonus, dc 8 (under threshold) -> 23
            ("MID", line(goals_scored=3, assists=1, bonus=3, defensive_contribution=8), 23),
            # De Cuyper DEF 77': goal, assist, clean sheet, 2 bonus -> 17
            ("DEF", line(minutes=77, goals_scored=1, assists=1, clean_sheets=1, bonus=2,
                         defensive_contribution=4), 17),
            # Mendy DEF 63': goal, clean sheet, 1 bonus, dc 13 -> threshold met, 15
            ("DEF", line(minutes=63, goals_scored=1, clean_sheets=1, bonus=1,
                         defensive_contribution=13), 15),
            # Stach MID 90': goal, clean sheet, 3 bonus, dc 16 -> threshold met, 13
            ("MID", line(goals_scored=1, clean_sheets=1, bonus=3,
                         defensive_contribution=16), 13),
            # Haaland FWD: 2 goals, 3 bonus -> 13
            ("FWD", line(goals_scored=2, bonus=3, defensive_contribution=5), 13),
        ]
        for position, stat_line, expected in cases:
            with self.subTest(position=position, expected=expected):
                self.assertEqual(self.s.points(stat_line, position), expected)

    def test_defensive_contribution_threshold_is_exclusive_below(self):
        """DEF scores at 10 actions and not at 9; MID at 12 and not at 11."""
        self.assertEqual(self.s.points(line(defensive_contribution=9), "DEF"), 2)
        self.assertEqual(self.s.points(line(defensive_contribution=10), "DEF"), 4)
        self.assertEqual(self.s.points(line(defensive_contribution=11), "MID"), 2)
        self.assertEqual(self.s.points(line(defensive_contribution=12), "MID"), 4)
        # Goalkeepers never score it, whatever the count
        self.assertEqual(self.s.points(line(defensive_contribution=30), "GKP"), 2)

    def test_appearance_points_follow_the_60_minute_boundary(self):
        self.assertEqual(self.s.points(line(minutes=0), "MID"), 0)
        self.assertEqual(self.s.points(line(minutes=59), "MID"), 1)
        self.assertEqual(self.s.points(line(minutes=60), "MID"), 2)

    def test_clean_sheet_requires_60_minutes(self):
        self.assertEqual(self.s.points(line(minutes=59, clean_sheets=1), "DEF"), 1)
        self.assertEqual(self.s.points(line(minutes=60, clean_sheets=1), "DEF"), 6)

    def test_goals_conceded_and_saves_score_in_blocks(self):
        self.assertEqual(self.s.points(line(goals_conceded=1), "DEF"), 2)   # 1//2 = 0
        self.assertEqual(self.s.points(line(goals_conceded=2), "DEF"), 1)   # -1
        self.assertEqual(self.s.points(line(goals_conceded=3), "DEF"), 1)
        self.assertEqual(self.s.points(line(saves=2), "GKP"), 2)            # 2//3 = 0
        self.assertEqual(self.s.points(line(saves=3), "GKP"), 3)            # +1

    def test_weights_come_from_the_payload_not_constants(self):
        """A scoring change must flow through without touching code."""
        changed = Scoring({**WEIGHTS, "assists": 4})
        self.assertEqual(changed.points(line(assists=1), "MID"), 6)  # 2 + 4

    def test_thresholds_document_forward_as_inferred(self):
        self.assertEqual(DC_THRESHOLDS["DEF"], 10)
        self.assertEqual(DC_THRESHOLDS["MID"], 12)
        self.assertIsNone(DC_THRESHOLDS["GKP"])


if __name__ == "__main__":
    unittest.main()

"""Price-window tests.

The threshold under test is FPL's own, from its Price Changes page: "When Predicted
Progress exceeds 100%, the player is considered Very Likely to rise or fall."
"""
import json
import unittest

from fpl_agent.engine import pricing
from fpl_agent.engine.pricing import (
    APPROACHING_PROGRESS, VERY_LIKELY_PROGRESS, PriceOutlook, assess,
)


def outlook(**overrides):
    row = {"element_id": 1, "web_name": "Target", "now_cost": 56, "percent": 96.4,
           "projected_percent": 106.0, "likelihood": 5, "locked": False,
           "net_transfers": 268143}
    row.update(overrides)
    return PriceOutlook(**row)


class VeryLikelyTests(unittest.TestCase):
    def test_over_one_hundred_percent_predicted_progress_is_very_likely(self):
        self.assertTrue(outlook(projected_percent=106.0).rising)
        self.assertTrue(outlook(projected_percent=100.0).rising)
        self.assertTrue(outlook(projected_percent=-107.7).falling)

    def test_below_one_hundred_is_not_a_predicted_change(self):
        """The old model treated 40% progress as a rise and over-predicted 15x."""
        self.assertFalse(outlook(projected_percent=99.4).rising)
        self.assertFalse(outlook(projected_percent=86.8).rising)
        self.assertFalse(outlook(projected_percent=40.5).rising)
        self.assertFalse(outlook(projected_percent=-94.7).falling)

    def test_price_only_moves_on_a_very_likely_change(self):
        self.assertEqual(outlook(projected_percent=106.0).cost_after_change, 57)
        self.assertEqual(outlook(projected_percent=-104.0).cost_after_change, 55)
        self.assertEqual(outlook(projected_percent=94.0).cost_after_change, 56)

    def test_approaching_sits_between_the_two(self):
        near = outlook(projected_percent=97.0)
        self.assertTrue(near.approaching_rise)
        self.assertFalse(near.rising)
        self.assertEqual(near.cost_after_change, 56)
        self.assertFalse(outlook(projected_percent=90.0).approaching_rise)

    def test_progress_and_predicted_progress_are_different_numbers(self):
        """Madueke was -93% of the way but -100.7% predicted, so he falls."""
        crossing = outlook(percent=-93.0, projected_percent=-100.7)
        self.assertTrue(crossing.falling)
        self.assertEqual(crossing.cost_after_change, 55)

    def test_locked_players_do_not_move_again(self):
        locked = outlook(projected_percent=106.0, locked=True)
        self.assertFalse(locked.rising)
        self.assertEqual(locked.cost_after_change, 56)
        self.assertEqual(locked.status, "locked")

    def test_status_uses_fpl_wording(self):
        self.assertEqual(outlook(projected_percent=106.0).status, "very likely to rise")
        self.assertEqual(outlook(projected_percent=-106.0).status, "very likely to fall")
        self.assertEqual(outlook(projected_percent=97.0).status, "approaching a rise")
        self.assertEqual(outlook(projected_percent=10.0).status, "stable")


class AffordabilityTests(unittest.TestCase):
    def test_over_budget_is_already_missed(self):
        result = assess(budget=55, target=outlook(now_cost=56))
        self.assertEqual(result.urgency, "missed")

    def test_window_closing_tonight_at_the_exact_boundary(self):
        """£5.6m budget, £5.6m target, Very Likely to rise: affordable now, not after."""
        result = assess(budget=56, target=outlook(now_cost=56, projected_percent=106.0))
        self.assertEqual(result.urgency, "tonight")
        self.assertEqual(result.margin, 0)
        self.assertLess(result.margin_after_change, 0)
        self.assertIn("very likely to rise", result.reason)

    def test_a_player_merely_approaching_is_not_tonight(self):
        result = assess(budget=56, target=outlook(now_cost=56, projected_percent=97.0))
        self.assertEqual(result.urgency, "soon")
        self.assertIn("watch the next update", result.reason)

    def test_headroom_survives_the_rise(self):
        result = assess(budget=57, target=outlook(now_cost=56, projected_percent=106.0))
        self.assertEqual(result.urgency, "soon")
        self.assertGreaterEqual(result.margin_after_change, 0)

    def test_a_falling_holding_shrinks_the_budget_too(self):
        target = outlook(now_cost=56, projected_percent=106.0)
        held = outlook(element_id=2, web_name="Held", projected_percent=-104.0)
        self.assertEqual(assess(budget=57, target=target).urgency, "soon")
        squeezed = assess(budget=57, target=target, holding=held)
        self.assertEqual(squeezed.urgency, "tonight")
        self.assertIn("shrinking the budget", squeezed.reason)

    def test_locked_target_is_not_urgent(self):
        result = assess(budget=56, target=outlook(now_cost=56, projected_percent=106.0,
                                                  locked=True))
        self.assertEqual(result.urgency, "none")
        self.assertIn("locked", result.reason)

    def test_no_pressure_when_nothing_is_moving(self):
        self.assertEqual(assess(budget=70, target=outlook(projected_percent=5.0)).urgency,
                         "none")


class ProjectionParsingTests(unittest.TestCase):
    def test_todays_update_is_the_one_that_matters(self):
        raw = json.dumps([{"offset": 2, "likelihood": 1, "projected_percent": "30"},
                          {"offset": 0, "likelihood": 5, "projected_percent": "106"},
                          {"offset": 1, "likelihood": 3, "projected_percent": "80"}])
        self.assertEqual(pricing._first_projection(raw)["projected_percent"], "106")

    def test_missing_or_broken_projections_are_tolerated(self):
        self.assertIsNone(pricing._first_projection(None))
        self.assertIsNone(pricing._first_projection("[]"))
        self.assertIsNone(pricing._first_projection("not json"))

    def test_thresholds_match_the_documented_rule(self):
        self.assertEqual(VERY_LIKELY_PROGRESS, 100.0)
        self.assertLess(APPROACHING_PROGRESS, VERY_LIKELY_PROGRESS)


if __name__ == "__main__":
    unittest.main()

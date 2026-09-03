"""Price-window tests. The likelihood scale is FPL's own; see pricing module docstring."""
import json
import unittest

from fpl_agent import pricing, storage
from fpl_agent.pricing import Affordability, PriceOutlook, assess


def outlook(**overrides):
    row = {"element_id": 1, "web_name": "Target", "now_cost": 56, "percent": 92.6,
           "likelihood": 5, "locked": False, "net_transfers": 268143}
    row.update(overrides)
    return PriceOutlook(**row)


class PriceOutlookTests(unittest.TestCase):
    def test_direction_follows_likelihood(self):
        self.assertTrue(outlook(likelihood=5).rising)
        self.assertTrue(outlook(likelihood=3).rising)
        self.assertFalse(outlook(likelihood=2).rising)
        self.assertTrue(outlook(likelihood=-3).falling)
        self.assertFalse(outlook(likelihood=-2).falling)
        self.assertFalse(outlook(likelihood=None).rising)

    def test_cost_after_change_moves_one_step(self):
        self.assertEqual(outlook(likelihood=5).cost_after_change, 57)
        self.assertEqual(outlook(likelihood=-4).cost_after_change, 55)
        self.assertEqual(outlook(likelihood=0).cost_after_change, 56)

    def test_locked_players_do_not_move_again(self):
        """price_change_locked_until means the change already happened today."""
        self.assertEqual(outlook(likelihood=5, locked=True).cost_after_change, 56)


class AffordabilityTests(unittest.TestCase):
    def test_over_budget_is_already_missed(self):
        result = assess(budget=55, target=outlook(now_cost=56))
        self.assertEqual(result.urgency, "missed")
        self.assertLess(result.margin, 0)

    def test_window_closing_tonight_at_the_exact_boundary(self):
        """£5.6m budget, £5.6m target, near-certain rise: affordable now, not tomorrow."""
        result = assess(budget=56, target=outlook(now_cost=56, likelihood=5))
        self.assertEqual(result.urgency, "tonight")
        self.assertEqual(result.margin, 0)
        self.assertLess(result.margin_after_change, 0)
        self.assertIn("rising", result.reason)

    def test_probable_rise_is_soon_rather_than_tonight(self):
        result = assess(budget=56, target=outlook(now_cost=56, likelihood=3))
        self.assertEqual(result.urgency, "soon")

    def test_headroom_survives_the_rise(self):
        result = assess(budget=57, target=outlook(now_cost=56, likelihood=5))
        self.assertEqual(result.urgency, "soon")
        self.assertGreaterEqual(result.margin_after_change, 0)

    def test_a_falling_holding_shrinks_the_budget_too(self):
        """The squeeze works from both ends: the target rises AND the budget drops."""
        target = outlook(now_cost=56, likelihood=5)
        held = outlook(element_id=2, web_name="Held", likelihood=-4)
        alone = assess(budget=57, target=target)
        with_fall = assess(budget=57, target=target, holding=held)
        self.assertEqual(alone.urgency, "soon")
        self.assertEqual(with_fall.urgency, "tonight")
        self.assertIn("shrinking the budget", with_fall.reason)

    def test_locked_target_is_not_urgent(self):
        result = assess(budget=56, target=outlook(now_cost=56, likelihood=5, locked=True))
        self.assertEqual(result.urgency, "none")
        self.assertIn("locked", result.reason)

    def test_no_pressure_when_nothing_is_moving(self):
        self.assertEqual(assess(budget=70, target=outlook(likelihood=0)).urgency, "none")


class ProjectionParsingTests(unittest.TestCase):
    def test_todays_tick_is_the_one_that_matters(self):
        """offset 0 closes the window tonight; later offsets are not the deadline."""
        raw = json.dumps([{"offset": 2, "likelihood": 1},
                          {"offset": 0, "likelihood": 5},
                          {"offset": 1, "likelihood": 3}])
        self.assertEqual(pricing._first_projection(raw)["likelihood"], 5)

    def test_missing_or_broken_projections_are_tolerated(self):
        self.assertIsNone(pricing._first_projection(None))
        self.assertIsNone(pricing._first_projection("[]"))
        self.assertIsNone(pricing._first_projection("not json"))


if __name__ == "__main__":
    unittest.main()

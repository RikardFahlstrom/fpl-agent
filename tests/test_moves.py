"""Multi-move transfer planning, on the brief's hand-built warehouse.

Every projection is 1.0 a gameweek unless a test says otherwise, so a swap's value is
exactly the difference its better player brings, and the hit arithmetic can be checked by
hand: horizon 3, so a 2.0-a-week player in for a 1.0 one is +3.0 gross.
"""

import unittest
from unittest import mock

from fpl_agent.engine import held, moves, squad as squads, storage, warehouse

from test_brief import Warehouse

#: Two keepers, five defenders, five midfielders, three forwards.
SHAPED = [1, 5, 2, 6, 10, 14, 18, 3, 7, 11, 15, 19, 4, 8, 12]


class MovesTestCase(unittest.TestCase):

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.w = Warehouse(self.conn).healthy(better=())
        self.conn.execute("DELETE FROM my_squad")
        self.w.squad(SHAPED)
        self.conn.commit()

    def better(self, *element_ids, xp=2.0):
        """These players project `xp` a gameweek; everyone else 1.0."""
        self.conn.execute("UPDATE projection SET expected_points = ? WHERE element_id IN (%s)"
                          % ",".join("?" * len(element_ids)), (xp, *element_ids))
        self.conn.commit()

    def state(self, **kwargs):
        self.w.state(**kwargs)
        self.conn.commit()

    def plan(self, bar=2.0, **kwargs) -> moves.MovePlan:
        capture = warehouse.latest(self.conn)
        return moves.plan(self.conn, held.read(self.conn, capture), capture, bar=bar,
                          **kwargs)


class ChoosingTests(MovesTestCase):

    def test_two_coordinated_moves_beat_the_best_single_swap(self):
        self.better(23, 27)                       # two midfielders worth +3.0 each
        self.state(free_transfers=2)
        plan = self.plan()
        self.assertEqual(plan.chosen.transfers, 2)
        self.assertEqual(plan.chosen.net, 6.0)
        self.assertEqual(plan.single.net, 3.0)
        self.assertGreater(plan.chosen.net, plan.single.net)
        self.assertTrue(plan.recommended)
        self.assertEqual({m.in_name for m in plan.moves}, {"P23", "P27"})

    def test_nothing_worth_doing_is_no_move_with_the_margin(self):
        self.better(23, xp=1.2)                   # +0.6 gross: under the 2.0 bar
        plan = self.plan()
        self.assertFalse(plan.recommended)
        self.assertEqual(plan.moves, ())
        self.assertEqual(plan.best_move.net, 0.6)
        self.assertAlmostEqual(plan.bar - plan.best_move.net, 1.4)

    def test_a_flat_market_has_no_move_at_all(self):
        plan = self.plan()
        self.assertFalse(plan.recommended)
        self.assertEqual(plan.chosen.transfers, 0)
        self.assertIsNone(plan.best_move)

    def test_do_nothing_is_always_an_option_and_is_worth_zero(self):
        plan = self.plan()
        self.assertEqual((plan.options[0].transfers, plan.options[0].net), (0, 0.0))


class HitTests(MovesTestCase):

    def test_hits_are_netted_and_only_beyond_the_free_transfers(self):
        self.better(23, 27, 31, 19, xp=3.0)       # +6.0 gross each, plenty to buy
        self.state(free_transfers=1)
        plan = self.plan()
        by_count = {o.transfers: o for o in plan.options}
        self.assertEqual(by_count[1].hit_cost, 0)
        self.assertEqual(by_count[2].hit_cost, 4)
        self.assertEqual(by_count[3].hit_cost, 8)
        for option in plan.options:
            self.assertAlmostEqual(option.net, option.gross - option.hit_cost, places=2)

    def test_the_default_cap_is_two_hits_over_the_banked_free_transfers(self):
        self.better(23, 27, 31, 19, 3, 7, xp=3.0)
        self.state(free_transfers=2)
        plan = self.plan()
        self.assertEqual(plan.hit_cap, 2)
        self.assertEqual(max(o.transfers for o in plan.options), 4)   # 2 free + 2 hits

    def test_no_free_transfer_means_every_move_pays(self):
        self.better(23, 27, 31, xp=3.0)
        self.state(free_transfers=0)
        plan = self.plan()
        self.assertEqual(max(o.transfers for o in plan.options), 2)
        self.assertTrue(all(o.hit_cost == 4 * o.transfers for o in plan.options))

    def test_a_smaller_cap_offers_fewer_moves(self):
        self.better(23, 27, 31, xp=3.0)
        self.state(free_transfers=1)
        plan = self.plan(hit_cap=0)
        self.assertEqual(max(o.transfers for o in plan.options), 1)

    def test_unknown_free_transfers_are_priced_as_none(self):
        self.better(23, xp=3.0)
        self.state(free_transfers=None)
        self.assertEqual(self.plan().free_transfers, 0)

    def test_an_active_transfer_chip_lifts_the_cap_and_charges_no_hit(self):
        self.better(23, 27, 31, 19, 3, 7, xp=3.0)
        self.w.wildcard()
        self.conn.commit()
        plan = self.plan()
        self.assertIsNone(plan.hit_cap)
        self.assertEqual(plan.chip, "wildcard")
        self.assertTrue(all(o.hit_cost == 0 for o in plan.options))
        self.assertGreaterEqual(plan.chosen.transfers, 4)


class LegalityTests(MovesTestCase):

    def test_the_budget_and_the_club_limit_hold_for_the_chosen_plan(self):
        self.better(23, 27, 31, 19, xp=3.0)
        self.state(bank=0, free_transfers=3)
        capture = warehouse.latest(self.conn)
        holding = held.read(self.conn, capture)
        plan = moves.plan(self.conn, holding, capture, bar=0.0)
        spent = sum(m.in_cost for m in plan.chosen.moves)
        returned = sum(m.out_price for m in plan.chosen.moves)
        self.assertLessEqual(spent - returned, holding.bank)
        after = holding
        for move in plan.chosen.moves:
            after = after.with_move(move.as_swap())
        self.assertLessEqual(max(after.club_counts().values()), holding.team_limit)
        self.assertEqual(len(after.players), 15)

    def test_a_transfer_hands_over_the_slot_but_not_the_armband(self):
        self.conn.execute("UPDATE my_squad SET multiplier = 2 WHERE element_id = 3")
        self.conn.commit()
        capture = warehouse.latest(self.conn)
        holding = held.read(self.conn, capture)
        swap = {"out": {"element_id": 3},
                "in": {"element_id": 23, "name": "P23", "team_id": 4, "now_cost": 50}}
        after = holding.with_move(swap)
        bought = next(p for p in after.players if p.element_id == 23)
        self.assertEqual((bought.position, bought.multiplier),
                         (next(p for p in holding.players if p.element_id == 3).position, 1))

    def test_a_squad_that_is_not_fpl_shaped_is_refused_not_called_no_move(self):
        self.conn.execute("DELETE FROM my_squad")
        self.w.squad(list(range(1, 16)))
        self.conn.commit()
        with self.assertRaises(LookupError) as raised:
            self.plan()
        self.assertIn("not FPL-shaped", str(raised.exception))

    def test_a_held_player_with_no_projection_is_refused(self):
        self.conn.execute("DELETE FROM projection WHERE element_id = 12")
        self.conn.commit()
        with self.assertRaises(LookupError):
            self.plan()


class ProofTests(MovesTestCase):

    def test_a_solver_that_does_not_prove_optimality_gives_no_plan(self):
        self.better(23, xp=3.0)
        with mock.patch.object(squads, "best_squad",
                               side_effect=RuntimeError("time limit reached")):
            with self.assertRaises(moves.NotProven):
                self.plan()

    def test_the_instructions_say_so_instead_of_presenting_a_partial_answer(self):
        from fpl_agent.engine import instructions
        from fpl_agent.engine.projection import MODEL_VERSION
        self.better(23, xp=3.0)
        capture = warehouse.latest(self.conn)
        with mock.patch.object(squads, "best_squad",
                               side_effect=RuntimeError("time limit reached")):
            built = instructions.build(self.conn, capture.gameweek, MODEL_VERSION,
                                       held.read(self.conn, capture), capture=capture)
        self.assertIsNone(built.plan)
        self.assertEqual(built.moves, ())
        self.assertIn("did not prove its answer optimal", instructions.moves_text(built))
        self.assertIsNotNone(built.lineup)       # the lineup is still advised

    def test_a_plan_survives_the_round_trip_through_storage(self):
        from fpl_agent.engine import instructions
        from fpl_agent.engine.projection import MODEL_VERSION
        self.better(23, 27, xp=2.0)
        self.state(free_transfers=2)
        capture = warehouse.latest(self.conn)
        built = instructions.build(self.conn, capture.gameweek, MODEL_VERSION,
                                   held.read(self.conn, capture), capture=capture)
        again = instructions.Instructions.from_payload(built.to_payload())
        self.assertEqual(again, built)
        self.assertEqual(len(built.to_payload()["moves"]), 2)


if __name__ == "__main__":
    unittest.main()

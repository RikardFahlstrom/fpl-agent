"""The stance: protect a lead or chase one, and what it does to the advice.

On the brief's hand-built warehouse. Ownership is arranged so one player is held by every
rival and another by none, with the second slightly worse on expected points: exactly the
case where the stance has to change the answer.
"""

import os
import unittest

from fpl_agent.engine import brief, instructions, moves, projection, stance, warehouse
from fpl_agent.engine.projection import MODEL_VERSION

from test_brief import GAMEWEEK, NOW
from test_instructions import InstructionsTestCase

LEAGUE = 7
OWNER = 1            # `Warehouse.state` logs in as entry 1
LEADER = 99


class StanceTestCase(InstructionsTestCase):

    def setUp(self):
        super().setUp()
        os.environ["FPL_RIVAL_LEAGUES"] = str(LEAGUE)     # restored by the base class
        self.conn.execute("UPDATE projection SET expected_points = 1.0")
        self.conn.execute("DELETE FROM rival_squad")
        self.conn.execute("INSERT INTO league VALUES (?, 'Test league', 'x', 3, 't')", (LEAGUE,))
        for entry in (LEADER, 98):
            self.conn.execute("INSERT INTO rival VALUES (?, ?, 'p', 'e', 1, 0)",
                              (entry, LEAGUE))
        self.conn.commit()

    def standings(self, owner_rank: int, owner_points: int, leader_points: int = 200):
        self.conn.execute("DELETE FROM league_standing")
        rows = [(LEAGUE, 2, OWNER, owner_rank, None, 0, owner_points, 1, "t"),
                (LEAGUE, 2, LEADER, 1 if owner_rank != 1 else 2, None, 0,
                 leader_points if owner_rank != 1 else owner_points - 10, 1, "t")]
        if owner_rank == 1:
            rows[1] = (LEAGUE, 2, LEADER, 2, None, 0, owner_points - 10, 1, "t")
        self.conn.executemany("INSERT INTO league_standing VALUES (?,?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def rivals_hold(self, element_id: int, *, captain: bool = False, gameweek: int = 2):
        for entry in (LEADER, 98):
            self.conn.execute("INSERT OR REPLACE INTO rival_squad VALUES (?,?,?,1,1,?,0)",
                              (entry, gameweek, element_id, 1 if captain else 0))
        self.conn.commit()

    def read(self, **kwargs) -> stance.Stance:
        return stance.read(self.conn, self.holding(), **kwargs)

    def plan(self, st, bar=0.0, **kwargs) -> moves.MovePlan:
        capture = warehouse.latest(self.conn)
        return moves.plan(self.conn, self.holding(), capture, bar=bar, stance=st, **kwargs)


class ModeTests(StanceTestCase):

    def test_trailing_is_chase_with_a_pull_scaled_by_the_gap(self):
        self.rivals_hold(12)
        self.standings(owner_rank=2, owner_points=150, leader_points=200)
        st = self.read()
        self.assertEqual((st.mode, st.rank, st.gap, st.applied), ("chase", 2, 50, True))
        self.assertAlmostEqual(st.pull, 0.5)

    def test_the_pull_is_full_from_the_set_gap_and_no_more(self):
        self.rivals_hold(12)
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        self.assertEqual(self.read().pull, 1.0)

    def test_first_is_protect(self):
        self.rivals_hold(12)
        self.standings(owner_rank=1, owner_points=300)
        st = self.read()
        self.assertEqual((st.mode, st.gap, st.pull), ("protect", 0, -1.0))

    def test_the_first_configured_league_is_the_one_used(self):
        self.rivals_hold(12)
        self.standings(owner_rank=2, owner_points=150)
        os.environ["FPL_RIVAL_LEAGUES"] = f"{LEAGUE},999"
        self.assertEqual(self.read().league, "Test league")

    def test_no_league_configured_means_no_stance_and_says_why(self):
        os.environ["FPL_RIVAL_LEAGUES"] = ""
        st = self.read()
        self.assertEqual(st.mode, "none")
        self.assertIn("no league is configured", st.line)

    def test_no_standings_means_no_stance_and_says_why(self):
        self.assertIn("no finished-round standings", self.read().line)

    def test_a_bonus_is_zero_without_a_stance(self):
        self.assertEqual(stance.NONE.bonus(5), 0.0)


class OwnershipTests(StanceTestCase):

    def test_stale_ownership_is_never_used_and_the_line_says_so(self):
        self.rivals_hold(12, gameweek=1)
        self.conn.execute("UPDATE fixture SET finished = 1 WHERE event = 2")
        self.w.fixtures(gameweek=2, finished=True)
        self.conn.commit()
        self.standings(owner_rank=2, owner_points=150)
        st = self.read()
        self.assertEqual(st.mode, "chase")
        self.assertFalse(st.applied)
        self.assertEqual(st.bonus(12, 3), 0.0)
        self.assertIn("not applied - ownership is not usable", st.line)

    def test_missing_ownership_is_never_used_either(self):
        self.standings(owner_rank=2, owner_points=150)       # nobody's picks captured
        st = self.read()
        self.assertFalse(st.applied)
        self.assertIn("not applied", st.line)

    def test_a_player_in_no_rival_squad_is_owned_by_nobody(self):
        self.rivals_hold(12)
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        st = self.read()
        self.assertEqual(st.eo(5), 0.0)
        self.assertGreater(st.bonus(5), st.bonus(12))


class WeightTests(StanceTestCase):

    def test_at_zero_weight_the_advice_is_the_plain_advice(self):
        self.rivals_hold(23)
        self.conn.execute("UPDATE projection SET expected_points = 2.0 WHERE element_id = 23")
        self.conn.execute("UPDATE projection SET expected_points = 1.9 WHERE element_id = 27")
        self.conn.commit()
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        zero = self.plan(self.read(weight=0.0))
        plain = self.plan(None)
        self.assertEqual(zero.chosen, plain.chosen)
        self.assertEqual(zero.options, plain.options)
        self.assertEqual(self.read(weight=0.0).bonus(5, 3), 0.0)
        self.assertIn("weight is zero", self.read(weight=0.0).line)

    def test_the_weight_is_one_named_small_constant(self):
        self.assertLessEqual(stance.RANK_WEIGHT, 0.5)

    def test_the_model_version_was_bumped_for_advice_that_moved(self):
        self.assertNotEqual(projection.MODEL_VERSION, "0.5.2")
        self.assertEqual(MODEL_VERSION, projection.MODEL_VERSION)


class AdviceTests(StanceTestCase):
    """The same squad, led and trailed, where the field holds one candidate and not the other."""

    def setUp(self):
        super().setUp()
        # Two unowned midfielders for the same slot: the template one projects a little better.
        self.conn.execute("UPDATE projection SET expected_points = 2.0 WHERE element_id = 23")
        self.conn.execute("UPDATE projection SET expected_points = 1.9 WHERE element_id = 27")
        self.conn.commit()
        self.rivals_hold(23, captain=True)

    def bought(self, st) -> set[str]:
        return {m.in_name for m in self.plan(st).moves}

    def test_trailing_buys_the_differential_and_leading_buys_the_template_player(self):
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        chasing = self.bought(self.read())
        self.standings(owner_rank=1, owner_points=500)
        protecting = self.bought(self.read())
        self.assertEqual(self.bought(None), {"P23"})
        self.assertEqual(protecting, {"P23"})
        self.assertEqual(chasing, {"P27"})

    def test_the_plain_numbers_stay_plain_expected_points(self):
        # Leading, the template player (2.0 a week against a 1.0 held) is worth 6.0 - 3.0
        # = 3.0 xP; the stance adds 0.6 for leaving the held player nobody else owns.
        self.standings(owner_rank=1, owner_points=500)
        chosen = self.plan(self.read()).chosen
        self.assertEqual((chosen.gross, chosen.net), (3.0, 3.0))
        self.assertAlmostEqual(chosen.score, 3.6)
        plain = self.plan(None).chosen
        self.assertEqual((plain.net, plain.score), (3.0, 3.0))

    def test_the_captain_follows_the_stance(self):
        # Two forwards in the XI: the field's captain and a differential, 0.1 apart.
        self.conn.execute("UPDATE projection SET expected_points = 5.0 WHERE element_id = 12 "
                          "AND gameweek = ?", (GAMEWEEK,))
        self.conn.execute("UPDATE projection SET expected_points = 4.9 WHERE element_id = 8 "
                          "AND gameweek = ?", (GAMEWEEK,))
        self.conn.commit()
        self.rivals_hold(12, captain=True)
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        chasing = instructions.lineup(self.conn, GAMEWEEK, MODEL_VERSION, self.holding(),
                                      self.read())[0]
        self.standings(owner_rank=1, owner_points=500)
        protecting = instructions.lineup(self.conn, GAMEWEEK, MODEL_VERSION, self.holding(),
                                         self.read())[0]
        plain = instructions.lineup(self.conn, GAMEWEEK, MODEL_VERSION, self.holding())[0]
        self.assertEqual(plain.captain.name, "P12")
        self.assertEqual(protecting.captain.name, "P12")
        self.assertEqual(chasing.captain.name, "P8")
        self.assertIn("The stance chose P8", chasing.captain_note)
        self.assertEqual(plain.captain_note, "")

    def test_the_chip_alternatives_carry_the_stance_too(self):
        import json
        self.w.state(chips=json.dumps([
            {"name": "bboost", "chip_type": "team", "status_for_entry": "available",
             "start_event": 1, "stop_event": GAMEWEEK + 2, "played_by_entry": []}]))
        self.conn.commit()
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        capture = warehouse.latest(self.conn)
        built = instructions.build(self.conn, GAMEWEEK, MODEL_VERSION, self.holding(),
                                   capture=capture, bar=0.0)
        self.assertEqual(built.stance.mode, "chase")
        self.assertEqual({m.in_name for m in built.alternatives[0].plan.moves}, {"P27"})


class BriefTests(StanceTestCase):

    def evaluate(self):
        return brief.evaluate(self.conn, GAMEWEEK, now=NOW, include_token=False,
                              learnings_dir=__import__("pathlib").Path(self.tmp.name) / "l")

    def stance_row(self) -> str:
        return [line for line in brief.render_block(self.evaluate())
                if line.startswith("- **Stance:**")][0]

    def test_the_brief_states_the_mode_the_gap_and_the_effect_in_plain_english(self):
        self.conn.execute("UPDATE projection SET expected_points = 2.0 WHERE element_id = 23")
        self.conn.execute("UPDATE projection SET expected_points = 1.9 WHERE element_id = 27")
        self.rivals_hold(23, captain=True)
        self.standings(owner_rank=2, owner_points=50, leader_points=400)
        row = self.stance_row()
        self.assertIn("Chase - 2 of 2 in Test league, 350 points behind the leader", row)
        self.assertIn("Effect: on moves, without it the advice would be", row)
        self.assertIn("P23 in for", row)

    def test_leading_says_protect(self):
        self.rivals_hold(23)
        self.standings(owner_rank=1, owner_points=500)
        self.assertIn("Protect - first (1 of 2 in Test league)", self.stance_row())

    def test_unusable_ownership_is_stated_in_the_stance_line(self):
        self.standings(owner_rank=2, owner_points=150)
        self.assertIn("not applied - ownership is not usable", self.stance_row())

    def test_the_stance_is_stored_with_the_record(self):
        self.rivals_hold(23)
        self.standings(owner_rank=2, owner_points=150)
        built = self.evaluate().instructions
        again = instructions.Instructions.from_payload(built.to_payload())
        self.assertEqual(again.stance.to_dict(), built.stance.to_dict())
        self.assertEqual(again.effect, built.effect)


if __name__ == "__main__":
    unittest.main()

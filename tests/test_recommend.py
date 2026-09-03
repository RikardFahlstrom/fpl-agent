"""Recommender tests: squad rules, budget, and the decision log."""
import json
import tempfile
import unittest
from pathlib import Path

from fpl_agent.engine import recommend, storage
from test_scoring import WEIGHTS


def element(element_id, team, element_type, cost, xg=0.5, likelihood=0, percent=0.0,
            projected=None):
    return {
        "id": element_id, "web_name": f"P{element_id}", "first_name": "F",
        "second_name": f"S{element_id}", "team": team, "element_type": element_type,
        "now_cost": cost, "form": "5.0", "points_per_game": "5.0", "total_points": 40,
        "minutes": 900, "status": "a", "chance_of_playing_next_round": None,
        "selected_by_percent": "10.0", "expected_goals_per_90": str(xg),
        "expected_assists_per_90": "0.2", "expected_goals_conceded_per_90": "1.1",
        "starts_per_90": "1.0", "penalties_order": None, "news": "",
        "price_change_percent": str(percent), "price_change_locked_until": None,
        "price_change_projections": [
            {"offset": 0,
             "projected_percent": str(percent if projected is None else projected),
             "likelihood": likelihood}],
        "transfers_in_event": 1000, "transfers_out_event": 100,
    }


class RecommendTests(unittest.TestCase):
    def _seed(self, bank=10, squad_ids=(1,), elements=None):
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        elements = elements or [
            element(1, team=1, element_type=3, cost=50, xg=0.10),   # owned, weak
            element(2, team=2, element_type=3, cost=55, xg=0.90),   # strong upgrade
            element(3, team=2, element_type=4, cost=55, xg=0.90),   # wrong position
            element(4, team=3, element_type=3, cost=80, xg=0.95),   # too expensive
        ]
        bootstrap = {
            "elements": elements,
            "teams": [{"id": i, "name": f"Team{i}", "short_name": f"T{i}"} for i in (1, 2, 3)],
            "element_types": [{"id": 3, "singular_name_short": "MID"},
                              {"id": 4, "singular_name_short": "FWD"}],
            "events": [{"id": 3, "is_current": False, "is_next": True, "finished": False}],
            "game_settings": {},
            "game_config": {"scoring": WEIGHTS, "rules": {"squad_team_limit": 3}},
            "chips": [],
        }
        snapshot_id = storage.create_snapshot(conn, bootstrap)
        storage.upsert_teams(conn, bootstrap)
        storage.upsert_players(conn, bootstrap)
        storage.record_player_snapshot(conn, snapshot_id, bootstrap)
        storage.record_game_config(conn, bootstrap)
        storage.upsert_fixtures(conn, [
            {"id": gw, "event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 3,
             "team_a_difficulty": 3, "team_h_score": None, "team_a_score": None,
             "kickoff_time": None, "finished": False} for gw in (3, 4, 5)])
        storage.record_my_team(conn, snapshot_id, 1, {
            "picks": [{"element": eid, "position": i + 1, "multiplier": 1,
                       "is_captain": False, "is_vice_captain": False,
                       "selling_price": 50, "purchase_price": 50}
                      for i, eid in enumerate(squad_ids)],
            "transfers": {"bank": bank, "value": 1000, "limit": 1, "made": 0, "cost": 4},
            "chips": []})
        return conn

    def test_recommends_a_like_for_like_upgrade(self):
        conn = self._seed()
        results = recommend.recommend(conn, weeks=3)
        self.assertTrue(results)
        self.assertEqual(results[0]["in"]["element_id"], 2)
        self.assertEqual(results[0]["out"]["element_id"], 1)
        self.assertGreater(results[0]["xp_delta"], 0)

    def test_never_swaps_across_positions(self):
        """Squad structure is fixed, so a midfielder cannot be replaced by a forward."""
        conn = self._seed()
        ids = {r["in"]["element_id"] for r in recommend.recommend(conn, weeks=3, limit=50)}
        self.assertNotIn(3, ids)

    def test_respects_the_budget(self):
        conn = self._seed(bank=0)   # 50 selling + 0 bank cannot reach the 80 option
        ids = {r["in"]["element_id"] for r in recommend.recommend(conn, weeks=3, limit=50)}
        self.assertNotIn(4, ids)

    def test_respects_the_club_limit_from_game_config(self):
        """Three players from team 2 already, so a fourth must not be proposed.

        Swapping one team-2 player for another is still legal - the count stays at
        three - so the illegal move is specifically buying into team 2 while selling
        from a different club.
        """
        elements = [element(1, team=1, element_type=3, cost=50, xg=0.10),
                    element(5, team=2, element_type=3, cost=50, xg=0.10),
                    element(6, team=2, element_type=3, cost=50, xg=0.10),
                    element(7, team=2, element_type=3, cost=50, xg=0.10),
                    element(2, team=2, element_type=3, cost=55, xg=0.90)]
        conn = self._seed(squad_ids=(1, 5, 6, 7), elements=elements)
        results = recommend.recommend(conn, weeks=3, limit=50)

        illegal = [r for r in results
                   if r["in"]["element_id"] == 2 and r["out"]["element_id"] == 1]
        self.assertEqual(illegal, [], "buying a 4th team-2 player breaks squad_team_limit")

        same_club = [r for r in results
                     if r["in"]["element_id"] == 2 and r["out"]["element_id"] in (5, 6, 7)]
        self.assertTrue(same_club, "a same-club swap keeps the count at three and is legal")

    def test_only_positive_gains_are_offered(self):
        conn = self._seed()
        for r in recommend.recommend(conn, weeks=3, limit=50):
            self.assertGreater(r["xp_delta"], 0)

    def test_a_closing_window_is_flagged(self):
        """A target Very Likely to rise that the budget only just covers is urgent.

        Predicted Progress must exceed 100%: that is FPL's own threshold, and 96.4%
        progress with a 106% prediction is exactly the shape of a player rising tonight.
        """
        elements = [element(1, team=1, element_type=3, cost=50, xg=0.10),
                    element(2, team=2, element_type=3, cost=50, xg=0.90,
                            likelihood=5, percent=96.4, projected=106.0)]
        conn = self._seed(bank=0, elements=elements)   # budget exactly 50
        results = recommend.recommend(conn, weeks=3, limit=50)
        self.assertTrue(results)
        self.assertEqual(results[0]["urgency"], "tonight")
        self.assertIn("very likely to rise", results[0]["affordability"]["reason"])
        self.assertIn("predicted progress", results[0]["affordability"]["reason"])

    def test_unowned_candidates_are_differentials_not_unknown(self):
        """Regression: a player in nobody's squad was labelled unknown and lost.

        That is where the edge lives - 0% ownership in your league is the strongest
        differential there is.
        """
        conn = self._seed()
        conn.executemany(
            "INSERT OR REPLACE INTO rival_squad VALUES (?,?,?,?,?,?,?)",
            [(900 + i, 2, 1, 1, 1, 0, 0) for i in range(4)])  # rivals all own element 1
        conn.commit()

        results = recommend.recommend(conn, weeks=3, limit=50)
        incoming = {r["in"]["element_id"]: r["in"] for r in results}
        self.assertIn(2, incoming)
        self.assertEqual(incoming[2]["profile"], "differential")
        self.assertEqual(incoming[2]["league_eo"], 0.0)
        # and the owned player they are replacing reads as template
        self.assertEqual(results[0]["out"]["profile"], "template")

    def test_profiles_are_unknown_before_any_rivals_are_captured(self):
        conn = self._seed()
        results = recommend.recommend(conn, weeks=3, limit=50)
        self.assertEqual(results[0]["in"]["profile"], "unknown")
        self.assertIsNone(results[0]["in"]["league_eo"])

    def test_a_bench_upgrade_is_discounted_against_the_same_upgrade_in_the_xi(self):
        """A bench player only scores through substitutions, so the slot is worth less.

        Regression: the top six recommendations were all swaps for bench players,
        including a reserve goalkeeper, because every slot counted the same.
        """
        elements = [element(1, team=1, element_type=3, cost=50, xg=0.10),   # starter
                    element(5, team=1, element_type=3, cost=50, xg=0.10),   # bench
                    element(2, team=2, element_type=3, cost=50, xg=0.90)]
        conn = self._seed(squad_ids=(1, 5), elements=elements)
        # element 5 sits at squad position 2, so move it to the bench
        conn.execute("UPDATE my_squad SET position = 13 WHERE element_id = 5")
        conn.commit()

        results = recommend.recommend(conn, weeks=3, limit=50)
        by_out = {r["out"]["element_id"]: r for r in results}
        self.assertIn(1, by_out)
        self.assertIn(5, by_out)
        self.assertEqual(by_out[1]["out"]["slot"], "xi")
        self.assertEqual(by_out[5]["out"]["slot"], "bench")

        # the underlying projection gain is the same; only the slot differs
        self.assertAlmostEqual(by_out[1]["raw_xp_delta"], by_out[5]["raw_xp_delta"], places=2)
        self.assertLess(by_out[5]["xp_delta"], by_out[1]["xp_delta"])
        self.assertAlmostEqual(by_out[5]["xp_delta"],
                               by_out[1]["xp_delta"] * recommend.BENCH_VALUE, places=2)
        self.assertEqual(results[0]["out"]["slot"], "xi", "the XI upgrade must rank first")

    def test_requires_a_captured_squad(self):
        conn = self._seed(squad_ids=())
        with self.assertRaises(LookupError):
            recommend.recommend(conn, weeks=3)


class DecisionLogTests(unittest.TestCase):
    def test_decisions_round_trip_to_jsonl(self):
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        recommendation = {
            "gameweek": 3, "horizon": 3,
            "out": {"element_id": 1, "name": "Old", "selling_price": 50, "xp": 6.0},
            "in": {"element_id": 2, "name": "New", "team": "T2", "now_cost": 55, "xp": 12.0},
            "xp_delta": 6.0, "urgency": "tonight",
            "affordability": {"reason": "New is rising (likelihood 5)"},
        }
        decision_id = recommend.record_decision(conn, recommendation)
        self.assertEqual(decision_id, 1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "actions.jsonl"
            self.assertEqual(recommend.export_actions(conn, path), 1)
            entry = json.loads(path.read_text().strip())

        self.assertEqual(entry["kind"], "transfer")
        self.assertEqual(entry["urgency"], "tonight")
        self.assertEqual(entry["status"], "proposed")
        self.assertIn("New over Old", entry["rationale"])
        self.assertIn("rising", entry["rationale"])
        self.assertEqual(entry["payload"]["in"]["name"], "New")

    def test_export_is_append_only_in_effect(self):
        """Earlier lines must be untouched, so the git diff is a pure addition."""
        conn = storage.connect(":memory:")
        self.addCleanup(conn.close)
        base = {"gameweek": 3, "horizon": 3, "out": {"element_id": 1, "name": "A",
                "selling_price": 50, "xp": 1.0}, "in": {"element_id": 2, "name": "B",
                "team": "T", "now_cost": 50, "xp": 2.0}, "xp_delta": 1.0,
                "urgency": "none", "affordability": {"reason": "none"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "actions.jsonl"
            recommend.record_decision(conn, base)
            recommend.export_actions(conn, path)
            first = path.read_text().splitlines()

            recommend.record_decision(conn, {**base, "xp_delta": 2.0})
            recommend.export_actions(conn, path)
            second = path.read_text().splitlines()

        self.assertEqual(len(second), 2)
        self.assertEqual(second[0], first[0], "the existing line must not be rewritten")


if __name__ == "__main__":
    unittest.main()

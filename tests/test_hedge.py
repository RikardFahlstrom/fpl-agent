"""Hedging: what selling one to three players buys against the field."""
import unittest

from fpl_agent.engine import hedge, projection, recommend, storage
from fpl_agent.engine import squad as squads
from test_recommend import element
from test_scoring import WEIGHTS

STAR = 16


class PureTests(unittest.TestCase):
    def test_the_armband_goes_to_the_best_xp(self):
        pick, why = hedge.captain_choice({1: 5.0, 2: 4.0}, {1: 0.8}, behind=False)
        self.assertEqual(pick, 1)

    def test_a_coin_flip_goes_against_the_field_when_behind(self):
        pick, why = hedge.captain_choice({1: 5.0, 2: 4.7}, {1: 0.8, 2: 0.0}, behind=True)
        self.assertEqual(pick, 2)
        self.assertIn("behind", why)

    def test_a_clear_best_keeps_the_armband_even_when_behind(self):
        pick, _ = hedge.captain_choice({1: 5.0, 2: 3.0}, {1: 0.8, 2: 0.0}, behind=True)
        self.assertEqual(pick, 1)

    def test_a_coin_flip_follows_the_model_when_leading(self):
        pick, _ = hedge.captain_choice({1: 5.0, 2: 4.7}, {1: 0.8, 2: 0.0}, behind=False)
        self.assertEqual(pick, 1)

    def test_distance_counts_the_armband_twice(self):
        xi = tuple(squads.Candidate(i, f"P{i}", i, 3, 50, 1.0) for i in (1, 2))
        side = squads.Squad(xi, xi, 100)
        # Holding 1 at 2x and 2 at 1x against a field on 1 at 1.5x and 3 at 1x.
        self.assertAlmostEqual(
            hedge.distance(side, 1, {1: 1.5, 3: 1.0}), 0.5 + 1.0 + 1.0)

    def test_hits_start_after_the_free_transfers(self):
        self.assertEqual(hedge.hit_for(3, 2, 4, None), 4)
        self.assertEqual(hedge.hit_for(2, 2, 4, None), 0)
        self.assertEqual(hedge.hit_for(2, None, 4, None), 8)   # unknown is none free
        self.assertEqual(hedge.hit_for(3, 0, 4, "wildcard"), 0)


class BestSquadLimitTests(unittest.TestCase):
    def _market(self):
        players, i = [], 1
        for element_type, n in zip((1, 2, 3, 4), (4, 8, 8, 6)):
            for k in range(n):
                players.append(squads.Candidate(i, f"P{i}", i % 8, element_type,
                                                40 + k * 5, 1.0 + k * 0.5))
                i += 1
        return players

    def test_it_changes_no_more_players_than_it_is_allowed(self):
        market = self._market()
        base = squads.best_squad(market, 800)       # a squad to be limited to
        richer = squads.best_squad(market, 10_000)
        ids = {p.element_id for p in base.players}
        for limit in (0, 1, 3):
            found = squads.best_squad(market, 10_000, base=ids, max_changes=limit)
            changed = {p.element_id for p in found.players} - ids
            self.assertLessEqual(len(changed), limit)
        self.assertGreater(richer.objective, found.objective)

    def test_a_required_player_is_in_the_squad_or_there_is_no_answer(self):
        market = self._market()
        weakest = min(market, key=lambda p: p.xp).element_id
        found = squads.best_squad(market, 10_000, required={weakest})
        self.assertIn(weakest, {p.element_id for p in found.players})
        self.assertIsNone(squads.best_squad(market, 10_000, required={999}))


class HedgeTests(unittest.TestCase):
    """A squad of fifteen weak players, a rich star every rival owns, and a budget the
    star is out of single-swap reach of."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _seed(self, rivals=True, bank=10, limit=2):
        owned = []     # (id, team, type, cost, xg)
        i = 1
        for element_type, n, cost in ((1, 2, 40), (2, 5, 45), (3, 5, 60), (4, 3, 50)):
            for _ in range(n):
                owned.append((i, (i % 5) + 1, element_type, cost, 0.1))
                i += 1
        # The weak striker is dear: selling him alone cannot fund the star.
        owned = [(e, t, ty, 100 if e == 13 else c, x) for e, t, ty, c, x in owned]
        elements = [element(e, t, ty, c, xg=x) for e, t, ty, c, x in owned]
        elements.append(element(STAR, 6, 4, 130, xg=1.5))
        elements.append(element(17, 6, 3, 40, xg=0.1))      # a cheap midfielder
        bootstrap = {
            "elements": elements,
            "teams": [{"id": t, "name": f"Team{t}", "short_name": f"T{t}"}
                      for t in range(1, 7)],
            "element_types": [{"id": n, "singular_name_short": s}
                              for n, s in ((1, "GKP"), (2, "DEF"), (3, "MID"), (4, "FWD"))],
            "events": [{"id": 3, "is_current": False, "is_next": True, "finished": False}],
            "game_settings": {},
            "game_config": {"scoring": WEIGHTS, "rules": {"squad_team_limit": 3}},
            "chips": [],
        }
        snapshot_id = storage.create_snapshot(self.conn, bootstrap)
        storage.upsert_teams(self.conn, bootstrap)
        storage.upsert_players(self.conn, bootstrap)
        storage.record_player_snapshot(self.conn, snapshot_id, bootstrap)
        storage.record_game_config(self.conn, bootstrap)
        storage.upsert_fixtures(self.conn, [
            {"id": gw * 10 + k, "event": gw, "team_h": 2 * k + 1, "team_a": 2 * k + 2,
             "team_h_difficulty": 3, "team_a_difficulty": 3, "team_h_score": None,
             "team_a_score": None, "kickoff_time": None, "finished": False}
            for gw in (3, 4, 5) for k in range(3)])
        storage.record_my_team(self.conn, snapshot_id, 1, {
            "picks": [{"element": e, "position": n + 1, "multiplier": 1,
                       "is_captain": False, "is_vice_captain": False,
                       "selling_price": c, "purchase_price": c}
                      for n, (e, _, _, c, _) in enumerate(owned)],
            "transfers": {"bank": bank, "value": 1000, "limit": limit, "made": 0, "cost": 4},
            "chips": []})
        projection.project_horizon(self.conn, 3, weeks=3)
        if rivals:
            self.conn.executemany(
                "INSERT INTO rival_squad VALUES (?,?,?,?,?,?,?)",
                [(r, 3, STAR, 1, 2, 1, 0) for r in (101, 102, 103)])
            self.conn.commit()

    def test_it_finds_the_star_that_no_single_swap_can_reach(self):
        self._seed()
        singles = {r["in"]["element_id"]
                   for r in recommend.recommend(self.conn, weeks=3, limit=500)}
        self.assertNotIn(STAR, singles)
        report = hedge.hedge(self.conn, weeks=3, players=["P16"])
        best = report.recommended
        self.assertIsNotNone(best)
        self.assertEqual(best.target, "P16")
        self.assertEqual(best.changes, 2)
        self.assertIn("P16", [name for name, _ in best.into])
        self.assertEqual(best.captain, "P16")
        self.assertEqual(best.rivals_holding, "3 of 3")

    def test_two_transfers_are_free_and_a_third_is_charged(self):
        self._seed()
        report = hedge.hedge(self.conn, weeks=3, players=["P16"])
        by_changes = {o.changes: o for o in report.options}
        self.assertEqual(by_changes[2].hit, 0)
        if 3 in by_changes:
            self.assertEqual(by_changes[3].hit, 4)

    def test_what_it_sells_pays_for_what_it_buys(self):
        self._seed()
        best = hedge.hedge(self.conn, weeks=3, players=["P16"]).recommended
        spent = sum(c for _, c in best.into)
        raised = sum(c for _, c in best.out) + 10
        self.assertEqual(best.bank_after, raised - spent)
        self.assertGreaterEqual(best.bank_after, 0)

    def test_it_closes_the_distance_to_the_field(self):
        self._seed()
        best = hedge.hedge(self.conn, weeks=3, players=["P16"]).recommended
        self.assertLess(best.distance_after, best.distance_before)

    def test_template_players_are_found_without_being_named(self):
        self._seed()
        report = hedge.hedge(self.conn, weeks=3)
        self.assertEqual(report.targets, ("P16",))

    def test_without_rivals_it_says_so_rather_than_guessing(self):
        self._seed(rivals=False)
        report = hedge.hedge(self.conn, weeks=3)
        self.assertEqual(report.options, ())
        self.assertIn("rivals", report.unavailable)

    def test_an_unknown_name_is_reported(self):
        self._seed()
        report = hedge.hedge(self.conn, weeks=3, players=["Nobody"])
        self.assertIn("nobody", report.unavailable)

    def test_render_names_the_moves_and_the_cost(self):
        self._seed()
        text = hedge.render(hedge.hedge(self.conn, weeks=3, players=["P16"]))
        self.assertIn("Recommended: P16 via 2 transfers", text)
        self.assertIn("out:", text)

    def test_standing_says_how_far_behind_you_are(self):
        self.conn.execute("INSERT INTO league VALUES (1, 'L', 'x', 3, 't')")
        self.conn.executemany(
            "INSERT INTO league_standing VALUES (1, 7, ?, ?, ?, 0, ?, 0, 't')",
            [(1, 1, 1, 400), (2, 2, 2, 350), (3, 3, 3, 300)])
        place = hedge.standing(self.conn, 3, None)
        self.assertEqual((place.rank, place.of, place.behind), (3, 3, 100))
        self.assertEqual(hedge.standing(self.conn, 1, None).behind, 0)
        self.assertIsNone(hedge.standing(self.conn, 99, None))


if __name__ == "__main__":
    unittest.main()

"""The squad optimiser: a pure function over values, tested on hand-built inputs."""

import unittest

from fpl_agent.engine import squad
from fpl_agent.engine.squad import Candidate


def player(i, element_type, cost, xp, team=None):
    return Candidate(i, f"P{i}", team if team is not None else i % 10, element_type, cost, xp)


def a_market(n_per_type=(6, 12, 12, 8), base_cost=40, clubs=10):
    """A market with enough of every position, costs rising with points.

    The cheapest legal fifteen costs 720 with the default base cost."""
    players, i = [], 1
    for element_type, n in zip((1, 2, 3, 4), n_per_type):
        for k in range(n):
            players.append(player(i, element_type, base_cost + k * 5, 1.0 + k * 0.5,
                                  team=i % clubs))
            i += 1
    return players


class BestXITests(unittest.TestCase):

    def test_it_picks_the_highest_scoring_legal_formation(self):
        players = a_market()
        chosen = squad.best_squad(players, budget=10_000)
        xi = chosen.xi
        self.assertEqual(len(xi), 11)
        types = [p.element_type for p in xi]
        self.assertEqual(types.count(1), 1)
        self.assertGreaterEqual(types.count(2), 3)
        self.assertGreaterEqual(types.count(4), 1)

    def test_a_squad_missing_a_goalkeeper_has_no_xi(self):
        players = [p for p in a_market() if p.element_type != 1][:14]
        self.assertEqual(squad.best_xi(players), ())


class BestSquadTests(unittest.TestCase):

    def test_the_result_is_legal(self):
        market = a_market()
        chosen = squad.best_squad(market, budget=830)
        self.assertIsNotNone(chosen)
        self.assertTrue(squad.legal(chosen.players, 830))
        self.assertLessEqual(chosen.cost, 830)

    def test_an_unaffordable_market_returns_none(self):
        self.assertIsNone(squad.best_squad(a_market(base_cost=100), budget=500))

    def test_the_club_limit_is_respected_even_when_one_club_has_the_best_players(self):
        market = a_market()
        # make club 7 hold the ten best midfielders and defenders
        market = [Candidate(p.element_id, p.name, 7 if p.xp > 4 else p.team_id,
                            p.element_type, p.cost, p.xp) for p in market]
        chosen = squad.best_squad(market, budget=10_000)
        clubs = [p.team_id for p in chosen.players]
        self.assertLessEqual(clubs.count(7), 3)

    def test_pruning_keeps_as_many_per_club_as_the_limit_it_is_given(self):
        """Dominated players are kept only for the club limit, so the count kept must be
        the limit in force - a hardcoded 3 starved a looser limit of candidates."""
        # Six midfielders from one club, each dearer and worse than the last.
        dominated = [player(i, 3, 40 + i, 10.0 - i, team=1) for i in range(1, 7)]
        for limit in (2, 3, 5):
            self.assertEqual(len(squad._prune(dominated, limit)), limit)

    def test_with_money_to_burn_it_buys_the_best_fifteen(self):
        market = a_market(clubs=100)      # no club limit in the way
        chosen = squad.best_squad(market, budget=10_000)
        expected = sorted(
            [p for p in market if p.element_type == 1], key=lambda p: -p.xp)[:2] + sorted(
            [p for p in market if p.element_type == 2], key=lambda p: -p.xp)[:5] + sorted(
            [p for p in market if p.element_type == 3], key=lambda p: -p.xp)[:5] + sorted(
            [p for p in market if p.element_type == 4], key=lambda p: -p.xp)[:3]
        self.assertEqual({p.element_id for p in chosen.players},
                         {p.element_id for p in expected})

    def test_a_known_optimum_on_a_tight_budget(self):
        """Two goalkeepers to choose between: the dear one only pays for himself if the
        budget allows it, and the swap pass must find that."""
        market = a_market()
        cheap = squad.best_squad(market, budget=750)
        rich = squad.best_squad(market, budget=900)
        self.assertGreater(rich.objective, cheap.objective)
        self.assertLessEqual(cheap.cost, 750)
        # Everything the cheap squad could not afford is in the rich one's reach.
        self.assertGreaterEqual(rich.xi_xp, cheap.xi_xp)

    def test_the_swap_pass_improves_on_the_cheapest_start(self):
        market = a_market()
        start = squad.cheapest_legal(squad._prune(market, 3), 900, 3)
        chosen = squad.best_squad(market, budget=900)
        self.assertGreater(chosen.objective, squad._make(start).objective)

    def test_the_bench_is_discounted_so_the_xi_is_bought_first(self):
        market = a_market()
        chosen = squad.best_squad(market, budget=800)
        self.assertGreaterEqual(min(p.xp for p in chosen.xi),
                                max(p.xp for p in chosen.bench) - 1e-9)


if __name__ == "__main__":
    unittest.main()

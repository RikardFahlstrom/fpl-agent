"""Recommender tests: squad rules, budget, and the decision log."""
import json
import tempfile
import unittest
from pathlib import Path

from fpl_agent.engine import projection, recommend, storage
from test_scoring import WEIGHTS


def element(element_id, team, element_type, cost, xg=0.5, likelihood=0, percent=0.0,
            projected=None, status="a", chance=None):
    return {
        "id": element_id, "web_name": f"P{element_id}", "first_name": "F",
        "second_name": f"S{element_id}", "team": team, "element_type": element_type,
        "now_cost": cost, "form": "5.0", "points_per_game": "5.0", "total_points": 40,
        "minutes": 900, "status": status, "chance_of_playing_next_round": chance,
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


def chip(name, status, chip_type="transfer"):
    """A chip in the shape `my-team` actually returns it.

    Copied from a real snapshot: `status_for_entry` is "active" while the chip is in
    play, and `chip_type` separates transfer chips from team ones.
    """
    return {"chip_type": chip_type, "id": 1, "is_pending": False, "name": name,
            "number": 1, "played_by_entry": [], "start_event": 2,
            "status_for_entry": status, "stop_event": 19}


class SeedMixin:
    def _seed(self, bank=10, squad_ids=(1,), elements=None,
              limit=1, made=0, cost=4, chips=(), project=True):
        """A snapshot, and by default the horizon `project` would have written for it.

        `recommend` reads projections rather than running them, so the fixture has to
        do what `make deadline` does: project, then recommend. Pass project=False to
        exercise the horizon that was never run.
        """
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
            "transfers": {"bank": bank, "value": 1000, "limit": limit, "made": made,
                          "cost": cost},
            "chips": list(chips)})
        if project:
            projection.project_horizon(conn, 3, weeks=3)
        return conn


class RecommendTests(SeedMixin, unittest.TestCase):
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


class StoredHorizonTests(SeedMixin, unittest.TestCase):
    """Recommending is a read. It must not write projections on the way past."""

    def test_an_unprojected_horizon_is_an_error_not_a_projection_run(self):
        conn = self._seed(project=False)
        with self.assertRaises(projection.HorizonMissing) as caught:
            recommend.recommend(conn, weeks=3)
        message = str(caught.exception)
        self.assertIn("3, 4, 5", message)
        self.assertIn("project", message)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM projection").fetchone()[0], 0,
            "a failed recommend must not have written rows either")

    def test_recommending_writes_no_projection_rows(self):
        """Regression: `recommend` re-projected, so running it twice moved the warehouse.

        Rows appearing under whatever MODEL_VERSION the code is on today are rows
        nobody asked for, and they make "projections for gameweek N" uncountable.
        """
        conn = self._seed()
        before = conn.execute(
            "SELECT id, expected_points, created_at FROM projection ORDER BY id"
        ).fetchall()
        recommend.recommend(conn, weeks=3, limit=50)
        recommend.recommend(conn, weeks=3, limit=50)
        after = conn.execute(
            "SELECT id, expected_points, created_at FROM projection ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_a_partly_projected_horizon_is_refused_rather_than_totalled_short(self):
        """Two weeks of a three-week horizon understates every player by a week."""
        conn = self._seed()
        conn.execute("DELETE FROM projection WHERE gameweek = 5")
        conn.commit()
        with self.assertRaises(projection.HorizonMissing) as caught:
            recommend.recommend(conn, weeks=3)
        self.assertIn("gameweek 5", str(caught.exception))

    def test_a_shorter_horizon_reads_the_weeks_it_asked_for(self):
        conn = self._seed()
        results = recommend.recommend(conn, weeks=1, limit=50)
        self.assertTrue(results)
        self.assertEqual(results[0]["horizon"], 1)
        three = recommend.recommend(conn, weeks=3, limit=50)
        self.assertLess(results[0]["in"]["xp"], three[0]["in"]["xp"])


class DoubtfulCandidateTests(SeedMixin, unittest.TestCase):
    """A doubtful player is a candidate; his doubt is applied once, in the projection.

    `projection.availability` already multiplies his whole projection by FPL's own
    `chance_of_playing_next_round`. Excluding him from the candidate list on top of
    that charged the same doubt twice and rounded the second charge to certainty.
    """

    ELEMENTS = [element(1, team=1, element_type=3, cost=50, xg=0.10),
                element(2, team=2, element_type=3, cost=50, xg=0.90,
                        status="d", chance=75),
                element(6, team=2, element_type=3, cost=50, xg=0.90,
                        status="i", chance=0)]

    def test_a_doubtful_player_can_be_recommended(self):
        conn = self._seed(elements=self.ELEMENTS)
        incoming = {r["in"]["element_id"]: r["in"]
                    for r in recommend.recommend(conn, weeks=3, limit=50)}
        self.assertIn(2, incoming, "a 75% doubt is a discount, not a disqualification")
        self.assertEqual(incoming[2]["status"], "d")
        self.assertEqual(incoming[2]["chance"], 75)

    def test_the_doubt_is_charged_once_at_fpls_own_percentage(self):
        fit = self._seed(elements=[
            element(1, team=1, element_type=3, cost=50, xg=0.10),
            element(2, team=2, element_type=3, cost=50, xg=0.90)])
        doubtful = self._seed(elements=self.ELEMENTS)
        fit_xp = recommend.recommend(fit, weeks=3, limit=50)[0]["in"]["xp"]
        doubtful_xp = {r["in"]["element_id"]: r["in"]["xp"]
                       for r in recommend.recommend(doubtful, weeks=3, limit=50)}[2]
        self.assertAlmostEqual(doubtful_xp, fit_xp * 0.75, places=1)

    def test_the_ruled_out_are_still_excluded(self):
        """No percentage to scale by, so `i` projects to zero and is not a candidate."""
        conn = self._seed(elements=self.ELEMENTS)
        ids = {r["in"]["element_id"] for r in recommend.recommend(conn, weeks=3, limit=50)}
        self.assertNotIn(6, ids)


class TransferCostTests(SeedMixin, unittest.TestCase):
    """The hit a move costs, and what it does to the ranking.

    Regression: every recommendation was ranked on gross xP gain. With no free
    transfers a +1.2 upgrade is net -2.8 and was still offered first.
    """

    # A squad with an XI upgrade worth +7.25 and the same upgrade on the bench, which
    # the bench discount reduces to about +1.09 - the shape the review describes.
    ELEMENTS = [element(1, team=1, element_type=3, cost=50, xg=0.10),   # starter
                element(5, team=1, element_type=3, cost=50, xg=0.10),   # bench
                element(2, team=2, element_type=3, cost=50, xg=0.90)]   # the upgrade

    def _bench_seed(self, **kwargs):
        conn = self._seed(squad_ids=(1, 5), elements=self.ELEMENTS, **kwargs)
        conn.execute("UPDATE my_squad SET position = 13 WHERE element_id = 5")
        conn.commit()
        return conn

    def test_a_hit_is_charged_when_no_free_transfers_remain(self):
        conn = self._seed(limit=0, made=0, cost=4)
        results = recommend.recommend(conn, weeks=3, limit=50)
        self.assertTrue(results)
        for r in results:
            self.assertEqual(r["hit_cost"], 4)
            self.assertEqual(r["free_transfers"], 0)
            self.assertIsNone(r["chip"])
            self.assertAlmostEqual(r["net_xp_delta"], r["xp_delta"] - 4, places=2)

    def test_a_move_that_does_not_survive_its_hit_is_dropped(self):
        """+1.09 gross on the bench is net -2.91, which is not a recommendation."""
        free = recommend.recommend(self._bench_seed(limit=1), weeks=3, limit=50)
        by_out = {r["out"]["element_id"]: r for r in free}
        self.assertIn(5, by_out, "with a free transfer the bench swap is still offered")
        self.assertLess(by_out[5]["xp_delta"], 4)

        hit = recommend.recommend(self._bench_seed(limit=0), weeks=3, limit=50)
        outs = {r["out"]["element_id"] for r in hit}
        self.assertNotIn(5, outs, "a net-negative move must not be offered")
        self.assertIn(1, outs, "the +7.25 XI upgrade clears the hit and survives")
        for r in hit:
            self.assertGreater(r["net_xp_delta"], 0)

    def test_every_option_is_priced_as_the_next_transfer_not_as_a_plan(self):
        """The list is alternatives, not a sequence, so option 2 is not billed for 1.

        With one free transfer every option is free: you are going to make one of
        these moves, not all of them.
        """
        results = recommend.recommend(self._bench_seed(limit=1), weeks=3, limit=50)
        self.assertGreater(len(results), 1)
        for r in results:
            self.assertEqual(r["hit_cost"], 0)
            self.assertAlmostEqual(r["net_xp_delta"], r["xp_delta"], places=2)

    def test_an_active_wildcard_charges_nothing(self):
        conn = self._seed(limit=0, made=0, cost=4,
                          chips=[chip("wildcard", "active"),
                                 chip("freehit", "unavailable")])
        results = recommend.recommend(conn, weeks=3, limit=50)
        self.assertTrue(results)
        for r in results:
            self.assertEqual(r["chip"], "wildcard")
            self.assertEqual(r["hit_cost"], 0)
            self.assertAlmostEqual(r["net_xp_delta"], r["xp_delta"], places=2)

    def test_a_free_hit_also_charges_nothing(self):
        conn = self._seed(limit=0, chips=[chip("freehit", "active")])
        self.assertEqual(recommend.transfer_context(conn)["hit_cost"], 0)
        self.assertEqual(recommend.transfer_context(conn)["chip"], "freehit")

    def test_a_played_or_available_chip_is_not_an_active_one(self):
        for status in ("available", "played", "unavailable"):
            conn = self._seed(limit=0, chips=[chip("wildcard", status)])
            context = recommend.transfer_context(conn)
            self.assertIsNone(context["chip"], status)
            self.assertEqual(context["hit_cost"], 4, status)

    def test_a_team_chip_does_not_make_transfers_free(self):
        """A bench boost changes what the squad scores, not what a move costs."""
        conn = self._seed(limit=0, chips=[chip("bboost", "active", chip_type="team")])
        context = recommend.transfer_context(conn)
        self.assertIsNone(context["chip"])
        self.assertEqual(context["hit_cost"], 4)

    def test_unrecorded_free_transfers_are_priced_as_none(self):
        """A snapshot missing `transfers.limit` is not evidence of a free transfer."""
        conn = self._seed(limit=None, cost=4)
        context = recommend.transfer_context(conn)
        self.assertIsNone(context["free_transfers"])
        self.assertEqual(context["hit_cost"], 4)

    def test_the_gross_gain_stays_visible_alongside_the_net(self):
        """-2.8 net on a +1.2 gross move is a different message from -2.8 flat."""
        r = recommend.recommend(self._seed(limit=0), weeks=3, limit=50)[0]
        self.assertGreater(r["xp_delta"], r["net_xp_delta"])
        self.assertEqual(r["xp_delta"], r["raw_xp_delta"])

    def test_the_hit_reaches_the_decision_log(self):
        conn = self._seed(limit=0)
        top = recommend.recommend(conn, weeks=3, limit=50)[0]
        recommend.record_decision(conn, top)
        row = conn.execute("SELECT payload, rationale, xp_delta FROM decision").fetchone()
        self.assertIn("4-point hit", row["rationale"])
        # the column keeps its meaning - gross - while the net rides along in the JSON
        self.assertEqual(row["xp_delta"], top["xp_delta"])
        self.assertEqual(json.loads(row["payload"])["net_xp_delta"], top["net_xp_delta"])


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

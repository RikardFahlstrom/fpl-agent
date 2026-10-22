"""The gameweek brief.

Offline: every warehouse here is built in memory and every file is written to a temporary
directory. Nothing in this file may touch `data/fpl.db`, the real `logs/`, or the token
cache - the status check the brief calls is pointed at a temporary cache through
FPL_TOKEN_CACHE, which is the knob `headless_auth.cache_path` reads in production.

The opening block is the contract: the same lines in the same order every run, each
saying "none" or "not evaluated" rather than going missing.
"""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fpl_agent.engine import brief, storage
from fpl_agent.engine import status as status_module
from fpl_agent.engine.projection import MODEL_VERSION

# The default warehouse kicks off at 09:00 on 5 September, so the derived deadline is
# 07:30 that morning - 19.5 hours after NOW. Tests that want another distance to the
# deadline move `now`, not the fixtures.
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
GAMEWEEK = 3


class Warehouse:
    """A warehouse the brief reads clean, with one lever per state worth testing.

    Built rather than fixtured because every trigger is a statement about a *combination*
    of rows - a deadline, a chip, a squad and a projected horizon - and a fixture file
    hides which of them a given test is actually moving.
    """

    def __init__(self, conn):
        self.conn = conn
        self.snapshot_id = None

    # -- the pieces -------------------------------------------------------
    # 20 clubs, so `recommend`'s three-per-club limit is never the reason a candidate is
    # dropped. With three clubs every swap was refused and every "does the threshold
    # work" test passed for the wrong reason.
    CLUBS = 20

    def teams(self):
        for i in range(1, self.CLUBS + 1):
            self.conn.execute("INSERT OR REPLACE INTO team VALUES (?,?,?,'{}')",
                              (i, f"Club {i}", f"C{i:02d}"))

    def players(self, n=40):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO player VALUES (?,?,?,?,?,?,'t','t')",
                (element_id, f"P{element_id}", "First", f"Last{element_id}",
                 1 + (element_id - 1) % self.CLUBS, 1 + (element_id - 1) % 4))

    def game_config(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO game_config VALUES ('t','{}',?,'{}','[]')",
            ('{"squad_team_limit": 3}',))

    def snapshot(self, gameweek=GAMEWEEK, captured_at="2026-09-04T09:00:00+00:00"):
        self.conn.execute(
            "INSERT INTO snapshot (captured_at, gameweek, kind) VALUES (?,?,'test')",
            (captured_at, gameweek))
        self.snapshot_id = self.conn.execute(
            "SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]
        return self.snapshot_id

    def player_snapshots(self, n=40, status="a", chance=None, news=""):
        for element_id in range(1, n + 1):
            self.conn.execute(
                """INSERT OR REPLACE INTO player_snapshot
                   (snapshot_id, element_id, now_cost, status,
                    chance_of_playing_next_round, news, price_change_percent,
                    price_change_projections, transfers_in_event, transfers_out_event,
                    raw)
                   VALUES (?,?,?,?,?,?,0.0,'[]',0,0,'{}')""",
                (self.snapshot_id, element_id, 50, status, chance, news))

    def flag(self, element_id, status="i", chance=None, news="Knee injury"):
        self.conn.execute(
            """UPDATE player_snapshot SET status = ?, chance_of_playing_next_round = ?,
               news = ? WHERE snapshot_id = ? AND element_id = ?""",
            (status, chance, news, self.snapshot_id, element_id))

    def falling(self, element_id, projected=-120.0):
        self.conn.execute(
            """UPDATE player_snapshot SET price_change_projections = ?
               WHERE snapshot_id = ? AND element_id = ?""",
            (f'[{{"offset": 0, "projected_percent": {projected}, "likelihood": -5}}]',
             self.snapshot_id, element_id))

    def squad(self, element_ids=None):
        """15 players, positions 1-15. Positions 1-11 are the XI."""
        element_ids = element_ids or list(range(1, 16))
        for position, element_id in enumerate(element_ids, start=1):
            self.conn.execute(
                "INSERT OR REPLACE INTO my_squad VALUES (?,?,?,1,0,0,50,50)",
                (self.snapshot_id, element_id, position))

    def state(self, bank=10, free_transfers=1, transfer_cost=4, chips="[]"):
        self.conn.execute(
            "INSERT OR REPLACE INTO my_state VALUES (?,1,?,1000,?,?,?)",
            (self.snapshot_id, bank, free_transfers, transfer_cost, chips))

    def wildcard(self):
        self.state(chips='[{"chip_type": "transfer", "name": "wildcard", '
                         '"status_for_entry": "active"}]')

    def projections(self, gameweek=GAMEWEEK, weeks=3, n=40, better=(),
                    model_version=MODEL_VERSION):
        """Everyone projects 1.0 a gameweek; `better` players project 2.0.

        Over a three-gameweek horizon that is a 3.0 gross gain for swapping any ordinary
        player for a `better` one - comfortably over the 2.0 bar, which is what makes the
        threshold tests about the threshold rather than about the arithmetic.
        """
        for gw in range(gameweek, gameweek + weeks):
            for element_id in range(1, n + 1):
                self.conn.execute(
                    """INSERT OR REPLACE INTO projection
                       (snapshot_id, gameweek, element_id, model_version,
                        expected_points, p_start, expected_minutes, fixture_count,
                        components, created_at)
                       VALUES (?,?,?,?,?,0.9,80,1,'{}','t')""",
                    (self.snapshot_id, gw, element_id, model_version,
                     2.0 if element_id in better else 1.0))

    def fixtures(self, gameweek=GAMEWEEK, kickoff="2026-09-05T09:00:00Z",
                 finished=False, count=2):
        for i in range(count):
            self.conn.execute(
                "INSERT OR REPLACE INTO fixture VALUES (?,?,1,2,3,3,NULL,NULL,?,?,'{}')",
                (gameweek * 100 + i, gameweek, kickoff, 1 if finished else 0))

    def lineups(self, gameweek=GAMEWEEK, n=40, snapshot_id=None):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO predicted_lineup "
                "VALUES (?,?,?,'ARS','rotowire','M',1,NULL,0)",
                (snapshot_id or self.snapshot_id, gameweek, element_id))

    def lineup_out(self, element_id, gameweek=GAMEWEEK, code="OUT"):
        self.conn.execute(
            "INSERT OR REPLACE INTO predicted_lineup "
            "VALUES (?,?,?,'ARS','rotowire','M',0,?,0)",
            (self.snapshot_id, gameweek, element_id, code))

    # -- the whole thing --------------------------------------------------
    def healthy(self, better=(16, 17, 18)):
        """A warehouse `status` calls clean and `recommend` can rank."""
        self.teams()
        self.players()
        self.game_config()
        self.snapshot()
        self.player_snapshots()
        self.squad()
        self.state()
        self.projections(better=better)
        self.fixtures()
        self.lineups()
        self.conn.execute(
            "INSERT OR REPLACE INTO rival_squad VALUES (99, 2, 1, 1, 1, 0, 0)")
        self.conn.commit()
        return self


class BriefTestCase(unittest.TestCase):
    """A fresh in-memory warehouse and a token cache that is never the real one."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.warehouse = Warehouse(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        patch = mock.patch.dict(
            os.environ,
            {"FPL_TOKEN_CACHE": str(Path(self.tmp.name) / "token.json"),
             "FPL_RIVAL_LEAGUES": ""},
        )
        patch.start()
        self.addCleanup(patch.stop)

    def evaluate(self, **kwargs):
        kwargs.setdefault("now", NOW)
        # Never the repo's own learnings/: the suite runs from the repo root.
        kwargs.setdefault("learnings_dir", Path(self.tmp.name) / "learnings")
        return brief.evaluate(self.conn, GAMEWEEK, **kwargs)


class DecisionContextTests(BriefTestCase):
    def test_gameweek_references_include_the_stockholm_deadline_in_both_outputs(self):
        self.warehouse.healthy()
        evaluation = self.evaluate()
        for text in (brief.render_brief(evaluation),
                     "\n".join(brief.render_block(evaluation, markdown=False))):
            self.assertIn("GW3 (deadline 5 Sep 09:30)", text)

    def test_the_previous_round_is_explicitly_unsettled(self):
        self.warehouse.healthy()
        evaluation = self.evaluate()
        text = "\n".join(brief.render_block(evaluation))
        self.assertIn("not settled", text)
        self.assertIn("league analysis pending", text)

    def test_a_settled_round_reports_the_owners_league_movement(self):
        self.warehouse.healthy()
        self.warehouse.fixtures(gameweek=2, finished=True)
        self.conn.execute(
            "INSERT INTO outcome VALUES (1,1,2,?,5.0,2.0,3.0,0.9,50,3,'t')",
            (MODEL_VERSION,))
        self.conn.execute("INSERT INTO league VALUES (100,'The inner','x',6,'t')")
        self.conn.executemany(
            "INSERT INTO league_standing VALUES (100,2,?,?,?,?,?,1,'t')",
            [(99, 1, 1, 50, 210), (888, 2, 4, 62, 194)])
        self.conn.execute("UPDATE my_state SET entry_id=888")
        evaluation = self.evaluate()
        for text in (brief.render_brief(evaluation),
                     "\n".join(brief.render_block(evaluation, markdown=False))):
            self.assertIn("settled", text)
            self.assertIn("The inner: 4 → 2 (up 2)", text)
            self.assertIn("62 points", text)
            self.assertIn("16 points behind the leader", text)

    def test_provisional_standings_are_not_reported_as_a_final_league_result(self):
        self.warehouse.healthy()
        self.warehouse.fixtures(gameweek=2, finished=True)
        self.conn.execute(
            "INSERT INTO outcome VALUES (1,1,2,?,5.0,2.0,3.0,0.9,50,3,'t')",
            (MODEL_VERSION,))
        self.conn.execute("INSERT INTO league VALUES (100,'The inner','x',6,'t')")
        self.conn.execute(
            "INSERT INTO league_standing VALUES (100,2,888,2,4,62,194,0,'t')")
        self.conn.execute("UPDATE my_state SET entry_id=888")
        text = "\n".join(brief.render_block(self.evaluate()))
        self.assertIn("settled", text)
        self.assertIn("final standings unavailable", text)
        self.assertNotIn("up 2", text)


# --------------------------------------------------------------------------
# The seam: names, shapes, and the rule that an action is never empty
# --------------------------------------------------------------------------

class BriefPathTests(unittest.TestCase):

    def test_gwnn_is_zero_padded_so_a_listing_sorts(self):
        self.assertEqual(brief.brief_path(3), Path("logs/gw03.md"))
        self.assertEqual(brief.brief_path(10), Path("logs/gw10.md"))

    def test_the_root_is_overridable(self):
        self.assertEqual(brief.brief_path(3, Path("/tmp/x")), Path("/tmp/x/gw03.md"))


class ThresholdTests(unittest.TestCase):

    def test_the_default_is_the_stated_constant(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(brief.worth_making_threshold(), brief.WORTH_MAKING_NET_XP)

    def test_the_environment_overrides_it(self):
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "3.5"}):
            self.assertEqual(brief.worth_making_threshold(), 3.5)

    def test_an_unreadable_override_falls_back_rather_than_failing_the_run(self):
        """A brief that dies on a typo in the ini tells the owner nothing at all."""
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "soon"}):
            self.assertEqual(brief.worth_making_threshold(), brief.WORTH_MAKING_NET_XP)


class MoveLineTests(BriefTestCase):
    """The opening block's Move line says whether the best move clears the bar."""

    def setUp(self):
        super().setUp()
        self.warehouse.healthy()

    def block(self, **kwargs) -> str:
        return "\n".join(brief.render_block(self.evaluate(**kwargs)))

    def test_a_move_over_the_bar_says_it_clears_it(self):
        self.assertIn("clears the 2.0 bar", self.block())

    def test_topping_the_list_is_not_enough(self):
        self.assertIn("under the 100.0 bar", self.block(min_net_xp=100))

    def test_the_threshold_is_overridable_from_the_environment(self):
        with mock.patch.dict(os.environ, {brief.MIN_NET_XP_ENV: "100"}):
            self.assertIn("under the 100.0 bar", self.block())

    def test_an_active_wildcard_is_said_beside_the_move(self):
        self.warehouse.wildcard()
        self.conn.commit()
        self.assertIn("wildcard active: single swaps, not a rebuild", self.block())

    def test_no_horizon_projected_is_a_sentence_not_a_traceback(self):
        self.conn.execute("DELETE FROM projection WHERE gameweek > ?", (GAMEWEEK,))
        self.conn.commit()
        move = [line for line in brief.render_block(self.evaluate())
                if line.startswith("- **Move:**")][0]
        self.assertIn("none - ", move)


class LockedGameweekTests(BriefTestCase):
    """After the deadline nothing is advised, and the page says why and until when."""

    LATE = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)    # deadline was 07:30

    def setUp(self):
        super().setUp()
        self.warehouse.healthy()

    def test_nothing_is_evaluated_and_the_block_says_so(self):
        evaluation = self.evaluate(now=self.LATE)
        self.assertIn("is locked", evaluation.locked)
        self.assertEqual(evaluation.listing["moves"], [])
        self.assertEqual(evaluation.chips, [])
        self.assertEqual(evaluation.captain_picks, [])
        text = brief.render_brief(evaluation)
        self.assertIn("> **GAMEWEEK 3 LOCKED.**", text)
        self.assertIn("- **Captain:** not evaluated - gameweek 3 (deadline 5 Sep 09:30) is locked", text)
        self.assertIn("- **Chips:** not evaluated - gameweek 3 (deadline 5 Sep 09:30) is locked", text)
        self.assertIn("rolls over to gameweek 4", text)

    def test_before_the_deadline_it_is_not_locked(self):
        self.assertIsNone(self.evaluate().locked)

    def test_a_far_deadline_says_lineups_are_not_yet_firm(self):
        text = brief.render_brief(self.evaluate(
            now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)))
        self.assertIn("predicted lineups not yet firm", text)
        self.assertNotIn("predicted lineups not yet firm", brief.render_brief(self.evaluate()))


class AnotherGameweekTests(BriefTestCase):
    """`brief --gameweek N` for N other than the target: transfers are not priced.

    It used to rank the target's moves under N's heading, so a brief for gameweek 4
    offered gameweek 3's transfers as if they were gameweek 4's.
    """

    def setUp(self):
        super().setUp()
        self.warehouse.healthy()

    def test_no_move_is_offered_and_the_brief_says_why(self):
        evaluation = brief.evaluate(self.conn, GAMEWEEK + 1, now=NOW,
                                    learnings_dir=Path(self.tmp.name) / "learnings")
        self.assertEqual(evaluation.listing["moves"], [])
        self.assertIn(f"targets gameweek {GAMEWEEK}", evaluation.listing["reason"])

    def test_the_target_itself_is_still_priced(self):
        self.assertTrue(self.evaluate().listing["moves"])


# --------------------------------------------------------------------------
# The written brief
# --------------------------------------------------------------------------

class RenderBriefTests(BriefTestCase):

    def render(self, **kwargs):
        return brief.render_brief(self.evaluate(**kwargs))

    def test_the_wildcard_banner_comes_before_anything_it_would_change(self):
        """It re-reads every recommendation under it, so a reader who scrolls past it has
        been misled by the layout rather than by the numbers."""
        self.warehouse.healthy()
        self.warehouse.wildcard()
        self.conn.commit()
        text = self.render()
        self.assertIn("WILDCARD ACTIVE", text)
        self.assertLess(text.index("WILDCARD ACTIVE"), text.index("- **Move:**"))
        self.assertLess(text.index("WILDCARD ACTIVE"), text.index("Transfers ranked"))

    def test_the_block_comes_before_the_tables(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertLess(text.index("- **Move:**"), text.index("## Transfers ranked"))
        self.assertLess(text.index("- **Move:**"), text.index("## Warehouse"))

    def test_the_ranked_table_says_how_many_rivals_own_each_player(self):
        """Ownership is measured against the people you race, so the table shows the
        count for both sides of every move, and the block's Ownership line carries it."""
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("| rivals own |", text)
        self.assertRegex(text, r"\| \d of 1 \|")
        self.assertIn("owned by 0 of 1 rivals (0%)", text)
        self.assertIn("from their gameweek 2 (deadline unknown) squads (1 rivals)", text)

    def test_stale_rivals_leave_the_ownership_column_empty_and_say_why(self):
        self.warehouse.healthy()
        self.warehouse.fixtures(gameweek=GAMEWEEK, finished=True)
        text = self.render()
        self.assertIn("Ownership not shown: rivals were last captured for gameweek 2 (deadline unknown) "
                      "and gameweek 3 (deadline 5 Sep 09:30) has finished", text)
        self.assertNotIn("rivals (", text)
        self.assertRegex(text, r"\| - \| \S+ \| - \|")

    def test_a_quiet_week_still_has_every_line_of_the_block(self):
        """A wildcard week, days before the deadline: a missing line would be
        indistinguishable from a brief that checked nothing."""
        self.warehouse.healthy()
        self.warehouse.wildcard()
        self.conn.commit()
        text = self.render(now=datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc))
        self.assertIn("- **Chips:** wildcard active this gameweek", text)
        self.assertIn("- **Learnings:** none proposed", text)
        self.assertNotIn("Push", text)
        self.assertNotIn("notif", text.lower())

    def test_the_block_opens_every_brief_with_the_same_lines_in_the_same_order(self):
        self.warehouse.healthy()
        text = self.render()
        labels = [line.split(":**")[0] for line in text.splitlines()
                  if line.startswith("- **")]
        self.assertEqual(labels[:8], ["- **Move", "- **Ownership", "- **Captain",
                                      "- **Chips", "- **Availability", "- **Deadline",
                                      "- **Learnings", "- **Data"])
        self.assertIn("- **Captain:** P1 (C01), 1.0 xP; next P2 1.0 - no armband captured",
                      text)
        self.assertIn("- **Learnings:** none proposed", text)

    def test_the_learnings_line_groups_repeats_and_speaks_plainly(self):
        self.warehouse.healthy()
        root = Path(self.tmp.name) / "learnings"
        root.mkdir()
        for number, gameweek in ((1, 3), (2, 4)):
            (root / f"000{number}-gw{gameweek}-under.md").write_text(
                f"---\nid: 000{number}\ngameweek: {gameweek}\nmodel_version: 0.5.0\n"
                f"metric: bias_by_start_probability\nslice: P(start) 75-100%\n"
                f"observation: under-projected by 0.54 points across 218 players\n"
                f"status: proposed\naction: none yet\n---\n\n# body\n")
        (root / "0003-gw4-over.md").write_text(
            "---\nid: 0003\ngameweek: 4\nmetric: bias_by_position\nslice: FWD\n"
            "observation: over-projected by 1.10 points across 40 players\n"
            "status: rejected\n---\n")
        text = self.render(learnings_dir=root)
        self.assertIn("- **Learnings:** 2 proposed - players almost certain to start "
                      "scored about half a point per game more than the model expected "
                      "(GW3 (deadline 5 Sep 09:30) and GW4 (deadline unknown) both; 0001, 0002)", text)
        self.assertNotIn("P(start)", text.split("## Deadline and transfers")[0])

    def test_the_block_names_who_cannot_play_and_why(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "d", chance=25, news="Knock")
        self.warehouse.flag(5, "s", news="Red card")
        self.conn.commit()
        text = self.render()
        self.assertIn("- **Availability:** 13 of 15 - P4 (doubtful, 25% to play), "
                      "P5 (suspended)", text)

    def test_an_unavailable_player_is_reported_on_the_availability_line(self):
        self.warehouse.healthy()
        self.warehouse.flag(4, "i", news="Hamstring")
        self.conn.commit()
        text = self.render()
        self.assertIn("- **Availability:** 14 of 15 - P4 (injured)", text)
        self.assertIn("## Squad availability", text)

    def test_a_falling_holding_is_in_the_brief_as_a_watch(self):
        self.warehouse.healthy()
        self.warehouse.falling(3)
        self.conn.commit()
        text = self.render()
        section = text.split("## Price watch")[1].split("\n## ")[0]
        self.assertIn("P3", section)
        self.assertIn("a watch, not a reason to move", " ".join(section.split()))

    def test_an_ungraded_warehouse_says_so_rather_than_showing_an_empty_table(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("No gameweek has been graded yet", text)

    def test_a_settled_gameweek_reports_its_calibration(self):
        self.warehouse.healthy()
        self.conn.execute(
            """INSERT INTO outcome VALUES (1, 1, 2, ?, 5.0, 2.0, 3.0, 0.9, 50, 3, 't')""",
            (MODEL_VERSION,))
        self.conn.commit()
        text = self.render()
        self.assertIn("Gameweek 2 (deadline unknown) under model", text)
        self.assertIn("+3.00", text)

    def test_the_deadline_and_free_transfers_are_stated(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("2026-09-05T09:30", text)
        self.assertIn("Free transfers", text)

    def test_a_passed_deadline_says_transfers_land_next_gameweek(self):
        self.warehouse.healthy()
        text = self.render(now=datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc))
        self.assertIn("has passed", text)

    def test_the_status_report_is_carried_verbatim(self):
        self.warehouse.healthy()
        text = self.render()
        self.assertIn("## Warehouse", text)
        for label in ("snapshot", "squad", "projections", "lineups", "actuals",
                      "grading", "rivals", "decisions"):
            self.assertIn(label, text.split("## Warehouse")[1])

    def test_an_empty_warehouse_renders_rather_than_raising(self):
        """A brief that crashes on a fresh clone is a brief nobody can use to find out
        why the clone is empty."""
        text = self.render()
        self.assertIn("# Gameweek 3 (deadline unknown) brief", text)
        self.assertIn("no snapshot captured", text)

    def test_a_market_only_snapshot_renders_rather_than_raising(self):
        self.warehouse.teams()
        self.warehouse.players()
        self.warehouse.game_config()
        self.warehouse.snapshot()
        self.warehouse.player_snapshots()
        self.warehouse.projections()
        self.warehouse.fixtures()
        self.conn.commit()
        text = self.render()
        self.assertIn("No squad captured", text)
        self.assertIn("no squad captured", text.lower())

    def test_the_page_is_rendered_from_the_evaluation_alone(self):
        """Reading the warehouse twice for one page is how its two halves disagree, so
        the renderer is handed a value and no connection: once evaluated, the warehouse
        can change or go away and the page is the same page."""
        self.warehouse.healthy()
        evaluation = self.evaluate()
        before = brief.render_brief(evaluation)
        self.conn.execute("DELETE FROM my_squad")
        self.conn.execute("DELETE FROM rival_squad")
        self.conn.commit()
        self.conn.close()
        self.assertEqual(brief.render_brief(evaluation), before)
        for line in brief.render_block(evaluation, markdown=False):
            self.assertTrue(line.split(":")[0] in before, line)


# --------------------------------------------------------------------------
# Writing the file, and never writing anything else
# --------------------------------------------------------------------------

class WriteBriefTests(BriefTestCase):

    def test_it_creates_the_directory_and_writes_gwnn(self):
        self.warehouse.healthy()
        root = Path(self.tmp.name) / "logs"
        self.assertFalse(root.exists())
        path = brief.write_brief(self.evaluate(), root)
        self.assertEqual(path, root / "gw03.md")
        self.assertIn("# Gameweek 3 (deadline 5 Sep 09:30) brief", path.read_text())

    def test_a_second_run_replaces_the_file_rather_than_appending(self):
        self.warehouse.healthy()
        root = Path(self.tmp.name) / "logs"
        first = brief.write_brief(self.evaluate(), root).read_text()
        second = brief.write_brief(self.evaluate(), root).read_text()
        self.assertEqual(first, second)

    def test_the_brief_writes_nothing_to_the_warehouse(self):
        """Read-only for the same reason `status` is: a brief that could change the
        warehouse is a brief you cannot trust to describe it."""
        self.warehouse.healthy()
        path = Path(self.tmp.name) / "fpl.db"
        disk = storage.connect(path)
        Warehouse(disk).healthy()
        disk.close()

        # A read-only connection raises on any write, so a clean render is the proof.
        conn = status_module.connect_readonly(path)
        try:
            text = brief.render_brief(brief.evaluate(
                conn, GAMEWEEK, now=NOW, learnings_dir=Path(self.tmp.name) / "learnings"))
        finally:
            conn.close()
        self.assertIn("# Gameweek 3 (deadline 5 Sep 09:30) brief", text)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

class CommandTests(BriefTestCase):

    def setUp(self):
        super().setUp()
        self.db = Path(self.tmp.name) / "fpl.db"
        disk = storage.connect(self.db)
        Warehouse(disk).healthy()
        disk.close()
        self.logs = Path(self.tmp.name) / "logs"

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = brief.main(["--db", str(self.db), "--logs", str(self.logs), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_it_writes_the_brief_for_the_latest_snapshots_target(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue((self.logs / "gw03.md").exists())
        self.assertIn("gw03.md", out)

    def test_gameweek_is_overridable(self):
        code, _, _ = self.run_cli("--gameweek", "4")
        self.assertEqual(code, 0)
        self.assertTrue((self.logs / "gw04.md").exists())

    def test_dry_run_prints_and_writes_nothing(self):
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.logs.exists())
        self.assertIn("# Gameweek 3 (deadline 5 Sep 09:30) brief", out)

    def test_a_missing_warehouse_is_a_sentence_and_exit_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = brief.main(["--db", str(Path(self.tmp.name) / "nope.db")])
        self.assertEqual(code, brief.EXIT_UNREADABLE)
        self.assertIn("no warehouse", err.getvalue())

    def test_the_command_is_registered_on_the_dispatcher(self):
        from fpl_agent import cli
        self.assertIn("brief", cli.COMMANDS)
        self.assertEqual(cli.COMMANDS["brief"][0], "fpl_agent.engine.brief")


if __name__ == "__main__":
    unittest.main()

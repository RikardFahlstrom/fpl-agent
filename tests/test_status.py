"""The warehouse health report. Offline: every warehouse here is built in memory.

Nothing in this file may touch `data/fpl.db` or the real token cache. The token check is
pointed at a temporary file through FPL_TOKEN_CACHE, which is the same knob
`headless_auth.cache_path` reads in production, so the shape under test is the real one.
"""
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fpl_agent.engine import status, storage
from fpl_agent.engine.projection import MODEL_VERSION
from fpl_agent.engine.snapshot import SQUAD_SIZE


class WarehouseBuilder:
    """A warehouse `status` calls clean, with one lever per inconsistency."""

    def __init__(self, conn):
        self.conn = conn

    def snapshot(self, gameweek=3, captured_at="2026-09-04T11:29:31+00:00"):
        self.conn.execute(
            "INSERT INTO snapshot (captured_at, gameweek, kind) VALUES (?,?,'test')",
            (captured_at, gameweek))
        return self.conn.execute("SELECT MAX(id) AS id FROM snapshot").fetchone()["id"]

    def squad(self, snapshot_id, rows=SQUAD_SIZE):
        for element_id in range(1, rows + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO my_squad VALUES (?,?,?,1,0,0,50,50)",
                (snapshot_id, element_id, element_id))

    def projections(self, snapshot_id, gameweek, n=3, model_version=MODEL_VERSION):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO player VALUES (?,?,'F','S',1,3,'t','t')",
                (element_id, f"P{element_id}"))
            self.conn.execute(
                """INSERT OR REPLACE INTO projection (snapshot_id, gameweek, element_id,
                   model_version, expected_points, p_start, expected_minutes,
                   fixture_count, components, created_at)
                   VALUES (?,?,?,?,4.0,0.9,80,1,'{}','t')""",
                (snapshot_id, gameweek, element_id, model_version))

    def lineups(self, snapshot_id, gameweek, n=11):
        for element_id in range(1, n + 1):
            self.conn.execute(
                "INSERT OR REPLACE INTO predicted_lineup "
                "VALUES (?,?,?,'ARS','rotowire','M',1,NULL,0)",
                (snapshot_id, gameweek, element_id))

    def fixtures(self, gameweek, finished=True, count=2):
        for i in range(count):
            self.conn.execute(
                "INSERT OR REPLACE INTO fixture VALUES (?,?,1,2,3,3,NULL,NULL,NULL,?,'{}')",
                (gameweek * 100 + i, gameweek, 1 if finished else 0))

    def actuals(self, gameweek, players=22):
        for element_id in range(900, 900 + players):
            self.conn.execute(
                "INSERT OR REPLACE INTO player VALUES (?,?,'F','S',1,3,'t','t')",
                (element_id, f"P{element_id}"))
            self.conn.execute(
                """INSERT OR REPLACE INTO player_gameweek (element_id, round,
                   total_points, minutes, raw) VALUES (?,?,2,90,'{}')""",
                (element_id, gameweek))

    def rivals(self, gameweek, managers=5):
        self.conn.execute("INSERT OR REPLACE INTO league VALUES (1,'L','x',10,'t')")
        for entry_id in range(1, managers + 1):
            self.conn.execute("INSERT OR REPLACE INTO rival VALUES (?,1,'p','e',1,10)",
                              (entry_id,))
            self.conn.execute("INSERT OR REPLACE INTO rival_squad VALUES (?,?,1,1,1,0,0)",
                              (entry_id, gameweek))

    def graded_gameweek(self, gameweek, n=3, model_version=MODEL_VERSION):
        """A past gameweek, projected from its own snapshot and settled.

        `outcome.projection_id` really does reference `projection(id)` and foreign keys
        are on, so the outcomes have to hang off projections that exist - which is also
        how settle writes them.
        """
        snapshot_id = self.snapshot(
            gameweek=gameweek, captured_at=f"2026-08-{gameweek + 9:02d}T00:00:00+00:00")
        self.projections(snapshot_id, gameweek, n=n, model_version=model_version)
        rows = self.conn.execute(
            "SELECT id, element_id FROM projection WHERE snapshot_id = ? AND gameweek = ?",
            (snapshot_id, gameweek)).fetchall()
        for row in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO outcome VALUES (?,?,?,?,4.0,4.0,0.0,0.9,55,3,'t')",
                (row["id"], row["element_id"], gameweek, model_version))
        return snapshot_id


class StatusTestCase(unittest.TestCase):
    """A warehouse in the state a healthy `make deadline` leaves behind.

    Gameweeks 1 and 2 played, backfilled and graded; a latest snapshot targeting gameweek
    3 with the squad captured, projections over a three-gameweek horizon under the current
    model, lineups filed under 3; rivals captured for gameweek 2.
    """

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.w = WarehouseBuilder(self.conn)
        for gameweek in (1, 2):
            self.w.fixtures(gameweek, finished=True)
            self.w.actuals(gameweek)
            self.older_snapshot_id = self.w.graded_gameweek(gameweek)
        # The current snapshot is created last so it is the latest one.
        self.snapshot_id = self.w.snapshot(gameweek=3)
        self.w.squad(self.snapshot_id)
        for gameweek in (3, 4, 5):
            self.w.projections(self.snapshot_id, gameweek)
        self.w.lineups(self.snapshot_id, 3)
        self.w.fixtures(3, finished=False)
        self.w.rivals(2)
        self.conn.commit()

    def gather(self):
        # The token cache has its own tests; excluding it here keeps every other
        # assertion independent of the machine the suite runs on.
        return status.gather(self.conn, include_token=False)

    def by_label(self):
        return {c.label: c for c in self.gather()}

    def assertClean(self):
        failed = [(c.label, c.detail) for c in self.gather() if c.failed]
        self.assertEqual(failed, [], f"expected no faults, got {failed}")

    def assertFails(self, label, *phrases):
        """The named check, and only it, failed - and its line says what is wrong rather
        than only what the number is. The reader has no other context."""
        failed = [c for c in self.gather() if c.failed]
        self.assertEqual([c.label for c in failed], [label],
                         f"expected only {label} to fail, got "
                         f"{[(c.label, c.detail) for c in failed]}")
        for phrase in phrases:
            self.assertIn(phrase, failed[0].detail)

    def _dump_to(self, path):
        """Copy the in-memory warehouse to a file, so main() can open it read-only."""
        target = sqlite3.connect(path)
        self.conn.backup(target)
        target.close()

    def _main(self, path, expected):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = status.main(["--db", str(path), "--no-token"])
        self.assertEqual(code, expected, buffer.getvalue())
        return buffer.getvalue()


class CleanWarehouseTests(StatusTestCase):
    def test_a_complete_warehouse_has_no_faults(self):
        self.assertClean()

    def test_the_report_names_every_check_and_says_it_agrees(self):
        report = status.render(self.gather(), "data/test.db")
        for label in ("snapshot", "squad", "projections", "lineups", "actuals",
                      "grading", "rivals", "decisions"):
            self.assertIn(label, report)
        self.assertIn("the warehouse agrees with itself", report)

    def test_main_exits_zero_over_a_clean_warehouse(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fpl.db"
            self._dump_to(path)
            self._main(path, status.EXIT_OK)

    def test_main_exits_seven_and_names_the_fault(self):
        """The exit code is the point: cron reads the status, not the log. 7 is status's
        own code, so a cron mail is unambiguous about which command complained."""
        self.conn.execute("DELETE FROM my_squad")
        self.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fpl.db"
            self._dump_to(path)
            report = self._main(path, status.EXIT_INCONSISTENT)
        self.assertIn("1 inconsistency(ies): squad", report)

    def test_main_exits_two_when_there_is_no_warehouse(self):
        """An absent file must not be created and then called healthy."""
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "absent.db"
            with redirect_stderr(io.StringIO()):
                code = status.main(["--db", str(absent), "--no-token"])
            self.assertEqual(code, status.EXIT_UNREADABLE)
            self.assertFalse(absent.exists())

    def test_the_connection_it_opens_cannot_write(self):
        """`status` must never be the thing that corrupts what it is checking."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fpl.db"
            self._dump_to(path)
            conn = status.connect_readonly(path)
            self.addCleanup(conn.close)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM my_squad")


class SnapshotTests(StatusTestCase):
    def test_no_snapshot_at_all_fails(self):
        self.conn.execute("DELETE FROM outcome")
        self.conn.execute("DELETE FROM projection")
        self.conn.execute("DELETE FROM predicted_lineup")
        self.conn.execute("DELETE FROM my_squad")
        self.conn.execute("DELETE FROM snapshot")
        self.conn.commit()
        self.assertFails("snapshot", "never been captured")

    def test_a_snapshot_with_no_target_gameweek_fails(self):
        self.conn.execute("UPDATE snapshot SET gameweek = NULL WHERE id = ?",
                          (self.snapshot_id,))
        self.conn.commit()
        self.assertFails("snapshot", "no target gameweek")

    def test_a_stale_snapshot_is_flagged_but_does_not_fail(self):
        """How stale is too stale depends on the deadline, which status cannot know."""
        self.conn.execute(
            "UPDATE snapshot SET captured_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (self.snapshot_id,))
        self.conn.commit()
        self.assertEqual(self.by_label()["snapshot"].level, status.WARN)
        self.assertClean()


class SquadTests(StatusTestCase):
    def test_a_missing_squad_fails_and_names_itself(self):
        """Regression: the preflight promised a squad, nothing logged in, and the run
        exited 0 with a market-only snapshot nobody noticed."""
        self.conn.execute("DELETE FROM my_squad WHERE snapshot_id = ?",
                          (self.snapshot_id,))
        self.conn.commit()
        self.assertFails("squad", "absent for snapshot", "recommend")

    def test_a_partial_squad_fails(self):
        self.conn.execute("DELETE FROM my_squad WHERE element_id > 9")
        self.conn.commit()
        self.assertFails("squad", f"9 of {SQUAD_SIZE}")

    def test_a_squad_on_an_older_snapshot_does_not_count(self):
        """15 rows somewhere in the table is not 15 rows on the snapshot being used."""
        newer = self.w.snapshot(gameweek=3, captured_at="2026-09-04T23:00:00+00:00")
        self.w.projections(newer, 3)
        self.w.lineups(newer, 3)
        self.conn.commit()
        self.assertFails("squad", f"absent for snapshot {newer}")


class ProjectionTests(StatusTestCase):
    def test_no_projections_for_the_target_gameweek_fails(self):
        self.conn.execute("DELETE FROM projection WHERE snapshot_id = ? AND gameweek = 3",
                          (self.snapshot_id,))
        self.conn.commit()
        self.assertFails("projections", "none for gameweek 3", "never projected")

    def test_projections_only_under_a_superseded_model_version_fail(self):
        """CLAUDE.md: bumping MODEL_VERSION leaves both versions in the warehouse, so the
        presence of a projection proves nothing about whether `project` re-ran."""
        self.conn.execute("DELETE FROM projection WHERE snapshot_id = ? AND gameweek = 3",
                          (self.snapshot_id,))
        self.w.projections(self.snapshot_id, 3, model_version="0.0.1-old")
        self.conn.commit()
        self.assertFails("projections", f"none under model {MODEL_VERSION}", "0.0.1-old")

    def test_projections_on_an_older_snapshot_do_not_count(self):
        """The horizon of last week's snapshot also covers gameweek 3; that is not the
        same as this snapshot having been projected."""
        self.conn.execute("DELETE FROM projection WHERE snapshot_id = ? AND gameweek = 3",
                          (self.snapshot_id,))
        self.w.projections(self.older_snapshot_id, 3)
        self.conn.commit()
        self.assertFails("projections", "none for gameweek 3")

    def test_a_superseded_version_alongside_the_current_one_is_fine(self):
        self.w.projections(self.snapshot_id, 3, model_version="0.0.1-old")
        self.conn.commit()
        self.assertClean()
        detail = self.by_label()["projections"].detail
        self.assertIn(f"under model {MODEL_VERSION}", detail)
        self.assertIn("also stored: 0.0.1-old", detail)

    def test_the_clean_line_reports_the_horizon(self):
        self.assertIn("3-gameweek horizon", self.by_label()["projections"].detail)


class LineupTests(StatusTestCase):
    def test_the_clean_line_names_what_project_will_use(self):
        detail = self.by_label()["lineups"].detail
        self.assertIn(f"11 for gameweek 3, snapshot {self.snapshot_id}", detail)
        self.assertIn("`project` will use", detail)

    def test_no_lineups_yet_is_a_warning_not_a_fault(self):
        """RotoWire publishes near matchday; a Tuesday snapshot legitimately has none."""
        self.conn.execute("DELETE FROM predicted_lineup")
        self.conn.commit()
        lineups = self.by_label()["lineups"]
        self.assertEqual(lineups.level, status.WARN)
        self.assertIn("historical start rates", lineups.detail)
        self.assertClean()

    def test_lineups_filed_under_an_earlier_round_are_not_a_fault(self):
        """Filing is per fixture: a snapshot taken between the gameweek 2 deadline and
        that round's last kickoff targets 3 while correctly filing gameweek 2 lineups.
        Calling that broken would train the reader to ignore the line."""
        self.conn.execute("UPDATE predicted_lineup SET gameweek = 2 WHERE snapshot_id = ?",
                          (self.snapshot_id,))
        self.conn.commit()
        lineups = self.by_label()["lineups"]
        self.assertEqual(lineups.level, status.WARN)
        self.assertIn("11 for gameweek 2", lineups.detail)
        self.assertIn("no snapshot holds any for gameweek 3", lineups.detail)
        self.assertClean()

    def test_a_snapshot_straddling_a_changeover_files_two_rounds(self):
        """`lineups.LineupCapture.gameweeks` is a list for exactly this case."""
        self.conn.execute(
            "UPDATE predicted_lineup SET gameweek = 2 WHERE snapshot_id = ? AND "
            "element_id <= 5", (self.snapshot_id,))
        self.conn.commit()
        detail = self.by_label()["lineups"].detail
        self.assertIn("5 for gameweek 2, 6 for gameweek 3", detail)
        self.assertIn("the 6 for gameweek 3 are what `project` will use", detail)
        self.assertClean()

    def test_it_names_the_older_snapshot_project_would_fall_back_to(self):
        """`lineup_start_rates` takes the latest snapshot holding the gameweek, which
        need not be the latest snapshot."""
        newer = self.w.snapshot(gameweek=3, captured_at="2026-09-04T23:00:00+00:00")
        self.w.squad(newer)
        self.w.projections(newer, 3)
        self.conn.commit()
        lineups = self.by_label()["lineups"]
        self.assertEqual(lineups.level, status.WARN)
        self.assertIn("filed none", lineups.detail)
        self.assertIn(f"older snapshot {self.snapshot_id} (11 rows)", lineups.detail)
        self.assertClean()


class ActualsTests(StatusTestCase):
    def test_backfill_behind_a_finished_gameweek_fails(self):
        self.conn.execute("DELETE FROM player_gameweek WHERE round = 2")
        self.conn.commit()
        self.assertFails("actuals", "backfilled through round 1",
                         "gameweek 2 has finished")

    def test_no_actuals_at_all_behind_a_finished_gameweek_fails(self):
        self.conn.execute("DELETE FROM player_gameweek")
        self.conn.commit()
        self.assertFails("actuals", "no player_gameweek rows at all")

    def test_an_unfinished_gameweek_with_no_actuals_is_not_a_fault(self):
        """CLAUDE.md: absence of a row is data only once the fixtures are played. Before
        kickoff every player is missing a row for gameweek 3, and that is correct."""
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM player_gameweek WHERE round = 3").fetchone()[0], 0)
        self.assertClean()
        detail = self.by_label()["actuals"].detail
        self.assertIn("backfilled through round 2", detail)
        self.assertIn("latest finished gameweek is 2", detail)

    def test_a_warehouse_where_nothing_has_finished_is_not_behind(self):
        self.conn.execute("UPDATE fixture SET finished = 0")
        self.conn.execute("DELETE FROM player_gameweek")
        self.conn.commit()
        self.assertClean()
        self.assertIn("nothing to be behind", self.by_label()["actuals"].detail)

    def test_a_partially_played_gameweek_does_not_count_as_finished(self):
        """The same rule as settle.gameweek_is_finished: one fixture still to play means
        the round is not over, so the backfill is not behind it."""
        self.conn.execute("UPDATE fixture SET finished = 1 WHERE id = 300")
        self.conn.commit()
        self.assertEqual(status.finished_gameweeks(self.conn), [1, 2])
        self.assertClean()

    def test_a_gameweek_with_no_fixtures_recorded_is_not_finished(self):
        self.assertNotIn(9, status.finished_gameweeks(self.conn))


class GradingTests(StatusTestCase):
    def test_an_unfinished_gameweek_is_never_reported_as_ungraded(self):
        """Gameweek 3 carries no outcome rows and must not: it has not been played."""
        grading = self.by_label()["grading"]
        self.assertEqual(grading.level, status.OK)
        self.assertIn("gameweek(s) 1, 2 finished and graded", grading.detail)
        self.assertNotIn("make settle", grading.detail)

    def test_a_finished_but_ungraded_gameweek_warns_without_failing(self):
        self.conn.execute("DELETE FROM outcome WHERE gameweek = 2")
        self.conn.commit()
        grading = self.by_label()["grading"]
        self.assertEqual(grading.level, status.WARN)
        self.assertIn("make settle GW=2", grading.detail)
        self.assertClean()

    def test_a_gameweek_that_predates_the_warehouse_is_not_a_standing_warning(self):
        """The real warehouse's first snapshot targeted gameweek 3, so 1 and 2 can never
        be graded - no projection was ever made from a snapshot targeting them, and the
        prices, lineups and ownership one would need are gone. Telling someone to settle
        them is advice that cannot be taken, and an unactionable warning teaches the
        reader to skim every other line in the block.
        """
        self.conn.execute("DELETE FROM outcome")
        self.conn.execute("DELETE FROM projection WHERE gameweek IN (1, 2)")
        self.conn.commit()

        grading = self.by_label()["grading"]

        self.assertEqual(grading.level, status.OK)
        self.assertIn("can never be graded", grading.detail)
        self.assertNotIn("make settle", grading.detail)
        self.assertClean()

    def test_a_settleable_gameweek_still_warns_and_names_the_unreachable_ones(self):
        """One of each: gameweek 1 is beyond reach, gameweek 2 is waiting to be settled."""
        self.conn.execute("DELETE FROM outcome")
        self.conn.execute("DELETE FROM projection WHERE gameweek = 1")
        self.conn.commit()

        grading = self.by_label()["grading"]

        self.assertEqual(grading.level, status.WARN)
        self.assertIn("make settle GW=2", grading.detail)
        self.assertIn("can never be graded", grading.detail)
        self.assertClean()

    def test_nothing_finished_means_an_empty_outcome_table_is_right(self):
        self.conn.execute("UPDATE fixture SET finished = 0")
        self.conn.execute("DELETE FROM outcome")
        self.conn.commit()
        grading = self.by_label()["grading"]
        self.assertEqual(grading.level, status.OK)
        self.assertIn("the right state", grading.detail)
        self.assertClean()

    def test_outcomes_under_a_superseded_model_do_not_count_as_graded(self):
        self.conn.execute("UPDATE outcome SET model_version = '0.0.1-old'")
        self.conn.commit()
        self.assertEqual(self.by_label()["grading"].level, status.WARN)
        self.assertClean()


class RivalTests(StatusTestCase):
    def test_no_rivals_warns_without_failing(self):
        """With none, every candidate has unknown ownership instead of zero - which is
        the bug that discarded 165 of 200 candidates."""
        self.conn.execute("DELETE FROM rival_squad")
        self.conn.commit()
        rivals = self.by_label()["rivals"]
        self.assertEqual(rivals.level, status.WARN)
        self.assertIn("unknown rather than zero", rivals.detail)
        self.assertClean()

    def test_rivals_behind_the_last_finished_gameweek_warn(self):
        self.conn.execute("UPDATE rival_squad SET gameweek = 1")
        self.conn.commit()
        rivals = self.by_label()["rivals"]
        self.assertEqual(rivals.level, status.WARN)
        self.assertIn("stale", rivals.detail)
        self.assertClean()

    def test_the_clean_line_counts_managers(self):
        self.assertIn("5 managers", self.by_label()["rivals"].detail)


class DecisionTests(StatusTestCase):
    def test_no_decisions_is_never_a_fault(self):
        self.assertEqual(self.by_label()["decisions"].level, status.OK)
        self.assertClean()

    def test_decisions_are_counted(self):
        self.conn.execute(
            """INSERT INTO decision (created_at, gameweek, model_version, kind, payload,
               rationale) VALUES ('2026-09-04T00:00:00+00:00',3,?,'transfer','{}','r')""",
            (MODEL_VERSION,))
        self.conn.commit()
        self.assertIn("1 recorded", self.by_label()["decisions"].detail)


class WarehouseShapeTests(unittest.TestCase):
    def test_a_file_that_is_not_a_warehouse_fails(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE something (id INTEGER)")
        checks = status.gather(conn, include_token=False)
        self.assertTrue(checks[0].failed)
        self.assertIn("not an fpl-agent warehouse", checks[0].detail)


class TokenTests(unittest.TestCase):
    """The cache is read, never exchanged: the account service rotates the refresh token
    on every exchange, so a status run that refreshed would leave a concurrent job holding
    a dead credential. FPL_TOKEN_CACHE points at a temporary file here; the real cache is
    never opened."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        patcher = mock.patch.dict(os.environ, {"FPL_TOKEN_CACHE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, payload):
        self.path.write_text(json.dumps(payload))

    def test_a_fresh_token_needs_no_browser(self):
        self._write({"api_token": "x", "refresh_token": "y",
                     "expires_at": time.time() + 8 * 3600})
        check = status.check_token()
        self.assertEqual(check.level, status.OK)
        self.assertIn("no browser needed", check.detail)

    def test_an_expired_token_with_a_refresh_token_still_needs_no_browser(self):
        """The refresh grant is why Chromium is a fallback rather than a nightly step."""
        self._write({"api_token": "x", "refresh_token": "y",
                     "expires_at": time.time() - 3600})
        check = status.check_token()
        self.assertEqual(check.level, status.OK)
        self.assertIn("refreshable", check.detail)

    def test_an_expired_token_with_no_refresh_token_needs_a_browser(self):
        self._write({"api_token": "x", "expires_at": time.time() - 3600})
        check = status.check_token()
        self.assertEqual(check.level, status.WARN)
        self.assertIn("launches a browser", check.detail)

    def test_no_cache_at_all_needs_a_browser(self):
        check = status.check_token()
        self.assertEqual(check.level, status.WARN)
        self.assertIn("launches a browser", check.detail)

    def test_an_unreadable_cache_needs_a_browser(self):
        self.path.write_text("{not json")
        check = status.check_token()
        self.assertEqual(check.level, status.WARN)
        self.assertIn("launches a browser", check.detail)

    def test_reading_the_cache_never_writes_to_it(self):
        payload = {"api_token": "x", "refresh_token": "y", "expires_at": time.time() - 1}
        self._write(payload)
        before = self.path.read_bytes()
        status.check_token()
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_part_of_the_token_is_ever_printed(self):
        """The cache is a bearer credential that can execute transfers."""
        secret, refresh = "SECRET-ACCESS-TOKEN", "SECRET-REFRESH-TOKEN"
        for expires_at in (time.time() + 8 * 3600, time.time() - 3600):
            self._write({"api_token": secret, "refresh_token": refresh,
                         "expires_at": expires_at})
            report = status.render([status.check_token()], "data/fpl.db")
            self.assertNotIn(secret, report)
            self.assertNotIn(refresh, report)
            for fragment in (secret[:6], refresh[:6]):
                self.assertNotIn(fragment, report)


if __name__ == "__main__":
    unittest.main()

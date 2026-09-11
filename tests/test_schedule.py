"""What each scheduled job plans, asserted as data.

The whole point of the module under test is that "what would run tonight" stops being a
dry run against a live warehouse and becomes an assertion. So nothing here runs a step,
and nothing here reads `data/fpl.db`: every warehouse is built in memory or in a
temporary file, the current time is passed in, and the Plan is compared field by field.

No test asserts that a helper was called. A Plan is the observable behaviour.
"""

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fpl_agent.engine import schedule, storage

from test_status import WarehouseBuilder

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


#: What every job that does anything ends on, once the tolerated tail is planned. With
#: no topic configured - the default in these tests - `notify` is skipped rather than
#: planned, so the tail is the brief alone.
TAIL = ["brief"]


def commands(plan: schedule.Plan) -> list[str]:
    """The invocations in order, which is what a caller would actually run."""
    return [step.invocation for step in plan.steps]


class ScheduleTestCase(unittest.TestCase):
    """A warehouse mid-season: gameweeks 1 and 2 played and graded, 3 still to come."""

    def setUp(self):
        self.conn = storage.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.w = WarehouseBuilder(self.conn)
        for gameweek in (1, 2):
            self.fixtures(gameweek, finished=True)
            self.w.actuals(gameweek)
            self.w.graded_gameweek(gameweek)
        self.fixtures(3, finished=False)
        self.snapshot_id = self.w.snapshot(gameweek=3)
        self.w.projections(self.snapshot_id, 3)
        self.conn.commit()

    #: What `hours_from_now` below is measured from. Every test that calls `due` passes
    #: NOW in, so the fixed anchor and the injected clock agree. `CommandTests` is the
    #: exception and overrides this: the command reads the real clock, deliberately.
    anchor = NOW

    def fixtures(self, gameweek: int, *, finished: bool,
                 hours_from_now: Optional[float] = None, count: int = 2) -> None:
        """A round's fixtures, written the way a capture writes them.

        Through `storage.upsert_fixtures` from API-shaped dicts rather than by hand, so
        the kickoff timestamps under test are the shape the API actually stores - which
        is `...Z`, the one `datetime.fromisoformat` refuses before Python 3.11.
        """
        kickoff = None if hours_from_now is None else (
            self.anchor + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")
        storage.upsert_fixtures(self.conn, [
            {"id": gameweek * 100 + i, "event": gameweek, "team_h": 1, "team_a": 2,
             "team_h_difficulty": 3, "team_a_difficulty": 3,
             "kickoff_time": kickoff, "finished": finished}
            for i in range(count)])
        self.conn.commit()

    def kickoff(self, hours_from_now: float, gameweek: int = 3) -> None:
        """Put the gameweek's first kickoff that many hours after NOW.

        The deadline is 90 minutes before it, so 5 hours here is 3.5 hours to the deadline.
        """
        self.fixtures(gameweek, finished=False, hours_from_now=hours_from_now)

    def settleable_gameweek(self, gameweek: int) -> None:
        """A round that has finished, was projected from its own snapshot, and is ungraded."""
        self.fixtures(gameweek, finished=True)
        self.w.actuals(gameweek)
        snapshot_id = self.w.snapshot(gameweek=gameweek)
        self.w.projections(snapshot_id, gameweek)
        self.conn.commit()

    def due(self, job: str, *, now: datetime = NOW, conn=None) -> schedule.Plan:
        warehouse = schedule.Opened(self.conn if conn is None else conn)
        return schedule.due(job, now=now, warehouse=warehouse, settings=schedule.Settings())


class DailyTests(ScheduleTestCase):
    """The overnight job: capture the market that no endpoint returns later, then grade."""

    def test_it_captures_with_the_backfill_and_projects_before_anything_else(self):
        plan = self.due("daily")
        self.assertEqual(commands(plan)[:3], [
            "snapshot --force",
            "snapshot --backfill-only",
            "project --horizon 3",
        ])

    def test_every_step_carries_a_reason(self):
        plan = self.due("daily")
        self.assertTrue(all(step.reason for step in plan.steps))

    def test_it_grades_every_settleable_gameweek_oldest_first(self):
        self.settleable_gameweek(4)
        self.settleable_gameweek(5)
        plan = self.due("daily")
        self.assertEqual(commands(plan)[3:], [
            "settle --gameweek 4 --learn",
            "settle --gameweek 5 --learn",
        ] + TAIL)

    def test_nothing_to_grade_is_a_skip_with_a_reason_rather_than_silence(self):
        plan = self.due("daily")
        self.assertEqual(commands(plan), [
            "snapshot --force", "snapshot --backfill-only", "project --horizon 3"] + TAIL)
        self.assertTrue(any("grad" in skip.reason for skip in plan.skipped),
                        plan.skipped)

    def test_an_unfinished_gameweek_is_never_planned_for_grading(self):
        # Gameweek 3 is projected from its own snapshot but is still being played.
        plan = self.due("daily")
        self.assertNotIn("settle --gameweek 3 --learn", commands(plan))

    def test_it_does_not_rank_however_close_the_deadline_is(self):
        self.kickoff(2)
        plan = self.due("daily")
        self.assertNotIn("recommend", [step.command for step in plan.steps])


class TailTests(ScheduleTestCase):
    """The brief and the notifier, which are Steps now rather than a special case."""

    def test_both_tolerate_failure(self):
        settings = schedule.Settings(notifications_configured=True)
        plan = schedule.due("daily", now=NOW, warehouse=schedule.Opened(self.conn),
                            settings=settings)
        tail = [step for step in plan.steps if step.command in ("brief", "notify")]
        self.assertEqual([step.command for step in tail], ["brief", "notify"])
        self.assertTrue(all(step.tolerated for step in tail))

    def test_they_come_last_so_they_describe_what_the_run_left_behind(self):
        plan = schedule.due("daily", now=NOW, warehouse=schedule.Opened(self.conn),
                            settings=schedule.Settings(notifications_configured=True))
        self.assertEqual(commands(plan)[-2:], ["brief", "notify"])

    def test_an_unconfigured_topic_skips_the_push_rather_than_failing_it(self):
        # notify is opt-in: a host that has never set a topic must not be mailed an error
        # every hour.
        plan = self.due("daily")
        self.assertNotIn("notify", [step.command for step in plan.steps])
        self.assertTrue(any(skip.what == "notify" for skip in plan.skipped), plan.skipped)

    def test_a_job_with_nothing_due_writes_no_brief(self):
        self.kickoff(80)
        self.assertEqual(self.due("deadline").steps, ())


class DeadlineTests(ScheduleTestCase):
    """The hourly job: cheap when idle, and a full re-capture inside the window."""

    def test_nothing_is_planned_when_the_deadline_is_far_out(self):
        self.kickoff(80)
        plan = self.due("deadline")
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.exit_code, 0)
        self.assertTrue(any("78" in skip.reason for skip in plan.skipped), plan.skipped)

    def test_nothing_is_planned_when_no_fixture_is_left_to_play(self):
        self.fixtures(3, finished=True)
        plan = self.due("deadline")
        self.assertEqual(plan.steps, ())
        self.assertTrue(plan.skipped)

    def test_a_deadline_already_passed_is_not_a_deadline_to_rank_for(self):
        # A round played but not yet confirmed finished by FPL sits here for hours.
        self.kickoff(-5)
        self.assertEqual(self.due("deadline").steps, ())

    def test_a_deadline_thirty_minutes_gone_is_already_gone(self):
        # Truncating toward zero would call this "0h away", which is inside every window:
        # a full re-capture and ranking for a deadline nobody can act on any more.
        self.kickoff(1.0)   # kickoff in an hour, so the deadline went half an hour ago
        plan = self.due("deadline")
        self.assertEqual(plan.steps, ())
        self.assertTrue(any("passed" in skip.reason for skip in plan.skipped), plan.skipped)

    def test_inside_the_window_it_recaptures_ranks_and_ends_on_status(self):
        self.kickoff(5)
        plan = self.due("deadline")
        self.assertEqual(commands(plan), [
            "snapshot --force",
            "project --horizon 3",
            "rivals",
            "recommend",
            "status",
        ] + TAIL)

    def test_the_hourly_job_skips_the_backfill_and_says_why(self):
        self.kickoff(5)
        plan = self.due("deadline")
        self.assertNotIn("snapshot --backfill-only", commands(plan))
        self.assertTrue(any("backfill" in skip.what for skip in plan.skipped),
                        plan.skipped)

    def test_the_window_is_a_setting_not_a_number_buried_in_the_decision(self):
        self.kickoff(20)
        warehouse = schedule.Opened(self.conn)
        narrow = schedule.due("deadline", now=NOW, warehouse=warehouse,
                              settings=schedule.Settings(deadline_within_hours=4))
        self.assertEqual(narrow.steps, ())
        wide = schedule.due("deadline", now=NOW, warehouse=warehouse,
                            settings=schedule.Settings(deadline_within_hours=26))
        self.assertTrue(wide.steps)

    def test_the_same_warehouse_is_due_or_not_according_to_the_time_passed_in(self):
        self.kickoff(30)
        self.assertEqual(self.due("deadline").steps, ())
        later = self.due("deadline", now=NOW + timedelta(hours=10))
        self.assertTrue(later.steps)


class AutoTests(ScheduleTestCase):
    """Both halves, for a person at a terminal who does not want to know which day it is."""

    def test_both_halves_hang_off_a_single_capture(self):
        self.kickoff(5)
        self.settleable_gameweek(4)
        plan = self.due("auto")
        self.assertEqual(commands(plan), [
            "snapshot --force",
            "snapshot --backfill-only",
            "project --horizon 3",
            "settle --gameweek 4 --learn",
            "rivals",
            "recommend",
            "status",
        ] + TAIL)

    def test_out_of_the_window_it_is_the_daily_job(self):
        self.settleable_gameweek(4)
        plan = self.due("auto")
        self.assertEqual(commands(plan), [
            "snapshot --force",
            "snapshot --backfill-only",
            "project --horizon 3",
            "settle --gameweek 4 --learn",
        ] + TAIL)
        self.assertTrue(any("rank" in skip.what or "rank" in skip.reason
                            for skip in plan.skipped), plan.skipped)


class StandingsTests(ScheduleTestCase):
    """The league table is part of the capture, not of the ranking half (issue #49).

    It went five days stale because `rivals` - standings and picks together - sat behind
    the deadline window that exists for the picks. The table is public and one request,
    so every capture refreshes it, as long as there is a league to refresh.
    """

    STEP = "rivals --standings-only"

    def known_league(self):
        self.w.rivals(2)
        self.conn.commit()

    def test_the_daily_capture_refreshes_the_table_after_projecting(self):
        self.known_league()
        self.assertEqual(commands(self.due("daily"))[:4], [
            "snapshot --force", "snapshot --backfill-only", "project --horizon 3",
            self.STEP])

    def test_the_hourly_recapture_refreshes_it_too(self):
        self.known_league()
        self.kickoff(5)
        self.assertEqual(commands(self.due("deadline"))[:3], [
            "snapshot --force", "project --horizon 3", self.STEP])

    def test_auto_refreshes_it_once(self):
        self.known_league()
        self.kickoff(5)
        self.assertEqual(commands(self.due("auto")).count(self.STEP), 1)

    def test_it_is_not_tolerated_and_carries_a_reason(self):
        self.known_league()
        step = [s for s in self.due("daily").steps if s.invocation == self.STEP][0]
        self.assertFalse(step.tolerated)
        self.assertTrue(step.reason)

    def test_no_known_league_is_a_skip_that_says_what_records_one(self):
        plan = self.due("daily")
        self.assertNotIn(self.STEP, commands(plan))
        skip = [s for s in plan.skipped if "standings" in s.what][0]
        self.assertIn("make rivals", skip.reason)

    def test_an_unreadable_warehouse_skips_it_with_the_warehouses_reason(self):
        plan = schedule.due("daily", now=NOW, settings=schedule.Settings(),
                            warehouse=schedule.Opened(problem="no warehouse at x"))
        self.assertNotIn(self.STEP, commands(plan))
        skip = [s for s in plan.skipped if "standings" in s.what][0]
        self.assertEqual(skip.reason, "no warehouse at x")

    def test_the_summary_does_not_mention_it(self):
        self.known_league()
        self.kickoff(24 * 7)
        self.assertIn("nothing due", schedule.summarise(self.due("auto")))

    def test_its_failure_reports_after_the_snapshots_never_before(self):
        self.known_league()
        executor = RecordingExecutor({"snapshot": 3, "rivals": 1})
        outcome = schedule.run(self.due("daily"), executor)
        self.assertEqual(outcome.exit_code, 3)
        self.assertIn(self.STEP, executor.ran)
        alone = schedule.run(self.due("daily"), RecordingExecutor({"rivals": 1}))
        self.assertEqual(alone.exit_code, 1)
        self.assertEqual(alone.reported.step.invocation, self.STEP)


class ReadOnlyTests(ScheduleTestCase):
    """Producing a Plan is a read. Nothing about it may touch the warehouse."""

    def test_a_plan_can_be_produced_over_a_read_only_connection(self):
        self.kickoff(5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fpl.db"
            with sqlite3.connect(path) as target:
                self.conn.backup(target)
            conn = storage.connect_readonly(path)
            self.addCleanup(conn.close)
            for job in schedule.JOBS:
                self.assertTrue(self.due(job, conn=conn).steps, job)


class ColdStartTests(unittest.TestCase):
    """A host with no warehouse. The capture is what creates one, so it is still due."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fpl.db"

    def due(self, job: str) -> schedule.Plan:
        return schedule.due(job, now=NOW,
                            warehouse=schedule.open_warehouse(self.path),
                            settings=schedule.Settings())

    def test_daily_still_captures_and_marks_the_rest_skipped_with_that_reason(self):
        plan = self.due("daily")
        self.assertEqual(commands(plan)[0], "snapshot --force")
        self.assertTrue(plan.skipped)
        self.assertIn(str(self.path),
                      [skip.reason for skip in plan.skipped if skip.what == "grading"][0])
        self.assertEqual(plan.exit_code, 0)

    def test_auto_captures_too(self):
        self.assertIn("snapshot --force", commands(self.due("auto")))

    def test_the_hourly_job_plans_nothing_and_exits_two(self):
        plan = self.due("deadline")
        self.assertEqual(plan.steps, ())
        self.assertEqual(plan.exit_code, schedule.EXIT_UNREADABLE)

    def test_could_not_ask_never_renders_the_same_as_nothing_is_due(self):
        unreadable = schedule.render(self.due("deadline"))
        quiet = schedule.render(schedule.Plan(job="deadline", at=NOW, skipped=(
            schedule.Skipped("the ranking half", "no unfinished fixture"),)))
        self.assertNotEqual(unreadable, quiet)
        self.assertIn(str(self.path), unreadable)

    def test_the_summary_line_of_a_partial_plan_says_it_is_partial(self):
        # The one line is what a caller may print instead of the rest, so a plan made
        # without being able to read the warehouse cannot look like a complete one.
        self.assertIn("could not be read",
                      schedule.render(self.due("daily"), one_line=True))

    def test_a_file_that_is_not_a_warehouse_reads_as_unreadable_not_as_empty(self):
        self.path.write_text("this is not a database")
        warehouse = schedule.open_warehouse(self.path)
        self.assertIsNone(warehouse.conn)
        self.assertIn(str(self.path), warehouse.problem)

    def test_a_database_missing_the_tables_is_not_a_warehouse(self):
        sqlite3.connect(self.path).close()
        self.assertIsNone(schedule.open_warehouse(self.path).conn)


class RenderTests(ScheduleTestCase):
    """A dry run has to explain itself, not list commands."""

    def test_every_step_and_every_skip_appears_with_its_reason(self):
        self.kickoff(5)
        plan = self.due("auto")
        text = schedule.render(plan)
        for step in plan.steps:
            self.assertIn(step.invocation, text)
            self.assertIn(step.reason, text)
        for skip in plan.skipped:
            self.assertIn(skip.reason, text)

    def test_a_step_whose_failure_is_tolerated_says_so(self):
        plan = schedule.Plan(job="daily", at=NOW, steps=(
            schedule.Step("brief", (), "the reasoning is worth keeping", tolerated=True),))
        self.assertIn("tolerated", schedule.render(plan))

    def test_one_line_is_the_first_line_of_the_whole_thing(self):
        plan = self.due("daily")
        self.assertEqual(schedule.render(plan, one_line=True),
                         schedule.render(plan).splitlines()[0])

    def test_the_one_line_says_how_much_is_due(self):
        self.kickoff(80)
        self.assertIn("nothing due", schedule.render(self.due("deadline"), one_line=True))
        self.assertIn("4 steps due", schedule.render(self.due("daily"), one_line=True))


class RecordingExecutor:
    """A hand-written stand-in for the subprocess executor.

    It records what it was asked to run, in order, and returns whatever exit code the
    test scripted for that command - defaulting to success. Ordering is asserted from
    inside the stub rather than reconstructed afterwards, which is how the notifier's
    "record the fingerprint only after a successful send" test is built.
    """

    def __init__(self, codes: Optional[dict[str, int]] = None):
        self.codes = codes or {}
        self.ran: list[str] = []

    def __call__(self, step: schedule.Step) -> int:
        self.ran.append(step.invocation)
        return self.codes.get(step.command, 0)


class RunTests(ScheduleTestCase):
    """The precedence rule, pinned. It was a comment in a shell script and a live bug."""

    def plan(self, *steps: schedule.Step) -> schedule.Plan:
        return schedule.Plan(job="daily", at=NOW, steps=steps)

    def test_every_step_runs_in_order(self):
        executor = RecordingExecutor()
        outcome = schedule.run(self.due("daily"), executor)
        self.assertEqual(executor.ran, commands(outcome.plan))
        self.assertEqual(outcome.exit_code, 0)

    def test_the_first_failure_is_reported_not_the_last(self):
        # The live bug: a snapshot with no session (3) followed by a project that failed
        # (1) reported 1 - the recoverable code for the irrecoverable failure.
        outcome = schedule.run(self.due("daily"),
                               RecordingExecutor({"snapshot": 3, "project": 1}))
        self.assertEqual(outcome.exit_code, 3)

    def test_a_failing_step_does_not_stop_the_steps_after_it(self):
        executor = RecordingExecutor({"snapshot": 3})
        outcome = schedule.run(self.due("daily"), executor)
        self.assertEqual(executor.ran, commands(outcome.plan))
        self.assertIn("brief", executor.ran)

    def test_a_tolerated_failure_is_reported_when_nothing_else_failed(self):
        outcome = schedule.run(self.plan(
            schedule.Step("snapshot", (), "captures"),
            schedule.Step("notify", (), "pushes", tolerated=True)),
            RecordingExecutor({"notify": 8}))
        self.assertEqual(outcome.exit_code, 8)

    def test_a_tolerated_failure_never_masks_a_real_one(self):
        # A dead ntfy server must not make a run that captured the market look like one
        # that lost it.
        outcome = schedule.run(self.plan(
            schedule.Step("snapshot", (), "captures"),
            schedule.Step("notify", (), "pushes", tolerated=True)),
            RecordingExecutor({"snapshot": 3, "notify": 8}))
        self.assertEqual(outcome.exit_code, 3)
        self.assertEqual([r.step.command for r in outcome.masked], ["notify"])

    def test_a_real_failure_after_a_tolerated_one_still_wins(self):
        # Order must not decide it: the brief fails first, the settle after it.
        outcome = schedule.run(self.plan(
            schedule.Step("brief", (), "writes", tolerated=True),
            schedule.Step("settle", (), "grades")),
            RecordingExecutor({"brief": 1, "settle": 6}))
        self.assertEqual(outcome.exit_code, 6)

    def test_the_plans_own_code_stands_when_nothing_ran(self):
        # The hourly job on a host whose warehouse will not open: 2, not 0.
        plan = schedule.due("deadline", now=NOW,
                            warehouse=schedule.Opened(problem="unreadable"),
                            settings=schedule.Settings())
        executor = RecordingExecutor()
        outcome = schedule.run(plan, executor)
        self.assertEqual(executor.ran, [])
        self.assertEqual(outcome.exit_code, schedule.EXIT_UNREADABLE)

    def test_two_failures_sharing_a_code_are_not_confused_for_each_other(self):
        # Matching the reported code back to a step by its number named the wrong one:
        # the tolerated brief here would have been reported as the failure that mattered.
        outcome = schedule.run(self.plan(
            schedule.Step("brief", (), "writes", tolerated=True),
            schedule.Step("snapshot", (), "captures")),
            RecordingExecutor({"brief": 3, "snapshot": 3}))
        self.assertEqual(outcome.reported.step.command, "snapshot")
        self.assertEqual([r.step.command for r in outcome.masked], ["brief"])
        self.assertIn("masked by snapshot's 3", schedule.render_outcome(outcome))

    def test_the_masked_failure_is_named_in_the_report(self):
        outcome = schedule.run(self.plan(
            schedule.Step("snapshot", (), "captures"),
            schedule.Step("notify", (), "pushes", tolerated=True)),
            RecordingExecutor({"snapshot": 3, "notify": 8}))
        text = schedule.render_outcome(outcome)
        self.assertIn("exiting 3", text)
        self.assertIn("masked by snapshot's 3", text)


class ExecutorTests(unittest.TestCase):
    """What the production executor would invoke, without invoking it."""

    def test_it_calls_the_console_script_the_way_the_shell_does(self):
        executor = schedule.SubprocessExecutor(agent=Path(".venv/bin/fpl-agent"))
        self.assertEqual(
            executor.argv(schedule.Step("settle", ("--gameweek", "3", "--learn"), "")),
            [".venv/bin/fpl-agent", "settle", "--gameweek", "3", "--learn"])

    def test_the_default_warehouse_is_not_named_on_the_command_line(self):
        executor = schedule.SubprocessExecutor(agent=Path("fpl-agent"),
                                               db=storage.DEFAULT_DB_PATH)
        self.assertEqual(executor.argv(schedule.Step("rivals", (), "")),
                         ["fpl-agent", "rivals"])

    def test_a_warehouse_that_is_not_the_default_is_passed_to_every_step(self):
        # The shell's own FPL_DB reached its queries and not the commands it ran, so a
        # non-default warehouse was planned from one database and written to another.
        executor = schedule.SubprocessExecutor(agent=Path("fpl-agent"),
                                               db=Path("/tmp/other.db"))
        self.assertEqual(executor.argv(schedule.Step("rivals", (), "")),
                         ["fpl-agent", "rivals", "--db", "/tmp/other.db"])


class SummariseTests(ScheduleTestCase):
    """The one line `status` ends its report on, which is a reader of the Plan.

    The point of it living here is that the line and `make now` cannot disagree: both
    come from the same plan. It used to be `status`'s own settle query and its own
    deadline window - a fourth statement of the pipeline.
    """

    def summary(self, **kwargs) -> str:
        return schedule.summarise(self.due("auto", **kwargs))

    def test_the_routine_capture_is_not_worth_a_line(self):
        # Every plan captures and projects. Saying so every time trains the reader to
        # skip the line, so what it names is what changes about today.
        self.kickoff(24 * 7)
        self.assertIn("nothing due", self.summary())

    def test_a_deadline_inside_the_window_is_named_with_its_hours(self):
        self.kickoff(10)
        self.assertIn("deadline in 8h", self.summary())

    def test_a_gradeable_gameweek_outranks_a_deadline(self):
        # Grading is the one that expires: the deadline comes round again, but a gameweek
        # left ungraded is a projection never scored against its result.
        self.kickoff(10)
        self.settleable_gameweek(4)
        summary = self.summary()
        self.assertIn("gameweek 4 ready to grade", summary)
        self.assertNotIn("deadline in", summary)

    def test_several_gradeable_gameweeks_are_all_named(self):
        self.settleable_gameweek(4)
        self.settleable_gameweek(5)
        self.assertIn("gameweeks 4, 5 ready to grade", self.summary())

    def test_a_warehouse_that_could_not_be_read_never_reads_as_nothing_due(self):
        # The capture is planned either way, so an absent settle step proves nothing here.
        plan = schedule.due("auto", now=NOW,
                            warehouse=schedule.Opened(problem="fpl.db is not a warehouse"),
                            settings=schedule.Settings())
        summary = schedule.summarise(plan)
        self.assertIn("could not tell", summary)
        self.assertNotIn("nothing due", summary)

    def test_it_never_promises_work_the_plan_does_not_hold(self):
        self.kickoff(10)
        plan = self.due("auto")
        summary = schedule.summarise(plan)
        if "ready to grade" in summary:
            self.assertTrue(any(step.command == "settle" for step in plan.steps))
        if "deadline in" in summary:
            self.assertTrue(any(step.command == "recommend" for step in plan.steps))


class CommandTests(ScheduleTestCase):
    """The entry point. A dry run writes nothing and says so.

    The one place the clock is not injected: `main` asks the real one, which is the whole
    point of everything below it taking `now` as an argument. So the fixtures here are
    anchored to the real clock too. Anchored to the fixed NOW instead, this file passed
    all morning and failed by the evening - a deadline five hours after noon has gone by
    18:30, and the plan correctly said so.
    """

    @property
    def anchor(self) -> datetime:
        return datetime.now(timezone.utc)

    def _db(self, tmp: str) -> Path:
        path = Path(tmp) / "fpl.db"
        with sqlite3.connect(path) as target:
            self.conn.backup(target)
        return path

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = schedule.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_dry_run_prints_the_plan_and_exits_zero(self):
        self.kickoff(5)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._db(tmp)
            before = path.stat().st_mtime_ns, path.stat().st_size
            code, out, _ = self._run(["deadline", "--dry-run", "--db", str(path)])
            self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), before)
        self.assertEqual(code, 0)
        self.assertIn("recommend", out)

    def test_the_hourly_job_on_a_host_with_no_warehouse_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._run(["deadline", "--dry-run",
                                    "--db", str(Path(tmp) / "fpl.db")])
        self.assertEqual(code, schedule.EXIT_UNREADABLE)

    def test_a_dry_run_never_reaches_the_executor(self):
        # Nothing in this file may run a step for real: the production executor launches
        # `fpl-agent snapshot`, which talks to the FPL API and writes a warehouse.
        class Exploding:
            def __init__(self, **kwargs):
                raise AssertionError("a dry run built an executor")

        self.kickoff(5)
        original = schedule.SubprocessExecutor
        schedule.SubprocessExecutor = Exploding
        self.addCleanup(setattr, schedule, "SubprocessExecutor", original)
        with tempfile.TemporaryDirectory() as tmp:
            code, _, _ = self._run(["deadline", "--dry-run", "--db", str(self._db(tmp))])
        self.assertEqual(code, 0)

    def test_an_unknown_job_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            self._run(["weekly", "--dry-run"])


if __name__ == "__main__":
    unittest.main()

"""What is due, as data.

The unattended half of this project - what runs, when, in what order - lived in a shell
script, and nothing it decided could be tested. Every job this module knows about had the
same three questions asked of it in bash and answered slightly differently in each place:
what is due, in what order, and what was deliberately not done.

`due` answers all three at once and returns a **Plan**: the ordered **Step**s, why each
one is due, and every **Skipped** item with the reason it was skipped. A Plan is produced
by reads only - no writes, no subprocesses, no clock of its own - so "what would run
tonight" is an assertion rather than a dry run against a live warehouse.

`run` takes a Plan and an executor and returns an **Outcome**, which holds the rule about
which failure wins: the *first* non-zero code, and a tolerated step's code only when
nothing else failed. Execution stays out of process - each command loads its own
configuration and one of them may launch a browser - so the executor is the seam, not the
process boundary.

Three jobs, and each is a different question:

    daily      capture the market that no endpoint returns later, then grade what has
               finished. Runs overnight, whether or not a deadline is near.
    deadline   only inside the window: re-capture and re-rank, because predicted lineups
               firm up on matchday. Runs hourly, so out of the window it plans nothing
               and costs one read.
    auto       both halves against a single capture, for a person at a terminal who does
               not want to work out whether today is a settling day or a deadline day.

Two states that must never render the same: *nothing is due* and *the question could not
be asked*. A warehouse that will not open is the second, and the renderer says so.

    fpl-agent schedule --dry-run daily      what is due, and why
    fpl-agent schedule daily                run it, reporting the first failure
"""

import argparse
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config

from . import settle, storage

JOBS = ("daily", "deadline", "auto")

# The one statement of "the deadline is near enough to rank for". It was three - the
# brief's, `status`'s, and the shell's own - and at 25 hours out they disagreed: cron
# ranked transfers for a deadline the brief declined to mention. The schedule owns the
# window now; `status` reads it from here.
DEADLINE_WITHIN_HOURS = 26

# The horizon `project` is asked for, everywhere a scheduled job asks for it.
PROJECTION_HORIZON = 3

# The warehouse could not be read at all - the same meaning code 2 carries in `status`
# and `notify`. Only the hourly job reports it: `daily` and `auto` have a capture to run
# regardless, and that capture is what creates a warehouse on a new host.
EXIT_OK = 0
EXIT_UNREADABLE = 2
# Tables a Plan is decided from. A file without them is not this project's warehouse, and
# saying so beats "no such table: projection" arriving from three lines deeper.
REQUIRED_TABLES = ("snapshot", "projection", "outcome", "fixture")


@dataclass(frozen=True)
class Settings:
    """The knobs a Plan depends on, passed as a value rather than read from the process.

    Injected for the same reason `now` is: a window is a decision worth testing at its
    edges, and a test that has to set an environment variable to reach one is a test that
    leaks into the next.
    """
    deadline_within_hours: int = DEADLINE_WITHIN_HOURS
    horizon: int = PROJECTION_HORIZON
    #: Whether a push target is configured. The engine decides this, not a regex over
    #: `fpl-agent.ini`: the shell grepped the file and so was a fourth place that could
    #: get it wrong - it saw neither `FPL_NTFY_TOPIC` in the environment nor a topic set
    #: any other way `notify` accepts.
    notifications_configured: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Settings for the process this is running in.

        The one place the schedule looks at process state, called from `main` and never
        from `due`, so a Plan stays a function of its arguments. `notify` owns the
        question of whether it is configured, so it is asked rather than re-answered;
        the import is local because `notify` reaches `status`, which reads this module.
        """
        from . import notify
        return cls(notifications_configured=notify.target_from_env() is not None)


@dataclass(frozen=True)
class Step:
    """One command a job would run, and why.

    `tolerated` says whether a failure here may be dropped rather than failing the run:
    reported only when nothing untolerated failed, and logged and dropped otherwise. The
    brief and the notification carry it, and nothing else does - every other step is
    either the capture, which is the irrecoverable one, or a decision made from it. The
    rule lives in `Outcome` and reads the flag from the Plan rather than holding its own
    list of which commands are which.
    """
    command: str
    args: tuple[str, ...] = ()
    reason: str = ""
    tolerated: bool = False
    #: The gameweek this step is about, where it is about one. Carried rather than parsed
    #: back out of `args` by whoever needs it: the number goes into the argv as a string,
    #: and reading it back means a second place that knows the flag's name and position.
    gameweek: Optional[int] = None

    @property
    def invocation(self) -> str:
        """What a caller would actually run, `fpl-agent` implied."""
        return " ".join((self.command, *self.args))


@dataclass(frozen=True)
class Skipped:
    """Something a job might have done and did not, with the reason it did not.

    Half the value of a Plan. A run that captured and ranked nothing, and a run that
    could not tell whether ranking was due, are the same empty step list and completely
    different states.
    """
    what: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """Everything a job would do at one moment, and everything it would not.

    `problem` is set when the warehouse could not be asked. It is not the same as an
    empty plan, and `render` never lets the two look alike.
    """
    job: str
    at: datetime
    steps: tuple[Step, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    problem: Optional[str] = None
    #: Hours to the next deadline as this Plan was decided, or None when the warehouse
    #: could not be asked or no fixture is left to play. Carried because a caller that
    #: summarises a Plan should not have to ask the question a second time and risk a
    #: different answer - which is the whole failure this module was built out of.
    hours_to_deadline: Optional[int] = None

    @property
    def exit_code(self) -> int:
        """0 unless the job had a question to ask and could not ask it.

        A cold-start `daily` still has its capture to run, so it is not a failure; an
        hourly `deadline` that cannot read the warehouse has nothing left to say and
        must not report the same 0 it reports on a quiet Tuesday.
        """
        if self.problem and not self.steps:
            return EXIT_UNREADABLE
        return EXIT_OK


@dataclass(frozen=True)
class Warehouse:
    """The warehouse as a value: an open connection, or the reason there is not one."""
    conn: Optional[sqlite3.Connection] = None
    problem: Optional[str] = None

    @property
    def readable(self) -> bool:
        return self.conn is not None


def open_warehouse(path: Path | str) -> Warehouse:
    """Open the warehouse read-only, turning every failure into a reason rather than a
    raise.

    Deliberately never `storage.connect`: that creates the file and runs the schema,
    which turns "there is no warehouse" into "there is an empty warehouse that looks
    healthy" - a confident report about a question nobody managed to ask.
    """
    path = Path(path)
    try:
        conn = storage.connect_readonly(path)
    except FileNotFoundError:
        return Warehouse(problem=f"no warehouse at {path} - nothing has been captured yet")
    except sqlite3.Error as e:
        return Warehouse(problem=f"could not open {path}: {e}")
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as e:
        conn.close()
        return Warehouse(problem=f"could not read {path}: {e}")
    absent = [table for table in REQUIRED_TABLES if table not in present]
    if absent:
        conn.close()
        return Warehouse(problem=f"{path} is missing {', '.join(absent)}, so it is not "
                                 f"an fpl-agent warehouse")
    return Warehouse(conn=conn)


# --------------------------------------------------------------------------
# The steps, each named once
# --------------------------------------------------------------------------

def _capture(settings: Settings, *, backfill: bool) -> list[Step]:
    """Snapshot, optionally backfill, project - and always project.

    These used to be gated separately, so the ordinary state of the warehouse between
    Tuesday and Friday was a snapshot with no projections: the thing `status` calls an
    inconsistency and exits 7 for, produced nightly. Projecting is cheap and reads only
    what the capture just stored, so a capture is not finished until it has been
    projected. The window below gates `rivals` and `recommend`, which is where the cost
    and the decisions are.
    """
    steps = [Step("snapshot", ("--force",),
                  "prices, ownership and the squad are current-state only; a day not "
                  "captured can never be recovered")]
    if backfill:
        # The hourly job skips this: it is refreshing a market, not learning a result.
        steps.append(Step("snapshot", ("--backfill-only",),
                          "actuals feed the projection's per-90 rates, so they have to "
                          "land before it runs"))
    steps.append(Step("project", ("--horizon", str(settings.horizon)),
                      "a capture with no projection is an inconsistency `status` exits "
                      "7 for, and projecting is cheap"))
    return steps


def _grading(warehouse: Warehouse) -> tuple[list[Step], list[Skipped]]:
    """Grade every gameweek that can be graded, oldest first.

    Which ones those are is asked of `settle.settleable_gameweeks` and never decided
    here. The scheduler asked its own SQL once and got it wrong twice over: it took the
    highest gameweek with *any* finished fixture, so on the Saturday of gameweek 4 it
    offered a round still being played and stepped over an ungraded gameweek 3 that would
    then never have been graded at all.
    """
    if not warehouse.readable:
        return [], [Skipped("grading", warehouse.problem)]
    pending = settle.settleable_gameweeks(warehouse.conn)
    if not pending:
        return [], [Skipped("grading", "no finished gameweek is waiting to be graded")]
    return [Step("settle", ("--gameweek", str(gameweek), "--learn"),
                 f"gameweek {gameweek} has finished and has never been graded",
                 gameweek=gameweek)
            for gameweek in pending], []


def _ranking() -> list[Step]:
    """The expensive half, and the only half that is actually deadline-shaped.

    It ends on `status`, which the scheduled job never used to run. Every step before it
    reports its own success; this is the one that checks the state they claim to have left
    behind - which is the whole lesson of this project's bug history. It is not tolerated,
    so its 7 is now reachable from an hourly cron mail, deliberately.
    """
    return [
        Step("rivals", (), "effective ownership has to exist before it can be judged; a "
                           "player in no rival squad is owned by 0%, not unknown"),
        Step("recommend", (), "the deadline is near enough that a ranking is worth having"),
        Step("status", (), "the run ends by checking the state it claims to have left "
                           "behind, rather than that each command exited zero"),
    ]


def _ranking_or_skip(warehouse: Warehouse, hours: Optional[int],
                     settings: Settings) -> tuple[list[Step], list[Skipped]]:
    """The ranking half if a deadline is near, else the reason it is not due."""
    what = "the ranking half"
    if not warehouse.readable:
        return [], [Skipped(what, warehouse.problem)]
    if hours is None:
        return [], [Skipped(what, "no fixture is left to play, so there is no deadline "
                                  "to rank against")]
    if hours < 0:
        return [], [Skipped(what, f"the last deadline passed {-hours}h ago and the round "
                                  f"is not confirmed finished yet")]
    if hours > settings.deadline_within_hours:
        return [], [Skipped(what, f"the next deadline is {hours}h away, further out than "
                                  f"{settings.deadline_within_hours}h; lineups are not "
                                  f"firm enough to rank on")]
    return _ranking(), []


def _tail(settings: Settings) -> tuple[list[Step], list[Skipped]]:
    """Write the brief, then push what is worth interrupting a person for.

    Both tolerate failure, which is the whole reason they are marked rather than
    special-cased: the snapshot is the irrecoverable asset and neither of these is. A
    dead ntfy server must never make a run that captured the market look like one that
    lost it, and a formatting problem in the brief must not mask a healthy capture.

    They were the shell's tail, outside the job dispatch, with the masking rule written
    by hand around them. Here they are ordinary Steps and the rule is `Outcome`'s.
    """
    steps = [Step("brief", (),
                  "the push carries only what is worth a phone buzzing; the brief is the "
                  "rest of the reasoning, and `logs/` is tracked so it survives",
                  tolerated=True)]
    if not settings.notifications_configured:
        # Skipped, not failed: notify is opt-in, and a host that has never set a topic
        # should not be mailed an error every hour.
        return steps, [Skipped("notify", "no ntfy topic is configured; notification is "
                                         "opt-in - see docs/SCHEDULING.md")]
    steps.append(Step("notify", (),
                      "each trigger is pushed once; the fingerprints already sent live "
                      "in the warehouse, which is what makes an hourly job safe to "
                      "notify from",
                      tolerated=True))
    return steps, []


# --------------------------------------------------------------------------
# The jobs
# --------------------------------------------------------------------------

def due(job: str, *, now: datetime, warehouse: Warehouse,
        settings: Settings = Settings()) -> Plan:
    """What `job` would do at `now`, given this warehouse and these settings.

    Reads only. Nothing here writes, and nothing here runs a subprocess, so a Plan is
    safe to produce anywhere - including hourly on a host where the answer is nothing.
    """
    if job not in JOBS:
        raise ValueError(f"unknown job: {job} (expected one of {', '.join(JOBS)})")

    steps: list[Step] = []
    skipped: list[Skipped] = []
    hours = (storage.hours_to_deadline(warehouse.conn, now) if warehouse.readable
             else None)

    if job in ("daily", "auto"):
        # The capture is due whatever the warehouse says - on a host that has none, it is
        # what creates one, and refusing to bootstrap is how a new server stays empty.
        steps += _capture(settings, backfill=True)
        graded, not_graded = _grading(warehouse)
        steps += graded
        skipped += not_graded

    if job in ("deadline", "auto"):
        ranking, not_ranking = _ranking_or_skip(warehouse, hours, settings)
        if job == "deadline" and ranking:
            # The hourly job re-captures rather than ranking over a stale market:
            # RotoWire firms predicted lineups up through matchday, so a projection built
            # 24 hours out and one built 3 hours out are different answers.
            steps += _capture(settings, backfill=False)
            skipped.append(Skipped(
                "the backfill", "the hourly job refreshes a market, not a result; "
                                "re-fetching a season of actuals twelve times a day "
                                "buys nothing"))
        steps += ranking
        skipped += not_ranking

    if steps:
        # Only over a job that did something. The shell writes the brief and evaluates
        # the triggers on every hourly wake-up, which over an idle Tuesday is 24 rewrites
        # of a tracked file about a warehouse nothing touched.
        tail, no_tail = _tail(settings)
        steps += tail
        skipped += no_tail

    return Plan(job=job, at=now, steps=tuple(steps), skipped=tuple(skipped),
                problem=warehouse.problem, hours_to_deadline=hours)


# --------------------------------------------------------------------------
# Running a Plan
# --------------------------------------------------------------------------

#: Anything that can run a Step and report its exit code. A subprocess in production, a
#: recording stub in the tests. The seam is here rather than at the process boundary,
#: because the precedence rule below has no test surface without it.
Executor = Callable[[Step], int]


@dataclass(frozen=True)
class Result:
    """What one Step did."""
    step: Step
    code: int

    @property
    def failed(self) -> bool:
        return self.code != 0


@dataclass(frozen=True)
class Outcome:
    """What a whole run did, and the single code it reports.

    **The first failure wins.** The shell assigned each step's code unconditionally, so
    the *last* failure was reported: a `snapshot` exiting 3 (no session) followed by a
    `project` exiting 1 reported 1, and a lost snapshot - the irrecoverable one - arrived
    in the cron mail wearing the code of something recoverable.

    A tolerated step's failure is reported only when nothing else failed. That is the
    shell's hand-written masking of `notify` behind the job's own code, generalised: the
    snapshot is the irrecoverable asset and a push is not, so a dead ntfy server can turn
    a 0 into an 8 and can never turn a 3 into one.
    """
    plan: Plan
    results: tuple[Result, ...] = ()

    @property
    def reported(self) -> Optional[Result]:
        """The one failure the run reports, or None when nothing failed.

        The winner is carried as the Result rather than as its integer, because two steps
        can fail with the same code and matching on the number then names the wrong one -
        in the very line that exists to stop an hour being spent on the wrong failure.
        """
        for result in self.results:
            if result.failed and not result.step.tolerated:
                return result
        for result in self.results:
            if result.failed:
                return result
        return None

    @property
    def exit_code(self) -> int:
        reported = self.reported
        if reported is not None:
            return reported.code
        # Nothing ran, or everything passed: the plan's own code still stands, which is
        # how an hourly job that could not read the warehouse reports 2 rather than 0.
        return self.plan.exit_code

    @property
    def failures(self) -> tuple[Result, ...]:
        return tuple(result for result in self.results if result.failed)

    @property
    def masked(self) -> tuple[Result, ...]:
        """Failures that happened and are not what is being reported."""
        reported = self.reported
        return tuple(result for result in self.failures if result is not reported)


def run(plan: Plan, executor: Executor) -> Outcome:
    """Run every Step in order and report what happened.

    A failing step does not stop the ones after it, which is today's behaviour and is
    deliberate: a `settle` that refuses an unfinished gameweek must not cost the run its
    brief, and a failed capture still leaves a warehouse worth reporting on. What changes
    is only which code comes out - see `Outcome`.
    """
    results = []
    for step in plan.steps:
        results.append(Result(step=step, code=executor(step)))
    return Outcome(plan=plan, results=tuple(results))


@dataclass(frozen=True)
class SubprocessExecutor:
    """Run each Step as its own process, the way the shell does.

    Process isolation is load-bearing, not incidental: each command loads its own
    configuration, sets up its own logging, opens and closes its own connection, and one
    of them may launch a browser. It is also what preserves the per-command exit codes
    for free - the table in `docs/SCHEDULING.md` is a contract between the commands and
    cron, not something this module invents.

    `--db` is appended only when it is not the default, so the ordinary deployment's
    invocation is exactly the one the shell makes today. The shell's own `FPL_DB` reached
    its queries and not the commands it ran, so a non-default warehouse was planned from
    one database and written to another.
    """
    agent: Path
    db: Optional[Path] = None

    def argv(self, step: Step) -> list[str]:
        argv = [str(self.agent), step.command, *step.args]
        if self.db is not None and Path(self.db) != storage.DEFAULT_DB_PATH:
            argv += ["--db", str(self.db)]
        return argv

    def __call__(self, step: Step) -> int:
        argv = self.argv(step)
        print(f"--- {' '.join(argv)}", flush=True)
        return subprocess.run(argv).returncode


def agent_binary() -> Path:
    """The console script to run steps with, overridable the way the shell allows."""
    return Path(os.environ.get("FPL_AGENT_BIN") or ".venv/bin/fpl-agent")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(plan: Plan, *, one_line: bool = False) -> str:
    """The Plan as a person reads it: every step with its reason, every skip with its own.

    `one_line=True` returns exactly the first line of the full rendering, for the caller
    that wants a summary - `status`'s `next:` line - without a second statement of what
    the schedule believes.
    """
    steps = f"{len(plan.steps)} step{'' if len(plan.steps) == 1 else 's'} due"
    if plan.steps:
        headline = f"{plan.job}: {steps}"
        if plan.skipped:
            headline += f", {len(plan.skipped)} skipped"
        if plan.problem:
            # A plan made without being able to read the warehouse is a partial answer,
            # and the summary line has to say so - it is the line a caller may print
            # instead of the rest.
            headline += " (the warehouse could not be read)"
    elif plan.problem:
        headline = f"{plan.job}: nothing due - the warehouse could not be read: {plan.problem}"
    else:
        headline = f"{plan.job}: nothing due"
        if plan.skipped:
            headline += f" - {plan.skipped[0].reason}"
    if one_line:
        return headline

    at = plan.at.isoformat(timespec="seconds")
    lines = [headline, "", f"fpl-agent schedule {plan.job}  planned at {at}"]
    if plan.problem and plan.steps:
        lines += ["", f"the warehouse could not be read: {plan.problem}"]
    if plan.steps:
        lines += ["", "due:"]
        width = max(len(step.invocation) for step in plan.steps)
        for number, step in enumerate(plan.steps, start=1):
            tolerated = "  (a failure here is tolerated)" if step.tolerated else ""
            lines.append(f"  {number}  {step.invocation:<{width}}  "
                         f"{step.reason}{tolerated}")
    if plan.skipped:
        lines += ["", "skipped:"]
        width = max(len(skip.what) for skip in plan.skipped)
        lines += [f"     {skip.what:<{width}}  {skip.reason}" for skip in plan.skipped]
    return "\n".join(lines)


def summarise(plan: Plan) -> str:
    """What is due, in the one line a reader gets who does not know the pipeline.

    `status` ends its report on this. It used to decide for itself - its own settle
    query, its own deadline window - which is a fourth statement of the pipeline and
    exactly how the scheduler and the engine came to disagree before.

    What it names is what a *person* has to know about. The capture, the projection and
    the brief are due every time and saying so every time would train the reader to skip
    the line; a gameweek sitting ungraded and a deadline coming up are the two facts that
    change what today looks like.
    """
    if plan.problem:
        # "Could not ask the question" and "nothing is due" must never render the same.
        # A plan made without a readable warehouse still holds the capture, which is due
        # regardless - so the absence of a settle step here is not evidence of anything.
        return f"could not tell what is due - {plan.problem}"
    grading = [step for step in plan.steps if step.command == "settle"]
    if grading:
        # Grading is the one that expires. A deadline comes round again; a gameweek left
        # ungraded is a projection never scored against the result it was made for.
        gameweeks = [str(step.gameweek) for step in grading]
        which = "gameweek" if len(gameweeks) == 1 else "gameweeks"
        return f"{which} {', '.join(gameweeks)} ready to grade - run `make now`"
    if any(step.command == "recommend" for step in plan.steps):
        # The hours cannot be absent while a ranking is due - the window is what put the
        # ranking in the plan - but saying "deadline in Noneh" if that ever stops being
        # true is worse than saying less.
        if plan.hours_to_deadline is None:
            return "a deadline is near - run `make now`"
        return f"deadline in {plan.hours_to_deadline}h - run `make now`"
    return "nothing due - `make now` is safe to run anyway and will say the same"


def render_outcome(outcome: Outcome) -> str:
    """What ran, what it exited, and which code is being reported - and why.

    A cron mail is read at 03:00 by somebody with no other context, so a masked failure
    is named rather than dropped silently. "notify exited 8, masked by snapshot's 3" is
    the line that stops an hour being spent on the wrong one.
    """
    lines = [f"{outcome.plan.job}: ran {len(outcome.results)} step(s)"]
    width = max((len(r.step.invocation) for r in outcome.results), default=0)
    for result in outcome.results:
        verdict = "ok" if not result.failed else f"exited {result.code}"
        if result.failed and result.step.tolerated:
            verdict += " (tolerated)"
        lines.append(f"  {result.step.invocation:<{width}}  {verdict}")
    lines.append("")
    code = outcome.exit_code
    reported = outcome.reported
    if reported is None:
        lines.append("every step succeeded; exiting 0" if not code else
                     f"nothing ran and the plan itself could not be made; exiting {code}")
        return "\n".join(lines)
    lines.append(f"exiting {code}, from {reported.step.command} - the first failure, and "
                 f"the one worth acting on. See the exit-code table in "
                 f"docs/SCHEDULING.md.")
    for result in outcome.masked:
        lines.append(f"  also: {result.step.invocation} exited {result.code}, masked by "
                     f"{reported.step.invocation}'s {reported.code}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fpl-agent schedule",
        description="Say what a scheduled job is due to do, and why.")
    parser.add_argument("job", choices=JOBS)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and touch nothing")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    # Before the settings are read, since the ini is how a topic is usually configured
    # and whether one is decides whether `notify` is a Step at all.
    config.load()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    warehouse = open_warehouse(args.db)
    try:
        plan = due(args.job, now=datetime.now(timezone.utc), warehouse=warehouse,
                   settings=Settings.from_env())
    finally:
        # Closed before anything runs: the steps open the warehouse themselves, and each
        # of them writes to it.
        if warehouse.conn is not None:
            warehouse.conn.close()

    print(render(plan))
    if args.dry_run:
        print("\nnothing above was run.")
        return plan.exit_code

    print()
    outcome = run(plan, SubprocessExecutor(agent=agent_binary(), db=args.db))
    print(render_outcome(outcome))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

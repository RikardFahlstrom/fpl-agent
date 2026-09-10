"""What is due, as data.

The unattended half of this project - what runs, when, in what order - lived in a shell
script, and nothing it decided could be tested. Every job this module knows about had the
same three questions asked of it in bash and answered slightly differently in each place:
what is due, in what order, and what was deliberately not done.

`due` answers all three at once and returns a **Plan**: the ordered **Step**s, why each
one is due, and every **Skipped** item with the reason it was skipped. A Plan is produced
by reads only - no writes, no subprocesses, no clock of its own - so "what would run
tonight" is an assertion rather than a dry run against a live warehouse.

Nothing here executes a Step. Running a Plan, and the rule about which failure wins, is
the next piece; a Step's `tolerated` flag is where that rule will read whether a failure
may be dropped.

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

    fpl-agent schedule --dry-run daily
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
EXIT_USAGE = 64

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


@dataclass(frozen=True)
class Step:
    """One command a job would run, and why.

    `tolerated` says whether a failure here may be dropped rather than failing the run.
    Nothing in a Plan tolerates failure today - every step below is either the capture,
    which is the irrecoverable one, or a decision made from it. The flag exists because
    the pieces that do tolerate failure (the brief, the notification) become Steps when
    running a Plan lands, and the precedence rule has to read the answer from the Plan
    rather than hold its own list.
    """
    command: str
    args: tuple[str, ...] = ()
    reason: str = ""
    tolerated: bool = False

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
    problem: Optional[str] = field(default=None)

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
# Reading the two facts a Plan turns on
# --------------------------------------------------------------------------

def hours_to_deadline(conn: sqlite3.Connection, now: datetime) -> Optional[int]:
    """Hours from `now` until the next deadline, or None when no fixture is unplayed.

    Measured against the time passed in rather than the database's `now`, which is what
    makes a window testable at its edges. Truncated toward zero, and negative is a real
    answer: a round whose deadline has passed but whose fixtures FPL has not confirmed
    finished sits there for hours.
    """
    deadline = storage.next_deadline(conn)
    if deadline is None:
        return None
    return int((deadline - now).total_seconds() / 3600)


def _settleable(warehouse: Warehouse) -> list[int]:
    return settle.settleable_gameweeks(warehouse.conn)


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
    pending = _settleable(warehouse)
    if not pending:
        return [], [Skipped("grading", "no finished gameweek is waiting to be graded")]
    return [Step("settle", ("--gameweek", str(gameweek), "--learn"),
                 f"gameweek {gameweek} has finished and has never been graded")
            for gameweek in pending], []


def _ranking() -> list[Step]:
    """The expensive half, and the only half that is actually deadline-shaped.

    It ends on `status`, which cron does not run today. Every step before it reports its
    own success; this is the one that checks the state they claim to have left behind -
    which is the whole lesson of this project's bug history.
    """
    return [
        Step("rivals", (), "effective ownership has to exist before it can be judged; a "
                           "player in no rival squad is owned by 0%, not unknown"),
        Step("recommend", (), "the deadline is near enough that a ranking is worth having"),
        Step("status", (), "the run ends by checking the state it claims to have left "
                           "behind, rather than that each command exited zero"),
    ]


def _ranking_or_skip(warehouse: Warehouse, now: datetime,
                     settings: Settings) -> tuple[list[Step], list[Skipped]]:
    """The ranking half if a deadline is near, else the reason it is not due."""
    what = "the ranking half"
    if not warehouse.readable:
        return [], [Skipped(what, warehouse.problem)]
    hours = hours_to_deadline(warehouse.conn, now)
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

    if job in ("daily", "auto"):
        # The capture is due whatever the warehouse says - on a host that has none, it is
        # what creates one, and refusing to bootstrap is how a new server stays empty.
        steps += _capture(settings, backfill=True)
        graded, not_graded = _grading(warehouse)
        steps += graded
        skipped += not_graded

    if job in ("deadline", "auto"):
        ranking, not_ranking = _ranking_or_skip(warehouse, now, settings)
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

    return Plan(job=job, at=now, steps=tuple(steps), skipped=tuple(skipped),
                problem=warehouse.problem)


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


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fpl-agent schedule",
        description="Say what a scheduled job is due to do, and why.")
    parser.add_argument("job", choices=JOBS)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and touch nothing (the only mode today)")
    parser.add_argument("--db", type=Path, default=storage.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    if not args.dry_run:
        # Running a Plan is a separate piece of work, and the rule about which failure
        # wins belongs with it. Saying so beats a command that silently plans when it was
        # asked to act - this project's own recurring bug wearing a new hat.
        print("schedule can only plan today; running a plan is not wired up yet. "
              "Re-run with --dry-run to see what is due.", file=sys.stderr)
        return EXIT_USAGE

    warehouse = open_warehouse(args.db)
    try:
        plan = due(args.job, now=datetime.now(timezone.utc), warehouse=warehouse,
                   settings=Settings())
    finally:
        if warehouse.conn is not None:
            warehouse.conn.close()

    print(render(plan))
    print("\nnothing above was run.")
    return plan.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

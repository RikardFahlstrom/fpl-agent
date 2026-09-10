# Terms this project uses precisely

One definition each, so that the next reader finds the meaning rather than inferring it
from five call sites. Only terms that have actually been resolved by work are here; the
file grows when a term gets settled, not in advance. What each term means is what the code
does today — where a term is still unsettled, this file says so rather than describing the
intention.

The scheduling terms below were settled by the work that moved the unattended pipeline out
of `deploy/fpl-cron.sh` and into `engine/schedule.py`.

## job

One of `daily`, `deadline`, `auto` — `schedule.JOBS`. Besides `--dry-run`, it is the only
argument the cron entry point takes.

A job is not a schedule. Cron's clock decides *when* something is asked; the job decides
*what is considered*. `daily` considers the capture and the grading; `deadline` considers
the ranking, and only inside the window; `auto` considers both against a single capture,
for a person at a terminal who does not want to work out which kind of day it is. Each of
the three is safe to ask on a day when the answer is nothing, which is most days.

## due

The question `schedule.due(job, *, now, warehouse, settings)` answers: what would this job
do, at this moment, given this warehouse.

Answering it **reads and nothing else** — no writes, no subprocesses, and no clock of its
own. The time, the warehouse and the settings are arguments, which is what makes "what
would run tonight" an assertion in a test rather than a dry run against a live database.
`due` never decides what is gradeable or when a deadline falls: it asks
`settle.settleable_gameweeks` and `storage.hours_to_deadline`, because those rules already
exist and a second statement of one is how the scheduler and the engine came to disagree.

## Plan

The answer `due` returns: the ordered `Step`s that are due, every `Skipped` item with the
reason it was skipped, the job and the time it was decided at, why the warehouse could not
be read if it could not, and the hours to the next deadline it decided from.

A Plan is data. Nothing about holding one runs anything — `run` does that, separately and
later. Two of its parts carry weight beyond their obvious use:

- **The skips are half the value.** A run that had nothing to do and a run that could not
  find out are the same empty step list and completely different states, and only the
  reasons tell them apart.
- **`problem` is not "empty".** A `daily` or `auto` Plan made without a readable warehouse
  still holds the capture, because on a host with no warehouse the capture is what creates
  one; a `deadline` Plan holds nothing at all and reports 2, because an hourly job that
  could not ask the question has nothing to say. Either way the absence of a grading step
  there is evidence of nothing, so every reader of a Plan has to check `problem` before
  concluding that nothing is due.

## Step

One command a job would run, with the arguments to run it with, the reason it is due,
whether its failure may be tolerated, and — where the step is about one — the gameweek it
concerns, carried as a field so that nobody has to parse it back out of the arguments.

A Step is one process. Execution stays out of process deliberately: each engine command
loads its own configuration, sets up its own logging, opens and closes its own connection,
and one of them may launch a browser — and process isolation is also what preserves the
per-command exit codes that `docs/SCHEDULING.md` promises cron.

**Tolerated** means a failure here is reported only when nothing untolerated failed. The
brief and the notification are the tolerated steps: the snapshot is the irrecoverable
asset and neither of those is, so a dead ntfy server can turn a 0 into an 8 and can never
turn a 3 into one.

## capture

One `snapshot` row and everything hanging off it — the market, your squad, the fixtures
and the predicted lineups as they stood at that moment. It is the irrecoverable asset:
`bootstrap-static` serves current state only, so a day not captured can never be
recovered, and every projection is tied to the capture it was made from.

Inside the schedule, "the capture" is a step group rather than a single step —
`snapshot --force`, the backfill on the jobs that learn from results, and `project`.
A capture is not finished until it has been projected: leaving those separately gated is
what used to leave the warehouse holding a snapshot with no projections between Tuesday
and Friday, which `status` calls an inconsistency and exits 7 for.

**Still unsettled, deliberately.** *Which* capture a later reader means is derived
independently in six modules (`brief`, `lineups`, `pricing`, `projection`, `recommend`,
`status`), and those derivations do not all ask the same question — the latest snapshot,
the latest holding lineups for a gameweek, the one targeting a gameweek, and "the latest
unless told otherwise" are four different things. Naming that properly is its own piece of work with its own spec. This entry is
vocabulary; it moves no code.

## The seam

`engine/schedule` is tested through **the executor passed to `run`** — a callable taking a
`Step` and returning its exit code. `SubprocessExecutor` in production, a recording class
in the tests that captures the invocations and returns scripted codes.

That is the seam, and it is the only one. The rule about which failure a run reports — the
first non-zero, with a tolerated step's code counting only when nothing else failed — has
no test surface without it. That rule was a live bug in the shell this came from: it
assigned each step's code unconditionally, so a lost snapshot arrived in the cron mail
wearing the code of whatever recoverable thing failed after it.

Test through it rather than inventing a second one. In particular, nothing in the suite may
run a Step for real: the production executor launches `fpl-agent snapshot`, which talks to
the FPL API and writes a warehouse.

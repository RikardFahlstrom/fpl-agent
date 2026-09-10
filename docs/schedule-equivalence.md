# The schedule module against the script it replaces

`deploy/fpl-cron.sh` has no tests, so "the new module behaves like the old script" was
unfalsifiable — and three of the changes in this rewrite alter the behaviour of an
unattended job that mails its owner on failure. This is the comparison, run before any
shell logic is deleted, so those three are deliberate rather than discovered in a 03:00
cron mail.

Re-run it whenever either side changes:

```sh
tools/schedule-equivalence.sh     # what each side would run, per state
tools/schedule-precedence.sh      # which exit code wins when steps fail
```

Both read only. Every state is a copy of `data/fpl.db` mutated into the shape being
tested, the first script runs both sides in dry-run mode, and the second replaces the
console script with a stub that exits with scripted codes.

## What each side would run

Six states × three jobs, run 2026-09-10 against a copy of the real warehouse.
Fourteen of the eighteen agree exactly, step for step and code for code.

| State | `daily` | `deadline` | `auto` |
| --- | --- | --- | --- |
| no deadline (every fixture played) | same | **diverges (1)** | same |
| a deadline outside the window (46h) | same | **diverges (1)** | same |
| a deadline inside it (3.5h) | same | **diverges (2)** | **diverges (2)** |
| a gameweek pending grading | same | **diverges (1)** | same |
| no warehouse at all | same, exit 0 | **diverges (1)**, exit 2 both | same, exit 0 |
| an unreadable warehouse | same, exit 0 | **diverges (1)**, exit 2 both | same, exit 0 |

Worth noting what "same" covers: the capture order, the backfill only on the daily half,
`settle --gameweek N --learn` for the same gameweek the shell picked (4 in one state, 3 in
another), `rivals` before `recommend`, `notify` skipped for the same reason on a host with
no topic, and the cold-start behaviour — `daily` and `auto` still plan the capture that
creates a warehouse, while the hourly job exits 2.

## The divergences, each deliberate

**(1) An idle hourly run plans nothing; the shell still writes the brief.** The shell's
tail runs `brief` and `notify` after every job, including a `deadline` job that decided
there was nothing to do — 24 rewrites a day of a tracked file about a warehouse nothing
touched, on a state that has not changed since the last one. A Plan with nothing due is
now nothing due. Nothing is lost: the triggers are fingerprinted, so an hourly re-
evaluation of unchanged state sends nothing anyway, and the `daily` job still writes the
brief every night.

**(2) The deadline job ends by checking what it left behind.** The plan closes the ranking
half with `status`; the shell never runs it. This is the point of the change — every
command before it reports its own success, and `status` is the one that checks the state
they claim to have left behind. It exits 7 on an inconsistency, so a deadline run over a
warehouse that disagrees with itself now reports 7 where the shell reported 0. That is the
intended behaviour and the reason the step exists.

## Which exit code wins

The comparison a dry run cannot make. Stub codes, same warehouse copy, both sides:

| Failure pattern | Shell | Plan | |
| --- | --- | --- | --- |
| `snapshot` 3, then `project` 1 | 1 | **3** | **diverges — the point of the ticket** |
| `project` 1, then `settle` 6 | 1 | 1 | same |
| `snapshot` 4 alone | 4 | 4 | same |
| `brief` 2 alone | 0 | **2** | **diverges (3)** |
| `snapshot` 3 and `brief` 2 | 3 | 3 | same |
| nothing fails | 0 | 0 | same |

**The first failure wins.** The shell assigned each step's code unconditionally, so the
*last* failure was reported: a `snapshot` that exited 3 with no session, followed by a
`project` that exited 1, arrived as a 1. That is the recoverable code standing in for the
irrecoverable failure — the precise outcome the comment in the `auto` job says the guard
there exists to prevent. This is the expected divergence, and it is why the comparison
exists.

**(3) A failed brief now surfaces on an otherwise-clean run.** The shell logs the brief's
code and drops it always; a tolerated step here reports its code when every other step
succeeded, which is `notify`'s rule made general. Safe because `brief` only ever returns 0
or 2, and 2 already means exactly what it would mean here — the warehouse could not be
read at all. It still cannot mask anything: a real failure always outranks it, which the
row above pins.

## What is not compared

The lock, which stays in the shell and is about the rotating credential rather than the
database; and the crontab, which is untouched by design. Neither is a decision this module
makes.

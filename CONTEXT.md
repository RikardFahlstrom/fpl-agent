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
do, at this moment, given this *reading* of the warehouse.

Answering it is **a function of its arguments** — no writes, no subprocesses, no queries,
and no clock of its own. The time, the reading and the settings are arguments, which is
what makes "what would run tonight" an assertion in a test rather than a dry run against
a live database. `due` never decides what is gradeable or when a deadline falls: the
reading carries the *ledger*'s answer (`settleable`) and the *target gameweek*'s deadline, and
`due` applies `storage.hours_until` to the latter with the `now` it was given, because
those rules already exist and a second statement of one is how the scheduler and the
engine came to disagree.

## reading

What the warehouse said when asked the three questions a Plan is decided from, or the
reason it could not be asked: `schedule.Reading` — `problem`, `deadline` (the *target
gameweek*'s, from `warehouse.target`),
`settleable`, `league_known`. `schedule.read(conn)` produces one from an open
connection, through the owner of each fact; `schedule.read_warehouse(path)` opens the
file read-only, reads, and closes it before returning, turning a missing or foreign file
into a `problem` rather than a raise.

A reading is a value, not a seam. It carries the deadline as a moment rather than as
hours so that it does not depend on when it was taken: `due` is the one place the window
is decided, from the one clock it was handed. A test hands `due` a `Reading` directly;
the SQLite-backed tests are there to prove `read` asks the right questions.

## Plan

The answer `due` returns: the ordered `Step`s that are due, every `Skipped` item with the
reason it was skipped, the job and the time it was decided at, why the warehouse could not
be read if it could not, and the hours to the *target gameweek*'s deadline it decided
from.

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
`snapshot --force`, the backfill on the jobs that learn from results, `project`, and the
league table refresh (`rivals --standings-only`, planned once a league is known) — and,
on the jobs that learn from results, the rival picks when they are behind the last
finished gameweek, because *stale ownership* is withheld rather than shown.
A capture is not finished until it has been projected: leaving those separately gated is
what used to leave the warehouse holding a snapshot with no projections between Tuesday
and Friday, which `status` calls an inconsistency and exits 7 for. The table refresh is
in the group for the mirror reason: gated with the rival picks behind the deadline
window, it sat five days stale between deadlines.

*Which* capture a later reader means is `engine/warehouse`'s question, and it has four
answers, each a `Capture` value or None (a fifth question, which rival picks ownership
is measured from, is under *stale ownership*):

- `latest` — the one the pipeline is *in*: what `recommend` prices against, `brief`
  describes, `status` checks, `project` writes to. "The latest unless told otherwise"
  (`pricing.price_outlooks`) is this with an override.
- `with_lineups(n)` — the most recent capture holding predicted lineups for gameweek
  `n`, which is often older than `latest` and need not target `n`, because lineups are
  filed per fixture and RotoWire publishes near matchday.
- `with_squad` — the most recent capture that logged in; a market-only capture has no
  `my_state` row and no entry id.
- `projected(n, version)` — the most recent capture *targeting* `n` with projections of
  it under `version`: the decision-time record `settle` grades. A projection of `n`
  made from a capture targeting `n - 1` is a horizon row, not that record.

Readers ask the module rather than the `snapshot` table, so that `status` and `lineups`
agree on the lineup source by construction rather than by a comment saying they should.
`engine/warehouse` is where "what the warehouse holds" lives; a reader that wants a fact
about a capture or a gameweek asks it there, not the tables.

## target gameweek

The gameweek decisions are being made for: the latest capture's gameweek, which is FPL's
`is_next` when it was taken — the one `recommend` prices, `brief` describes and `status`
checks. `warehouse.target(conn)` reads the capture, its gameweek and that gameweek's
deadline together, and `warehouse.deadline(conn, n)` is the one statement of when a
gameweek locks: `storage.DEADLINE_BEFORE_KICKOFF` before its first kickoff, played or
not. `schedule`, `status --hours-to-deadline` and `brief` all read it there.

A command that cannot proceed without a gameweek asks `warehouse.require_target(conn,
gameweek)` instead: the same read, refusing in one wording when nothing is captured or
the capture has no gameweek, and carrying a gameweek the caller named rather than
swapping it for the capture's. `project`, `recommend`, `brief` and `notify` all resolve
through it. Whether the latest capture can answer for a named gameweek is the caller's
call: `project` projects any gameweek from it, while `recommend` refuses any but the
target's, because bank, free transfers and selling prices exist only for the moment
the capture was taken — so `brief --gameweek N` for another N says no move is priced
rather than showing the target's.

It is not "the lowest gameweek with a fixture to play". That was the scheduler's rule
until #97, and mid-round it put a deadline before each remaining match. Between the
deadline passing and the next capture, the target's deadline is in the past; the hourly
job reads that as "passed" and ranks nothing, which is the intended skip.

## ledger

What the warehouse holds for every gameweek, read once as a value:
`warehouse.gameweeks(conn, model_version) -> GameweekLedger`. Per round — `fixtures`,
`played`, `actuals`, `projected` (a capture *targeting* the round projected it under
`model_version`), `graded` (outcome rows under `model_version`) — and derived from them
*finished* (every fixture played; no fixtures recorded is not finished) and
*has_actuals* (at least eleven a side per played fixture, and zero never passes). The
ledger's `finished()` and `settleable()` (finished, projected, not graded; oldest first)
are the one statement of "grade a gameweek only once it has finished".

It is a value, not a seam: `settle` reads it for its two refusals and for `--list`,
`status` reads it once in `gather` and hands it to the checks, and `schedule.read` asks
it for `settleable` on the way to a *reading*. None of them holds a rule of its own — the fourth
copy, in `deploy/fpl-cron.sh`, is the one that offered a round still being played and
stepped over an ungraded one. `model_version` is a parameter so that a bump can compare
two ledgers.

## The seam

`engine/schedule` is tested through **the executor passed to `run`** — a callable taking a
`Step` and returning its exit code. `SubprocessExecutor` in production, a recording class
in the tests that captures the invocations and returns scripted codes.

That is the seam, and it is the only one: the only place a test substitutes behaviour for
the real thing. Deciding a Plan needs no seam, because `due` takes values — a *reading*, a
time, the settings — and a value is handed over, not stood in for. The rule about which
failure a run reports — the first non-zero, with a tolerated step's code counting only
when nothing else failed — has no test surface without the executor. That rule was a live bug in the shell this came from: it
assigned each step's code unconditionally, so a lost snapshot arrived in the cron mail
wearing the code of whatever recoverable thing failed after it.

Test through it rather than inventing a second one. In particular, nothing in the suite may
run a Step for real: the production executor launches `fpl-agent snapshot`, which talks to
the FPL API and writes a warehouse.

The output terms below were settled by the rework of the brief and the push (September
2026), when the brief gained its fixed opening block and the push its three states.

## brief

`logs/gwNN.md`: everything the warehouse knows that a person needs before a deadline,
rewritten every run, read on a phone. It opens with the same block of lines in the same
order regardless of what happened — the move, the ownership of that move, the wildcard,
the availability of the squad, the deadline, the push, the learnings, the data — each
line spelling out "none" or "not evaluated" when there is nothing, so that the reader
looks at the same line every time rather than reading the whole page to find out that
nothing happened. Beneath the block come the sections that show the working.

The brief is written in plain English: no name from the code, no level, no slice id
appears without its meaning beside it on first use. Internal names survive only where a
command has to be typed. A reader who has to open the code to understand the brief will
stop reading the brief.

The brief is rendered from one value, `brief.Evaluation`: everything the page prints -
the triggers and why each silent one stayed silent, the checks, the ranking, the chip
verdicts, the captain, the ownership source, the price watch, the last settled
gameweek, the learnings on disk and what became of each *push* - read once by
`evaluate` and handed to `render_brief` and `render_block`, which take no connection,
no clock and no directory. That is what keeps the opening block and the working beneath
it from disagreeing: they cannot read different warehouses because neither reads one.
`notify` evaluates the same way and sends from the same triggers.

## push

A message to the owner's phone about one trigger, and the record of whether it got there.
Every trigger is in one of three states on every run, and the words are not
interchangeable:

- **did not fire** — the condition was not met. Most triggers, most days.
- **sent** — the phone got it, now or on an earlier run (the fingerprint is in the
  `notification` table); when, is part of the state.
- **fired, not delivered** — the condition was met and the message did not reach the
  phone: no topic configured, or the server refused. This is a *data* problem and is
  reported as one, because a push that fired and went nowhere must never look like one
  that had nothing to say.

## stale ownership

Ownership is *effective ownership*: the share of rival squads in the configured leagues
that hold a player, which is what makes a differential a differential. It is fresh only
when the rival picks it is measured from are for the last finished gameweek or later —
picks for a round exist only after its deadline, so before the GW5 deadline the freshest
possible picks are GW4's. Picks older than the last finished gameweek are **stale**, and
stale ownership is not shown: not in the brief, the table, the push or the ranking. The
line says instead which gameweek rivals were last captured for. Never captured is
reported the same way. A number from two rounds ago is not a caveat, it is a wrong
number at exactly the point the edge lives.

Whether the picks are fresh is one answer, `warehouse.ownership(conn, ledger,
league_ids) -> OwnershipSource`: the gameweek the picks are for, how many rivals in
the configured leagues (`config.rival_leagues`) hold them, and the ledger's last
finished gameweek. `recommend` withholds on it, `status` warns on it, the scheduler's
*reading* carries it to decide the `rivals` step, and `rivals` captures the ledger's
last finished gameweek so that the capture is what makes the answer fresh. None of
the four holds a comparison of its own: three of them did, each docstring promising to
match the others, and the capture followed a fourth rule (the highest round with
actuals) that could lag the ledger by a backfill. Rivals captured for a league other
than the configured ones count as never captured, which for the number shown they are.

## availability

Whether each of the fifteen players you own can be expected to play, from two sources
that disagree usefully: FPL's own flag (`i` injured, `s` suspended, `d` doubtful with a
chance) and the predicted lineup, which catches rotation FPL never reports. "15 of 15"
means neither source names anyone; otherwise the names, each with its reason.

## learning

One drafted finding from `settle --learn`: a calibration slice that deviated enough to
be written down, filed in `learnings/` as `proposed`. It is a claim about the model, not
about the gameweek, and it stays `proposed` until a person applies it — a weight changes,
`MODEL_VERSION` is bumped and the file says `applied` — or rejects it with a reason. Nothing accepts a learning
on its own; the `/fpl-learn` skill does it when the owner says so, in plain words, one
learning at a time. Two drafts naming the same slice in consecutive rounds are the
signal a single draft asks the reader to wait for, and the brief says so when it
happens.

## held squad

The fifteen you own and the rules they are held under, read once from a capture:
`held.read(conn, capture) -> HeldSquad` - each player's squad position, element type,
club, selling price and multiplier; the bank, free transfers and the hit; the chip
payload, parsed once into `ChipState`s; the entry id; and the club limit from
`game_config`. None for a capture that logged no squad. `budget` is the bank plus
selling prices, what a rebuild can spend; `xi` and `bench` split it at `STARTING_XI`;
`active_transfer_chip` is the chip that suspends hits this gameweek.

It is a value: the transfer ranking, the *chip value*s, the captain pick, the brief's
availability table and `rivals`' own-entry lookup all take it rather than querying
`my_squad`, `my_state` and `game_config` themselves, which is how the chip rebuilds came
to ignore the club limit the transfers honoured. `with_move` is the held squad after a
recommended transfer - the incoming player in the outgoing one's slot, bought at today's
price - which is the squad chips are valued over.

The rules a squad is held under are stated in `engine/held` once: `STARTING_XI`,
`BENCH_VALUE` (what a bench slot is worth, to a transfer and to a rebuilt squad alike),
`BUYABLE` (FPL statuses `a` and `d`) and `DEFAULT_TEAM_LIMIT`.

## chip

One of FPL's four - bench boost, triple captain, free hit, wildcard - as FPL reports it for
the entry: a status (available, active, played) and a window (`start_event` to
`stop_event`). Two sets a season, each with a wall; nothing in the code hardcodes that,
the window is read from the payload. A chip is *evaluable* when it is available and its
window covers the gameweek; anything else is reported as it is (played in GW3, not until
GW20, expired) and never valued.

## chip value

What a chip would add in one gameweek, on the squad after the recommended move: the
bench's projected points (bench boost), the best XI player's projected points (triple
captain), the best legal rebuilt squad against the held one for that week (free hit) or
over the wildcard horizon (wildcard). One value per week from the target to the wall,
read from stored projections - which is why the capture projects the *chip window*.

## chip window

Every gameweek from the target to the wall of the set in play, projected and stored on
each capture so a verdict is re-readable later. Only the next gameweek has predicted
lineups; the rest are rates against fixtures, which is why the current week tends to
look best and why a verdict needs two gates: the chip's *bar* (a stated number of
points) and being the best remaining week within a noise band. Under the bar is hold,
whatever the window says.

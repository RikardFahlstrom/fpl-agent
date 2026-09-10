---
name: fpl-deadline
description: Run the pre-deadline cycle - capture, project, and rank transfers - and interpret the result. Use before an FPL gameweek deadline, or when asked what transfer to make.
---

# Pre-deadline cycle

Run `make deadline`. It runs the deadline half of `engine/schedule`'s plan: a fresh
capture, the three-gameweek projection, rival squads, the transfer ranking, and then
`status` over what that left behind. The order matters - rivals must exist before ownership
means anything - and it is stated once, in the schedule, so this file does not restate it.

**It does nothing when no deadline is near**, which is the same answer the hourly cron job
gives and is not a failure. `make deadline DRY=--dry-run` shows the decision without acting
on it, and prints the reason each step is due or was skipped.

It deliberately does not backfill actuals: this half is refreshing a market, not learning a
result. `make now` is the one that does both.

Then interpret. The numbers are not the answer.

## Check the warehouse first

The plan ends on `status`, so a `make deadline` that ranked has already run it - read
those lines before reading a single recommendation. Run `.venv/bin/fpl-agent status` by
hand when you are reading a ranking from an earlier run. It is read-only - it authenticates against nothing and writes nothing -
and it answers in one command what used to be three SQL queries and a hope.

Exit 0 means the warehouse is consistent and the recommendations rest on something. Any
non-zero exit names a specific inconsistency; the code table is in `docs/SCHEDULING.md`
and is shared with `deploy/fpl-cron.sh`, so a failure here is the same failure cron would
have mailed you. Read the code, do not re-derive it.

**Do not read the recommendations over a non-zero status.** The output will look entirely
normal - ranked moves, prices, gains - because every one of those numbers is computed
whether or not the inputs are sound. That is exactly the failure this project keeps
hitting: a confident answer to a question the data could not support.

What has to be true before a recommendation means anything, and why each one poisons the
output when it is not. Whatever `status` does not cover, check by hand:

- **The squad was captured.** Without it there are no selling prices, so the budget is
  wrong and so is every recommendation resting on it. A snapshot that failed to log in
  still captures the market half, so the run looks like it worked.
- **The projection targets this gameweek.** That is the one settle will grade later. A
  projection carried over from an earlier gameweek is not a decision-time projection.
- **Rivals are captured for the most recent finished gameweek.** Ownership necessarily
  lags: rival squads only become public after a deadline, so you are seeing what the
  league owned last week. Say so rather than implying it is current.

## Reading the output

**Two numbers, deliberately separate.** Expected-points gain says whether the move is
worth making. Urgency says whether the chance to make it is disappearing. A modest upgrade
whose window closes tonight is a different decision from a large upgrade that can wait.
Never blend them into one score.

**Price urgency runs on its own clock.** Changes resolve nightly, not at the gameweek
deadline, so "act tonight" can be true days before the deadline and false hours before it.

**Ownership is league-relative.** A differential is only a differential against the people
being played. Weigh it by league position: chasing from the bottom justifies variance that
a leader should refuse.

**Selling a template player is a risk even when the swap gains points** - if he hauls, the
whole league gains on you. That is reported separately for a reason.

## When not to act

- The model is **uncalibrated** until several gameweeks have settled. Treat gaps as
  directional, not precise, and say so.
- A suspiciously round or extreme number is usually a bug. Two players at exactly 6.00, or
  a reserve keeper among the best value in the game, have both happened.
- One free transfer and no urgent problem is often a bank, not a move.

## Afterwards

Run `make record` once the transfer has actually been made. It logs the top-ranked move to
`logs/actions.jsonl`, creating the file and its directory on the first run, and that file
is committed. `make deadline` deliberately does not record: recording is a claim about
what was done, not about what was suggested. If the move you made was not the top-ranked
one, edit the recorded line rather than leaving a decision the log misattributes.

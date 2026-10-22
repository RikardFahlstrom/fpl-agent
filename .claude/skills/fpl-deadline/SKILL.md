---
name: fpl-deadline
description: Run the pre-deadline cycle - capture, project, and produce the Instructions (moves, chip, captain, XI, bench) - and interpret the result. Use before an FPL gameweek deadline, or when asked what transfer to make.
---

# Pre-deadline cycle

Run `make now`. It is the one manual job: a fresh capture (never skipped, because the
market it records cannot be recovered), the actuals backfill, the three-gameweek projection,
the rival squads and league table, any gameweek that can now be graded, and then the
**Instructions** for the target gameweek - the transfers (possibly several, possibly none),
the chip to play or hold, the captain and vice-captain, the starting eleven and the bench
order - and `status` over what the run left behind. The order matters, and it is stated
once, in the schedule. `make now DRY=--dry-run` shows the decision without acting on it, and
prints the reason each step is due or was skipped.

It always produces Instructions: the hours to the deadline are a caveat (far out, it says
the predicted lineups are not yet firm), not a condition. **After the deadline it says the
gameweek is locked** and when FPL rolls over, and advises nothing - that is the answer, not a
failure. A second run takes the lock only if the first has finished; it is safe to repeat,
and each run adds a fresh record of its Instructions.

Then interpret. The numbers are not the answer.

## Check the warehouse first

The plan ends on `status`, so a `make now` that advised has already run it - read
those lines before reading a single recommendation. Run `.venv/bin/fpl-agent status` by
hand when you are reading a ranking from an earlier run; it is read-only.

Exit 0 means the warehouse is consistent and the recommendations rest on something. Any
non-zero exit names a specific inconsistency: look the code up in `docs/SCHEDULING.md`
rather than re-deriving it.

**Do not read the recommendations over a non-zero status.** The output will look entirely
normal - ranked moves, prices, gains - because every one of those numbers is computed
whether or not the inputs are sound. That is exactly the failure this project keeps
hitting: a confident answer to a question the data could not support.

**Read the lines, not just the exit code.** `status` already checks the things a
recommendation rests on - that the squad was captured (no squad, no selling prices, so
the budget and everything on it is wrong), that the projection targets this gameweek
under the current model (the one settle will grade later), and that rival squads exist
(a player in no rival squad is owned by 0%, but only if rivals were captured at all).
The first two are `FAIL` and move the exit code. **Rivals are a `WARN`**: stale or absent
ownership sits behind an exit 0, so find that line before weighing any differential.

Ownership necessarily lags even when the line is `ok`: rival squads only become public
after a deadline, so you are seeing what the league owned last week. Say so rather than
implying it is current.

## Reading the Instructions

The block opens in a fixed order: **Stance**, **Chip**, **Moves**, **Starting XI**,
**Captain**, **Vice-captain**, **Bench**. Read the stance first - it says whether the advice
is protecting a lead or chasing one, the gap, and what that changed against plain expected
points. Its weight is small and uncalibrated: where the effect line says the stance changed
a move or the captain, that is the place to argue with the advice, not the numbers.

**Moves** is a plan, not a ranking: how many transfers, netting out 4-point hits beyond your
free transfers, beside "do nothing" and "best single swap". Extra moves have to pay for
their hits. "No move" with a margin is a real answer, as is "not evaluated" (the solver did
not prove its answer, or the horizon is not projected) - never read a move into either.

**Chip** compares no chip against each chip *with its own best moves*. A chip is played only
if it clears its own bar and is the best remaining week within the noise band; otherwise it
says hold, with the margin to the best alternative. A chip that is played, expired or not
yet open is reported as it is and never valued.

**Two numbers, deliberately separate.** Expected-points gain says whether the move is
worth making. Urgency says whether the chance to make it is disappearing. A modest upgrade
whose window closes tonight is a different decision from a large upgrade that can wait.
Never blend them into one score.

**Price urgency runs on its own clock.** Changes resolve nightly, not at the gameweek
deadline, so "act tonight" can be true days before the deadline and false hours before it.

**Ownership is league-relative.** A differential is only a differential against the people
being played, and the stance already weighs it by league position: chasing from behind
justifies variance that a leader should refuse. When ownership is stale the stance line says
it was not applied - then the advice is plain expected points.

**Selling a template player is a risk even when the swap gains points** - if he hauls, the
whole league gains on you. That is reported separately for a reason.

## When not to act

- The model is **uncalibrated** until several gameweeks have settled. Treat gaps as
  directional, not precise, and say so.
- A suspiciously round or extreme number is usually a bug. Two players at exactly 6.00, or
  a reserve keeper among the best value in the game, have both happened.
- One free transfer and no urgent problem is often a bank, not a move.

## Afterwards

Run `make record` once you have actually acted, and say what you did:
`make record ARGS='--move 1 --move 2 --captain Haaland --chip none'`. Moves are numbered as
the Moves line lists them; `--all` only if you made the whole plan. The captain and the chip
you actually played are stored beside the recommended ones, so a deviation can be graded
later by `make settle`. It writes to `logs/actions.jsonl`, which is committed. `make now`
deliberately does not record: recording is a claim about what was done, not about what was
suggested. A bare `make record` records nothing.

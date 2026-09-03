---
name: fpl-deadline
description: Run the pre-deadline cycle - capture, project, and rank transfers - and interpret the result. Use before an FPL gameweek deadline, or when asked what transfer to make.
---

# Pre-deadline cycle

Run `make deadline`. It captures a snapshot, backfills actuals, projects the three-gameweek
horizon, captures rival squads and ranks transfers, in that order. The order matters:
actuals feed the projection's rates, and rivals must exist before ownership means anything.

Then interpret. The numbers are not the answer.

## Check these before reading the recommendations

**Did the snapshot capture the squad?** It refuses without auth, but confirm the run says
so rather than assuming. Without a squad there are no selling prices, so the budget - and
every recommendation resting on it - is wrong.

**Is the projection from a snapshot targeting this gameweek?** That is the one P4 will
grade later. A projection made for an earlier gameweek is not a decision-time projection.

**Are rivals captured for the most recent finished gameweek?** Ownership necessarily lags:
rival squads only become public after a deadline, so you are seeing what the league owned
last week. Say so rather than implying it is current.

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

`make recommend` with `--record` logs the chosen decision to `logs/actions.jsonl`, which is
committed. Record what was actually done, not what was suggested.

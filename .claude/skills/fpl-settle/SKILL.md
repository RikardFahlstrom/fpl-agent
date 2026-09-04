---
name: fpl-settle
description: Grade projections against a finished gameweek, read the calibration slices, and decide whether a deviation is a finding or noise. Use after an FPL gameweek completes.
---

# Settle a finished gameweek

Run `make settle GW=n`. It backfills actuals, grades the decision-time projections, prints
calibration slices and drafts a learning file.

## Check the warehouse first

Run `.venv/bin/fpl-agent status` **before `make settle`.** It is read-only - it
authenticates against nothing and writes nothing - and it says whether gameweek `n` has a
decision-time projection and the actuals to grade it against.

Exit 0 means there is something real to settle. Any non-zero exit names a specific
inconsistency; the code table is in `docs/SCHEDULING.md`, shared with `deploy/fpl-cron.sh`.
Look the code up there before reacting to it - some are the normal answer rather than a
fault. "The gameweek has not finished, or there was nothing to grade" is most mornings,
and it means stop, not investigate.

**A non-zero status is not something to work around.** Settle's own guards will refuse the
run anyway, and they exist because grading a gameweek the warehouse cannot honestly
support once produced a confident +1.65 bias out of actuals that did not exist. If status
says the actuals are missing, backfill and re-check; do not force the grade.

## Preconditions the tool enforces, and why

**The gameweek must be finished.** Grading early scores every player against a zero that
has not happened, which reads as a huge over-projection. This is checked; do not work
around it.

**Only the decision-time projection is graded** - the one whose snapshot targeted that
gameweek. Horizon projections made weeks earlier are not what a decision rested on.

**A player with no actuals row scored zero**, once the gameweek is finished. He never made
a matchday squad. Dropping those rows would flatter the model by grading only the players
who turned up.

## Reading calibration

Bias is **predicted minus actual**, so positive means over-projecting.

The overall number is not actionable. The slices are: "forwards over-projected by 0.8"
names a term to change; "MAE 2.1" names nothing.

**Ignore small slices.** Findings need at least 20 players. Two elite players missing by 12
points is not evidence about elite players - it has already produced a −12.40 bias that
meant nothing.

## Turning a slice into a learning

The drafted file is a **hypothesis with evidence**, `status: proposed`. It is not applied.

Before changing a weight:

- One gameweek is mostly variance. Confirm the direction holds across at least three
  before fitting anything.
- Ask which term produces the bias. The components are stored on every projection for
  exactly this - trace the number rather than guessing at it.
- When a weight does change, **bump `MODEL_VERSION`** and set the learning's `status` to
  `applied` with the version in `action`. Both model versions then sit in the warehouse
  and can be compared against the same gameweeks.

A learning that is rejected is still worth keeping - set `status: rejected` and say why.

## Committed

`learnings/*.md` and `logs/actions.jsonl` are tracked once they exist. Neither is in the
checkout yet: `--learn` writes the first learning file and creates `learnings/` doing it.
Commit what it wrote. The database is not tracked: it is derived and re-fetchable, the
reasoning is not.

---
name: fpl-learn
description: Walk the owner through the proposed learnings in learnings/, one at a time, in plain English, and apply or reject each on their say-so. Use only when the user asks to go through, accept, apply or reject learnings - never on your own.
---

# Act on the proposed learnings

`settle --learn` drafts a learning when a calibration slice deviates enough; it stays
`status: proposed` until a person decides. This skill is that decision, and **only runs
when the user asks for it**. The brief's `Learnings:` line is what tells them there is
something waiting; it never applies anything.

## 1. Read what is waiting

```bash
grep -l "status: proposed" learnings/*.md
```

Read each file whole. Group drafts that name the same `metric` and `slice`, and count
them - that count is the evidence bar, and this is the one place it is set:

- **One** draft is a sample. Say it exists; do not propose anything.
- **Two** with the same sign is worth noticing. Tell the owner the direction is
  repeating and that one more settled gameweek decides it - still nothing to apply.
- **Three** with the same sign is a finding. Trace it (step 2) and propose (step 3).

A slice of 218 players looks like a lot of evidence, but the players are the same 218
every week and one week's fixtures move all of them together; the independent samples
are gameweeks, not players. Three is the smallest number where a sign that held every
time is unlikely to be the fixture list. A draft whose sign flips resets the count.

## 2. Trace the number before proposing anything

Every projection stores its components, and the actuals decompose into the same
categories. Run the trace on each draft in the group:

```bash
.venv/bin/python .claude/skills/fpl-learn/scripts/trace_slice.py learnings/0002-*.md
```

It prints predicted, actual and bias per component for the slice, largest gap first,
and a totals line that must match the learning's own numbers - if it does not, stop.
Read the trace for all three gameweeks side by side. A slice-level bias is often several
small gaps that change order week to week rather than one term that is wrong; only a
component that carries the gap **every** time names a weight. If nothing does, the
learning is real but not actionable yet - say so and leave it `proposed`.

The candidate weights live at the top of `src/fpl_agent/engine/projection.py`
(`BASE_START_PROB`, `LINEUP_STARTER_PROB`, `BONUS_PRIOR_APPEARANCES`, the priors, …).
Scoring weights are **not** candidates - they come from `game_config` and are FPL's.

## 3. Propose, in the reader's words, one learning at a time

Say what happened, what you would change, and what you would expect to see - no engine
vocabulary without its meaning:

> Defenders have scored about 0.3 more per game than the model expected in each of the
> last three gameweeks. The trace puts nearly all of it in one place: the model expects
> them to concede more than they do, so the goals-conceded penalty is too heavy. I would
> soften that term by a quarter, which lifts a typical defender's projection by about
> 0.2 and leaves the other positions alone. Apply it?

The shape matters more than the words: the observation, the component the trace blamed,
the specific constant and its new value, and the size of the move it should produce. The
example is invented - the slice waiting in `learnings/` will name its own term, and only
the trace can say which.

Wait for **yes** or **no** on each. Do not batch the questions and do not apply on a
"looks fine" - the answer has to be to a specific change.

## 4. On yes

1. Change the weight in `projection.py`, with a comment naming the learning id.
2. **Bump `MODEL_VERSION`** (`engine/projection.py`). Both versions then sit in the
   warehouse and can be compared against the same gameweeks; a settled gameweek keeps its
   projection (CLAUDE.md invariants).
3. In the learning file set `status: applied` and `action: <what changed>, model
   <new version>`.
4. `make test`, then `make project` so the new version is in the warehouse, and read
   `fpl-agent brief --dry-run` to see the projections move the way you said they would.
5. Commit the weight, the version and the learning file together, on a branch.

## 5. On no

Set `status: rejected` and put the owner's reason in `action:` - one line, their words.
A rejected learning is kept, not deleted: the next draft on the same slice should be read
against it.

## Never

- Apply a learning nobody said yes to.
- Change a weight without bumping `MODEL_VERSION`.
- Re-grade a settled gameweek to make the numbers agree.

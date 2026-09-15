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

Read each file whole. Group drafts that name the same `metric` and `slice`: two of them
in different gameweeks are the "direction holds" signal a single draft asks you to wait
for, and a lone draft from one gameweek is mostly variance (`/fpl-settle` says so).

## 2. Trace the number before proposing anything

Every projection stores its components. Before suggesting a weight, find which term
produced the bias - do not guess from the slice name:

```bash
.venv/bin/python - <<'PY'
import sqlite3, json
conn = sqlite3.connect("data/fpl.db"); conn.row_factory = sqlite3.Row
# components of graded projections in the slice, against actuals - adapt the join to
# the slice (position, price band, p_start band) named in the learning
PY
```

The candidate weights live at the top of `src/fpl_agent/engine/projection.py`
(`BASE_START_PROB`, `LINEUP_STARTER_PROB`, `BONUS_PRIOR_APPEARANCES`, the priors, …).
Scoring weights are **not** candidates - they come from `game_config` and are FPL's.

## 3. Propose, in the reader's words, one learning at a time

Say what happened, what you would change, and what you would expect to see - no engine
vocabulary without its meaning:

> Players almost certain to start scored about half a point more per game than the model
> expected, two gameweeks running (218 players each time). The bonus term is where the
> gap is: predicted starters earn bonus more often than the prior assumes. I would raise
> `BONUS_PRIOR_APPEARANCES` from 3.0 to 2.0, which lifts their projection by about 0.4.
> Apply it?

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

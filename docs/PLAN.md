# FPL decision engine — plan

Turning this repo from an MCP server that reads FPL into a decision engine that
projects points, recommends actions, records what it decided, and measures itself
against what actually happened.

## 0. The constraint that sets the order of work

`bootstrap-static` is **current-state only**. Prices, ownership, form and price-change
projections are overwritten in place; there is no historical endpoint for them.

`element-summary` backfills *actuals* (per-gameweek points, minutes, xG) and
`history_past` gives prior seasons — but **the state you were deciding against is gone
the moment the gameweek turns.**

Every gameweek without a snapshot is permanently unlearnable. Phase 1 therefore lands
before any modelling work.

## 1. Repo identity — `fpl-agent`, detached

The login page already brands itself **FPLAgent** (`web.py`: the `FA` seal, "FPLAgent ·
local connection"). The product name exists; the repo name hasn't caught up.
`fpl-agent` needs no invention and stops undersells once there is a warehouse and a
learning loop inside.

Detaching: self-serve fork detachment on GitHub is inconsistent and often needs Support.
With no stars or issues to preserve, the reliable path is a fresh repo plus a full
history push. Confirm before relying on either route.

## 2. Data layer — SQLite, hybrid schema

Typed columns for what the model reads; `raw JSON` for the rest. FPL adds fields between
seasons (`defensive_contribution` is new this year), and a 109-column table turns every
addition into a migration.

```sql
snapshot(id, gameweek, captured_at, kind)
player_snapshot(snapshot_id, element_id, now_cost, form, status,
                chance_of_playing_next_round, selected_by_percent,
                expected_goals_per_90, expected_assists_per_90, starts_per_90,
                minutes, penalties_order, price_change_percent,
                price_change_projections JSON, price_change_locked_until,
                transfers_in_event, transfers_out_event, raw JSON)
player(element_id PK, web_name, first_name, second_name, team_id, element_type)
team(id, name, short_name, strength_attack_home, strength_defence_away, ...)
fixture(id, event, team_h, team_a, team_h_difficulty, team_a_difficulty,
        kickoff_time, finished)
player_gameweek(element_id, round, minutes, total_points, goals_scored, assists,
                bonus, bps, expected_goals, ..., PRIMARY KEY(element_id, round))
game_config(captured_at, scoring JSON, rules JSON)
my_squad(snapshot_id, element_id, position, is_captain, selling_price, purchase_price)
my_state(snapshot_id, bank, squad_value, free_transfers, chips JSON)
```

`player_gameweek` is the actuals table and the join target for calibration. It backfills
from `element-summary` immediately, so the schema starts with real data.

Snapshot cadence: **daily**, not per-gameweek. Price changes resolve nightly (~01:30 UK),
so a weekly snapshot would miss the price dynamics section 4 depends on.

## 3. Projection engine — read the weights, never hardcode them

`game_config.scoring` is position-keyed data and must be read, not transcribed:

```
xP(player, gw) = P(start) × [ minutes_pts
                            + xG90 × mins/90 × scoring.goals_scored[pos]
                            + xA90 × mins/90 × scoring.assists
                            + P(clean_sheet) × scoring.clean_sheets[pos]
                            + E[def_contribution] × scoring.defensive_contribution[pos]
                            + E[bonus] − E[cards] ]
```

Inputs already fetched and currently unused: `expected_goals_per_90`,
`expected_assists_per_90`, `starts_per_90`, `chance_of_playing_next_round`, opponent
`strength_defence_*`, fixture difficulty, and the RotoWire predicted XI for `P(start)`.

Every projection records `model_version` and its components, so a weight change is
comparable against the previous era rather than silently replacing it.

## 4. Price-change awareness and urgency

FPL publishes a **first-party price forecast**. Verified fields:

| Field | Meaning |
|---|---|
| `price_change_percent` | Progress toward the next change; ±100% crosses the threshold |
| `price_change_projections` | List of `{offset, projected_percent, likelihood}` for offsets 0, 1, 2 (days ahead) |
| `price_change_locked_until` | Timestamp; the player has already moved and is locked |
| `transfers_in_event` / `transfers_out_event` | The net-transfer pressure driving the change |

Observed `likelihood` spans **−5 … +5**. Live example at time of writing: Calafiori,
£5.6m, 92.6% toward a rise, day-0 projection 100.7%, likelihood 5, +268k net transfers in
— rising within hours.

**Note:** the exact semantics of `likelihood` are undocumented. The −5…+5 range and its
correlation with `price_change_percent` and net transfers were observed empirically.
Validate against realised changes over a few gameweeks before trusting fixed thresholds —
this is itself a good first calibration target.

### The affordability trap

Budget for a transfer is `bank + selling_price(player_out)`. Two forces close the window:

1. **The target rises.** Affordable at £5.6m today, out of reach at £5.7m tomorrow.
2. **Your own players fall.** A held player dropping reduces its selling price, shrinking
   the budget — the squeeze works from both ends at once.

And `game_config.rules.transfers_sell_on_fee = 0.5`: a rise in a player you own returns
only half the profit, so your budget grows more slowly than the market moves.

### Urgency signal

Alongside `expected_points`, each candidate carries:

```
affordability_margin = (bank + selling_price(out)) − now_cost(target)
window_closes_at     = next price-change tick (~01:30 UK) when likelihood is high
urgency              = f(affordability_margin, likelihood, hours_to_tick)
```

- `margin < 0.1m` and `likelihood >= 3` → **act tonight**; the player leaves your reach
  at the next tick, independent of how many days remain to the gameweek deadline
- A held player at `likelihood <= -3` → selling now preserves value
- `price_change_locked_until` set → already moved; no urgency, don't double-count

This is deliberately **separate from `expected_points`**. A player can be a modest
projection improvement but a high-urgency buy purely because the window is closing —
conflating the two into one score hides the reason. Recommendations carry both, and the
urgency is explained in words, not just a number.

## 5. Learning loop and tracked logs

```sql
projection(id, snapshot_id, gameweek, element_id, model_version,
           expected_points, components JSON, created_at)
outcome(projection_id, actual_points, error, evaluated_at)
```

After each gameweek settles: pull actuals → join to projections → compute **MAE overall
and sliced by position, price band, and start-probability bucket.** Slicing is what makes
it learnable. "MAE 2.1" teaches nothing; "we over-project forwards by 0.8pt and rotation
risks by 1.4pt" names a fix.

This grades **every player scored** — ~650 signals per gameweek, not just the one or two
transfers taken.

### The two logs, as tracked files

SQLite is gitignored, so the reasoning trail must live in git separately. Both files are
exported from the DB after each run — the DB stays the query engine, git holds the record.

**`logs/actions.jsonl`** — append-only, one JSON object per action taken:

```json
{"ts":"2026-09-03T22:14:00Z","gw":3,"model_version":"0.3.1","kind":"transfer",
 "out":{"id":427,"name":"Mbeumo","selling_price":57},
 "in":{"id":182,"name":"Calafiori","now_cost":56},
 "xp_delta":1.8,"urgency":"high","reason":"price rise likelihood 5, margin £0.0m",
 "projection_ids":[91823,91844]}
```

JSONL rather than markdown **for efficiency**: appending one line produces a pure-addition
git diff, no rewrite churn and no merge conflicts across concurrent runs, and the file
loads directly for analysis. A markdown table rewrites the whole file on every append.

**`learnings/NNNN-slug.md`** — one markdown file per learning, YAML frontmatter plus prose:

```markdown
---
id: 0007
gameweek: 3
model_version: 0.3.1
metric: mae_by_position
observation: forwards over-projected by 0.82 pts
status: applied          # proposed | applied | reverted
action: scaled xG term for FWD by 0.85 in 0.3.2
---

Forwards were over-projected in GW1–3 (MAE 2.4 vs 1.6 overall). The xG term assumes
full-90 minutes; rotated forwards inherit the per-90 rate without the minutes discount.
...
```

Markdown here, not JSONL: learnings are **prose hypotheses with evidence**, low volume
(a few per gameweek), and get read, edited and argued with. Frontmatter keeps the
machine-readable parts queryable; the body holds the reasoning. `status` and `action`
close the loop — a learning is not done until it changed a weight or was explicitly
rejected.

**`logs/gw03.md`** — a rendered per-gameweek brief. With artifacts out of scope, this is
the human-readable deliverable: recommendations, urgency flags, and last gameweek's
calibration, committed alongside the decisions it explains.

## 6. Claude-way — skills in git

`.claude/skills/`, version-controlled, each a thin wrapper over a Python entry point so
the logic stays testable outside Claude:

| Skill | Does |
|---|---|
| `/fpl-snapshot` | Capture current state → SQLite. Idempotent per day. |
| `/fpl-project` | Run projections for the upcoming gameweek. |
| `/fpl-decide` | Recommend transfers/captain/chip; append to `logs/actions.jsonl`. |
| `/fpl-settle` | Pull actuals, score projections, draft a learning file. |
| `/fpl-review` | Calibration trend and what changed between model versions. |

Plus **`CLAUDE.md`** at the root holding the rules that are easy to violate:

- Read scoring weights from `game_config`; never hardcode them
- Bump `model_version` on any weight change
- Never mutate a settled `projection` row
- Snapshot before deciding; a decision without a snapshot cannot be graded later

With `FPL_AUTO_LOGIN` and `FPL_READ_ONLY` already in place, a scheduled nightly
`snapshot` and a deadline-eve `project → decide` follow naturally.

## 7. Phasing

| Phase | Work | Gate |
|---|---|---|
| **P0** | Rename/detach, `CLAUDE.md` | — |
| **P1** | Schema, daily snapshot, backfill actuals | **Before the next deadline** |
| **P2** | Projection engine, `game_config` parsing | After P1 has data |
| **P3** | Price/urgency signals, `logs/actions.jsonl` | After P2 |
| **P4** | Settle, calibration slices, `learnings/` | Needs one settled gameweek |
| **P5** | Feed learnings back into weights | Needs ≥3 gameweeks of calibration |

P1 is the only phase with a real deadline. Everything after it can take its time.

## 8. Assumptions

Single manager, not multi-tenant. Python with stdlib `sqlite3`, no ORM. Projections one
gameweek ahead initially; multi-gameweek horizon later. `make_transfers` stays manual —
the engine recommends, a human executes. `FPL_READ_ONLY` stays set for scheduled runs.

## 9. Open questions

- Does `likelihood` mean the same thing at +5 and −5, or is the scale asymmetric?
  Validate against realised changes (see §4). **Still open.**

### Decided

- **Planning horizon: 3 gameweeks.** Transfer value is judged on projected points over
  the next three gameweeks rather than the next one, so a good fixture run counts and a
  one-week spike does not dominate.
- **Rivals are modelled.** Squads of managers in your leagues are public for completed
  gameweeks, so the engine tracks them and reports effective ownership *within your
  leagues* rather than globally. A template player everyone owns is a risk to skip, not
  an edge; a differential is only a differential relative to the people you are actually
  playing against.

  Implemented in `rivals.py`. Only private leagues (`league_type` `x`) under a rival cap
  are captured: FPL's own leagues are type `s` and unusably large — "Overall" carries
  around 9.9 million entries. Effective ownership counts a captain twice, matching how
  much of the field's score a player actually drives.

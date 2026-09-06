# fpl-agent

An FPL decision engine. The MCP server is one interface onto it; the warehouse,
projections and learning loop are the substance. `docs/PLAN.md` holds the roadmap.

## Stack

Python 3.10+, `uv` for the virtualenv and lockfile, SQLite at `data/fpl.db`. No framework
and no CI; linting is `make lint` on demand, not enforced. `MODEL_VERSION` is at
`engine/projection.py:34`.

## Map

| Path | What is there |
| --- | --- |
| `src/fpl_agent/engine/` | snapshot, actuals, lineups, projection, scoring, pricing, rivals, recommend, settle, status, brief, notify, storage |
| `src/fpl_agent/mcp/` | the MCP server: tools, resources, prompts |
| `src/fpl_agent/` | auth, headless_auth, sessions, client, config, models, reference, rotowire_scraper, cli, main |
| `tests/` | one `test_<module>.py` per engine module; not a package, hence `-t tests` |
| `deploy/fpl-cron.sh` | the unattended entry point |
| `.claude/hooks/` | tests gate `git commit`; `fpl-agent status` gates the end of a turn |

## Commands

```bash
make now                                 # do whatever is due; DRY=--dry-run to preview
make status                              # read-only; exits 7 if the warehouse disagrees
uv sync                                  # creates .venv, which the Makefile expects
make test                                # 486 tests, ~11s
PYTHONPATH=src:tests .venv/bin/python -m unittest test_settle.SettleTests   # one class
make lint                                # unused imports and undefined names; FIX=--fix
```

**`make now` is the one to reach for.** It asks the warehouse what is due — capture,
settle anything finished and ungraded, project if a deadline is within 26h — and is safe
on a day when the answer is nothing. `status` ends on a `next:` line saying the same
without doing it.

Every other target is a step, and the order matters: actuals feed the projection's rates,
rivals must exist before ownership means anything, and `settle` reads `finished` from the
warehouse rather than the API, so fixtures must be re-snapshotted before a gameweek can be
graded. **Do not run bare `make`** — the first target is `snapshot`, so it captures live
rather than printing help. The skills `/fpl-deadline` and `/fpl-settle` wrap the
hand-driven halves with what to check and when not to act. Unattended, cron runs
`deploy/fpl-cron.sh daily` and `deadline` on their own clocks. Nothing here ever executes
transfers.

## The one that has bitten repeatedly

**Verify the effect, not the invocation.** Every serious bug in this project so far was
code that reported success for something that did not happen:

- the snapshot preflight announced "this snapshot will include your squad", then nothing
  logged in
- settling an unplayed gameweek scored all 651 players against actuals that did not
  exist, reporting a confident +1.65 bias
- players in nobody's squad were labelled "unknown ownership" and dropped, discarding 165
  of 200 candidates at exactly the point the edge lives

After any change, check the thing you wanted, not the fact that the command exited zero.
Run it against real data and read the output.

## Invariants

- **Read scoring weights from `game_config`.** Never hardcode them. FPL changes them
  between seasons; `defensive_contribution` is new this year. See `engine/scoring.py`.
- **Bump `MODEL_VERSION` on any change that moves projections.** Both versions then sit
  in the warehouse and can be compared, rather than one silently replacing the other.
- **Never grade a gameweek that has not finished.** Absence of an actual is not a zero
  until the fixtures are played. See `engine/settle.gameweek_is_finished`.
- **Never re-project a settled gameweek.** A graded projection is the record of what the
  model believed before the result was known; rewriting it under today's code scores the
  model against a result it can see. Bump `MODEL_VERSION` instead. See
  `engine/projection.SettledProjection`.
- **Snapshot before deciding.** `bootstrap-static` is current-state only: prices,
  ownership and price forecasts are overwritten in place with no historical endpoint. A
  gameweek without a snapshot can never be learned from.
- **Absence of a row is data.** A player with no `player_gameweek` row scored zero once
  the gameweek finished; a player in no rival squad is owned by 0%. Neither is "missing".
- **Never run `make record` unprompted.** Recording is a claim about what the user
  actually did, not a step in a plan. See the comment on the target.
- **Never put credentials in the conversation.** `fpl-agent.ini` is gitignored (`*.ini`
  with `!*.ini.example`); point at the file or the browser login flow instead.

## What is committed

Code, `docs/`, and the reasoning trail: `learnings/` and `logs/actions.jsonl`. **Not**
`data/fpl.db` (derived and re-fetchable) or `fpl-agent.ini` (credentials).

`settle --learn` and `recommend --record` each create their directory on first write;
commit what they leave behind. Do not commit a placeholder to make the directories
appear: an empty `learnings/` claims a loop has run that has not.

## Further reading

- `docs/FACTS.md` — per-subsystem facts and where they live. Read when working in `engine/`.
- `docs/SCHEDULING.md` — the unattended setup, exit codes, and what cron decides.
- `docs/PLAN.md` — the roadmap.

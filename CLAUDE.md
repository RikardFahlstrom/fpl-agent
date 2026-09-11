# fpl-agent

An FPL decision engine. The MCP server is one interface onto it; the warehouse,
projections and learning loop are the substance.

## Stack

Python 3.10+, `uv` for the virtualenv and lockfile (`uv sync` creates the `.venv` the
Makefile expects), SQLite at `data/fpl.db`. No framework, no CI; `make lint` is on demand.

## Map

- `src/fpl_agent/engine/` — the real work: snapshot, projection, scoring, settle, schedule…
- `src/fpl_agent/mcp/` — inherited fork surface; the MCP server over the engine.
- `tests/` — one `test_<module>.py` per engine module; not a package, hence `-t tests`.
  One class: `PYTHONPATH=src:tests .venv/bin/python -m unittest test_settle.SettleTests`.
- `deploy/fpl-cron.sh` — the unattended entry point. `tools/` — the schedule-versus-shell
  equivalence harness; not shipped.
- `learnings/`, `logs/` — the committed reasoning trail; `settle --learn` and
  `recommend --record` write here, and what they leave is committed, not scratch.
- `.claude/hooks/` — tests gate `git commit`; `fpl-agent status` gates the end of a turn.

## Commands

**`make now` is the one to reach for.** It asks the warehouse what is due and does it;
safe on a day when the answer is nothing. `make status` is the read-only twin, ending on a
`next:` line. `make -n` or the `##` comments in the Makefile list the rest.

Every other target is a step, and the order matters: actuals feed the projection's rates,
rivals must exist before ownership means anything, and `settle` reads `finished` from the
warehouse, so fixtures must be re-snapshotted before a gameweek can be graded. **Bare
`make` captures live** — the first target is `snapshot`. `/fpl-deadline` and `/fpl-settle`
wrap the hand-driven halves with what to check and when not to act. Nothing here ever
executes transfers.

## Verify the effect, not the invocation

Every serious bug in this project so far was code that reported success for something that
did not happen: a preflight announced the squad would be captured, then nothing logged in;
settling an unplayed gameweek scored 651 players against actuals that did not exist;
unowned players were labelled "unknown ownership" and dropped at exactly the point the
edge lives. After any change, run it against real data and read the output for the thing
you wanted, not the exit code.

## Invariants

- **Read scoring weights from `game_config`.** FPL changes them between seasons;
  `defensive_contribution` is new this year. See `engine/scoring.py`.
- **Bump `MODEL_VERSION` (`engine/projection.py`) on any change that moves projections.**
  Both versions then sit in the warehouse and can be compared.
- **Grade a gameweek only once it has finished.** Absence of an actual is a zero only
  after the fixtures are played. See `engine/settle.gameweek_is_finished`.
- **A settled gameweek keeps its projection.** The graded row is the record of what the
  model believed before the result; to re-score it, bump `MODEL_VERSION`. See
  `engine/projection.SettledProjection`.
- **Snapshot before deciding.** `bootstrap-static` is current-state only, overwritten in
  place with no history; a gameweek without a snapshot can never be learned from.
- **Absence of a row is data.** No `player_gameweek` row after a finished gameweek is a
  zero; a player in no rival squad is owned by 0%.
- **`make record` only when the user says they made the transfer.** Recording is a claim
  about what they did, not a step in a plan.
- **Credentials stay in `fpl-agent.ini`** (gitignored). Point at the file or the browser
  login flow; the values never enter the conversation.

## Further reading

- `CONTEXT.md` — before using job, due, Plan, Step, capture in code or prose.
- `docs/FACTS.md` — before working in `engine/`; per-subsystem facts and where they live.
- `docs/SCHEDULING.md` — before touching cron, exit codes, or `deploy/fpl-cron.sh`.
- `docs/schedule-equivalence.md` — before changing `engine/schedule.py`.
- `docs/PLAN.md` — the roadmap; before picking up new work.
- `docs/agents/issue-tracker.md`, `triage-labels.md`, `domain.md` — before filing,
  labelling or triaging on GitHub (`origin`, never `upstream`), or looking for ADRs.

## Plan Mode

- Make the plan extremely concise. Sacrifice grammar for the sake of concision.
- At the end of each plan, give me a list of unresolved questions to answer, if any.

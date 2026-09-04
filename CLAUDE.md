# fpl-agent

An FPL decision engine. The MCP server is one interface onto it; the warehouse,
projections and learning loop are the substance. `docs/PLAN.md` holds the roadmap.

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
- **Snapshot before deciding.** `bootstrap-static` is current-state only: prices,
  ownership and price forecasts are overwritten in place with no historical endpoint. A
  gameweek without a snapshot can never be learned from.
- **Absence of a row is data.** A player with no `player_gameweek` row scored zero once
  the gameweek finished; a player in no rival squad is owned by 0%. Neither is "missing".
- **Never put credentials in the conversation.** `fpl-agent.ini` is gitignored (`*.ini`
  with `!*.ini.example`); point at the file or the browser login flow instead.

## Facts worth not rediscovering

Each lives in the docstring of the code it constrains:

| Fact | Where |
| --- | --- |
| Price change rule: Predicted Progress > 100% is "Very Likely"; `likelihood` is a derived band of the same number | `engine/pricing.py` |
| Defensive-contribution thresholds (DEF >= 10, MID >= 12) are not published; derived from 1236 scored appearances | `engine/scoring.py` |
| `/me/` carries no league membership - leagues are on `entry/{id}/` | `state.get_user_leagues` |
| `league_type` `x` is a private league, `s` is global and unusable ("Overall" has ~9.9M entries) | `engine/rivals.py` |
| Per-90 rates from tiny samples must be shrunk toward a prior | `engine/projection.shrink` |
| The sell-on fee returns only half of any profit, so budget grows slower than the market | `engine/pricing.py` |
| The account service rotates the refresh token on every exchange, so two concurrent refreshes leave one caller holding a dead credential | `headless_auth.refresh_access_token` |
| Each recommendation is priced as the *next* transfer you would make, not as the nth move of a plan | `engine/recommend.transfer_price` |

## Workflow

`make deadline` before a deadline, `make settle GW=n` after the gameweek. The skills
`/fpl-deadline` and `/fpl-settle` wrap those with what to check and when not to act.

Unattended, those same commands run from `deploy/fpl-cron.sh`, which decides *whether*
there is anything to do rather than encoding the FPL calendar in a crontab. It never
executes transfers. See `docs/SCHEDULING.md`.

## What is committed

Code, `learnings/`, `logs/actions.jsonl`, `docs/PLAN.md`. **Not** `data/fpl.db` (derived
and re-fetchable) or `fpl-agent.ini` (credentials).

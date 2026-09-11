# Facts worth not rediscovering

Per-subsystem knowledge that cost something to learn. Each one also lives in the docstring
of the code it constrains — that copy is the authority, and this table is an index so you
can find out a fact exists before you need it.

Read this when you are working inside `engine/`. It is not loaded into every session; the
standing rules are in [CLAUDE.md](../CLAUDE.md).

| Fact | Where |
| --- | --- |
| Price change rule: Predicted Progress > 100% is "Very Likely"; `likelihood` is a derived band of the same number | `engine/pricing.py` |
| Defensive-contribution thresholds (DEF >= 10, MID >= 12) are not published; derived from the 622 played appearances, not the 1236 stored rows | `engine/scoring.py` |
| `/me/` carries no league membership - leagues are on `entry/{id}/` | `sessions.get_user_leagues`, `sessions.py:90` |
| `league_type` `x` is a private league, `s` is global and unusable ("Overall" has ~9.9M entries) | `engine/rivals.py` |
| League standings (`leagues-classic/{id}/standings/`) are public and always current; a session is needed only to learn membership from `/me/`, and picks are hidden until the gameweek starts | `engine/rivals.refresh_standings`, `rivals --standings-only` |
| Per-90 rates from tiny samples must be shrunk toward a prior | `engine/projection.shrink` |
| The sell-on fee returns only half of any profit, so budget grows slower than the market | `engine/pricing.py:9` |
| The account service rotates the refresh token on every exchange, so two concurrent refreshes leave one caller holding a dead credential | `headless_auth.save_refreshed_session`, `headless_auth.py:140` |
| Each recommendation is priced as the *next* transfer you would make, not as the nth move of a plan | `engine/recommend.transfer_price`, `recommend.py:131` |
| The deadline is 90 minutes before the round's first kickoff, derived from stored fixtures because the warehouse does not keep `deadline_time`; a postponed opening fixture moves the kickoff but not the real deadline | `engine/storage.next_deadline` |
| The scheduler reported the *last* failing step's code, so a lost snapshot (3) arrived wearing whatever a later recoverable step exited | `engine/schedule.Outcome` |
| `deploy/fpl-cron.sh`'s `FPL_DB` reached the queries it asked and not the commands it ran, so a non-default warehouse was planned from one database and written to another | `engine/schedule.SubprocessExecutor` |
| "Ready to settle" (finished, projected from a capture targeting the round, ungraded under the version) and "actuals fetched" (at least eleven a side per played fixture; zero never passes) are one ledger, read as a value by `settle`, `status` and the scheduler; a fourth copy in `deploy/fpl-cron.sh` offered a round still being played and stepped over an ungraded one | `engine/warehouse.GameweekLedger` |

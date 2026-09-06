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
| Per-90 rates from tiny samples must be shrunk toward a prior | `engine/projection.shrink` |
| The sell-on fee returns only half of any profit, so budget grows slower than the market | `engine/pricing.py:9` |
| The account service rotates the refresh token on every exchange, so two concurrent refreshes leave one caller holding a dead credential | `headless_auth.save_refreshed_session`, `headless_auth.py:140` |
| Each recommendation is priced as the *next* transfer you would make, not as the nth move of a plan | `engine/recommend.transfer_price`, `recommend.py:131` |

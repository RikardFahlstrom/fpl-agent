# fpl-agent review (per docs/REVIEW-PROMPT.md)

## Context

Reviewed on 2026-09-03 against commit fa96e9a. Every file under `src/`, `tests/`, `docs/`,
`.claude/`, plus `CLAUDE.md`, `Makefile`, `pyproject.toml` was read. Claims below were
checked by running code or querying the local `data/fpl.db` read-only (6 snapshots, all
targeting GW3; GW1-2 finished; no `outcome` rows yet, so settle has never run on real data).
`make test` passes: 194 tests.

Owner's answers: single user, one team. Success = beats private leagues, runs unattended and
never lies, recommendations get followed, projections measurably improve. Effort goes to
model quality and automation. The important interface is *a scheduled run on a remote server
that reaches out when I need to know something* - an assistant coach. Not the MCP server.

That last answer reframes the review. `docs/SCHEDULING.md` deliberately keeps a human in the
loop for `deadline` and `settle`; the owner wants the opposite. Several findings are graded
against "unattended and never lies" rather than the interactive workflow the docs assume.

## Assessment

**a. Structure.** The engine/MCP split is right and the dependency direction holds
(`engine/` never imports `mcp/`). The engine modules say what they do. What is confusing:
the MCP layer is the inherited fork and knows nothing about the warehouse - none of the 34
tools reads a projection, outcome or decision - so there are two unrelated
`recommend_transfers` (a heuristic scorer in `mcp/tools/squad.py`, the real one in
`engine/recommend.py`). `recommend` writes projection rows as a side effect. `backfill`
lives in `snapshot.py` but is imported by `settle`. Every engine docstring still says
`python -m fpl_agent.snapshot`, a path that no longer exists. `learnings/` and
`logs/actions.jsonl` are described as committed in four places and exist in none.

**b. Correctness.** The invariants in `CLAUDE.md` are honoured. The bugs found are all of
the "confident wrong answer" class and cluster in two places: the projection's minutes model
(items 3, 4) and the settle/snapshot handshake under a real schedule (items 1, 2, 5). The
recommender's arithmetic is sound but it ignores the cost of the transfer itself (item 6).

**c. Claude-efficiency.** `CLAUDE.md` is the right length and the "verify the effect"
section is the most valuable thing in the repo. The skills are genuinely judgement, not
ceremony. What is missing is anything that serves the unattended goal: no status check, no
brief, no notification, and the settings allowlist covers only `cd`. What is stale:
`PLAN.md` §6 lists five skills that were never built under those names; the deadline skill
tells you to run `make recommend` with `--record`, which the Makefile cannot pass.

---

# Instructions, most valuable first

## Do now

### 1. Refuse to settle a gameweek that has no actuals, and make backfill fail loudly

**Why.** `settle_gameweek` guards against an *unfinished* gameweek but not against a finished
one whose actuals were never fetched. `backfill_actuals` swallows every per-player failure
with a warning and returns normally. Sequence that produces a confident wrong answer: the
nightly snapshot marks GW N's fixtures finished; `make settle GW=N` runs while the FPL API
is refusing requests (it does, intermittently); all 652 element-summary calls fail with
warnings; `settle_gameweek` sees `gameweek_is_finished` true, `COALESCE(pg.total_points, 0)`
turns every missing row into a zero, and calibration reports a bias of about +1.5 across
652 players. `--learn` then writes a learning file asserting it. This is the GW3 settle bug
from `CLAUDE.md` in a new coat.

**Scope.** `engine/settle.py` (`settle_gameweek`, `_run`), `engine/snapshot.py`
(`backfill_actuals`). Out of scope: changing what counts as an actual once rows exist.

**Acceptance.** A test where fixtures for GW 2 are finished but `player_gameweek` has no rows
for round 2 must raise (a new `ActualsMissing` or reuse `GameweekNotFinished` with a distinct
message) and leave `outcome` empty. `backfill_actuals` must return the failure count and
`settle._run` must exit non-zero when more than a small fraction (say 5%) of players failed.
Threshold for "has actuals": rows for the round ≥ 11 × number of finished fixtures × 2 is
generous and safe; at minimum, more than zero.

**Risk.** A legitimately empty round cannot exist once fixtures are finished, so no false
refusals. Verify before/after by running `fpl-agent settle --gameweek 2 --no-backfill` on
the local DB (rounds 1 and 2 have 610 and 626 rows) - it should still grade, then delete
round-2 rows in a scratch copy and confirm it refuses.

**Size.** Small.

### 2. Grade the latest snapshot that *has* projections, and refresh fixtures inside settle

**Why.** `settle_gameweek` selects `MAX(snapshot.id) WHERE gameweek = N` and then requires
projections from that snapshot. `docs/SCHEDULING.md` runs `make snapshot` nightly at 03:00
with `--force`. For any Saturday deadline: Friday `make deadline` projects on snapshot A;
Saturday 03:00 creates snapshot B (still targeting N, no projections); settle later picks B
and reports "nothing to grade" every single week. The local DB already shows the pattern:
snapshots 4 and 5 carry the squad and zero projections. Separately, `gameweek_is_finished`
reads `fixture.finished`, which only a snapshot updates, so settle's verdict on whether GW N
is over depends on whether an unrelated job ran.

**Scope.** `engine/settle.py` only. The subquery becomes "latest snapshot targeting N that has
at least one projection for N under this model_version". `_run` fetches `/fixtures/` and
upserts before the finished check (one request; the client is already there). Out of
scope: changing how `project` picks its snapshot.

**Acceptance.** Extend `test_only_the_projection_current_at_the_deadline_is_graded`: add a
third, later snapshot targeting GW 2 with no projections; settle must still grade the earlier
one. A second test: fixtures table says GW 2 unfinished, the fetched fixtures say finished;
settle proceeds.

**Risk.** If two snapshots targeting N both have projections (re-run `make deadline`), the
later still wins - that is the decision-time one, unchanged. Verify on the real DB that
`settle --gameweek 3` after GW3 finishes grades 652 rows from snapshot 6.

**Size.** Small.

### 3. Fix the start-rate denominator: benched games count against starting

**Why.** `_player_history` groups `player_gameweek` rows `WHERE minutes > 0`, so
`appearances` counts only games played and `start_rate = starts / appearances`. A player who
started once and was an unused substitute once has appearances 1, starts 1, rate 1.0.
`element-summary` history *does* carry a row for every team fixture (the DB has 300 of 610
round-1 rows at zero minutes), so the evidence is there and ignored. In the current
snapshot 37 players are affected; Bizot (AVL) and Onyeka (COV) each have one start and one
benching and are projected at p_start 1.00 for GW4, four times what the data supports. This
is the reserve-goalkeeper bug of model 0.2.0 in a partial form.

**Scope.** `engine/projection.py` (`_player_history`, `start_rate`). Keep `appearances`
(minutes > 0) for the bonus and card rates, which are per-appearance; add `games` (all rows)
as the start denominator and shrink toward the positional or league start rate with
`APPEARANCE_PRIOR`. Bump `MODEL_VERSION`. Out of scope: distinguishing "benched" from
"injured with a zero-minute row" - the FPL flag handles current injuries.

**Acceptance.** A test seeding rows (starts 1, minutes 90) and (starts 0, minutes 0) for one
player must give a start rate near 0.5, not 1.0. After re-projecting the real DB, Bizot and
Onyeka must no longer show p_start 1.0 for GW4.

**Risk.** Zero-minute rows also exist for long-term injured players; their availability is
already zeroed by `chance_of_playing_next_round`, so no double penalty on the current GW, but
horizon weeks will read them as low starters, which is correct.

**Size.** Small.

### 4. A doubtful player named in the predicted XI is a starter, not an omission

**Why.** RotoWire lists a questionable starter both in the XI and in the injury list.
`record_lineups` writes the injury entries first (to keep the flag) and its dedupe keeps
that row, whose `is_starter` is 0. `lineup_start_rates` then gives every QUES player the
`omitted` rate (0.15), and `availability()` multiplies by FPL's own chance. All 32 QUES rows
in snapshot 6 have `is_starter = 0`; Caicedo (CHE), Mount (MUN) and Sávio (TOT) are at FPL
75% and projected at p_start 0.112 - as if benched *and* injured. Eleven of the 32 are
players FPL says are fully available.

**Scope.** `engine/lineups.py` (`record_lineups`, `lineup_start_rates`). Keep the injury
flag, take `is_starter` from the XI entry when both exist. In `lineup_start_rates`: OUT → 0;
named in the XI → starter rate regardless of QUES (fitness is FPL's flag's job, as the
docstring already argues); QUES and not named → `omitted`. Bump `MODEL_VERSION`. Out of
scope: changing the availability model.

**Acceptance.** `test_a_doubtful_starter_is_stored_once_keeping_the_flag` must additionally
assert `is_starter == 1` for Isak, and a new start-rate test must give him 0.90 (not 0.15).
On the real DB after re-capture, Caicedo's p_start should be about 0.75 × 0.90.

**Risk.** `to_statuses` in the scraper (used by the MCP injuries tool) deliberately reports
doubtful over expected; leave it alone, it is a different consumer.

**Size.** Small.

### 5. Exit non-zero when the squad promised by the preflight was not captured

**Why.** `CLAUDE.md`'s first lesson. `auth_readiness` checks configuration and
`authenticated_client` checks a session exists, but `capture()` catches any exception from
`get_my_team` and logs a warning, and `_run` returns 0. A 403 on `my-team/` (which FPL
returns during maintenance windows) produces a snapshot with no `my_squad` rows, a clean exit,
and a `recommend` that raises "no squad captured" hours later at the deadline - or, if the
nightly job is the last snapshot before `make deadline` re-snapshots, nothing at all. The
lineup scrape failing is also only a warning; that is acceptable (third party) but should be
visible in the summary.

**Scope.** `engine/snapshot.py` (`capture`, `_run`). After capture, count `my_squad` rows for
the snapshot; if auth was configured and the count is not 15, exit 4 unless
`--allow-partial`. Print a one-line summary of what was captured (players, fixtures, squad
rows, lineup rows and the gameweek they were filed under). Out of scope: rolling back the
market half - keep it, it is the irrecoverable part.

**Acceptance.** A test with a client whose `get_my_team` raises, `FPL_AUTO_LOGIN` set and a
mocked session, must make `_run` return non-zero and the log must say the squad is missing.
The existing partial-snapshot tests still pass.

**Risk.** None to data. The nightly launchd job will now fail visibly on a broken `my-team/`,
which is the point.

**Size.** Small.

### 6. Charge the transfer hit and respect free transfers and active chips in `recommend`

**Why.** `my_state` stores `free_transfers`, `transfer_cost` and `chips`; `recommend` reads
none of them. Every recommendation is ranked on gross xP gain over three gameweeks. With zero
free transfers a +1.2 xP move costs 4 points and is net −2.8, and the tool ranks it first
with no warning. The current snapshot records one free transfer and a 4-point cost, and the
chips JSON shows a wildcard marked active for GW3 - under which single like-for-like swaps
are the wrong shape of advice entirely. For an owner who wants "recommendations I actually
follow", this is the highest-leverage correctness gap in the recommender.

**Scope.** `engine/recommend.py`. Carry `free_transfers`, `hit_cost` (0 for the first
`free_transfers` moves, `transfer_cost` after) and `net_xp_delta` on each recommendation;
rank on net; print the hit. When a transfer chip (`wildcard`, `freehit`) is `active` in
`my_state.chips`, say so at the top and do not charge hits. Out of scope: multi-transfer
planning or chip strategy.

**Acceptance.** A test with `limit 0, made 0, cost 4` must show `net_xp_delta = xp_delta − 4`
and drop any recommendation whose net is ≤ 0. A test with an active wildcard must charge
nothing. `test_recommends_a_like_for_like_upgrade` (limit 1) is unchanged.

**Risk.** FPL's `limit` semantics (rolling up to 5 this season, `max_extra_free_transfers` 4
in `rules`) - use `limit − made` as stored; do not model banking here.

**Size.** Medium.

## Do next

### 7. Model goalkeeper saves, and stop over-charging goals conceded

**Why.** Two systematic position biases that calibration will report as findings but are
already known. Saves are absent from the projection: across the 40 goalkeeper starts in the
DB the saves term is worth 0.60 points per start, so every keeper is under-projected by
about that. Goals conceded uses `expected_conceded / 2`, but the rule is per *completed*
pair: across 208 GKP/DEF starts the realised `floor(gc/2)` averages 0.567 against `gc/2` of
0.764, so defenders and keepers are over-charged by about 0.2 per start. Together that is
roughly −0.8 on a keeper over the horizon, enough to reorder the position.

**Scope.** `engine/projection.py`. Saves: a per-90 saves rate from `player_gameweek.raw`
(already stored), shrunk like yellows, × `minutes_share` × `scoring.w["saves"] / SAVES_PER`,
GKP only. Goals conceded: replace `expected_conceded / 2` with the Poisson expectation of
`floor(X/2)` (sum over k of P(X ≥ 2k), truncate at k = 4). Bump `MODEL_VERSION`. Out of
scope: penalty saves, red cards, own goals (each < 0.05 per game).

**Acceptance.** A test that a keeper with a realised 3 saves per 90 gains about 1 point per
full game; a test that the goals-conceded component at λ = 1.4 is about −0.47 for a DEF, not
−0.70. Components must still sum to the total.

**Risk.** Pure model change; visible in calibration by position. Compare 0.3.0 and the new
version on the same gameweek once GW3 settles.

**Size.** Small.

### 8. File lineups under the gameweek their fixtures belong to, not the snapshot's target

**Why.** `capture` stores every scraped match under `target_gameweek(bootstrap)`. RotoWire
shows the next round of fixtures; FPL flips `is_next` the moment a deadline passes. A
snapshot between the GW N deadline and its last kickoff (the nightly job on a Saturday, any
Friday-to-Monday round) stores GW N lineups as GW N+1. `lineup_start_rates` picks the latest
snapshot for a gameweek, so a later correct scrape usually wins, but only if one happens
before `project` runs. The page carries no gameweek label, so this cannot be parsed - but
the `fixture` table knows which (home, away) pair sits in which event.

**Scope.** `engine/lineups.py` (`record_lineups` takes the fixture rows, or a
`team → event` map built in `capture`). A match whose pair is not in the target gameweek is
filed under the event that has it, and a warning names any pair found in no unfinished
event. Out of scope: the scraper.

**Acceptance.** A test where the fixture table has LIV–MCI in GW 3 and the snapshot targets
GW 4 must store the rows with `gameweek = 3`. The current snapshot 6 (all 20 clubs matched
GW3 pairs exactly) must be unaffected.

**Risk.** Double gameweeks: a pair can appear once per event; take the earliest unfinished.

**Size.** Small.

### 9. Use the refresh token so the nightly job stops driving a browser

**Why.** The cached token lives 8 hours (`expires_at − obtained_at` = 28 800 s in the local
cache) and the nightly job runs every 24. `save_cached_session` stores `refresh_token`;
nothing reads it. So every scheduled run, and every 401 mid-run, launches headless Chromium
against the Premier League account service. On a remote server that is the flakiest
component in the system and the one most likely to trigger "too many attempts". For the
unattended goal this is the top robustness item.

**Scope.** `headless_auth.py`: on an expired or rejected cached token, POST the refresh grant
to the same `/as/token` endpoint the login captures, cache the result, and fall back to the
browser only if that fails. Out of scope: the interactive web login.

**Acceptance.** With a cache whose `expires_at` is in the past and a mocked token endpoint
returning a new access token, `load_cached_session` must return a session without
constructing `FPLAutomation`. Then verify for real: expire the local cache by hand and run
`fpl-agent snapshot --backfill-only`; the log must not say "Launching the FPL authentication
browser".

**Risk.** Unverified whether the account service honours the refresh grant for this client
id; **check with one manual request before writing code** (see decisions). If it does not,
the fallback is still the browser and nothing regresses.

**Size.** Medium.

### 10. Add `fpl-agent status`: one command that says whether the warehouse is trustworthy

**Why.** Every bug in `CLAUDE.md`'s history was a state the tools reported as fine. Nothing
today can answer, in one call, "is the latest snapshot complete, are projections attached to
it, are actuals current, did lineups land under the right gameweek, will tonight's job need a
browser?" Items 1, 2 and 5 each add a guard at one point; this is the general one, and it is
what an unattended job should end with and what a notification should be built from.

**Scope.** New `engine/status.py` and a `status` entry in `cli.COMMANDS`. Report: latest
snapshot id/age/target gw, `my_squad` rows for it, projections for it by model version,
lineups (count, gameweek filed under, snapshot), max backfilled round vs latest finished
fixture event, rivals gameweek and manager count, token cache expiry, `decision` count.
Exit non-zero on any inconsistency (squad missing, projections absent for the target gw,
backfill behind a finished gameweek). Append it to the `deadline` target and the launchd
command in `docs/SCHEDULING.md`. Out of scope: fixing what it finds.

**Acceptance.** Run it on the local DB: it must report snapshot 6 with 15 squad rows, 1956
projections, 298 lineups for GW3, backfill through round 2, rivals GW2 with 5 managers, and
exit 0. Delete the `my_squad` rows for snapshot 6 in a scratch copy and it must exit non-zero
naming the problem.

**Risk.** None; read-only.

**Size.** Medium.

### 11. Delete or implement `get_players_to_avoid`

**Why.** `mcp/tools/injuries.py` calls `scraper.convert_to_ai_format`, which does not exist
on `RotoWireLineupScraper`. The tool and the `fpl://injuries/avoid` resource always return
"Error fetching players to avoid: ... has no attribute 'convert_to_ai_format'". Dead since
the scraper rewrite.

**Scope.** `mcp/tools/injuries.py`, `mcp/resources.py`, README tool count. Recommend
deleting: `get_injury_and_lineup_predictions` already lists OUT and DOUBTFUL.

**Acceptance.** A regression test in `test_endpoint_regressions.py` calling the tool (if
kept) must not contain "no attribute". Tool count in README updated.

**Risk.** None.

**Size.** Trivial.

### 12. Bring the written record back in line with what exists

**Why.** A fresh session reads these and acts on them. Specifically: `CLAUDE.md` "What is
committed" and `docs/SCHEDULING.md` list `learnings/` and `logs/actions.jsonl` as tracked;
neither exists (no decision has ever been recorded, `decision` count is 0). `PLAN.md` §6
lists `/fpl-snapshot`, `/fpl-project`, `/fpl-decide`, `/fpl-review`; the real skills are
`deadline`, `settle`, `verify`. `scoring.py` says the DC thresholds came from 622
appearances; `CLAUDE.md` and the verify skill say 1236. Five engine docstrings and two error
messages say `python -m fpl_agent.snapshot` (the module is `fpl_agent.engine.snapshot`; the
CLI is `fpl-agent snapshot`). `Makefile` `snapshot` passes `--force`, which makes
`snapshot_taken_today` and the README's "idempotent per day" claim dead in practice. The
deadline skill's "Afterwards" says `make recommend` with `--record`; the target cannot pass
it - add a `record` target or `ARGS`.

**Scope.** `CLAUDE.md`, `docs/PLAN.md` §6 and §7, `docs/SCHEDULING.md`, docstrings in
`engine/*.py`, `Makefile`, `.claude/skills/fpl-deadline/SKILL.md`. Out of scope: rewriting
the plan.

**Acceptance.** `grep -rn "python -m fpl_agent\." src docs .claude` returns nothing; the
skill's stated command works when pasted.

**Risk.** None.

**Size.** Small.

## Consider

### 13. Build the assistant coach: a rendered brief plus a notifier with stated triggers

**Why.** This is what the owner said the project is for, and nothing serves it. `PLAN.md`
§5 already promised `logs/gwNN.md`. `deadline` is deterministic and can run unattended;
`settle` only *drafts* learnings and can too. The human is needed for exactly two things:
choosing to act on a recommendation, and accepting or rejecting a drafted learning.

**Scope.** New `engine/brief.py` rendering markdown from the warehouse (top recommendations
with net xP and urgency, held players very likely to fall, injured or doubtful starters in
the squad, free transfers and deadline time, last settled calibration, status output from
item 10). A `notify` step that sends it, or only its trigger lines, to a channel (see
decisions). Trigger set to start with: any `tonight` urgency; a held XI player at
`status_for_entry`-independent "very likely to fall"; a squad player flagged `i`/`s` or OUT
in lineups; deadline within 24 h with an unused free transfer and a positive-net move;
`status` exit non-zero. Out of scope: executing transfers.

**Acceptance.** `fpl-agent brief` writes `logs/gw03.md` from the local DB; a dry-run notify
prints which triggers fired and why.

**Risk.** Notification spam erodes trust fast; every message must end with the one action
wanted from the human.

**Size.** Large.

### 14. Use prior-season rates as the early-season prior instead of positional medians

**Why.** `element-summary` returns `history_past` (previous seasons) and `backfill_actuals`
discards it. With `RATE_PRIOR_MINUTES = 270`, two matches of data already carry 40% of the
weight; the current top projection is a centre-back at 1.17 goal points per fixture. A
player's own last-season xG90 is a far better prior than the position median for the first
six gameweeks.

**Scope.** `storage.py` (a `player_season` table), `snapshot.backfill_actuals`,
`projection.shrink` callers. Bump `MODEL_VERSION`.

**Acceptance.** Guéhi's GW3 goals component drops materially; a test that a player with a
prior season of 0.05 xG90 and 63 minutes of 2.0 xG90 this season lands near the prior.

**Risk.** Promoted-club players have no PL prior; fall back to the positional median.

**Size.** Medium.

### 15. Decide what the MCP server is for

**Why.** 34 tools, none reading the warehouse; two `recommend_transfers`; every tool requires
a session even for public data. If the assistant coach is the product, the MCP surface
either shrinks to a few warehouse-backed tools (`projections`, `recommendations`,
`calibration`, `decisions`, `status`) or stays as-is as a browsing convenience. It should not
keep offering a second, contradictory transfer recommendation.

**Scope.** `mcp/tools/`, `mcp/resources.py`, `mcp/prompts.py`, README.

**Acceptance.** One `recommend_transfers` in the codebase.

**Size.** Medium to large, depending on the decision.

### 16. Smaller model and code items

- `recommend()` calls `project_horizon`, writing projection rows. Read the stored horizon and
  fail if absent; `make deadline` already projects. (small)
- The defensive-contribution prior is pooled across positions, so every forward and
  goalkeeper-less slot gets 0.111 points from it; use a per-position league rate. (trivial)
- Candidates are filtered to `status = 'a'`, excluding the 16 `d` players who already have a
  `chance_of_playing_next_round`. Deliberate but undocumented; either document or include
  them (availability already scales them). (trivial)
- `PriceOutlook.locked` is `bool(price_change_locked_until)`; compare against now. All 60
  locks in the current snapshot are in the future, so no wrong answer today. (trivial)
- RotoWire's `SUS` is not in `INJURY_STATUS` (`SUSP` is) and `lineup_start_rates` only zeroes
  `OUT`; a suspended player named in the injury list gets 0.15. FPL's `s` flag catches most.
  (trivial)
- `client.get_current_gameweek` falls back to 38; `make_transfers` would submit for GW38.
  Raise instead. (trivial)
- Move `backfill_actuals` to `engine/actuals.py`; drop unused imports in `mcp/tools/core.py`.
  (trivial)
- Re-projecting a settled gameweek hits the `outcome` foreign key under `INSERT OR REPLACE`
  and crashes rather than silently rewriting. That is accidental enforcement of the "never
  mutate a settled projection" rule from `PLAN.md` §6; make it deliberate with a check and
  a clear message, and add the rule to `CLAUDE.md`. (small)

### 17. Claude Code setup

- `.claude/settings.local.json` allows only `cd`. Add `make test`, `sqlite3 -readonly`,
  `.venv/bin/fpl-agent status`/`recommend`/`project`, and `git` read commands so a session
  can verify effects without prompting. (trivial)
- A `PreToolUse` hook on `git commit` that runs `make test` is cheap insurance for a repo
  whose suite runs in 0.2 s. (trivial)
- After items 2 and 10 land, the deadline and settle skills should say to run
  `fpl-agent status` first and what each failure line means, replacing their current
  "confirm the run says so" prose. The verify skill is fine as it is.
- `docs/PLAN.md` §1 (detaching the fork) and §9 are done; prune them so the file is the
  roadmap again.

## Needs a decision from the human

1. **Notification channel and trigger list for item 13.** Email, Telegram, ntfy, or a Claude
   session summary? And is the trigger set above the right definition of "I need to know"?
2. **Should `deadline` and `settle` run unattended on the remote server?** The review
   recommends yes for both once items 1, 2, 5 and 10 are in, with a human only acting on the
   brief and on drafted learnings. `docs/SCHEDULING.md` currently says no.
3. **Refresh grant (item 9):** confirm with one manual POST to the account service that the
   stored `refresh_token` is exchangeable before building on it.
4. **MCP direction (item 15):** trim to warehouse-backed tools, or keep the inherited set as a
   browsing convenience and only remove the duplicate recommender?
5. **Doubtful starters (item 4):** trust RotoWire's XI for selection and FPL's percentage for
   fitness (recommended), or keep treating QUES as an omission?

## Verification for the whole batch

1. `make test` green, with the new failing-today tests listed above added and passing.
2. `fpl-agent project --horizon 3` on the real DB, then check by hand: Bizot and Onyeka not
   at p_start 1.0; Caicedo about 0.68; a goalkeeper's components include `saves`; a
   defender's `goals_conceded` magnitude is about 70% of before.
3. `fpl-agent status` exits 0 and its numbers match a manual `sqlite3` count.
4. After GW3 finishes: `make settle GW=3` grades 652 rows from the snapshot that has
   projections, refuses on a scratch copy with round-3 rows deleted, and the calibration
   table prints without any slice at bias ≈ mean xP.

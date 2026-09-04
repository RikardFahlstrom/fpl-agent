# Running this unattended

The target is a Linux server running everything on cron with no human present, and
**no automated transfers**. The agent captures, projects and recommends; a person
reads the output and makes the move. `read_only = true` is the setting that enforces
that, and it is the reason a bearer token can sit on a remote host at all.

The organising principle is that **cron fires dumbly and often; the guards decide
whether there is anything to do.** The FPL calendar cannot be written in a crontab -
deadlines move, fixtures are postponed, double gameweeks exist, and a gameweek ends
when its last fixture ends rather than on a weekday. Any crontab encoding that is
wrong within weeks. `settle` already refuses a gameweek that has not finished, so
attempting it every morning costs one process and answers correctly.

`deploy/fpl-cron.sh` is that decision layer. Cron calls it; it calls the agent.

## What runs, and how often

| Job | Cadence (UTC) | Why that cadence |
| --- | --- | --- |
| `daily` | once, 02:30 | Prices resolve around 01:30 UK. A missed day is price and ownership movement that no endpoint can return - this is the irrecoverable one. |
| `deadline` | hourly | Cheap when idle: it reads one row and exits. Inside 26 hours of a deadline it re-snapshots and re-projects, because predicted lineups firm up on matchday and a projection built 24 hours out is a different answer from one built 3 hours out. |

`daily` also attempts to settle: it asks the warehouse for the highest finished
gameweek that has never been graded and, if there is one, grades it and drafts a
learning. Absence from the `outcome` table is the test, not a marker file - the
warehouse is the only state worth trusting.

**Use UTC, not UK time.** The API speaks UTC and British Summer Time moves the UK
clock twice a season. 02:30 UTC is safely after the price change in both halves of
the year.

```cron
CRON_TZ=UTC
MAILTO=you@example.com

30 2 * * *  /srv/fpl-agent/deploy/fpl-cron.sh daily
7  * * * *  /srv/fpl-agent/deploy/fpl-cron.sh deadline
```

The odd minute on the hourly job is deliberate: it keeps the two off each other on
the one night they would otherwise collide.

## Locking, and why it is about the credential

Every job re-execs itself under `flock -n /tmp/fpl-agent.lock`. `-n` means a run
already in progress wins and the new one exits rather than queueing; a backlog of
snapshots helps nobody.

The lock exists because **the account service rotates the refresh token on every
exchange** (verified against the live service, 2026-09-04). Each refresh returns a
new refresh token and invalidates the old one. Two jobs refreshing concurrently both
read the same cached token, one wins, and the loser is left holding a dead
credential - which degrades to launching a browser, exactly what the refresh grant
was added to avoid. Install `util-linux` if `flock` is missing; the script warns and
continues without it rather than silently running unserialised.

For the same reason: **do not copy the token cache between machines.** Two hosts
refreshing the same token fight, and both lose.

## Exit codes

Cron mails you anything non-zero. Each code names a distinct failure so the mail is
actionable without opening the log:

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success, or nothing to do | Nothing. |
| 1 | `settle`: the gameweek has not finished, or there was nothing to grade | Nothing. This is the normal answer most mornings. |
| 2 | `snapshot`: auth is not configured | Fix `fpl-agent.ini` or the environment. |
| 3 | `snapshot`: auth is configured but no session could be established | The refresh token has died and the browser fallback failed. Log in once by hand. |
| 4 | `snapshot`: the squad the preflight promised was not captured | Usually `my-team/` returning 403 during an FPL maintenance window. The market half was kept; re-run later. |
| 5 | Backfill failed for more than 5% of players | Transient FPL trouble. Re-run; if it persists, the API shape may have changed. |
| 6 | `settle`: the gameweek is finished but the actuals are not there to grade it | Run the backfill first. Never force this - grading against absent actuals is the bug this code exists to prevent. |

Codes 3 and 4 are the two worth alerting on loudly. 4 in particular is the one that
used to exit 0.

## Authentication: Chromium runs once, not nightly

The login is OAuth2 against a PingFederate service at
`https://account.premierleague.com/as`. There is no password grant, so the *first*
login has to traverse the HTML form and that needs a real browser:

```bash
uv run playwright install chromium     # once, on the server
FPL_AUTO_LOGIN=true .venv/bin/fpl-agent snapshot --force
```

After that the cached refresh token renews the session, and Chromium is only a
fallback. The access token lives 8 hours against a 24-hour schedule, so before the
refresh grant was wired up every scheduled run launched a browser - the flakiest
component in the system, and the one most likely to trip "too many attempts".

Watch the log for which path a run took:

```
Restored the FPL session from cache; no browser login needed.
Refreshed the FPL access token; no browser login needed.
Launching the FPL authentication browser        <- should be rare
```

The refresh token's own lifetime is not published and has not been measured. Because
it rotates on every use, a server that runs daily should keep itself alive
indefinitely; a server that sits idle for a long stretch may need one manual login.
Keep Chromium installed unless image size genuinely matters - the fallback is the
only thing standing between a dead token and a silent gap in the snapshot history.

## Configuration

Every setting is an environment variable. `fpl-agent.ini` is an alternative way to
populate them and **the environment always wins**, so a systemd drop-in or a secret
store can override the file without editing it. See `fpl-agent.ini.example`.

For an unattended host:

```ini
[auth]
auto_login = true
email = you@example.com
password = ...
read_only = true                          ; refuse make_transfers - keep this on
token_cache = /srv/fpl-agent/state/session.json
```

- `chmod 600 fpl-agent.ini`. It holds a plaintext password, and loading warns if the
  file is readable by anyone else.
- Put `token_cache` somewhere persistent and backed up *as a location*, not as
  content - the file rotates constantly, so a restored old copy is a dead token.
  The default is `~/.config/fpl-mcp/session.json`, which is fine if the service user
  has a stable home directory.
- `read_only = true` is a real control, not tidiness. The cached token is a bearer
  credential that can execute transfers. On a remote host it is the crown jewel.
- `FPL_TOKEN_ENDPOINT` and `FPL_OAUTH_CLIENT_ID` exist so a client-id rotation is a
  config change rather than a code change. Leave them unset unless the login breaks.

## What is not automated yet

Two gaps, both known, both with review items against them:

- **Nothing notifies you.** Today the only channel is cron's `MAILTO` and the log.
  The intended shape is a pre-deadline brief pushed to a channel you actually read
  (review item 13), which is what makes this an assistant coach rather than a cron
  job you have to remember to check.
- **There is no single "is the warehouse trustworthy?" command.** `fpl-agent status`
  (review item 10) is meant to answer that in one line - last snapshot, last graded
  gameweek, whether the squad is present - so a morning check is one command rather
  than three SQL queries.

Until then, `deploy/fpl-cron.sh --dry-run daily` and `--dry-run deadline` print what
each job would do without touching anything, which is the quickest way to see what
the scheduler currently believes.

## What ends up in git

| Path | Tracked | Why |
| --- | --- | --- |
| `src/`, `tests/`, `deploy/`, `.claude/skills/`, `CLAUDE.md`, `Makefile` | yes | the system and how to run it |
| `learnings/*.md` | yes | the reasoning, which is not re-derivable |
| `logs/actions.jsonl` | yes | what was decided, append-only |
| `docs/` | yes | plan and conventions |
| `data/fpl.db` | no | derived, and re-fetchable except for the snapshot history |
| `fpl-agent.ini` | no | credentials |
| the token cache | no | a live bearer credential, and it rotates |

The database is the one asset that is *not* re-fetchable in full - past snapshots
cannot be recovered - but it is too large and too churny for git. Back it up
separately if the snapshot history matters:

```bash
sqlite3 data/fpl.db ".backup 'backups/fpl-$(date -u +%F).db'"
```

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

## `status` is the last line of a run

`fpl-agent status` reads the warehouse - read-only, `mode=ro`, no network, no token
exchange - and answers in one block: is the latest snapshot complete, are projections
attached to it under the current model, are the actuals current, which gameweek's
lineups will `project` find, and will tonight's job need a browser. It fixes nothing.

It is the natural last step of any scheduled run, and `make deadline` ends on it for
that reason. Every command before it reports its own success; `status` is the one that
checks the state they claim to have left behind - which is the whole lesson of this
project's bug history. Adding it to the end of a `daily` job costs one process:

```sh
"$AGENT" status || echo "warehouse check failed"   # exits 7 on an inconsistency
```

Run it by hand the same way. It is also the first of the four things the notifier pushes
on: `status.gather()` returns the checks as data rather than as text, so the log line and
the push message cannot disagree about what failed.

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

## Notifications

Both jobs end in `fpl-agent notify`. It asks `brief.evaluate` what is worth interrupting
a person for, drops anything it has already said, and pushes the rest to
[ntfy](https://ntfy.sh) - no account, no new dependency, one HTTP POST per message.

Four things push, and nothing else. Everything the agent knows goes in `logs/gwNN.md`;
only these four are worth a phone buzzing:

| Trigger | Priority | Why it is worth a notification |
| --- | --- | --- |
| `status_failed` | 4 | The scheduled run is broken and nothing else can be trusted. |
| `squad_player_unavailable` | 4 | A player you own cannot play. These are points already lost. |
| `deadline_with_move` | 4 | The deadline is inside 24 hours, a free transfer is unused, and there is somewhere to spend it. |
| `move_worth_making` | 3 | A move clears the net-xP bar on its own, deadline or no deadline. Standing advice, so it arrives quietly. |

**Every message ends with the action.** That is the whole discipline of the thing: the
risk is that pushes become noise you swipe away, and the cure is that the last line of
each one is the single thing being asked of you.

**You are told each thing once.** The `deadline` job runs hourly, so a fact that fires at
09:00 is still firing at 15:00. Each trigger carries a fingerprint built from identity
alone - the gameweek, the player, the `out -> in` pair - and the fingerprints already
delivered are stored in the warehouse's `notification` table. A fingerprint that is there
is dropped silently; a fingerprint that changes (a different top move, a different player
injured) is news and is sent. Nothing is recorded until the server has accepted the
message, so a failed push is retried on the next run rather than lost.

To see what the warehouse currently believes without sending anything:

```sh
make notify DRY_RUN=--dry-run          # or: fpl-agent notify --dry-run
```

It prints every message it would send, every fingerprint it is suppressing as already
sent, and - for each trigger that did not fire - the condition that stopped it. A run
that sends nothing is a correct run, and this is how you tell it from a broken one.

### Turning it on

1. **Generate a topic, and treat it as a password.** ntfy has no accounts: the topic *is*
   the address and the credential together, and anyone who knows it can subscribe and
   read every message on it - your squad, your transfers, when you are about to act. So
   it must be long and random. Not `fpl`, not your name.

   ```bash
   python3 -c "import secrets; print('fpl-' + secrets.token_urlsafe(24))"
   ```

2. **Put it in `fpl-agent.ini`** (or set `FPL_NTFY_TOPIC`; the environment wins):

   ```ini
   [notify]
   ntfy_topic = fpl-<the long random string from step 1>
   ; ntfy_server = https://ntfy.sh          ; only if you self-host
   ```

   It is in `config.SECRET_ENV`, so the ini loader logs it as `***` and `notify` prints
   only the server host and a redacted topic. Keep `chmod 600` on the file.

3. **Subscribe on the phone.** Install the ntfy app, add that exact topic. Nothing else
   is needed - no login, no key.

4. **Check it end to end**, without waiting for a deadline:

   ```sh
   fpl-agent notify --dry-run       # what would go out, and what is being suppressed
   fpl-agent notify                 # actually send; the phone should buzz
   fpl-agent notify                 # and this one sends nothing. That is the dedupe.
   ```

If you would rather not use the public instance, self-host ntfy and point
`ntfy_server` at it. The topic is still a secret there unless you have put access
control in front of it.

Related: `[brief] min_net_xp` (`FPL_BRIEF_MIN_NET_XP`) is the net expected-points bar
`move_worth_making` has to clear, default 2.0 over the three-gameweek horizon. Raise it
if the pushes feel like noise.

### Why a failed push cannot fail the job

`notify` runs *after* `daily` and `deadline`, and its exit code is reported only when the
job itself succeeded:

```
job's code != 0  ->  exit the job's code (notify's is logged and discarded)
job's code == 0  ->  exit notify's code (0, or 8 if a send failed)
```

The snapshot is the irrecoverable asset; a notification is not. A dead ntfy server must
never make a `daily` run that captured the market look like one that lost it. And if no
topic is configured, `fpl-cron.sh` skips notify with a line saying so rather than
mailing a failure every hour.

## Exit codes

Cron mails you anything non-zero. Each code names a distinct failure so the mail is
actionable without opening the log:

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success, or nothing to do | Nothing. |
| 1 | `settle`: the gameweek has not finished, or there was nothing to grade | Nothing. This is the normal answer most mornings. |
| 2 | `snapshot`: auth is not configured. `status` and `notify`: the warehouse could not be read at all, or `notify` has no topic configured | Fix `fpl-agent.ini` or the environment; for `status` and `notify`, the database is missing or is not a warehouse. |
| 3 | `snapshot`: auth is configured but no session could be established | The refresh token has died and the browser fallback failed. Log in once by hand. |
| 4 | `snapshot`: the squad the preflight promised was not captured | Usually `my-team/` returning 403 during an FPL maintenance window. The market half was kept; re-run later. |
| 5 | Backfill failed for more than 5% of players | Transient FPL trouble. Re-run; if it persists, the API shape may have changed. |
| 6 | `settle`: the gameweek is finished but the actuals are not there to grade it | Run the backfill first. Never force this - grading against absent actuals is the bug this code exists to prevent. |
| 7 | `status`: the warehouse disagrees with itself | Read the `FAIL` lines - each names what is wrong and what to run. Nothing is broken *by* status; it only reports. |
| 8 | `notify`: a push was not accepted by the server | The message was **not** recorded as sent, so the next run will try it again. If it persists, check the ntfy server and the topic. Never masks the job's own failure - see below. |

Codes 3 and 4 are the two worth alerting on loudly. 4 in particular is the one that
used to exit 0.

Codes are not shared between commands, deliberately: a 7 in the subject line means
`status` and nothing else, an 8 means `notify` and nothing else, so the mail is
actionable before it is opened. Code 2 is the one exception, and it says the same thing
in every command that returns it: the thing this needed was not configured, or could not
be read at all.

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

- **Transfers are never executed.** By design, and `read_only = true` enforces it. The
  agent tells you what to do; you do it.
- **The deadline is derived, not fetched.** 90 minutes before the first stored kickoff,
  which a postponed opening fixture would move. `bootstrap-static`'s `deadline_time` is
  authoritative and the warehouse does not store it yet.

`deploy/fpl-cron.sh --dry-run daily` and `--dry-run deadline` print what
each job would do without touching anything, which is the quickest way to see what
the scheduler currently believes.

## What ends up in git

| Path | Tracked | Why |
| --- | --- | --- |
| `src/`, `tests/`, `deploy/`, `CLAUDE.md`, `Makefile` | yes | the system and how to run it |
| `.claude/skills/`, `.claude/settings.json`, `.claude/hooks/` | yes | shared: the workflow, the command allowlist, and the pre-commit test hook |
| `.claude/settings.local.json` | no | personal, and globally gitignored on this machine |
| `learnings/*.md` | yes, once written | the reasoning, which is not re-derivable |
| `logs/actions.jsonl` | yes, once written | what was decided, append-only |
| `docs/` | yes | plan and conventions |
| `data/fpl.db` | no | derived, and re-fetchable except for the snapshot history |
| `fpl-agent.ini` | no | credentials |
| the token cache | no | a live bearer credential, and it rotates |

Neither `learnings/` nor `logs/` is in the checkout yet, because the loop that fills them
has not run: `settle --learn` creates the first and `recommend --record` the second, each
making its own directory. An empty repo is the honest state - a placeholder committed
ahead of them would make a fresh clone look as though the loop had already produced
something. Once a file appears there, commit it.

The database is the one asset that is *not* re-fetchable in full - past snapshots
cannot be recovered - but it is too large and too churny for git. Back it up
separately if the snapshot history matters:

```bash
sqlite3 data/fpl.db ".backup 'backups/fpl-$(date -u +%F).db'"
```

# fpl-agent

A Fantasy Premier League decision engine. It captures the market daily, projects expected
points for every player, ranks transfers against your own league's ownership, and grades
its own projections once a gameweek finishes.

**No AI subscription is required to run it.** The engine is plain Python and calls no
language model: `make deadline`, `make settle` and the unattended cron jobs need no Claude
subscription, no API key and no AI tool of any kind. The `.claude/skills/` directory adds
judgement on top for anyone who runs it from Claude Code, and is the only part that needs
one.

## Install

Prerequisites: **Python 3.10 or newer**, `git`, `make`, and [uv](https://docs.astral.sh/uv/),
which owns the virtualenv and the lockfile:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # installs uv to ~/.local/bin
```

Debian 11 and Ubuntu 20.04 ship Python 3.9, which `uv sync` refuses. `uv python install 3.12`
fetches a newer interpreter without touching the system one.

```bash
git clone git@github.com:RikardFahlstrom/fpl-agent.git
cd fpl-agent
uv sync                                    # creates .venv, which the Makefile expects
uv run playwright install chromium         # only for the first credential login
```

On a server, clone over HTTPS instead — `https://github.com/RikardFahlstrom/fpl-agent.git`.
A scheduled `git pull` cannot answer a passphrase prompt, so SSH there means a key with no
passphrase or an agent that has to survive reboots. The server only ever reads this repo.
The crontab examples below assume it is checked out at `/srv/fpl-agent`; anywhere the
service user can write is fine, as long as the paths match.

On a Linux box, one system package first: `flock`, from `util-linux`, which serialises
the scheduled jobs. The scheduled jobs do not need the `sqlite3` *command* — python's
`sqlite3` module is a different thing, and is what the warehouse is read with. The
scheduler used to require the CLI and refuse to start without it, long after the queries
that needed it had moved into the engine. Install it anyway if you want the backup
recipe in `docs/SCHEDULING.md` or the comparison harness in `tools/`; nothing that runs
unattended does.

```bash
sudo apt install util-linux
uv run playwright install-deps chromium    # needs root; Chromium's shared libraries
```

`install-deps` is the step most often missed on a fresh server: without it Chromium fails
to start with a loader error rather than anything that names the cause.

## Configure

```bash
cp fpl-agent.ini.example fpl-agent.ini
chmod 600 fpl-agent.ini              # it holds a password in plaintext
```

```ini
[auth]
auto_login = true
email = you@example.com
password = ...

[rivals]
leagues = 920863          ; measure ownership against these leagues only
                          ; leave empty to use every private league you are in

[notify]
ntfy_topic =              ; a long random string; see "Run it on a server"
```

Every setting is also an environment variable (`FPL_AUTO_LOGIN`, `FPL_EMAIL`, …) and
**the environment wins**, so a scheduled run can override the file. `*.ini` is gitignored.
After the first login a token is cached in `~/.config/fpl-agent/session.json`, and
credentials are no longer needed.

## Run

```bash
make now             # do whatever is due today; DRY=--dry-run to see it decide first
make status          # read-only: does the warehouse agree with itself, and what is next?
make deadline        # the deadline half: re-capture and rank, if one is near
make settle GW=3     # after a gameweek: grade projections, draft a learning
make brief           # write logs/gwNN.md: what changed and what needs you
make test
```

**Start with `make now`.** It asks the warehouse what is due and runs only the steps that
answer — capture and project, settle any gameweek that has finished and never been graded,
and rank transfers if a deadline is close. It is safe to run on a day when the answer is nothing, which
is most days, and `make now DRY=--dry-run` shows the decision without acting on it. The
targets below it are the individual steps, for when you want one.

`make deadline` runs the same half of the pipeline the hourly cron job runs, and asks the
same question first: it does nothing when no deadline is near, and when one is it
re-captures, projects, captures rivals, ranks, and ends by checking the state it claims to
have left behind. The order matters — actuals feed the projection's rates, and rivals must
exist before ownership means anything — and it is stated once, in `engine/schedule`.
`settle` reads
`finished` from the warehouse rather than the API, so a gameweek cannot be graded until a
snapshot has refreshed its fixtures — which is one of the things `make now` gets right for
you. Individual steps are `fpl-agent snapshot`, `fpl-agent project`, `fpl-agent rivals`,
`fpl-agent recommend`, `fpl-agent settle` and `fpl-agent status` — run `fpl-agent` for
the list. `make record` logs the move you actually made; nothing records for you.

Snapshot daily. `bootstrap-static` serves current state only — prices, ownership and
price forecasts are overwritten in place with no history endpoint — so a day not captured
can never be recovered. See [docs/SCHEDULING.md](docs/SCHEDULING.md) for the unattended
setup: cron calls `deploy/fpl-cron.sh`, which takes a lock and asks the engine's
schedule what is due.

Snapshotting **refuses to run** if it cannot capture your squad, because selling prices,
bank and free transfers exist in no public endpoint. Pass `--allow-partial` to take the
market alone.

## Run it on a server

The intended deployment: cron, no human present, and **no automated transfers**. The agent
captures, projects and recommends; you read the brief and make the move. There is no code
path that executes a transfer, and that is the reason a bearer token can sit on a remote
host at all.

After Install and Configure above, on the server itself:

**1. Pick an ntfy topic and treat it as a password.** ntfy has no accounts, so the topic
*is* the address and the credential — anyone who guesses it reads your squad and your
moves. Not `fpl`, not your name.

```bash
python3 -c "import secrets; print('fpl-' + secrets.token_urlsafe(24))"
```

Put it in `fpl-agent.ini` under `[notify] ntfy_topic`, then subscribe to that exact string
in the ntfy phone app. Set `token_cache` to an absolute path on a persistent disk while
you are there.

**2. Log in once.** This is the only step that needs a browser. A server has no `DISPLAY`,
so Chromium runs headless automatically:

```bash
.venv/bin/fpl-agent snapshot --force        # look for "Captured API token"
```

After this the cached refresh token renews the session and Chromium is only a fallback.
**Do not copy the token cache from another machine**: the account service rotates the
refresh token on every exchange, so two hosts sharing one would fight and both lose.

**3. Check before scheduling anything.**

```bash
.venv/bin/fpl-agent status                  # exit 0, and read the token line
.venv/bin/fpl-agent notify --dry-run        # what would reach your phone, and why not
./deploy/fpl-cron.sh --dry-run daily
./deploy/fpl-cron.sh --dry-run deadline
```

The dry runs are one read each and print what the job is due to do and why, which is what
catches a misconfiguration before it becomes a week of jobs quietly doing nothing.

**4. Schedule it.** Cron fires dumbly and often; the guards decide whether there is work.

```cron
CRON_TZ=UTC
MAILTO=you@example.com
PATH=/usr/local/bin:/usr/bin:/bin:/home/YOU/.local/bin

30 2 * * *  /srv/fpl-agent/deploy/fpl-cron.sh daily
7  * * * *  /srv/fpl-agent/deploy/fpl-cron.sh deadline
```

Use UTC: the API speaks it, and British Summer Time moves the UK clock twice a season.
Every job runs under `flock`, because the refresh token rotates and two concurrent jobs
would leave one holding a dead credential. The jobs themselves need nothing on `PATH` —
`fpl-cron.sh` cds into the checkout and calls `.venv/bin/fpl-agent` by relative path — but
step 5 does, which is why the line is there.

**5. Keep the checkout current, if you want that automatic.**

```cron
25 2 * * *  cd /srv/fpl-agent && flock -n /tmp/fpl-agent.lock -c 'git checkout -- "logs/gw*.md" 2>/dev/null; git pull -q --ff-only && uv sync >/dev/null'
```

Four things in that line are load-bearing. It takes **the same lock** as the jobs, so a
pull cannot rewrite `.py` files under a run that is mid-snapshot; `-n` means it gives up
rather than queueing, and tomorrow's run gets it. It discards the **brief** first, because
`brief` rewrites the tracked `logs/gwNN.md` every run and `--ff-only` refuses a dirty tree
— that discards nothing a server was keeping, since nothing commits it there, but see
[docs/SCHEDULING.md](docs/SCHEDULING.md) if you want that record to survive. **`--ff-only`**
fails loudly instead of quietly merging on a host nobody is watching. And **`uv sync`**
follows, because a pull that moves `uv.lock` otherwise leaves `.venv` stale and the next
run is the thing that discovers it. Quiet on success, or `MAILTO` gets "Already up to
date." every morning until you stop reading it.

That `uv` has to be findable, and by default it is not. Cron runs with a minimal `PATH`,
usually `/usr/bin:/bin`, while uv installs to `~/.local/bin` — so without the `PATH` line
above, `git pull` succeeds and `uv sync` does not. The checkout moves to new code while
`.venv` stays on the old lockfile, which is precisely the breakage the `uv sync` was added
to prevent, and the next run is what discovers it. An absolute path to `uv` in the line
works just as well.

This makes `MODEL_VERSION` load-bearing rather than a nicety: the server starts projecting
with new code the morning after you push, and the bump is the only thing that keeps what
the model used to believe distinguishable from what it believes now. If that is not a
trade you want, pull by hand — pushes to this repo are deliberate and infrequent.

Anything non-zero gets mailed to you, and each exit code names one failure —
`4` the squad was not captured, `7` the warehouse disagrees with itself, `8` a
notification failed. The full table, the trigger set and the reasoning are in
[docs/SCHEDULING.md](docs/SCHEDULING.md).

## Use from Claude Code

Optional. A session started in this checkout loads the skills in `.claude/skills/`: the
`/fpl-*` ones — `/fpl-deadline`, `/fpl-settle`, `/fpl-verify` — wrap the `make` targets
with what to check and when not to act; `ls .claude/skills/` is the current list. The
skills read the same warehouse and logs the commands write, so what Claude sees is what
`make status` sees, not a second opinion.

## How it works

| Stage | What it does |
| --- | --- |
| `snapshot` | Market, fixtures, scoring rules, your squad and predicted lineups into SQLite |
| `projection` | Expected points per player over a 3-gameweek horizon, weights read from FPL's own `game_config` |
| `rivals` | Rival squads from your leagues, for ownership relative to the people you actually play |
| `recommend` | Ranks transfers on projected gain, price-window urgency and league ownership |
| `settle` | Grades projections against actuals, slices the error, drafts a learning |

Projections carry a `model_version`, so a weight change is measured against the previous
version on the same gameweeks rather than silently replacing it.

## Layout

```
src/fpl_agent/
  api/               the FPL side of the wire: client, auth, headless_auth,
                     account; and rotowire_scraper, the one other source
  engine/            snapshot, actuals, lineups, projection, scoring, pricing,
                     rivals, recommend, settle, status, brief, notify, storage
  config, cli        settings from fpl-agent.ini; the `fpl-agent` command
.claude/skills/      /fpl-deadline, /fpl-settle, /fpl-verify,
                     /claude-md-review
deploy/              fpl-cron.sh, the unattended entry point; a crontab names it by
                     path, so it does not move
tools/               the schedule comparison harness; run by hand, and nothing
                     deployed depends on it
docs/                PLAN.md, SCHEDULING.md, FACTS.md, adr/
learnings/           what the model learned, as markdown with frontmatter
logs/actions.jsonl   decisions taken, append-only
```

The last two are tracked but not yet present: `fpl-agent settle --learn` and
`fpl-agent recommend --record` create them on first write.

`data/fpl.db` and `fpl-agent.ini` are gitignored. Conventions and invariants are in
[CLAUDE.md](CLAUDE.md) and the roadmap in [docs/PLAN.md](docs/PLAN.md).

## Licence

MIT — see [LICENSE](LICENSE). The FPL API client and browser login in `api/` derive
from [lewis-king/fpl-mcp-server](https://github.com/lewis-king/fpl-mcp-server), also
MIT; its notice is in [LICENSE-THIRD-PARTY](LICENSE-THIRD-PARTY).

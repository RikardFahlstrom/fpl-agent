# fpl-agent

A Fantasy Premier League decision engine. It captures the market daily, projects expected
points for every player, ranks transfers against your own league's ownership, and grades
its own projections once a gameweek finishes. An MCP server exposes the same data to
Claude.

## Install

```bash
git clone git@github.com:RikardFahlstrom/fpl-agent.git
cd fpl-agent
uv sync                                    # creates .venv, which the Makefile expects
uv run playwright install chromium         # only for the first credential login
```

On a server, clone over HTTPS instead — `https://github.com/RikardFahlstrom/fpl-agent.git`.
A scheduled `git pull` cannot answer a passphrase prompt, so SSH there means a key with no
passphrase or an agent that has to survive reboots. The server only ever reads this repo.

On a Linux box, two system packages first. `sqlite3` is the CLI, which is a separate
package from python's `sqlite3` module — `deploy/fpl-cron.sh` uses the command to ask the
warehouse what needs doing, and without it the scheduled jobs refuse to run rather than
guessing. `flock` comes from `util-linux` and serialises those jobs.

```bash
sudo apt install sqlite3 util-linux
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
read_only = true          ; refuse make_transfers; analyse and report only

[rivals]
leagues = 920863          ; measure ownership against these leagues only

[notify]
ntfy_topic =              ; a long random string; see "Run it on a server"
```

Every setting is also an environment variable (`FPL_AUTO_LOGIN`, `FPL_EMAIL`, …) and
**the environment wins**, so a scheduled run can override the file. `*.ini` is gitignored.
After the first login a token is cached in `~/.config/fpl-mcp/session.json`, and
credentials are no longer needed.

## Run

```bash
make deadline        # snapshot, backfill, project, capture rivals, recommend, status
make settle GW=3     # after a gameweek: grade projections, draft a learning
make brief           # write logs/gwNN.md: what changed and what needs you
make status          # read-only: does the warehouse agree with itself?
make test
```

`make deadline` runs the steps in order because the order matters: actuals feed the
projection's rates, and rivals must exist before ownership means anything. Individual steps are `fpl-agent snapshot`, `fpl-agent project`, `fpl-agent rivals`,
`fpl-agent recommend`, `fpl-agent settle` and `fpl-agent status` — run `fpl-agent` for
the list. `make record` logs the move you actually made; nothing records for you.

Snapshot daily. `bootstrap-static` serves current state only — prices, ownership and
price forecasts are overwritten in place with no history endpoint — so a day not captured
can never be recovered. See [docs/SCHEDULING.md](docs/SCHEDULING.md) for the unattended
setup: cron calls `deploy/fpl-cron.sh`, which decides whether there is anything to do.

Snapshotting **refuses to run** if it cannot capture your squad, because selling prices,
bank and free transfers exist in no public endpoint. Pass `--allow-partial` to take the
market alone.

## Run it on a server

The intended deployment: cron, no human present, and **no automated transfers**. The agent
captures, projects and recommends; you read the brief and make the move. `read_only = true`
is what enforces that, and it is the reason a bearer token can sit on a remote host at all.

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

The dry runs cost ten seconds and are what catches a missing `sqlite3` before it becomes a
week of jobs quietly doing nothing.

**4. Schedule it.** Cron fires dumbly and often; the guards decide whether there is work.

```cron
CRON_TZ=UTC
MAILTO=you@example.com

30 2 * * *  /srv/fpl-agent/deploy/fpl-cron.sh daily
7  * * * *  /srv/fpl-agent/deploy/fpl-cron.sh deadline
```

Use UTC: the API speaks it, and British Summer Time moves the UK clock twice a season.
Every job runs under `flock`, because the refresh token rotates and two concurrent jobs
would leave one holding a dead credential.

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

This makes `MODEL_VERSION` load-bearing rather than a nicety: the server starts projecting
with new code the morning after you push, and the bump is the only thing that keeps what
the model used to believe distinguishable from what it believes now. If that is not a
trade you want, pull by hand — pushes to this repo are deliberate and infrequent.

Anything non-zero gets mailed to you, and each exit code names one failure —
`4` the squad was not captured, `7` the warehouse disagrees with itself, `8` a
notification failed. The full table, the trigger set and the reasoning are in
[docs/SCHEDULING.md](docs/SCHEDULING.md).

## Use from Claude

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows:
`%APPDATA%\Claude\claude_desktop_config.json`), replacing the path:

```json
{
  "mcpServers": {
    "fpl": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/fpl-agent",
               "run", "python", "-m", "fpl_agent.main"],
      "env": { "PYTHONPATH": "/ABSOLUTE/PATH/TO/fpl-agent/src" }
    }
  }
}
```

Use `--directory` rather than `cwd`: Claude Desktop does not reliably apply `cwd` before
`uv` resolves the project. Restart Claude fully, then check Settings → Connectors.

The server exposes 32 tools, 17 resources (`fpl://…`) and 7 prompts. Ask in names, not
ids: *"compare Salah and Haaland"*, *"who should I transfer out?"* Most of them are a
browsing view of the live FPL API and need a session; `recommend_transfers` is the
exception — it reads the warehouse `make deadline` left behind, so it answers offline
and gives the same ranking as `fpl-agent recommend` rather than a second opinion.

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
  engine/            capture, actuals, projection, pricing, rivals, recommend,
                     settle, status, brief, notify
  mcp/               server: tools/, resources, prompts, web
  (root)             auth, client, config, models, state, rotowire_scraper
.claude/skills/      /fpl-deadline, /fpl-settle, /fpl-verify
deploy/              fpl-cron.sh, the unattended entry point
docs/                PLAN.md, SCHEDULING.md
learnings/           what the model learned, as markdown with frontmatter
logs/actions.jsonl   decisions taken, append-only
```

The last two are tracked but not yet present: `fpl-agent settle --learn` and
`fpl-agent recommend --record` create them on first write.

`data/fpl.db` and `fpl-agent.ini` are gitignored. Conventions and invariants are in
[CLAUDE.md](CLAUDE.md), the roadmap in [docs/PLAN.md](docs/PLAN.md), and a brief for
handing the repo to an external reviewer in [docs/REVIEW-PROMPT.md](docs/REVIEW-PROMPT.md).

## Credit

Forked from [lewis-king/fpl-mcp-server](https://github.com/lewis-king/fpl-mcp-server).

## Licence

MIT — see [LICENSE](LICENSE).

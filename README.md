# fpl-agent

A Fantasy Premier League decision engine. It captures the market daily, projects expected
points for every player, ranks transfers against your own league's ownership, and grades
its own projections once a gameweek finishes. An MCP server exposes the same data to
Claude.

## Install

```bash
git clone git@github.com:RikardFahlstrom/fpl-agent.git
cd fpl-agent
uv sync
uv run playwright install chromium   # only for credential login
```

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
```

Every setting is also an environment variable (`FPL_AUTO_LOGIN`, `FPL_EMAIL`, …) and
**the environment wins**, so a scheduled run can override the file. `*.ini` is gitignored.
After the first login a token is cached in `~/.config/fpl-mcp/session.json`, and
credentials are no longer needed.

## Run

```bash
make deadline        # snapshot, backfill, project, capture rivals, recommend
make settle GW=3     # after a gameweek: grade projections, draft a learning
make test
```

`make deadline` runs the steps in order because the order matters: actuals feed the
projection's rates, and rivals must exist before ownership means anything. Individual steps are `fpl-agent snapshot`, `fpl-agent project`, `fpl-agent rivals`,
`fpl-agent recommend`, `fpl-agent settle` — run `fpl-agent` for the list.

Snapshot daily. `bootstrap-static` serves current state only — prices, ownership and
price forecasts are overwritten in place with no history endpoint — so a day not captured
can never be recovered. See [docs/SCHEDULING.md](docs/SCHEDULING.md) for a launchd job.

Snapshotting **refuses to run** if it cannot capture your squad, because selling prices,
bank and free transfers exist in no public endpoint. Pass `--allow-partial` to take the
market alone.

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

The server exposes 33 tools, 17 resources (`fpl://…`) and 7 prompts. Ask in names, not
ids: *"compare Salah and Haaland"*, *"who should I transfer out?"*

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
  engine/            capture, projection, pricing, rivals, recommend, settle
  mcp/               server: tools/, resources, prompts, web
  (root)             auth, client, config, models, state, rotowire_scraper
.claude/skills/      /fpl-deadline, /fpl-settle, /fpl-verify
docs/                PLAN.md, SCHEDULING.md
learnings/           what the model learned, as markdown with frontmatter
logs/actions.jsonl   decisions taken, append-only
```

`data/fpl.db` and `fpl-agent.ini` are gitignored. Conventions and invariants are in
[CLAUDE.md](CLAUDE.md), the roadmap in [docs/PLAN.md](docs/PLAN.md), and a brief for
handing the repo to an external reviewer in [docs/REVIEW-PROMPT.md](docs/REVIEW-PROMPT.md).

## Credit

Forked from [lewis-king/fpl-mcp-server](https://github.com/lewis-king/fpl-mcp-server).

## Licence

MIT — see [LICENSE](LICENSE).

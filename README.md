# fpl-agent

A Fantasy Premier League decision engine. Run by hand before a deadline, it captures the
market, projects expected points for every player, and produces one set of **Instructions**
for the gameweek — the transfers to make (possibly several, possibly none), the chip to play
or to hold, the captain and vice-captain, the starting eleven and the order of the bench —
chosen for where you stand in your own league. After the gameweek it grades its projections
and its Instructions against what happened.

**No AI subscription is required to run it.** The engine is plain Python and calls no
language model: `make now`, `make settle` and `make record` need no Claude subscription, no
API key and no AI tool of any kind. The `.claude/skills/` directory adds judgement on top
for anyone who runs it from Claude Code, and is the only part that needs one.

Nothing here ever executes a transfer. The agent tells you what to do; you do it.

## Install

Prerequisites: **Python 3.10 or newer**, `git`, `make`, and [uv](https://docs.astral.sh/uv/),
which owns the virtualenv and the lockfile:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # installs uv to ~/.local/bin
```

```bash
git clone git@github.com:RikardFahlstrom/fpl-agent.git
cd fpl-agent
uv sync                                    # creates .venv, which the Makefile expects
uv run playwright install chromium         # only for the first credential login
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

[rivals]
leagues = 920863          ; measure ownership against these leagues only; the first one
                          ; is the league your stance (Protect or Chase) is set from
                          ; required: rivals and recommend stop while it is empty
```

Every setting is also an environment variable (`FPL_AUTO_LOGIN`, `FPL_EMAIL`, …) and
**the environment wins**. `*.ini` is gitignored. After the first login a token is cached in
`~/.config/fpl-agent/session.json`, and credentials are no longer needed.

**Log in once.** This is the only step that needs a browser:

```bash
.venv/bin/fpl-agent snapshot --force        # look for "Captured API token"
```

After this the cached refresh token renews the session and Chromium is only a fallback. **Do
not copy the token cache from another machine**: the account service rotates the refresh
token on every exchange, so two hosts sharing one would fight and both lose.

## Run

```bash
make now             # the one to reach for: capture, settle, update every number, advise
make now DRY=--dry-run   # see what it would do first
make status          # read-only: does the warehouse agree with itself, and what is next?
make record ARGS='--move 1 --captain Haaland --chip none'   # what you actually did
make settle GW=3     # settle one gameweek by hand: grade, draft a learning
make brief           # rewrite logs/gwNN.md without capturing again
make test
```

**`make now` is the only entry point, and it runs one job.** Every run captures the market
and your squad first (the snapshot is the one thing that can never be recovered), backfills
actuals, projects, refreshes the rivals and the league table, settles any gameweek that has
finished and never been graded, then works out the Instructions and writes the brief. It
never waits for a deadline to be near: the hours to the deadline are a caveat — far out, it
says the predicted lineups are not yet firm — not a condition. Once the deadline has passed
it says the gameweek is **locked**, when FPL rolls over, and advises for nothing until a
capture targets the next one. It holds a lock for its whole length, so two runs cannot both
refresh the login. It is safe to run any number of times.

**The Instructions** open the terminal output and the brief, in a fixed order every run:

- **Stance** — Protect when you are first in your league (stay close to what the field
  holds), otherwise Chase (prefer players few rivals hold, pulling harder the further
  behind). One small weight, `stance.RANK_WEIGHT`, adjusts the moves, the captain and the
  chip; it is uncalibrated, and at zero the advice is plain expected points. The line says
  the mode, the gap, and what it changed. Stale or missing ownership is never used — the line
  says so instead.
- **Chip** — no chip, bench boost, triple captain, free hit and wildcard are compared as
  whole alternatives, each with its own best moves, each scored as net expected points
  against doing nothing. Each chip still has to clear its own bar and be the best remaining
  week within a noise band; otherwise the answer is hold, with the margin to the best
  alternative. A chip that is played, expired or not yet open is reported as it is and never
  valued.
- **Moves** — how many transfers to make over the 3-gameweek horizon, netting out 4-point
  hits beyond your free transfers, with a default hit cap of 2 (a chip lifts it) and the club
  limit. Beside it: doing nothing, and the best single swap, so the value of extra moves is
  visible. With nothing worth doing it says *no move* and gives the margin. The solver
  proves its answer optimal or says it did not; a partial answer is never presented as advice.
- **Starting XI, Captain, Vice-captain, Bench** — the best legal eleven on the squad after
  the moves, the captain with the why, a vice-captain, and the bench in the order the
  automatic substitutions will use it, each with a short reason.

Each run appends one record of its Instructions to the warehouse, tagged with the model
version and the capture it came from. `make settle` later grades the last one before the
deadline in the points actually scored: the captain against the other starters, the XI and
the bench order against the best possible, and the plan against doing nothing.

**Recording is a claim, not a step.** `make record` records exactly what it is told:
`--move N` for each move of the plan you made (or `--all`, if you made all of it), `--captain
NAME`, `--chip CHIP|none`. Captain and chip are stored beside the recommended ones so
deviations can be graded. A bare `make record` records nothing.

**Reading the brief.** `logs/gwNN.md` opens with the Instructions block and then the same
lines every run — Ownership, Chips, Availability, Deadline, Learnings, Data — each saying
"none" or "not evaluated" when there is nothing, so you look at the same line every time.
The sections beneath show the working. Chips are valued week by week to the set's expiry
(`project --chips` projects that far); `## Chips` shows it. Ownership is how many of the
rivals in your leagues hold each player, and is withheld (with the reason) when the rival
picks are older than the last finished gameweek. Gameweek references include deadline dates
in Stockholm time. The previous-round line says whether it is settled and, once settled,
summarises your rank movement and gap to the leader from final saved standings.

Free Hit, Wildcard and the transfer plan use a MILP solver over the stored projections,
maximising the XI plus the existing discounted bench value under the budget, squad shape and
club limit. A solve that cannot establish optimality within 30 seconds fails explicitly. To
compare it with the previous greedy search, without writing to the warehouse, run
`.venv/bin/python tools/benchmark-squad.py` (or pass `--db PATH`).

The individual steps are `fpl-agent snapshot`, `project`, `rivals`, `recommend`, `settle`,
`record`, `status` and `brief` — run `fpl-agent` for the list. The order matters: actuals
feed the projection's rates, and rivals must exist before ownership means anything. It is
stated once, in `engine/schedule`. `settle` reads `finished` from the warehouse rather than
the API, so a gameweek cannot be graded until a snapshot has refreshed its fixtures — which
is one of the things `make now` gets right for you.

Snapshot before every decision. `bootstrap-static` serves current state only — prices,
ownership and price forecasts are overwritten in place with no history endpoint — so a day
not captured can never be recovered. Snapshotting **refuses to run** if it cannot capture
your squad, because selling prices, bank and free transfers exist in no public endpoint. Pass
`--allow-partial` to take the market alone. See [docs/SCHEDULING.md](docs/SCHEDULING.md) for
what a run does, its lock, and the exit codes.

## Use from Claude Code

Optional. A session started in this checkout loads the skills in `.claude/skills/`: the
`/fpl-*` ones — `/fpl-deadline`, `/fpl-settle`, `/fpl-learn`, `/fpl-verify` — wrap the
`make` targets with what to check and when not to act (`/fpl-learn` walks the proposed
learnings with you, in plain words, and applies or rejects each only on your say-so); `ls
.claude/skills/` is the current list. The skills read the same warehouse and logs the
commands write, so what Claude sees is what `make status` sees, not a second opinion.

## How it works

| Stage | What it does |
| --- | --- |
| `snapshot` | Market, fixtures, scoring rules, your squad and predicted lineups into SQLite |
| `projection` | Expected points per player over a 3-gameweek horizon, weights read from FPL's own `game_config` |
| `rivals` | Rival squads and the league table from your leagues, for ownership and for your stance |
| `instructions` | The XI, captain, vice-captain, bench, the transfer plan and the chip, as one value, stored per run |
| `settle` | Grades projections against actuals, slices the error, drafts a learning, grades the Instructions |
| `record` | Logs the moves, captain and chip you actually played |

Projections carry a `model_version`, so a weight change is measured against the previous
version on the same gameweeks rather than silently replacing it.

## Layout

```
src/fpl_agent/
  api/               the FPL side of the wire: client, auth, headless_auth,
                     account; and rotowire_scraper, the one other source
  engine/            snapshot, actuals, lineups, projection, scoring, pricing,
                     rivals, recommend, moves, stance, instructions, grading,
                     recording, settle, status, brief, schedule, storage
  config, cli        settings from fpl-agent.ini; the `fpl-agent` command
.claude/skills/      /fpl-deadline, /fpl-settle, /fpl-learn, /fpl-verify,
                     /claude-md-review
tools/               one-off developer scripts; nothing depends on them
docs/                PLAN.md, SCHEDULING.md, FACTS.md, adr/
learnings/           what the model learned, as markdown with frontmatter
logs/                the gameweek briefs, and actions.jsonl: decisions taken, append-only
```

`logs/actions.jsonl` is created by the first `fpl-agent record` run and `learnings/` by the
first `fpl-agent settle --learn`; both are tracked once they exist.

`data/fpl.db` and `fpl-agent.ini` are gitignored. Conventions and invariants are in
[CLAUDE.md](CLAUDE.md) and the roadmap in [docs/PLAN.md](docs/PLAN.md).

## Licence

MIT — see [LICENSE](LICENSE). The FPL API client and browser login in `api/` derive
from [lewis-king/fpl-mcp-server](https://github.com/lewis-king/fpl-mcp-server), also
MIT; its notice is in [LICENSE-THIRD-PARTY](LICENSE-THIRD-PARTY).

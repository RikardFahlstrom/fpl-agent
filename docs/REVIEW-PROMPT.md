# External review prompt

A self-contained brief for handing this repository to a fresh model for review. It
assumes no prior context, so it states the run commands, the conventions and what is
already known — the reviewer should spend its effort on what is *not*.

Copy everything inside the fence.

---

````markdown
You are reviewing a Python project, `fpl-agent`, at the repository root. Do not
change any files, do not commit, and do not push. Your entire output is a set of
instructions for a Claude Opus model to carry out afterwards.

## Step 1 — ask me first

Before reviewing anything, ask me about the project's purpose, who uses it, what
"done" looks like, and what I intend to do with it over the next few months. Ask
whatever else you need to judge whether the current design serves those goals.
Ask up to about six questions, then STOP and wait for my answers. Do not begin
the review until I have replied.

## Step 2 — then review, in this order

**a. Structure.** Is the layout right for what this is becoming? Look at package
boundaries, module responsibilities, dependency direction, and whether names say
what things do. Say specifically what is confusing and what you would rename,
split, merge or delete.

**b. Correctness.** Read every file under `src/`, `tests/`, `docs/`,
`.claude/`, plus `CLAUDE.md`, `Makefile` and `pyproject.toml`. Look for bugs,
silent-failure paths, unhandled edge cases, wrong assumptions about external
data, and gaps in test coverage. Prioritise defects that would produce a
confident wrong answer over ones that crash — this project's history is almost
entirely the former.

**c. Claude-efficiency.** Assess whether the repo uses Claude Code well:
`CLAUDE.md` content and length, the skills in `.claude/skills/`, whether hooks
or settings would help, whether anything that should be a skill is prose and
vice versa, and whether the human/automated split is sensible. Say what is
missing and what is ceremony.

## How to run things

```bash
uv sync
make test            # offline suite, ~200 tests, must stay green
fpl-agent            # lists the commands
```

The live FPL API needs credentials in `fpl-agent.ini` (gitignored) which you do
not have, so you cannot run `fpl-agent snapshot` or anything downstream of it.
Read `docs/PLAN.md` for the roadmap and `CLAUDE.md` for the invariants.

## Ground rules

- **Verify, do not assume.** Read the file or run the code before asserting what
  it does. If you cannot verify something, say so explicitly rather than
  asserting it confidently.
- Distinguish **deliberate** design from **accidental** mess. Several apparent
  inconsistencies are documented choices; check the docstring before flagging
  one, and if it is deliberate but undocumented, say that instead.
- `docs/PLAN.md` §9 lists open questions and `CLAUDE.md` lists known invariants.
  Do not re-report those as discoveries — focus on what is *not* already known.
- Nothing under `data/` is in git; it is derived and re-fetchable.

## Output format

A single ordered list of change instructions, most valuable first. No code — an
Opus model will write it. Each instruction must have:

1. **Title** — one line, imperative.
2. **Why** — the concrete cost of leaving it alone. If it is a bug, give the
   inputs and the wrong output it produces.
3. **Scope** — the files or modules affected, and explicitly what is *out* of
   scope.
4. **Acceptance** — how the Opus model will know it is done. Prefer a test that
   would fail today, or an observable behaviour change.
5. **Risk** — what could break, and what to verify before and after.
6. **Size** — trivial / small / medium / large.

Group them under **Do now**, **Do next**, and **Consider**, and put anything you
are unsure about in a final **Needs a decision from the human** section with the
question stated plainly.

Be blunt about severity. If something is fine, say it is fine and move on — do
not manufacture findings to fill a section.
````

---

## Why it is shaped this way

**Step 1 stops and waits.** Without an explicit instruction to stop, a model tends to
ask its questions and then answer them itself in the same turn, which defeats the point.

**"Confident wrong answers over crashes"** is the most load-bearing line. Nearly every
real defect in this project was code reporting success for something that did not happen:
a preflight announcing a squad capture when nothing had logged in; settling grading 651
players against a gameweek that had not kicked off; ownership discarding 165 of 200
candidates as "unknown". Crashes announce themselves. These do not.

**Pointing at what is already known** keeps the review from spending itself rediscovering
decisions that are recorded in `docs/PLAN.md` and `CLAUDE.md`.

**Instructions, not patches.** The reviewer names the change and how to know it is done;
the model doing the work writes the code with the repository in front of it. Acceptance
criteria phrased as "a test that would fail today" keeps findings falsifiable.

---
name: claude-md-review
description: Audit an existing CLAUDE.md or AGENTS.md against context-engineering principles, interview the user to fill in what's missing, and produce a rewritten file. Use this whenever the user mentions reviewing, auditing, cleaning up, shortening, improving or writing a CLAUDE.md / AGENTS.md, says their agent "ignores CLAUDE.md" or "doesn't follow instructions", or asks how to onboard Claude Code into a repo — even if they don't use the words "review" or "skill".
---

# CLAUDE.md review

`CLAUDE.md` is loaded into every single session, so it is the highest-leverage
file in the harness and also the easiest one to ruin. This skill turns a bloated,
accreted file into a short, universally applicable one — and asks the user for
what's missing rather than inventing it.

Two things this skill must never do:

- **Never auto-generate.** Every line has to be justified out loud. `/init`-style
  output is what created the problem in most repos.
- **Never guess at facts.** If the repo doesn't reveal it and the user hasn't said
  it, ask. A confidently wrong line in `CLAUDE.md` poisons every future session.

## The principles being applied

1. **Onboarding, not rulebook.** The file answers WHY (what this project is for),
   WHAT (a map of the code) and HOW (how to build, test and verify a change).
2. **Fewer instructions is better.** Frontier models follow roughly 150–200
   instructions reliably, and the Claude Code system prompt already spends ~50.
   Instruction-following degrades *uniformly* as the count grows — extra rules
   don't get ignored last, they make every rule weaker.
3. **Universal applicability.** Claude Code wraps `CLAUDE.md` in a reminder that
   it may not be relevant. The more task-specific content the file holds, the more
   likely Claude discards the whole thing.
4. **Progressive disclosure.** Task-specific knowledge lives in separate files
   with self-describing names; `CLAUDE.md` lists them and says when to read them.
5. **Pointers over copies.** `file:line` references instead of pasted code, which
   goes stale.
6. **Claude is not a linter.** Style and formatting belong to a formatter, a
   linter, or a `Stop` hook — not to instructions.
7. **Budget.** Aim for under ~60 lines in the root file. Treat 300 as a hard
   ceiling, not a target.

Present these as heuristics from one team's practice, not as law. If the user has
a considered reason to break one, record the reason and move on.

## Workflow

### 1. Locate everything

Find `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md` at the repo root **and in
subdirectories** — nested files stack in monorepos. Resolve any `@path` imports
and count their contents against the budget too. Check `~/.claude/CLAUDE.md` if
the user is debugging global behaviour. If nothing exists, skip to step 4 and run
the interview greenfield.

### 2. Ground yourself in the repo before judging anything

You cannot tell whether "always use `bun`" is universally applicable without
knowing the repo. Before forming an opinion, read:

- top-level directory structure, and workspace/monorepo config
- `package.json` / `pyproject.toml` / `Makefile` / `justfile` scripts
- CI workflow files — these show the commands that actually matter
- linter and formatter config — anything covered here should leave `CLAUDE.md`
- any existing `agent_docs/`, `docs/`, `CONTRIBUTING.md`

### 3. Audit

Produce a short table of findings. For each line or block in the current file,
classify it as **keep**, **move** (to a task-specific doc), **delegate** (to a
linter, hook, or slash command), or **drop** (obsolete, generic, or discoverable
in one search).

Measure and report: total lines, approximate instruction count (count imperatives,
not bullets), number of inline code blocks, and how much of the file applies to
every task versus some tasks.

High-frequency offenders worth naming explicitly:

| Pattern | Why it hurts | Where it goes |
|---|---|---|
| Code style and formatting rules | Slow, expensive, non-deterministic | Linter / formatter / `Stop` hook |
| Long dumps of every possible command | Dilutes the instructions that matter | Point at `package.json` or `Makefile` |
| Pasted code snippets | Goes stale silently | `file:line` pointer |
| Accreted "ALWAYS remember to…" hotfixes | Each one weakens all the others | Drop, or fix the underlying cause |
| Deployment / migration / schema procedures | Irrelevant in most sessions | `agent_docs/*.md` |
| Generic LLM etiquette ("be concise", "don't hallucinate") | Already in the system prompt | Drop |

### 4. Interview the user for the gaps

This is the core of the skill. Research first, ask second: never ask for something
the repo already answers. When you can infer an answer, state it as a draft and
ask for confirmation rather than asking an open question — it is much cheaper for
the user to correct a wrong guess than to write prose from scratch.

Ask in **small batches of three to five related questions**, grouped by theme, and
wait for answers before moving on. Say how many themes are left so the user knows
the shape of the conversation.

Draw from this bank, skipping anything already known:

**WHY** — what the project does and for whom, in one or two sentences; what a new
engineer consistently misunderstands about the domain; what is deliberately *not*
in scope for this repo.

**WHAT** — what each top-level directory is for; in a monorepo, what each app and
shared package is and which depend on which; where the real entry points are;
which directories are generated, vendored, or off-limits; where tests live
relative to source.

**HOW** — the exact install, build, test, typecheck and lint commands; how to run
a *single* test; anything non-obvious about the toolchain (`bun` not `node`, `uv`
not `pip`, a required Node version); what has to be running locally before tests
pass; how Claude verifies a change is actually correct; anything Claude must never
run unprompted.

**Gotchas** — the two or three mistakes a new contributor makes every time; any
convention that is invisible from the code itself.

For each answer, ask yourself whether it belongs in every session. If not, route
it to an `agent_docs/` file instead of the root file, and tell the user you're
doing that.

If the user doesn't know an answer, or answers vaguely, leave it out. An omission
is recoverable; a wrong line is not.

### 5. Propose before writing

Show the plan and get a yes:

- what stays in `CLAUDE.md` (with the projected line count)
- what moves out, and into which new file
- what gets delegated to tooling, with a concrete suggestion
- what gets dropped, and why
- anything you're still unsure about

### 6. Write

Back up the original (`CLAUDE.md.bak` or rely on git being clean) before
overwriting. Then produce the new root file using this shape, dropping any section
that would be empty:

```markdown
# <Project name>

<One to three sentences: what this is and why it exists.>

## Stack
<Runtime, package manager, framework, datastore — one line.>

## Map
<Path — one-line purpose. One entry per top-level area. `file:line` pointers
for the handful of places that genuinely anchor the system.>

## Commands
<Only the ones needed on essentially every task: install, test, single test,
typecheck, build.>

## Gotchas
<Only what is non-discoverable AND universally relevant. If it's longer than
three bullets, something belongs elsewhere.>

## Further reading
<agent_docs/x.md — read this when you are doing Y.>
```

Also create the extracted `agent_docs/*.md` files with the content you moved. Give
them self-describing names (`running_tests.md`, `service_architecture.md`,
`database_schema.md`) so the one-line index in `CLAUDE.md` is enough for Claude to
choose correctly.

Report the before/after line and instruction counts, and show the diff.

### 7. Hand over

Close with what wasn't solved in the file itself: linter rules worth adding, a
`Stop` hook worth configuring, a slash command worth creating, and any question
the user deferred. Suggest they re-run this review after the next significant
change to the repo's structure or tooling.

## Tone

The user probably wrote the file being reviewed. Be direct about what's wrong with
it and why, without hedging, and without apologising for the criticism either.
Explain the mechanism behind each recommendation — "this competes for the same
instruction budget as everything else" lands better than "this violates rule 2".

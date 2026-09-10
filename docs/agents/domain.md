# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists: it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## This repo

**Single-context.** One `CONTEXT.md` and one `docs/adr/` at the repo root — no
`CONTEXT-MAP.md`, no per-context directories. `CONTEXT.md` exists and holds the terms the
scheduling work resolved; `docs/adr/` does not, because nothing so far has reversed a
recorded decision. `/domain-modeling` creates them lazily, so proceed silently until then.

The rest of the standing domain documentation:

- `CLAUDE.md` — the stack, the map of `src/fpl_agent/`, the commands, and the invariants
  (read scoring weights from `game_config`; bump `MODEL_VERSION` when projections move;
  never grade or re-project a gameweek out of order). Treat the invariants as binding.
- `docs/FACTS.md` — per-subsystem facts and where they live. **Read before working in
  `engine/`.**
- `docs/SCHEDULING.md` — the unattended cron setup, exit codes, and what cron decides.
- `docs/PLAN.md` — the roadmap.
- `learnings/` and `logs/actions.jsonl` — the committed reasoning trail: what the model
  believed, what happened, and what was concluded. Prior art for any projection question.

These are not a substitute for a glossary. When a term keeps needing explanation, that is
the signal to start `CONTEXT.md` rather than to add another paragraph here.

## File structure

Single-context repo (most repos):

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

Multi-context repo (presence of `CONTEXT-MAP.md` at the root):

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_

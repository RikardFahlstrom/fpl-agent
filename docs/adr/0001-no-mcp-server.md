# No MCP server; Claude Code integrates through skills over `make`

This repo began as a fork of an MCP server that browsed the live FPL API from Claude.
By 2026-09 the engine (`make now`, the cron, the warehouse) was the whole point, the
engine never imported the server, and the server was registered on no machine. On
2026-09-12 it was deleted rather than maintained: 32 tools over a live API drift the
moment they stop being used, and every review kept flagging them as inherited debt.

The Claude Code integration is `.claude/skills/fpl-*`: judgement layered over the same
`make` targets a person runs, reading the same warehouse and logs. A chat surface over
live FPL data was the alternative and is not wanted; if one is ever wanted again, it
should be written against `engine/`, not restored from history.

One consequence worth naming: there is now no code path that executes a transfer. The
docs used to lean on `read_only = true`; the stronger statement replaced it.

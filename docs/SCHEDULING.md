# Running this every week

Three things happen on a schedule. Only one of them is judgement-free, and only that one
should be automated.

## Nightly, automated: the snapshot

Prices resolve nightly, around 01:30 UK, on a clock independent of the gameweek deadline.
A day without a snapshot is a day of price and ownership movement that can never be
recovered - `bootstrap-static` has no history endpoint.

macOS, via launchd (`~/Library/LaunchAgents/com.fplagent.snapshot.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.fplagent.snapshot</string>
  <key>WorkingDirectory</key><string>/ABSOLUTE/PATH/TO/fpl-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-lc</string>
    <string>make snapshot &amp;&amp; make backfill</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/tmp/fplagent-snapshot.log</string>
  <key>StandardErrorPath</key><string>/tmp/fplagent-snapshot.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.fplagent.snapshot.plist
```

Run it at 03:00, after prices have settled. The snapshot is idempotent per day, so a
missed run followed by a manual one costs nothing.

**It refuses to run without auth**, so a broken token cache surfaces as a failure in the
log rather than as weeks of snapshots quietly missing the squad. Check the log
occasionally; that is the failure mode worth watching for.

## Before each deadline, with a human: `/fpl-deadline`

Not automated. It ends in a decision, and the decision needs someone to weigh urgency
against expected points and league position. The skill runs `make deadline` and says what
to check.

## After each gameweek, with a human: `/fpl-settle`

Not automated either. Grading is mechanical, but deciding whether a slice is a finding or
variance is not, and a wrong call there changes the model's weights.

## What ends up in git

| Path | Tracked | Why |
| --- | --- | --- |
| `src/`, `tests/`, `.claude/skills/`, `CLAUDE.md`, `Makefile` | yes | the system and how to run it |
| `learnings/*.md` | yes | the reasoning, which is not re-derivable |
| `logs/actions.jsonl` | yes | what was decided, append-only |
| `docs/` | yes | plan and conventions |
| `data/fpl.db` | no | derived, and re-fetchable except for the snapshot history |
| `fpl-agent.ini` | no | credentials |

The database is the one asset that is *not* re-fetchable in full - past snapshots cannot be
recovered - but it is also too large and too churny for git. Back it up separately if the
snapshot history matters; `sqlite3 data/fpl.db ".backup 'somewhere/fpl-$(date +%F).db'"`.

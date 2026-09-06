#!/usr/bin/env python3
"""Run `fpl-agent status` when a turn ends, and say so if the warehouse is now
self-inconsistent.

A Stop hook. It fires when the assistant has finished replying, which is the last
moment before the inconsistency becomes something you discover on the next
`make deadline` instead of now.

Why this check and not a linter: every serious bug in this project was semantic -
a projection graded against actuals that did not exist, a snapshot that reported
a squad it never captured. All of it was syntactically perfect, and a linter sees
none of it. `status` is the cheap thing that asks whether the warehouse means what
it says: read-only, authenticates against nothing, a third of a second.

Blocking rules, in the spirit of `test-before-commit.py`: this must be hard to
make block for the wrong reason, because a hook that interrupts for a condition
you cannot act on gets switched off, and then it protects nothing.

  * It blocks on exit 7 and nothing else. 7 means the warehouse disagrees with
    itself (docs/SCHEDULING.md). 2 means the database is missing or unreadable,
    which is the normal state of a fresh clone and not a reason to interrupt.
  * It blocks at most once. `stop_hook_active` is true when this turn is already
    the continuation of an earlier block, and on that pass it always allows. A
    warehouse inconsistency often needs a snapshot, a network or a finished
    gameweek to resolve, none of which the assistant can conjure mid-turn.
  * It allows silently whenever it cannot honestly run: no repo, no console
    script, no database, an unreadable payload, or a run that overshoots the
    timeout.
"""

import json
import os
import subprocess
import sys

TIMEOUT_SECONDS = 20
INCONSISTENT = 7          # `status`: the warehouse disagrees with itself


def allow() -> None:
    """Let the turn end. Exit 0 is the only thing that matters here."""
    raise SystemExit(0)


def block(reason: str) -> None:
    """Hand the turn back to the assistant. Exit 2 is the code that means that."""
    print(reason, file=sys.stderr)
    raise SystemExit(2)


def repo_root(cwd: str) -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        allow()

    # Already the continuation of a block. Never interrupt the same turn twice.
    if event.get("stop_hook_active"):
        allow()

    root = repo_root(event.get("cwd") or os.getcwd())
    if not root:
        allow()

    agent = os.path.join(root, ".venv", "bin", "fpl-agent")
    database = os.path.join(root, "data", "fpl.db")
    # No warehouse is not an inconsistent warehouse. A fresh clone looks like this.
    if not os.path.exists(agent) or not os.path.exists(database):
        allow()

    try:
        done = subprocess.run(
            [agent, "status"], cwd=root, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        allow()

    if done.returncode != INCONSISTENT:
        allow()

    output = (done.stdout or "") + (done.stderr or "")
    failures = [line for line in output.splitlines() if line.strip().startswith("FAIL")]

    block("\n".join([
        "`fpl-agent status` exits 7: the warehouse disagrees with itself.",
        "",
        *(failures[:10] or ["(no FAIL lines - run `.venv/bin/fpl-agent status` to see it)"]),
        *([f"... and {len(failures) - 10} more"] if len(failures) > 10 else []),
        "",
        "Each FAIL names what is wrong and what to run. Nothing is broken *by* status;",
        "it only reports. If this predates the turn, or needs a snapshot or a finished",
        "gameweek to resolve, say so and stop - this will not interrupt again.",
        "This hook is .claude/hooks/status-after-turn.py.",
    ]))


if __name__ == "__main__":
    main()

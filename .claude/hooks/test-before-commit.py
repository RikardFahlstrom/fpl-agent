#!/usr/bin/env python3
"""Run the suite before a `git commit`, and block the commit if it is red.

A PreToolUse hook on Bash. The suite is ~250 tests in half a second, so paying it
on every commit costs nothing and means a commit can never be the thing that
discovers a broken test.

The design constraint is that this must be *hard to make block for the wrong
reason*. A hook that refuses every commit inside a git worktree gets switched off
within a day, and then it protects nothing at all. So it exits silently - allowing
the commit - in every case where it cannot honestly run the suite:

  * the tool call is not a `git commit`
  * the payload on stdin is not the JSON this expects
  * `git rev-parse` cannot find a repository
  * there is no `.venv/bin/python` or no `tests/` under the repo root
    (this is exactly what a git worktree looks like, and it is the common case)
  * the run overshoots its timeout

Only a suite that actually ran and actually failed stops anything, and when it
does it names the failing tests and the command to reproduce them rather than
saying "tests failed".
"""

import json
import os
import re
import subprocess
import sys

TIMEOUT_SECONDS = 45

# `git ... commit`, allowing for `git -C path commit`, but not reaching past a
# pipe or a separator into a different command (`git log | grep commit`).
COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b")


def allow(note: str = "") -> None:
    """Let the tool call proceed. Exit 0 is the only thing that matters here."""
    if note:
        print(note, file=sys.stderr)
    raise SystemExit(0)


def block(reason: str) -> None:
    """Stop the tool call. Exit 2 is the one code that means 'deny'."""
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

    if event.get("tool_name") != "Bash":
        allow()
    if not COMMIT.search((event.get("tool_input") or {}).get("command", "")):
        allow()

    root = repo_root(event.get("cwd") or os.getcwd())
    if not root:
        allow()

    python = os.path.join(root, ".venv", "bin", "python")
    tests = os.path.join(root, "tests")
    # No .venv is the worktree case, and it is not a reason to refuse a commit.
    if not os.path.exists(python) or not os.path.isdir(tests):
        allow()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(root, "src"), tests, env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    try:
        done = subprocess.run(
            [python, "-m", "unittest", "discover",
             "-s", "tests", "-t", "tests", "-p", "test_*.py"],
            cwd=root, env=env, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        allow(f"pre-commit: suite did not finish in {TIMEOUT_SECONDS}s; commit allowed.")
    except OSError as error:
        allow(f"pre-commit: could not run the suite ({error}); commit allowed.")

    if done.returncode == 0:
        allow()

    output = (done.stderr or "") + (done.stdout or "")
    failures = [line for line in output.splitlines()
                if line.startswith(("FAIL:", "ERROR:"))]
    tally = next((line for line in reversed(output.splitlines())
                  if line.startswith(("FAILED", "OK"))), "")

    block("\n".join([
        "Commit blocked: `make test` is red on this working tree.",
        "",
        *(failures[:10] or ["(no FAIL:/ERROR: lines - see the full output below)"]),
        *([f"... and {len(failures) - 10} more"] if len(failures) > 10 else []),
        "",
        tally,
        "",
        "Reproduce with `make test`. Fix the tests, or the code that broke them,",
        "then commit again. This hook is .claude/hooks/test-before-commit.py.",
    ]))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run the suite before a `git commit`, and block the commit if it is red.

A PreToolUse hook on Bash. The suite is ~250 tests in half a second, so paying it
on every commit costs nothing and means a commit can never be the thing that
discovers a broken test.

The design constraint is that this must be *hard to make block for the wrong
reason*. A hook that refuses every commit inside a git worktree gets switched off
within a day, and then it protects nothing at all. So it exits silently - allowing
the commit - in every case where it cannot honestly run the suite:

  * the tool call is not a `git commit` (decided by `is_git_commit`, which
    tokenizes the command rather than pattern-matching it - the docstring
    there records the false positive that motivated the change)
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
import shlex
import subprocess
import sys

TIMEOUT_SECONDS = 45

# Shell separators. A command is split on these first, so that a match can never
# reach out of one command and into another (`git log | grep commit`).
SEPARATORS = re.compile(r"[|;&\n]+")

# `git`'s own options, before the subcommand. The ones here take a value as the
# next token (`git -C path commit`), so that value must not be mistaken for the
# subcommand. In `--opt=value` form the value is attached and there is no next
# token to skip.
GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--config-env",
}


def is_git_commit(command: str) -> bool:
    """Whether this shell command actually runs `git commit`.

    Matching this by regex is what the previous version did, and it was wrong in
    a way worth recording: `\\bgit\\b[^|;&]*\\bcommit\\b` matched the `git` in a
    *path* - a checkout under `~/git/` - and then, because a negated character
    class matches newlines too, ran on across a heredoc to find `conn.commit()`
    in the body of an unrelated Python script. Editing a file called
    `test_notify.py` was refused because the repository lives in a directory
    called `git`. A hook that blocks the wrong thing gets switched off, so this
    tokenizes instead of pattern-matching.

    Parsing is deliberately literal: the command is split on shell separators,
    and a segment counts only if its first word *is* git - after leading
    `VAR=value` assignments - and the first thing that is not one of git's own
    options is `commit`. Quoted text (`echo "git commit"`) and heredoc bodies do
    not qualify, because neither starts a command with git.
    """
    for segment in SEPARATORS.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # An unbalanced quote, usually because a heredoc was split across
            # segments. Fall back to whitespace so a real commit is still seen.
            tokens = segment.split()

        # `GIT_EDITOR=true git commit` is still a commit.
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens.pop(0)
        if not tokens or os.path.basename(tokens[0]) != "git":
            continue

        rest = iter(tokens[1:])
        for token in rest:
            if token in GLOBAL_OPTIONS_WITH_VALUE:
                next(rest, None)            # skip the value, not the subcommand
                continue
            if token.startswith("-"):
                continue                    # a flag, or `--opt=value`
            if token == "commit":
                return True
            break                           # some other subcommand
    return False


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
    if not is_git_commit((event.get("tool_input") or {}).get("command", "")):
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

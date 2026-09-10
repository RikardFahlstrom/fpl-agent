#!/usr/bin/env bash
#
# One entry point for every scheduled run.
#
#   fpl-cron.sh daily              capture, backfill, project, and grade what is ready
#   fpl-cron.sh deadline           re-capture and rank, if a deadline is near
#   fpl-cron.sh auto               both of the above, deciding which applies
#   fpl-cron.sh --dry-run <job>    print what would run, touch nothing
#
# What is due, in what order, and which failure gets reported are decided in
# `engine/schedule` and not here. They lived in this file for eight commits, every
# one of them a bug fix to the same three rules, in the only module of its size with
# no test file. `fpl-agent schedule` answers all three now and can be asserted; this
# script is what is left once the decisions have gone, and it is deliberately small
# enough to read in one screen.
#
# It keeps its path and its argv, so an existing crontab needs no edit. See
# docs/SCHEDULING.md for the exit-code table, and docs/schedule-equivalence.md for
# the comparison against the version of this script that made the decisions itself.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 70

LOCK="${FPL_LOCK:-/tmp/fpl-agent.lock}"

# Re-exec under the lock before doing anything else.
#
# The lock is about the credential, not the database. The token cache holds a refresh
# token that the account service ROTATES on each exchange, so two jobs refreshing
# concurrently leave one of them holding a dead token and falling back to a browser
# login. That is why it wraps the whole process and why it stays in the shell: nothing
# inside a single command can serialise against another process.
#
# -n means a run that is still going wins and this one exits rather than queueing: a
# backlog of snapshots helps nobody.
if [ -z "${FPL_CRON_LOCKED:-}" ]; then
    if command -v flock >/dev/null 2>&1; then
        export FPL_CRON_LOCKED=1
        exec flock -n "$LOCK" "$0" "$@"
    fi
    echo "warning: flock not found; running unserialised. Concurrent runs can" >&2
    echo "         invalidate the rotating refresh token." >&2
fi

AGENT="${FPL_AGENT_BIN:-.venv/bin/fpl-agent}"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    shift
fi
JOB="${1:-}"

# The job names are checked here so that a typo in a crontab is a usage error from the
# thing cron actually invoked, with the exit code this script has always used for it. They
# are `schedule.JOBS`; a new job is added there and echoed here.
case "$JOB" in
    daily|deadline|auto) ;;
    *) echo "usage: $0 [--dry-run] {daily|deadline|auto}" >&2; exit 64 ;;
esac

# The arguments are assembled in the positional parameters rather than in an array:
# `set -u` and an empty array are an unbound-variable error in bash 3.2, which is what
# a macOS box runs, and this script has to behave the same in both places.
#
# FPL_DB reaches the commands that run, not just the question of what to run. It used
# to reach only the queries this script asked, so a non-default warehouse was planned
# from one database and written to another.
set -- "$JOB"
[ -n "${FPL_DB:-}" ] && set -- "$@" --db "$FPL_DB"
[ "$DRY_RUN" = 1 ] && set -- --dry-run "$@"

exec "$AGENT" schedule "$@"

#!/usr/bin/env bash
#
# One entry point for every scheduled run.
#
# Cron fires this dumbly and often; whether there is anything to do is decided
# here and in the engine's own guards, never in the crontab. The FPL calendar is
# not expressible in cron: deadlines move, fixtures are postponed, double
# gameweeks exist, and a gameweek finishes when its last fixture finishes rather
# than on a fixed weekday. A crontab encoding any of that is wrong within weeks.
# `settle` already exits 1 when a gameweek has not finished, so attempting it
# daily costs one process and answers correctly.
#
#   fpl-cron.sh daily              snapshot, backfill, and settle if one is ready
#   fpl-cron.sh deadline           project and recommend, if a deadline is near
#   fpl-cron.sh --dry-run <job>    print what would run, touch nothing
#
# Both jobs end in `notify`, which pushes whatever the brief thinks is worth
# interrupting a person for and remembers what it has already said. Its failure
# never masks the job's: a lost notification is recoverable and a lost snapshot
# is not.
#
# Every job takes the same lock. The token cache holds a refresh token that the
# account service ROTATES on each exchange, so two jobs refreshing concurrently
# leave one of them holding a dead token and falling back to a browser login.
# The lock is about the credential, not the database.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 70

LOCK="${FPL_LOCK:-/tmp/fpl-agent.lock}"

# Re-exec under the lock before doing anything else. -n means a run that is still
# going wins and this one exits rather than queueing: a backlog of snapshots
# helps nobody.
if [ -z "${FPL_CRON_LOCKED:-}" ]; then
    if command -v flock >/dev/null 2>&1; then
        export FPL_CRON_LOCKED=1
        exec flock -n "$LOCK" "$0" "$@"
    fi
    echo "warning: flock not found; running unserialised. Concurrent runs can" >&2
    echo "         invalidate the rotating refresh token." >&2
fi

AGENT="${FPL_AGENT_BIN:-.venv/bin/fpl-agent}"
DB="${FPL_DB:-data/fpl.db}"
DRY_RUN=0

if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    shift
fi
JOB="${1:-}"

run() {
    if [ "$DRY_RUN" = 1 ]; then
        echo "would run: $AGENT $*"
        return 0
    fi
    echo "--- $AGENT $*"
    "$AGENT" "$@"
}

# Ask the warehouse a question, and fail loudly if it could not be asked.
#
# The obvious version of this swallowed stderr and returned an empty string, so
# a missing sqlite3 binary - which is the default state of a fresh Debian or
# Ubuntu box, where python has the module but the CLI is a separate package -
# produced "nothing to project" and exit 0. That is this project's oldest bug
# wearing a shell script: a confident report of nothing to do, from a run that
# never managed to look.
ask() {
    local out
    if ! out=$(sqlite3 -readonly "$DB" "$1" 2>&1); then
        echo "could not read $DB: ${out:-sqlite3 failed with no message}" >&2
        return 1
    fi
    printf '%s' "$out"
}

require_sqlite() {
    command -v sqlite3 >/dev/null 2>&1 && return 0
    echo "sqlite3 is not installed, so this script cannot ask the warehouse what" >&2
    echo "needs doing. Install it (apt install sqlite3) - python's sqlite3 module" >&2
    echo "is not the same thing and does not provide the command." >&2
    return 1
}

# The highest finished gameweek that has never been graded. Absence from
# `outcome` is the test rather than a marker file: the warehouse is the only
# state worth trusting, and a marker file can disagree with it.
# Every gameweek that can be graded and has not been, oldest first, one per line.
#
# The engine answers this, not a query written here. This script used to ask its own SQL
# and got it wrong in two ways at once: it took the highest gameweek with *any* finished
# fixture, so on the Saturday of gameweek 4 it offered a round still being played, failed
# on it, and stepped over an ungraded gameweek 3 that would then never have been graded
# at all. The rule lives in `settle.settleable_gameweeks`, `settle --list` prints it, and
# `status` asks the same function - one definition, three readers, no drift.
gameweeks_to_settle() {
    "$AGENT" settle --list --db "$DB"
}

# FPL's deadline is 90 minutes before the first kickoff of the gameweek. Derived
# from stored fixtures rather than fetched, so this is free to call hourly.
# Caveat: a postponed opening fixture moves the kickoff but not the real
# deadline. bootstrap-static's `deadline_time` is authoritative and the warehouse
# does not store it yet.
hours_to_deadline() {
    ask "SELECT CAST((julianday(MIN(kickoff_time)) - 90.0/1440 - julianday('now'))
                     * 24 AS INTEGER)
           FROM fixture
          WHERE finished = 0
            AND event = (SELECT MIN(event) FROM fixture WHERE finished = 0);"
}

job_daily() {
    local status=0 rc gw pending
    run snapshot --force || { rc=$?; status=$rc
        echo "snapshot exited $rc; see the exit-code table in docs/SCHEDULING.md" >&2; }
    run snapshot --backfill-only || { rc=$?; status=$rc
        echo "backfill exited $rc" >&2; }

    # A failed query is not "nothing to settle". Say so and give up the settle,
    # rather than reporting a clean run that never asked the question.
    if ! pending="$(gameweeks_to_settle)"; then
        echo "cannot tell whether a gameweek needs grading; not settling" >&2
        return "${status:-0}"
    fi
    if [ -z "$pending" ]; then
        echo "no finished gameweek is waiting to be graded"
        return "$status"
    fi
    # All of them, in order. A week the box was down, or a midweek round that finished
    # while an earlier one was still ungraded, must catch up rather than be skipped.
    for gw in $pending; do
        echo "gameweek $gw has finished and has never been graded; settling it"
        run settle --gameweek "$gw" --learn || { rc=$?; status=$rc
            echo "settle exited $rc for gameweek $gw" >&2; }
    done
    return "$status"
}

# Predicted lineups are the perishable input: RotoWire firms them up on matchday,
# so a projection built 24 hours out and one built 3 hours out are different
# answers. Re-snapshot each time rather than projecting over stale lineups.
job_deadline() {
    local hours status=0 rc
    # Distinguish "the warehouse says there is no next fixture" from "the
    # warehouse could not be read". The first is a quiet, correct no-op at the
    # end of a season; the second used to look exactly like it.
    if ! hours="$(hours_to_deadline)"; then
        echo "cannot tell when the next deadline is; not projecting" >&2
        return 2
    fi
    if [ -z "$hours" ]; then
        echo "no unfinished fixtures; nothing to project"
        return 0
    fi
    if [ "$hours" -lt 0 ] || [ "$hours" -gt 26 ]; then
        echo "next deadline is ${hours}h away; too far out to be worth projecting"
        return 0
    fi
    echo "next deadline is ${hours}h away; refreshing and projecting"
    run snapshot --force  || { rc=$?; status=$rc; echo "snapshot exited $rc" >&2; }
    run project --horizon 3 || { rc=$?; status=$rc; echo "project exited $rc" >&2; }
    run rivals            || { rc=$?; status=$rc; echo "rivals exited $rc" >&2; }
    run recommend         || { rc=$?; status=$rc; echo "recommend exited $rc" >&2; }
    return "$status"
}

# Push whatever the brief thinks is worth interrupting a person for. Runs after both
# jobs, on the state they just left behind, and says each thing once - the fingerprints
# it has already sent live in the warehouse, which is what makes an hourly job safe to
# notify from.
#
# Skipped, not failed, when no topic is configured: notify is opt-in, and a host that
# has never set one should not be mailed an error every hour. `notify` exits 2 on its
# own if called without one anyway.
job_notify() {
    if [ -z "${FPL_NTFY_TOPIC:-}" ] && ! grep -qs '^[[:space:]]*ntfy_topic[[:space:]]*=[[:space:]]*[^[:space:]]' fpl-agent.ini; then
        echo "no ntfy topic configured; not notifying (see docs/SCHEDULING.md)"
        return 0
    fi
    run notify
}

case "$JOB" in
    daily|deadline) require_sqlite || exit 2 ;;
esac

case "$JOB" in
    daily)    job_daily ;;
    deadline) job_deadline ;;
    *)        echo "usage: $0 [--dry-run] {daily|deadline}" >&2; exit 64 ;;
esac
JOB_STATUS=$?

# Write the brief before notifying. `notify` only ever sends the handful of lines worth
# interrupting someone for; the brief is the rest of the reasoning, and `logs/` is
# tracked precisely so that record survives. Failing to write it is not worth failing a
# run over - the snapshot is the irrecoverable asset - so its exit code is reported and
# then dropped.
#
# It rewrites a tracked file, so a server's checkout will show `logs/gwNN.md` modified.
# `git pull` there will refuse until those changes are committed or discarded.
run brief || echo "brief exited $?; the push below still reflects the same evaluation" >&2

# The job's own exit code wins. A failed push must never be what makes a `daily` run
# look like it lost the snapshot it actually captured: the snapshot is the irrecoverable
# asset and a notification is not. So notify's 8 is only ever reported when the job
# itself succeeded, and is never allowed to overwrite a 3, 4 or 5 above.
job_notify
NOTIFY_STATUS=$?

if [ "$JOB_STATUS" -ne 0 ]; then
    if [ "$NOTIFY_STATUS" -ne 0 ]; then
        echo "notify also exited $NOTIFY_STATUS, masked by the job's $JOB_STATUS" >&2
    fi
    exit "$JOB_STATUS"
fi
exit "$NOTIFY_STATUS"

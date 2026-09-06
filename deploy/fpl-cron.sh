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
#   fpl-cron.sh auto               both of the above, deciding which applies
#   fpl-cron.sh --dry-run <job>    print what would run, touch nothing
#
# `auto` is the one for a person. cron wants the two halves on their own clocks -
# `daily` overnight, `deadline` hourly - but somebody at a terminal wants "do
# whatever is due", without having to know which half today is. It captures once
# and then asks both questions, so it is safe to run on any day, including one
# where the answer to both is nothing.
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
# Asked of the engine, not computed here. This used to be its own SQL, which meant
# two statements of "when is the deadline" that could drift apart silently - the same
# trap `gameweeks_to_settle` avoids by calling `settle --list`. Exits non-zero when the
# warehouse cannot be read, which the caller must not confuse with "no deadline".
hours_to_deadline() {
    "$AGENT" status --hours-to-deadline --db "$DB"
}

# A capture is not finished until it has been projected.
#
# These were two steps, gated separately: every job snapshotted, and only a job
# inside the deadline window projected. That leaves the ordinary state of the
# warehouse - captured on a Tuesday, deadline five days out - holding a snapshot
# with no projections, which is exactly what `status` calls an inconsistency and
# exits 7 for. The nightly job produced it every night and the brief pushed
# `status_failed` about it every morning.
#
# Projecting is cheap, deterministic, and reads only what the snapshot just
# stored, so there is no reason to defer it. The deadline window still gates
# `rivals` and `recommend` below, which is where the cost and the decisions are.
_capture() {
    local backfill="$1" status=0 rc
    run snapshot --force || { rc=$?; status=$rc
        echo "snapshot exited $rc; see the exit-code table in docs/SCHEDULING.md" >&2; }
    # Actuals feed the projection's per-90 rates, so they must land before it runs.
    # The hourly job skips this: it is refreshing a market, not learning a result.
    if [ "$backfill" = with-backfill ]; then
        run snapshot --backfill-only || { rc=$?; status=$rc
            echo "backfill exited $rc" >&2; }
    fi
    run project --horizon 3 || { rc=$?; status=$rc; echo "project exited $rc" >&2; }
    return "$status"
}

# Grade every gameweek that has finished and never been graded. The rule is not
# stated here: `settle --list` answers it, so the scheduler and the engine cannot
# come to disagree about what is gradeable.
_settle_pending() {
    local status=0 rc gw pending
    # A failed query is not "nothing to settle". Say so and give up the settle,
    # rather than reporting a clean run that never asked the question.
    if ! pending="$(gameweeks_to_settle)"; then
        echo "cannot tell whether a gameweek needs grading; not settling" >&2
        return 0
    fi
    if [ -z "$pending" ]; then
        echo "no finished gameweek is waiting to be graded"
        return 0
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

# Ownership and the transfer ranking - the decision half, and the expensive one.
_rank() {
    local status=0 rc
    run rivals    || { rc=$?; status=$rc; echo "rivals exited $rc" >&2; }
    run recommend || { rc=$?; status=$rc; echo "recommend exited $rc" >&2; }
    return "$status"
}

# Whether a deadline is close enough to be worth ranking for, with the hours left
# in DEADLINE_HOURS. Returns 0 near, 1 not near, 2 the warehouse could not be read -
# the third distinguished from the second because a season that has ended and a
# database that will not open used to look identical.
DEADLINE_HOURS=""
_deadline_is_near() {
    if ! DEADLINE_HOURS="$(hours_to_deadline)"; then
        echo "cannot tell when the next deadline is; not ranking" >&2
        return 2
    fi
    if [ -z "$DEADLINE_HOURS" ]; then
        echo "no unfinished fixtures; nothing to rank"
        return 1
    fi
    if [ "$DEADLINE_HOURS" -lt 0 ] || [ "$DEADLINE_HOURS" -gt 26 ]; then
        echo "next deadline is ${DEADLINE_HOURS}h away; too far out to rank yet"
        return 1
    fi
    return 0
}

job_daily() {
    local status=0 rc
    _capture with-backfill || rc=$?
    [ "${rc:-0}" -ne 0 ] && status=$rc
    _settle_pending || { rc=$?; [ "$status" -eq 0 ] && status=$rc; }
    return "$status"
}

# Predicted lineups are the perishable input: RotoWire firms them up on matchday,
# so a projection built 24 hours out and one built 3 hours out are different
# answers. Re-capture each time rather than ranking over stale lineups.
job_deadline() {
    local status=0 rc
    _deadline_is_near; rc=$?
    [ "$rc" -eq 2 ] && return 2
    [ "$rc" -ne 0 ] && return 0
    echo "next deadline is ${DEADLINE_HOURS}h away; refreshing and ranking"
    _capture no-backfill || status=$?
    _rank || { rc=$?; [ "$status" -eq 0 ] && status=$rc; }
    return "$status"
}

# Everything the two cron jobs do, in one pass and on one capture. For a person at
# a terminal, who wants "do whatever is due" without first working out whether
# today is a settling day or a deadline day.
job_auto() {
    local status=0 rc
    _capture with-backfill || rc=$?
    # The first failure wins. A lost snapshot is the irrecoverable one and must not
    # be reported as whatever a later step did.
    [ "${rc:-0}" -ne 0 ] && status=$rc
    _settle_pending || { rc=$?; [ "$status" -eq 0 ] && status=$rc; }
    if _deadline_is_near; then
        echo "ranking against it on the capture above"
        _rank || { rc=$?; [ "$status" -eq 0 ] && status=$rc; }
    fi
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
    daily|deadline|auto) require_sqlite || exit 2 ;;
esac

case "$JOB" in
    daily)    job_daily ;;
    deadline) job_deadline ;;
    auto)     job_auto ;;
    *)        echo "usage: $0 [--dry-run] {daily|deadline|auto}" >&2; exit 64 ;;
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

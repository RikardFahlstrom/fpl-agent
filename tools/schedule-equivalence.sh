#!/usr/bin/env bash
#
# Diff what `deploy/fpl-cron.sh` does today against what it did when it made the decisions
# itself, across the six states that can be enumerated.
#
# The old script is taken out of git rather than remembered, so the comparison stays live:
# it is the answer to "does each job name still map to the same work?", which is the one
# thing a crontab on a deployed server depends on. Comparing today's entry point against
# `fpl-agent schedule` instead would prove nothing - the entry point execs it, so the two
# are the same process and agree by construction.
#
#   tools/schedule-equivalence.sh              # against the commit before the shrink
#   tools/schedule-equivalence.sh <git-ref>    # against any other version of the script
#
# Reads only. Every state is a *copy* of data/fpl.db, both sides run in dry-run mode, and
# neither writes anything. The findings, and why each divergence is deliberate, are in
# docs/schedule-equivalence.md.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WORK="$(mktemp -d)"
AGENT=.venv/bin/fpl-agent
# The last commit in which the script decided what to run for itself.
BASELINE="${1:-52a54f5}"
trap 'rm -rf "$WORK"' EXIT

OLD="$WORK/fpl-cron-$BASELINE.sh"
if ! git show "$BASELINE:deploy/fpl-cron.sh" > "$OLD" 2>/dev/null; then
    echo "no deploy/fpl-cron.sh at $BASELINE" >&2
    exit 2
fi
chmod +x "$OLD"
# It resolves the repository root from its own path, which in a temporary directory is
# the wrong answer, so it is run from a copy that skips the `cd`.
sed -i '' 's|^cd "$(dirname "$0")/\.\." .*|:|' "$OLD" 2>/dev/null || \
    sed -i 's|^cd "$(dirname "$0")/\.\." .*|:|' "$OLD"

# Each state is reached by mutating a copy, because the real warehouse only ever holds
# one of them at a time and the interesting ones are the rare ones.
prepare() {
    case "$1" in
        no-deadline)
            cp data/fpl.db "$WORK/$1.db"
            sqlite3 "$WORK/$1.db" "UPDATE fixture SET finished = 1;" ;;
        outside-window)
            cp data/fpl.db "$WORK/$1.db" ;;
        inside-window)
            cp data/fpl.db "$WORK/$1.db"
            sqlite3 "$WORK/$1.db" "UPDATE fixture SET kickoff_time = strftime('%Y-%m-%dT%H:%M:%SZ','now','+5 hours') WHERE event = (SELECT MIN(event) FROM fixture WHERE finished = 0);" ;;
        pending-grading)
            cp data/fpl.db "$WORK/$1.db"
            sqlite3 "$WORK/$1.db" "DELETE FROM outcome WHERE gameweek = (SELECT MAX(gameweek) FROM outcome);" ;;
        no-warehouse)
            : ;;                         # the file is simply never created
        unreadable)
            echo "not a database" > "$WORK/$1.db" ;;
    esac
}

# The invocation column of the plan: everything before the run of spaces that separates a
# step from the reason it is due.
steps_of_plan() {
    awk '/^due:/ {inside = 1; next}
         /^$/    {inside = 0}
         inside  {sub(/^ *[0-9]+  /, ""); sub(/  +.*$/, ""); print}'
}

for state in no-deadline outside-window inside-window pending-grading no-warehouse unreadable; do
    prepare "$state"
    db="$WORK/$state.db"
    for job in daily deadline auto; do
        old_out=$(FPL_DB="$db" FPL_AGENT_BIN="$AGENT" FPL_LOCK="$WORK/lock" \
                  "$OLD" --dry-run "$job" 2>&1)
        old_code=$?
        # The old script announced each step as `would run: <agent> <command>`.
        old_steps=$(printf '%s\n' "$old_out" | sed -n "s|^would run: $AGENT ||p" | tr '\n' '|')
        now_out=$(FPL_DB="$db" FPL_AGENT_BIN="$AGENT" FPL_LOCK="$WORK/lock" \
                  ./deploy/fpl-cron.sh --dry-run "$job" 2>&1)
        now_code=$?
        now_steps=$(printf '%s\n' "$now_out" | steps_of_plan | tr '\n' '|')
        verdict=$([ "$old_steps" = "$now_steps" ] && [ "$old_code" = "$now_code" ] \
                  && echo same || echo DIVERGES)
        printf '%-16s %-9s %s\n  %s(%s): %s\n  now      (%s): %s\n' \
            "$state" "$job" "$verdict" \
            "$BASELINE" "$old_code" "${old_steps:-<nothing>}" \
            "$now_code" "${now_steps:-<nothing>}"
    done
done

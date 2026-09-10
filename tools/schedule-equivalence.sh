#!/usr/bin/env bash
#
# Diff what `engine/schedule` plans against what `deploy/fpl-cron.sh` decides, across the
# six states that can be enumerated. The script has no tests, so "the new one behaves like
# the old one" is otherwise unfalsifiable - and three of the changes in this rewrite alter
# the behaviour of an unattended job that mails its owner on failure.
#
#   tools/schedule-equivalence.sh            # what each side would run, per state
#
# Reads only. Every state is a *copy* of data/fpl.db, both sides run in dry-run mode, and
# neither writes anything. Findings live in docs/schedule-equivalence.md; re-run this
# before cutting the shell down and after, and the two runs should agree.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WORK="$(mktemp -d)"
AGENT=.venv/bin/fpl-agent
trap 'rm -rf "$WORK"' EXIT

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
        shell_out=$(FPL_DB="$db" FPL_AGENT_BIN="$AGENT" FPL_LOCK="$WORK/lock" \
                    ./deploy/fpl-cron.sh --dry-run "$job" 2>&1)
        shell_code=$?
        shell_steps=$(printf '%s\n' "$shell_out" | sed -n "s|^would run: $AGENT ||p" | tr '\n' '|')
        plan_out=$("$AGENT" schedule --dry-run "$job" --db "$db" 2>&1)
        plan_code=$?
        plan_steps=$(printf '%s\n' "$plan_out" | steps_of_plan | tr '\n' '|')
        verdict=$([ "$shell_steps" = "$plan_steps" ] && [ "$shell_code" = "$plan_code" ] \
                  && echo same || echo DIVERGES)
        printf '%-16s %-9s %s\n  shell(%s): %s\n  plan (%s): %s\n' \
            "$state" "$job" "$verdict" \
            "$shell_code" "${shell_steps:-<nothing>}" \
            "$plan_code" "${plan_steps:-<nothing>}"
    done
done

#!/usr/bin/env bash
#
# The one comparison a dry run cannot make: when several steps fail, which exit code does
# the run report? Both sides run the same stub in place of the console script, so a
# failure pattern is reproducible without breaking anything for real.
#
#   tools/schedule-precedence.sh
#
# Reads only: the stub never touches the warehouse, and the copy it is pointed at exists
# so that the *plan* is made from real data. Findings in docs/schedule-equivalence.md.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

WORK="$(mktemp -d)"
STUB="tools/schedule-stub-agent.sh"
trap 'rm -rf "$WORK"' EXIT
cp data/fpl.db "$WORK/fpl.db"

compare() {
    local label="$1" job="$2" codes="$3" shell_code plan_code
    STUB_CODES="$codes" FPL_DB="$WORK/fpl.db" FPL_AGENT_BIN="$STUB" FPL_LOCK="$WORK/lock" \
        ./deploy/fpl-cron.sh "$job" >/dev/null 2>&1
    shell_code=$?
    STUB_CODES="$codes" FPL_AGENT_BIN="$STUB" \
        .venv/bin/fpl-agent schedule "$job" --db "$WORK/fpl.db" >/dev/null 2>&1
    plan_code=$?
    printf '%-34s %-26s shell=%-3s plan=%-3s %s\n' "$label" "$codes" \
        "$shell_code" "$plan_code" \
        "$([ "$shell_code" = "$plan_code" ] && echo same || echo DIVERGES)"
}

compare "irrecoverable then recoverable" daily "snapshot=3 project=1"
compare "recoverable then irrecoverable" daily "project=1 settle=6"
compare "one failure only"               daily "snapshot=4"
compare "tolerated failure alone"        daily "brief=2"
compare "tolerated masked by a real one" daily "snapshot=3 brief=2"
compare "everything succeeds"            daily ""

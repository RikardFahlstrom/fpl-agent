#!/usr/bin/env bash
#
# Stands in for the installed console script, for `tools/schedule-precedence.sh`. Exits
# with whatever code was scripted for the command it was asked to run, so a pattern of
# failures can be compared between the shell and the schedule module without breaking
# anything for real.
#
#   STUB_CODES="snapshot=3 project=1" tools/schedule-stub-agent.sh snapshot --force
#
# `settle --list` and `status --hours-to-deadline` are what the shell *asks* rather than
# runs, so those two are passed through to the real command and answer from the warehouse.
set -u

cmd="$1"; shift

case "$cmd" in
    settle) [ "${1:-}" = "--list" ] && exec .venv/bin/fpl-agent settle "$@" ;;
    status) [ "${1:-}" = "--hours-to-deadline" ] && exec .venv/bin/fpl-agent status "$@" ;;
esac

for pair in ${STUB_CODES:-}; do
    if [ "${pair%%=*}" = "$cmd" ]; then
        echo "stub: $cmd exiting ${pair#*=}" >&2
        exit "${pair#*=}"
    fi
done
echo "stub: $cmd ok" >&2
exit 0

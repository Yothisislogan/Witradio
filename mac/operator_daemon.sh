#!/bin/bash
# WRIT-FM Operator Daemon — runs the Claude maintenance loop on an interval.
# This replaces the separate talk and bumper stocking daemons.

set -euo pipefail

RADIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RADIO_DIR"
eval "$(uv run python mac/station_config.py --env)"

# Stations on the OpenAI-compatible client (wit-next) have no operator loop:
# a deterministic scheduler and per-show producers fill their shows.
if [ "${WRIT_AGENT_KIND:-}" = "openai" ]; then
    echo "[operator] ${WRIT_STATION_ID:-station} has no operator loop; nothing to do." >&2
    exit 0
fi
INTERVAL_SECONDS="${WRIT_OPERATOR_INTERVAL_SECONDS:-900}"

ts() { date +%H:%M; }

echo "[operator-daemon $(ts)] Starting ${WRIT_CALL_SIGN:-WRIT-FM}. Interval: ${INTERVAL_SECONDS}s"

while true; do
    echo "[operator-daemon $(ts)] Running ${WRIT_CALL_SIGN:-WRIT-FM} operator loop..."
    (
        ./run_operator.sh
    ) || echo "[operator-daemon $(ts)] operator pass exited with status $?; continuing"
    echo "[operator-daemon $(ts)] Sleeping ${INTERVAL_SECONDS}s..."
    sleep "$INTERVAL_SECONDS"
done

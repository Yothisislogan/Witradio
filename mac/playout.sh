#!/usr/bin/env bash
# Start the WIT-FM Liquidsoap playout master for a station (default wit-next).
#
#   bash mac/playout.sh [station]
#
# Exports the station's settings, checks the source password and Liquidsoap,
# makes the fallback folders, then runs liquidsoap/wit_next.liq in the
# foreground (writ runs it in tmux). The director and alerts run with the
# project's Python: .venv/bin/python when it exists, so no `uv run` sync can
# stall the stream.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"
STATION="${1:-${WRIT_STATION_ID:-wit-next}}"

# The project's virtualenv if it is set up, else the system python3. The
# director needs PyYAML.
if [[ -z "${WRIT_PYTHON:-}" ]]; then
    for candidate in "$PROJECT_ROOT/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -x "$candidate" ]] && "$candidate" -c "import yaml" 2>/dev/null; then
            WRIT_PYTHON="$candidate"
            break
        fi
    done
    if [[ -z "${WRIT_PYTHON:-}" ]]; then
        echo "no Python with PyYAML found: run uv sync, or set WRIT_PYTHON" >&2
        exit 2
    fi
fi
export WRIT_PYTHON WRIT_PROJECT_ROOT="$PROJECT_ROOT"
if ! station_env="$("$WRIT_PYTHON" mac/station_config.py --station "$STATION" --env)"; then
    echo "could not read the settings for station $STATION (with $WRIT_PYTHON)" >&2
    exit 2
fi
eval "$station_env"

password_var="${WRIT_SOURCE_PASSWORD_ENV:-WRIT_SOURCE_PASSWORD}"
if [[ -z "${!password_var:-}" ]]; then
    echo "$password_var is not set: export the Icecast source password first" >&2
    exit 2
fi
if ! command -v liquidsoap >/dev/null; then
    echo "liquidsoap is not installed (Ubuntu 24.04: apt install liquidsoap; needs 2.2 or newer)" >&2
    exit 2
fi

mkdir -p "$WRIT_SAFE_DIR" "$WRIT_IDS_DIR" "$WRIT_RUNTIME_DIR/playout"
echo "Playout for $WRIT_CALL_SIGN ($STATION): MP3 on $WRIT_ICECAST_MOUNT, FLAC on ${WRIT_LOSSLESS_MOUNT:-/lossless}"
liquidsoap --version | head -1

# The director runs beside Liquidsoap and is restarted if it ever exits;
# while it is down, Liquidsoap plays the safe library.
(
    while true; do
        "$WRIT_PYTHON" mac/playout_director.py --station "$STATION" serve --port "$WRIT_DIRECTOR_PORT" \
            || echo "director exited ($?); restarting" >&2
        sleep 2
    done
) &
DIRECTOR_LOOP=$!
trap 'kill "$DIRECTOR_LOOP" 2>/dev/null; pkill -f "playout_director.py --station $STATION serve" 2>/dev/null' EXIT
for _ in $(seq 1 50); do
    curl -sf "http://127.0.0.1:$WRIT_DIRECTOR_PORT/health" >/dev/null && break
    sleep 0.2
done

liquidsoap "$PROJECT_ROOT/liquidsoap/wit_next.liq"

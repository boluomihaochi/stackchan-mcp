#!/bin/bash
# Start/stop the host-side Stack-chan pushed-voice upload receiver.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

load_frontend_token() {
    local env_path="${STACKCHAN_FRONTEND_ENV:-/Users/Isa/Projects/migratorybird-astro/relay/.env}"
    if [ -n "${STACKCHAN_FRONTEND_TOKEN:-}" ] || [ ! -f "$env_path" ]; then
        return 0
    fi
    local token
    token="$(python3 - "$env_path" <<'PY'
from pathlib import Path
import sys

for raw_line in Path(sys.argv[1]).read_text().splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != "AGENT_HOST_TOKEN":
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
)"
    if [ -n "$token" ]; then
        export STACKCHAN_FRONTEND_TOKEN="$token"
    fi
}

load_frontend_token

HOST="${STACKCHAN_VOICE_UPLOAD_HOST:-127.0.0.1}"
PORT="${STACKCHAN_VOICE_UPLOAD_PORT:-8767}"
LOG_FILE="${STACKCHAN_VOICE_UPLOAD_LOG:-/tmp/stackchan_voice_upload.log}"
PID_FILE="${STACKCHAN_VOICE_UPLOAD_PIDFILE:-/tmp/stackchan_voice_upload.pid}"
LANGUAGE="${STACKCHAN_VOICE_LANG:-zh}"

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

port_owner() {
    lsof -ti:"$PORT" 2>/dev/null | head -n 1
}

health_url() {
    echo "http://$HOST:$PORT/health"
}

wait_for_health() {
    for _ in {1..20}; do
        if curl -fsS --max-time 1 "$(health_url)" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.25
    done
    return 1
}

case "${1:-start}" in
    start)
        if is_running; then
            echo "Stack-chan voice upload receiver already running: PID $(cat "$PID_FILE")"
            exit 0
        fi
        owner="$(port_owner || true)"
        if [ -n "$owner" ]; then
            echo "Port $PORT is already in use by PID $owner. Stop that process or choose STACKCHAN_VOICE_UPLOAD_PORT."
            exit 1
        fi
        cd "$SCRIPT_DIR" || exit 1
        nohup uv run python scripts/stackchan_voice_upload_server.py \
            --host "$HOST" \
            --port "$PORT" \
            --lang "$LANGUAGE" \
            >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "Started Stack-chan voice upload receiver: PID $(cat "$PID_FILE")"
        echo "URL: http://$HOST:$PORT/voice/upload"
        echo "Health: $(health_url)"
        echo "Log: $LOG_FILE"
        if wait_for_health; then
            echo "Status: ready"
        else
            echo "Status: starting or failed; check $LOG_FILE"
        fi
        if [ -n "${STACKCHAN_FRONTEND_SESSION_ID:-}" ]; then
            echo "Frontend forwarding: enabled for session ${STACKCHAN_FRONTEND_SESSION_ID}"
        else
            echo "Frontend forwarding: disabled (set STACKCHAN_FRONTEND_SESSION_ID to enable)"
        fi
        if [ -n "${STACKCHAN_VOICE_WAKE_WORDS:-}" ]; then
            echo "Wake words: ${STACKCHAN_VOICE_WAKE_WORDS}"
        else
            echo "Wake words: disabled"
        fi
        ;;
    stop)
        if is_running; then
            kill "$(cat "$PID_FILE")"
            rm -f "$PID_FILE"
            echo "Stopped Stack-chan voice upload receiver."
        else
            rm -f "$PID_FILE"
            echo "Stack-chan voice upload receiver is not running."
        fi
        ;;
    status)
        if is_running; then
            echo "Stack-chan voice upload receiver running: PID $(cat "$PID_FILE")"
            curl -fsS --max-time 3 "$(health_url)" || true
            echo ""
        else
            rm -f "$PID_FILE"
            echo "Stack-chan voice upload receiver is not running."
        fi
        ;;
    *)
        echo "Usage: $0 [start|stop|status]"
        exit 2
        ;;
esac

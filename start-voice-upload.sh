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
    local env_path="${STACKCHAN_FRONTEND_ENV:-}"
    if [ -z "$env_path" ]; then
        return 0
    fi
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

resolve_frontend_session() {
    local session_id="${STACKCHAN_FRONTEND_SESSION_ID:-}"
    local title="${STACKCHAN_FRONTEND_SESSION_TITLE:-}"
    local auto="${STACKCHAN_FRONTEND_AUTO_SESSION:-}"
    if [ -z "$title" ] && [ "$session_id" != "latest" ] && [ "$session_id" != "auto" ] && [ "$auto" != "1" ]; then
        return 0
    fi

    local resolved
    if [ -n "$title" ]; then
        if ! resolved="$(python3 "$SCRIPT_DIR/scripts/stackchan_frontend_session.py" --title "$title")"; then
            echo "Could not resolve frontend session. Set STACKCHAN_FRONTEND_SESSION_ID explicitly." >&2
            exit 1
        fi
    else
        if ! resolved="$(python3 "$SCRIPT_DIR/scripts/stackchan_frontend_session.py")"; then
            echo "Could not resolve frontend session. Set STACKCHAN_FRONTEND_SESSION_ID explicitly." >&2
            exit 1
        fi
    fi
    if [ -z "$resolved" ]; then
        echo "Could not resolve frontend session. Set STACKCHAN_FRONTEND_SESSION_ID explicitly." >&2
        exit 1
    fi
    export STACKCHAN_FRONTEND_SESSION_ID="$resolved"
}

load_frontend_token
resolve_frontend_session

HOST="${STACKCHAN_VOICE_UPLOAD_HOST:-127.0.0.1}"
PORT="${STACKCHAN_VOICE_UPLOAD_PORT:-8767}"
LOG_FILE="${STACKCHAN_VOICE_UPLOAD_LOG:-/tmp/stackchan_voice_upload.log}"
PID_FILE="${STACKCHAN_VOICE_UPLOAD_PIDFILE:-/tmp/stackchan_voice_upload.pid}"
LANGUAGE="${STACKCHAN_VOICE_LANG:-zh}"
PUBLIC_URL="${STACKCHAN_VOICE_PUBLIC_URL:-}"
CLOUDFLARED_LABEL="${STACKCHAN_CLOUDFLARED_LAUNCHD_LABEL:-xyz.stackchan.cloudflared}"
VOICE_UPLOAD_LABEL="${STACKCHAN_VOICE_UPLOAD_LAUNCHD_LABEL:-xyz.stackchan.voice-upload}"
FRONTEND_HEALTH_URL="${STACKCHAN_FRONTEND_HEALTH_URL:-http://127.0.0.1:3200/health}"
PYTHON_BIN="${STACKCHAN_VOICE_PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
STACKCHAN_VOICE_HEALTH_ATTEMPTS="${STACKCHAN_VOICE_HEALTH_ATTEMPTS:-20}"
STACKCHAN_VOICE_HEALTH_TIMEOUT_SEC="${STACKCHAN_VOICE_HEALTH_TIMEOUT_SEC:-1}"
STACKCHAN_VOICE_HEALTH_INTERVAL_SEC="${STACKCHAN_VOICE_HEALTH_INTERVAL_SEC:-0.25}"
STACKCHAN_VOICE_STATUS_TIMEOUT_SEC="${STACKCHAN_VOICE_STATUS_TIMEOUT_SEC:-5}"

python_cmd() {
    if [ -x "$PYTHON_BIN" ]; then
        printf '%s\n' "$PYTHON_BIN"
    else
        printf '%s\n' "uv run python"
    fi
}

start_upload_server() {
    local -a cmd
    if [ -x "$PYTHON_BIN" ]; then
        cmd=("$PYTHON_BIN")
    else
        cmd=(uv run python)
    fi
    nohup "${cmd[@]}" scripts/stackchan_voice_upload_server.py \
        --host "$HOST" \
        --port "$PORT" \
        --lang "$LANGUAGE" \
        >> "$LOG_FILE" 2>&1 &
}

is_running() {
    [ -n "$(running_pid)" ]
}

launchd_loaded() {
    launchctl print "gui/$(id -u)/$VOICE_UPLOAD_LABEL" >/dev/null 2>&1
}

launchd_pid() {
    launchctl print "gui/$(id -u)/$VOICE_UPLOAD_LABEL" 2>/dev/null | awk -F'= ' '/pid =/ {print $2; exit}'
}

running_pid() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        cat "$PID_FILE"
        return 0
    fi
    if launchd_loaded; then
        launchd_pid
        return 0
    fi
    port_owner || true
}

port_owner() {
    lsof -ti:"$PORT" 2>/dev/null | head -n 1
}

health_url() {
    local check_host="$HOST"
    if [ "$check_host" = "0.0.0.0" ] || [ "$check_host" = "::" ]; then
        check_host="127.0.0.1"
    fi
    echo "http://$check_host:$PORT/health"
}

public_health_url() {
    if [ -n "$PUBLIC_URL" ]; then
        echo "${PUBLIC_URL%/}/health"
    fi
}

recorder_url() {
    echo "${PUBLIC_URL%/}/"
}

print_upload_token_hint() {
    if [ -n "${STACKCHAN_VOICE_UPLOAD_TOKEN:-}" ]; then
        echo "Upload token: configured; enter it in the recorder page. It is not printed in URLs."
    fi
}

check_url() {
    local label="$1"
    local url="$2"
    if curl -fsS --max-time "$STACKCHAN_VOICE_STATUS_TIMEOUT_SEC" "$url" >/dev/null 2>&1; then
        echo "[ok] $label: $url"
    else
        echo "[fail] $label: $url"
    fi
}

check_cloudflared() {
    if launchctl print "gui/$(id -u)/$CLOUDFLARED_LABEL" >/dev/null 2>&1; then
        local state
        state="$(launchctl print "gui/$(id -u)/$CLOUDFLARED_LABEL" 2>/dev/null | awk -F'= ' '/state =/ {print $2; exit}')"
        echo "[ok] cloudflared launchd: $CLOUDFLARED_LABEL (${state:-unknown})"
    elif pgrep -f "cloudflared tunnel run" >/dev/null 2>&1; then
        echo "[ok] cloudflared process: running"
    else
        echo "[fail] cloudflared: not running"
    fi
}

wait_for_health() {
    local attempt
    for ((attempt = 1; attempt <= STACKCHAN_VOICE_HEALTH_ATTEMPTS; attempt++)); do
        if curl -fsS --max-time "$STACKCHAN_VOICE_HEALTH_TIMEOUT_SEC" "$(health_url)" >/dev/null 2>&1; then
            return 0
        fi
        sleep "$STACKCHAN_VOICE_HEALTH_INTERVAL_SEC"
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
        start_upload_server
        echo $! > "$PID_FILE"
        echo "Started Stack-chan voice upload receiver: PID $(cat "$PID_FILE")"
        echo "Runtime: $(python_cmd)"
        echo "URL: http://$HOST:$PORT/voice/upload"
        echo "Health: $(health_url)"
        if [ -n "$PUBLIC_URL" ]; then
            echo "Public recorder: $(recorder_url)"
            print_upload_token_hint
        fi
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
        if launchd_loaded; then
            launchctl bootout "gui/$(id -u)/$VOICE_UPLOAD_LABEL" >/dev/null 2>&1 || true
            rm -f "$PID_FILE"
            echo "Stopped Stack-chan voice upload receiver launchd service: $VOICE_UPLOAD_LABEL"
        elif is_running; then
            kill "$(running_pid)"
            rm -f "$PID_FILE"
            echo "Stopped Stack-chan voice upload receiver."
        else
            rm -f "$PID_FILE"
            echo "Stack-chan voice upload receiver is not running."
        fi
        ;;
    status)
        if is_running; then
            echo "Stack-chan voice upload receiver running: PID $(running_pid)"
            if launchd_loaded; then
                echo "Launchd: $VOICE_UPLOAD_LABEL"
            fi
            check_url "local upload health" "$(health_url)"
            if [ -n "$PUBLIC_URL" ]; then
                check_url "public upload health" "$(public_health_url)"
                echo "Recorder: $(recorder_url)"
                print_upload_token_hint
            fi
            check_url "frontend agent-host" "$FRONTEND_HEALTH_URL"
            check_cloudflared
            if [ -n "${STACKCHAN_FRONTEND_SESSION_ID:-}" ]; then
                echo "Frontend session: ${STACKCHAN_FRONTEND_SESSION_ID}"
            else
                echo "Frontend session: disabled"
            fi
            if [ -n "${STACKCHAN_VOICE_WAKE_WORDS:-}" ]; then
                echo "Wake words: ${STACKCHAN_VOICE_WAKE_WORDS}"
            else
                echo "Wake words: disabled"
            fi
        else
            rm -f "$PID_FILE"
            echo "Stack-chan voice upload receiver is not running."
            if [ -n "$PUBLIC_URL" ]; then
                check_url "public upload health" "$(public_health_url)"
            fi
            check_url "frontend agent-host" "$FRONTEND_HEALTH_URL"
            check_cloudflared
        fi
        ;;
    *)
        echo "Usage: $0 [start|stop|status]"
        exit 2
        ;;
esac

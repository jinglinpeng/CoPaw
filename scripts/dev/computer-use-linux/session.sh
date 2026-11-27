#!/usr/bin/env bash
# Start or stop the whole Linux Computer Use test setup:
# virtual desktop, helper, recording proxy and a QwenPaw test instance.
#
#   CU_STATE=<dir> CU_RUNS=<dir> CU_QWENPAW_HOME=<dir> session.sh start|stop
#
# The QwenPaw instance must already be initialized in CU_QWENPAW_HOME with the
# computer-use plugin installed and the DashScope provider configured (its key
# lives in <CU_QWENPAW_HOME>.secret). On start the provider's base URL is
# pointed at the recording proxy, whose log is $CU_RUNS/llm_calls.jsonl.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
STATE="${CU_STATE:-$HOME/.cache/qwenpaw-cu-linux}"
RUNS="${CU_RUNS:-$STATE/runs}"
HOME_DIR="${CU_QWENPAW_HOME:?set CU_QWENPAW_HOME to the test instance directory}"
APP_PORT="${CU_APP_PORT:-18790}"
PYTHON="$ROOT/.venv/bin/python"
export CU_STATE="$STATE"
mkdir -p "$RUNS"

free_port() {
  "$PYTHON" -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1])"
}

stop_pid() {
  if [[ -f "$1" ]]; then
    kill "$(cat "$1")" 2>/dev/null || true
    rm -f "$1"
  fi
}

start() {
  bash "$HERE/desktop.sh" start
  bash "$HERE/helper.sh" start
  local port
  port="$(free_port)"
  echo "$port" >"$RUNS/proxy.port"
  setsid "$PYTHON" "$HERE/record_proxy.py" --port "$port" \
    --upstream https://dashscope.aliyuncs.com --log "$RUNS/llm_calls.jsonl" \
    >>"$RUNS/proxy.log" 2>&1 &
  echo $! >"$RUNS/proxy.pid"
  (
    # shellcheck disable=SC1091
    source "$STATE/env.sh"
    # shellcheck disable=SC1091
    source "$STATE/helper.env"
    export QWENPAW_WORKING_DIR="$HOME_DIR" QWENPAW_TELEMETRY_DISABLED=1
    export QWENPAW_KEYRING_ACCOUNT=cu-linux-test
    setsid "$ROOT/.venv/bin/qwenpaw" app --host 127.0.0.1 --port "$APP_PORT" \
      >"$RUNS/app.log" 2>&1 &
    echo $! >"$RUNS/app.pid"
  )
  for _ in $(seq 90); do
    curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$APP_PORT/api/models" 2>/dev/null | grep -q 200 && break
    sleep 2
  done
  curl -s -X PUT "http://127.0.0.1:$APP_PORT/api/models/dashscope/config" \
    -H "Content-Type: application/json" \
    -d "{\"base_url\": \"http://127.0.0.1:$port/compatible-mode/v1\"}" >/dev/null
  curl -s "http://127.0.0.1:$APP_PORT/api/computer-use/status"
  echo
  echo "QwenPaw on 127.0.0.1:$APP_PORT, model calls recorded through port $port"
}

stop() {
  stop_pid "$RUNS/app.pid"
  stop_pid "$RUNS/proxy.pid"
  bash "$HERE/helper.sh" stop
  bash "$HERE/desktop.sh" stop
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

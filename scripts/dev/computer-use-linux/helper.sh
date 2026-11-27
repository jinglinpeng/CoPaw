#!/usr/bin/env bash
# Start or stop the Computer Use helper inside the virtual desktop.
#
#   helper.sh start   # writes $STATE/helper.env with the two variables QwenPaw reads
#   helper.sh stop
#
# The capability secret is generated here, passed to the helper through its
# environment, and stored only in $STATE (mode 700).
set -euo pipefail

STATE="${CU_STATE:-$HOME/.cache/qwenpaw-cu-linux}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HELPER="${CU_HELPER:-$ROOT/console/src-tauri/computer-use-helper/target/release/qwenpaw-computer-use-helper}"
# Unix socket paths are limited to 107 bytes, so the socket lives in a short
# private directory rather than next to the other state files.
SOCKET_DIR="${XDG_RUNTIME_DIR:-/tmp}/qwenpaw-cu-$(id -u)"
SOCKET="$SOCKET_DIR/helper.sock"

stop() {
  if [[ -f "$STATE/helper.pid" ]]; then
    kill "$(cat "$STATE/helper.pid")" 2>/dev/null || true
    rm -f "$STATE/helper.pid"
  fi
  rm -f "$SOCKET" "$STATE/helper.env"
}

start() {
  stop
  mkdir -p "$SOCKET_DIR"
  chmod 700 "$SOCKET_DIR"
  # shellcheck disable=SC1091
  source "$STATE/env.sh"
  # Keep one secret per state directory, so a restarted helper still matches
  # the capability a running QwenPaw read at startup.
  if [[ ! -s "$STATE/helper.secret" ]]; then
    (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$STATE/helper.secret")
  fi
  local secret
  secret="$(cat "$STATE/helper.secret")"
  QWENPAW_CU_CAPABILITY="$secret" \
  QWENPAW_CU_DETACHED=1 \
  QWENPAW_CU_SIDECAR_LOG="$STATE/sidecar.jsonl" \
    setsid "$HELPER" serve --pipe "$SOCKET" >"$STATE/helper.out" 2>"$STATE/helper.err" &
  echo $! >"$STATE/helper.pid"
  for _ in $(seq 50); do
    grep -q "QWENPAW_COMPUTER_USE_READY" "$STATE/helper.out" 2>/dev/null && break
    sleep 0.1
  done
  if ! grep -q "QWENPAW_COMPUTER_USE_READY" "$STATE/helper.out"; then
    echo "helper did not become ready; see $STATE/helper.err" >&2
    exit 1
  fi
  umask 077
  cat >"$STATE/helper.env" <<EOF
export QWENPAW_COMPUTER_USE_PIPE='$SOCKET'
export QWENPAW_COMPUTER_USE_CAPABILITY='$secret'
EOF
  echo "helper ready at $SOCKET"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

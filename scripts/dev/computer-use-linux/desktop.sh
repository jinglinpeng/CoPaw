#!/usr/bin/env bash
# Start or stop a private virtual X desktop for Linux Computer Use testing.
#
#   desktop.sh start   # Xvnc + session D-Bus + openbox + AT-SPI, env in $STATE/env.sh
#   desktop.sh stop
#
# The display only accepts local connections. To watch it, tunnel port 5997
# over SSH and attach any VNC client (no password).
set -euo pipefail

STATE="${CU_STATE:-$HOME/.cache/qwenpaw-cu-linux}"
DISPLAY_NUM="${CU_DISPLAY:-97}"
GEOMETRY="${CU_GEOMETRY:-1440x900}"
mkdir -p "$STATE"
chmod 700 "$STATE"

stop() {
  for name in openbox atspi dbus xvnc; do
    if [[ -f "$STATE/$name.pid" ]]; then
      kill "$(cat "$STATE/$name.pid")" 2>/dev/null || true
      rm -f "$STATE/$name.pid"
    fi
  done
  rm -f "$STATE/env.sh"
}

start() {
  stop
  Xvnc ":$DISPLAY_NUM" -geometry "$GEOMETRY" -depth 24 -localhost \
    -SecurityTypes None -rfbport "59$DISPLAY_NUM" -AlwaysShared \
    >"$STATE/xvnc.log" 2>&1 &
  echo $! >"$STATE/xvnc.pid"
  export DISPLAY=":$DISPLAY_NUM"
  for _ in $(seq 50); do
    [[ -S "/tmp/.X11-unix/X$DISPLAY_NUM" ]] && break
    sleep 0.1
  done
  eval "$(dbus-launch --sh-syntax)"
  echo "$DBUS_SESSION_BUS_PID" >"$STATE/dbus.pid"
  /usr/libexec/at-spi-bus-launcher --launch-immediately --a11y=1 \
    >"$STATE/atspi.log" 2>&1 &
  echo $! >"$STATE/atspi.pid"
  openbox >"$STATE/openbox.log" 2>&1 &
  echo $! >"$STATE/openbox.pid"
  cat >"$STATE/env.sh" <<EOF
export DISPLAY=$DISPLAY
export DBUS_SESSION_BUS_ADDRESS='$DBUS_SESSION_BUS_ADDRESS'
# LibreOffice picks its X11 backend without a desktop environment; the GTK3
# backend is the one that publishes an accessibility tree.
export SAL_USE_VCLPLUGIN=gtk3
export GTK_A11Y=atspi
unset NO_AT_BRIDGE
EOF
  sleep 1
  echo "desktop ready on $DISPLAY; source $STATE/env.sh to join it"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

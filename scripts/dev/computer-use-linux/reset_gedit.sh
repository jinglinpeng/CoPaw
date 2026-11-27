#!/usr/bin/env bash
# Close every gedit window and open one empty document, so each run starts
# from the same desktop state. Removes the run's target file.
set -euo pipefail
STATE="${CU_STATE:-$HOME/.cache/qwenpaw-cu-linux}"
TARGET="${1:?target file}"
# shellcheck disable=SC1091
source "$STATE/env.sh"
pkill -u "$(id -u)" -x gedit 2>/dev/null || true
sleep 1
rm -f "$TARGET"
mkdir -p "$(dirname "$TARGET")"
setsid gedit --new-window >"$STATE/gedit.log" 2>&1 < /dev/null &
for _ in $(seq 40); do
  sleep 0.25
  DISPLAY="$DISPLAY" python3 - <<'PY' && break
import gi, sys
gi.require_version("Wnck", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Wnck, Gdk
Gdk.init([]); s = Wnck.Screen.get_default(); s.force_update()
sys.exit(0 if any("gedit" in (w.get_name() or "") for w in s.get_windows()) else 1)
PY
done
echo "gedit ready"

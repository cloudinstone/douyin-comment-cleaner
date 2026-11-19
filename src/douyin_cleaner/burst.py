"""On-device closed-loop deletion script and its result parsing.

The script runs inside a single `adb shell` session and inspects the window
stack (`dumpsys window windows`, ~0.1-0.2s) around every touch, so a delete tap
is only ever sent while the comment action dialog is verifiably on top. Any
unexpected state aborts the burst immediately instead of tapping blindly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REMOTE_SCRIPT_PATH = "/data/local/tmp/douyin_cleaner.sh"

# Window states reported by the script:
#   list   - target activity window on top and (near) full-screen
#   popup  - target activity's dialog on top with the calibrated size
#   dialog - target activity's dialog on top with an unexpected size
#   other  - any other window on top (video page, other app, ...)
DEVICE_SCRIPT = r"""#!/system/bin/sh
# Usage:
#   probe  ACT FULLH
#   burst  ACT FULLH N PX PY LONG_MS REL_X REL_Y POPUP_W POPUP_H SETTLE_S
MODE=$1; ACT=$2; FULLH=$3

state() {
  W=$(dumpsys window windows)
  B=$(echo "$W" | grep -m1 -A30 -E "Window #[0-9]+ Window\{[^ ]+ u0 [^ /}]+/")
  T=$(echo "$B" | head -1)
  set -- $(echo "$B" | grep -m1 -oE " frame=\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]" | grep -oE "[0-9]+")
  FL=${1:-0}; FT=${2:-0}; FR=${3:-0}; FB=${4:-0}
  FW=$((FR - FL)); FH=$((FB - FT))
  case "$T" in
    *".$ACT}"*|*"/$ACT}"*) ;;
    *) S=other; return ;;
  esac
  if [ "$FH" -ge "$FULLH" ]; then
    S=list
  elif [ "$FW" -eq "${POPUP_W:--1}" ] && [ "$FH" -eq "${POPUP_H:--1}" ]; then
    S=popup
  else
    S=dialog
  fi
}

if [ "$MODE" = "probe" ]; then
  state
  echo "STATE $S $FL $FT $FR $FB"
  exit 0
fi

N=$4; PX=$5; PY=$6; LONG_MS=$7; REL_X=$8; REL_Y=$9
POPUP_W=${10}; POPUP_H=${11}; SETTLE=${12}

state
if [ "$S" != "list" ]; then echo "FAIL pre $S"; exit 0; fi

i=0
while [ "$i" -lt "$N" ]; do
  input swipe "$PX" "$PY" "$PX" "$PY" "$LONG_MS"
  j=0
  while :; do
    state
    [ "$S" = "popup" ] && break
    j=$((j + 1))
    [ "$j" -ge 4 ] && break
    sleep 0.1
  done
  if [ "$S" != "popup" ]; then echo "FAIL popup $S"; exit 0; fi

  input tap $((FL + REL_X)) $((FT + REL_Y))
  j=0
  while :; do
    state
    [ "$S" = "list" ] && break
    j=$((j + 1))
    [ "$j" -ge 6 ] && break
    sleep 0.1
  done
  if [ "$S" != "list" ]; then echo "FAIL after $S"; exit 0; fi

  i=$((i + 1))
  echo "OK"
  sleep "$SETTLE"
done
echo "DONE"
"""

_STATE_RE = re.compile(r"^STATE (\w+) (\d+) (\d+) (\d+) (\d+)\s*$", re.MULTILINE)
_FAIL_RE = re.compile(r"^FAIL (\w+) (\w+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class WindowState:
    """Top window classification reported by the device script."""

    kind: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class BurstResult:
    """Outcome of one on-device burst run."""

    deleted: int
    completed: bool
    failure: str | None


def parse_probe(output: str) -> WindowState | None:
    """Parse the `probe` mode output line."""
    match = _STATE_RE.search(output)
    if not match:
        return None
    kind, left, top, right, bottom = match.groups()
    return WindowState(kind, int(left), int(top), int(right), int(bottom))


def parse_burst(output: str) -> BurstResult:
    """Parse `burst` mode output; every OK line is one verified deletion."""
    deleted = sum(1 for line in output.splitlines() if line.strip() == "OK")
    fail = _FAIL_RE.search(output)
    if fail:
        return BurstResult(deleted, False, f"{fail.group(1)}:{fail.group(2)}")
    completed = any(line.strip() == "DONE" for line in output.splitlines())
    return BurstResult(deleted, completed, None if completed else "interrupted")

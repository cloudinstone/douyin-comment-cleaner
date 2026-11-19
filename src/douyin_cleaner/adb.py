"""Android Debug Bridge (ADB) client wrapper."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Sequence


class AdbError(RuntimeError):
    """Raised when an ADB command fails or no valid device is available."""


class AdbClient:
    """Thin wrapper around the `adb` CLI for UI inspection and touch input."""

    def __init__(self, serial: str | None = None, adb_bin: str = "adb") -> None:
        self.serial = serial
        self.adb_bin = adb_bin

    def _base_cmd(self, with_serial: bool = True) -> list[str]:
        cmd = [self.adb_bin]
        if with_serial and self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def _run(
        self,
        args: Sequence[str],
        *,
        with_serial: bool = True,
        timeout: float = 15.0,
        check: bool = True,
    ) -> str:
        cmd = self._base_cmd(with_serial=with_serial) + list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise AdbError(
                f"ADB binary '{self.adb_bin}' not found in PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out: {' '.join(cmd)}") from exc

        if check and proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise AdbError(f"ADB command failed ({' '.join(cmd)}): {err}")

        return proc.stdout

    def list_devices(self) -> list[tuple[str, str]]:
        """Return connected devices as (serial, state) pairs."""
        output = self._run(["devices"], with_serial=False)
        devices: list[tuple[str, str]] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                devices.append((parts[0], parts[1]))
        return devices

    def resolve_device(self) -> str:
        """Ensure a usable device is connected and pin self.serial to it."""
        devices = self.list_devices()
        if not devices:
            raise AdbError(
                "No Android devices detected. Enable USB debugging and connect your phone."
            )

        if self.serial:
            for serial, state in devices:
                if serial == self.serial:
                    if state != "device":
                        raise AdbError(
                            f"Device '{serial}' is in state '{state}' (expected 'device')."
                        )
                    return serial
            raise AdbError(f"Requested device '{self.serial}' not found.")

        ready = [serial for serial, state in devices if state == "device"]
        if not ready:
            states = ", ".join(f"{s} ({st})" for s, st in devices)
            raise AdbError(f"No authorized devices ready. Found: {states}")
        if len(ready) > 1:
            raise AdbError(
                f"Multiple devices connected ({', '.join(ready)}). Pass -s SERIAL."
            )

        self.serial = ready[0]
        return self.serial

    def get_device_model(self) -> str:
        """Return ro.product.model for the active device."""
        model = self._run(["shell", "getprop", "ro.product.model"]).strip()
        return model or "Unknown"

    def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) in pixels, falling back to (1080, 2340)."""
        output = self._run(["shell", "wm", "size"], check=False)
        match = re.search(r"Override size:\s*(\d+)x(\d+)", output)
        if not match:
            match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if match:
            return int(match.group(1)), int(match.group(2))
        return 1080, 2340

    def is_screen_locked(self) -> bool:
        """Return True if the screen is asleep/dozing or locked on keyguard."""
        power_out = self._run(["shell", "dumpsys", "power"], check=False)
        if re.search(r"mWakefulness=(?:Asleep|Dozing)", power_out):
            return True

        window_out = self._run(["shell", "dumpsys", "window"], check=False)
        if re.search(r"mDreamingLockscreen=true|isStatusBarKeyguard=true", window_out):
            return True

        focus_match = re.search(r"mCurrentFocus=Window\{[^}]*\s([^\s}]+)\}", window_out)
        if focus_match:
            focused_win = focus_match.group(1)
            if "Keyguard" in focused_win or focused_win == "NotificationShade":
                # Verify keyguard policy state when NotificationShade has focus
                if re.search(r"showing=true", window_out):
                    return True
        return False

    def wake_up(self) -> None:
        """Wake the device screen."""
        self._run(["shell", "input", "keyevent", "KEYCODE_WAKEUP"])

    def get_foreground_activity(self) -> tuple[str, str]:
        """Return (package, activity) of the current focused app."""
        output = self._run(["shell", "dumpsys", "window"], check=False)

        # Prefer mCurrentFocus when it points to an app window
        focus_matches = re.findall(
            r"mCurrentFocus=Window\{[^\s]+\s+u\d+\s+([^\s}]+)\}", output
        )
        for token in reversed(focus_matches):
            if "/" in token:
                pkg, act = token.split("/", 1)
                if act.startswith("."):
                    act = f"{pkg}{act}"
                return pkg, act

        # Fall back to mFocusedApp ActivityRecord (useful when shade/keyguard overlays)
        app_matches = re.findall(
            r"mFocusedApp=ActivityRecord\{[^\s]+\s+u\d+\s+([^\s}]+)", output
        )
        for token in reversed(app_matches):
            if "/" in token:
                pkg, act = token.split("/", 1)
                if act.startswith("."):
                    act = f"{pkg}{act}"
                return pkg, act

        return "", ""

    def dump_hierarchy(self, retries: int = 2) -> str:
        """Dump the current UI hierarchy XML string via uiautomator."""
        for attempt in range(retries + 1):
            raw = self._run(
                ["exec-out", "uiautomator", "dump", "/dev/tty"],
                timeout=20.0,
                check=False,
            )
            xml_str = self._extract_xml(raw)
            if xml_str:
                return xml_str

            # Fallback via /sdcard/window_dump.xml if /dev/tty output was empty/truncated
            self._run(
                ["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
                timeout=20.0,
                check=False,
            )
            fallback_raw = self._run(
                ["exec-out", "cat", "/sdcard/window_dump.xml"],
                timeout=15.0,
                check=False,
            )
            xml_str = self._extract_xml(fallback_raw)
            if xml_str:
                return xml_str

            if attempt < retries:
                time.sleep(0.6)

        raise AdbError(
            "Failed to dump UI hierarchy XML. Ensure screen is unlocked and idle."
        )

    @staticmethod
    def _extract_xml(raw: str) -> str:
        start = raw.find("<?xml")
        if start == -1:
            start = raw.find("<hierarchy")
        end_tag = "</hierarchy>"
        end = raw.rfind(end_tag)
        if start == -1 or end == -1:
            return ""
        return raw[start : end + len(end_tag)].strip()

    def tap(self, x: int, y: int) -> None:
        """Tap screen coordinate (x, y)."""
        self._run(["shell", "input", "tap", str(int(x)), str(int(y))])

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        """Long-press screen coordinate (x, y) using an in-place swipe."""
        ix, iy = int(x), int(y)
        self._run(
            [
                "shell",
                "input",
                "swipe",
                str(ix),
                str(iy),
                str(ix),
                str(iy),
                str(int(duration_ms)),
            ]
        )

    def batch_long_press_and_tap(
        self,
        press_x: int,
        press_y: int,
        tap_x: int,
        tap_y: int,
        *,
        count: int,
        long_press_ms: int = 450,
        popup_settle_s: float = 0.15,
        after_tap_s: float = 0.25,
        delays_s: Sequence[float] | None = None,
    ) -> None:
        """Execute multiple long-press + tap pairs in a single adb shell call."""
        if count <= 0:
            return

        px, py = int(press_x), int(press_y)
        tx, ty = int(tap_x), int(tap_y)
        lp_ms = int(long_press_ms)

        if not delays_s:
            body_parts = [f"input swipe {px} {py} {px} {py} {lp_ms}"]
            if popup_settle_s > 0:
                body_parts.append(f"sleep {popup_settle_s:.3f}")
            body_parts.append(f"input tap {tx} {ty}")
            if after_tap_s > 0:
                body_parts.append(f"sleep {after_tap_s:.3f}")
            body = "; ".join(body_parts)
            script = (
                f"i=0; while [ $i -lt {count} ]; do {body}; i=$((i+1)); done"
            )
            per_item_s = lp_ms / 1000.0 + 0.5 + max(0.0, popup_settle_s) + max(0.0, after_tap_s)
            timeout = max(20.0, per_item_s * count * 2.0 + 10.0)
            self._run(["shell", script], timeout=timeout)
            return

        cmds: list[str] = []
        estimated_s = 0.0
        for idx in range(count):
            cmds.append(f"input swipe {px} {py} {px} {py} {lp_ms}")
            estimated_s += lp_ms / 1000.0 + 0.25
            if popup_settle_s > 0:
                cmds.append(f"sleep {popup_settle_s:.3f}")
                estimated_s += popup_settle_s
            cmds.append(f"input tap {tx} {ty}")
            estimated_s += 0.25

            extra = delays_s[idx] if idx < len(delays_s) else 0.0
            tail_sleep = (after_tap_s if idx < count - 1 else popup_settle_s) + max(
                0.0, extra
            )
            if tail_sleep > 0:
                cmds.append(f"sleep {tail_sleep:.3f}")
                estimated_s += tail_sleep

        timeout = max(20.0, estimated_s * 2.0 + 10.0)
        self._run(["shell", "; ".join(cmds)], timeout=timeout)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400
    ) -> None:
        """Swipe from (x1, y1) to (x2, y2)."""
        self._run(
            [
                "shell",
                "input",
                "swipe",
                str(int(x1)),
                str(int(y1)),
                str(int(x2)),
                str(int(y2)),
                str(int(duration_ms)),
            ]
        )

    def press_back(self) -> None:
        """Send Android BACK keyevent (KEYCODE_BACK = 4)."""
        self._run(["shell", "input", "keyevent", "4"])

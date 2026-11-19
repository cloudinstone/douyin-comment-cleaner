"""Android Debug Bridge (ADB) client wrapper."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
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

    def push_text(self, content: str, remote_path: str) -> None:
        """Write `content` to `remote_path` on the device via `adb push`."""
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", suffix=".sh", delete=False
        ) as fp:
            fp.write(content)
            local_path = fp.name
        try:
            self._run(["push", local_path, remote_path], timeout=20.0)
        finally:
            os.unlink(local_path)

    def run_script(
        self, remote_path: str, args: Sequence[str], timeout: float = 30.0
    ) -> str:
        """Run a pushed shell script and return stdout, tolerating non-zero exit.

        Partial stdout is still returned when the adb transport drops mid-run, so
        callers can account for the work that was reported before the failure.
        """
        return self._run(
            ["shell", "sh", remote_path, *[str(a) for a in args]],
            timeout=timeout,
            check=False,
        )

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

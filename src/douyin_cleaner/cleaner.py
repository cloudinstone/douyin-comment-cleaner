"""Deletion controller for Douyin outbound comments.

Two deletion paths:

* Verified path: one UI dump per comment. Used for calibration, text filters,
  non-deletable rows and as the fallback whenever anything looks unexpected.
* Burst path: after calibration, an on-device script deletes the top comment
  repeatedly, checking the window stack around every touch (see `burst.py`).
  It never taps "delete" unless the action dialog is verifiably on top.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adb import AdbClient
from .burst import (
    DEVICE_SCRIPT,
    REMOTE_SCRIPT_PATH,
    WindowState,
    parse_burst,
    parse_probe,
)
from .parser import Bounds, CommentRow, ScreenAnalysis, analyze_screen

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"
COMMENT_LIST_ACTIVITY_HINTS = (
    "NotificationDetailActivity",
    "IntegratedInteractionActivity",
)

POPUP_WAIT_S = 0.3  # wait after long-press before dumping the popup
AFTER_TAP_S = 0.3  # wait after tapping a dialog button
BURST_SIZE = 10  # deletions per adb shell session (keeps sessions short)
VERIFY_EVERY = 30  # full UI dump after this many burst deletions
BURST_SETTLE_S = 0.35  # list re-layout time after each burst deletion
MAX_NO_PROGRESS = 2
MAX_ROW_ATTEMPTS = 2


class StopRun(Exception):
    """Raised to end the run with a machine-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CleanerConfig:
    """Runtime configuration for CommentCleaner."""

    limit: int = 0
    min_delay: float = 0.0
    max_delay: float = 0.0
    long_press_ms: int = 550
    max_empty_scrolls: int = 5
    dry_run: bool = False
    burst: bool = True
    contains: str | None = None
    exclude: str | None = None
    audit_log: bool = False
    skip_subtab_check: bool = False
    log_file: Path = Path("logs/deleted_comments.jsonl")


@dataclass(frozen=True)
class CleanerResult:
    """Summary returned after a cleaner run completes or stops."""

    deleted_count: int
    skipped_count: int
    stopped_reason: str


@dataclass(frozen=True)
class Calibration:
    """Burst parameters learned from one verified deletion."""

    press: tuple[int, int]
    rel_delete: tuple[int, int]
    popup_size: tuple[int, int]


class CommentCleaner:
    """Deletes outbound comments on the currently open '发出的评论' list."""

    def __init__(self, adb: AdbClient, config: CleanerConfig) -> None:
        self.adb = adb
        self.config = config
        self.deleted_count = 0
        self.skipped: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.screen_w = 1080
        self.screen_h = 2340
        self.list_activity = ""
        self.calibration: Calibration | None = None
        self.burst_supported = config.burst
        self.script_ready = False
        self.empty_scrolls = 0
        self.no_progress = 0
        self._last_viewport: set[str] = set()

    # ------------------------------------------------------------------ run

    def run(self) -> CleanerResult:
        """Delete comments until finished, limited, or halted by a safety check."""
        serial = self.adb.resolve_device()
        model = self.adb.get_device_model()
        self.screen_w, self.screen_h = self.adb.get_screen_size()
        print(
            f"[INIT] Device: {model} ({serial}), resolution: "
            f"{self.screen_w}x{self.screen_h}, dry_run={self.config.dry_run}"
        )
        try:
            self._preflight()
            reason = self._loop()
        except StopRun as stop:
            print(f"[STOP] {stop}")
            reason = stop.reason
        return CleanerResult(self.deleted_count, len(self.skipped), reason)

    def _preflight(self) -> None:
        if not self._ensure_unlocked():
            raise StopRun("screen_locked", "Device remained locked.")
        pkg, act = self.adb.get_foreground_activity()
        if pkg != DOUYIN_PACKAGE or not any(
            hint in act for hint in COMMENT_LIST_ACTIVITY_HINTS
        ):
            raise StopRun(
                "wrong_app_or_activity",
                f"Foreground is '{pkg}/{act}'. Open Douyin '发出的评论' before starting.",
            )
        self.list_activity = act
        analysis = self._dump()
        if not self.config.skip_subtab_check and analysis.subtab_sent_selected is not True:
            raise StopRun(
                "wrong_subtab",
                "Sub-tab '发出的评论' is not visible/selected. Switch to it first "
                "(or pass --skip-subtab-check if the header is scrolled out of view).",
            )

    def _loop(self) -> str:
        while True:
            if self._limit_reached():
                print(f"[DONE] Reached deletion limit ({self.config.limit}).")
                return "limit_reached"

            self._ensure_on_list()
            analysis = self._dump()
            self._check_safety(analysis)

            if analysis.confirm_button or analysis.has_popup_menu:
                # Leftover dialog from an interrupted step: close it, re-evaluate
                print("[RECOVER] Closing leftover dialog.")
                self.adb.press_back()
                time.sleep(AFTER_TAP_S)
                continue

            row = self._pick_next_row(analysis)
            if row is None:
                if analysis.reached_end:
                    print("[DONE] Reached end of comment list.")
                    return "reached_end"
                if self._scroll_or_finish(analysis):
                    return "max_empty_scrolls"
                continue
            self.empty_scrolls = 0

            if self.config.dry_run:
                print(f"[DRY-RUN] Would delete: {row.summary}")
                self.skipped.add(row.fingerprint)
                continue

            if self._can_burst(analysis, row):
                self._burst_until_verify(row)
            else:
                self._delete_verified(row)

    # ---------------------------------------------------------- safety

    def _dump(self) -> ScreenAnalysis:
        xml_str = self.adb.dump_hierarchy()
        return analyze_screen(xml_str, self.screen_w, self.screen_h)

    def _check_safety(self, analysis: ScreenAnalysis) -> None:
        if analysis.is_lockscreen and not self._ensure_unlocked():
            raise StopRun("screen_locked", "Device remained locked.")
        if analysis.rate_limit_or_captcha:
            raise StopRun(
                "rate_limit_or_captcha",
                f"Rate-limit or captcha detected: {analysis.rate_limit_or_captcha}",
            )
        if analysis.subtab_sent_selected is False:
            raise StopRun("wrong_subtab", "Sub-tab '发出的评论' is no longer selected.")

    def _ensure_on_list(self) -> None:
        """Verify the comment list is foreground; press BACK at most once."""
        pkg, act = self.adb.get_foreground_activity()
        if pkg == DOUYIN_PACKAGE and act == self.list_activity:
            return
        if pkg != DOUYIN_PACKAGE:
            raise StopRun(
                "wrong_app_or_activity",
                f"Foreground app is '{pkg}/{act}', expected Douyin.",
            )
        print(f"[RECOVER] Left the comment list ('{act}'), pressing BACK once...")
        self.calibration = None
        self.adb.press_back()
        time.sleep(1.0)
        pkg, act = self.adb.get_foreground_activity()
        if pkg != DOUYIN_PACKAGE or act != self.list_activity:
            raise StopRun(
                "lost_comment_list",
                f"Still on '{pkg}/{act}' after one BACK. Return to '发出的评论' and rerun.",
            )

    def _ensure_unlocked(self, timeout_s: float = 20.0) -> bool:
        if not self.adb.is_screen_locked():
            return True
        self.adb.wake_up()
        print("[WAIT] Screen is locked. Unlock your phone to continue...")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(2.0)
            if not self.adb.is_screen_locked():
                print("[READY] Device unlocked.")
                return True
        return False

    # ---------------------------------------------------------- row picking

    def _pick_next_row(self, analysis: ScreenAnalysis) -> CommentRow | None:
        for row in analysis.comment_rows:
            if row.fingerprint in self.skipped:
                continue
            if not self._matches_filters(row):
                self.skipped.add(row.fingerprint)
                print(f"[FILTER] Skipped: {row.summary}")
                continue
            return row
        return None

    def _matches_filters(self, row: CommentRow) -> bool:
        joined = "\n".join(row.texts)
        if self.config.contains and self.config.contains not in joined:
            return False
        if self.config.exclude and self.config.exclude in joined:
            return False
        return True

    def _scroll_or_finish(self, analysis: ScreenAnalysis) -> bool:
        """Scroll for more rows; return True once scrolling stops yielding rows."""
        current = {row.fingerprint for row in analysis.comment_rows}
        if current and current == self._last_viewport:
            self.empty_scrolls += 1
        self._last_viewport = current
        if self.empty_scrolls >= self.config.max_empty_scrolls:
            print(f"[DONE] No new comments after {self.empty_scrolls} scrolls.")
            return True
        x = self.screen_w // 2
        self.adb.swipe(x, int(self.screen_h * 0.75), x, int(self.screen_h * 0.28), 300)
        time.sleep(0.6)
        return False

    # ---------------------------------------------------------- verified path

    def _delete_verified(self, row: CommentRow) -> None:
        """Long-press, confirm the delete option via UI dump, then tap it."""
        px, py = row.tap_point
        self.adb.long_press(px, py, duration_ms=self.config.long_press_ms)
        time.sleep(POPUP_WAIT_S)
        post = self._dump()
        self._check_safety(post)

        if post.delete_button is None:
            if post.has_popup_menu:
                print(f"[SKIP] No delete option: {row.summary}")
                self.adb.press_back()
                time.sleep(AFTER_TAP_S)
                self.skipped.add(row.fingerprint)
                return
            attempts = self.attempts.get(row.fingerprint, 0) + 1
            self.attempts[row.fingerprint] = attempts
            if attempts >= MAX_ROW_ATTEMPTS:
                print(f"[SKIP] Long-press did not open a menu: {row.summary}")
                self.skipped.add(row.fingerprint)
            return  # main loop re-checks foreground and state

        if self._is_top_row_candidate(row):
            self._calibrate(row, post.delete_button)

        dx, dy = post.delete_button.center
        self.adb.tap(dx, dy)
        time.sleep(AFTER_TAP_S)

        follow = self._dump()
        if follow.confirm_button is not None:
            cx, cy = follow.confirm_button.center
            self.adb.tap(cx, cy)
            time.sleep(AFTER_TAP_S)
        self._record(row)
        self._sleep_random_delay()

    # ---------------------------------------------------------- burst path

    def _burst_eligible(self) -> bool:
        return (
            self.burst_supported
            and not self.config.dry_run
            and not self.config.contains
            and not self.config.exclude
            and not self.config.audit_log
        )

    def _is_top_row_candidate(self, row: CommentRow) -> bool:
        return self._burst_eligible() and self.calibration is None

    def _can_burst(self, analysis: ScreenAnalysis, row: CommentRow) -> bool:
        return (
            self._burst_eligible()
            and self.calibration is not None
            and bool(analysis.comment_rows)
            and analysis.comment_rows[0].fingerprint == row.fingerprint
            and not analysis.reached_end
            and len(analysis.comment_rows) >= 3
        )

    def _ensure_script(self) -> None:
        if not self.script_ready:
            self.adb.push_text(DEVICE_SCRIPT, REMOTE_SCRIPT_PATH)
            self.script_ready = True

    def _full_height_threshold(self) -> int:
        return int(self.screen_h * 0.8)

    def _activity_simple_name(self) -> str:
        return self.list_activity.rsplit(".", 1)[-1]

    def _probe(self) -> WindowState | None:
        self._ensure_script()
        output = self.adb.run_script(
            REMOTE_SCRIPT_PATH,
            ["probe", self._activity_simple_name(), self._full_height_threshold()],
            timeout=15.0,
        )
        return parse_probe(output)

    def _calibrate(self, row: CommentRow, delete_button: Bounds) -> None:
        """Learn the action dialog geometry while it is open (before tapping)."""
        state = self._probe()
        dx, dy = delete_button.center
        if (
            state is None
            or state.kind != "dialog"
            or not (state.left <= dx <= state.right and state.top <= dy <= state.bottom)
        ):
            print(
                "[INFO] Action menu is not a separate window on this device; "
                "using verified per-comment mode (slower)."
            )
            self.burst_supported = False
            return
        self.calibration = Calibration(
            press=row.tap_point,
            rel_delete=(dx - state.left, dy - state.top),
            popup_size=(state.width, state.height),
        )
        print(
            f"[CALIBRATE] press={row.tap_point} dialog={state.width}x{state.height} "
            f"delete@+{dx - state.left},+{dy - state.top}"
        )

    def _burst_until_verify(self, top_row: CommentRow) -> None:
        """Run guarded bursts until VERIFY_EVERY deletions or the first anomaly."""
        assert self.calibration is not None
        cal = self.calibration
        done_since_verify = 0
        while done_since_verify < VERIFY_EVERY and not self._limit_reached():
            n = min(BURST_SIZE, VERIFY_EVERY - done_since_verify)
            if self.config.limit > 0:
                n = min(n, self.config.limit - self.deleted_count)
            if self.config.max_delay > 0:
                n = 1
            args = [
                "burst",
                self._activity_simple_name(),
                self._full_height_threshold(),
                n,
                cal.press[0],
                cal.press[1],
                self.config.long_press_ms,
                cal.rel_delete[0],
                cal.rel_delete[1],
                cal.popup_size[0],
                cal.popup_size[1],
                BURST_SETTLE_S,
            ]
            self._ensure_script()
            output = self.adb.run_script(REMOTE_SCRIPT_PATH, args, timeout=n * 4.0 + 15.0)
            result = parse_burst(output)
            self.deleted_count += result.deleted
            done_since_verify += result.deleted
            if result.deleted:
                print(f"[+] Deleted {result.deleted} (total: {self.deleted_count})")
            if not result.completed:
                print(f"[CHECK] Burst paused ({result.failure}); re-verifying screen.")
                if result.deleted == 0:
                    self.calibration = None  # recalibrate via the verified path
                break
            self._sleep_random_delay()

        self._verify_progress(top_row, done_since_verify)

    def _verify_progress(self, top_row: CommentRow, deleted: int) -> None:
        """Confirm the list actually changed after reported deletions."""
        if deleted == 0:
            return
        self._ensure_on_list()
        analysis = self._dump()
        self._check_safety(analysis)
        top = analysis.comment_rows[0].fingerprint if analysis.comment_rows else None
        if top == top_row.fingerprint:
            self.no_progress += 1
            self.calibration = None
            print("[CHECK] Top comment unchanged after burst; recalibrating.")
            if self.no_progress >= MAX_NO_PROGRESS:
                raise StopRun(
                    "no_progress",
                    "Deletions are not taking effect (possible silent rate limit).",
                )
        else:
            self.no_progress = 0

    # ---------------------------------------------------------- bookkeeping

    def _limit_reached(self) -> bool:
        return self.config.limit > 0 and self.deleted_count >= self.config.limit

    def _record(self, row: CommentRow) -> None:
        self.deleted_count += 1
        print(f"[+] Deleted #{self.deleted_count}: {row.summary}")
        if not self.config.audit_log:
            return
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "index": self.deleted_count,
            "summary": row.summary,
            "texts": row.texts,
        }
        log_path = self.config.log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _sleep_random_delay(self) -> None:
        low = min(self.config.min_delay, self.config.max_delay)
        high = max(self.config.min_delay, self.config.max_delay)
        if high > 0:
            time.sleep(random.uniform(low, high))

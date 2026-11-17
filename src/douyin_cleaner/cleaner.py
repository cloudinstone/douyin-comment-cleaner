"""Batch deletion state machine for Douyin outbound comments."""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adb import AdbClient
from .parser import Bounds, CommentRow, ScreenAnalysis, analyze_screen

DOUYIN_PACKAGE = "com.ss.android.ugc.aweme"
# Short pause to let popup/menu animations settle before the next UI dump
SETTLE_DELAY = 0.3

DRIFT_ACTIVITY_HINTS = (
    "DetailActivity",
    "UserProfileActivity",
    "LivePlayActivity",
    "FeedActivity",
)


@dataclass(frozen=True)
class CleanerConfig:
    """Runtime configuration for CommentCleaner."""

    limit: int = 0
    min_delay: float = 0.0
    max_delay: float = 0.0
    long_press_ms: int = 600
    max_empty_scrolls: int = 5
    dry_run: bool = False
    contains: str | None = None
    exclude: str | None = None
    log_file: Path = Path("logs/deleted_comments.jsonl")


@dataclass(frozen=True)
class CleanerResult:
    """Summary returned after a cleaner run completes or stops."""

    deleted_count: int
    skipped_count: int
    stopped_reason: str


class CommentCleaner:
    """Drives ADB touch events to delete outbound comments safely."""

    def __init__(self, adb: AdbClient, config: CleanerConfig) -> None:
        self.adb = adb
        self.config = config
        self.deleted_count = 0
        self.skipped_fingerprints: set[str] = set()
        self.retry_counts: dict[str, int] = {}
        self.empty_scrolls = 0
        self.consecutive_wrong_activity = 0
        self._last_viewport_fps: set[str] = set()
        self._initial_activity: str | None = None
        self._awaiting_confirm = False

    def run(self) -> CleanerResult:
        """Execute the batch deletion loop until finished, limited, or halted."""
        serial = self.adb.resolve_device()
        model = self.adb.get_device_model()
        screen_w, screen_h = self.adb.get_screen_size()
        print(
            f"[INIT] Device: {model} ({serial}), resolution: {screen_w}x{screen_h}, "
            f"dry_run={self.config.dry_run}"
        )

        if not self._ensure_unlocked():
            return CleanerResult(
                deleted_count=self.deleted_count,
                skipped_count=len(self.skipped_fingerprints),
                stopped_reason="screen_locked",
            )

        stopped_reason = "completed"
        while True:
            if self.config.limit > 0 and self.deleted_count >= self.config.limit:
                print(f"[DONE] Reached deletion limit ({self.config.limit}).")
                stopped_reason = "limit_reached"
                break

            if not self._check_foreground_ready():
                stopped_reason = "wrong_app_or_activity"
                break

            xml_str = self.adb.dump_hierarchy()
            analysis = analyze_screen(xml_str, screen_w, screen_h)

            if analysis.is_lockscreen:
                if not self._ensure_unlocked():
                    stopped_reason = "screen_locked"
                    break
                continue

            if analysis.rate_limit_or_captcha:
                print(
                    f"[STOP] Rate-limit or captcha prompt detected: "
                    f"{analysis.rate_limit_or_captcha}"
                )
                stopped_reason = "rate_limit_or_captcha"
                break

            if (
                analysis.subtab_sent_comments is not None
                and analysis.subtab_sent_selected is False
            ):
                print(
                    "[STOP] Sub-tab '发出的评论' is not selected. "
                    "Switch to '发出的评论' to avoid touching other messages."
                )
                stopped_reason = "wrong_subtab"
                break

            if analysis.confirm_button is not None:
                if self.config.dry_run:
                    print("[DRY-RUN] Confirmation dialog visible; stopping preview.")
                    stopped_reason = "dry_run_dialog"
                    break
                cx, cy = analysis.confirm_button.center
                self.adb.tap(cx, cy)
                time.sleep(SETTLE_DELAY)
                # Deletion was already recorded when "删除" was tapped
                if not self._awaiting_confirm:
                    self._record_deletion(None)
                self._awaiting_confirm = False
                continue

            if analysis.delete_button is not None:
                if self.config.dry_run:
                    print("[DRY-RUN] Delete button visible; stopping preview.")
                    stopped_reason = "dry_run_dialog"
                    break
                halt = self._execute_delete_tap(
                    analysis.delete_button, row=None, screen_w=screen_w, screen_h=screen_h
                )
                if halt:
                    stopped_reason = halt
                    break
                self._sleep_between_deletes()
                continue

            # No dialog on screen: any pending delete/confirm flow is finished
            self._awaiting_confirm = False
            target_row = self._pick_next_row(analysis)
            if target_row is not None:
                if self.config.dry_run:
                    print(
                        f"[DRY-RUN] Would delete at {target_row.tap_point}: "
                        f"{target_row.summary}"
                    )
                    self.skipped_fingerprints.add(target_row.fingerprint)
                    self.empty_scrolls = 0
                    continue

                halt = self._process_row(target_row, screen_w, screen_h)
                if halt:
                    stopped_reason = halt
                    break
                continue

            if analysis.reached_end:
                print("[DONE] Reached end of comment list.")
                stopped_reason = "reached_end"
                break

            current_fps = {row.fingerprint for row in analysis.comment_rows}
            new_unskipped = (
                current_fps - self._last_viewport_fps - self.skipped_fingerprints
            )
            if new_unskipped:
                self.empty_scrolls = 0
            elif self._last_viewport_fps:
                self.empty_scrolls += 1

            self._last_viewport_fps = current_fps
            if self.empty_scrolls >= self.config.max_empty_scrolls:
                print(
                    f"[DONE] No new comments found after {self.empty_scrolls} scrolls."
                )
                stopped_reason = "max_empty_scrolls"
                break

            self._scroll_down_list(screen_w, screen_h)

        return CleanerResult(
            deleted_count=self.deleted_count,
            skipped_count=len(self.skipped_fingerprints),
            stopped_reason=stopped_reason,
        )

    def _ensure_unlocked(self, timeout_s: float = 20.0) -> bool:
        if not self.adb.is_screen_locked():
            return True
        self.adb.wake_up()
        print(
            "[WAIT] Device screen is locked. Woke screen; unlock your phone to continue..."
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(2.0)
            if not self.adb.is_screen_locked():
                print("[READY] Device unlocked.")
                return True
        print("[STOP] Device remained locked.")
        return False

    def _check_foreground_ready(self) -> bool:
        pkg, act = self.adb.get_foreground_activity()
        if not pkg:
            return True

        if pkg != DOUYIN_PACKAGE:
            print(
                f"[STOP] Foreground app is '{pkg}/{act}', expected '{DOUYIN_PACKAGE}'."
            )
            return False

        if self._initial_activity is None and not self._is_drift_activity(act):
            self._initial_activity = act

        is_drifted = self._is_drift_activity(act) or (
            self._initial_activity is not None
            and act
            and act != self._initial_activity
            and "Dialog" not in act
            and "Popup" not in act
        )
        if is_drifted:
            self.consecutive_wrong_activity += 1
            if self.consecutive_wrong_activity > 3:
                print(
                    f"[STOP] Stuck on unexpected activity '{act}' after recovery attempts."
                )
                return False
            print(f"[RECOVER] Drifted to '{act}', pressing BACK...")
            self.adb.press_back()
            time.sleep(1.0)
            return self._check_foreground_ready()

        self.consecutive_wrong_activity = 0
        return True

    @staticmethod
    def _is_drift_activity(activity: str) -> bool:
        if not activity:
            return False
        if "NotificationDetailActivity" in activity:
            return False
        return any(hint in activity for hint in DRIFT_ACTIVITY_HINTS)

    def _pick_next_row(self, analysis: ScreenAnalysis) -> CommentRow | None:
        for row in analysis.comment_rows:
            if row.fingerprint in self.skipped_fingerprints:
                continue
            if not self._matches_filters(row):
                self.skipped_fingerprints.add(row.fingerprint)
                print(f"[FILTER] Skipped by text filter: {row.summary}")
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

    def _process_row(
        self, row: CommentRow, screen_w: int, screen_h: int
    ) -> str | None:
        tx, ty = row.tap_point
        self.adb.long_press(tx, ty, duration_ms=self.config.long_press_ms)
        time.sleep(SETTLE_DELAY)

        post_xml = self.adb.dump_hierarchy()
        post = analyze_screen(post_xml, screen_w, screen_h)

        if post.rate_limit_or_captcha:
            print(
                f"[STOP] Rate-limit or captcha prompt detected: "
                f"{post.rate_limit_or_captcha}"
            )
            return "rate_limit_or_captcha"

        if post.delete_button is not None:
            halt = self._execute_delete_tap(post.delete_button, row, screen_w, screen_h)
            if halt:
                return halt
            self._sleep_between_deletes()
            return None

        if post.confirm_button is not None:
            self._tap_confirm_and_record(post.confirm_button, row)
            self._sleep_between_deletes()
            return None

        if post.has_popup_menu:
            self.adb.press_back()
            time.sleep(SETTLE_DELAY)
            self.skipped_fingerprints.add(row.fingerprint)
            print(f"[SKIP] Non-deletable item (no delete option): {row.summary}")
            return None

        # Recover if long-press or accidental tap navigated away
        pkg, act = self.adb.get_foreground_activity()
        if pkg == DOUYIN_PACKAGE and (
            self._is_drift_activity(act)
            or (self._initial_activity and act != self._initial_activity)
        ):
            print(f"[RECOVER] Unexpected navigation to '{act}', pressing BACK...")
            self.adb.press_back()
            time.sleep(0.8)

        attempts = self.retry_counts.get(row.fingerprint, 0) + 1
        self.retry_counts[row.fingerprint] = attempts
        if attempts >= 2:
            self.skipped_fingerprints.add(row.fingerprint)
            print(f"[SKIP] Unresponsive or non-deletable item: {row.summary}")
        return None

    def _execute_delete_tap(
        self,
        delete_bounds: Bounds,
        row: CommentRow | None,
        screen_w: int,
        screen_h: int,
    ) -> str | None:
        # No re-dump here: if a confirmation dialog appears, the main loop's
        # next dump detects and taps it without double-counting.
        dx, dy = delete_bounds.center
        self.adb.tap(dx, dy)
        time.sleep(SETTLE_DELAY)
        if not self._awaiting_confirm:
            self._record_deletion(row)
        self._awaiting_confirm = True
        return None

    def _tap_confirm_and_record(
        self, confirm_bounds: Bounds, row: CommentRow | None
    ) -> None:
        cx, cy = confirm_bounds.center
        self.adb.tap(cx, cy)
        time.sleep(SETTLE_DELAY)
        self._record_deletion(row)

    def _record_deletion(self, row: CommentRow | None) -> None:
        self.deleted_count += 1
        self.empty_scrolls = 0
        self._last_viewport_fps.clear()

        summary = row.summary if row else "<popup-triggered>"
        print(f"[+] Deleted #{self.deleted_count}: {summary}")

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "index": self.deleted_count,
            "summary": summary,
            "texts": row.texts if row else [],
            "bounds": row.bounds.format() if row else None,
        }
        log_path = self.config.log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _scroll_down_list(self, screen_w: int, screen_h: int) -> None:
        x = screen_w // 2
        y_start = int(screen_h * 0.75)
        y_end = int(screen_h * 0.28)
        self.adb.swipe(x, y_start, x, y_end, duration_ms=300)
        time.sleep(0.5)

    def _sleep_between_deletes(self) -> None:
        low = min(self.config.min_delay, self.config.max_delay)
        high = max(self.config.min_delay, self.config.max_delay)
        time.sleep(random.uniform(low, high))

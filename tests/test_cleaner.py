"""Unit tests for CommentCleaner batch burst deletion and ADB batching."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from douyin_cleaner.adb import AdbClient
from douyin_cleaner.cleaner import CleanerConfig, CommentCleaner
from douyin_cleaner.parser import Bounds, CommentRow, ScreenAnalysis


def _make_row(idx: int, text: str | None = None) -> CommentRow:
    top = 445 + idx * 240
    body = text or f"comment-{idx}"
    return CommentRow(
        bounds=Bounds(0, top, 1080, top + 240),
        tap_point=(540, top + 120),
        texts=(body,),
        summary=body,
        fingerprint=f"fp-{idx}",
    )


def _make_screen(
    *,
    comment_rows: list[CommentRow] | None = None,
    delete_button: Bounds | None = None,
    reached_end: bool = False,
) -> ScreenAnalysis:
    return ScreenAnalysis(
        package="com.ss.android.ugc.aweme",
        is_lockscreen=False,
        rate_limit_or_captcha=None,
        delete_button=delete_button,
        confirm_button=None,
        has_popup_menu=delete_button is not None,
        subtab_sent_comments=None,
        subtab_sent_selected=True,
        comment_rows=comment_rows or [],
        reached_end=reached_end,
    )


class FakeAdb:
    """In-memory stub of AdbClient for deterministic state machine tests."""

    def __init__(self) -> None:
        self.dumps: list[ScreenAnalysis] = []
        self.batch_calls: list[tuple[int, int, int, int, int]] = []
        self.long_presses: list[tuple[int, int, int]] = []
        self.taps: list[tuple[int, int]] = []
        self.dump_index = 0

    def resolve_device(self) -> str:
        return "TEST-SERIAL"

    def get_device_model(self) -> str:
        return "Pixel Test"

    def get_screen_size(self) -> tuple[int, int]:
        return 1080, 2340

    def is_screen_locked(self) -> bool:
        return False

    def get_foreground_activity(self) -> tuple[str, str]:
        return (
            "com.ss.android.ugc.aweme",
            "com.ss.android.ugc.aweme.socialnotice.view.activity.NotificationDetailActivity",
        )

    def dump_hierarchy(self) -> str:
        idx = self.dump_index
        self.dump_index += 1
        return f"dump-{idx}"

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.long_presses.append((x, y, duration_ms))

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

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
        delays_s=None,
    ) -> None:
        self.batch_calls.append((press_x, press_y, tap_x, tap_y, count))

    def press_back(self) -> None:
        pass

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        pass


class AdbBatchCommandTests(unittest.TestCase):
    def test_batch_long_press_and_tap_builds_single_shell_script(self) -> None:
        client = AdbClient(serial="SER123")
        captured: list[tuple[list[str], float]] = []

        def fake_run(args, *, with_serial=True, timeout=15.0, check=True):
            captured.append((list(args), timeout))
            return ""

        client._run = fake_run  # type: ignore[method-assign]
        client.batch_long_press_and_tap(
            540,
            568,
            540,
            1192,
            count=3,
            long_press_ms=450,
            popup_settle_s=0.15,
            after_tap_s=0.25,
        )

        self.assertEqual(len(captured), 1)
        args, timeout = captured[0]
        self.assertEqual(args[0], "shell")
        script = args[1]
        self.assertIn("while [ $i -lt 3 ]", script)
        self.assertIn("input swipe 540 568 540 568 450", script)
        self.assertIn("input tap 540 1192", script)
        self.assertGreaterEqual(timeout, 20.0)


class CleanerBatchTests(unittest.TestCase):
    def test_burst_deletes_contiguous_rows_and_stops_at_filter_gap(self) -> None:
        row0 = _make_row(0, "delete-me-0")
        row1 = _make_row(1, "delete-me-1")
        row2 = _make_row(2, "delete-me-2")
        row_keep = _make_row(3, "keep-me")
        row4 = _make_row(4, "delete-me-4")

        screens = {
            # Initial screen before calibration
            "dump-0": _make_screen(comment_rows=[row0, row1, row2, row_keep, row4]),
            # Post long-press dump for calibration
            "dump-1": _make_screen(delete_button=Bounds(400, 1140, 680, 1240)),
            # After row0 deleted, row1 & row2 are contiguous before row_keep
            "dump-2": _make_screen(comment_rows=[row1, row2, row_keep, row4]),
            # After batch(row1, row2) deleted, row_keep is skipped and row4 is deleted
            "dump-3": _make_screen(comment_rows=[row_keep, row4]),
            # End of list
            "dump-4": _make_screen(comment_rows=[], reached_end=True),
        }

        adb = FakeAdb()
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = CleanerConfig(
                fast=True,
                batch_size=0,
                exclude="keep-me",
                log_file=Path(tmpdir) / "deleted.jsonl",
            )
            cleaner = CommentCleaner(adb=adb, config=cfg)  # type: ignore[arg-type]
            with (
                patch("douyin_cleaner.cleaner.analyze_screen", side_effect=lambda xml, w, h: screens[xml]),
                patch("douyin_cleaner.cleaner.time.sleep"),
            ):
                result = cleaner.run()

        self.assertEqual(result.deleted_count, 4)
        self.assertEqual(result.stopped_reason, "reached_end")
        # 1 calibrated single delete, then batch of 2 (row1, row2), then batch of 1 (row4)
        self.assertEqual(len(adb.long_presses), 1)
        self.assertEqual([call[4] for call in adb.batch_calls], [2, 1])

    def test_failed_batch_invalidates_cache_and_recalibrates(self) -> None:
        row0 = _make_row(0)
        row1 = _make_row(1)
        row2 = _make_row(2)

        screens = {
            "dump-0": _make_screen(comment_rows=[row0, row1, row2]),
            "dump-1": _make_screen(delete_button=Bounds(400, 1140, 680, 1240)),
            "dump-2": _make_screen(comment_rows=[row1, row2]),
            # Simulate row2 still present after batch(row1, row2) -> must recalibrate
            "dump-3": _make_screen(comment_rows=[row2]),
            "dump-4": _make_screen(delete_button=Bounds(400, 1140, 680, 1240)),
            "dump-5": _make_screen(comment_rows=[], reached_end=True),
        }

        adb = FakeAdb()
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = CleanerConfig(
                fast=True,
                batch_size=0,
                log_file=Path(tmpdir) / "deleted.jsonl",
            )
            cleaner = CommentCleaner(adb=adb, config=cfg)  # type: ignore[arg-type]
            with (
                patch("douyin_cleaner.cleaner.analyze_screen", side_effect=lambda xml, w, h: screens[xml]),
                patch("douyin_cleaner.cleaner.time.sleep"),
            ):
                result = cleaner.run()

        self.assertEqual(result.stopped_reason, "reached_end")
        # Two full calibration long-presses: initial row0 + fallback row2
        self.assertEqual(len(adb.long_presses), 2)
        self.assertEqual([call[4] for call in adb.batch_calls], [2])

    def test_blind_burst_skips_dumps_until_verify_every(self) -> None:
        rows = [_make_row(i) for i in range(6)]
        screens = {
            # Initial screen before calibration (6 rows -> not near_end)
            "dump-0": _make_screen(comment_rows=rows),
            # Calibration dump
            "dump-1": _make_screen(delete_button=Bounds(400, 1140, 680, 1240)),
            # Post-calibration verification dump -> triggers burst #1 (5 items)
            "dump-2": _make_screen(comment_rows=rows[1:] + [_make_row(6)]),
            # Burst #2 skips dump! Next dump is dump-3 after verify_every=2 bursts
            "dump-3": _make_screen(comment_rows=[], reached_end=True),
        }

        adb = FakeAdb()
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = CleanerConfig(
                fast=True,
                batch_size=5,
                verify_every=2,
                audit_log=False,
                log_file=Path(tmpdir) / "deleted.jsonl",
            )
            cleaner = CommentCleaner(adb=adb, config=cfg)  # type: ignore[arg-type]
            with (
                patch("douyin_cleaner.cleaner.analyze_screen", side_effect=lambda xml, w, h: screens[xml]),
                patch("douyin_cleaner.cleaner.time.sleep"),
            ):
                result = cleaner.run()

        self.assertEqual(result.stopped_reason, "reached_end")
        # 1 calibrated delete + 2 blind bursts of 5 = 11 total
        self.assertEqual(result.deleted_count, 11)
        self.assertEqual([call[4] for call in adb.batch_calls], [5, 5])
        # Only 4 dumps total (dump-0..dump-3); burst #2 skipped dumping
        self.assertEqual(adb.dump_index, 4)
        # Audit log disabled -> file should not be created
        self.assertFalse((Path(tmpdir) / "deleted.jsonl").exists())

    def test_refuses_to_start_outside_comment_list_activity(self) -> None:
        adb = FakeAdb()
        adb.get_foreground_activity = lambda: (  # type: ignore[method-assign]
            "com.ss.android.ugc.aweme",
            "com.ss.android.ugc.aweme.splash.SplashActivity",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = CleanerConfig(log_file=Path(tmpdir) / "deleted.jsonl")
            cleaner = CommentCleaner(adb=adb, config=cfg)  # type: ignore[arg-type]
            result = cleaner.run()

        self.assertEqual(result.deleted_count, 0)
        self.assertEqual(result.stopped_reason, "wrong_app_or_activity")
        self.assertEqual(adb.dump_index, 0)
        self.assertEqual(adb.long_presses, [])
        self.assertEqual(adb.batch_calls, [])


if __name__ == "__main__":
    unittest.main()

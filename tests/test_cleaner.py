"""Unit tests for the closed-loop deletion controller and device script parsing."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from douyin_cleaner.burst import parse_burst, parse_probe
from douyin_cleaner.cleaner import CleanerConfig, CommentCleaner
from douyin_cleaner.parser import Bounds, CommentRow, ScreenAnalysis

LIST_ACT = "com.ss.android.ugc.aweme.socialnotice.view.activity.NotificationDetailActivity"
DOUYIN = "com.ss.android.ugc.aweme"


def _row(idx: int, text: str | None = None) -> CommentRow:
    top = 445 + idx * 240
    body = text or f"comment-{idx}"
    return CommentRow(
        bounds=Bounds(0, top, 1080, top + 240),
        tap_point=(540, top + 120),
        texts=[body],
        summary=body,
        fingerprint=f"fp-{body}",
    )


def _screen(
    rows: list[CommentRow] | None = None,
    *,
    delete_button: Bounds | None = None,
    popup: bool = False,
    reached_end: bool = False,
    captcha: str | None = None,
) -> ScreenAnalysis:
    return ScreenAnalysis(
        package=DOUYIN,
        is_lockscreen=False,
        rate_limit_or_captcha=captcha,
        delete_button=delete_button,
        confirm_button=None,
        has_popup_menu=popup or delete_button is not None,
        subtab_sent_comments=None,
        subtab_sent_selected=True,
        comment_rows=rows or [],
        reached_end=reached_end,
    )


DELETE_BTN = Bounds(28, 1130, 1052, 1254)
PROBE_DIALOG = "STATE dialog 28 1066 1052 1318\n"


class FakeAdb:
    """Scripted ADB stub: screens and script outputs are consumed in order."""

    def __init__(self, screens: list[ScreenAnalysis], scripts: list[str]) -> None:
        self.screens = list(screens)
        self.scripts = list(scripts)
        self.foreground: list[tuple[str, str]] = []
        self.events: list[str] = []

    def resolve_device(self) -> str:
        return "SER"

    def get_device_model(self) -> str:
        return "Pixel"

    def get_screen_size(self) -> tuple[int, int]:
        return 1080, 2340

    def is_screen_locked(self) -> bool:
        return False

    def wake_up(self) -> None:
        pass

    def get_foreground_activity(self) -> tuple[str, str]:
        if self.foreground:
            return self.foreground.pop(0)
        return DOUYIN, LIST_ACT

    def dump_hierarchy(self) -> str:
        self.events.append("dump")
        return "xml"

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.events.append(f"long_press {x},{y}")

    def tap(self, x: int, y: int) -> None:
        self.events.append(f"tap {x},{y}")

    def press_back(self) -> None:
        self.events.append("back")

    def swipe(self, *args) -> None:
        self.events.append("swipe")

    def push_text(self, content: str, remote_path: str) -> None:
        self.events.append("push")

    def run_script(self, remote_path: str, args, timeout: float = 30.0) -> str:
        self.events.append(f"script {args[0]}")
        return self.scripts.pop(0)


def _run(adb: FakeAdb, **cfg) -> tuple:
    screens = adb.screens
    with tempfile.TemporaryDirectory() as tmp:
        config = CleanerConfig(log_file=Path(tmp) / "log.jsonl", **cfg)
        cleaner = CommentCleaner(adb=adb, config=config)  # type: ignore[arg-type]
        with (
            patch(
                "douyin_cleaner.cleaner.analyze_screen",
                side_effect=lambda *_: screens.pop(0),
            ),
            patch("douyin_cleaner.cleaner.time.sleep"),
        ):
            result = cleaner.run()
    return result, cleaner


class ParseTests(unittest.TestCase):
    def test_parse_probe(self) -> None:
        state = parse_probe("STATE dialog 28 1066 1052 1318\r\n")
        assert state is not None
        self.assertEqual(state.kind, "dialog")
        self.assertEqual((state.width, state.height), (1024, 252))

    def test_parse_burst_counts_only_verified_ok_lines(self) -> None:
        self.assertEqual(parse_burst("OK\nOK\nDONE\n").deleted, 2)
        self.assertTrue(parse_burst("OK\nOK\nDONE\n").completed)
        fail = parse_burst("OK\nFAIL popup other\n")
        self.assertEqual((fail.deleted, fail.completed, fail.failure), (1, False, "popup:other"))
        cut = parse_burst("OK\nOK\n")
        self.assertEqual((cut.deleted, cut.completed, cut.failure), (2, False, "interrupted"))


class CleanerTests(unittest.TestCase):
    def _rows(self, start: int, n: int = 6) -> list[CommentRow]:
        return [_row(i) for i in range(start, start + n)]

    def test_refuses_to_start_outside_comment_list(self) -> None:
        adb = FakeAdb([], [])
        adb.foreground = [(DOUYIN, "com.ss.android.ugc.aweme.splash.SplashActivity")]
        result, _ = _run(adb)
        self.assertEqual(result.stopped_reason, "wrong_app_or_activity")
        self.assertNotIn("dump", adb.events)

    def test_calibrates_then_bursts_and_counts_verified_deletions(self) -> None:
        screens = [
            _screen(self._rows(0)),  # preflight
            _screen(self._rows(0)),  # loop: top row 0
            _screen(delete_button=DELETE_BTN),  # after long-press
            _screen(self._rows(1)),  # after delete tap (no confirm)
            _screen(self._rows(1)),  # loop: calibrated -> burst
            _screen(self._rows(4)),  # progress verification
            _screen([], reached_end=True),  # loop: end
        ]
        scripts = [PROBE_DIALOG, "OK\nOK\nOK\nFAIL popup list\n"]
        adb = FakeAdb(screens, scripts)
        result, cleaner = _run(adb)

        self.assertEqual(result.stopped_reason, "reached_end")
        self.assertEqual(result.deleted_count, 4)  # 1 verified + 3 burst OK lines
        # Failure with progress keeps calibration for the next burst
        self.assertIsNotNone(cleaner.calibration)

    def test_burst_failure_without_progress_drops_calibration(self) -> None:
        screens = [
            _screen(self._rows(0)),
            _screen(self._rows(0)),
            _screen(delete_button=DELETE_BTN),
            _screen(self._rows(1)),
            _screen(self._rows(1)),  # burst attempt fails immediately
            _screen([], reached_end=True),
        ]
        adb = FakeAdb(screens, [PROBE_DIALOG, "FAIL popup list\n"])
        result, cleaner = _run(adb)
        self.assertEqual(result.deleted_count, 1)
        self.assertIsNone(cleaner.calibration)

    def test_drift_recovery_presses_back_at_most_once(self) -> None:
        detail = (DOUYIN, "com.ss.android.ugc.aweme.detail.ui.DetailActivity")
        screens = [_screen(self._rows(0))]
        adb = FakeAdb(screens, [])
        # preflight ok, then loop sees detail page twice (before and after one BACK)
        adb.foreground = [(DOUYIN, LIST_ACT), detail, detail]
        result, _ = _run(adb)
        self.assertEqual(result.stopped_reason, "lost_comment_list")
        self.assertEqual(adb.events.count("back"), 1)

    def test_no_burst_when_menu_is_not_a_separate_window(self) -> None:
        screens = [
            _screen(self._rows(0)),
            _screen(self._rows(0)),
            _screen(delete_button=DELETE_BTN),
            _screen(self._rows(1)),
            _screen([], reached_end=True),
        ]
        adb = FakeAdb(screens, ["STATE list 0 0 1080 2340\n"])
        result, cleaner = _run(adb)
        self.assertEqual(result.deleted_count, 1)
        self.assertFalse(cleaner.burst_supported)
        self.assertNotIn("script burst", adb.events)

    def test_filters_use_verified_path_only(self) -> None:
        keep = _row(0, "keep-me")
        drop = _row(1, "drop-me")
        screens = [
            _screen([keep, drop]),
            _screen([keep, drop]),
            _screen(delete_button=DELETE_BTN),
            _screen([keep]),
            _screen([keep], reached_end=True),
        ]
        adb = FakeAdb(screens, [])
        result, _ = _run(adb, exclude="keep")
        self.assertEqual(result.deleted_count, 1)
        self.assertNotIn("push", adb.events)
        self.assertIn(f"long_press {drop.tap_point[0]},{drop.tap_point[1]}", adb.events)

    def test_stops_when_bursts_make_no_progress(self) -> None:
        rows = self._rows(0)
        screens = [
            _screen(rows),
            _screen(rows),
            _screen(delete_button=DELETE_BTN),
            _screen(self._rows(1)),
            _screen(self._rows(1)),  # burst #1
            _screen(self._rows(1)),  # verify: unchanged -> recalibrate
            _screen(self._rows(1)),  # loop: verified delete (recalibration)
            _screen(delete_button=DELETE_BTN),
            _screen(self._rows(1)),  # delete did not take effect
            _screen(self._rows(1)),  # burst #2
            _screen(self._rows(1)),  # verify: unchanged again -> stop
        ]
        scripts = [PROBE_DIALOG, "OK\nOK\nFAIL popup list\n", PROBE_DIALOG, "OK\nFAIL popup list\n"]
        adb = FakeAdb(screens, scripts)
        result, _ = _run(adb)
        self.assertEqual(result.stopped_reason, "no_progress")

    def test_captcha_stops_run(self) -> None:
        screens = [_screen(self._rows(0)), _screen(self._rows(0), captcha="请完成验证")]
        result, _ = _run(FakeAdb(screens, []))
        self.assertEqual(result.stopped_reason, "rate_limit_or_captcha")


if __name__ == "__main__":
    unittest.main()

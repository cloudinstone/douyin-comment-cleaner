"""Command-line interface for douyin-comment-cleaner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .adb import AdbClient, AdbError
from .cleaner import DOUYIN_PACKAGE, CleanerConfig, CommentCleaner
from .parser import analyze_screen


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin-comment-cleaner",
        description=(
            "Batch-delete outbound Douyin comments via Android ADB UI automation."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    status_p = subparsers.add_parser(
        "status", help="Check connected device, lock state, and Douyin readiness"
    )
    status_p.add_argument("-s", "--serial", help="Target ADB device serial")

    inspect_p = subparsers.add_parser(
        "inspect", help="Dump and analyze the current screen hierarchy"
    )
    inspect_p.add_argument("-s", "--serial", help="Target ADB device serial")
    inspect_p.add_argument(
        "--save-xml",
        type=Path,
        default=None,
        help="Optional path to save the raw uiautomator XML dump",
    )

    run_p = subparsers.add_parser(
        "run", help="Run batch deletion on the currently open comment list"
    )
    run_p.add_argument("-s", "--serial", help="Target ADB device serial")
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview detected comments and scroll without tapping delete",
    )
    run_p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of comments to delete (0 = unlimited)",
    )
    run_p.add_argument(
        "--min-delay",
        type=float,
        default=0.0,
        help="Minimum random delay in seconds between deletions (default: 0)",
    )
    run_p.add_argument(
        "--max-delay",
        type=float,
        default=0.0,
        help="Maximum random delay in seconds between deletions (default: 0)",
    )
    run_p.add_argument(
        "--long-press-ms",
        type=int,
        default=450,
        help="Long-press duration in milliseconds (default: 450)",
    )
    run_p.add_argument(
        "--no-fast",
        action="store_true",
        help="Disable adaptive fast-path (blind tap) deletion",
    )
    run_p.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help=(
            "Max comments to burst-delete per UI dump in fast mode "
            "(default: 5; 0 = all contiguous matching rows on screen, 1 = verify every item)"
        ),
    )
    run_p.add_argument(
        "--max-empty-scrolls",
        type=int,
        default=5,
        help="Stop after this many scrolls with no new comments (default: 5)",
    )
    run_p.add_argument(
        "--contains",
        default=None,
        help="Only delete comments whose row text contains this substring",
    )
    run_p.add_argument(
        "--exclude",
        default=None,
        help="Skip comments whose row text contains this substring",
    )
    run_p.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/deleted_comments.jsonl"),
        help="JSONL audit log output path (default: logs/deleted_comments.jsonl)",
    )

    return parser


def _cmd_status(serial: str | None) -> int:
    adb = AdbClient(serial=serial)
    resolved = adb.resolve_device()
    model = adb.get_device_model()
    width, height = adb.get_screen_size()
    locked = adb.is_screen_locked()
    pkg, act = adb.get_foreground_activity()

    in_douyin = pkg == DOUYIN_PACKAGE
    in_comment_list = in_douyin and (
        "NotificationDetailActivity" in act or "Comment" in act
    )

    print(f"Device:       {model} ({resolved})")
    print(f"Resolution:   {width}x{height}")
    print(f"Screen Lock:  {'LOCKED' if locked else 'UNLOCKED'}")
    print(f"Foreground:   {pkg or '<unknown>'}/{act or '<unknown>'}")
    if locked:
        print("Ready State:  NO (unlock device first)")
    elif in_comment_list:
        print("Ready State:  YES (Douyin comment/notification list is active)")
    elif in_douyin:
        print("Ready State:  PARTIAL (Douyin is open; navigate to '发出的评论' list)")
    else:
        print("Ready State:  NO (open Douyin and navigate to '发出的评论')")
    return 0


def _cmd_inspect(serial: str | None, save_xml: Path | None) -> int:
    adb = AdbClient(serial=serial)
    resolved = adb.resolve_device()
    model = adb.get_device_model()
    width, height = adb.get_screen_size()

    if adb.is_screen_locked():
        print(
            f"Device {model} ({resolved}) is locked. Unlock the screen before inspecting.",
            file=sys.stderr,
        )
        return 2

    xml_str = adb.dump_hierarchy()
    if save_xml is not None:
        save_xml.parent.mkdir(parents=True, exist_ok=True)
        save_xml.write_text(xml_str, encoding="utf-8")
        print(f"Saved XML dump to {save_xml}")

    analysis = analyze_screen(xml_str, width, height)
    print(f"Device:         {model} ({resolved}) [{width}x{height}]")
    print(f"Package:        {analysis.package or '<unknown>'}")
    print(f"Lockscreen:     {analysis.is_lockscreen}")
    print(f"Sent Subtab:    {analysis.subtab_sent_selected}")
    print(f"Popup Menu:     {analysis.has_popup_menu}")
    print(
        f"Delete Button:  "
        f"{analysis.delete_button.format() if analysis.delete_button else None}"
    )
    print(
        f"Confirm Button: "
        f"{analysis.confirm_button.format() if analysis.confirm_button else None}"
    )
    print(f"Rate/Captcha:   {analysis.rate_limit_or_captcha}")
    print(f"Reached End:    {analysis.reached_end}")
    print(f"Detected Rows:  {len(analysis.comment_rows)}")
    for idx, row in enumerate(analysis.comment_rows, start=1):
        print(
            f"  [{idx}] bounds={row.bounds.format()} tap={row.tap_point} "
            f"text={row.summary}"
        )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.min_delay < 0 or args.max_delay < 0:
        print("Error: --min-delay and --max-delay must be >= 0.", file=sys.stderr)
        return 2
    if args.limit < 0:
        print("Error: --limit must be >= 0.", file=sys.stderr)
        return 2
    if args.batch_size < 0:
        print("Error: --batch-size must be >= 0.", file=sys.stderr)
        return 2

    adb = AdbClient(serial=args.serial)
    config = CleanerConfig(
        limit=args.limit,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        long_press_ms=args.long_press_ms,
        max_empty_scrolls=args.max_empty_scrolls,
        dry_run=args.dry_run,
        fast=not args.no_fast,
        batch_size=args.batch_size,
        contains=args.contains,
        exclude=args.exclude,
        log_file=args.log_file,
    )
    cleaner = CommentCleaner(adb=adb, config=config)
    try:
        result = cleaner.run()
    except KeyboardInterrupt:
        print(
            f"\n[INTERRUPTED] Deleted {cleaner.deleted_count} comment(s), "
            f"skipped {len(cleaner.skipped_fingerprints)}."
        )
        return 130

    print(
        f"[SUMMARY] deleted={result.deleted_count}, "
        f"skipped={result.skipped_count}, reason={result.stopped_reason}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return _cmd_status(args.serial)
        if args.command == "inspect":
            return _cmd_inspect(args.serial, args.save_xml)
        if args.command == "run":
            return _cmd_run(args)
    except AdbError as exc:
        print(f"ADB Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Douyin Comment Cleaner

[简体中文](README.zh-CN.md) | English

A zero-dependency CLI that bulk-deletes the comments **you have posted** on Douyin (抖音), the Chinese version of TikTok.

Douyin has no "select all / delete all" option for your own comment history. You have to long-press each comment and tap delete, one at a time. This tool does those taps for you on a connected Android phone, using ADB and the Android UI hierarchy dump. It does not use private APIs, reverse-engineered request signing, or a companion app.

> [!WARNING]
> Deletion is **irreversible**. This project is not affiliated with Douyin or ByteDance. Automating the app may conflict with the platform's terms of service. Use it on your own account and at your own risk. See [Disclaimer](#disclaimer).

## Features

- **Pure Python standard library.** Needs only `adb` and Python 3.10+. Nothing is installed on the phone.
- **UI-hierarchy based.** Comment rows and the delete menu are found by reading the screen structure, not fixed coordinates, so it works across screen sizes.
- **Safety stops.** It stops when it sees a rate-limit or captcha prompt, when the screen locks, when the wrong app is in front, or when the "发出的评论" (sent comments) tab is not selected.
- **Self-recovery.** If a tap accidentally opens a video or profile page, it presses BACK and continues.
- **Skips undeletable items.** Comments with no delete option (for example, when the original video was removed) are skipped instead of looping forever.
- **Filters and limits.** `--contains`, `--exclude`, `--limit` and `--dry-run`.
- **Audit log.** Every deleted comment is appended to a local JSONL file.

## Requirements

- A computer with [`adb`](https://developer.android.com/tools/adb) (Android platform-tools) and Python >= 3.10
  - macOS: `brew install --cask android-platform-tools`
- An Android phone or emulator with **USB debugging** enabled and the Douyin app signed in
- `adb devices` must list the device as `device` (not `unauthorized`)

iOS is not supported. iOS does not allow this kind of cross-app UI automation. If your account is on an iPhone, sign in to the same account in an Android emulator.

## Quick start

1. On the phone, open Douyin and go to your sent-comments list:
   - **消息 (Messages)** → **互动消息 (Interactions)** → top dropdown **全部消息 ▾** → **发出的评论 (Sent comments)**, or
   - **我 (Me)** → **≡** → **我的评论 (My comments)**
2. Run from the repository root (no installation needed):

```sh
git clone https://github.com/cloudinstone/douyin-comment-cleaner.git
cd douyin-comment-cleaner

./douyin-comment-cleaner status          # device, lock state, is Douyin ready?
./douyin-comment-cleaner inspect         # show detected comment rows (read-only)
./douyin-comment-cleaner run --dry-run   # preview, no taps
./douyin-comment-cleaner run --limit 1   # delete one comment, then check it is gone
./douyin-comment-cleaner run             # delete everything
```

Optional: install it as a global command.

```sh
uv tool install .      # or: pipx install .
douyin-comment-cleaner status
```

Press `Ctrl+C` at any time to stop.

## Usage

```
douyin-comment-cleaner status  [-s SERIAL]
douyin-comment-cleaner inspect [-s SERIAL] [--save-xml PATH]
douyin-comment-cleaner run     [-s SERIAL] [options]
```

| Option | Default | Description |
| --- | --- | --- |
| `-s, --serial` | auto | ADB device serial. Required if more than one device is connected. |
| `--dry-run` | off | Detect rows and scroll, but never long-press or delete. |
| `--limit N` | `0` | Stop after N deletions. `0` means no limit. |
| `--min-delay` / `--max-delay` | `0` / `0` | Random pause in seconds between deletions. Raise these if Douyin starts rate-limiting you. |
| `--long-press-ms` | `600` | Long-press duration in milliseconds. |
| `--max-empty-scrolls` | `5` | Stop after this many scrolls that reveal no new comments. |
| `--contains TEXT` | – | Only delete comments whose row text contains `TEXT`. |
| `--exclude TEXT` | – | Never delete comments whose row text contains `TEXT`. |
| `--log-file PATH` | `logs/deleted_comments.jsonl` | Where to append the audit log. |

## How it works

Each deletion goes through this loop:

1. `adb exec-out uiautomator dump` reads the current UI tree.
2. The parser finds comment rows, menus, confirmation dialogs, captcha or rate-limit prompts, and the end of the list.
3. It long-presses the first eligible row at the horizontal center, away from the avatar and the video thumbnail.
4. It dumps the UI again, taps **删除 (Delete)**, and handles a confirmation dialog if one appears.
5. When no eligible rows are left on screen, it scrolls and repeats.

**Speed.** Each `uiautomator dump` takes about 2 s on a typical phone, and each deletion needs two dumps. That works out to roughly **5–7 seconds per comment**, set by ADB rather than by an artificial delay.

## Tested on

| Device | Android | Douyin |
| --- | --- | --- |
| Pixel 7 Pro | 17 | 40.4.0 |

Douyin changes its UI often. If detection breaks on your version, run `./douyin-comment-cleaner inspect --save-xml dump.xml` and open an issue with the output. Remove any personal content from the XML before attaching it.

## Development

```sh
python3 -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This software is provided "as is", without warranty of any kind (see [LICENSE](LICENSE)). It is an independent project and is not endorsed by, affiliated with, or supported by Douyin or ByteDance. You are responsible for how you use it, including compliance with the platform's terms of service and any applicable laws. Deleted comments cannot be restored. The authors are not liable for account restrictions, data loss, or any other consequences.

## License

[MIT](LICENSE)

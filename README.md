# Douyin Comment Cleaner

一个零依赖的命令行工具，用于批量删除你在**抖音上发出的评论**。

抖音没有提供"全选 / 一键删除"自己历史评论的功能，只能逐条长按删除。本工具通过 ADB 读取 Android 界面结构并模拟触摸，在已连接的安卓手机上自动完成这些操作。不调用私有接口、不逆向签名、不需要在手机上安装任何辅助 App。

> [!WARNING]
> 删除操作**不可恢复**。本项目与抖音 / 字节跳动无任何关联。自动化操作可能违反平台服务条款，请仅用于你自己的账号并自行承担风险。详见[免责声明](#免责声明)。

## 特性

- **纯 Python 标准库**：只需要 `adb` 和 Python 3.10+，手机端无需安装任何东西。
- **基于界面结构识别**：通过读取屏幕控件树定位评论和删除菜单，而不是写死坐标，适配不同分辨率。
- **安全急停**：检测到"操作太频繁"/验证码、锁屏、前台不是抖音、或未选中「发出的评论」标签时自动停止。
- **自动纠偏**：误触进入视频页或个人主页时，自动按返回键回到列表。
- **跳过不可删条目**：没有删除选项的评论（例如原视频已被删除）会被跳过，不会死循环。
- **过滤与限量**：支持 `--contains`、`--exclude`、`--limit`、`--dry-run`。
- **审计日志**：每条被删除的评论都会追加记录到本地 JSONL 文件（仅保存在本机，含评论原文，不要分享或提交到仓库）。

## 前置要求

- 电脑安装 [`adb`](https://developer.android.com/tools/adb)（Android platform-tools）和 Python >= 3.10
  - macOS：`brew install --cask android-platform-tools`
  - Windows：`winget install Google.PlatformTools`（或从官网解压后将目录加入系统环境变量 `Path`）
- 安卓手机或模拟器，已开启 **USB 调试**，并已登录抖音
- `adb devices` 中设备状态为 `device`（不是 `unauthorized`）

不支持 iOS（iOS 不允许此类跨应用 UI 自动化）。如果账号在 iPhone 上，可以在安卓模拟器中登录同一账号使用。

## 快速开始

1. 在手机上打开抖音，进入"发出的评论"列表：
   - **消息** → **互动消息** → 顶部下拉 **全部消息 ▾** → **发出的评论**，或
   - **我** → 右上角 **≡** → **我的评论**
2. 在项目目录下直接运行（无需安装）：

```sh
git clone https://github.com/cloudinstone/douyin-comment-cleaner.git
cd douyin-comment-cleaner

# macOS / Linux:
./douyin-comment-cleaner status          # 检查设备、锁屏状态、抖音页面是否就绪
./douyin-comment-cleaner inspect         # 查看识别到的评论条目（只读）
./douyin-comment-cleaner run --dry-run   # 演练，不点击
./douyin-comment-cleaner run --limit 1   # 先删 1 条，确认确实被删掉
./douyin-comment-cleaner run             # 全部删除

# Windows（PowerShell / CMD）:
python -m douyin_cleaner.cli status
python -m douyin_cleaner.cli run
```

也可以安装为全局命令：

```sh
uv tool install .      # 或：pipx install .
douyin-comment-cleaner status
```

随时按 `Ctrl+C` 停止。

## 参数说明

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-s, --serial` | 自动 | ADB 设备序列号，连接多台设备时必填 |
| `--dry-run` | 关 | 只识别和滚动，不长按、不删除 |
| `--limit N` | `0` | 删除 N 条后停止，`0` 表示不限 |
| `--min-delay` / `--max-delay` | `0` / `0` | 每次删除之间的随机等待秒数，遇到风控时调高 |
| `--long-press-ms` | `550` | 长按时长（毫秒） |
| `--no-burst` | 关 | 禁用端侧闭环快速删除模式，全程使用逐条 UI 校验（较慢） |
| `--audit-log` | 关 | 记录每条删除的评论原文到 JSONL 文件（会自动切换为逐条 UI 校验模式） |
| `--max-empty-scrolls` | `5` | 连续滚动多少次没有新评论后停止 |
| `--contains TEXT` | – | 只删除包含 `TEXT` 的评论 |
| `--exclude TEXT` | – | 跳过包含 `TEXT` 的评论 |
| `--log-file PATH` | `logs/deleted_comments.jsonl` | 审计日志路径 |

## 工作原理

工具支持两种删除模式：

1. **端侧闭环连续删除（默认推荐）**：
   - 先通过一次界面分析校准长按位置与删除弹窗规格。
   - 推送轻量脚本到手机端，在单个 ADB 会话中快速循环操作。
   - 每次长按与点击前后，实时检查窗口栈（`dumpsys window`），只有确认删除弹窗在最上层时才会点击删除，彻底避免误触或跳入视频页。
   - 每删除一批（默认 30 条）做一次完整的界面滚动与进度比对，确认最上层评论真实减少。
   - 速度可达到 **约 1.3–1.5 秒 / 条**。

2. **逐条校验模式（`--no-burst` 或开启过滤 / 审计时）**：
   - 每次长按和删除均完整 dump 界面控件树。
   - 速度约为 **4–6 秒 / 条**。适合需要精准记录每条评论文本或使用正则/关键词过滤的场景。

遇到未捕获的界面异常时，工具最多尝试按 1 次返回键纠偏；若仍不在评论列表页，会立即停止退出，绝不在其他界面乱点。

## 已测试环境

| 设备 | Android | 抖音版本 |
| --- | --- | --- |
| Pixel 7 Pro | 17 | 40.4.0 |

抖音界面改版频繁。如果在你的版本上识别失败，请运行 `./douyin-comment-cleaner inspect --save-xml dump.xml`，并附上输出提交 issue。附上 XML 前请先删除其中的个人内容。

## 开发

```sh
python3 -m unittest discover -s tests -v
```

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 免责声明

本软件按"原样"提供，不附带任何形式的担保（见 [LICENSE](LICENSE)）。本项目为独立项目，未获得抖音或字节跳动的认可、授权或支持。使用者需自行对使用方式负责，包括遵守平台服务条款及相关法律法规。被删除的评论无法恢复。对于账号受限、数据丢失或其他任何后果，作者不承担责任。

## 许可证

[MIT](LICENSE)

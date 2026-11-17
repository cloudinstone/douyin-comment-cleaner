# Douyin Comment Cleaner

通过 Android ADB 自动化批量清理抖音「发出的评论 / 我的评论」的命令行工具。零第三方 Python 依赖，开箱即用，无需在手机端安装辅助 App 或逆向接口。

## 前置要求

1. **电脑端**：
   - `adb`（macOS 可通过 `brew install --cask android-platform-tools` 安装）
   - Python >= 3.10
2. **手机端（或 Android 模拟器）**：
   - 开启「开发者选项」和「USB 调试」并连接电脑，确保 `adb devices` 显示为 `device`
   - 打开抖音并手动停留在 **发出的评论** 列表页：
     - 路径 A：**消息** $\rightarrow$ **互动消息** $\rightarrow$ 顶部下拉 **全部消息 ▾** $\rightarrow$ **发出的评论**
     - 路径 B：**我** $\rightarrow$ 右上角 **≡** $\rightarrow$ **我的评论**

## 快速使用

无需安装，直接在项目目录运行：

```zsh
# 1. 检查设备连接、锁屏状态及抖音当前页面是否就绪
./douyin-comment-cleaner status

# 2. 检查当前页面识别到的评论条目与点击坐标（不执行任何操作）
./douyin-comment-cleaner inspect

# 3. 演练模式（预览将被操作的条目和滑动逻辑，不实际长按或删除）
./douyin-comment-cleaner run --dry-run

# 4. 小批量试跑（例如先删 5 条）
./douyin-comment-cleaner run --limit 5

# 5. 全量清理（默认无间隔，可用 --min-delay/--max-delay 调整）
./douyin-comment-cleaner run
```

如需全局命令安装，也可使用 `uv` 或 `pipx`：

```zsh
uv tool install -e .
# 之后可在任意目录直接执行：
douyin-comment-cleaner status
```

## 常用参数

- `-s, --serial`: 指定目标设备序列号（多台设备同时连接时必填）
- `--dry-run`: 仅演练识别与滚动，不触发真实触摸删除
- `--limit N`: 最多删除 N 条后自动停止（`0` 表示不限制）
- `--min-delay SEC` / `--max-delay SEC`: 每次删除之间的随机等待区间（默认 `0`，若遇风控再调高）
- `--long-press-ms MS`: 长按时长（默认 `600` ms）
- `--contains TEXT`: 仅删除行文本包含指定关键词的评论
- `--exclude TEXT`: 跳过行文本包含指定关键词的评论
- `--log-file PATH`: 已删除评论的 JSONL 审计日志路径（默认 `logs/deleted_comments.jsonl`）

## 安全与风控机制

- **精准避让点击区**：自动计算屏幕水平居中（50% 宽度）坐标长按评论文本区域，避开左侧用户头像与右侧视频缩略图。
- **防跑偏自动回退**：若误触跳入视频详情页或个人主页，自动发送 `BACK` 返回列表页。
- **不可删条目自动跳过**：遇到原视频已删除/隐藏导致长按无「删除」菜单的记录，自动记录指纹并跳过，不会卡死循环。
- **风控/验证码急停**：检测到「操作太频繁」「安全验证」「拖动滑块」等提示时立即终止任务。
- **文本过滤**：支持 `--contains` / `--exclude` 按关键词精确清理或保留特定评论。

# 贡献指南

欢迎提交 issue 和 pull request。

## 报告识别问题

抖音界面改版频繁，大部分问题都是解析器无法识别新版界面。提交时请附上：

1. 设备型号、Android 版本和抖音版本（抖音：我 → ≡ → 设置 → 关于抖音）
2. `./douyin-comment-cleaner status` 和 `./douyin-comment-cleaner inspect` 的输出
3. 如果可以，附上用 `./douyin-comment-cleaner inspect --save-xml dump.xml` 导出的界面结构

> [!IMPORTANT]
> 界面 dump 和 `inspect` 输出包含屏幕上的文字，包括昵称和评论内容。提交前请先删除个人信息。

## 开发

除 Python >= 3.10 外没有任何依赖。

```sh
python3 -m unittest discover -s tests -v
```

## 约定

- 运行时只使用 Python 标准库。
- 代码和注释使用英文；`"删除"` 等抖音界面文字保留为字符串常量。
- 遵循 PEP 8。
- 修改解析器时，在 `tests/` 中补充测试，测试数据请使用精简并匿名化的 XML。
- 优先通过文字、控件类名和结构识别界面元素，不要依赖混淆过的 resource-id（每次抖音发版都可能变化）。
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `fix(parser): detect new delete menu layout`。

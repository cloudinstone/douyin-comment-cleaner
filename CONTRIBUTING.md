# Contributing

Thanks for your interest. Bug reports and pull requests are welcome.

## Reporting a detection problem

Douyin changes its UI often, so most bugs come from the parser no longer recognizing a screen. Please include:

1. Your device model, Android version and Douyin version (Douyin: 我 → ≡ → 设置 → 关于抖音)
2. The output of `./douyin-comment-cleaner status` and `./douyin-comment-cleaner inspect`
3. If possible, a UI dump captured with `./douyin-comment-cleaner inspect --save-xml dump.xml`

> [!IMPORTANT]
> UI dumps contain the text on your screen, including nicknames and comment content. Redact personal data before attaching them.

## Development setup

No dependencies are required beyond Python >= 3.10.

```sh
python3 -m unittest discover -s tests -v
```

## Guidelines

- Use only the Python standard library at runtime.
- Write code and comments in English. Douyin UI strings such as `"删除"` stay as literals.
- Follow PEP 8.
- Add a test in `tests/` for parser changes, preferably using a small, anonymized XML fixture.
- Prefer detecting UI elements by text, class and structure over obfuscated resource IDs, which change between Douyin releases.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages, for example `fix(parser): detect new delete menu layout`.

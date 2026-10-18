# Third-party notices / 第三方声明

## 源码 / Source

`cc_sessionsync` 的依赖：`tomlkit`（MIT，改 Codex 的 config.toml 时保留注释和格式）；`zstandard`（BSD-3-Clause，0.2.5 起，读写 DeepSeek Harness 的
zstd 会话文件）；桌面窗口可选 `pywebview`（BSD-3-Clause）。
Dependencies: `tomlkit` (MIT, round-trips Codex `config.toml`); `zstandard` (BSD-3-Clause, since 0.2.5, reads/writes DeepSeek Harness zstd
session logs); optional `pywebview` (BSD-3-Clause) for the desktop window.

### DeepSeek Harness（0.2.5）

| 项目 / Project | 许可 / License | 用在哪 / What is used |
|---|---|---|
| [DeepSeek Harness (dsh)](https://github.com/deepseek-ai/deepseek-harness) — Copyright (c) 2026 DeepSeek | MIT (`licenses/deepseek-harness-MIT.txt`) | 会话记录格式（第 4 版 `session.v4.jsonl[.zstd]`：头一行、事件、`surfaceOp`、独立带校验和的 zstd 帧、目录名编码 `--projectKey--/encodeSegment(id)`、`session.lock`、附件库路径）和事件类型清单（`docs/persistence-catalog.md`），按它公开的源码和文档在 `dsh.py` 里重写读 / 写 / 校验；没有拷贝它的代码。CI 和 `tools/verify_dsh.py` 用 npm 上的 `@deepseek-ai/dsh` 命令行（临时 DSH_HOME + 本机假模型）验证导进去的会话能接着聊；它不随本项目分发 / the v4 session-log format and event-type catalog, re-implemented in `dsh.py` from the public source and docs (no code copied); the npm CLI is used only in CI / `tools/verify_dsh.py` against a local mock model and is not distributed |

"DeepSeek" / "DeepSeek Harness" 是 DeepSeek 的商标；本项目和它没有关系。界面里 dsh 的图标是自己画的鲸鱼轮廓，不是官方 logo。
"DeepSeek" and "DeepSeek Harness" are trademarks of DeepSeek; this project is not affiliated with it. The whale glyph used for dsh in the UI is an original drawing, not the official logo.

### 从 CC Msg 搬来的代码 / Code ported from CC Msg

0.2.0 的桌面壳、托盘、检查更新、用量统计、模型与 API、凭据库、界面样式是从同一作者的 [CC Msg](https://github.com/helefee/CCMessage)
（MIT）复制过来再改的（CC Msg 仓库本身没动）。详见 README「哪些是搬来的」。
The 0.2.0 desktop shell, tray, updater, usage stats, Model & API page, keystore and UI styling are copied from the same author's
CC Msg (MIT) and adapted; see "What was ported" in the README.

| 项目 / Project | 许可 / License | 用在哪 / What is used |
|---|---|---|
| [cc-switch](https://github.com/farion1231/cc-switch) — Copyright (c) 2025 Jason Young | MIT (`licenses/cc-switch-MIT.txt`) | 模型价格种子表（`usage_prices.json`）、导入 CC Switch 用量时的「新输入」口径、用量页布局、供应商预设和只读导入（不拷代码）、Codex 自定义供应商的 `model_catalog_json` 思路（本程序写自己的 `ccss-models-<id>.json`） / seed price table, usage page layout, provider presets and read-only import, the model-catalog idea |
| [magpie](https://github.com/yetone/magpie) — Copyright (c) 2026 yetone | MIT (`licenses/magpie-MIT.txt`) | 会话日志用量去重规则（Claude `message.id` + `requestId`；Codex 累计 / 单轮 `token_count`、重置分段、归档去重），在 `usage.py` 里重写 / usage dedup rules, re-implemented in `usage.py` |
| [opencodex](https://github.com/lidge-jun/opencodex) — Copyright (c) 2026 opencodex contributors | MIT (`licenses/opencodex-MIT.txt`) | 「模型与 API → 代理」里探测本地网关（缺省端口 10100）和读它的 config.json / proxy detection on the Model & API page |
| [Inter](https://rsms.me/inter/) | SIL OFL 1.1 (`cc_sessionsync/fonts/Inter-OFL.txt`) | 界面字体子集 / UI font subset |
| [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | SIL OFL 1.1 (`cc_sessionsync/fonts/JetBrainsMono-OFL.txt`) | 等宽字体子集 / monospace font subset |
| [Lucide](https://lucide.dev) | ISC | 界面里的 SVG 图标（内联在 `index.html`） / SVG icons inlined in `index.html` |

- LevelDB 表文件里的 Snappy 解压（`claude_sync._unsnappy`）是按公开的 Snappy 格式说明自己写的，没有拷贝 snappy 的代码。
  The Snappy block decoder in `claude_sync._unsnappy` is an independent implementation of the public Snappy format description.
- Codex 对话格式（rollout `*.jsonl`）按 OpenAI Codex 开源仓库（Apache-2.0，https://github.com/openai/codex ，`codex-rs/protocol`）
  里公开的结构体定义来读；没有拷贝它的代码。
  The Codex rollout format is read according to the public type definitions in the OpenAI Codex repository (Apache-2.0); no code is copied.
- Claude Code 会话文件格式和项目目录命名规则（`projects/<路径换成 ->`，超过 200 字符加哈希）是对照本机 Claude Code 生成的文件观察得来的，
  不是官方公开格式，Claude Code 升级可能改变。
  The Claude Code transcript layout and project-folder naming were derived by observing files written by Claude Code; they are not a
  documented public format and may change.

"Claude" / "Claude Code" 是 Anthropic 的商标，"Codex" / "ChatGPT" 是 OpenAI 的商标；本项目和它们没有关系。
"Claude" and "Claude Code" are trademarks of Anthropic; "Codex" and "ChatGPT" are trademarks of OpenAI. This project is not affiliated with either.

## Windows 安装版 / Windows installer

发布页上的 `CCSessionSync-Setup-*.exe` 用 Inno Setup 打包（Inno Setup License），里面的 `CCSessionSync.exe` 用 Nuitka 编译，带了：
`CCSessionSync-Setup-*.exe` is built with Inno Setup; the `CCSessionSync.exe` inside is compiled with Nuitka and contains:

- **CPython** 运行时和标准库 — Python Software Foundation License Version 2（https://docs.python.org/3/license.html）
- **Nuitka** 运行时辅助代码 — Apache License 2.0（https://github.com/Nuitka/Nuitka/blob/develop/LICENSE.txt）；
  Nuitka 对它编译出来的程序里的这部分代码不额外施加限制。
- **pywebview** — BSD-3-Clause（https://github.com/r0x0r/pywebview）；Windows 上通过它用系统自带的 Microsoft Edge WebView2 显示界面。
- **pythonnet**（pywebview 在 Windows 上的依赖）— MIT；**tomlkit** — MIT；**zstandard**（python-zstandard，带 zstd 库）— BSD-3-Clause
  （https://github.com/indygreg/python-zstandard ；zstd 本身 BSD-3-Clause / GPLv2 双许可，这里按 BSD 用）。
- 安装程序的中文界面文字 `installer/ChineseSimplified.isl` 来自 Inno Setup 的非官方翻译（随 Inno Setup 许可）。

## 测试工具 / Test tooling

CI 里会用 npm 装 `@anthropic-ai/claude-code`，只用来验证导入的会话能被 `claude --resume` 打开（连本机的假 API，见 `tools/mock_anthropic.py`）；
它不随本项目分发。
CI installs `@anthropic-ai/claude-code` from npm only to verify that imported sessions open with `claude --resume` against a local mock API;
it is not distributed with this project.
CI 也会用 npm 装 `@deepseek-ai/dsh`（MIT）命令行，只用来验证从 Claude Code 导进 DeepSeek Harness 的会话能接着聊（临时 DSH_HOME，连本机的假模型
`tools/mock_openai.py`），不随本项目分发。
CI also installs the `@deepseek-ai/dsh` CLI (MIT) only to verify that sessions imported into DeepSeek Harness can be resumed (scratch DSH_HOME,
local mock model `tools/mock_openai.py`); it is not distributed with this project.

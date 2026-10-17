# Third-party notices / 第三方声明

## 源码 / Source

`cc_sessionsync` 只用 Python 标准库，没有打包任何第三方 Python 库的代码。
The `cc_sessionsync` package uses only the Python standard library and vendors no third-party code.

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

## Windows exe

发布页上的 `cc-sessionsync-*-windows-x64.exe` 是用 Nuitka 编译的，里面带了：
The Windows executable on the release page is built with Nuitka and contains:

- **CPython** 运行时和标准库 — Python Software Foundation License Version 2（https://docs.python.org/3/license.html）
- **Nuitka** 运行时辅助代码 — Apache License 2.0（https://github.com/Nuitka/Nuitka/blob/develop/LICENSE.txt）；
  Nuitka 对它编译出来的程序里的这部分代码不额外施加限制。

## 测试工具 / Test tooling

CI 里会用 npm 装 `@anthropic-ai/claude-code`，只用来验证导入的会话能被 `claude --resume` 打开（连本机的假 API，见 `tools/mock_anthropic.py`）；
它不随本项目分发。
CI installs `@anthropic-ai/claude-code` from npm only to verify that imported sessions open with `claude --resume` against a local mock API;
it is not distributed with this project.

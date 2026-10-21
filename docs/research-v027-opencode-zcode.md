# v0.2.7 调研笔记：OpenCode 与 ZCode 的会话存储（只看结构 / 数量，不含任何会话内容或密钥）

## OpenCode（sst/opencode，开源终端 / 桌面 AI 编码代理）
- 数据目录：`$XDG_DATA_HOME/opencode`，缺省 `~/.local/share/opencode`；Windows 上同样是 `%USERPROFILE%\.local\share\opencode`（不是 %APPDATA%）。
- 会话库：`opencode.db`（SQLite，WAL 模式）。旧版的 `storage/` JSON 记录只计数，不读。
- 1.x 表：`session` / `message` / `part`（message.data、part.data 是 JSON：text、reasoning、tool、file、step-*）。
- 2.x 表：`session_v2` / `session_message`（type = user / assistant / idle…，data JSON 里 content[] 带 text / reasoning / tool）。同一个库里两套表可以并存。
- 同一个库里还有 `account` / `credential` 表：本程序**从不读**。桌面版的 `service.json` 也不读。
- 用户 PC：OpenCode 桌面版 2.0.32（`%LOCALAPPDATA%\Programs\@opencodedesktop`），库里只有 2.x 表，1 个会话；命令行随桌面版打包（`resources\opencode-cli.exe`，以及 `%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe`）。
- 写入方式：只通过 OpenCode 自己的命令行导入新会话：
  - 2.x：`opencode session import --standalone [--directory cwd] file` / `session list --standalone --format json` / `session delete --standalone id` / `run --standalone -s id …`
  - 1.x：`opencode import file` / `session list --format json` / `session delete id` / `run -s id …`
  - 命令行要在 PWD = 会话目录、stdin = /dev/null 下跑（1.x 不然会卡住）。
- 结论：**原生目标**（找得到命令行时；否则退回导出上下文文件）。

## ZCode（智谱 Z.ai 的 GLM 编码代理桌面 IDE，Electron，闭源）
- 安装：`C:\Program Files\ZCode`（用户 PC 上 3.16.2）。引擎 zcode-cli 是 OpenCode 1.x 的分支。
- 主目录：`$ZCODE_HOME`，缺省 `~/.zcode`。
- 会话库：`cli/db/db.sqlite`：`session` / `message` / `part`（多了 sequence 列、session.task_type（interactive / subagent_child）、time_deleted）。
  message.data.semantics.kind：user_prompt / assistant_response / todo_reminder / background_notification / timeline_event / subagent_notification / system_reminder —— 只取前两种。
- 桌面端任务索引：`v2/tasks-index.sqlite` 的 `tasks`（task_id = 会话号、title、title_overridden、archived、deleted、workspace_path），按会话号合并标题 / 删除标记。
- `credentials.json` 等：从不读。
- 用户 PC 数量：29 个会话（21 interactive、8 subagent_child），599 条消息，2467 个 part，24 个任务。
- 结论：**只导出**（闭源；两个库要一起改，还有迁移记录；没办法验证写进去的会话能接着聊）。

## 读取方式（两者共用）
把 db 和 -wal 复制到临时目录（`ccss-oc-*`）再打开副本，读完删掉；按 db / wal 的 mtime + 大小缓存。应用开着也能读，不加锁、不写。

## PC 上的调研
只读脚本，只打印键名 / 表结构 / 数量。留在 PC 上的临时文件：`C:\Users\Qzen\AppData\Local\Temp\ccss-probe\`（pcprobe.py、probe27.py、out27.txt、asar27.py、asar27.txt），删除要用户同意。

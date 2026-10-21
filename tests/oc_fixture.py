# -*- coding: utf-8 -*-
"""造 OpenCode（1.x 的 session/message/part、2.x 的 session_v2/session_message）和 ZCode（cli/db/db.sqlite + v2/tasks-index.sqlite）的库。
表结构照着真库（OpenCode 1.19.3 / 2.1.1 在临时目录里跑出来的、ZCode 3.16.2 的列）抄的，只造测试用的几行。
账号 / 凭据表里放一个 SECRET 标记：读的时候绝不能碰到它。"""
from __future__ import annotations

import json
import random
import sqlite3
import string
from pathlib import Path

SECRET = "SECRET-OC-TOKEN-MARKER"

PROJECT = """CREATE TABLE `project` (`id` text PRIMARY KEY, `worktree` text NOT NULL, `vcs` text, `name` text, `icon_url` text,
 `icon_url_override` text, `icon_color` text, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `time_initialized` integer,
 `time_active` integer DEFAULT 0 NOT NULL, `time_archived` integer, `sandboxes` text NOT NULL, `commands` text)"""
V1 = ["""CREATE TABLE `session` (`id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text, `slug` text NOT NULL,
 `directory` text NOT NULL, `path` text, `title` text NOT NULL, `version` text NOT NULL, `share_url` text, `summary_additions` integer,
 `summary_deletions` integer, `summary_files` integer, `summary_diffs` text, `metadata` text, `cost` real DEFAULT 0 NOT NULL,
 `tokens_input` integer DEFAULT 0 NOT NULL, `tokens_output` integer DEFAULT 0 NOT NULL, `tokens_reasoning` integer DEFAULT 0 NOT NULL,
 `tokens_cache_read` integer DEFAULT 0 NOT NULL, `tokens_cache_write` integer DEFAULT 0 NOT NULL, `revert` text, `permission` text,
 `agent` text, `model` text, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `time_compacting` integer, `time_archived` integer)""",
      "CREATE TABLE `message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL)",
      "CREATE TABLE `part` (`id` text PRIMARY KEY, `message_id` text NOT NULL, `session_id` text NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL)"]
V2 = ["""CREATE TABLE `session_v2` (`id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text, `fork_session_id` text,
 `fork_boundary` text, `slug` text NOT NULL, `directory` text NOT NULL, `path` text, `title` text, `version` text NOT NULL, `share_url` text,
 `summary_additions` integer, `summary_deletions` integer, `summary_files` integer, `summary_diffs` text, `metadata` text, `cost` real DEFAULT 0 NOT NULL,
 `tokens_input` integer DEFAULT 0 NOT NULL, `tokens_output` integer DEFAULT 0 NOT NULL, `tokens_reasoning` integer DEFAULT 0 NOT NULL,
 `tokens_cache_read` integer DEFAULT 0 NOT NULL, `tokens_cache_write` integer DEFAULT 0 NOT NULL, `revert` text, `permission` text, `agent` text,
 `model` text, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `time_idle` integer, `time_viewed` integer, `idle_outcome` text,
 `time_compacting` integer, `time_archived` integer, `time_suspended` integer, `resume_attempts` integer DEFAULT 0 NOT NULL)""",
      "CREATE TABLE `session_message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `type` text NOT NULL, `seq` integer NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL)"]
SECRETS = ["CREATE TABLE `account` (`id` text, `email` text, `url` text, `access_token` text, `refresh_token` text, `token_expiry` integer, `time_created` integer, `time_updated` integer)",
           "CREATE TABLE `credential` (`id` text, `integration_id` text, `label` text, `value` text, `connector_id` text, `method_id` text, `active` integer, `time_created` integer, `time_updated` integer)"]
ZC = ["""CREATE TABLE `session` (`id` text PRIMARY KEY, `project_id` text NOT NULL, `workspace_id` text, `parent_id` text, `slug` text NOT NULL,
 `directory` text NOT NULL, `path` text, `title` text NOT NULL, `version` text NOT NULL, `share_url` text, `summary_additions` integer,
 `summary_deletions` integer, `summary_files` integer, `summary_diffs` text, `revert` text, `permission` text, `time_created` integer NOT NULL,
 `time_updated` integer NOT NULL, `time_compacting` integer, `time_archived` integer, `task_type` text, `title_source` text, `title_message_id` text,
 `time_title_updated` integer, `trace_id` text, `time_deleted` integer, `last_outcome` text)""",
      "CREATE TABLE `message` (`id` text PRIMARY KEY, `session_id` text NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL, `sequence` integer)",
      "CREATE TABLE `part` (`id` text PRIMARY KEY, `message_id` text NOT NULL, `session_id` text NOT NULL, `time_created` integer NOT NULL, `time_updated` integer NOT NULL, `data` text NOT NULL, `sequence` integer)",
      "CREATE TABLE `model_usage` (`id` text, `session_id` text, `provider_id` text, `model_id` text)",
      "CREATE TABLE `zcode_database_identity` (`scope` text, `identity` text)"]
TASKS = """CREATE TABLE `tasks` (`workspace_key` text, `workspace_path` text, `workspace_identity` text, `task_id` text, `title` text,
 `task_status` text, `provider` text, `mode` text, `model` text, `migration_source` text, `forked_from_task_id` text, `created_at` integer,
 `updated_at` integer, `unread_at` integer, `last_unread_at` integer, `pinned` integer, `archived` integer, `deleted` integer,
 `title_overridden` integer, `meta_json` text, `searchable_text` text)"""


def _rid(p, ms, n, desc=False):
    v = ms * 0x1000 + n
    if desc:
        v = (~v) & 0xFFFFFFFFFFFF
    return f"{p}_{v & 0xFFFFFFFFFFFF:012x}" + "".join(random.choice(string.ascii_letters) for _ in range(14))


def _db(f: Path, ddl: list[str], wal: bool = True) -> sqlite3.Connection:
    f.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(f))
    if wal:
        c.execute("pragma journal_mode=wal")
    for s in ddl:
        c.execute(s)
    return c


def opencode(data: Path, cwd: str, v1: bool = True, v2: bool = True, t0: int = 1792500000000) -> dict:
    """data = …/opencode（数据目录）。→ {"v1": 会话号, "v2": 会话号, "v2_sub": 子会话号}。库开着 WAL、留一部分在 -wal 里。"""
    f = data / "opencode.db"
    c = _db(f, [PROJECT] + SECRETS + (V1 if v1 else []) + (V2 if v2 else []))
    c.execute("insert into project values ('global', '/', null, null, null, null, null, ?, ?, null, 0, null, '[]', null)", (t0, t0))
    c.execute("insert into credential values ('c1', 'opencode', 'x', ?, null, null, 1, 0, 0)", (json.dumps({"access": SECRET}),))
    c.execute("insert into account values ('a1', 'me@example.com', 'u', ?, ?, 0, 0, 0)", (SECRET, SECRET))
    out = {}
    if v1:
        sid = _rid("ses", t0, 0, True)
        out["v1"] = sid
        c.execute("insert into session (id, project_id, slug, directory, title, version, agent, model, time_created, time_updated) "
                  "values (?, 'global', 'kind-orchid', ?, 'OC1 修复构建', '1.19.3', 'build', ?, ?, ?)",
                  (sid, cwd, json.dumps({"id": "gpt-x", "providerID": "openai"}), t0, t0 + 5000))
        n = 0

        def msg(data, parts, ts):
            nonlocal n
            n += 1
            mid = _rid("msg", t0, n)
            c.execute("insert into message values (?, ?, ?, ?, ?)", (mid, sid, ts, ts, json.dumps(data)))
            for i, p in enumerate(parts):
                c.execute("insert into part values (?, ?, ?, ?, ?, ?)", (_rid("prt", t0, n * 10 + i), mid, sid, ts, ts, json.dumps(p)))
            return mid
        u = msg({"role": "user", "time": {"created": t0}, "agent": "build", "model": {"providerID": "openai", "modelID": "gpt-x"}},
                [{"type": "text", "text": "OC1-MARK 构建挂了，帮我看看"}, {"type": "text", "text": "<system-reminder>x</system-reminder>", "synthetic": True},
                 {"type": "file", "mime": "image/png", "url": "data:image/png;base64,AAAA", "filename": "a.png"}], t0)
        msg({"role": "assistant", "parentID": u, "modelID": "gpt-x", "providerID": "openai", "time": {"created": t0 + 1000, "completed": t0 + 2000}},
            [{"type": "step-start"}, {"type": "reasoning", "text": "OC1-THINK 先看日志"},
             {"type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "completed", "input": {"command": "npm run build"},
                                                                          "output": "error TS2304 OC1-TOOL-OUT", "title": "npm run build"}},
             {"type": "step-finish", "reason": "tool-calls"}], t0 + 1000)
        msg({"role": "assistant", "parentID": u, "modelID": "gpt-x", "providerID": "openai", "time": {"created": t0 + 3000}},
            [{"type": "text", "text": "OC1-ANSWER 少了一个 import，补上了。"}], t0 + 3000)
    if v2:
        sid = _rid("ses", t0 + 10000, 0, True)
        sub = _rid("ses", t0 + 20000, 0, True)
        out["v2"], out["v2_sub"] = sid, sub
        for s, parent, title in ((sid, None, "OC2 写单测"), (sub, sid, "子代理")):
            c.execute("insert into session_v2 (id, project_id, parent_id, slug, directory, title, version, agent, model, time_created, time_updated) "
                      "values (?, 'global', ?, 'brave-fox', ?, ?, '2.0.32', 'build', ?, ?, ?)",
                      (s, parent, cwd, title, json.dumps({"id": "deepseek-v4.1-flash", "providerID": "opencode"}), t0 + 10000, t0 + 15000))
        rows = [("user", {"text": "OC2-MARK 给 utils 写单测", "files": [{"mime": "image/png", "url": "x"}], "time": {"created": t0 + 10000}}),
                ("assistant", {"agent": "build", "model": {"id": "deepseek-v4.1-flash", "providerID": "opencode"}, "time": {"created": t0 + 11000},
                               "content": [{"type": "reasoning", "text": "OC2-THINK"},
                                           {"type": "tool", "id": "call_9", "name": "shell", "state": {"status": "completed", "input": {"command": "pytest -q"},
                                                                                                       "content": [{"type": "text", "text": "2 passed OC2-TOOL-OUT"}]}},
                                           {"type": "text", "text": "OC2-ANSWER 写好了，2 个测试都过了。"}], "finish": "stop"}),
                ("idle", {"outcome": "succeeded", "time": {"created": t0 + 12000}}),
                ("assistant", {"error": {"type": "provider.quota", "message": "quota"}, "time": {"created": t0 + 13000}, "content": []})]
        for i, (t, d) in enumerate(rows):
            c.execute("insert into session_message values (?, ?, ?, ?, ?, ?, ?)", (_rid("msg", t0 + 10000, i + 1), sid, t, i, d["time"]["created"],
                                                                                  d["time"]["created"], json.dumps(d)))
        c.execute("insert into session_message values (?, ?, 'user', 0, ?, ?, ?)", (_rid("msg", t0 + 20000, 1), sub, t0 + 20000, t0 + 20000,
                                                                                  json.dumps({"text": "找一下 utils.py"})))
    c.commit()
    c.close()                    # 关掉会把 WAL 合并回去；再开一个不提交的连接，让 -wal 文件留着（像应用开着时那样）
    return out


def zcode(home: Path, cwd: str, t0: int = 1792600000000) -> dict:
    f = home / "cli" / "db" / "db.sqlite"
    c = _db(f, ZC, wal=False)
    out = {}
    specs = [("main", "interactive", None, "ZC 修登录", None), ("sub", "subagent_child", "main", "子任务", None),
             ("gone", "interactive", None, "删掉的", t0), ("renamed", "interactive", None, "原标题", None)]
    ids = {}
    for k, tt, parent, title, deleted in specs:
        ids[k] = _rid("ses", t0 + len(ids) * 1000, 0, True)
        c.execute("insert into session (id, project_id, parent_id, slug, directory, title, version, permission, time_created, time_updated, task_type, time_deleted) "
                  "values (?, 'p1', ?, 's', ?, ?, '0.17.0', '{\"mode\":\"yolo\"}', ?, ?, ?, ?)",
                  (ids[k], ids.get(parent) if parent else None, cwd, title, t0, t0 + 9000, tt, deleted))
    out.update(ids)
    sid = ids["main"]
    seq = 0

    def msg(data, parts):
        nonlocal seq
        seq += 1
        mid = _rid("msg", t0, seq)
        ts = t0 + seq * 100
        c.execute("insert into message values (?, ?, ?, ?, ?, ?)", (mid, sid, ts, ts, json.dumps(data), seq))
        for i, p in enumerate(parts):
            c.execute("insert into part values (?, ?, ?, ?, ?, ?, ?)", (_rid("prt", t0, seq * 10 + i), mid, sid, ts, ts, json.dumps(p), seq * 10 + i))
    sem = lambda k: {"origin": "user" if k == "user_prompt" else "runtime", "kind": k, "uiVisibility": "visible",
                     "providerVisibility": "visible", "transcriptVisibility": "visible"}
    msg({"role": "user", "time": {"created": t0}, "agent": "zcode-agent", "semantics": sem("user_prompt")}, [{"type": "text", "text": "ZC-MARK 登录按钮没反应"}])
    msg({"role": "user", "time": {"created": t0}, "agent": "zcode-agent", "semantics": sem("todo_reminder")}, [{"type": "text", "text": "ZC-REMINDER", "synthetic": True}])
    msg({"role": "assistant", "time": {"created": t0}, "agent": "zcode-agent", "semantics": sem("assistant_response"), "modelId": "glm-5.3", "providerId": "glm"},
        [{"type": "step-start"}, {"type": "reasoning", "text": "ZC-THINK", "metadata": {"openai": {"reasoningEncryptedContent": "enc"}}},
         {"type": "tool", "tool": "Bash", "callID": "c1", "state": {"status": "completed", "input": {"command": "rg onClick", "description": "找"},
                                                                  "output": "src/Login.tsx:3 ZC-TOOL-OUT", "metadata": {"schemaVersion": 1}}},
         {"type": "step-finish"}])
    msg({"role": "assistant", "time": {"created": t0}, "agent": "zcode-agent", "semantics": sem("assistant_response"), "modelId": "glm-5.3", "providerId": "glm"},
        [{"type": "text", "text": "ZC-ANSWER 绑错了事件，改好了。"}, {"type": "timeline", "timelineType": "model_switch"}])
    msg({"role": "user", "time": {"created": t0}, "agent": "zcode-agent", "semantics": sem("background_notification")}, [{"type": "text", "text": "ZC-NOTIFY"}])
    c.execute("insert into model_usage values ('u1', ?, 'glm', 'glm-5.3')", (sid,))
    c.commit()
    c.close()
    t = _db(home / "v2" / "tasks-index.sqlite", [TASKS], wal=False)
    for k, title, over, deleted in (("main", "ZC 修登录", 0, 0), ("renamed", "用户改的名字", 1, 0)):
        t.execute("insert into tasks (workspace_path, task_id, title, title_overridden, archived, deleted, created_at, updated_at) values (?, ?, ?, ?, 0, ?, ?, ?)",
                  (cwd, ids[k], title, over, deleted, t0, t0))
    t.commit()
    t.close()
    return out


def hold_wal(f: Path) -> sqlite3.Connection:
    """开一个连接写一行不 checkpoint：-wal 里留着没合并的内容（像应用开着时那样）。调用方负责关。"""
    c = sqlite3.connect(str(f))
    c.execute("pragma wal_autocheckpoint=0")
    return c

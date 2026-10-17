# -*- coding: utf-8 -*-
"""造 Codex 的 rollout-*.jsonl（按 codex-rs/protocol 的 RolloutItem / ResponseItem 格式），测试和验证用。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _ts(base: datetime, i: int) -> str:
    return (base + timedelta(seconds=i)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rollout(home: Path, cwd: str, title: str | None = "修登录页的 bug", tid: str | None = None, originator: str = "Codex Desktop",
            thread_source: str | None = "user", archived: bool = False, when: datetime | None = None, extra_turn: bool = False,
            secret_marker: str = "SECRET-CODEX-MARKER") -> tuple[Path, str]:
    tid = tid or str(uuid.uuid4())
    when = when or datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
    d = home / ("archived_sessions" if archived else f"sessions/{when:%Y/%m/%d}")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{when:%Y-%m-%dT%H-%M-%S}-{tid}.jsonl"
    meta = {"id": tid, "timestamp": _ts(when, 0), "cwd": cwd, "originator": originator, "cli_version": "0.130.0",
            "instructions": None, "source": "vscode", "model_provider": "openai",
            "git": {"commit_hash": "abc123", "branch": "fix/login", "repository_url": "git@github.com:x/y.git"}}
    if thread_source is not None:
        meta["thread_source"] = thread_source
    L = [{"type": "session_meta", "payload": meta},
         {"type": "turn_context", "payload": {"cwd": cwd, "approval_policy": "on-request", "model": "gpt-5.5"}},
         {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions instructions>…"}]}},
         {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions for " + cwd + "\n\n<INSTRUCTIONS>…</INSTRUCTIONS>"}]}},
         {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>" + cwd + "</cwd>\n</environment_context>"}]}},
         {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "登录页点了没反应，帮我看看 " + secret_marker},
             {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="}]}},
         {"type": "event_msg", "payload": {"type": "user_message", "message": "登录页点了没反应，帮我看看"}},
         {"type": "response_item", "payload": {"type": "reasoning", "summary": [], "content": None, "encrypted_content": "gAAAAAB..."}},
         {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "先看一下登录组件。"}]}},
         {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "rg -n onSubmit src/login.tsx", "workdir": cwd}), "call_id": "call_1"}},
         {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_1", "output": "12:  const onSubmit = () => {}\n" + "\n".join(f"line {i}" for i in range(60))}},
         {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["bash", "-lc", "npm test -- login"]}), "call_id": "call_2"}},
         {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_2", "output": json.dumps({"output": "1 failed ```", "metadata": {"exit_code": 1, "duration_seconds": 1.2}})}},
         {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "**定位问题** onSubmit 是空函数"}], "encrypted_content": "gAAAA"}},
         {"type": "response_item", "payload": {"type": "custom_tool_call", "status": "completed", "call_id": "call_3", "name": "apply_patch",
                                               "input": "*** Begin Patch\n*** Update File: src/login.tsx\n@@\n-  const onSubmit = () => {}\n+  const onSubmit = () => login(form)\n*** End Patch"}},
         {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call_3", "output": json.dumps({"output": "Success. Updated the following files:\nM src/login.tsx", "metadata": {"exit_code": 0}})}},
         {"type": "response_item", "payload": {"type": "function_call", "name": "update_plan", "arguments": json.dumps({"plan": [{"step": "修 onSubmit", "status": "completed"}, {"step": "跑测试", "status": "in_progress"}]}), "call_id": "call_4"}},
         {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_4", "output": "Plan updated"}},
         {"type": "response_item", "payload": {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "react form onSubmit not firing"}}},
         {"type": "response_item", "payload": {"type": "function_call", "name": "mcp__github__get_issue", "namespace": "github", "arguments": "{\"issue\": 7}", "call_id": "call_5"}},
         {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_5", "output": [{"type": "input_text", "text": "issue 7: login broken"}, {"type": "input_image", "image_url": "data:x"}]}},
         {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "修好了：onSubmit 原来是空函数，现在调用 login(form)。"}]}},
         {"type": "event_msg", "payload": {"type": "agent_message", "message": "修好了"}},
         {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
         {"type": "compacted", "payload": {"message": "summary of earlier turns"}},
         {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "再加个单测"}]}},
         {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "加好了 login.test.tsx。"}]}},
         ]
    if extra_turn:
        L += [{"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "顺便改下按钮颜色"}]}},
              {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "按钮改成蓝色了。"}]}}]
    with open(f, "w", encoding="utf-8", newline="\n") as fh:
        for i, x in enumerate(L):
            fh.write(json.dumps(dict({"timestamp": _ts(when, i)}, **x), ensure_ascii=False) + "\n")
        fh.write("{broken json line\n")
    if title:
        with open(home / "session_index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": tid, "thread_name": title, "updated_at": _ts(when, 99)}, ensure_ascii=False) + "\n")
    return f, tid


def append_turn(f: Path, user: str, assistant: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with open(f, "a", encoding="utf-8", newline="\n") as fh:
        for role, t, typ in (("user", user, "input_text"), ("assistant", assistant, "output_text")):
            fh.write(json.dumps({"timestamp": now, "type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": typ, "text": t}]}}, ensure_ascii=False) + "\n")

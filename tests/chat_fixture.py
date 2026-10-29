# -*- coding: utf-8 -*-
"""0.4.9 对话样式用的假会话：你的话、Markdown 回复（代码块 / 列表 / 表格）、思考、连着的工具调用（Bash / Read / Edit / Grep）、
出错的命令、派出去的子代理（Task，带自己的记录文件）。全是编的内容。"""
import json, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

U = lambda: str(uuid.uuid4())


def _iso(d):
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def claude_rich(proj_dir: Path, sid: str, cwd: str, when: datetime | None = None, agent_id: str = "a1b2c3d4e5f6a7b8",
                with_meta: bool = True, result_agent_id: bool = True, rounds: int = 1) -> Path:
    when = when or datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)
    f = proj_dir / f"{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    L, t = [], when
    def rec(kind, content, dt=20, **kw):
        nonlocal t
        t += timedelta(seconds=dt)
        L.append(dict({"type": kind, "uuid": U(), "sessionId": sid, "cwd": cwd, "timestamp": _iso(t), "version": "2.1.0",
                       "message": {"role": kind, "content": content} if kind == "user" else
                       {"id": "msg_" + U()[:10], "role": "assistant", "model": "claude-opus-4-5", "type": "message", "content": content}}, **kw))
    for r in range(rounds):
        rec("user", "登录页点了没反应，帮我看看是哪里的问题，顺便把表单校验补上。" if r == 0 else f"第 {r + 1} 轮：再跑一遍测试看看")
        rec("assistant", [{"type": "thinking", "thinking": "先看 onSubmit 有没有绑上，再看校验逻辑。用户说点了没反应，多半是 handler 没传进去。", "signature": "x"}])
        b1, b2, b3, b4, b5 = (f"toolu_{r}_{i}" for i in range(5))
        rec("assistant", [{"type": "text", "text": "我先看一下登录组件和相关测试。"},
                          {"type": "tool_use", "id": b1, "name": "Grep", "input": {"pattern": "onSubmit", "path": "src"}}])
        rec("user", [{"type": "tool_result", "tool_use_id": b1, "content": "src/login.tsx:12:  const onSubmit = () => {}"}], dt=2)
        rec("assistant", [{"type": "tool_use", "id": b2, "name": "Read", "input": {"file_path": f"{cwd}/src/login.tsx"}}])
        rec("user", [{"type": "tool_result", "tool_use_id": b2, "content": "1\timport { useState } from 'react'\n12\tconst onSubmit = () => {}\n"}], dt=2)
        rec("assistant", [{"type": "tool_use", "id": b3, "name": "Bash", "input": {"command": "npm test -- login\nnpm run lint", "description": "跑登录相关的测试"}}])
        rec("user", [{"type": "tool_result", "tool_use_id": b3, "content": "FAIL src/login.test.tsx\n  ✕ submits the form (12 ms)", "is_error": True}], dt=5)
        rec("assistant", [{"type": "tool_use", "id": b4, "name": "Edit", "input": {"file_path": f"{cwd}/src/login.tsx", "old_string": "const onSubmit = () => {}",
                                                                                  "new_string": "const onSubmit = (e) => {\n  e.preventDefault()\n  if (!validate(form)) return\n  login(form)\n}"}}])
        rec("user", [{"type": "tool_result", "tool_use_id": b4, "content": "The file has been updated."}], dt=2)
        rec("assistant", [{"type": "tool_use", "id": b5, "name": "Task", "input": {"description": "检查其它表单", "subagent_type": "Explore", "prompt": "找出项目里所有没做校验的表单"}}])
        tur = {"agentId": agent_id, "status": "completed"} if result_agent_id else {"status": "completed"}
        rec("user", [{"type": "tool_result", "tool_use_id": b5, "content": [{"type": "text", "text": "还有 2 个表单没做校验：注册页、找回密码页。"}]}], dt=30, toolUseResult=tur)
        rec("assistant", [{"type": "text", "text": "## 找到原因了\n\n`onSubmit` 是个**空函数**，按钮点了当然没反应。我改成了：\n\n"
                                                   "```tsx\nconst onSubmit = (e) => {\n  e.preventDefault() // 不刷新页面\n  if (!validate(form)) return\n  login(form)\n}\n```\n\n"
                                                   "另外子代理查到还有两处表单没校验：\n\n1. 注册页\n2. 找回密码页\n\n| 文件 | 状态 |\n|---|---|\n| login.tsx | 已修 |\n| signup.tsx | 待修 |\n\n> 要我一起补上吗？"}])
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in L) + "\n", encoding="utf-8")
    sd = f.parent / sid / "subagents"
    sd.mkdir(parents=True, exist_ok=True)
    st = when + timedelta(minutes=1)
    sub = [{"type": "user", "isSidechain": True, "agentId": agent_id, "sessionId": sid, "timestamp": _iso(st), "message": {"role": "user", "content": "找出项目里所有没做校验的表单"}},
           {"type": "assistant", "isSidechain": True, "agentId": agent_id, "sessionId": sid, "timestamp": _iso(st + timedelta(seconds=9)),
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_s1", "name": "Glob", "input": {"pattern": "src/**/*form*.tsx"}}]}},
           {"type": "user", "isSidechain": True, "agentId": agent_id, "sessionId": sid, "timestamp": _iso(st + timedelta(seconds=10)),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_s1", "content": "src/signup-form.tsx\nsrc/reset-form.tsx"}]}},
           {"type": "assistant", "isSidechain": True, "agentId": agent_id, "sessionId": sid, "timestamp": _iso(st + timedelta(seconds=20)),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "还有 2 个表单没做校验：注册页、找回密码页。"}]}}]
    (sd / f"agent-{agent_id}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in sub) + "\n", encoding="utf-8")
    if with_meta:
        (sd / f"agent-{agent_id}.meta.json").write_text(json.dumps({"agentType": "Explore", "description": "检查其它表单"}, ensure_ascii=False), encoding="utf-8")
    return f

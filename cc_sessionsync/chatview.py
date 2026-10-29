# -*- coding: utf-8 -*-
"""0.4.9：会话详情的「对话样式」记录（只读）。
比 sessions.read 多带结构：你的话 / 回复（Markdown 原文）/ 思考 / 工具调用（名字、一行摘要、完整输入、结果、出错没有），
子代理（Task / Agent）调用连到它自己的记录（projects/<项目>/<会话>/subagents/agent-<id>.jsonl）。
边读边丢：只留最后 limit 条，每条的长字段截短，几百 MB 的会话也只占一点内存。"""
from __future__ import annotations

import json
import re
from collections import OrderedDict, deque
from pathlib import Path

from . import sessions as S

TEXT_MAX = 20000          # 一条你的话 / 回复最多留这么多字
SIDE_MAX = 4000           # 工具输入 / 结果、思考最多留这么多字
PENDING_MAX = 4000        # 等结果的工具调用最多记这么多个


def _cut(s, n=SIDE_MAX) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False, indent=1)
    return s if len(s) <= n else s[:n] + f"\n…（还有 {len(s) - n} 字）"


def _brief(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return str(inp or "")[:200]
    for k in ("command", "cmd", "pattern", "query", "file_path", "notebook_path", "path", "url", "description", "prompt", "skill"):
        v = inp.get(k)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v.strip():
            return v.strip().splitlines()[0][:200]
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        return f"{len(inp['todos'])} 项"
    return ""


def _input_text(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return _cut(inp)
    if isinstance(inp.get("command"), str) and len(inp) <= 3:
        return _cut(inp["command"])
    if name in ("Edit", "MultiEdit") and "old_string" in inp:
        return _cut(f"{inp.get('file_path', '')}\n--- 原来\n{inp.get('old_string', '')}\n+++ 改成\n{inp.get('new_string', '')}")
    if name == "Write" and "content" in inp:
        return _cut(f"{inp.get('file_path', '')}\n{inp.get('content', '')}")
    return _cut(inp)


def _result_text(c) -> str:
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") == "image":
                out.append("[图片]")
    return "\n".join(out)


def _user_text(c) -> str:
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if isinstance(b, dict):
            if b.get("type") == "text" and b.get("text"):
                out.append(b["text"])
            elif b.get("type") == "image":
                out.append("[图片]")
    return "\n".join(out)


class _Acc:
    """边读边收：最后 limit 条 + 还在等结果的工具调用。"""
    def __init__(self, limit: int):
        self.es: deque = deque(maxlen=max(1, limit))
        self.pend: OrderedDict = OrderedDict()
        self.total = 0

    def add(self, e: dict) -> None:
        self.total += 1
        self.es.append(e)
        if e.get("role") == "tool" and e.get("id"):
            self.pend[e["id"]] = e
            if len(self.pend) > PENDING_MAX:
                self.pend.popitem(last=False)

    def result(self, tid, text: str, err: bool, extra: dict | None = None) -> None:
        e = self.pend.pop(tid, None)
        if e is None:
            return
        e["out"] = _cut(text)
        if err:
            e["err"] = True
        if extra:
            e.update(extra)


def _claude_line(d: dict, A: _Acc, sidechain: bool) -> None:
    t = d.get("type")
    if t not in ("user", "assistant") or d.get("isMeta") or (d.get("isSidechain") and not sidechain):
        return
    ts = S._ts(d.get("timestamp"))
    m = d.get("message") or {}
    c = m.get("content")
    if t == "user":
        tur = d.get("toolUseResult")
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                extra = {}
                if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
                    extra["agent_id"] = tur["agentId"]
                A.result(b.get("tool_use_id"), _result_text(b.get("content")), bool(b.get("is_error")), extra)
        text = _user_text(c)
        if text.strip() and not any(text.lstrip().startswith(x) for x in S.SKIP_USER):
            A.add({"role": "user", "text": text[:TEXT_MAX], "ts": ts})
        return
    for b in c if isinstance(c, list) else [{"type": "text", "text": c}] if isinstance(c, str) else []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text" and (b.get("text") or "").strip():
            A.add({"role": "assistant", "text": b["text"][:TEXT_MAX], "ts": ts})
        elif bt in ("thinking", "redacted_thinking"):
            tx = b.get("thinking") or ""
            if tx.strip():
                A.add({"role": "thinking", "text": _cut(tx), "ts": ts})
        elif bt == "tool_use":
            name, inp = b.get("name") or "tool", b.get("input") or {}
            e = {"role": "tool", "id": b.get("id"), "name": name, "brief": _brief(name, inp), "input": _input_text(name, inp), "ts": ts}
            if name in ("Task", "Agent") and isinstance(inp, dict):
                e["sub"] = {"type": inp.get("subagent_type") or "", "desc": inp.get("description") or ""}
            A.add(e)


def _codex_out(o) -> tuple[str, bool]:
    if isinstance(o, dict):
        o = o.get("content") or o.get("output") or json.dumps(o, ensure_ascii=False)
    s = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
    err = False
    try:
        j = json.loads(s)
        if isinstance(j, dict):
            code = (j.get("metadata") or {}).get("exit_code")
            err = isinstance(code, int) and code != 0
            s = j.get("output") if isinstance(j.get("output"), str) else s
    except Exception:
        m = re.search(r"(?:Exit code|Process exited with code)[:\s]+(-?\d+)", s[:400])
        err = bool(m and m.group(1) != "0")
    return s, err


def _codex_line(d: dict, A: _Acc) -> None:
    if d.get("type") != "response_item":
        return
    p = d.get("payload") or {}
    ts = S._ts(d.get("timestamp"))
    pt = p.get("type")
    if pt == "message" and p.get("role") in ("user", "assistant"):
        text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict) and c.get("text"))
        if text.strip() and not (p["role"] == "user" and any(text.lstrip().startswith(x) for x in S.SKIP_USER)):
            A.add({"role": p["role"], "text": text[:TEXT_MAX], "ts": ts})
    elif pt == "reasoning":
        tx = "\n".join(x.get("text", "") for x in p.get("summary") or [] if isinstance(x, dict))
        if tx.strip():
            A.add({"role": "thinking", "text": _cut(tx), "ts": ts})
    elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
        a = p.get("arguments") if p.get("arguments") is not None else p.get("input") if p.get("input") is not None else (p.get("action") or {})
        inp = a
        if isinstance(a, str):
            try:
                inp = json.loads(a)
            except Exception:
                inp = a
        name = p.get("name") or "shell"
        if isinstance(inp, dict) and isinstance(inp.get("command"), list):
            inp = dict(inp, command=" ".join(map(str, inp["command"])))
        A.add({"role": "tool", "id": p.get("call_id") or p.get("id"), "name": name, "brief": _brief(name, inp) if isinstance(inp, dict) else str(inp).strip().splitlines()[0][:200] if str(inp).strip() else "",
               "input": _input_text(name, inp), "ts": ts})
    elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
        s, err = _codex_out(p.get("output"))
        A.result(p.get("call_id"), s, err)


def _sub_file(sid: str, agent_id: str) -> Path | None:
    f = S.claude_file(sid)
    if not f or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", agent_id or ""):
        return None
    for c in (f.parent / sid / "subagents" / f"agent-{agent_id}.jsonl", f.parent / f"agent-{agent_id}.jsonl"):
        if c.exists():
            return c
    return None


def _link_subs(sid: str, es: list) -> None:
    """Task / Agent 调用 → 子代理记录文件：先认结果里带的 agentId，没有的按 .meta.json 里的描述对。"""
    f = S.claude_file(sid)
    d = f.parent / sid / "subagents" if f else None
    metas = {}
    if d and d.is_dir():
        for m in d.glob("agent-*.meta.json"):
            try:
                j = json.loads(m.read_text(encoding="utf-8"))
                if isinstance(j, dict) and j.get("description"):
                    metas.setdefault(j["description"], m.name[len("agent-"):-len(".meta.json")])
            except Exception:
                pass
    for e in es:
        if e.get("role") != "tool" or "sub" not in e:
            continue
        aid = e.pop("agent_id", None) or metas.get(e["sub"].get("desc") or "\0")
        if aid and _sub_file(sid, aid):
            e["sub"]["agent_id"] = aid


def read(agent: str, sid: str, limit: int = 300, sub: str | None = None) -> dict:
    if agent not in ("claude", "codex"):
        r = S.read(agent, sid, limit)              # 其它应用：还是一行一行的（界面照样画成对话样式）
        return r
    if sub:
        f = _sub_file(sid, sub) if agent == "claude" else None
    else:
        f = S.claude_file(sid) if agent == "claude" else S.codex_file(sid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    A = _Acc(min(max(1, limit), 5000))
    with open(f, "rb") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if agent == "claude":
                _claude_line(d, A, bool(sub))
            else:
                _codex_line(d, A)
    es = list(A.es)
    if agent == "claude":
        _link_subs(sid, es)
    for e in es:
        e.pop("agent_id", None)
    return {"ok": True, "rich": True, "total": A.total, "entries": es, "cut": max(0, A.total - len(es)), "sub": sub or None}

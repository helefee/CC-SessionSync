# -*- coding: utf-8 -*-
"""0.5.1：会话详情的「Token 流转」（只读）。
把一个会话里每一次模型调用的真实用量按时间排好，给界面回放：新输入 / 缓存读 / 缓存写 → 模型 → 输出（含思考）→ 工具调用 → 下一次的输入。
用量的口径、去重、费用都直接用 usage 模块里那一套（_claude_lines / _codex_lines / _dsh_rows → mkrow），和「用量」页的数字一致：
- Claude Code：assistant 行的 message.usage（input / cache_read / cache_creation 5m·1h / output），同一次调用的多行按 message.id + requestId
  合成一条、留最完整的那份；子代理（<会话>/subagents/agent-*.jsonl）的调用也算进来，标成子代理。
- Codex：token_usage_record（新版）或 event_msg/token_count 的累计差值（老版）；新输入 = input − 缓存读 − 缓存写，reasoning 含在输出里。
- DeepSeek Harness：assistant/message 的 usage。
- Cursor / OpenCode / ZCode 的本地记录里没有逐次的 token 用量：返回空列表和原因，不编数字。
只读：不写任何应用的文件；大会话只留最后 MAX_CALLS 次调用。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import sessions as S
from . import usage as U

MAX_CALLS = 4000
BRIEF = 120
NO_DATA = {"cursor": "Cursor 的本地记录里没有逐次的 token 用量", "opencode": "OpenCode 的本地记录里没有逐次的 token 用量",
           "zcode": "ZCode 的本地记录里没有逐次的 token 用量"}


def _brief(s: str) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= BRIEF else s[:BRIEF] + "…"


def _pricing():
    """价格表 + 供应商倍率（和用量页同一份）；库打不开就不算费用（None），token 照样给。"""
    try:
        c = U._conn()
        try:
            return U.price_table(c), U._mults(c)
        finally:
            c.close()
    except Exception:
        return None, None


class _Calls:
    """按 usage 的去重规则收调用：同一个 id 留 token 最多的那份，时间用最早的；顺序按第一次出现。"""
    def __init__(self):
        self.by: dict = {}
        self.order: list = []

    def add(self, row: tuple, tools: list, turn: int, sub: str = "") -> dict:
        r = dict(zip(U.COLS, row))
        k = r["id"]
        cur = self.by.get(k)
        if cur is None:
            cur = self.by[k] = {"id": k, "tools": [], "turn": turn, "sub": sub, "_r": r}
            self.order.append(k)
        else:
            old = cur["_r"]
            if r["tot"] > old["tot"] or (r["tot"] == old["tot"] and r["stop"] > old["stop"]):
                r["ts"] = min(x for x in (old["ts"], r["ts"]) if x) if (old["ts"] or r["ts"]) else 0
                if not r["model"]:
                    r["model"] = old["model"]
                cur["_r"] = r
        seen = cur.setdefault("_tid", set())
        for t in tools:                      # Claude 同一条回复会拆成好几行写：按 tool_use 的 id 去重（同名工具调了两次算两次）
            if isinstance(t, tuple):
                key, t = (t[0] or "name:" + t[1]), t[1]
                if key in seen:
                    continue
                seen.add(key)
            cur["tools"].append(t)
        return cur


def _out(c: dict) -> dict:
    r = c["_r"]
    cost = sum(r[k] or 0 for k in U.CPARTS) if r["priced"] else None
    return {"id": c["id"], "ts": r["ts"], "model": r["model"], "turn": c["turn"], "sub": c["sub"] or None,
            "inp": r["inp"], "cr": r["cr"], "cw": r["cw5"] + r["cw1h"], "cw5": r["cw5"], "cw1h": r["cw1h"], "out": r["out"],
            "reasoning": min(r["reasoning"] or 0, r["out"]),
            "prompt": r["prompt"], "tot": r["tot"], "cost": cost, "est": bool(r["est"]), "tools": c["tools"][:40],
            "ntools": len(c["tools"])}


# ---------------- Claude Code ----------------

def _claude_tools(d: dict) -> list:
    m = d.get("message") or {}
    c = m.get("content")
    return [(b.get("id"), b.get("name") or "tool") for b in c if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(c, list) else []


def _claude_user(d: dict) -> str | None:
    """真正的「你的话」（不是工具结果、不是系统注入）→ 摘要；否则 None。"""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        text = "\n".join(b.get("text") or "" for b in c if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    if not text.strip() or any(text.lstrip().startswith(x) for x in S.SKIP_USER):
        return None
    return _brief(text)


def _claude_file(f: Path, C: _Calls, turns: list, ctx: dict, pt, mults, sub: str = "", turn_of=None) -> None:
    st: dict = {}
    hours: set = set()
    with open(f, "rb") as fh:
        for raw in fh:
            ln = raw.decode("utf-8", "replace")
            if '"user"' in ln and not sub:
                try:
                    d = json.loads(ln)
                except ValueError:
                    d = None
                if isinstance(d, dict) and d.get("type") == "user":
                    b = _claude_user(d)
                    if b is not None:
                        turns.append({"i": len(turns), "ts": S._ts(d.get("timestamp")), "text": b})
                    continue
            if '"assistant"' not in ln or '"usage"' not in ln:
                continue
            rows: list = []
            U._claude_lines([ln], st, "flow", ctx, 1 if sub else 0, hours, rows, pt=pt, mults=mults)
            if not rows:
                continue
            try:
                tools = _claude_tools(json.loads(ln))
            except ValueError:
                tools = []
            for row in rows:
                turn = turn_of(row[3]) if turn_of else max(0, len(turns) - 1)
                C.add(row, tools, turn, sub)


def _claude(sid: str, pt, mults) -> dict:
    f = S.claude_file(sid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    ctx = U._claude_ctx()
    C, turns = _Calls(), []
    _claude_file(f, C, turns, ctx, pt, mults)
    d = f.parent / sid / "subagents"
    subs = 0
    if d.is_dir():
        starts = [t["ts"] for t in turns]

        def turn_of(ts):
            i = 0
            for j, s in enumerate(starts):
                if s and ts and s <= ts:
                    i = j
            return i
        for sf in sorted(d.glob("agent-*.jsonl")):
            aid = sf.name[len("agent-"):-len(".jsonl")]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", aid):
                subs += 1
                _claude_file(sf, C, [], ctx, pt, mults, sub=aid, turn_of=turn_of)
    return {"ok": True, "C": C, "turns": turns, "subagents": subs, "file": str(f)}


# ---------------- Codex ----------------

def _codex(tid: str, pt, mults) -> dict:
    f = S.codex_file(tid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    ctx = U._codex_ctx()
    C, turns, pend = _Calls(), [], []
    st: dict = {}
    hours: set = set()
    keys = ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')
    with open(f, "rb") as fh:
        for raw in fh:
            ln = raw.decode("utf-8", "replace")
            if '"response_item"' in ln:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                pt_ = p.get("type")
                if pt_ == "message" and p.get("role") == "user":
                    text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict) and c.get("text"))
                    if text.strip() and not any(text.lstrip().startswith(x) for x in S.SKIP_USER):
                        turns.append({"i": len(turns), "ts": S._ts(d.get("timestamp")), "text": _brief(text)})
                elif pt_ in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                    pend.append(p.get("name") or ("web_search" if pt_ == "web_search_call" else "shell"))
                continue
            if not any(k in ln for k in keys):
                continue
            rows: list = []
            U._codex_lines([ln], st, "flow", ctx, hours, rows, pt=pt, mults=mults)
            for row in rows:
                C.add(row, pend, max(0, len(turns) - 1))
                pend = []
    return {"ok": True, "C": C, "turns": turns, "subagents": 0, "file": str(f)}


# ---------------- DeepSeek Harness ----------------

def _dsh(sid: str, pt, mults) -> dict:
    from . import dsh
    f = dsh.find_file(sid)
    if not f:
        return {"ok": False, "error": "找不到这个 DeepSeek Harness 会话"}
    try:
        L = dsh.load(f)
    except Exception as e:
        return {"ok": False, "error": f"读不了：{e}"}
    rows: list = []
    U._dsh_rows(L, "flow", set(), rows, pt, mults)
    byid = {r[0]: r for r in rows}
    C, turns = _Calls(), []
    for e in L["events"]:
        t = e.get("type")
        m = dsh._msg(e)
        if t == "user/message" and ((m.get("source") or {}).get("kind")) == "user":
            s = dsh._txt(m.get("content"))
            if s:
                turns.append({"i": len(turns), "ts": (e.get("time") or 0) / 1000, "text": _brief(s)})
        elif t == "assistant/message":
            sid_ = L["header"].get("id") or sid
            mid = m.get("id") or "%s:%s" % (sid_, e.get("seq"))
            key = "ds:%s:%s" % (sid_, mid)
            row = byid.get(key)
            if row:
                tools = [b.get("name") or "tool" for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool-call"]
                C.add(row, tools, max(0, len(turns) - 1))
    return {"ok": True, "C": C, "turns": turns, "subagents": 0, "file": str(f)}


def flow(agent: str, sid: str) -> dict:
    """→ {ok, agent, id, calls:[…按时间], turns:[…], totals, models, cut}；没有用量数据的应用 ok=True、calls=[]、reason 说明。"""
    if agent in NO_DATA:
        return {"ok": True, "agent": agent, "id": sid, "calls": [], "turns": [], "totals": _totals([]), "models": [], "cut": 0,
                "reason": NO_DATA[agent]}
    fn = {"claude": _claude, "codex": _codex, "dsh": _dsh}.get(agent)
    if not fn:
        return {"ok": False, "error": "不认识的应用"}
    pt, mults = _pricing()
    r = fn(sid, pt, mults)
    if not r.get("ok"):
        return r
    calls = [_out(r["C"].by[k]) for k in r["C"].order]
    calls = [c for c in calls if c["tot"] > 0]
    calls.sort(key=lambda c: (c["ts"] or 0))
    cut = max(0, len(calls) - MAX_CALLS)
    totals = _totals(calls)                            # 合计按全部调用算（只回放最后 MAX_CALLS 次）
    calls = calls[cut:]
    for i, c in enumerate(calls):
        c["i"] = i
    models: dict = {}
    for c in calls:
        if c["model"]:
            models[c["model"]] = models.get(c["model"], 0) + c["tot"]
    return {"ok": True, "agent": agent, "id": sid, "calls": calls, "turns": r["turns"][-2000:], "totals": totals,
            "models": sorted(models, key=lambda m: -models[m]), "subagents": r["subagents"], "cut": cut,
            "reason": "" if calls else "这个会话的记录里没有 token 用量（可能是导入的、还没回复，或者走了模型路由）"}


def _totals(calls: list) -> dict:
    t = {k: sum(c[k] for c in calls) for k in ("inp", "cr", "cw", "out", "reasoning", "tot")}
    t["calls"] = len(calls)
    t["tools"] = sum(c["ntools"] for c in calls)
    priced = [c["cost"] for c in calls if c["cost"] is not None]
    t["cost"] = sum(priced) if priced else None
    t["cost_partial"] = bool(priced) and len(priced) < len(calls)
    prompt = t["inp"] + t["cr"] + t["cw"]
    t["hit"] = (t["cr"] / prompt) if prompt else 0
    t["sub_calls"] = sum(1 for c in calls if c.get("sub"))
    return t

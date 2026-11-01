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

import itertools
import json
import os
import re
import threading
import time
from collections import OrderedDict
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
        self.v = 0                                   # 当前版本号：add 过的调用记成这个（实时：只回比客户端新的）

    def add(self, row: tuple, tools: list, turn: int, sub: str = "") -> dict:
        r = dict(zip(U.COLS, row))
        k = r["id"]
        cur = self.by.get(k)
        if cur is None:
            cur = self.by[k] = {"id": k, "tools": [], "turn": turn, "sub": sub, "_r": r, "_v": self.v}
            self.order.append(k)
        else:
            old = cur["_r"]
            if r["tot"] > old["tot"] or (r["tot"] == old["tot"] and r["stop"] > old["stop"]):
                r["ts"] = min(x for x in (old["ts"], r["ts"]) if x) if (old["ts"] or r["ts"]) else 0
                if not r["model"]:
                    r["model"] = old["model"]
                cur["_r"] = r
                cur["_v"] = self.v
        seen = cur.setdefault("_tid", set())
        for t in tools:                      # Claude 同一条回复会拆成好几行写：按 tool_use 的 id 去重（同名工具调了两次算两次）
            if isinstance(t, tuple):
                key, t = (t[0] or "name:" + t[1]), t[1]
                if key in seen:
                    continue
                seen.add(key)
            cur["tools"].append(t)
            cur["_v"] = self.v
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


class _Reset(Exception):
    """文件被截短 / 换了一个（轮转、整个重写）：这个会话从头再读一遍。"""


class _Tail:
    """按字节偏移往后读一个 jsonl：只读新增的部分；最后半行（还在写）留到下次。
    文件变短、inode 换了、开头那段字节变了 → _Reset。"""
    HEAD = 512

    def __init__(self, path: Path):
        self.path = Path(path)
        self.off = 0
        self.key = None
        self.head = b""
        self.mtime = 0.0

    def lines(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return                                   # 暂时没了（正在被替换）：下次再看
        key = (st.st_dev, st.st_ino)
        if self.key is not None and (key != self.key or st.st_size < self.off):
            raise _Reset()
        self.key, self.mtime = key, st.st_mtime
        if st.st_size == self.off:
            return
        with open(self.path, "rb") as fh:
            if self.off:
                if fh.read(len(self.head)) != self.head:
                    raise _Reset()
                fh.seek(self.off)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    try:                             # 没有换行结尾：是完整的一行 JSON 才算（最后一行常常不带换行）；半行留着
                        ok = isinstance(json.loads(raw), dict)
                    except ValueError:
                        ok = False
                    if not ok:
                        break
                self.off += len(raw)
                yield raw.decode("utf-8", "replace")
        if len(self.head) < self.HEAD:
            with open(self.path, "rb") as fh:
                self.head = fh.read(min(self.HEAD, self.off))


class _Sess:
    """一个会话的增量状态。step() 只读新增的字节；改动过的调用打上版本号 _v（> 客户端手里的 v 就回给它）。"""
    def __init__(self, agent: str, sid: str, pt, mults):
        self.agent, self.sid, self.pt, self.mults = agent, sid, pt, mults
        self.C, self.turns, self.subs = _Calls(), [], 0
        self.gen = "%x%x" % (int(time.time() * 1000), next(_GEN))
        self.lock = threading.Lock()
        self.used = time.time()

    def mtime(self) -> float:
        return max([t.mtime for t in self.tails()] or [0.0])

    def tails(self) -> list:
        return []


class _ClaudeSess(_Sess):
    def __init__(self, sid, pt, mults):
        super().__init__("claude", sid, pt, mults)
        f = S.claude_file(sid)
        if not f:
            raise FileNotFoundError("找不到会话记录文件")
        self.file = f
        self.ctx = U._claude_ctx()
        self.main = _Tail(f)
        self.st: dict = {"": {}}
        self.sub: dict = {}
        self.subdir = f.parent / sid / "subagents"
        self.subm = None
        self.hours: set = set()

    def tails(self):
        return [self.main] + list(self.sub.values())

    def _turn_of(self, ts):
        i = 0
        for j, t in enumerate(self.turns):
            if t["ts"] and ts and t["ts"] <= ts:
                i = j
        return i

    def _line(self, ln: str, sub: str):
        if '"user"' in ln and not sub:
            try:
                d = json.loads(ln)
            except ValueError:
                d = None
            if isinstance(d, dict) and d.get("type") == "user":
                b = _claude_user(d)
                if b is not None:
                    self.turns.append({"i": len(self.turns), "ts": S._ts(d.get("timestamp")), "text": b})
                return
        if '"assistant"' not in ln or '"usage"' not in ln:
            return
        rows: list = []
        U._claude_lines([ln], self.st.setdefault(sub, {}), "flow", self.ctx, 1 if sub else 0, self.hours, rows, pt=self.pt, mults=self.mults)
        if not rows:
            return
        try:
            tools = _claude_tools(json.loads(ln))
        except ValueError:
            tools = []
        for row in rows:
            turn = self._turn_of(row[3]) if sub else max(0, len(self.turns) - 1)
            self.C.add(row, tools, turn, sub)

    def step(self):
        for ln in self.main.lines():
            self._line(ln, "")
        try:
            m = self.subdir.stat().st_mtime_ns if self.subdir.is_dir() else None
        except OSError:
            m = None
        if m is not None and m != self.subm:          # 子代理目录有变化（新开了子代理）才重新列一遍
            self.subm = m
            for sf in sorted(self.subdir.glob("agent-*.jsonl")):
                aid = sf.name[len("agent-"):-len(".jsonl")]
                if aid not in self.sub and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", aid):
                    self.sub[aid] = _Tail(sf)
            self.subs = len(self.sub)
        for aid, t in self.sub.items():
            for ln in t.lines():
                self._line(ln, aid)


class _CodexSess(_Sess):
    KEYS = ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')

    def __init__(self, tid, pt, mults):
        super().__init__("codex", tid, pt, mults)
        f = S.codex_file(tid)
        if not f:
            raise FileNotFoundError("找不到会话记录文件")
        self.file = f
        self.ctx = U._codex_ctx()
        self.main = _Tail(f)
        self.st: dict = {}
        self.hours: set = set()
        self.pend: list = []

    def tails(self):
        return [self.main]

    def step(self):
        for ln in self.main.lines():
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
                        self.turns.append({"i": len(self.turns), "ts": S._ts(d.get("timestamp")), "text": _brief(text)})
                elif pt_ in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                    self.pend.append(p.get("name") or ("web_search" if pt_ == "web_search_call" else "shell"))
                continue
            if not any(k in ln for k in self.KEYS):
                continue
            rows: list = []
            U._codex_lines([ln], self.st, "flow", self.ctx, self.hours, rows, pt=self.pt, mults=self.mults)
            for row in rows:
                self.C.add(row, self.pend, max(0, len(self.turns) - 1))
                self.pend = []


class _DshSess(_Sess):
    """DeepSeek Harness 的记录可能是 zstd 压缩的，没法按字节往后接：文件变了（大小 / 时间）才整个重读，没变的调用不算改动。"""
    def __init__(self, sid, pt, mults):
        super().__init__("dsh", sid, pt, mults)
        from . import dsh
        f = dsh.find_file(sid)
        if not f:
            raise FileNotFoundError("找不到这个 DeepSeek Harness 会话")
        self.file = f
        self.sig = None
        self._mt = 0.0

    def mtime(self):
        return self._mt

    def step(self):
        from . import dsh
        try:
            st = os.stat(self.file)
        except OSError:
            return
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        if sig == self.sig:
            return
        L = dsh.load(self.file)
        self.sig, self._mt = sig, st.st_mtime
        rows: list = []
        U._dsh_rows(L, "flow", set(), rows, self.pt, self.mults)
        byid = {r[0]: r for r in rows}
        C, turns = _Calls(), []
        C.v = self.C.v
        for e in L["events"]:
            t = e.get("type")
            m = dsh._msg(e)
            if t == "user/message" and ((m.get("source") or {}).get("kind")) == "user":
                s = dsh._txt(m.get("content"))
                if s:
                    turns.append({"i": len(turns), "ts": (e.get("time") or 0) / 1000, "text": _brief(s)})
            elif t == "assistant/message":
                sid_ = L["header"].get("id") or self.sid
                mid = m.get("id") or "%s:%s" % (sid_, e.get("seq"))
                row = byid.get("ds:%s:%s" % (sid_, mid))
                if row:
                    tools = [b.get("name") or "tool" for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool-call"]
                    C.add(row, tools, max(0, len(turns) - 1))
        for k, c in C.by.items():                      # 和上次一样的调用保留原来的版本号：只把新的 / 变了的回给界面
            o = self.C.by.get(k)
            if o and o["_r"]["tot"] == c["_r"]["tot"] and o["tools"] == c["tools"] and o["turn"] == c["turn"]:
                c["_v"] = o["_v"]
        if len(turns) < len(self.turns) or any(a["text"] != b["text"] for a, b in zip(turns, self.turns)):
            raise _Reset()
        self.C, self.turns = C, turns


_KIND = {"claude": _ClaudeSess, "codex": _CodexSess, "dsh": _DshSess}
_GEN = itertools.count(1)
_CACHE: "OrderedDict[tuple, _Sess]" = OrderedDict()
_CLOCK = threading.Lock()
CACHE_N = 8                       # 最多同时记着几个会话的增量状态
LIVE_S = 120                      # 记录文件这么多秒内改过 → 界面默认打开「实时」
WAIT_MAX = 20.0
TICK = 0.35


def _sess(agent: str, sid: str, fresh: bool = False) -> _Sess:
    k = (agent, sid)
    with _CLOCK:
        s = None if fresh else _CACHE.get(k)
        if s is None:
            pt, mults = _pricing()
            s = _KIND[agent](sid, pt, mults)
            _CACHE[k] = s
            while len(_CACHE) > CACHE_N:
                _CACHE.popitem(last=False)
        _CACHE.move_to_end(k)
        s.used = time.time()
        return s


def _poll(agent: str, sid: str) -> _Sess:
    """读一次新增的部分；文件被截短 / 换了就整个重来（新的 gen）。"""
    s = _sess(agent, sid)
    for _ in range(2):
        with s.lock:
            try:
                s.C.v += 1
                s.step()
                return s
            except _Reset:
                pass
        s = _sess(agent, sid, fresh=True)
    return s


def _calls(s: _Sess, since: int | None = None) -> list:
    out = []
    for k in s.C.order:
        c = s.C.by[k]
        if since is not None and c.get("_v", 0) <= since:
            continue
        o = _out(c)
        if o["tot"] > 0:
            out.append(o)
    out.sort(key=lambda c: (c["ts"] or 0))
    return out


def _resp(s: _Sess, since: int | None) -> dict:
    """since=None：整份（和 flow 一样）；否则只给 since 之后新增 / 变过的调用（totals 仍是整个会话的）。"""
    allc = _calls(s)
    totals = _totals(allc)
    models: dict = {}
    for c in allc:
        if c["model"]:
            models[c["model"]] = models.get(c["model"], 0) + c["tot"]
    mt = s.mtime()
    r = {"ok": True, "agent": s.agent, "id": s.sid, "gen": s.gen, "v": s.C.v, "totals": totals, "subagents": s.subs,
         "models": sorted(models, key=lambda m: -models[m]), "mtime": mt, "age": max(0.0, time.time() - mt) if mt else None,
         "live_s": LIVE_S, "nturns": len(s.turns)}
    if since is None:
        cut = max(0, len(allc) - MAX_CALLS)
        calls = allc[cut:]
        for i, c in enumerate(calls):
            c["i"] = i
        r.update(calls=calls, turns=list(s.turns), cut=cut, reset=True,
                 reason="" if calls else "这个会话的记录里没有 token 用量（可能是导入的、还没回复，或者走了模型路由）")
    else:
        r.update(calls=_calls(s, since), reset=False)
    return r


def _nodata(agent: str, sid: str) -> dict:
    return {"ok": True, "agent": agent, "id": sid, "calls": [], "turns": [], "totals": _totals([]), "models": [], "cut": 0,
            "reason": NO_DATA[agent], "gen": "", "v": 0, "age": None, "reset": True}


def flow(agent: str, sid: str) -> dict:
    """→ {ok, agent, id, calls:[…按时间], turns:[…], totals, models, cut, gen, v, age}；没有用量数据的应用 ok=True、calls=[]、reason 说明。
    读完的增量状态在内存里留着（最近 CACHE_N 个会话），之后的 live() 只读新增的部分。"""
    if agent in NO_DATA:
        return _nodata(agent, sid)
    if agent not in _KIND:
        return {"ok": False, "error": "不认识的应用"}
    try:
        _sess(agent, sid, fresh=True)                 # 打开时整个读一遍（价格表用最新的）；之后的「实时」接着它往后读
        return _resp(_poll(agent, sid), None)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"读不了：{e}"}


def live(agent: str, sid: str, gen: str = "", v: int = 0, nt: int = 0, wait: float = 0.0) -> dict:
    """实时：只回 v 之后新增 / 变过的调用，和 nt 之后的轮次。gen 对不上（服务重启、文件被截短 / 换了）→ 整份（reset=True）。
    wait>0 是长轮询：最多等 wait 秒，有新调用马上回；等的时候每 TICK 秒只 stat 一下文件。"""
    if agent in NO_DATA:
        return _nodata(agent, sid)
    if agent not in _KIND:
        return {"ok": False, "error": "不认识的应用"}
    end = time.time() + max(0.0, min(WAIT_MAX, float(wait or 0)))
    try:
        while True:
            s = _poll(agent, sid)
            if s.gen != gen:
                return _resp(s, None)
            r = _resp(s, int(v)) if (any(s.C.by[k].get("_v", 0) > int(v) for k in s.C.order) or len(s.turns) > int(nt)) else None
            if r is not None or time.time() >= end:
                if r is None:
                    r = _resp(s, int(v))
                r["turns"] = s.turns[int(nt):] if int(nt) <= len(s.turns) else list(s.turns)
                r["nt0"] = min(int(nt), len(s.turns))
                return r
            time.sleep(TICK)
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"读不了：{e}"}


def active(within: float = 6 * 3600) -> dict:
    """最近在动的那个会话（Claude Code / Codex / DeepSeek Harness 里记录文件最后改过的；Claude 的子代理算到主会话上）。"""
    best = (0.0, "", "")

    def see(mt, agent, sid):
        nonlocal best
        if mt > best[0]:
            best = (mt, agent, sid)
    try:
        proj = S.claude_home() / "projects"
        if proj.is_dir():
            for f in proj.glob("*/*.jsonl"):
                try:
                    see(f.stat().st_mtime, "claude", f.stem)
                except OSError:
                    pass
            for f in proj.glob("*/*/subagents/agent-*.jsonl"):
                try:
                    see(f.stat().st_mtime, "claude", f.parent.parent.name)
                except OSError:
                    pass
    except Exception:
        pass
    try:
        for f in S.rollouts():
            try:
                see(f.stat().st_mtime, "codex", f.stem[-36:])
            except OSError:
                pass
    except Exception:
        pass
    try:
        from . import dsh
        for sf in dsh.session_files():
            try:
                see(sf["file"].stat().st_mtime, "dsh", sf["dir"].name)
            except OSError:
                pass
    except Exception:
        pass
    mt, agent, sid = best
    if not agent or time.time() - mt > within:
        return {"ok": True, "agent": None, "reason": "最近没有在进行的 Claude Code / Codex / DeepSeek Harness 会话"}
    return {"ok": True, "agent": agent, "id": sid, "mtime": mt, "age": max(0.0, time.time() - mt)}


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

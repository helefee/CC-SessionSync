# -*- coding: utf-8 -*-
"""会话转换的公共中间格式（0.2.7）：一个会话 = 头 + 一串条目，和应用无关。

头：{"format": "ccss-ir/1", "agent": 来源应用, "id", "title", "cwd", "created"(毫秒), "updated", "model", "source", ...}
条目：{"role": "user" | "assistant", "kind": "text" | "thinking" | "tool", "text", "tool", "input", "output", "status",
      "error", "ts"(毫秒), "images"(张数), "model"}
Cursor 的镜像（ccss-cursor/1）就是这个格式的前身，同样认。

所有「X → Y」都是：X 的读取器 → 中间格式 → Y 的写入器（或给 Y 的上下文文件）。这里放和应用无关的部分：
读写中间格式、只读查看、拼 Claude Code / Codex / OpenCode 的记录、拼上下文 Markdown。会丢 / 会变的东西都写在各函数里。"""
from __future__ import annotations

import json
import random
import string
import time
import uuid
from datetime import datetime, timezone

FORMAT = "ccss-ir/1"
FORMATS = {FORMAT, "ccss-cursor/1"}
AGENTS = ("claude", "codex", "dsh", "cursor", "opencode", "zcode")
LABEL = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "cursor": "Cursor", "opencode": "OpenCode", "zcode": "ZCode"}
SHORT = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek", "cursor": "Cursor", "opencode": "OpenCode", "zcode": "ZCode"}
PREFIX = {k: f"（{v}）" for k, v in SHORT.items()}      # 导过去的会话标题前缀：「（Cursor）修首页白屏」


def _json(v):
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    try:
        return json.loads(v)
    except Exception:
        return None


def iso_ms(ms) -> str:
    ms = int(ms or time.time() * 1000)
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def to_ms(ts) -> int | None:
    """秒 / 毫秒 / ISO 字符串 → 毫秒。"""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return int(ts if ts > 1e11 else ts * 1000)
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


# ---------- 读写中间格式 ----------

def dumps(header: dict, items: list[dict]) -> bytes:
    h = dict(header, format=FORMAT, type="ccss-ir")
    lines = [json.dumps(h, ensure_ascii=False, sort_keys=True)]
    lines += [json.dumps({k: v for k, v in it.items() if v is not None}, ensure_ascii=False, sort_keys=True) for it in items]
    return ("\n".join(lines) + "\n").encode("utf-8")


def load(data: bytes) -> tuple[dict, list[dict]]:
    header, items = {}, []
    for ln in data.decode("utf-8", "replace").splitlines():
        o = _json(ln)
        if not isinstance(o, dict):
            continue
        if not header and (o.get("format") in FORMATS or o.get("type") in ("cursor", "ccss-ir")):
            header = o
        elif o.get("role") in ("user", "assistant"):
            items.append(o)
    if header.get("type") == "cursor" and not header.get("agent"):
        header["agent"] = "cursor"
    return header, items


def title_of(items: list[dict]) -> str:
    t = next((it.get("text") for it in items if it.get("role") == "user" and it.get("text")), "")
    return " ".join((t or "").split())[:60]


def brief(a) -> str:
    """工具参数一行摘要。"""
    if isinstance(a, str):
        j = _json(a) if a[:1] in "{[" else None
        a = j if j is not None else a
    if isinstance(a, dict):
        for k in ("command", "cmd", "targetFile", "file_path", "filePath", "relativeWorkspacePath", "path", "pattern", "globPattern",
                  "searchTerm", "url", "query", "description", "prompt"):
            v = a.get(k)
            if isinstance(v, list):
                v = " ".join(map(str, v))
            if isinstance(v, str) and v:
                return " ".join(v.split())[:160]
        return " ".join(json.dumps(a, ensure_ascii=False).split())[:160]
    return " ".join(str(a or "").split())[:160]


def entries(items: list[dict]) -> list[dict]:
    """只读看：人话 / 回复 / 工具一行（思考不显示）——和会话管理里别的应用一样。"""
    out = []
    for it in items:
        ts = (it.get("ts") or 0) / 1000
        if it["role"] == "user":
            s = it.get("text") or ""
            if it.get("images"):
                s = (s + "\n" if s else "") + f"[图片 ×{it['images']}]"
            if s:
                out.append({"role": "user", "text": s, "ts": ts})
        elif it.get("kind") == "text":
            out.append({"role": "assistant", "text": it.get("text") or "", "ts": ts, "model": it.get("model")})
        elif it.get("kind") == "tool":
            out.append({"role": "tool", "text": f"{it.get('tool')}  {brief(it.get('input'))}".strip(), "ts": ts})
    return out


def entries_from_bytes(data: bytes) -> list[dict]:
    return entries(load(data)[1])


def count_msgs(data: bytes) -> int:
    n = 0
    for ln in data.decode("utf-8", "replace").splitlines():
        o = _json(ln)
        if isinstance(o, dict) and (o.get("role") == "user" or (o.get("role") == "assistant" and o.get("kind") == "text")):
            n += 1
    return n


def stats_of(items: list[dict]) -> dict:
    return {"user": sum(1 for i in items if i["role"] == "user"),
            "assistant": sum(1 for i in items if i["role"] == "assistant" and i.get("kind") == "text"),
            "tools": sum(1 for i in items if i.get("kind") == "tool"),
            "thinking": sum(1 for i in items if i.get("kind") == "thinking"),
            "images": sum(int(i.get("images") or 0) for i in items)}


# ---------- 拼成一轮一轮的文字（各写入器共用） ----------

def _clip(s: str, lines: int, chars: int) -> str:
    s = s or ""
    ls = s.split("\n")
    cut = False
    if len(ls) > lines:
        ls, cut = ls[:lines], True
    s2 = "\n".join(ls)
    if len(s2) > chars:
        s2, cut = s2[:chars], True
    return s2 + ("\n…（截短了）" if cut else "")


def fence(s: str) -> str:
    n = 3
    while "`" * n in (s or ""):
        n += 1
    return "`" * n + "\n" + (s or "") + "\n" + "`" * n


def turns(h: dict, items: list[dict], reasoning: bool = False, out_lines: int = 20, out_chars: int = 2000,
          intro: bool = True, target: str = "claude") -> tuple[list[list], dict]:
    """→ ([[role, [文字…], ts毫秒, model]], 统计)。工具调用和结果变成文字（结果截短），思考缺省丢（reasoning=True 当文字带上），
    图片换成占位。相邻同角色的并成一轮。"""
    src = h.get("agent") or "claude"
    lab = h.get("source_name") or LABEL.get(src, src)
    sid, cwd = h.get("id") or "", h.get("cwd") or ""
    title = h.get("title") or title_of(items) or "对话"
    st = {"user": 0, "assistant": 0, "tools": 0, "reasoning_dropped": 0, "reasoning_kept": 0, "images": 0}
    out: list[list] = []

    def push(role, text, ts, model=None):
        if not text:
            return
        if out and out[-1][0] == role:
            out[-1][1].append(text)
            out[-1][3] = out[-1][3] or model
        else:
            out.append([role, [text], ts, model])

    if intro:
        push("user", f"（以下是从 {lab} 导入的对话「{title}」，{LABEL.get(src, src)} 会话 {sid}" + (f"，工作目录 {cwd}" if cwd else "") +
             f"。{LABEL.get(src, src)} 里跑过的工具只留了文字摘要，输出是截短的；接着做之前先看一下文件 / git 的当前状态。）", h.get("created"))
    for it in items:
        ts = it.get("ts")
        if it["role"] == "user":
            st["user"] += 1
            t = it.get("text") or ""
            if it.get("images"):
                st["images"] += int(it["images"])
                t = (t + "\n" if t else "") + f"[图片 ×{it['images']}，没有带过来]"
            push("user", t, ts)
        elif it.get("kind") == "text":
            st["assistant"] += 1
            push("assistant", it.get("text") or "", ts, it.get("model"))
        elif it.get("kind") == "thinking":
            if reasoning and it.get("text"):
                st["reasoning_kept"] += 1
                push("assistant", f"〔{LABEL.get(src, src)} 里的思考〕\n" + it["text"], ts, it.get("model"))
            else:
                st["reasoning_dropped"] += 1
        elif it.get("kind") == "tool":
            st["tools"] += 1
            a = it.get("input")
            a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False, indent=1)
            s = f"〔{LABEL.get(src, src)} 调用了 {it.get('tool')}〕" + ("（出错）" if it.get("status") == "error" or it.get("error") else "") + \
                "\n" + fence(_clip(a or "", 30, 3000))
            o = it.get("output") or it.get("error") or ""
            if o:
                s += "\n输出：\n" + fence(_clip(str(o), out_lines, out_chars))
            push("assistant", s, ts, it.get("model"))
    if not out:
        push("user", f"（{lab} 的这个会话是空的。）", h.get("created"))
    if out[-1][0] == "user":
        push("assistant", f"（{LABEL.get(src, src)} 那边到这里还没有回复。导入到此为止。）", out[-1][2])
    return out, dict(st, turns=len(out), title=title)


# ---------- → Claude Code ----------

def build_claude(h: dict, items: list[dict], new_sid: str, version: str, title: str | None = None, reasoning: bool = False,
                 out_lines: int = 20, out_chars: int = 2000, cwd: str | None = None) -> dict:
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning, out_lines, out_chars)
    title = title or st["title"]
    cwd = cwd or h.get("cwd") or ""
    recs, parent, last = [], None, None
    for role, parts, ts, _m in T:
        u = str(uuid.uuid4())
        ts = ts or last or h.get("created")
        last = ts
        r = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": new_sid,
             "version": version, "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": iso_ms(ts)}
        text = "\n\n".join(p for p in parts if p)
        if role == "user":
            r["message"] = {"role": "user", "content": text}
        else:
            r["message"] = {"id": f"msg_{src}import_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": f"{src}-import", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        parent = u
    full = PREFIX.get(src, "") + title
    recs.append({"type": "custom-title", "customTitle": full, "sessionId": new_sid})
    return {"records": recs, "cwd": cwd, "title": full, "turns": len(T), "stats": st}


# ---------- → Codex（rollout-*.jsonl） ----------

def build_codex(h: dict, items: list[dict], tid: str, cli_version: str, title: str | None = None, reasoning: bool = False,
                cwd: str | None = None, now_ms: int | None = None) -> dict:
    """Codex 的 rollout：session_meta + 一轮一轮的 response_item message（用户 input_text / 助手 output_text）+ 对应的 event_msg
    （Codex 用它列「用户说了什么」）。工具都已经是文字，不写 function_call（Codex 接着聊时也不会去对 call_id）。"""
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning)
    title = title or st["title"]
    cwd = cwd or h.get("cwd") or ""
    t0 = now_ms or int(time.time() * 1000)
    first = to_ms(T[0][2]) or t0
    meta = {"id": tid, "timestamp": iso_ms(first), "cwd": cwd, "originator": "cc_sessionsync", "cli_version": cli_version,
            "instructions": None, "source": "cli", "model_provider": "openai", "thread_source": "user"}
    L = [{"timestamp": iso_ms(first), "type": "session_meta", "payload": meta}]
    last = first
    for role, parts, ts, _m in T:
        ts = max(to_ms(ts) or last, last)
        last = ts
        text = "\n\n".join(p for p in parts if p)
        if role == "user":
            L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": text}]}})
            L.append({"timestamp": iso_ms(ts), "type": "event_msg", "payload": {"type": "user_message", "message": text, "images": []}})
        else:
            L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]}})
            L.append({"timestamp": iso_ms(ts), "type": "event_msg", "payload": {"type": "agent_message", "message": text}})
    full = PREFIX.get(src, "") + title
    return {"lines": [json.dumps(x, ensure_ascii=False) for x in L], "cwd": cwd, "title": full, "turns": len(T), "stats": st,
            "first_ms": first}


# ---------- → OpenCode（`opencode session import` 吃的 JSON） ----------

_B62 = string.ascii_letters + string.digits


def oc_id(prefix: str, ms: int, n: int, descending: bool = False) -> str:
    """OpenCode 的 id：前缀_ + 12 位十六进制（毫秒×4096+计数，会话号取反 → 新的排前面）+ 14 位随机。"""
    v = ms * 0x1000 + n
    if descending:
        v = (~v) & 0xFFFFFFFFFFFF
    return f"{prefix}_{v & 0xFFFFFFFFFFFF:012x}" + "".join(random.choice(_B62) for _ in range(14))


def build_opencode(h: dict, items: list[dict], title: str | None = None, reasoning: bool = False, cwd: str | None = None,
                   version: int = 2, now_ms: int | None = None) -> dict:
    """version=2：OpenCode 2.x 的 {info, messages:[{type:user|assistant, …}]}；version=1：1.x 的 {info, messages:[{info, parts}]}。
    只带文字（工具已经是文字摘要），助手消息记成模型 cc-sessionsync/<来源>-import、0 token。"""
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning)
    title = title or st["title"]
    full = PREFIX.get(src, "") + title
    cwd = cwd or h.get("cwd") or ""
    t0 = now_ms or int(time.time() * 1000)
    sid = oc_id("ses", t0, 0, descending=True)
    zero = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    model = {"id": f"{src}-import", "providerID": "cc-sessionsync"}
    msgs, last, prev_user = [], None, None
    for i, (role, parts, ts, _m) in enumerate(T):
        ts = max(to_ms(ts) or last or t0, last or 0)
        last = ts
        mid = oc_id("msg", t0, i + 1)
        text = "\n\n".join(p for p in parts if p)
        if version == 2:
            if role == "user":
                msgs.append({"id": mid, "time": {"created": ts}, "type": "user", "text": text, "files": []})
            else:
                msgs.append({"id": mid, "time": {"created": ts, "completed": ts}, "type": "assistant", "agent": "build", "model": model,
                             "content": [{"type": "text", "text": text}], "finish": "stop", "cost": 0, "tokens": zero})
        else:
            pid = oc_id("prt", t0, i + 1)
            if role == "user":
                info = {"id": mid, "sessionID": sid, "role": "user", "time": {"created": ts}, "agent": "build",
                        "model": {"providerID": model["providerID"], "modelID": model["id"]}}
                prev_user = mid
            else:
                info = {"id": mid, "sessionID": sid, "role": "assistant", "parentID": prev_user or mid, "mode": "build", "agent": "build",
                        "path": {"cwd": cwd, "root": cwd}, "cost": 0, "tokens": dict(zero, total=0), "modelID": model["id"],
                        "providerID": model["providerID"], "time": {"created": ts, "completed": ts}, "finish": "stop"}
            msgs.append({"info": info, "parts": [{"id": pid, "sessionID": sid, "messageID": mid, "type": "text", "text": text,
                                                  "time": {"start": ts, "end": ts}}]})
    if version == 2:
        info = {"id": sid, "projectID": "global", "title": full, "cost": 0, "tokens": zero,
                "time": {"created": to_ms(T[0][2]) or t0, "updated": last or t0}, "location": {"directory": cwd}}
    else:
        info = {"id": sid, "slug": "imported-" + sid[-6:].lower(), "projectID": "global", "directory": cwd, "title": full, "version": "1.0.0",
                "time": {"created": to_ms(T[0][2]) or t0, "updated": last or t0}}
    return {"data": {"info": info, "messages": msgs}, "id": sid, "cwd": cwd, "title": full, "turns": len(T), "stats": st}


# ---------- 上下文 Markdown（导不进去的方向一律走这个） ----------

def context_md(h: dict, items: list[dict], target: str = "cursor") -> dict:
    """一份给目标应用「看」的 Markdown：对话文字 + 工具一行摘要（不带原始记录，免得把上下文撑爆）。"""
    src = h.get("agent") or "claude"
    app = LABEL.get(src, src)
    title = h.get("title") or title_of(items) or "对话"
    cwd = h.get("cwd") or ""
    L = [f"# {title}", "",
         f"> 这是在 {app} 里的一段对话（会话 `{h.get('id') or ''}`{('，工作目录 `' + cwd + '`') if cwd else ''}），"
         f"由 CC Session Sync 导出给 {LABEL.get(target, target)} 当上下文。",
         "> 工具调用只留了一行摘要；接着做之前先看一下文件 / git 的当前状态。", ""]
    n = 0
    for e in entries(items):
        t = (e.get("text") or "").rstrip()
        if not t:
            continue
        if e["role"] == "user":
            L += ["## 用户", "", t, ""]
            n += 1
        elif e["role"] == "assistant":
            L += [f"## {app}", "", t, ""]
            n += 1
        else:
            L += [f"- 〔工具〕`{' '.join(t.split())[:200]}`", ""]
    L += ["---", "", "（导出到此为止。请先读完上面的对话，再接着做最后一个没做完的事。）", ""]
    return {"text": "\n".join(L), "title": title, "cwd": cwd, "msgs": n, "app": app}

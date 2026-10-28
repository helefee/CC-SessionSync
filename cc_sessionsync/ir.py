# -*- coding: utf-8 -*-
"""会话转换的公共中间格式（0.2.7）：一个会话 = 头 + 一串条目，和应用无关。

头：{"format": "ccss-ir/1", "agent": 来源应用, "id", "title", "cwd", "created"(毫秒), "updated", "model", "source", ...}
条目：{"role": "user" | "assistant", "kind": "text" | "thinking" | "tool", "text", "tool", "input", "output", "status",
      "error", "ts"(毫秒), "images"(张数), "model"}
Cursor 的镜像（ccss-cursor/1）就是这个格式的前身，同样认。

所有「X → Y」都是：X 的读取器 → 中间格式 → Y 的写入器（或给 Y 的上下文文件）。这里放和应用无关的部分：
读写中间格式、只读查看、拼 Claude Code / Codex / OpenCode 的记录、拼上下文 Markdown。会丢 / 会变的东西都写在各函数里。"""
from __future__ import annotations

import base64
import json
import random
import re
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
            if it.get("files"):
                s = (s + "\n" if s else "") + f"[附件 ×{it['files']}]"
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
            "images": sum(int(i.get("images") or 0) for i in items), "files": sum(int(i.get("files") or 0) for i in items)}


# ---------- 图片（0.4.1：能带的带过去） ----------

IMG_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
IMG_MAX = 5 * 1024 * 1024            # 单张（解码后）：Claude 的上限是 5 MB
IMG_TOTAL = 30 * 1024 * 1024         # 一个会话里带过去的图片合计


def img(mt, data) -> dict | None:
    """(类型, base64 字符串或原始字节) → {"mt", "b64"}；类型不认识、太大、解不开 → None。"""
    mt = str(mt or "").lower().split(";")[0].strip()
    if mt == "image/jpg":
        mt = "image/jpeg"
    if mt not in IMG_TYPES or not data:
        return None
    try:
        if isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        else:
            raw = base64.b64decode(str(data), validate=True)
    except Exception:
        return None
    if not raw or len(raw) > IMG_MAX:
        return None
    return {"mt": mt, "b64": base64.b64encode(raw).decode("ascii")}


def img_url(url) -> dict | None:
    """data:image/png;base64,… → {"mt", "b64"}；网址 / 本机路径不算（带不过去）。"""
    m = re.match(r"^data:([\w/+.-]+);base64,(.*)$", str(url or ""), re.S)
    return img(m.group(1), m.group(2).strip()) if m else None


def data_url(im: dict) -> str:
    return f"data:{im['mt']};base64,{im['b64']}"


# ---------- 拼成一轮一轮（各写入器共用） ----------

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


def tool_text(src: str, it: dict, out_lines: int = 20, out_chars: int = 2000) -> str:
    a = it.get("input")
    a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False, indent=1)
    s = f"〔{LABEL.get(src, src)} 调用了 {it.get('tool')}〕" + ("（出错）" if it.get("status") == "error" or it.get("error") else "") + \
        "\n" + fence(_clip(a or "", 30, 3000))
    o = it.get("output") or it.get("error") or ""
    if o:
        s += "\n输出：\n" + fence(_clip(str(o), out_lines, out_chars))
    return s


NATIVE_REASONING = ("dsh", "opencode")       # 这两家的思考就是纯文字块，可以原样放；Claude / Codex 的要签名 / 加密，只能当文字


def turns(h: dict, items: list[dict], reasoning: bool = False, out_lines: int = 20, out_chars: int = 2000,
          intro: bool = True, target: str = "claude", native: bool = False, opencode_major: int = 2) -> tuple[list[list], dict]:
    """→ ([[role, [段…], ts毫秒, model]], 统计)。相邻同角色的并成一轮。

    native=False（老样子）：段都是文字——工具调用和结果变成文字（结果截短），思考缺省丢（reasoning=True 当文字带上），图片换成占位。
    native=True（0.4.1）：段可以是 dict——
      {"type": "tool", "name", "input", "output", "error"}：目标应用里有对应工具的（toolmap），按目标的工具名 / 参数写；
      {"type": "image", "mt", "b64"}：用户贴的图有原图数据的，原样带（单张 ≤5 MB，合计 ≤30 MB）；
      {"type": "reasoning", "text"}：reasoning=True 且目标是 dsh / OpenCode。
      别的照旧当文字。"""
    from . import toolmap
    src = h.get("agent") or "claude"
    lab = h.get("source_name") or LABEL.get(src, src)
    sid, cwd = h.get("id") or "", h.get("cwd") or ""
    title = h.get("title") or title_of(items) or "对话"
    st = {"user": 0, "assistant": 0, "tools": 0, "tools_native": 0, "reasoning_dropped": 0, "reasoning_kept": 0, "reasoning_native": 0,
          "images": 0, "images_kept": 0}
    out: list[list] = []
    budget = [IMG_TOTAL]

    def push(role, part, ts, model=None):
        if not part:
            return
        if out and out[-1][0] == role:
            out[-1][1].append(part)
            out[-1][3] = out[-1][3] or model
        else:
            out.append([role, [part], ts, model])

    if intro:
        how = (f"{LABEL.get(src, src)} 里跑过的工具，{LABEL.get(target, target)} 有对应工具的按原样记成工具调用，别的只留了文字摘要；输出是截短的"
               if native else f"{LABEL.get(src, src)} 里跑过的工具只留了文字摘要，输出是截短的")
        push("user", f"（以下是从 {lab} 导入的对话「{title}」，{LABEL.get(src, src)} 会话 {sid}" + (f"，工作目录 {cwd}" if cwd else "") +
             f"。{how}；接着做之前先看一下文件 / git 的当前状态。）", h.get("created"))
    for it in items:
        ts = it.get("ts")
        if it["role"] == "user":
            st["user"] += 1
            t = it.get("text") or ""
            n_img = int(it.get("images") or 0)
            st["images"] += n_img
            kept = []
            if native:
                for im in it.get("image_data") or []:
                    im = img(im.get("mt"), im.get("b64")) if isinstance(im, dict) else None
                    if im and len(im["b64"]) * 3 // 4 <= budget[0]:
                        budget[0] -= len(im["b64"]) * 3 // 4
                        kept.append(im)
            left = max(n_img - len(kept), 0)
            if left:
                t = (t + "\n" if t else "") + f"[图片 ×{left}，没有带过来]"
            if it.get("files"):
                st["files"] = st.get("files", 0) + int(it["files"])
                t = (t + "\n" if t else "") + f"[附件 ×{it['files']}，没有带过来]"
            push("user", t, ts)
            for im in kept:
                st["images_kept"] += 1
                push("user", {"type": "image", "mt": im["mt"], "b64": im["b64"]}, ts)
        elif it.get("kind") == "text":
            st["assistant"] += 1
            push("assistant", it.get("text") or "", ts, it.get("model"))
        elif it.get("kind") == "thinking":
            if reasoning and it.get("text"):
                st["reasoning_kept"] += 1
                if native and target in NATIVE_REASONING:
                    st["reasoning_native"] += 1
                    push("assistant", {"type": "reasoning", "text": it["text"]}, ts, it.get("model"))
                else:
                    push("assistant", f"〔{LABEL.get(src, src)} 里的思考〕\n" + it["text"], ts, it.get("model"))
            else:
                st["reasoning_dropped"] += 1
        elif it.get("kind") == "tool":
            st["tools"] += 1
            nat = toolmap.native(target, it.get("tool"), it.get("input"), opencode_major) if native else None
            if nat:
                st["tools_native"] += 1
                err = it.get("status") == "error" or bool(it.get("error"))
                o = it.get("output") or it.get("error") or ""
                push("assistant", {"type": "tool", "name": nat[0], "input": nat[1], "src_name": it.get("tool"), "error": err,
                                   "output": _clip(str(o), out_lines, out_chars) if o else ""}, ts, it.get("model"))
            else:
                push("assistant", tool_text(src, it, out_lines, out_chars), ts, it.get("model"))
    if not out:
        push("user", f"（{lab} 的这个会话是空的。）", h.get("created"))
    if out[-1][0] == "user":
        push("assistant", f"（{LABEL.get(src, src)} 那边到这里还没有回复。导入到此为止。）", out[-1][2])
    return out, dict(st, turns=len(out), title=title, user_turns=sum(1 for t in out if t[0] == "user"))


def _texts(parts) -> str:
    return "\n\n".join(p if isinstance(p, str) else p["text"] for p in parts
                       if isinstance(p, str) and p or isinstance(p, dict) and p.get("type") == "reasoning")


def _no_output(p: dict) -> str:
    return p.get("output") or ("（出错，没有输出）" if p.get("error") else "（没有输出）")


def _rid(prefix: str, n: int = 24) -> str:
    return prefix + "".join(random.choice(_B62) for _ in range(n))


# ---------- → Claude Code ----------

def build_claude(h: dict, items: list[dict], new_sid: str, version: str, title: str | None = None, reasoning: bool = False,
                 out_lines: int = 20, out_chars: int = 2000, cwd: str | None = None, native: bool = True) -> dict:
    """Claude Code 的会话记录。native=True：能对上 Claude Code 工具的调用写成 tool_use + 紧跟的 tool_result（Claude 要求结果
    就在下一条用户消息里），图片写成 base64 image 块；思考只能当文字（Claude 的思考要签名）。"""
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning, out_lines, out_chars, target="claude", native=native)
    title = title or st["title"]
    cwd = cwd or h.get("cwd") or ""
    recs, state = [], {"parent": None, "last": None}

    def rec(role, content, ts):
        u = str(uuid.uuid4())
        ts = ts or state["last"] or h.get("created")
        state["last"] = ts
        r = {"parentUuid": state["parent"], "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": new_sid,
             "version": version, "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": iso_ms(ts)}
        if role == "user":
            r["message"] = {"role": "user", "content": content}
        else:
            r["message"] = {"id": f"msg_{src}import_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": f"{src}-import", "content": content, "stop_reason": "tool_use" if any(
                                b.get("type") == "tool_use" for b in content) else "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        state["parent"] = u

    pending: list[dict] = []                     # 还没写出去的 tool_result（跟着下一条用户消息走）
    for role, parts, ts, _m in T:
        if role == "user":
            blocks, texts = list(pending), []
            pending = []
            for p in parts:
                if isinstance(p, str):
                    texts.append(p)
                elif p.get("type") == "image":
                    if texts:
                        blocks.append({"type": "text", "text": "\n\n".join(texts)})
                        texts = []
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": p["mt"], "data": p["b64"]}})
            if texts:
                blocks.append({"type": "text", "text": "\n\n".join(texts)})
            only_text = len(blocks) == 1 and blocks[0]["type"] == "text"
            rec("user", blocks[0]["text"] if only_text else blocks, ts)
            continue
        cur, texts = [], []
        for p in parts:
            if isinstance(p, str) or p.get("type") == "reasoning":
                if cur and cur[-1]["type"] == "tool_use":         # 工具调用后面又有文字：先把这批调用和结果写出去
                    rec("assistant", cur, ts)
                    rec("user", pending, ts)
                    cur, pending = [], []
                texts.append(p if isinstance(p, str) else p["text"])
            elif p.get("type") == "tool":
                if texts:
                    cur.append({"type": "text", "text": "\n\n".join(texts)})
                    texts = []
                tid = _rid("toolu_imp")
                cur.append({"type": "tool_use", "id": tid, "name": p["name"], "input": p["input"]})
                pending.append({"type": "tool_result", "tool_use_id": tid, "content": _no_output(p), "is_error": bool(p.get("error"))})
        if texts:
            cur.append({"type": "text", "text": "\n\n".join(texts)})
        if cur:
            rec("assistant", cur, ts)
    if pending:                                   # 最后停在工具调用上：补上结果和一句收尾，接着聊时历史是完整的
        rec("user", pending, state["last"])
        rec("assistant", [{"type": "text", "text": f"（{LABEL.get(src, src)} 那边到这里为止。导入到此为止。）"}], state["last"])
    full = PREFIX.get(src, "") + title
    recs.append({"type": "custom-title", "customTitle": full, "sessionId": new_sid})
    return {"records": recs, "cwd": cwd, "title": full, "turns": len(T), "stats": st}


# ---------- → Codex（rollout-*.jsonl） ----------

def build_codex(h: dict, items: list[dict], tid: str, cli_version: str, title: str | None = None, reasoning: bool = False,
                cwd: str | None = None, now_ms: int | None = None, native: bool = True) -> dict:
    """Codex 的 rollout：session_meta + response_item（用户 input_text / input_image，助手 output_text）+ 对应的 event_msg
    （Codex 用它列「用户说了什么」）。native=True：命令行调用写成 function_call exec_command{cmd} + function_call_output
    （和 Codex 自己记的一样），图片写成 input_image（data URL）；别的工具 Codex 没有对应的，照旧当文字；思考只能当文字。"""
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning, target="codex", native=native)
    title = title or st["title"]
    cwd = cwd or h.get("cwd") or ""
    t0 = now_ms or int(time.time() * 1000)
    first = to_ms(T[0][2]) or t0
    meta = {"id": tid, "timestamp": iso_ms(first), "cwd": cwd, "originator": "cc_sessionsync", "cli_version": cli_version,
            "instructions": None, "source": "cli", "model_provider": "openai", "thread_source": "user"}
    L = [{"timestamp": iso_ms(first), "type": "session_meta", "payload": meta}]
    last = first

    def say(ts, texts):
        text = "\n\n".join(t for t in texts if t)
        if text:
            L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": text}]}})
            L.append({"timestamp": iso_ms(ts), "type": "event_msg", "payload": {"type": "agent_message", "message": text}})

    for role, parts, ts, _m in T:
        ts = max(to_ms(ts) or last, last)
        last = ts
        if role == "user":
            text = "\n\n".join(p for p in parts if isinstance(p, str) and p)
            ims = [p for p in parts if isinstance(p, dict) and p.get("type") == "image"]
            content = ([{"type": "input_text", "text": text}] if text else []) + [{"type": "input_image", "image_url": data_url(p)} for p in ims]
            L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "message", "role": "user", "content": content}})
            L.append({"timestamp": iso_ms(ts), "type": "event_msg", "payload": {"type": "user_message", "message": text,
                                                                                  "images": [data_url(p) for p in ims]}})
            continue
        texts = []
        for p in parts:
            if isinstance(p, str) or p.get("type") == "reasoning":
                texts.append(p if isinstance(p, str) else p["text"])
            elif p.get("type") == "tool":
                say(ts, texts)
                texts = []
                cid = _rid("call_imp")
                L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "function_call", "name": p["name"],
                          "arguments": json.dumps(p["input"], ensure_ascii=False), "call_id": cid}})
                L.append({"timestamp": iso_ms(ts), "type": "response_item", "payload": {"type": "function_call_output", "call_id": cid,
                          "output": _no_output(p)}})
        say(ts, texts)
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


def _oc_steps(parts) -> list[list]:
    """助手的一轮按「工具调用」切成几步：每步 = [文字 / 思考…, 工具…]，和 OpenCode 自己一步一条消息一样。"""
    steps, cur = [], []
    for p in parts:
        is_tool = isinstance(p, dict) and p.get("type") == "tool"
        if not is_tool and cur and isinstance(cur[-1], dict) and cur[-1].get("type") == "tool":
            steps.append(cur)
            cur = []
        cur.append(p)
    if cur:
        steps.append(cur)
    return steps


def build_opencode(h: dict, items: list[dict], title: str | None = None, reasoning: bool = False, cwd: str | None = None,
                   version: int = 2, now_ms: int | None = None, native: bool = True) -> dict:
    """version=2：OpenCode 2.x 的 {info, messages:[{type:user|assistant, …}]}；version=1：1.x 的 {info, messages:[{info, parts}]}。
    助手消息记成模型 cc-sessionsync/<来源>-import、0 token。native=True：能对上 OpenCode 工具的调用写成 tool 部件（状态 completed / error，
    带输入和输出），用户贴的图写成文件（2.x files[] / 1.x file 部件，data URL），思考（打开「带上思考」时）写成 reasoning 部件。"""
    src = h.get("agent") or "claude"
    T, st = turns(h, items, reasoning, target="opencode", native=native, opencode_major=version)
    title = title or st["title"]
    full = PREFIX.get(src, "") + title
    cwd = cwd or h.get("cwd") or ""
    t0 = now_ms or int(time.time() * 1000)
    sid = oc_id("ses", t0, 0, descending=True)
    zero = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    model = {"id": f"{src}-import", "providerID": "cc-sessionsync"}
    msgs, last, prev_user = [], None, None
    n = [0]

    def nid(prefix):
        n[0] += 1
        return oc_id(prefix, t0, n[0])

    for role, parts, ts, _m in T:
        ts = max(to_ms(ts) or last or t0, last or 0)
        last = ts
        if role == "user":
            text = "\n\n".join(p for p in parts if isinstance(p, str) and p)
            ims = [p for p in parts if isinstance(p, dict) and p.get("type") == "image"]
            mid = nid("msg")
            if version == 2:
                msgs.append({"id": mid, "time": {"created": ts}, "type": "user", "text": text,
                             "files": [{"data": p["b64"], "mime": p["mt"], "source": {"type": "inline"},
                                        "name": f"image-{i + 1}.{p['mt'].split('/')[-1]}"} for i, p in enumerate(ims)]})
            else:
                info = {"id": mid, "sessionID": sid, "role": "user", "time": {"created": ts}, "agent": "build",
                        "model": {"providerID": model["providerID"], "modelID": model["id"]}}
                prev_user = mid
                ps = [{"id": nid("prt"), "sessionID": sid, "messageID": mid, "type": "text", "text": text, "time": {"start": ts, "end": ts}}] if text else []
                ps += [{"id": nid("prt"), "sessionID": sid, "messageID": mid, "type": "file", "mime": p["mt"],
                        "filename": f"image-{i + 1}.{p['mt'].split('/')[-1]}", "url": data_url(p)} for i, p in enumerate(ims)]
                msgs.append({"info": info, "parts": ps})
            continue
        steps = _oc_steps(parts)
        for k, step in enumerate(steps):
            mid = nid("msg")
            has_tool = any(isinstance(p, dict) and p.get("type") == "tool" for p in step)
            fin = "tool-calls" if has_tool and k < len(steps) - 1 else "stop"
            if version == 2:
                content = []
                for p in step:
                    if isinstance(p, str):
                        content.append({"type": "text", "text": p})
                    elif p["type"] == "reasoning":
                        content.append({"type": "reasoning", "text": p["text"]})
                    elif p["type"] == "tool":
                        state = {"status": "error", "input": p["input"], "error": {"type": "tool.execution", "message": _no_output(p)}} \
                            if p.get("error") else {"status": "completed", "input": p["input"], "content": [{"type": "text", "text": _no_output(p)}],
                                                    "metadata": {}}
                        content.append({"type": "tool", "id": _rid("call_imp"), "name": p["name"], "executed": False, "state": state,
                                        "time": {"created": ts, "ran": ts, "completed": ts}})
                msgs.append({"id": mid, "time": {"created": ts, "completed": ts}, "type": "assistant", "agent": "build", "model": model,
                             "content": content, "finish": fin, "cost": 0, "tokens": zero})
            else:
                info = {"id": mid, "sessionID": sid, "role": "assistant", "parentID": prev_user or mid, "mode": "build", "agent": "build",
                        "path": {"cwd": cwd, "root": cwd}, "cost": 0, "tokens": dict(zero, total=0), "modelID": model["id"],
                        "providerID": model["providerID"], "time": {"created": ts, "completed": ts}, "finish": fin}
                ps = []
                for p in step:
                    base = {"id": nid("prt"), "sessionID": sid, "messageID": mid}
                    if isinstance(p, str):
                        ps.append(dict(base, type="text", text=p, time={"start": ts, "end": ts}))
                    elif p["type"] == "reasoning":
                        ps.append(dict(base, type="reasoning", text=p["text"], time={"start": ts, "end": ts}))
                    elif p["type"] == "tool":
                        state = {"status": "error", "input": p["input"], "error": _no_output(p), "time": {"start": ts, "end": ts}} if p.get("error") \
                            else {"status": "completed", "input": p["input"], "output": _no_output(p), "title": brief(p["input"]),
                                  "metadata": {}, "time": {"start": ts, "end": ts}}
                        ps.append(dict(base, type="tool", callID=_rid("call_imp"), tool=p["name"], state=state))
                msgs.append({"info": info, "parts": ps})
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

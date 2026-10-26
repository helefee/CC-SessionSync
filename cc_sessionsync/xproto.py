# -*- coding: utf-8 -*-
"""协议转换（纯函数，不联网）：Anthropic Messages ⇄ OpenAI Responses ⇄ OpenAI Chat Completions。

中枢是 Anthropic Messages 的请求格式：
  请求：客户端协议 → Anthropic 形式 → 上游协议（同协议直通不走这里）。
  回复：上游的 JSON / SSE → 中间事件（IR）→ 客户端协议的 JSON / SSE。

中间事件（tuple）：
  ("start", {"id", "model"})
  ("block", {"type": "text" | "thinking" | "redacted" | "tool", "id", "name", "data"})
  ("text", s) / ("think", s) / ("sig", s) / ("args", s)
  ("end", {})
  ("finish", {"stop": end_turn|max_tokens|tool_use|stop_sequence|refusal, "usage": {...}})
  ("error", {"status", "type", "message", "code"})
用量统一成 {"input": 不含缓存的输入, "cache_read", "cache_write", "output", "reasoning"}。

思考 / 推理跨协议往返：签名 / encrypted_content 用「ccss1.」+ base64(JSON) 包起来，下一轮客户端原样带回来时再拆开：
  OpenAI 的 reasoning（encrypted_content）→ Claude 的 thinking.signature；Claude 的 thinking + signature → Codex 的 reasoning.encrypted_content。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid

# ---------------------------------------------------------------- 小工具

def uid(prefix: str) -> str:
    return f"{prefix}_ccss{uuid.uuid4().hex[:24]}"


def sse(event, obj) -> bytes:
    data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return ((f"event: {event}\n" if event else "") + f"data: {data}\n\n").encode("utf-8")


def sse_events(lines):
    """bytes 行的迭代器 → (event, data 字符串)。"""
    ev, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                yield ev, "\n".join(data)
            ev, data = None, []
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(":")
        v = v[1:] if v.startswith(" ") else v
        if k == "event":
            ev = v
        elif k == "data":
            data.append(v)
    if data:
        yield ev, "\n".join(data)


def pack(d: dict) -> str:
    return "ccss1." + base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def unpack(s):
    if not isinstance(s, str) or not s.startswith("ccss1."):
        return None
    b = s[6:]
    try:
        return json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:
        return None


def _jload(s):
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"input": v}
    except ValueError:
        return {"input": s}


def _text_of(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    out = []
    for x in c:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            t = x.get("type")
            if t in ("text", "input_text", "output_text") or (t is None and "text" in x):
                out.append(str(x.get("text") or ""))
            elif t == "refusal":
                out.append(str(x.get("refusal") or ""))
            elif t == "tool_reference":
                out.append(f"[tool loaded: {x.get('tool_name') or x.get('name') or ''}]")
    return "\n".join(o for o in out if o)


NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_name(n: str) -> str:
    n = str(n or "tool")
    if NAME_OK.match(n):
        return n
    s = re.sub(r"[^A-Za-z0-9_-]", "_", n)
    if len(s) > 64:
        s = s[:55] + "_" + hashlib.sha1(n.encode()).hexdigest()[:8]
    return s or "tool"


class Names:
    """工具名编码：目标协议不认的字符 / 太长的名字换成安全的名字，回来时再换回去；Responses 的 namespace 工具摊平成 ns__name。"""

    def __init__(self):
        self.back = {}

    def enc(self, name, namespace=None, custom=False) -> str:
        full = f"{namespace}__{name}" if namespace else name
        s = safe_name(full)
        self.back.setdefault(s, {"name": name, "namespace": namespace, "custom": custom})
        if custom:
            self.back[s]["custom"] = True
        return s

    def dec(self, s) -> dict:
        return self.back.get(s) or {"name": s, "namespace": None, "custom": False}


def _clean_schema(s):
    if not isinstance(s, dict):
        return {"type": "object", "properties": {}}
    s = {k: v for k, v in s.items() if k != "$schema"}
    if s.get("type") != "object":
        s = {"type": "object", "properties": {"input": s}} if s else {"type": "object", "properties": {}}
    return s


def _data_url(src):
    if not isinstance(src, dict):
        return None
    if src.get("type") == "base64":
        return f"data:{src.get('media_type') or 'image/png'};base64,{src.get('data') or ''}"
    if src.get("type") == "url":
        return src.get("url")
    return None


def _src_from_url(u: str) -> dict:
    m = re.match(r"^data:([^;,]+);base64,(.*)$", u or "", re.S)
    if m:
        return {"type": "base64", "media_type": m.group(1), "data": m.group(2)}
    return {"type": "url", "url": u}


def _blocks(c) -> list:
    if c is None:
        return []
    if isinstance(c, str):
        return [{"type": "text", "text": c}] if c else []
    return [x if isinstance(x, dict) else {"type": "text", "text": str(x)} for x in c]


def _system_text(sys_) -> str:
    if isinstance(sys_, str):
        return sys_
    return "\n\n".join(str(b.get("text") or "") for b in (sys_ or []) if isinstance(b, dict) and b.get("type", "text") == "text")


def _no_result(i):
    return {"type": "tool_result", "tool_use_id": i, "content": "(no result)", "is_error": True}


def fix_anthropic_messages(msgs: list) -> list:
    """合并相邻同角色；每个 tool_use 后面的 user 消息里都要有对应的 tool_result（缺的补一个出错结果），没有前文的 tool_result 变成文字。"""
    out = []
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        bl = _blocks(m.get("content"))
        if not bl:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(bl)
        else:
            out.append({"role": role, "content": list(bl)})
    fixed, pending = [], []
    for m in out:
        if m["role"] == "assistant":
            if pending:
                fixed.append({"role": "user", "content": [_no_result(i) for i in pending]})
                pending = []
            tus = [b for b in m["content"] if b.get("type") == "tool_use"]
            if tus:
                m = {"role": "assistant", "content": [b for b in m["content"] if b.get("type") != "tool_use"] + tus}
            pending = [b.get("id") for b in tus]
            fixed.append(m)
        else:
            res = [b for b in m["content"] if b.get("type") == "tool_result"]
            rest = [b for b in m["content"] if b.get("type") != "tool_result"]
            have = {b.get("tool_use_id") for b in res}
            ok_res = [b for b in res if b.get("tool_use_id") in pending]
            for b in res:
                if b.get("tool_use_id") not in pending:
                    c = b.get("content")
                    rest.insert(0, {"type": "text", "text": "[tool result] " + (c if isinstance(c, str) else _text_of(c))})
            content = ok_res + [_no_result(i) for i in pending if i not in have] + rest
            pending = []
            if content:
                if fixed and fixed[-1]["role"] == "user":
                    fixed[-1]["content"].extend(content)
                else:
                    fixed.append({"role": "user", "content": content})
    if pending:
        fixed.append({"role": "user", "content": [_no_result(i) for i in pending]})
    if fixed and fixed[0]["role"] != "user":
        fixed.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return fixed


# ---------------------------------------------------------------- 请求：Responses / Chat → Anthropic 形式

EFFORT_BUDGET = {"low": 4000, "medium": 10000, "high": 20000, "xhigh": 32000, "max": 32000}


def _reminder(tx):
    return {"role": "user", "content": [{"type": "text", "text": f"<system-reminder>\n{tx}\n</system-reminder>"}]}


def r2a_request(b: dict, model: str, opt=None):
    opt = opt or {}
    names = Names()
    ctx = {"names": names, "client": "responses"}
    tools = []
    for t in b.get("tools") or []:
        ty = t.get("type")
        if ty == "function":
            tools.append({"name": names.enc(t["name"]), "description": t.get("description") or "", "input_schema": _clean_schema(t.get("parameters"))})
        elif ty == "namespace":
            for f in t.get("tools") or []:
                if f.get("type", "function") == "function":
                    pre = (t.get("description") or "") + " — " if t.get("description") else ""
                    tools.append({"name": names.enc(f["name"], t.get("name")), "description": pre + (f.get("description") or ""),
                                  "input_schema": _clean_schema(f.get("parameters"))})
        elif ty == "custom":
            fmt = t.get("format") or {}
            desc = t.get("description") or ""
            if fmt.get("definition"):
                desc += "\n\nInput format (" + str(fmt.get("syntax") or fmt.get("type") or "text") + "):\n" + str(fmt.get("definition"))[:4000]
            tools.append({"name": names.enc(t["name"], custom=True), "description": desc,
                          "input_schema": {"type": "object", "properties": {"input": {"type": "string", "description": "The raw freeform input for this tool."}},
                                           "required": ["input"]}})
        # web_search / local_shell / file_search / image_generation 等内置工具：Anthropic 没有对应，丢掉（README 里写了）
    system, msgs, seen_user = [], [], False
    if b.get("instructions"):
        system.append(str(b["instructions"]))
    inp = b.get("input")
    items = [{"type": "message", "role": "user", "content": inp}] if isinstance(inp, str) else (inp or [])
    for it in items:
        ty = it.get("type") or ("message" if it.get("role") else None)
        if ty == "message":
            role, c = it.get("role"), it.get("content")
            if role in ("system", "developer"):
                tx = c if isinstance(c, str) else _text_of(c)
                if not seen_user:
                    system.append(tx)
                else:
                    msgs.append(_reminder(tx))
                continue
            bl = []
            for x in ([{"type": "input_text", "text": c}] if isinstance(c, str) else (c or [])):
                xt = x.get("type")
                if xt in ("input_text", "output_text", "text"):
                    if x.get("text"):
                        bl.append({"type": "text", "text": x["text"]})
                elif xt == "refusal":
                    bl.append({"type": "text", "text": x.get("refusal") or ""})
                elif xt == "input_image":
                    if x.get("image_url"):
                        bl.append({"type": "image", "source": _src_from_url(x["image_url"])})
                elif xt == "input_file":
                    s = _src_from_url(x.get("file_data") or "") if x.get("file_data") else None
                    if s and s.get("type") == "base64" and "pdf" in (s.get("media_type") or ""):
                        bl.append({"type": "document", "source": s})
                    else:
                        bl.append({"type": "text", "text": f"[file {x.get('filename') or ''} not forwarded]"})
            if role == "user":
                seen_user = True
            if bl:
                msgs.append({"role": "assistant" if role == "assistant" else "user", "content": bl})
        elif ty == "reasoning":
            d = unpack(it.get("encrypted_content"))
            if d and d.get("k") == "ant" and d.get("s"):
                msgs.append({"role": "assistant", "content": [{"type": "thinking", "thinking": d.get("t") or "", "signature": d["s"]}]})
            elif d and d.get("k") == "ant_r":
                msgs.append({"role": "assistant", "content": [{"type": "redacted_thinking", "data": d.get("d") or ""}]})
            elif d and d.get("k") == "rc":
                msgs.append({"role": "assistant", "content": [{"type": "thinking", "thinking": d.get("t") or "", "signature": pack({"k": "rc"})}]})
        elif ty in ("function_call", "custom_tool_call"):
            custom = ty == "custom_tool_call"
            nm = names.enc(it.get("name") or "tool", it.get("namespace"), custom=custom)
            iv = {"input": it.get("input") or ""} if custom else _jload(it.get("arguments"))
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": it.get("call_id") or it.get("id") or uid("toolu"), "name": nm, "input": iv}]})
        elif ty in ("function_call_output", "custom_tool_call_output"):
            o = it.get("output")
            if isinstance(o, list):
                cont = []
                for x in o:
                    if x.get("type") in ("input_text", "output_text", "text"):
                        cont.append({"type": "text", "text": x.get("text") or ""})
                    elif x.get("type") == "input_image" and x.get("image_url"):
                        cont.append({"type": "image", "source": _src_from_url(x["image_url"])})
                cont = cont or [{"type": "text", "text": "(empty)"}]
            else:
                cont = str(o if o is not None else "")
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": it.get("call_id"), "content": cont}]})
    msgs = fix_anthropic_messages(msgs)
    req = {"model": model, "messages": msgs, "max_tokens": int(b.get("max_output_tokens") or opt.get("max_tokens") or 32000)}
    if system:
        req["system"] = [{"type": "text", "text": "\n\n".join(s for s in system if s)}]
    if tools:
        req["tools"] = tools
        tc = b.get("tool_choice")
        if tc == "required":
            req["tool_choice"] = {"type": "any"}
        elif tc == "none":
            req["tool_choice"] = {"type": "none"}
        elif isinstance(tc, dict) and tc.get("name"):
            req["tool_choice"] = {"type": "tool", "name": names.enc(tc["name"], tc.get("namespace"))}
        if b.get("parallel_tool_calls") is False:
            req["tool_choice"] = dict(req.get("tool_choice") or {"type": "auto"}, disable_parallel_tool_use=True)
    _apply_thinking(req, (b.get("reasoning") or {}).get("effort") or opt.get("effort"), opt)
    if "thinking" not in req:
        for k in ("temperature", "top_p"):
            if b.get(k) is not None:
                req[k] = b[k]
    if b.get("stream"):
        req["stream"] = True
    if opt.get("cache", True):
        add_cache_marks(req)
    return req, ctx


def c2a_request(b: dict, model: str, opt=None):
    """Chat Completions 请求 → Anthropic 形式。"""
    opt = opt or {}
    names = Names()
    ctx = {"names": names, "client": "chat", "include_usage": bool((b.get("stream_options") or {}).get("include_usage"))}
    tools = [{"name": names.enc((t.get("function") or {}).get("name") or "tool"), "description": (t.get("function") or {}).get("description") or "",
              "input_schema": _clean_schema((t.get("function") or {}).get("parameters"))} for t in b.get("tools") or [] if t.get("type") == "function"]
    system, msgs, seen_user = [], [], False
    for m in b.get("messages") or []:
        role, c = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            tx = c if isinstance(c, str) else _text_of(c)
            if not seen_user:
                system.append(tx)
            else:
                msgs.append(_reminder(tx))
        elif role == "user":
            seen_user = True
            bl = []
            for x in ([{"type": "text", "text": c}] if isinstance(c, str) else (c or [])):
                if x.get("type") == "text" and x.get("text"):
                    bl.append({"type": "text", "text": x["text"]})
                elif x.get("type") == "image_url":
                    iu = x.get("image_url")
                    u = iu.get("url") if isinstance(iu, dict) else iu
                    if u:
                        bl.append({"type": "image", "source": _src_from_url(u)})
                elif x.get("type") == "file":
                    s = _src_from_url((x.get("file") or {}).get("file_data") or "")
                    if s.get("type") == "base64":
                        bl.append({"type": "document", "source": s})
            msgs.append({"role": "user", "content": bl})
        elif role == "assistant":
            bl = []
            if m.get("reasoning_content"):
                bl.append({"type": "thinking", "thinking": m["reasoning_content"], "signature": pack({"k": "rc"})})
            tx = c if isinstance(c, str) else _text_of(c)
            if tx:
                bl.append({"type": "text", "text": tx})
            for tc in m.get("tool_calls") or []:
                f = tc.get("function") or {}
                bl.append({"type": "tool_use", "id": tc.get("id") or uid("toolu"), "name": names.enc(f.get("name") or "tool"), "input": _jload(f.get("arguments"))})
            msgs.append({"role": "assistant", "content": bl})
        elif role == "tool":
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": c if isinstance(c, str) else _text_of(c)}]})
    msgs = fix_anthropic_messages(msgs)
    req = {"model": model, "messages": msgs, "max_tokens": int(b.get("max_completion_tokens") or b.get("max_tokens") or opt.get("max_tokens") or 32000)}
    if system:
        req["system"] = [{"type": "text", "text": "\n\n".join(system)}]
    if tools:
        req["tools"] = tools
        tc = b.get("tool_choice")
        if tc == "required":
            req["tool_choice"] = {"type": "any"}
        elif tc == "none":
            req["tool_choice"] = {"type": "none"}
        elif isinstance(tc, dict) and (tc.get("function") or {}).get("name"):
            req["tool_choice"] = {"type": "tool", "name": names.enc(tc["function"]["name"])}
        if b.get("parallel_tool_calls") is False:
            req["tool_choice"] = dict(req.get("tool_choice") or {"type": "auto"}, disable_parallel_tool_use=True)
    if b.get("stop"):
        req["stop_sequences"] = [b["stop"]] if isinstance(b["stop"], str) else list(b["stop"])[:4]
    _apply_thinking(req, b.get("reasoning_effort") or opt.get("effort"), opt)
    if "thinking" not in req:
        for k in ("temperature", "top_p"):
            if b.get(k) is not None:
                req[k] = b[k]
    if b.get("stream"):
        req["stream"] = True
    if opt.get("cache", True):
        add_cache_marks(req)
    return req, ctx


def _apply_thinking(req: dict, effort, opt: dict):
    mode = opt.get("thinking") or "adaptive"
    if not effort or effort in ("none", "minimal") or mode == "off":
        _strip_thinking(req)
        return
    # 正在进行的工具循环里，带 tool_use 的 assistant 消息开头没有思考块 → 这一轮不开思考（否则 Anthropic 会拒绝）
    idx = [i for i, m in enumerate(req["messages"]) if m["role"] == "user" and any(x.get("type") != "tool_result" for x in m["content"])]
    for m in req["messages"][(idx[-1] if idx else -1) + 1:]:
        if m["role"] == "assistant" and any(x.get("type") == "tool_use" for x in m["content"]) \
                and m["content"][0].get("type") not in ("thinking", "redacted_thinking"):
            _strip_thinking(req)
            return
    if mode == "budget":
        if req["max_tokens"] <= 3072:
            _strip_thinking(req)
            return
        req["thinking"] = {"type": "enabled", "budget_tokens": max(1024, min(EFFORT_BUDGET.get(effort, 10000), req["max_tokens"] - 2048))}
    else:
        req["thinking"] = {"type": "adaptive"}
        req["output_config"] = {"effort": {"max": "max", "xhigh": "xhigh", "high": "high", "low": "low"}.get(effort, "medium")}
    tc = req.get("tool_choice")
    if tc and tc.get("type") in ("any", "tool"):          # 开思考时只允许 auto / none
        req["tool_choice"] = {"type": "auto", **({"disable_parallel_tool_use": True} if tc.get("disable_parallel_tool_use") else {})}


def _strip_thinking(req: dict):
    for m in req["messages"]:
        if m["role"] == "assistant":
            m["content"] = [x for x in m["content"] if x.get("type") not in ("thinking", "redacted_thinking")] or [{"type": "text", "text": "(…)"}]


def add_cache_marks(req: dict):
    """Anthropic 的提示缓存要显式标：system 末尾、工具表末尾、最后一条 user 消息的最后一块（最多 4 个断点，这里用 3 个）。"""
    def mark(x):
        if isinstance(x, dict) and x.get("type") not in ("thinking", "redacted_thinking"):
            x["cache_control"] = {"type": "ephemeral"}
            return True
        return False
    if req.get("system"):
        mark(req["system"][-1])
    if req.get("tools"):
        mark(req["tools"][-1])
    for m in reversed(req["messages"]):
        if m["role"] == "user" and m["content"]:
            for x in reversed(m["content"]):
                if mark(x):
                    break
            break


# ---------------------------------------------------------------- 请求：Anthropic 形式 → Responses / Chat

REASONING_RE = re.compile(r"^(o\d|gpt-5|gpt-6|codex)", re.I)


def is_reasoning(model: str, opt: dict) -> bool:
    r = opt.get("reasoning") or "auto"
    if r in ("on", "off"):
        return r == "on"
    return bool(REASONING_RE.match((model or "").split("/")[-1]))


def _anth_effort(b: dict, opt: dict):
    """Claude Code 这次请求要的推理强度（/effort、/model 里选的、--effort、effortLevel）→ OpenAI reasoning.effort。
    请求里没说的时候才用供应商 / 卡片设的缺省值。"""
    got = _anth_effort_req(b)
    return got if got is not None else (opt.get("effort") or None)


def _anth_effort_req(b: dict):
    th = b.get("thinking") or {}
    eff = (b.get("output_config") or {}).get("effort")
    if th.get("type") == "enabled":
        bt = int(th.get("budget_tokens") or 0)
        return "low" if bt <= 4096 else ("medium" if bt <= 16000 else "high")
    if th.get("type") == "adaptive" or eff:
        return {"max": "xhigh", "xhigh": "xhigh", "high": "high", "low": "low"}.get(eff or "medium", "medium")
    if th.get("type") == "disabled":
        return "low"
    return None


def _anth_tools(b: dict, names: Names) -> list:
    out = []
    for t in b.get("tools") or []:
        if "input_schema" not in t and t.get("type") not in (None, "custom"):
            continue                                   # Anthropic 服务器端工具（web_search_… 等）：OpenAI 那边没有对应
        out.append((names.enc(t["name"]), t.get("description") or "", _clean_schema(t.get("input_schema"))))
    return out


def _tool_result_parts(c):
    """tool_result 的 content → (文字, [图片 data URL])"""
    if isinstance(c, str):
        return c, []
    txt, imgs = [], []
    for x in c or []:
        if x.get("type") == "image":
            u = _data_url(x.get("source"))
            if u:
                imgs.append(u)
        else:
            t = _text_of([x])
            if t:
                txt.append(t)
    return "\n".join(txt), imgs


def _omit(b):
    return (b.get("thinking") or {}).get("display") == "omitted"


def a2r_request(b: dict, model: str, opt=None):
    opt = opt or {}
    names = Names()
    ctx = {"names": names, "client": "anthropic", "omit_thinking": _omit(b)}
    reasoning = is_reasoning(model, opt)
    items = []
    for m in b.get("messages") or []:
        role = m.get("role")
        bl = _blocks(m.get("content"))
        if role == "system":
            items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": _text_of(bl)}]})
            continue
        if role == "assistant":
            buf = []

            def flush():
                if buf:
                    items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "".join(buf)}]})
                    buf.clear()
            for x in bl:
                t = x.get("type")
                if t == "text":
                    buf.append(x.get("text") or "")
                elif t == "thinking":
                    d = unpack(x.get("signature"))
                    if d and d.get("k") == "oai" and d.get("e") and reasoning:
                        flush()
                        it = {"type": "reasoning", "summary": [{"type": "summary_text", "text": x["thinking"]}] if x.get("thinking") else [],
                              "encrypted_content": d["e"]}
                        if d.get("id"):
                            it["id"] = d["id"]
                        items.append(it)
                elif t == "tool_use":
                    flush()
                    items.append({"type": "function_call", "call_id": x.get("id"), "name": names.enc(x.get("name") or "tool"),
                                  "arguments": json.dumps(x.get("input") or {}, ensure_ascii=False)})
            flush()
            continue
        parts = []

        def flush_user():
            if parts:
                items.append({"type": "message", "role": "user", "content": list(parts)})
                parts.clear()
        for x in bl:
            t = x.get("type")
            if t == "text":
                if x.get("text"):
                    parts.append({"type": "input_text", "text": x["text"]})
            elif t == "image":
                u = _data_url(x.get("source"))
                if u:
                    parts.append({"type": "input_image", "image_url": u, "detail": "auto"})
            elif t == "document":
                s = x.get("source") or {}
                if s.get("type") == "base64":
                    parts.append({"type": "input_file", "filename": x.get("title") or "document.pdf", "file_data": _data_url(s)})
                elif s.get("type") == "text":
                    parts.append({"type": "input_text", "text": s.get("data") or ""})
            elif t == "tool_result":
                flush_user()
                txt, imgs = _tool_result_parts(x.get("content"))
                if x.get("is_error"):
                    txt = "[tool error] " + txt
                out = ([{"type": "input_text", "text": txt}] + [{"type": "input_image", "image_url": u} for u in imgs]) if imgs else txt
                items.append({"type": "function_call_output", "call_id": x.get("tool_use_id"), "output": out})
        flush_user()
    req = {"model": model, "input": items, "store": False}
    st = _system_text(b.get("system"))
    if st:
        req["instructions"] = st
    tl = _anth_tools(b, names)
    if tl:
        req["tools"] = [{"type": "function", "name": n, "description": d, "parameters": s, "strict": False} for n, d, s in tl]
        tc = b.get("tool_choice") or {}
        if tc.get("type") == "any":
            req["tool_choice"] = "required"
        elif tc.get("type") == "none":
            req["tool_choice"] = "none"
        elif tc.get("type") == "tool" and tc.get("name"):
            req["tool_choice"] = {"type": "function", "name": names.enc(tc["name"])}
        req["parallel_tool_calls"] = not tc.get("disable_parallel_tool_use")
    if b.get("max_tokens"):
        req["max_output_tokens"] = int(b["max_tokens"])
    if reasoning:
        eff = _anth_effort(b, opt)
        req["reasoning"] = {"summary": "auto", **({"effort": eff} if eff else {})}
        req["include"] = ["reasoning.encrypted_content"]
    else:
        for k in ("temperature", "top_p"):
            if b.get(k) is not None:
                req[k] = b[k]
    u = (b.get("metadata") or {}).get("user_id")
    if u:
        req["prompt_cache_key"] = hashlib.sha256(str(u).encode()).hexdigest()[:32]
    if b.get("stream"):
        req["stream"] = True
    return req, ctx


def a2c_request(b: dict, model: str, opt=None):
    opt = opt or {}
    names = Names()
    ctx = {"names": names, "client": "anthropic", "omit_thinking": _omit(b)}
    reasoning = is_reasoning(model, opt)
    sysrole = "developer" if reasoning and not opt.get("system_role") else (opt.get("system_role") or "system")
    msgs = []
    st = _system_text(b.get("system"))
    if st:
        msgs.append({"role": sysrole, "content": st})
    for m in b.get("messages") or []:
        role = m.get("role")
        bl = _blocks(m.get("content"))
        if role == "system":
            msgs.append({"role": sysrole, "content": _text_of(bl)})
        elif role == "assistant":
            txt = "".join(x.get("text") or "" for x in bl if x.get("type") == "text")
            tcs = [{"id": x.get("id"), "type": "function", "function": {"name": names.enc(x.get("name") or "tool"),
                                                                        "arguments": json.dumps(x.get("input") or {}, ensure_ascii=False)}}
                   for x in bl if x.get("type") == "tool_use"]
            mm = {"role": "assistant", "content": txt or None}
            rc = [x.get("thinking") or "" for x in bl if x.get("type") == "thinking" and (unpack(x.get("signature")) or {}).get("k") == "rc"]
            if rc:
                mm["reasoning_content"] = "\n".join(rc)
            if tcs:
                mm["tool_calls"] = tcs
            msgs.append(mm)
        else:
            parts, imgs_after = [], []
            for x in bl:
                t = x.get("type")
                if t == "tool_result":
                    txt, imgs = _tool_result_parts(x.get("content"))
                    msgs.append({"role": "tool", "tool_call_id": x.get("tool_use_id"), "content": ("[tool error] " if x.get("is_error") else "") + (txt or "(empty)")})
                    imgs_after += imgs
                elif t == "text" and x.get("text"):
                    parts.append({"type": "text", "text": x["text"]})
                elif t == "image":
                    u = _data_url(x.get("source"))
                    if u:
                        parts.append({"type": "image_url", "image_url": {"url": u}})
                elif t == "document":
                    s = x.get("source") or {}
                    if s.get("type") == "base64":
                        parts.append({"type": "file", "file": {"filename": x.get("title") or "document.pdf", "file_data": _data_url(s)}})
                    elif s.get("type") == "text":
                        parts.append({"type": "text", "text": s.get("data") or ""})
            parts = [{"type": "image_url", "image_url": {"url": u}} for u in imgs_after] + parts
            if parts:
                msgs.append({"role": "user", "content": parts if any(p["type"] != "text" for p in parts) else "\n".join(p["text"] for p in parts)})
    req = {"model": model, "messages": msgs}
    tl = _anth_tools(b, names)
    if tl:
        req["tools"] = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}} for n, d, s in tl]
        tc = b.get("tool_choice") or {}
        if tc.get("type") == "any":
            req["tool_choice"] = "required"
        elif tc.get("type") == "none":
            req["tool_choice"] = "none"
        elif tc.get("type") == "tool" and tc.get("name"):
            req["tool_choice"] = {"type": "function", "function": {"name": names.enc(tc["name"])}}
        if tc.get("disable_parallel_tool_use"):
            req["parallel_tool_calls"] = False
    if b.get("max_tokens"):
        req["max_completion_tokens" if (reasoning or opt.get("max_completion_tokens")) else "max_tokens"] = int(b["max_tokens"])
    if b.get("stop_sequences"):
        req["stop"] = list(b["stop_sequences"])[:4]
    if reasoning:
        eff = _anth_effort(b, opt)
        if eff:
            req["reasoning_effort"] = eff
    else:
        for k in ("temperature", "top_p"):
            if b.get(k) is not None:
                req[k] = b[k]
    if b.get("stream"):
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}
    return req, ctx


def convert_request(client: str, upstream: str, body: dict, model: str, opt=None):
    """不同协议之间：客户端请求 → 上游请求。返回 (上游请求, 上下文：工具名映射等，回复转换时用)。"""
    opt = opt or {}
    if client == "anthropic":
        mid, ctx = body, None
    elif client == "responses":
        mid, ctx = r2a_request(body, model, opt if upstream == "anthropic" else dict(opt, cache=False))
    elif client == "chat":
        mid, ctx = c2a_request(body, model, opt if upstream == "anthropic" else dict(opt, cache=False))
    else:
        raise ValueError(client)
    if upstream == "anthropic":
        return dict(mid, model=model), ctx or {"names": Names(), "client": client}
    fn = a2r_request if upstream == "responses" else a2c_request
    if client != "anthropic" and mid.get("thinking"):
        # Responses / Chat 客户端的思考强度已折成 Anthropic 写法；再转成 OpenAI 写法
        pass
    out, c2 = fn(mid, model, opt)
    return out, (c2 if client == "anthropic" else ctx)


# ---------------------------------------------------------------- 上游回复 → IR

def _usage_anth(u: dict) -> dict:
    return {"input": int(u.get("input_tokens") or 0), "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write": int(u.get("cache_creation_input_tokens") or 0), "output": int(u.get("output_tokens") or 0), "reasoning": 0}


def _usage_oai(u) -> dict:
    u = u or {}
    inp = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
    det = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    cached = int(det.get("cached_tokens") or 0)
    od = u.get("output_tokens_details") or u.get("completion_tokens_details") or {}
    return {"input": max(0, inp - cached), "cache_read": cached, "cache_write": int(det.get("cache_write_tokens") or 0),
            "output": int(u.get("output_tokens") or u.get("completion_tokens") or 0), "reasoning": int(od.get("reasoning_tokens") or 0)}


def _err_ev(obj, status=500) -> tuple:
    e = obj.get("error") if isinstance(obj, dict) else None
    e = e if isinstance(e, dict) else {"message": str(e or obj)}
    return ("error", {"status": status, "type": e.get("type") or "api_error", "message": str(e.get("message") or "upstream error")[:600],
                      "code": e.get("code")})


def ir_from_anthropic_json(m: dict) -> list:
    ev = [("start", {"id": m.get("id"), "model": m.get("model")})]
    for c in m.get("content") or []:
        t = c.get("type")
        if t == "text":
            ev += [("block", {"type": "text"}), ("text", c.get("text") or ""), ("end", {})]
        elif t == "thinking":
            ev += [("block", {"type": "thinking"}), ("think", c.get("thinking") or ""), ("sig", c.get("signature") or ""), ("end", {})]
        elif t == "redacted_thinking":
            ev += [("block", {"type": "redacted", "data": c.get("data")}), ("end", {})]
        elif t == "tool_use":
            ev += [("block", {"type": "tool", "id": c.get("id"), "name": c.get("name")}),
                   ("args", json.dumps(c.get("input") or {}, ensure_ascii=False)), ("end", {})]
    ev.append(("finish", {"stop": m.get("stop_reason") or "end_turn", "usage": _usage_anth(m.get("usage") or {})}))
    return ev


def ir_from_anthropic_stream(events):
    usage, stop, open_ = {}, None, False
    for name, data in events:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        t = d.get("type") or name
        if t == "message_start":
            m = d.get("message") or {}
            usage = _usage_anth(m.get("usage") or {})
            yield ("start", {"id": m.get("id"), "model": m.get("model")})
        elif t == "content_block_start":
            c = d.get("content_block") or {}
            ct = c.get("type")
            open_ = True
            if ct == "text":
                yield ("block", {"type": "text"})
                if c.get("text"):
                    yield ("text", c["text"])
            elif ct == "thinking":
                yield ("block", {"type": "thinking"})
            elif ct == "redacted_thinking":
                yield ("block", {"type": "redacted", "data": c.get("data")})
            elif ct == "tool_use":
                yield ("block", {"type": "tool", "id": c.get("id"), "name": c.get("name")})
            else:
                open_ = "skip"                     # server_tool_use / web_search_tool_result 等：不转
        elif t == "content_block_delta":
            if open_ == "skip":
                continue
            dl = d.get("delta") or {}
            dt = dl.get("type")
            if dt == "text_delta":
                yield ("text", dl.get("text") or "")
            elif dt == "thinking_delta":
                yield ("think", dl.get("thinking") or "")
            elif dt == "signature_delta":
                yield ("sig", dl.get("signature") or "")
            elif dt == "input_json_delta":
                yield ("args", dl.get("partial_json") or "")
        elif t == "content_block_stop":
            if open_ and open_ != "skip":
                yield ("end", {})
            open_ = False
        elif t == "message_delta":
            stop = (d.get("delta") or {}).get("stop_reason") or stop
            for k, v in _usage_anth(d.get("usage") or {}).items():
                if v:
                    usage[k] = v
        elif t == "message_stop":
            yield ("finish", {"stop": stop or "end_turn", "usage": usage})
            return
        elif t == "error":
            yield _err_ev(d, 529 if ((d.get("error") or {}).get("type") == "overloaded_error") else 500)
            return
    yield ("finish", {"stop": stop or "end_turn", "usage": usage, "truncated": True})


def _resp_stop(r: dict, saw_tool: bool) -> str:
    if r.get("status") == "incomplete":
        return "refusal" if (r.get("incomplete_details") or {}).get("reason") == "content_filter" else "max_tokens"
    return "tool_use" if saw_tool else "end_turn"


def _reasoning_sig(it: dict) -> str:
    return pack({"k": "oai", "id": it.get("id"), "e": it.get("encrypted_content")}) if it.get("encrypted_content") else ""


def ir_from_responses_json(r: dict) -> list:
    ev = [("start", {"id": r.get("id"), "model": r.get("model")})]
    saw = False
    for it in r.get("output") or []:
        t = it.get("type")
        if t == "reasoning":
            txt = "\n\n".join(s.get("text") or "" for s in it.get("summary") or [])
            ev += [("block", {"type": "thinking"}), ("think", txt), ("sig", _reasoning_sig(it)), ("end", {})]
        elif t == "message":
            txt = "".join((c.get("text") if c.get("type") == "output_text" else c.get("refusal")) or "" for c in it.get("content") or [])
            ev += [("block", {"type": "text"}), ("text", txt), ("end", {})]
        elif t in ("function_call", "custom_tool_call"):
            saw = True
            args = it.get("arguments") if t == "function_call" else json.dumps({"input": it.get("input") or ""})
            ev += [("block", {"type": "tool", "id": it.get("call_id"), "name": it.get("name")}), ("args", args or "{}"), ("end", {})]
    if r.get("status") == "failed":
        ev.append(_err_ev(r))
        return ev
    ev.append(("finish", {"stop": _resp_stop(r, saw), "usage": _usage_oai(r.get("usage"))}))
    return ev


def ir_from_responses_stream(events):
    saw, cur, parts = False, None, 0
    for name, data in events:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        t = d.get("type") or name
        if t == "response.created":
            r = d.get("response") or {}
            yield ("start", {"id": r.get("id"), "model": r.get("model")})
        elif t == "response.output_item.added":
            it = d.get("item") or {}
            ty = it.get("type")
            if cur:
                yield ("end", {})
                cur = None
            if ty == "reasoning":
                cur, parts = "thinking", 0
                yield ("block", {"type": "thinking"})
            elif ty == "message":
                cur = "text"
                yield ("block", {"type": "text"})
            elif ty in ("function_call", "custom_tool_call"):
                cur, saw = "tool", True
                yield ("block", {"type": "tool", "id": it.get("call_id"), "name": it.get("name")})
        elif t == "response.reasoning_summary_part.added":
            if parts and cur == "thinking":
                yield ("think", "\n\n")
            parts += 1
        elif t == "response.reasoning_summary_text.delta":
            if cur == "thinking":
                yield ("think", d.get("delta") or "")
        elif t in ("response.output_text.delta", "response.refusal.delta"):
            if cur == "text":
                yield ("text", d.get("delta") or "")
        elif t == "response.function_call_arguments.delta":
            if cur == "tool":
                yield ("args", d.get("delta") or "")
        elif t == "response.custom_tool_call_input.done":
            if cur == "tool":
                yield ("args", json.dumps({"input": d.get("input") or ""}))
        elif t == "response.output_item.done":
            it = d.get("item") or {}
            if it.get("type") == "reasoning" and cur == "thinking":
                s = _reasoning_sig(it)
                if s:
                    yield ("sig", s)
            if cur:
                yield ("end", {})
                cur = None
        elif t in ("response.completed", "response.incomplete"):
            if cur:
                yield ("end", {})
                cur = None
            r = d.get("response") or {}
            yield ("finish", {"stop": _resp_stop(r, saw), "usage": _usage_oai(r.get("usage"))})
            return
        elif t in ("response.failed", "error"):
            r = d.get("response") if isinstance(d.get("response"), dict) else d
            yield _err_ev(r if r.get("error") else {"error": d})
            return
    if cur:
        yield ("end", {})
    yield ("finish", {"stop": "tool_use" if saw else "end_turn", "usage": {}, "truncated": True})


CHAT_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use", "content_filter": "refusal"}


def ir_from_chat_json(r: dict) -> list:
    ev = [("start", {"id": r.get("id"), "model": r.get("model")})]
    ch = (r.get("choices") or [{}])[0]
    m = ch.get("message") or {}
    rc = m.get("reasoning_content") or (m.get("reasoning") if isinstance(m.get("reasoning"), str) else None)
    if rc:
        ev += [("block", {"type": "thinking"}), ("think", rc), ("sig", pack({"k": "rc"})), ("end", {})]
    c = m.get("content")
    if c:
        ev += [("block", {"type": "text"}), ("text", c if isinstance(c, str) else _text_of(c)), ("end", {})]
    for tc in m.get("tool_calls") or []:
        f = tc.get("function") or {}
        ev += [("block", {"type": "tool", "id": tc.get("id") or uid("call"), "name": f.get("name")}), ("args", f.get("arguments") or "{}"), ("end", {})]
    stop = "tool_use" if m.get("tool_calls") else CHAT_STOP.get(ch.get("finish_reason") or "stop", "end_turn")
    ev.append(("finish", {"stop": stop, "usage": _usage_oai(r.get("usage"))}))
    return ev


def ir_from_chat_stream(events):
    cur, stop, usage, started, tools = None, None, {}, False, {}
    for name, data in events:
        if data.strip() == "[DONE]":
            break
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if d.get("error"):
            yield _err_ev(d)
            return
        if not started:
            started = True
            yield ("start", {"id": d.get("id"), "model": d.get("model")})
        if d.get("usage"):
            usage = _usage_oai(d["usage"])
        for ch in d.get("choices") or []:
            dl = ch.get("delta") or {}
            rc = dl.get("reasoning_content") or dl.get("reasoning")
            if isinstance(rc, str) and rc:
                if cur != "thinking":
                    if cur:
                        yield ("end", {})
                    cur = "thinking"
                    yield ("block", {"type": "thinking"})
                yield ("think", rc)
            if isinstance(dl.get("content"), str) and dl["content"]:
                if cur != "text":
                    if cur == "thinking":
                        yield ("sig", pack({"k": "rc"}))
                    if cur:
                        yield ("end", {})
                    cur = "text"
                    yield ("block", {"type": "text"})
                yield ("text", dl["content"])
            for tc in dl.get("tool_calls") or []:
                # 工具调用先攒着、最后按顺序一次给（有的服务会把几个并行调用的参数交错着发）
                idx = tc.get("index", len(tools))
                f = tc.get("function") or {}
                t = tools.setdefault(idx, {"id": None, "name": None, "args": ""})
                t["id"] = t["id"] or tc.get("id")
                t["name"] = t["name"] or f.get("name")
                t["args"] += f.get("arguments") or ""
            if ch.get("finish_reason"):
                stop = CHAT_STOP.get(ch["finish_reason"], "end_turn")
    if cur == "thinking":
        yield ("sig", pack({"k": "rc"}))
    if cur:
        yield ("end", {})
    for idx in sorted(tools):
        t = tools[idx]
        yield ("block", {"type": "tool", "id": t["id"] or uid("call"), "name": t["name"]})
        if t["args"]:
            yield ("args", t["args"])
        yield ("end", {})
    yield ("finish", {"stop": "tool_use" if tools else (stop or "end_turn"), "usage": usage})


def ir_from(upstream: str, stream: bool, src):
    """src：stream 时是 sse_events(...) 的迭代器，否则是解析好的 JSON。"""
    if upstream == "anthropic":
        return ir_from_anthropic_stream(src) if stream else iter(ir_from_anthropic_json(src))
    if upstream == "responses":
        return ir_from_responses_stream(src) if stream else iter(ir_from_responses_json(src))
    return ir_from_chat_stream(src) if stream else iter(ir_from_chat_json(src))


# ---------------------------------------------------------------- IR → 客户端

class _Emit:
    def __init__(self, model: str, stream: bool, ctx=None):
        self.model, self.stream, self.ctx = model, stream, ctx or {}
        self.names = self.ctx.get("names") or Names()
        self.usage, self.stop, self.error, self.started, self.cur = {}, None, None, False, None

    def tool_name(self, raw: str) -> dict:
        return self.names.dec(raw) if self.ctx.get("client") in ("responses", "chat") else {"name": raw, "namespace": None, "custom": False}

    def feed_all(self, events) -> bytes:
        return b"".join(self.feed(e) for e in events)


class EmitAnthropic(_Emit):
    """给 Claude Code：Anthropic Messages（SSE 或 JSON）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.id = uid("msg")
        self.blocks, self.idx = [], -1

    def _start(self) -> bytes:
        if self.started:
            return b""
        self.started = True
        if not self.stream:
            return b""
        msg = {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [], "stop_reason": None,
               "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        return sse("message_start", {"type": "message_start", "message": msg}) + sse("ping", {"type": "ping"})

    def _open(self, kind: str, info: dict) -> bytes:
        out = self._close()
        self.idx += 1
        if kind == "text":
            cb = {"type": "text", "text": ""}
        elif kind == "thinking":
            cb = {"type": "thinking", "thinking": "", "signature": ""}
        elif kind == "redacted":
            cb = {"type": "redacted_thinking", "data": info.get("data") or ""}
        else:
            cb = {"type": "tool_use", "id": info.get("id") or uid("toolu"), "name": info.get("name") or "tool", "input": {}}
        self.cur = {"kind": kind, "cb": cb, "buf": [], "sig": ""}
        self.blocks.append(self.cur)
        if self.stream:
            out += sse("content_block_start", {"type": "content_block_start", "index": self.idx, "content_block": cb})
        return out

    def _close(self) -> bytes:
        c = self.cur
        if not c:
            return b""
        self.cur = None
        if not self.stream:
            return b""
        out = b""
        if c["kind"] == "tool" and not "".join(c["buf"]).strip():
            out += sse("content_block_delta", {"type": "content_block_delta", "index": self.idx, "delta": {"type": "input_json_delta", "partial_json": "{}"}})
        return out + sse("content_block_stop", {"type": "content_block_stop", "index": self.idx})

    def _need(self, kind: str) -> bytes:
        return b"" if (self.cur and self.cur["kind"] == kind) else self._open(kind, {})

    def _d(self, typ: str, field: str, s: str) -> bytes:
        return sse("content_block_delta", {"type": "content_block_delta", "index": self.idx, "delta": {"type": typ, field: s}}) if self.stream and s else b""

    def feed(self, ev) -> bytes:
        k, v = ev[0], (ev[1] if len(ev) > 1 else {})
        out = b"" if k == "error" and not self.started else self._start()
        if k == "block":
            out += self._open(v["type"] if v["type"] in ("text", "thinking", "redacted", "tool") else "text", v)
        elif k == "text":
            out += self._need("text")
            self.cur["buf"].append(v)
            out += self._d("text_delta", "text", v)
        elif k == "think":
            out += self._need("thinking")
            if not self.ctx.get("omit_thinking"):
                self.cur["buf"].append(v)
                out += self._d("thinking_delta", "thinking", v)
        elif k == "sig":
            out += self._need("thinking")
            self.cur["sig"] += v
            out += self._d("signature_delta", "signature", v)
        elif k == "args":
            out += self._need("tool")
            self.cur["buf"].append(v)
            out += self._d("input_json_delta", "partial_json", v)
        elif k == "end":
            out += self._close()
        elif k == "finish":
            out += self._close()
            self.stop, self.usage = v.get("stop") or "end_turn", v.get("usage") or {}
            if self.stream:
                out += sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": self.stop, "stop_sequence": None}, "usage": self._u()})
                out += sse("message_stop", {"type": "message_stop"})
        elif k == "error":
            self.error = v
            if self.stream:
                st, body = anth_error(v.get("status") or 500, v)
                out += sse("error", body)
        return out

    def _u(self) -> dict:
        u = self.usage
        return {"input_tokens": u.get("input", 0), "output_tokens": u.get("output", 0),
                "cache_read_input_tokens": u.get("cache_read", 0), "cache_creation_input_tokens": u.get("cache_write", 0)}

    def result(self) -> dict:
        content = []
        for b in self.blocks:
            cb, txt = dict(b["cb"]), "".join(b["buf"])
            if b["kind"] == "text":
                cb["text"] = txt
            elif b["kind"] == "thinking":
                cb.update(thinking=txt, signature=b["sig"])
            elif b["kind"] == "tool":
                cb["input"] = _jload(txt) if txt.strip() else {}
            content.append(cb)
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": content,
                "stop_reason": self.stop or "end_turn", "stop_sequence": None, "usage": self._u()}


def _oai_usage(u: dict) -> dict:
    inp = u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0)
    return {"input_tokens": inp, "input_tokens_details": {"cached_tokens": u.get("cache_read", 0)}, "output_tokens": u.get("output", 0),
            "output_tokens_details": {"reasoning_tokens": u.get("reasoning", 0)}, "total_tokens": inp + u.get("output", 0)}


class EmitResponses(_Emit):
    """给 Codex：OpenAI Responses（SSE 或 JSON）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.id = uid("resp")
        self.created = int(time.time())
        self.items, self.seq = [], 0

    def _ev(self, t: str, **d) -> bytes:
        if not self.stream:
            return b""
        self.seq += 1
        return sse(t, {"type": t, "sequence_number": self.seq, **d})

    def _resp(self, status: str, with_out=True) -> dict:
        return {"id": self.id, "object": "response", "created_at": self.created, "status": status, "model": self.model,
                "output": [i["item"] for i in self.items if i.get("done")] if with_out else [], "parallel_tool_calls": True,
                "tool_choice": "auto", "tools": [], "usage": None, "error": None, "incomplete_details": None}

    def _start(self) -> bytes:
        if self.started:
            return b""
        self.started = True
        r = self._resp("in_progress", False)
        return self._ev("response.created", response=r) + self._ev("response.in_progress", response=r)

    def _open(self, kind: str, info: dict) -> bytes:
        out = self._close()
        oi = len(self.items)
        if kind == "text":
            item = {"id": uid("msg"), "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        elif kind in ("thinking", "redacted"):
            item = {"id": uid("rs"), "type": "reasoning", "summary": []}
        else:
            meta = self.tool_name(info.get("name") or "tool")
            cid = info.get("id") or uid("call")
            if meta.get("custom"):
                item = {"id": uid("ctc"), "type": "custom_tool_call", "status": "in_progress", "call_id": cid, "name": meta["name"], "input": ""}
            else:
                item = {"id": uid("fc"), "type": "function_call", "status": "in_progress", "call_id": cid, "name": meta["name"], "arguments": ""}
                if meta.get("namespace"):
                    item["namespace"] = meta["namespace"]
        self.cur = {"kind": kind, "item": item, "buf": [], "oi": oi, "sig": "", "data": info.get("data")}
        self.items.append(self.cur)
        out += self._ev("response.output_item.added", output_index=oi, item=dict(item))
        if kind == "text":
            out += self._ev("response.content_part.added", item_id=item["id"], output_index=oi, content_index=0,
                            part={"type": "output_text", "text": "", "annotations": []})
        return out

    def _close(self) -> bytes:
        c = self.cur
        if not c:
            return b""
        self.cur = None
        it, oi, txt = c["item"], c["oi"], "".join(c["buf"])
        out = b""
        if c["kind"] == "text":
            part = {"type": "output_text", "text": txt, "annotations": []}
            out += self._ev("response.output_text.done", item_id=it["id"], output_index=oi, content_index=0, text=txt)
            out += self._ev("response.content_part.done", item_id=it["id"], output_index=oi, content_index=0, part=part)
            it.update(status="completed", content=[part])
        elif c["kind"] == "redacted":
            it["encrypted_content"] = pack({"k": "ant_r", "d": c.get("data") or ""})
        elif c["kind"] == "thinking":
            d = unpack(c["sig"])
            if d and d.get("k") == "rc":
                it["encrypted_content"] = pack({"k": "rc", "t": txt})
            elif c["sig"]:
                it["encrypted_content"] = pack({"k": "ant", "t": txt, "s": c["sig"]})
            if txt:
                out += self._ev("response.reasoning_summary_text.done", item_id=it["id"], output_index=oi, summary_index=0, text=txt)
                out += self._ev("response.reasoning_summary_part.done", item_id=it["id"], output_index=oi, summary_index=0,
                                part={"type": "summary_text", "text": txt})
                it["summary"] = [{"type": "summary_text", "text": txt}]
        else:
            if it["type"] == "custom_tool_call":
                a = _jload(txt) if txt.strip() else {}
                v = a.get("input") if isinstance(a, dict) and "input" in a else json.dumps(a)
                it["input"] = v if isinstance(v, str) else json.dumps(v)
                out += self._ev("response.custom_tool_call_input.delta", item_id=it["id"], output_index=oi, delta=it["input"])
                out += self._ev("response.custom_tool_call_input.done", item_id=it["id"], output_index=oi, input=it["input"])
            else:
                it["arguments"] = txt if txt.strip() else "{}"
                out += self._ev("response.function_call_arguments.done", item_id=it["id"], output_index=oi, arguments=it["arguments"])
            it["status"] = "completed"
        c["done"] = True
        return out + self._ev("response.output_item.done", output_index=oi, item=it)

    def _need(self, kind: str) -> bytes:
        return b"" if (self.cur and self.cur["kind"] == kind) else self._open(kind, {})

    def feed(self, ev) -> bytes:
        k, v = ev[0], (ev[1] if len(ev) > 1 else {})
        out = self._start()
        if k == "block":
            out += self._open(v["type"], v)
        elif k == "text":
            out += self._need("text")
            self.cur["buf"].append(v)
            if v:
                out += self._ev("response.output_text.delta", item_id=self.cur["item"]["id"], output_index=self.cur["oi"], content_index=0, delta=v)
        elif k == "think":
            out += self._need("thinking")
            c = self.cur
            if v:
                if not c["buf"]:
                    out += self._ev("response.reasoning_summary_part.added", item_id=c["item"]["id"], output_index=c["oi"], summary_index=0,
                                    part={"type": "summary_text", "text": ""})
                c["buf"].append(v)
                out += self._ev("response.reasoning_summary_text.delta", item_id=c["item"]["id"], output_index=c["oi"], summary_index=0, delta=v)
        elif k == "sig":
            out += self._need("thinking")
            self.cur["sig"] += v
        elif k == "args":
            out += self._need("tool")
            self.cur["buf"].append(v)
            if v and self.cur["item"]["type"] == "function_call":
                out += self._ev("response.function_call_arguments.delta", item_id=self.cur["item"]["id"], output_index=self.cur["oi"], delta=v)
        elif k == "end":
            out += self._close()
        elif k == "finish":
            out += self._close()
            self.stop, self.usage = v.get("stop") or "end_turn", v.get("usage") or {}
            r = self.result()
            out += self._ev("response.incomplete" if r["status"] == "incomplete" else "response.completed", response=r)
        elif k == "error":
            self.error = v
            st, body = oai_error(v.get("status") or 500, v)
            r = self._resp("failed")
            r["error"] = {"code": body["error"].get("code") or "server_error", "message": body["error"]["message"]}
            out += self._ev("response.failed", response=r)
        return out

    def result(self) -> dict:
        r = self._resp("completed")
        if self.stop in ("max_tokens", "refusal"):
            r["status"] = "incomplete"
            r["incomplete_details"] = {"reason": "max_output_tokens" if self.stop == "max_tokens" else "content_filter"}
        r["usage"] = _oai_usage(self.usage)
        return r


class EmitChat(_Emit):
    """给 Chat Completions 客户端。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.id = uid("chatcmpl")
        self.created = int(time.time())
        self.text, self.reason, self.tools = [], [], []

    def _chunk(self, delta: dict, fr=None) -> bytes:
        if not self.stream:
            return b""
        return sse(None, {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                          "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]})

    def _new_tool(self, cid, name) -> bytes:
        self.tools.append({"id": cid or uid("call"), "type": "function", "function": {"name": name, "arguments": ""}})
        return self._chunk({"tool_calls": [{"index": len(self.tools) - 1, "id": self.tools[-1]["id"], "type": "function",
                                            "function": {"name": name, "arguments": ""}}]})

    def feed(self, ev) -> bytes:
        k, v = ev[0], (ev[1] if len(ev) > 1 else {})
        out = b""
        if not self.started and k != "error":
            self.started = True
            out += self._chunk({"role": "assistant", "content": ""})
        if k == "block":
            self.cur = v["type"]
            if v["type"] == "tool":
                out += self._new_tool(v.get("id"), self.tool_name(v.get("name") or "tool")["name"])
        elif k == "text":
            self.text.append(v)
            out += self._chunk({"content": v}) if v else b""
        elif k == "think":
            self.reason.append(v)
            out += self._chunk({"reasoning_content": v}) if v else b""
        elif k == "args":
            if self.cur != "tool" or not self.tools:
                self.cur = "tool"
                out += self._new_tool(None, "tool")
            self.tools[-1]["function"]["arguments"] += v
            out += self._chunk({"tool_calls": [{"index": len(self.tools) - 1, "function": {"arguments": v}}]}) if v else b""
        elif k == "end":
            self.cur = None
        elif k == "finish":
            self.stop, self.usage = v.get("stop") or "end_turn", v.get("usage") or {}
            out += self._chunk({}, self._fr())
            if self.stream:
                if self.ctx.get("include_usage", True):
                    out += sse(None, {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                                      "choices": [], "usage": self._usage()})
                out += b"data: [DONE]\n\n"
        elif k == "error":
            self.error = v
            if self.stream:
                st, body = oai_error(v.get("status") or 500, v)
                out += sse(None, body) + b"data: [DONE]\n\n"
        return out

    def _fr(self):
        return {"max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter"}.get(self.stop or "end_turn", "stop")

    def _usage(self):
        u = self.usage
        inp = u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0)
        return {"prompt_tokens": inp, "completion_tokens": u.get("output", 0), "total_tokens": inp + u.get("output", 0),
                "prompt_tokens_details": {"cached_tokens": u.get("cache_read", 0)}, "completion_tokens_details": {"reasoning_tokens": u.get("reasoning", 0)}}

    def result(self) -> dict:
        msg = {"role": "assistant", "content": "".join(self.text) or None}
        if self.reason:
            msg["reasoning_content"] = "".join(self.reason)
        if self.tools:
            for t in self.tools:
                t["function"]["arguments"] = t["function"]["arguments"] or "{}"
            msg["tool_calls"] = self.tools
        return {"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "message": msg, "finish_reason": self._fr()}], "usage": self._usage()}


EMITTERS = {"anthropic": EmitAnthropic, "responses": EmitResponses, "chat": EmitChat}


# ---------------------------------------------------------------- 错误

def _ctx_too_long(msg: str, code) -> bool:
    m = (msg or "").lower()
    return code in ("context_length_exceeded", "string_above_max_length") or "prompt is too long" in m \
        or "maximum context length" in m or "context window" in m or "too many tokens" in m


def anth_err_type(status: int, e: dict) -> str:
    if _ctx_too_long(e.get("message") or "", e.get("code")):
        return "invalid_request_error"
    return {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
            413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error", 503: "overloaded_error"}.get(status, "api_error")


def parse_error(status: int, text: str) -> dict:
    """上游错误正文（任意协议）→ {status, type, message, code}"""
    try:
        j = json.loads(text)
    except (ValueError, TypeError):
        j = None
    e = (j.get("error") if isinstance(j, dict) else None) or (j if isinstance(j, dict) else {})
    if isinstance(e, str):
        e = {"message": e}
    msg = e.get("message") or (j.get("message") if isinstance(j, dict) else None) or (text or "")[:300] or f"HTTP {status}"
    return {"status": status, "type": e.get("type"), "message": str(msg)[:600], "code": e.get("code")}


def anth_error(status: int, e: dict):
    msg = e.get("message") or "upstream error"
    if _ctx_too_long(msg, e.get("code")) and "prompt is too long" not in msg.lower():
        msg = "prompt is too long: " + msg                    # Claude Code 认这句话才会自动压缩上下文
    return status, {"type": "error", "error": {"type": anth_err_type(status, e), "message": msg}}


def oai_error(status: int, e: dict):
    msg = e.get("message") or "upstream error"
    code = e.get("code")
    if _ctx_too_long(msg, code):
        code = "context_length_exceeded"
    elif "credit balance" in msg.lower() or code == "insufficient_quota":
        code = "insufficient_quota"
    elif status == 429:
        code = "rate_limit_exceeded"
    if status == 529:
        status = 503
    typ = "invalid_request_error" if 400 <= status < 429 else ("requests" if status == 429 else "server_error")
    return status, {"error": {"message": msg, "type": typ, "code": code, "param": None}}


def client_error(client: str, status: int, e: dict):
    return anth_error(status, e) if client == "anthropic" else oai_error(status, e)


def estimate_tokens(body: dict) -> int:
    """count_tokens 上游不是 Anthropic 时的粗估（英文约 3.8 字符 / token，中日韩约 1.5 字 / token）。"""
    s = json.dumps({k: body.get(k) for k in ("system", "messages", "tools")}, ensure_ascii=False)
    a = sum(1 for ch in s if ord(ch) < 128)
    return max(1, int(a / 3.8 + (len(s) - a) / 1.5))

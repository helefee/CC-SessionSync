# -*- coding: utf-8 -*-
"""本地假上游（Anthropic Messages + OpenAI Responses + OpenAI Chat Completions），给模型路由的测试用：不连外网、不用真钥匙。

    python tools/mock_upstream.py PORT LOG [EXPECTED_KEY]

- 每个请求记一行 JSON 到 LOG：路径、请求体、钥匙对不对（只记对 / 错，不记钥匙本身）。
- 对话里用户说了 USE-TOOL、还没有工具结果 → 调一次 shell 类工具（按请求里有的工具挑：Bash / exec_command / shell_command / shell），
  让它 echo ROUTE-TOOL-OUT；收到工具结果 → 回「ROUTE-FINAL: <工具结果开头>」；否则回 MOCK-UPSTREAM-OK。
- 请求要了思考（Anthropic thinking / OpenAI reasoning）→ 先给一段思考（签名 sig-mock-1 / encrypted_content enc-mock-1），
  下一轮请求里带回来没有记在 LOG 的 echo_sig / echo_enc 里。
- 用户说 RATE-LIMIT → 429 + retry-after: 7；BAD-REQUEST → 400。
"""
from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]
KEY = sys.argv[3] if len(sys.argv) > 3 else ""
CMD = "echo ROUTE-TOOL-OUT"
USAGE = {"in": 120, "cached": 20, "out": 30}


def _txt(c) -> str:
    if isinstance(c, str):
        return c
    out = []
    for x in c or []:
        if isinstance(x, dict):
            if isinstance(x.get("text"), str):
                out.append(x["text"])
            elif isinstance(x.get("content"), (str, list)):
                out.append(_txt(x["content"]))
            elif isinstance(x.get("output"), (str, list)):
                out.append(_txt(x["output"]))
    return " ".join(out)


def analyse(path: str, b: dict) -> dict:
    """统一看三种协议：用户说了什么、最后一个工具结果、带回来的签名、有哪些工具。"""
    users, result, sig, enc, tools = [], None, None, None, []
    if path.endswith("/messages"):
        for m in b.get("messages") or []:
            c = m.get("content")
            blocks = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
            for x in blocks:
                if m.get("role") == "user" and x.get("type") == "text":
                    users.append(x.get("text") or "")
                    result = None
                if x.get("type") == "tool_result":
                    result = _txt(x.get("content")) if not isinstance(x.get("content"), str) else x["content"]
                if x.get("type") == "thinking":
                    sig = x.get("signature")
        tools = [(t.get("name"), t.get("input_schema") or {}) for t in b.get("tools") or [] if t.get("name")]
    elif path.endswith("/responses"):
        inp = b.get("input")
        items = [{"type": "message", "role": "user", "content": inp}] if isinstance(inp, str) else (inp or [])
        for it in items:
            t = it.get("type") or ("message" if it.get("role") else "")
            if t == "message" and it.get("role") == "user":
                tx = _txt(it.get("content"))
                if not tx.startswith("<environment_context>") and "<permissions" not in tx and not tx.startswith("# AGENTS.md"):
                    users.append(tx)
                    result = None
            elif t in ("function_call_output", "custom_tool_call_output"):
                o = it.get("output")
                result = o if isinstance(o, str) else _txt(o)
            elif t == "reasoning":
                enc = it.get("encrypted_content")
        tools = [(t.get("name"), t.get("parameters") or {}) for t in b.get("tools") or [] if t.get("name")]
    else:
        for m in b.get("messages") or []:
            if m.get("role") == "user":
                users.append(_txt(m.get("content")))
                result = None
            elif m.get("role") == "tool":
                result = _txt(m.get("content")) if not isinstance(m.get("content"), str) else m["content"]
        tools = [((t.get("function") or {}).get("name"), (t.get("function") or {}).get("parameters") or {}) for t in b.get("tools") or []]
    return {"users": users, "result": result, "sig": sig, "enc": enc, "tools": tools}


def pick_tool(tools):
    names = {n: s for n, s in tools}
    win = sys.platform == "win32"
    if "Bash" in names:
        return "Bash", {"command": CMD, "description": "echo a marker"}
    if "exec_command" in names:
        return "exec_command", {"cmd": CMD}
    if "shell_command" in names:
        return "shell_command", {"command": CMD}
    if "shell" in names:
        return "shell", {"command": ["powershell.exe", "-NoProfile", "-Command", CMD] if win else ["bash", "-lc", CMD]}
    if tools:
        n, s = tools[0]
        return n, {k: "x" for k in (s.get("required") or [])}
    return None, None


def plan(path, b):
    a = analyse(path, b)
    last = a["users"][-1] if a["users"] else ""
    allu = " ".join(a["users"])
    if a["result"] is not None:
        return a, "text", "ROUTE-FINAL: " + " ".join(str(a["result"]).split())[:80]
    if "USE-TOOL" in last or ("USE-TOOL" in allu and a["result"] is None and "USE-TOOL" in last):
        n, args = pick_tool(a["tools"])
        if n:
            return a, "tool", (n, args)
    return a, "text", "MOCK-UPSTREAM-OK"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *x):
        pass

    def _send(self, code, obj, ctype="application/json", extra=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0].endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": "mock-model-a", "object": "model", "type": "model"},
                                                               {"id": "mock-model-b", "object": "model", "type": "model"}]})
        self._send(200, {"ok": True})

    def _key(self):
        k = self.headers.get("x-api-key") or ""
        a = self.headers.get("authorization") or ""
        if a.lower().startswith("bearer "):
            k = k or a[7:]
        return k

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            b = json.loads(raw or b"{}")
        except ValueError:
            b = {}
        key_ok = (not KEY) or self._key() == KEY
        a, kind, val = plan(path, b) if key_ok else ({}, "", None)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, "key_ok": key_ok, "body": b, "beta": self.headers.get("anthropic-beta"),
                                "version": self.headers.get("anthropic-version"), "kind": kind,
                                "echo_sig": a.get("sig"), "echo_enc": a.get("enc")}, ensure_ascii=False) + "\n")
        anth = path.endswith("/messages") or path.endswith("/count_tokens")
        if not key_ok:
            return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}} if anth
                              else {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}})
        last = (a.get("users") or [""])[-1]
        if "RATE-LIMIT" in last:
            return self._send(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "mock rate limit"}} if anth
                              else {"error": {"message": "mock rate limit", "type": "requests", "code": "rate_limit_exceeded"}}, extra={"retry-after": "7"})
        if "BAD-REQUEST" in last:
            return self._send(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 999999 tokens > 200000 maximum"}} if anth
                              else {"error": {"message": "This model's maximum context length is 400000 tokens.", "type": "invalid_request_error", "code": "context_length_exceeded"}})
        if path.endswith("/count_tokens"):
            return self._send(200, {"input_tokens": 42})
        if path.endswith("/messages"):
            return self.anthropic(b, kind, val)
        if path.endswith("/responses"):
            return self.do_responses(b, kind, val)
        if path.endswith("/chat/completions"):
            return self.chat(b, kind, val)
        self._send(404, {"error": {"message": "not found"}})

    # ---- 流式 ----
    def _sse_start(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

    def _ev(self, name, d):
        self.wfile.write((f"event: {name}\n" if name else "") .encode() + b"data: " + json.dumps(d).encode() + b"\n\n")
        self.wfile.flush()

    # ---- Anthropic ----
    def anthropic(self, b, kind, val):
        think = (b.get("thinking") or {}).get("type") in ("enabled", "adaptive")
        content = []
        if think:
            content.append({"type": "thinking", "thinking": "mock thinking about it", "signature": "sig-mock-1"})
        if kind == "tool":
            content.append({"type": "tool_use", "id": "toolu_mock1", "name": val[0], "input": val[1]})
        else:
            content.append({"type": "text", "text": val})
        stop = "tool_use" if kind == "tool" else "end_turn"
        usage = {"input_tokens": USAGE["in"] - USAGE["cached"], "cache_read_input_tokens": USAGE["cached"], "cache_creation_input_tokens": 5,
                 "output_tokens": USAGE["out"]}
        msg = {"id": "msg_mock1", "type": "message", "role": "assistant", "model": b.get("model"), "content": content,
               "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not b.get("stream"):
            return self._send(200, msg)
        self._sse_start()
        self._ev("message_start", {"type": "message_start", "message": dict(msg, content=[], stop_reason=None,
                                                                             usage=dict(usage, output_tokens=1))})
        self._ev("ping", {"type": "ping"})
        for i, c in enumerate(content):
            if c["type"] == "thinking":
                self._ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                for part in ("mock thinking ", "about it"):
                    self._ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": part}})
                self._ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig-mock-1"}})
            elif c["type"] == "tool_use":
                self._ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": c["id"], "name": c["name"], "input": {}}})
                js = json.dumps(c["input"])
                for j in range(0, len(js), 7):
                    self._ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": js[j:j + 7]}})
            else:
                self._ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                t = c["text"]
                for j in range(0, len(t), 5):
                    self._ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": t[j:j + 5]}})
            self._ev("content_block_stop", {"type": "content_block_stop", "index": i})
        self._ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": USAGE["out"]}})
        self._ev("message_stop", {"type": "message_stop"})

    # ---- OpenAI Responses ----
    def do_responses(self, b, kind, val):
        think = bool((b.get("reasoning") or {}).get("effort")) and (b.get("reasoning") or {}).get("effort") not in ("none", "minimal")
        out = []
        if think:
            out.append({"id": "rs_mock1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "mock reasoning summary"}],
                        "encrypted_content": "enc-mock-1" if "reasoning.encrypted_content" in (b.get("include") or []) else None})
        if kind == "tool":
            out.append({"id": "fc_mock1", "type": "function_call", "status": "completed", "call_id": "call_mock1", "name": val[0],
                        "arguments": json.dumps(val[1])})
        else:
            out.append({"id": "msg_mock1", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": val, "annotations": []}]})
        usage = {"input_tokens": USAGE["in"], "input_tokens_details": {"cached_tokens": USAGE["cached"]}, "output_tokens": USAGE["out"],
                 "output_tokens_details": {"reasoning_tokens": 8 if think else 0}, "total_tokens": USAGE["in"] + USAGE["out"]}
        resp = {"id": "resp_mock1", "object": "response", "created_at": int(time.time()), "status": "completed", "model": b.get("model"),
                "output": out, "usage": usage}
        if not b.get("stream"):
            return self._send(200, resp)
        self._sse_start()
        seq = [0]

        def ev(t, **d):
            seq[0] += 1
            self._ev(t, {"type": t, "sequence_number": seq[0], **d})
        ev("response.created", response=dict(resp, status="in_progress", output=[], usage=None))
        for i, it in enumerate(out):
            if it["type"] == "reasoning":
                ev("response.output_item.added", output_index=i, item=dict(it, summary=[], encrypted_content=None))
                ev("response.reasoning_summary_part.added", item_id=it["id"], output_index=i, summary_index=0, part={"type": "summary_text", "text": ""})
                for part in ("mock reasoning ", "summary"):
                    ev("response.reasoning_summary_text.delta", item_id=it["id"], output_index=i, summary_index=0, delta=part)
                ev("response.reasoning_summary_text.done", item_id=it["id"], output_index=i, summary_index=0, text="mock reasoning summary")
                ev("response.output_item.done", output_index=i, item=it)
            elif it["type"] == "function_call":
                ev("response.output_item.added", output_index=i, item=dict(it, arguments="", status="in_progress"))
                a = it["arguments"]
                for j in range(0, len(a), 9):
                    ev("response.function_call_arguments.delta", item_id=it["id"], output_index=i, delta=a[j:j + 9])
                ev("response.function_call_arguments.done", item_id=it["id"], output_index=i, arguments=a)
                ev("response.output_item.done", output_index=i, item=it)
            else:
                ev("response.output_item.added", output_index=i, item=dict(it, content=[], status="in_progress"))
                ev("response.content_part.added", item_id=it["id"], output_index=i, content_index=0, part={"type": "output_text", "text": "", "annotations": []})
                t = it["content"][0]["text"]
                for j in range(0, len(t), 5):
                    ev("response.output_text.delta", item_id=it["id"], output_index=i, content_index=0, delta=t[j:j + 5])
                ev("response.output_text.done", item_id=it["id"], output_index=i, content_index=0, text=t)
                ev("response.content_part.done", item_id=it["id"], output_index=i, content_index=0, part=it["content"][0])
                ev("response.output_item.done", output_index=i, item=it)
        ev("response.completed", response=resp)

    # ---- OpenAI Chat Completions ----
    def chat(self, b, kind, val):
        usage = {"prompt_tokens": USAGE["in"], "completion_tokens": USAGE["out"], "total_tokens": USAGE["in"] + USAGE["out"],
                 "prompt_tokens_details": {"cached_tokens": USAGE["cached"]}}
        if kind == "tool":
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_mock1", "type": "function",
                                                                         "function": {"name": val[0], "arguments": json.dumps(val[1])}}]}
            fin = "tool_calls"
        else:
            msg, fin = {"role": "assistant", "content": val}, "stop"
        base = {"id": "chatcmpl-mock1", "object": "chat.completion", "created": int(time.time()), "model": b.get("model")}
        if not b.get("stream"):
            return self._send(200, dict(base, choices=[{"index": 0, "message": msg, "finish_reason": fin}], usage=usage))
        self._sse_start()
        base["object"] = "chat.completion.chunk"

        def ch(delta, fr=None, **kw):
            self._ev(None, dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": fr}], **kw))
        ch({"role": "assistant", "content": ""})
        if kind == "tool":
            tc = msg["tool_calls"][0]
            ch({"tool_calls": [{"index": 0, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": ""}}]})
            a = tc["function"]["arguments"]
            for j in range(0, len(a), 9):
                ch({"tool_calls": [{"index": 0, "function": {"arguments": a[j:j + 9]}}]})
        else:
            for j in range(0, len(val), 5):
                ch({"content": val[j:j + 5]})
        ch({}, fin)
        if (b.get("stream_options") or {}).get("include_usage"):
            self._ev(None, dict(base, choices=[], usage=usage))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

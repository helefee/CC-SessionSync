"""本地假模型（OpenAI chat-completions 兼容，流式），给 tools/verify_dsh.py 里的 dsh headless 用：不连外网、不用任何真钥匙。
每个请求的 messages 记到 MOCK_LOG（只记角色和每条的前 2000 字，用来确认导入的历史有没有被送进模型）。"""
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("MOCK_LOG", "mock_openai.jsonl")
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"object": "list", "data": [{"id": "mock-1", "object": "model"}]}).encode())
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        self._body = body
        msgs = body.get("messages") or body.get("input") or []
        def txt(c):
            if isinstance(c, str): return c
            return " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
        with open(LOG, "a", encoding="utf-8") as f:   # Windows 缺省 cp1252，写中文会抛错 → dsh 只看到 Connection error
            f.write(json.dumps({"path": self.path, "model": body.get("model"), "stream": body.get("stream"),
                                "msgs": [{"role": m.get("role"), "type": m.get("type"), "text": txt(m.get("content"))[:2000]} for m in msgs if isinstance(m, dict)],
                                # MOCK_RAW=1：再记原样的历史（不含系统提示）和工具表，用来确认工具调用 / 图片是原生格式送进模型的
                                "raw": [m for m in msgs if isinstance(m, dict) and m.get("role") not in ("system", "developer")][-60:] if os.environ.get("MOCK_RAW") else None,
                                "tools": [{"name": (t.get("function") or t).get("name"), "params": list((((t.get("function") or t).get("parameters") or {}).get("properties") or {}).keys())}
                                          for t in body.get("tools") or [] if isinstance(t, dict)] if os.environ.get("MOCK_RAW") else None}, ensure_ascii=False) + "\n")
        reply = "MOCK-REPLY: 我收到了 %d 条消息" % len(msgs)
        tool = self._want_tool(body, msgs)
        if self.path.rstrip("/").endswith("/responses"):
            return self._responses(body, reply, tool)
        if body.get("stream"):
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            base = {"id": "cmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
            if tool:                                   # 测试工具调用：用户说了 USE-TOOL、还没有工具结果 → 调一次 shell 工具
                ch = dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_mock1", "type": "function",
                          "function": {"name": tool, "arguments": json.dumps(self._args(tool))}}]}, "finish_reason": None}])
                self.wfile.write(b"data: " + json.dumps(ch).encode() + b"\n\n")
                ch = dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], usage={"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107})
                self.wfile.write(b"data: " + json.dumps(ch).encode() + b"\n\ndata: [DONE]\n\n"); self.wfile.flush()
                return
            for i, piece in enumerate([reply[:6], reply[6:]]):
                ch = dict(base, choices=[{"index": 0, "delta": ({"role": "assistant"} if i == 0 else {}) | {"content": piece}, "finish_reason": None}])
                self.wfile.write(b"data: " + json.dumps(ch).encode() + b"\n\n")
            ch = dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage={"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107})
            self.wfile.write(b"data: " + json.dumps(ch).encode() + b"\n\ndata: [DONE]\n\n"); self.wfile.flush()
        else:
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"id": "cmpl-mock", "object": "chat.completion", "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107}}).encode())

    @staticmethod
    def _want_tool(body, msgs):
        names = [((t.get("function") or {}).get("name") or t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict)]
        if not names:
            return None
        # 最后一条助手消息之后的用户消息里有 USE-TOOL（dsh 会在用户消息后面再塞一条运行时上下文）
        last_a = max((i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "assistant"), default=-1)
        users = [i for i, m in enumerate(msgs) if i > last_a and isinstance(m, dict) and m.get("role") == "user"]
        last_user = next((i for i in reversed(users) if "USE-TOOL" in json.dumps(msgs[i].get("content"), ensure_ascii=False)), -1)
        if last_user < 0:
            return None
        if any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("type") == "function_call_output") for m in msgs[last_user:]):
            return None
        return next((n for n in names if n and n.lower() in ("bash", "shell", "shell_command", "exec_command")), names[0])

    def _args(self, tool):
        """按工具自己的参数表填：exec_command 给 cmd，command 是数组的给数组（老 Codex 的 shell），别的给字符串。"""
        t = next((t for t in self._body.get("tools") or [] if isinstance(t, dict) and ((t.get("function") or {}).get("name") or t.get("name")) == tool), {})
        props = ((t.get("function") or t).get("parameters") or {}).get("properties") or {}
        if "cmd" in props or tool == "exec_command":
            return {"cmd": "echo TOOL-OUT"}
        if (props.get("command") or {}).get("type") == "array":
            return {"command": ["bash", "-lc", "echo TOOL-OUT"]}
        return {"command": "echo TOOL-OUT", "description": "echo test"}

    def _responses(self, body, reply, tool=None):
        """OpenAI Responses API（Codex 用），流式。tool：先调一次 shell 类工具（function_call）。"""
        rid, mid = "resp_mock", "msg_mock"
        if tool:
            fc = {"type": "function_call", "id": "fc_mock1", "call_id": "call_mock1", "name": tool, "arguments": json.dumps(self._args(tool)), "status": "completed"}
            evs = [{"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "output": []}},
                   {"type": "response.output_item.added", "output_index": 0, "item": dict(fc, arguments="", status="in_progress")},
                   {"type": "response.function_call_arguments.done", "item_id": "fc_mock1", "output_index": 0, "arguments": fc["arguments"]},
                   {"type": "response.output_item.done", "output_index": 0, "item": fc},
                   {"type": "response.completed", "response": {"id": rid, "object": "response", "status": "completed", "output": [fc],
                    "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 107}}}]
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            for e in evs:
                self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            return
        evs = [{"type": "response.created", "response": {"id": rid, "object": "response", "status": "in_progress", "output": []}},
               {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "id": mid, "role": "assistant", "status": "in_progress", "content": []}},
               {"type": "response.content_part.added", "item_id": mid, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
               {"type": "response.output_text.delta", "item_id": mid, "output_index": 0, "content_index": 0, "delta": reply},
               {"type": "response.output_text.done", "item_id": mid, "output_index": 0, "content_index": 0, "text": reply},
               {"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": mid, "role": "assistant", "status": "completed",
                                                                                  "content": [{"type": "output_text", "text": reply, "annotations": []}]}},
               {"type": "response.completed", "response": {"id": rid, "object": "response", "status": "completed",
                "output": [{"type": "message", "id": mid, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": reply, "annotations": []}]}],
                "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 7, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 107}}}]
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
        for e in evs:
            self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 18080), H).serve_forever()

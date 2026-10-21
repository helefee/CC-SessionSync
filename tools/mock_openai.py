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
        msgs = body.get("messages") or body.get("input") or []
        def txt(c):
            if isinstance(c, str): return c
            return " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
        with open(LOG, "a", encoding="utf-8") as f:   # Windows 缺省 cp1252，写中文会抛错 → dsh 只看到 Connection error
            f.write(json.dumps({"path": self.path, "model": body.get("model"), "stream": body.get("stream"),
                                "msgs": [{"role": m.get("role"), "text": txt(m.get("content"))[:2000]} for m in msgs if isinstance(m, dict)]}, ensure_ascii=False) + "\n")
        reply = "MOCK-REPLY: 我收到了 %d 条消息" % len(msgs)
        tool = self._want_tool(body, msgs)
        if self.path.rstrip("/").endswith("/responses"):
            return self._responses(body, reply)
        if body.get("stream"):
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            base = {"id": "cmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
            if tool:                                   # 测试工具调用：用户说了 USE-TOOL、还没有工具结果 → 调一次 shell 工具
                ch = dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_mock1", "type": "function",
                          "function": {"name": tool, "arguments": json.dumps({"command": "echo TOOL-OUT", "description": "echo test"})}}]}, "finish_reason": None}])
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
        last_user = max((i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"), default=-1)
        if last_user < 0 or "USE-TOOL" not in json.dumps(msgs[last_user].get("content"), ensure_ascii=False):
            return None
        if any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("type") == "function_call_output") for m in msgs[last_user:]):
            return None
        return next((n for n in names if n and n.lower() in ("bash", "shell", "exec_command")), names[0])

    def _responses(self, body, reply):
        """OpenAI Responses API（Codex 用），流式。"""
        rid, mid = "resp_mock", "msg_mock"
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

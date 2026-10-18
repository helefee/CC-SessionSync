"""本地假模型（OpenAI chat-completions 兼容，流式），给 tools/verify_dsh.py 里的 dsh headless 用：不连外网、不用任何真钥匙。
每个请求的 messages 记到 MOCK_LOG（只记角色和每条的前 80 字，用来确认导入的历史有没有被送进模型）。"""
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
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "model": body.get("model"), "stream": body.get("stream"),
                                "msgs": [{"role": m.get("role"), "text": txt(m.get("content"))[:80]} for m in msgs if isinstance(m, dict)]}, ensure_ascii=False) + "\n")
        reply = "MOCK-REPLY: 我收到了 %d 条消息" % len(msgs)
        if body.get("stream"):
            self.send_response(200); self.send_header("content-type", "text/event-stream"); self.end_headers()
            base = {"id": "cmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
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
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 18080), H).serve_forever()

# -*- coding: utf-8 -*-
"""本地假 Anthropic API：给 `claude --resume` 验证用，不连外网、不花额度。
把收到的 /v1/messages 请求体存进 LOG（一行一个 JSON），回一句固定的话。

    python tools/mock_anthropic.py PORT LOG
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]
REPLY = "MOCK-OK"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, "{}")

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "body": body}, ensure_ascii=False) + "\n")
        if "count_tokens" in self.path:
            return self._send(200, json.dumps({"input_tokens": 10}))
        if not self.path.startswith("/v1/messages"):
            return self._send(200, "{}")
        model = body.get("model", "claude-mock")
        msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}
        if not body.get("stream"):
            msg.update(content=[{"type": "text", "text": REPLY}], stop_reason="end_turn")
            return self._send(200, json.dumps(msg))
        ev = [("message_start", {"type": "message_start", "message": msg}),
              ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
              ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}}),
              ("content_block_stop", {"type": "content_block_stop", "index": 0}),
              ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}}),
              ("message_stop", {"type": "message_stop"})]
        self._send(200, "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in ev), "text/event-stream")


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

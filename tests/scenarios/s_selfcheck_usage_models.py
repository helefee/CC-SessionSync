# -*- coding: utf-8 -*-
"""自检、用量统计（自己扫、去重、缓存 tokens、没价格的模型是 None）。0.3.5 拿掉了供应商（模型与 API），这里只确认它不再出现在自检里。
全在临时目录 + 本机假接口。"""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-models-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
H = T / "home"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
SEEN = []
class M(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        SEEN.append({"path": self.path, "ua": self.headers.get("user-agent"), "auth": self.headers.get("authorization") or self.headers.get("x-api-key")})
        self._send(200, {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-haiku-4-5"}, {"id": "gpt-5.5"}]})
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        SEEN.append({"path": self.path, "ua": self.headers.get("user-agent"), "auth": self.headers.get("x-api-key"), "model": body.get("model")})
        self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}], "model": body.get("model"), "usage": {"input_tokens": 1, "output_tokens": 1}})
srv = ThreadingHTTPServer(("127.0.0.1", 0), M); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

from cc_sessionsync import selfcheck, usage
# 自检
r = selfcheck.run(net=False)
by = {i["id"]: i for i in r["items"]}
ok(all(i["status"] in ("pass", "warn", "fail") and i["name"] for i in r["items"]), "selfcheck items have pass/warn/fail")
ok(by["install"]["status"] == "pass" and by["home"]["status"] == "pass", "install + data dir writable pass")
ok("keystore" not in by and not any(i["id"].startswith(("route", "card-")) for i in r["items"]), "0.3.5: no keystore / route / card checks (供应商 removed)")
ok(by["claude-found"]["status"] == "pass" and by["codex-found"]["status"] == "pass" and by["claude-projects"]["status"] == "pass", "Claude / Codex found + data dirs readable")
ok(selfcheck.last()["time"] == r["time"] and "自检" in selfcheck.text(r), "selfcheck saved + text report")
os.environ["CC_SESSIONSYNC_UPDATE_URL"] = BASE + "/release"
ok({i["id"]: i for i in selfcheck.run(net=True)["items"]}["update"]["status"] == "pass", "update endpoint reachable (local fake)")

# 用量
s1 = usage.scan(full=True)
t = usage.summary({})["totals"]
ok(t["n"] == 13 and t["cr"] > 0 and t["cw5"] > 0, f"usage: 13 requests from 6 Claude + 4 Codex logs, cache tokens counted ({t['n']})")
ok(usage.scan()["added"] == 0 and usage.summary({})["totals"]["n"] == 13, "rescan adds nothing (incremental)")
f = next((H / ".claude/projects").rglob("*.jsonl")); line = f.read_text(encoding="utf-8").splitlines()[1]
with open(f, "a", encoding="utf-8") as fh: fh.write(line + "\n")           # 同一条回复写了两遍（Claude Code 流式会这样）
usage.scan(); ok(usage.summary({})["totals"]["n"] == 13, "duplicate message.id + requestId deduped (magpie style)")
d = json.loads(line); d["message"]["id"] = "msg_unpriced"; d["requestId"] = "req_unpriced"; d["message"]["model"] = "my-private-model-x"
with open(f, "a", encoding="utf-8") as fh: fh.write(json.dumps(d) + "\n")
d2 = dict(d, requestId="req_imp"); d2["message"] = dict(d["message"], id="msg_imp", model="codex-import")
with open(f, "a", encoding="utf-8") as fh: fh.write(json.dumps(d2) + "\n")
usage.scan(); lg = usage.logs({}, 0, 500)
row = next((x for x in lg["rows"] if x.get("model") == "my-private-model-x"), None)
ok(row is not None and row.get("cost") is None, "unpriced model → cost None (UI shows —)")
ok(not any(x.get("model") == "codex-import" for x in lg["rows"]), "our imported sessions (model codex-import) are not counted")
ok((T / "ss/usage.sqlite").exists(), "usage DB lives in ~/.cc-sessionsync")

srv.shutdown()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

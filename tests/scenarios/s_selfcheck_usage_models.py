# -*- coding: utf-8 -*-
"""自检（文件版凭据库）、用量统计（自己扫、去重、缓存 tokens、没价格的模型是 None）、模型与 API（key 进凭据库不落明文、
apiKeyHelper / model_providers auth、写之前备份、tomlkit 保留注释、连接测试带 User-Agent、模型列表）。全在临时目录 + 本机假接口。"""
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

from cc_sessionsync import keystore, modelcfg, providers, selfcheck, usage
# 自检
r = selfcheck.run(net=False)
by = {i["id"]: i for i in r["items"]}
ok(all(i["status"] in ("pass", "warn", "fail") and i["name"] for i in r["items"]), "selfcheck items have pass/warn/fail")
ok(by["install"]["status"] == "pass" and by["home"]["status"] == "pass", "install + data dir writable pass")
ok(by["keystore"]["status"] == "warn" and by["keystore"]["fix"], "file keystore → warn with a fix")
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

# 模型与 API：Claude 供应商
cs = H / ".claude/settings.json"; before = cs.read_text()
p = providers.upsert("claude", {"name": "测试中转", "mode": "key", "base_url": BASE, "models": {"model": "claude-sonnet-4-5", "haiku": "claude-haiku-4-5"}}, "sk-test-SECRET-123456")
ok(p["id"] and "SECRET" not in json.dumps(p) and keystore.get(p["key"]) == "sk-test-SECRET-123456", "provider key stored in the keystore, view is masked")
r = providers.apply_global("claude", p["id"])
j = json.loads(cs.read_text())
ok(j["env"]["ANTHROPIC_BASE_URL"] == BASE and "key get" in j.get("apiKeyHelper", "") and "SECRET" not in cs.read_text(), "switch → settings.json gets base URL + apiKeyHelper, no plaintext key")
ok(j.get("model") == "claude-sonnet-4-5" and j["env"].get("ANTHROPIC_DEFAULT_HAIKU_MODEL") == "claude-haiku-4-5", "model mapping written")
ok(len(modelcfg.backups(50)) >= 1, "backup made before writing")
tc = providers.test("claude", p["id"])
post = [x for x in SEEN if x["path"].startswith("/v1/messages")]
ok(tc.get("ok") and post and post[-1]["ua"].startswith("CCSessionSync/") and post[-1]["auth"] == "sk-test-SECRET-123456", "connection test hits the provider with User-Agent CCSessionSync/x and the stored key")
ml = providers.models("claude", p["id"], None, None)
ok("claude-haiku-4-5" in json.dumps(ml), "model list fetched")
# Codex 供应商：config.toml 注释保留，model_providers.<id>.auth 走 key get
ct = H / ".codex/config.toml"; ct.write_text('# 我的注释\nmodel = "gpt-5.5"\nmodel_provider = "openai"\n\n[projects."/work/web"]\ntrust_level = "trusted"\n', encoding="utf-8")
q = providers.upsert("codex", {"name": "Codex 中转", "mode": "key", "base_url": BASE + "/v1", "models": {"model": "gpt-5.5"}}, "sk-codex-SECRET-999")
providers.apply_global("codex", q["id"])
txt = ct.read_text(encoding="utf-8")
ok("# 我的注释" in txt and '[projects."/work/web"]' in txt, "config.toml comments / other tables preserved (tomlkit)")
import tomllib
tj = tomllib.loads(txt)
mp = tj["model_providers"][tj["model_provider"]]
ok(mp["base_url"] == BASE + "/v1" and "SECRET" not in txt and ("auth" in mp or "env_key" in mp), f"Codex provider written with command auth, no plaintext ({list(mp)})")
ok(not list(ct.parent.glob("*.ccss-*.tmp")), "atomic write leaves no temp files")
# 恢复备份
b0 = modelcfg.backups(50)[-1]
rr = modelcfg.restore(str(b0["id"])); ok(rr.get("ok") is not False, "restore a config backup")
srv.shutdown()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

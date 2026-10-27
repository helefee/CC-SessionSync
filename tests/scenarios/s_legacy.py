# -*- coding: utf-8 -*-
"""0.3.5：供应商（模型路由 / 卡片）拿掉后，第一次启动的收尾（cc_sessionsync/legacy.py）。
A 卡片启用过、文件没再动 → 按字节还原 + 停代理 + 删目录 / 口令；B 启用后用户又改过 → 只拿掉我们写的；
C 没有记录（老版本 / 记录丢了）→ 只删 cc-route 相关的，不留 model_provider = cc-route；D 本来就干净 → 什么都不动；
E 0.3.0「高级 → 模型路由」的记录；F 旧队列里的卡片任务 / 旧的开机自启命令不报错。全在临时目录，代理是本机假的。"""
import hashlib, json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
TOKEN = "ccss-local-test-token-1234567890"

class Fake(BaseHTTPRequestHandler):
    """假的后台代理：/health 认得出是我们的，/ccss/shutdown 要口令。"""
    hits = []
    def log_message(self, *a): pass
    def do_GET(self):
        b = b'{"ok":true,"app":"cc-sessionsync-route"}'
        self.send_response(200); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        good = self.headers.get("authorization") == "Bearer " + TOKEN
        Fake.hits.append((self.path, good))
        self.send_response(200 if good else 401); self.send_header("content-length", "2"); self.end_headers(); self.wfile.write(b"{}")
srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake); threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]

T = Path(tempfile.mkdtemp(prefix="ccss-legacy-"))
H = T / "home"; CX = H / ".codex"; CL = H / ".claude"; SS = T / "ss"
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1", CC_SESSIONSYNC_NO_LOGIN_ITEM="1")
from cc_sessionsync import keystore, legacy, apps
from cc_sessionsync import _toml as tomllib
A = {"__absent__": True}
sha = lambda b: hashlib.sha256(b).hexdigest()

def reset():
    import shutil
    for d in (H, SS):
        shutil.rmtree(d, ignore_errors=True)
    CX.mkdir(parents=True); CL.mkdir(parents=True); SS.mkdir(parents=True)
    keystore.delete(legacy.TOKEN_KEY)

def put_route(d):
    (SS / "route.json").write_text(json.dumps(dict({"version": 1, "port": PORT, "autostart": True, "providers": [], "provs": {}}, **d)), encoding="utf-8")

CODEX_ORIG = b'# my codex config\nmodel = "gpt-5.5"\nmodel_provider = "openai"\napproval_policy = "on-request"\n\n[features]\nweb_search = true\n'
CLAUDE_ORIG = b'{\n  "env": {\n    "FOO": "1"\n  },\n  "apiKeyHelper": "my-helper.sh"\n}\n'
PROV = {"name": "CC Session Sync · Claude API Key", "base_url": f"http://127.0.0.1:{PORT}/v1", "wire_api": "responses",
        "auth": {"command": "python", "args": ["keyhelper.py", "key", "get", "route-token"], "timeout_ms": 15000, "refresh_interval_ms": 0}, "request_max_retries": 1}
CODEX_OURS = (b'# my codex config\nmodel = "claude-opus-5-5"\nmodel_provider = "cc-route"\napproval_policy = "on-request"\nmodel_catalog_json = "ccss-models-cc-route.json"\n\n'
              b'[features]\nweb_search = true\n\n[model_providers.cc-route]\nname = "CC Session Sync \xc2\xb7 Claude API Key"\nbase_url = "http://127.0.0.1:' + str(PORT).encode() +
              b'/v1"\nwire_api = "responses"\nrequest_max_retries = 1\n\n[model_providers.cc-route.auth]\ncommand = "python"\nargs = ["keyhelper.py", "key", "get", "route-token"]\n'
              b'timeout_ms = 15000\nrefresh_interval_ms = 0\n')
CLAUDE_OURS = ('{\n  "env": {\n    "FOO": "1",\n    "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d",\n    "ANTHROPIC_AUTH_TOKEN": "%s"\n  },\n  "apiKeyHelper": "my-helper.sh"\n}\n' % (PORT, TOKEN)).encode()

def enable_cards():
    """照 0.3.4 cards.enable 写的样子：先备份原文件，再写我们的版本，route.json 里记 cards_applied。"""
    cfg, st = CX / "config.toml", CL / "settings.json"
    cfg.write_bytes(CODEX_ORIG); st.write_bytes(CLAUDE_ORIG)
    b1 = legacy.backup(cfg, "card-codex-config"); b2 = legacy.backup(st, "card-claude")
    cfg.write_bytes(CODEX_OURS); st.write_bytes(CLAUDE_OURS)
    (CX / "ccss-models-cc-route.json").write_text('{"models":[]}'); (CX / "models_cache.json").write_text("{}")
    keystore.put(legacy.TOKEN_KEY, TOKEN, "test")
    keystore.put("prov-anthropic", "sk-user-own-key", "test")
    put_route({"cards": {"codex": {"enabled": True}, "claude": {"enabled": True}}, "cards_applied": {
        "codex": {"config": {"file": str(cfg), "fmt": "toml", "existed": True, "backup": b1, "sha": sha(CODEX_OURS), "edits": [
            {"path": ["model_provider"], "orig": "openai", "ours": "cc-route"}, {"path": ["model"], "orig": "gpt-5.5", "ours": "claude-opus-5-5"},
            {"path": ["model_catalog_json"], "orig": A, "ours": "ccss-models-cc-route.json"}, {"path": ["model_providers", "cc-route"], "orig": A, "ours": PROV}]}},
        "claude": {"settings": {"file": str(st), "fmt": "json", "existed": True, "backup": b2, "sha": sha(CLAUDE_OURS), "edits": [
            {"path": ["env", "ANTHROPIC_BASE_URL"], "orig": A, "ours": f"http://127.0.0.1:{PORT}"}, {"path": ["env", "ANTHROPIC_AUTH_TOKEN"], "orig": A, "ours": TOKEN},
            {"path": ["env", "ANTHROPIC_API_KEY"], "orig": A, "ours": A}]}}}})

# ================================================================ A. 启用过、没再动 → 按字节还原
reset(); enable_cards()
r = legacy.migrate()
ok((CX / "config.toml").read_bytes() == CODEX_ORIG, "A: Codex config.toml byte-identical to before enabling")
ok((CL / "settings.json").read_bytes() == CLAUDE_ORIG, "A: Claude settings.json byte-identical to before enabling")
ok(not list(CX.glob("ccss-models-*.json")) and not (CX / "models_cache.json").exists(), "A: our model catalog + Codex models_cache deleted")
ok(("/ccss/shutdown", True) in Fake.hits, "A: background proxy asked to shut down with our token")
ok(keystore.get(legacy.TOKEN_KEY) is None and keystore.get("prov-anthropic") == "sk-user-own-key", "A: local proxy token deleted; the user's own provider key left alone")
ok(not (SS / "route.json").exists() and (SS / "route.json.removed-0.3.5").exists(), "A: route.json retired (renamed, not read again)")
ok(r["notice"] and not r["errors"] and any("按字节还原" in x for x in r["done"]) and any("停掉后台代理" in x for x in r["done"]), f"A: notice with what was restored ({r['done']})")
idx = [json.loads(x) for x in (SS / "backups/index.jsonl").read_text(encoding="utf-8").splitlines()]
ok(any(x["label"] == "legacy-before-restore" and x["file"].endswith("config.toml") for x in idx), "A: our version backed up before restoring")
ok((SS / "legacy-0.3.5.log").exists() and legacy.state()["done"] == r["done"], "A: logged + state saved")
ok("ccss-local" not in (SS / "legacy-0.3.5.log").read_text(encoding="utf-8") + (SS / "legacy-0.3.5.json").read_text(encoding="utf-8"), "A: token never written to the log / state")
b1 = (CX / "config.toml").read_bytes(); n = len(idx)
r2 = legacy.migrate()
ok(r2 == r and (CX / "config.toml").read_bytes() == b1, "A: second launch: nothing done again (idempotent)")
r3 = legacy.migrate(force=True)
ok((CX / "config.toml").read_bytes() == b1 and len((SS / "backups/index.jsonl").read_text().splitlines()) == n and r3["done"] == r["done"], "A: forced rerun on clean files changes nothing, keeps the notice list")
ok(legacy.seen()["notice"] is False and legacy.migrate()["notice"] is False, "A: notice dismissed → not shown again")

# ================================================================ B. 启用后用户又改过 → 只拿掉我们写的
reset(); enable_cards()
cfg, st = CX / "config.toml", CL / "settings.json"
cfg.write_bytes(cfg.read_bytes() + b'\n[mcp_servers.docs]\ncommand = "npx"\n')
st.write_text(st.read_text().replace('"FOO": "1"', '"FOO": "2"'))
r = legacy.migrate()
d = tomllib.loads(cfg.read_text(encoding="utf-8"))
ok(d.get("model_provider") == "openai" and d.get("model") == "gpt-5.5" and "model_catalog_json" not in d and "model_providers" not in d,
   "B: Codex provider / model / catalog put back to the pre-enable values, cc-route block removed")
ok(d["approval_policy"] == "on-request" and d["features"]["web_search"] and d["mcp_servers"]["docs"]["command"] == "npx" and "# my codex config" in cfg.read_text(),
   "B: user's other settings + later edits + comments kept")
j = json.loads(st.read_text())
ok(j == {"env": {"FOO": "2"}, "apiKeyHelper": "my-helper.sh"}, f"B: Claude env vars we added removed, user's edit + own apiKeyHelper kept ({j})")
ok(any("只动我们写的" in x for x in r["done"]), "B: notice says partial restore")

# ================================================================ C. 没有记录（老版本 / route.json 丢了）
reset()
keystore.put(legacy.TOKEN_KEY, TOKEN, "test")
cfg.write_text('model = "claude-sonnet-5-5"\nmodel_provider = "cc-route"\nmodel_catalog_json = "ccss-models-cpa.json"\nmodel_reasoning_effort = "high"\n\n'
               '[model_providers.cc-route]\nbase_url = "http://127.0.0.1:1/v1"\n\n[model_providers.mine]\nbase_url = "https://example.invalid/v1"\n\n'
               '[profiles.cc-route]\nmodel_provider = "cc-route"\n\n[projects."/w"]\ntrust_level = "trusted"\n', encoding="utf-8")
(CX / "cc-route.config.toml").write_text('model_provider = "cc-route"\n'); (CX / "ccss-models-cpa.json").write_text("{}"); (CX / "ccss-models-cc-route.json").write_text("{}")
(CX / "other-models.json").write_text("{}")
st.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_AUTH_TOKEN": TOKEN, "KEEP": "1"},
                          "apiKeyHelper": '"/opt/CCSessionSync.exe" key get route-token', "model": "opus"}))
r = legacy.migrate()
d = tomllib.loads(cfg.read_text(encoding="utf-8"))
ok("model_provider" not in d and "model" not in d, "C: model_provider no longer cc-route (key removed → Codex default ChatGPT login), provider-only model removed")
ok("cc-route" not in d.get("model_providers", {}) and d["model_providers"]["mine"]["base_url"].startswith("https://example") and "profiles" not in d,
   "C: [model_providers.cc-route] + [profiles.cc-route] removed, the user's own provider kept")
ok("model_catalog_json" not in d and d["model_reasoning_effort"] == "high" and d["projects"]["/w"]["trust_level"] == "trusted", "C: catalog pointer removed, unrelated keys kept")
ok(not (CX / "cc-route.config.toml").exists() and not list(CX.glob("ccss-models-*.json")) and (CX / "other-models.json").exists(), "C: our profile + ccss-models-*.json (cc-route and cpa) deleted, other files kept")
j = json.loads(st.read_text())
ok(j == {"env": {"KEEP": "1"}, "model": "opus"}, f"C: Claude base URL / token / route-token apiKeyHelper removed, rest kept ({j})")
ok(r["notice"] and len(r["done"]) >= 4, "C: notice lists what was cleaned")

# 用户自己的本机代理（别的端口、别的口令、自己的 apiKeyHelper）→ 不动
reset()
own = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456", "ANTHROPIC_AUTH_TOKEN": "users-own"}, "apiKeyHelper": '"x.exe" key get prov-glm'}
st.write_text(json.dumps(own)); before = st.read_bytes()
legacy.migrate()
ok(st.read_bytes() == before, "C: the user's own localhost proxy + a v0.2 key-helper line are not touched")

# ================================================================ D. 本来就干净
reset()
cfg.write_bytes(CODEX_ORIG); st.write_bytes(CLAUDE_ORIG)
r = legacy.migrate()
ok(cfg.read_bytes() == CODEX_ORIG and st.read_bytes() == CLAUDE_ORIG and r["done"] == [] and r["notice"] is False and not r["errors"], "D: already clean → nothing changed, no notice")
ok(not (SS / "backups").exists(), "D: no backups made when nothing changes")
ok(legacy.migrate() == r, "D: idempotent")

# ================================================================ E. 0.3.0「高级 → 模型路由」应用过（profile + 默认）
reset()
keystore.put(legacy.TOKEN_KEY, TOKEN, "test")
cfg.write_bytes(b'model = "gpt-5.5"\n\n[projects."/w"]\ntrust_level = "trusted"\n'); st.write_bytes(b'{"model": "opus"}\n')
bc, bs = legacy.backup(cfg, "route-codex-default"), legacy.backup(st, "route-claude")
prof = CX / "cc-route.config.toml"; prof.write_text('model_provider = "cc-route"\n')
cfg.write_text('model = "claude-x"\nmodel_provider = "cc-route"\n\n[projects."/w"]\ntrust_level = "trusted"\n\n[model_providers.cc-route]\nbase_url = "http://127.0.0.1:1/v1"\n\n# user edit\n')
ours_cl = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}", "ANTHROPIC_AUTH_TOKEN": TOKEN}
stb = json.dumps({"model": "opus", "env": ours_cl}, indent=2).encode(); st.write_bytes(stb)
(SS / "route").mkdir(); (SS / "route/claude-settings.json").write_text("{}")
put_route({"applied": {"codex": {"files": [
    {"kind": "profile", "file": str(prof), "existed": False, "backup": None, "sha": sha(prof.read_bytes()), "orig": {}, "ours": {}},
    {"kind": "config", "file": str(cfg), "existed": True, "backup": bc, "sha": "changed-after", "orig": {"model_provider": None, "model": "gpt-5.5"}, "ours": {"model_provider": "cc-route", "model": "claude-x"}}]},
    "claude": {"file": str(st), "existed": True, "backup": bs, "sha": sha(stb), "had_env": False, "orig": {}, "ours": ours_cl}}})
r = legacy.migrate()
d = tomllib.loads(cfg.read_text(encoding="utf-8"))
ok(not prof.exists() and "model_provider" not in d and d["model"] == "gpt-5.5" and "model_providers" not in d and d["projects"]["/w"]["trust_level"] == "trusted",
   "E: 0.3.0 Codex profile deleted, default config: provider / model back, cc-route removed, rest kept")
ok(st.read_bytes() == b'{"model": "opus"}\n' and not (SS / "route/claude-settings.json").exists(), "E: 0.3.0 Claude settings byte-restored, session settings file deleted")
ok(not r["errors"] and r["notice"], f"E: done ({r['done']})")

# ================================================================ F. 旧队列里的「启用卡片」任务、旧的开机自启命令
reset()
res = apps._ops()["route-card-enable"](client="codex")
ok(res.get("ok") and res.get("skipped"), "F: a queued 0.3.4 route-card job no longer raises (runs the cleanup instead)")
p = subprocess.run([sys.executable, "-m", "cc_sessionsync", "route", "serve", "--background"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
ok(p.returncode == 0 and not p.stdout.strip(), "F: old autostart `route serve --background` exits 0 quietly")
p = subprocess.run([sys.executable, "-m", "cc_sessionsync", "route", "status"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
ok(p.returncode == 0 and "0.3.5 移除" in p.stdout, "F: `route` prints that the feature is gone")
for m in ("router", "cards", "providers", "modelcfg", "presets", "effort", "xproto"):
    ok(not (ROOT / "cc_sessionsync" / f"{m}.py").exists(), f"F: {m}.py removed")
srv.shutdown()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

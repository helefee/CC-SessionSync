# -*- coding: utf-8 -*-
"""「供应商」卡片（v0.3.1）：Codex 使用… / Claude Code 使用…
启用 → 写 ~/.codex/config.toml 缺省供应商（或只写 profile）/ ~/.claude/settings.json；要转换的走后台代理（独立进程，取口令时按需拉起）；
停用 → 没动过就按字节还原、动过只拿掉我们写的；Codex 桌面端开着 → 排队；key 不进配置 / 日志；没配好时给客户端清楚的错误。全在临时目录。"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-cards-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["PYTHONPATH"] = str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
H = T / "home"; H.mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)

from cc_sessionsync import cards, keystore, modelcfg, router, selfcheck
from cc_sessionsync import _toml as tomllib

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
MP = free(); LOG = T / "mock.jsonl"
mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_upstream.py"), str(MP), str(LOG), "sk-ant-card-good,sk-oa-card-good"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", MP), 0.2).close(); break
    except OSError:
        time.sleep(0.1)
MB = f"http://127.0.0.1:{MP}"
def mlog():
    try:
        return [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return []

CX = H / ".codex"; CX.mkdir(parents=True)
CFG = CX / "config.toml"; AUTH = CX / "auth.json"
ORIG = b'# my codex config\nmodel = "gpt-5.5"\napproval_policy = "on-request"\n\n[features]\nweb_search = true\n\n[mcp_servers.docs]\ncommand = "npx"\nargs = ["-y", "docs-mcp"]\n'
CFG.write_bytes(ORIG); AUTH.write_bytes(b'{"auth_mode":"chatgpt","tokens":{"id_token":"x"}}')
CL = H / ".claude"; CL.mkdir(parents=True)
SET = CL / "settings.json"
SORIG = b'{\n  "env": {\n    "FOO": "1"\n  },\n  "apiKeyHelper": "my-helper.sh",\n  "permissions": {"allow": ["Bash(ls:*)"]}\n}\n'
SET.write_bytes(SORIG)
router.save(dict(router.load(), port=free()))

# ================================================================ 1. Codex 使用 Claude API Key（中转站 Base URL = 假 Anthropic）
v = cards.save_card("codex", {"kind": "claude", "secret": "sk-ant-card-good", "base_url": MB, "models": ["claude-haiku-5-5", "claude-opus-4-1-20250805", "claude-opus-5-5", "claude-sonnet-5-5"]})
ok(v["model"] == "claude-opus-5-5" and v["needs_proxy"] and v["keys"]["claude"] and "card-good" not in json.dumps(v), f"saved: default model = newest Opus ({v['model']}), key masked")
ok("sk-ant-card-good" not in (T / "ss" / "route.json").read_text(encoding="utf-8"), "route.json has no plaintext key")
ok(cards.latest(["claude-opus-4-1-20250805", "claude-opus-4-5", "claude-opus-5-5", "claude-opus-5"], "opus") == "claude-opus-5-5", "latest(): picks newest opus")
ok(not router.health(), "proxy not running before 启用")
r = cards.run("codex", "enable")
ok(r["ok"] and not r["deferred"] and r["proxy"] and r["proxy_ok"], f"enable codex card: written, background proxy up ({r.get('files')})")
ok(router.health() and not router._SRV["srv"], "proxy runs as a separate background process (not inside this process)")
doc = tomllib.loads(CFG.read_text(encoding="utf-8"))
mp = doc["model_providers"]["cc-route"]
ok(doc["model_provider"] == "cc-route" and doc["model"] == "claude-opus-5-5" and doc["approval_policy"] == "on-request" and doc["features"]["web_search"]
   and doc["mcp_servers"]["docs"]["command"] == "npx", "config.toml: default provider = cc-route, model set, user's other settings kept")
ok(mp["base_url"] == router.base_url() + "/v1" and mp["wire_api"] == "responses" and mp["auth"]["args"][-1] == "route-token", "provider block → local proxy, auth via token helper")
ok(AUTH.read_bytes() == b'{"auth_mode":"chatgpt","tokens":{"id_token":"x"}}', "auth.json (ChatGPT login) untouched")
ok("sk-ant" not in CFG.read_text(encoding="utf-8"), "no key in config.toml")

TOK = router.token()
def call(path, body, hdr=None):
    h = {"content-type": "application/json", "authorization": "Bearer " + TOK}; h.update(hdr or {})
    req = urllib.request.Request(router.base_url() + path, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
n0 = len(mlog())
st, b = call("/v1/responses", {"model": "gpt-5.5", "input": "hi", "stream": False})
last = mlog()[-1] if len(mlog()) > n0 else {}
ok(st == 200 and b"MOCK-UPSTREAM-OK" in b and last.get("path") == "/v1/messages" and last.get("key_ok") and last["body"]["model"] == "claude-opus-5-5",
   "Codex desktop's gpt-5.5 request → card model on the Anthropic upstream (converted, card key)")
st, b = call("/v1/responses", {"model": "claude-sonnet-5-5", "input": "hi", "stream": False})
ok(st == 200 and mlog()[-1]["body"]["model"] == "claude-sonnet-5-5", "explicit Claude model from /model kept")
t = cards.test("codex")
ok(t["ok"] and t.get("via") == "proxy", "测试 goes through the background proxy")

# 取口令命令按需拉起代理（桌面端没开、自启关了）
ok(router.shutdown_background() and not router.health(), "background proxy stopped (simulate: app not running, nothing at login)")
p = subprocess.run(keystore.helper_cmd(router.TOKEN_KEY), capture_output=True, timeout=30, cwd=tempfile.gettempdir())
ok(p.returncode == 0 and p.stdout.decode().strip() == TOK and router.health(), "Codex's token helper auto-starts the proxy, prints only the token")
st, b = call("/v1/responses", {"model": "gpt-5.5", "input": "hi"})
ok(st == 200, "requests work again via the auto-started proxy")

# 卡片 key 丢了 → 客户端看到清楚的错误
kv = keystore.get(cards.key_name("codex", "claude")); keystore.delete(cards.key_name("codex", "claude"))
st, b = call("/v1/responses", {"model": "gpt-5.5", "input": "hi"})
ok(st == 503 and "供应商" in b.decode("utf-8") and "key" in b.decode("utf-8"), "missing key → clear error returned to Codex")
keystore.put(cards.key_name("codex", "claude"), kv, "t")

sc = {i["id"]: i for i in selfcheck.check_route()}
ok(sc.get("card-codex", {}).get("status") == "pass" and sc.get("route", {}).get("status") == "pass", "自检: card + proxy pass")

# 停用：没动过 → 字节还原
r = cards.run("codex", "disable")
ok(r["ok"] and CFG.read_bytes() == ORIG and not router.health(), "停用: config.toml byte-identical, proxy shut down (nothing else needs it)")
ok(not cards.needs_proxy(), "no card needs the proxy any more")

# 启用后用户又改了 config.toml → 停用只拿掉我们写的
cards.run("codex", "enable")
CFG.write_text(CFG.read_text(encoding="utf-8") + '\n[tui]\ntheme = "dark"\n', encoding="utf-8")
cards.run("codex", "disable")
doc = tomllib.loads(CFG.read_text(encoding="utf-8"))
ok("model_provider" not in doc and "cc-route" not in (doc.get("model_providers") or {}) and doc["model"] == "gpt-5.5" and doc["tui"]["theme"] == "dark"
   and "model_context_window" not in doc, "edited after enable → surgical revert keeps the user's edit, restores model")
CFG.write_bytes(ORIG)

# 只在 codex --profile cc-route 时使用
cards.save_card("codex", {"profile_only": True})
cards.run("codex", "enable")
prof = CX / "cc-route.config.toml"
ok(CFG.read_bytes() == ORIG and prof.exists() and tomllib.loads(prof.read_text(encoding="utf-8"))["model_provider"] == "cc-route", "profile_only: only cc-route.config.toml written")
cards.save_card("codex", {"profile_only": False}); cards.run("codex", "enable")
ok(not prof.exists() and tomllib.loads(CFG.read_text(encoding="utf-8"))["model_provider"] == "cc-route", "switching to default mode removes the profile file")
cards.run("codex", "disable")
ok(CFG.read_bytes() == ORIG and not prof.exists(), "disable after switching: byte-identical")

# Codex 桌面端开着 → 排队
(T / "desktop-running").write_text("1")
r = cards.run("codex", "enable")
ok(r.get("deferred") and r["op"] == "route-card-enable" and CFG.read_bytes() == ORIG, "Codex desktop running → queued (prompt to restart), nothing written yet")
from cc_sessionsync import apps
apps.cancel(r["job"]); (T / "desktop-running").unlink()

# ================================================================ 2. Codex 使用 OpenAI API Key：不用转换，直连
cards.save_card("codex", {"kind": "openai", "secret": "sk-oa-card-good", "base_url": MB + "/v1", "model": "gpt-5.5"})
r = cards.run("codex", "enable")
doc = tomllib.loads(CFG.read_text(encoding="utf-8"))
ok(not r["proxy"] and doc["model_providers"]["cc-route"]["base_url"] == MB + "/v1" and doc["model_providers"]["cc-route"]["auth"]["args"][-1] == "card-codex-openai"
   and not router.health(), "OpenAI key: config.toml points straight at upstream, card key via helper, no proxy")
cards.run("codex", "disable")
ok(CFG.read_bytes() == ORIG, "disable → byte-identical")

# ================================================================ 3. Claude Code 使用 OpenAI API Key（走转换）
cards.save_card("claude", {"kind": "openai", "secret": "sk-oa-card-good", "base_url": MB + "/v1", "model": "gpt-5.5", "small_model": "gpt-5.4-mini", "effort": "high"})
r = cards.run("claude", "enable")
s = json.loads(SET.read_text(encoding="utf-8"))
ok(r["proxy"] and r["proxy_ok"] and s["env"]["ANTHROPIC_BASE_URL"] == router.base_url() and s["env"]["ANTHROPIC_AUTH_TOKEN"] == TOK and s["env"]["FOO"] == "1"
   and s["apiKeyHelper"] == "my-helper.sh" and s["permissions"], "Claude Code card: settings.json env → local proxy, user's settings kept")
st, b = call("/v1/messages", {"model": "claude-opus-5-5", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}, {"anthropic-version": "2023-06-01"})
lg = mlog()[-1]
ok(st == 200 and lg["path"] == "/v1/responses" and lg["body"]["model"] == "gpt-5.5" and lg["body"]["reasoning"]["effort"] == "high" and lg["key_ok"],
   "claude-opus-… → gpt-5.5 on Responses with card effort")
call("/v1/messages", {"model": "claude-haiku-5-5", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}, {"anthropic-version": "2023-06-01"})
ok(mlog()[-1]["body"]["model"] == "gpt-5.4-mini", "haiku → small model")
ok(cards.overview()["proxy"]["running"], "overview: proxy running")
r = cards.run("claude", "disable")
ok(SET.read_bytes() == SORIG and not router.health(), "停用: settings.json byte-identical, proxy stopped")

# ================================================================ 4. Claude Code 使用 Claude API Key：原生，不转换
cards.save_card("claude", {"kind": "claude", "secret": "sk-ant-card-good", "base_url": ""})
r = cards.run("claude", "enable")
s = json.loads(SET.read_text(encoding="utf-8"))
ok(not r["proxy"] and "card-claude-claude" in s["apiKeyHelper"] and "ANTHROPIC_BASE_URL" not in s["env"] and not router.health(), "native Claude key: apiKeyHelper only, no proxy")
p = subprocess.run(s["apiKeyHelper"], shell=True, capture_output=True, timeout=30)
ok(p.stdout.decode().strip() == "sk-ant-card-good", "apiKeyHelper prints the stored key")
cards.run("claude", "disable")
ok(SET.read_bytes() == SORIG, "disable → byte-identical")

# ================================================================ 5. 高级路由已经指过去了 → 卡片拒绝
d = router.load(); d["applied"] = {"codex": {"files": []}}; router.save(d)
try:
    cards.run("codex", "enable"); ok(False, "conflict not detected")
except ValueError as e:
    ok("高级" in str(e), "advanced routing already applied → clear error")
d = router.load(); d["applied"] = {}; router.save(d)

# 自定义缺 Base URL
cards.save_card("claude", {"kind": "custom", "protocol": "chat", "base_url": ""})
try:
    cards.run("claude", "enable"); ok(False, "custom without url accepted")
except ValueError as e:
    ok("Base URL" in str(e), "custom without Base URL → error")

allt = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (T / "ss").rglob("*") if f.is_file() and f.name != "keys.json" and f.suffix in (".json", ".jsonl", ".log", ".toml", ".txt"))
allt += CFG.read_text(encoding="utf-8") + SET.read_text(encoding="utf-8")
ok("sk-ant-card-good" not in allt and "sk-oa-card-good" not in allt, "no plaintext key in configs / logs / backups (test keystore file excluded)")

router.shutdown_background()
mock.kill()
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)

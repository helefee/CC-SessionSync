# -*- coding: utf-8 -*-
"""命令行各子命令 + 本地界面的接口（临时目录，端口 8799）。"""
import json, os, pathlib, subprocess, sys, tempfile, time, urllib.error, urllib.request, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-cli-")); H = T / "home"; H.mkdir(); (T / "noprocs.json").write_text("[]")
os.environ.update(HOME=str(H), USERPROFILE=str(H), XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"),
                  CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"),
                  CC_SESSIONSYNC_NO_WATCHER="1", CC_SESSIONSYNC_KEYSTORE="file", CC_SESSIONSYNC_UPDATE_URL=(T / "nope.json").as_uri(),
                  PYTHONIOENCODING="utf-8")
from codex_fixture import rollout
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def cli(*a):
    r = subprocess.run([sys.executable, "-m", "cc_sessionsync", *a], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr
U = lambda: str(uuid.uuid4())
A1, O1, A2, O2 = U(), U(), U(), U()
root = T / "cfg/Claude/claude-code-sessions"; proj = H / ".claude/projects/p"; proj.mkdir(parents=True)
for a, o, n in ((A1, O1, 2), (A2, O2, 0)):
    d = root / a / o; d.mkdir(parents=True)
    for i in range(n):
        c = U(); (proj / f"{c}.jsonl").write_text("{}\n")
        (d / f"local_{i}.json").write_text(json.dumps({"sessionId": f"local_{i}", "cliSessionId": c, "title": f"旧号会话{i}", "cwd": "/p", "lastActivityAt": 1000 + i}))
ldb = T / "cfg/Claude/Local Storage/leveldb"; ldb.mkdir(parents=True)
(ldb / "000003.log").write_bytes(b'{"eventName":"e","clientTimestamp":"2026-10-16T00:00:00Z","accountUuid":"%s","organizationUuid":"%s"}' % (A2.encode(), O2.encode()))
rollout(H / ".codex", str(T / "proj"))
rc, out = cli("--version"); ok(rc == 0 and "0.3.8" in out, "--version")
rc, out = cli("status"); ok(rc == 0 and A2 in out and "Codex 对话 1 个" in out, "status")
rc, out = cli("sync", "--dry-run"); ok(rc == 0 and "可以补进来 2 个" in out and not list((root / A2 / O2).glob("local_*")), "sync --dry-run")
(T / "desktop-running").write_text("1")
rc, out = cli("sync"); ok(rc == 0 and "已排队" in out and not list((root / A2 / O2).glob("local_*")), "sync while desktop running → queued")
rc, out = cli("jobs"); ok("waiting" in out, "jobs lists it")
jid = out.split()[0]; rc, out = cli("jobs", "--cancel", jid); ok("已取消" in out, "jobs --cancel")
(T / "desktop-running").unlink()
rc, out = cli("sync"); ok(rc == 0 and "已补齐 2 个" in out and len(list((root / A2 / O2).glob("local_*"))) == 2, "sync")
rc, out = cli("backups"); bid = out.split()[0]; ok(rc == 0 and "补 2" in out, "backups")
rc, out = cli("restore", bid); ok(rc == 0 and '"removed": 2' in out and not list((root / A2 / O2).glob("local_*")), "restore")
rc, out = cli("codex", "list", "--days", "0"); ok(rc == 0 and "修登录页的 bug" in out, "codex list")
rc, out = cli("codex", "import", "--days", "0", "--apply"); ok(rc == 0 and "claude --resume" in out, "codex import --apply")
rc, out = cli("codex", "ledgers"); lid = out.split()[0]; ok(rc == 0 and "导入 1 个对话" in out, "codex ledgers")
rc, out = cli("codex", "undo", lid); ok(rc == 0 and "撤回了" in out, "codex undo")
rc, out = cli("diag"); ok(rc == 0 and "[Claude Code]" in out and str(H) not in out, "diag (home masked)")
rc, out = cli("auto", "--once"); ok(rc == 0, "auto --once")
rc, out = cli("selfcheck", "--offline"); ok("程序文件完整" in out and "数据目录可写" in out, "selfcheck --offline")
rc, out = cli("sessions", "list"); ok(rc == 0 and "修登录页的 bug" in out, "sessions list")
rc, out = cli("sessions", "backup"); ok(rc == 0 and "新快照" in out, "sessions backup")
rc, out = cli("usage"); ok(rc == 0 and "请求" in out, "usage")
rc, out = cli("key", "list"); ok(rc == 0, "key list (file keystore)")
rc, out = cli("--help"); ok("desktop" in out and "after-quit" not in out and "{key" not in out, "help hides internal commands")
# --- UI
try:
    urllib.request.urlopen("http://127.0.0.1:8799/api/ping", timeout=1); ok(False, "port 8799 is free (something else is listening)"); sys.exit(1)
except urllib.error.URLError:
    pass
p = subprocess.Popen([sys.executable, "-m", "cc_sessionsync", "ui", "--port", "8799", "--no-browser"], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            html = urllib.request.urlopen("http://127.0.0.1:8799/", timeout=1).read().decode("utf-8"); break
        except Exception:
            time.sleep(0.2)
    tok = html.split('const TOKEN = "')[1].split('"')[0]
    ok("<title>CC Session Sync</title>" in html and len(tok) > 10 and 'VERSION = "0.3.8"' in html, "UI page served with token")
    def g(u, t=None):
        rq = urllib.request.Request("http://127.0.0.1:8799" + u, headers={"x-token": tok if t is None else t})
        try:
            return json.loads(urllib.request.urlopen(rq, timeout=30).read())
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), code=e.code)
    ok(g("/api/status", "")["code"] == 403 and g("/api/sessions", "bad")["code"] == 403, "GET /api/* without the token → 403")
    ok(json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/ping", timeout=5).read())["app"] == "cc-sessionsync", "/api/ping needs no token")
    ok(urllib.request.urlopen("http://127.0.0.1:8799/icon.png", timeout=5).read()[:4] == b"\x89PNG", "icon served")
    ok(urllib.request.urlopen("http://127.0.0.1:8799/fonts/inter-regular.woff2", timeout=5).read()[:4] == b"wOF2", "fonts served")
    def post(u, b, t=tok):
        rq = urllib.request.Request("http://127.0.0.1:8799" + u, data=json.dumps(b).encode(), headers={"content-type": "application/json", "x-token": t})
        try:
            return json.loads(urllib.request.urlopen(rq, timeout=30).read())
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), code=e.code)
    s = g("/api/status"); ok(s["accounts"]["desktop"]["key"] == f"{A2}/{O2}", "api/status")
    pl = g("/api/sync/plan?source=desktop"); ok(pl["ok"] and len(pl["add"]) == 2, "api/sync/plan")
    ok(post("/api/sync", {"source": "desktop"}, "wrong").get("code") == 403, "POST without the token → 403")
    r = post("/api/sync", {"source": "desktop"}); ok(r.get("done") == 2, "api/sync")
    ok(len(g("/api/backups")) >= 2, "api/backups")
    ok(not any(x in html for x in ('data-p="models"', 'id="pg-models"', "function agPickOpen", "function pvSheet", "/api/route/", "/api/providers/", "/api/modelcfg")), "0.3.5: 供应商 page / nav / APIs gone from the page")
    ok(post("/api/route/prov-list", {}).get("code") == 404 and g("/api/modelcfg").get("code") == 404, "0.3.5: old provider APIs → 404")
    lg = g("/api/legacy"); ok(lg.get("version") == "0.3.5" and lg.get("notice") is False and lg.get("done") == [], f"0.3.5: legacy cleanup ran once at startup, nothing to restore → no notice ({lg})")
    cp = g("/api/codex/plan?days=0"); ok(cp["counts"]["new"] == 1 and cp["rows"][0]["turns"], "api/codex/plan (dry)")
    r = post("/api/codex/import", {"days": "0", "only": [cp["rows"][0]["thread"]], "desktop": "1"}); ok(r.get("done") == 1, "api/codex/import")
    r = post("/api/codex/undo", {"ledger": r["ledger"]}); ok(r.get("ok") and r["moved"] == 2, "api/codex/undo")
    d = g("/api/diag"); ok("[Codex]" in d["text"], "api/diag")
    ok(g("/api/status")["settings"]["codex_mark_imported"] is False, "mark setting defaults off in UI")
    ok(post("/api/settings", {"codex_mark_imported": True})["codex_mark_imported"] is True and g("/api/status")["settings"]["codex_mark_imported"] is True, "UI toggle persists the setting")
    cp = g("/api/codex/plan?days=0&mark=1"); ok(cp["mark"] and cp["mark_check"]["ok"], "api/codex/plan reports the mark check")
    r = post("/api/codex/import", {"days": "0", "desktop": "0"}); imp = H / ".codex/external_agent_session_imports.json"
    ok(r.get("marked", {}).get("ok") and imp.exists(), "api/codex/import uses the saved setting → marked")
    u = post("/api/codex/undo", {"ledger": r["ledger"]}); ok(u.get("codex_imports", {}).get("ok") and not imp.exists(), "api/codex/undo reverts the mark")
    post("/api/settings", {"codex_mark_imported": False})
    # 0.2.7 会话转换接口：方向表、来源列表、演练、导出（→ ZCode）、账本、撤回
    m = g("/api/convert/matrix"); ok(m["cells"]["claude"]["zcode"] == "export" and m["cells"]["codex"]["claude"] == "native" and "opencode" in m["detect"], "api/convert/matrix")
    src = g("/api/convert/sources?agent=codex"); ok(len(src["rows"]) >= 1 and src["rows"][0]["id"], "api/convert/sources")
    sid = src["rows"][0]["id"]
    pl = g(f"/api/convert/plan?src=codex&dst=zcode&ids={sid}"); ok(pl["mode"] == "export" and pl["rows"][0]["state"] == "new" and pl["dry"], "api/convert/plan (dry)")
    r = post("/api/convert/run", {"src": "codex", "dst": "zcode", "ids": [sid]}); ok(r.get("done") == 1 and Path(r["rows"][0]["file"]).exists() and r.get("paste"), "api/convert/run → export file + paste prompt")
    lg = g("/api/convert/ledgers"); ok(lg and lg[0]["name"] == r["ledger"] and lg[0]["count"] == 1, "api/convert/ledgers")
    u = post("/api/convert/undo", {"ledger": r["ledger"]}); ok(u.get("ok") and not Path(r["rows"][0]["file"]).exists(), "api/convert/undo removes the export")
    ok("（OpenCode / ZCode" in d["text"] or "OpenCode：" in d["text"], "api/diag mentions OpenCode / ZCode")
    rq = urllib.request.Request("http://127.0.0.1:8799/api/status", headers={"host": "evil.example"})
    try:
        urllib.request.urlopen(rq, timeout=5); ok(False, "foreign Host header refused")
    except urllib.error.HTTPError as e:
        ok(e.code == 403, "foreign Host header refused (DNS rebinding)")
finally:
    p.kill()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

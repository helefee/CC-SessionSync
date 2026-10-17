# -*- coding: utf-8 -*-
import os, sys, pathlib, json, tempfile, time, uuid, shutil
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="msix28-")); H = T / "home"; H.mkdir()
AD = H / "AppData/Roaming"; LA = H / "AppData/Local"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ab"), APPDATA=str(AD), XDG_CONFIG_HOME=str(AD), LOCALAPPDATA=str(LA),
                  CLAUDE_CONFIG_DIR=str(T / "claude"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"))
for k in ("CODEX_HOME", "CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_APP"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
from cc_sessionsync import claude_sync as C, detect
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
REAL = AD / "Claude"; MS = LA / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"
A1, O1, A2, O2 = (str(uuid.uuid4()) for _ in range(4))
def ldb(base, acct, org, ts, mt):
    l = base / "Local Storage/leveldb"; l.mkdir(parents=True, exist_ok=True); f = l / "000003.log"
    f.write_bytes(b'{"eventName":"b","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), acct.encode(), org.encode())); os.utime(f, (mt, mt))
proj = T / "claude/projects/p"; proj.mkdir(parents=True)
def sess(base, acct, org, n, mt):
    d = base / "claude-code-sessions" / acct / org; d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        cli = str(uuid.uuid4()); (proj / f"{cli}.jsonl").write_text("{}\n")
        f = d / f"local_{uuid.uuid4().hex[:8]}.json"; f.write_text(json.dumps({"cliSessionId": cli, "title": f"t{i}", "cwd": "/x", "lastActivityAt": 1760000000000 + i})); os.utime(f, (mt, mt))
    os.utime(d, (mt, mt))
def reset(): C.forget(); detect.forget()
now = time.time(); old = now - 44 * 86400
# --- case 1: this PC today: real %APPDATA% fresh (sessions + Local Storage), MSIX has only config-ish files
sess(REAL, A1, O1, 3, now - 60); ldb(REAL, A1, O1, "2026-10-17T05:00:00Z", now - 30)
MS.mkdir(parents=True); (MS / "config.json").write_text("{}")
reset(); r = C.app_resolve()
ok(r["parts"]["sessions"]["path"].startswith(str(REAL)) and r["parts"]["ldb"]["path"].startswith(str(REAL)), "PC case: sessions + Local Storage only in %APPDATA% → %APPDATA%")
ok(C.accounts()["desktop"]["key"] == f"{A1}/{O1}", "PC case: account from %APPDATA% Local Storage")
# --- case 2: reported case: %APPDATA% stale (44 d), MSIX active → MSIX for both
for f in (REAL / "claude-code-sessions").rglob("*"): os.utime(f, (old, old))
os.utime(REAL / "Local Storage/leveldb/000003.log", (old, old))
sess(MS, A1, O1, 2, now - 3600); (MS / "claude-code-sessions" / A2 / O2).mkdir(parents=True); ldb(MS, A2, O2, "2026-10-17T06:00:00Z", now - 300)
reset(); r = C.app_resolve()
ok(r["parts"]["sessions"]["path"].startswith(str(MS)) and r["parts"]["ldb"]["path"].startswith(str(MS)), "stale %APPDATA% + active MSIX → MSIX")
ok(C.accounts()["desktop"]["key"] == f"{A2}/{O2}", "account read from MSIX Local Storage (the switched one)")
p = C.plan(source="desktop")
ok(p.get("ok") and p["target"]["key"] == f"{A2}/{O2}" and len(p["add"]) == 2, f"plan targets MSIX folder, adds 2 (got {p.get('error')} {len(p.get('add') or [])})")
ok(str(C.sessions_root()).startswith(str(MS)), "sessions_root → MSIX")
# --- case 3: tie, desktop running from WindowsApps → MSIX; from AnthropicClaude → %APPDATA%
for b in (REAL, MS):
    for f in (b / "claude-code-sessions").rglob("*.json"): os.utime(f, (now - 100, now - 100))
    os.utime(b / "Local Storage/leveldb/000003.log", (now - 100, now - 100))
(T / "procs.json").write_text(json.dumps([{"name": "claude.exe", "exe": "C:\\Program Files\\WindowsApps\\Claude_2.42304.0.0_x64__pzs8sxrjxfjjc\\app\\claude.exe"}]))
reset(); r = C.app_resolve(); ok(r["msix_running"] is True and r["parts"]["ldb"]["path"].startswith(str(MS)) and "商店版" in r["parts"]["ldb"]["why"], "tie + store Desktop running → MSIX")
(T / "procs.json").write_text(json.dumps([{"name": "Claude.exe", "exe": "C:\\Users\\F\\AppData\\Local\\AnthropicClaude\\app-1\\Claude.exe"}]))
reset(); r = C.app_resolve(); ok(r["msix_running"] is False and r["parts"]["ldb"]["path"].startswith(str(REAL)), "tie + installer Desktop → %APPDATA%")
# --- case 4: manual override
(T / "ab").mkdir(exist_ok=True); (T / "ab/settings.json").write_text(json.dumps({"claude_app_dir": str(REAL)}))
os.utime(REAL / "Local Storage/leveldb/000003.log", (old, old))
reset(); r = C.app_resolve(); ok(r["override"] == str(REAL) and all(v["path"].startswith(str(REAL)) for v in r["parts"].values()), "manual override wins")
ok(str(C.claude_app_dir()) == str(REAL), "claude_app_dir = override")
(T / "ab/settings.json").write_text("{}"); reset()
b = C.app_dirs_brief(); ok(len(b["choices"]) == 2 and {x["part"] for x in b["parts"]} == {"sessions", "ldb", "idb"}, "brief lists choices + parts")
t = detect.text(); ok("桌面端会话列表：用" in t and "桌面端Local Storage（账号）：用" in t, "诊断 report shows dirs in use")
# --- Codex store
pk = LA / "Packages/OpenAI.Codex_2p2nqsd0c76g0"; (pk / "LocalCache/Local/Codex/Logs").mkdir(parents=True); (pk / "LocalCache/Roaming/Codex/web").mkdir(parents=True)
ok(detect.codex_store_homes() == [], "Codex store with only Logs/web → no store home (use ~/.codex)")
t = detect.text(); ok("商店版 Codex 已安装" in t and "用 ~/.codex" in t, "诊断 notes Codex store installed, no data there")
(pk / "LocalCache/Local/.codex/sessions").mkdir(parents=True)
ok(len(detect.codex_store_homes()) == 1, "Codex store home with sessions found")
r = detect.report(); ok(any(d["label"].startswith("商店版 Codex") and d["exists"] for d in r["codex"]["dirs"]), "store home listed as Codex data dir")
for n, e in [("Codex.exe", "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\\app\\Codex.exe"),
             ("ChatGPT.exe", "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe"),
             ("codex.exe", "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\\app\\resources\\codex.exe")]:
    ok(detect.classify(n, e) == [("codex", "desktop")], f"store Codex {n} → desktop running")
(T / "procs.json").write_text(json.dumps([{"name": "Codex.exe", "exe": "C:\\Program Files\\WindowsApps\\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\\app\\Codex.exe"}]))
reset(); r = detect.report(); ok(r["codex"]["running"] and r["codex"]["kinds"] == ["desktop"], "store Codex process → running")
print("FAILS", FAILS)
sys.exit(1 if [v for k, v in list(globals().items()) if k in ("fails", "FAILS") and v] else 0)

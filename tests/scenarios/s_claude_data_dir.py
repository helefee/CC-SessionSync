# -*- coding: utf-8 -*-
"""商店版桌面端 2026-11 起把数据目录搬到 %LOCALAPPDATA%\\Claude-Data\\claude_<包号>：会话列表和 Local Storage 都要能找到。"""
import os, sys, pathlib, json, tempfile, time, uuid
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="cdata-")); H = T / "home"; H.mkdir()
AD = H / "AppData/Roaming"; LA = H / "AppData/Local"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ab"), APPDATA=str(AD), XDG_CONFIG_HOME=str(AD), LOCALAPPDATA=str(LA),
                  CLAUDE_CONFIG_DIR=str(T / "claude"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"))
for k in ("CODEX_HOME", "CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_APP", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
from cc_sessionsync import claude_sync as C, detect
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def reset(): C.forget(); detect.forget()
REAL = AD / "Claude"; MS = LA / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude"; CD = LA / "Claude-Data/claude_pzs8sxrjxfjjc"
A1, O1, A2, O2 = (str(uuid.uuid4()) for _ in range(4))
proj = T / "claude/projects/p"; proj.mkdir(parents=True)
def ldb(base, acct, org, ts, mt):
    l = base / "Local Storage/leveldb"; l.mkdir(parents=True, exist_ok=True); f = l / "000003.log"
    f.write_bytes(b'{"eventName":"b","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), acct.encode(), org.encode())); os.utime(f, (mt, mt))
def sess(base, acct, org, n, mt):
    d = base / "claude-code-sessions" / acct / org; d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        cli = str(uuid.uuid4()); (proj / f"{cli}.jsonl").write_text("{}\n")
        f = d / f"local_{uuid.uuid4().hex[:8]}.json"; f.write_text(json.dumps({"cliSessionId": cli, "title": f"t{i}", "cwd": "/x", "lastActivityAt": 1760000000000 + i})); os.utime(f, (mt, mt))
    os.utime(d, (mt, mt))
now = time.time(); old = now - 10 * 86400
# --- missing → skipped; Claude-3p never matched
(LA / "Claude-3p/claude-code-sessions").mkdir(parents=True)
reset(); ok(detect.claude_data_dirs() == [], "no Claude-Data → nothing")
ok(not any("claudedata:" in c["id"] for c in C.app_dir_candidates()), "no Claude-Data candidate when missing")
(LA / "Claude-Data").mkdir(); (LA / "Claude-Data/README.txt").write_text("x"); (LA / "Claude-Data/Claude-3p").mkdir()
ok(detect.claude_data_dirs() == [], "Claude-Data/Claude-3p and files not matched")
# --- both cases matched
CD.mkdir(parents=True); (LA / "Claude-Data/Claude_otherpkg").mkdir()
got = {d.name for d in detect.claude_data_dirs()}
ok(got == {"claude_pzs8sxrjxfjjc", "Claude_otherpkg"}, f"claude_* and Claude_* matched ({got})")
(LA / "Claude-Data/Claude_otherpkg").rmdir()
# --- old leftover %APPDATA% stale + MSIX dir + Claude-Data active → Claude-Data for sessions and Local Storage
sess(REAL, A1, O1, 2, old); ldb(REAL, A1, O1, "2026-11-01T05:00:00Z", old)
MS.mkdir(parents=True); (MS / "config.json").write_text("{}")
sess(CD, A2, O2, 3, now - 60); ldb(CD, A2, O2, "2026-11-08T05:00:00Z", now - 30)
reset(); ids = [c["id"] for c in C.app_dir_candidates()]
ok(ids[0] == "appdata" and any(i.startswith("msix:") for i in ids) and "claudedata:claude_pzs8sxrjxfjjc" in ids, f"candidates keep appdata + msix + Claude-Data ({ids})")
r = C.app_resolve()
ok(r["parts"]["sessions"]["path"].startswith(str(CD)) and r["parts"]["ldb"]["path"].startswith(str(CD)), "active Claude-Data wins for sessions + Local Storage")
ok(C.accounts()["desktop"]["key"] == f"{A2}/{O2}", "desktop account read from Claude-Data Local Storage")
ok(str(C.sessions_root()).startswith(str(CD)), "sessions_root → Claude-Data")
dc = [d for d in detect.candidates("claude", settings={}) if d["kind"] == "app"]
ok(any(Path(d["path"]) == CD for d in dc) and any(Path(d["path"]) == REAL for d in dc) and any(Path(d["path"]) == MS for d in dc),
   "detect.candidates lists %APPDATA%, MSIX and Claude-Data")
ok(not any("Claude-3p" in str(d["path"]) for d in dc), "detect.candidates does not list Claude-3p as app dir")
# --- tie + store Desktop running → Claude-Data preferred
for b in (REAL, CD):
    for f in (b / "claude-code-sessions").rglob("*.json"): os.utime(f, (now - 100, now - 100))
    os.utime(b / "Local Storage/leveldb/000003.log", (now - 100, now - 100))
(T / "procs.json").write_text(json.dumps([{"name": "claude.exe", "exe": "C:\\Program Files\\WindowsApps\\Claude_2.5.0.0_x64__pzs8sxrjxfjjc\\app\\claude.exe"}]))
reset(); r = C.app_resolve(); ok(r["parts"]["ldb"]["path"].startswith(str(CD)), "tie + store Desktop → Claude-Data")
# --- dedupe: CLAUDE_USER_DATA_DIR pointing at the Claude-Data dir not listed twice
os.environ["CLAUDE_USER_DATA_DIR"] = str(CD); reset()
ok(sum(1 for c in C.app_dir_candidates() if Path(c["path"]).resolve() == CD.resolve()) == 1, "same resolved path listed once")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
# v0.7.6 auto-sync unit test on temp dirs only
import json, os, sys, tempfile, pathlib, time, uuid
T = pathlib.Path(tempfile.mkdtemp()); (T/"noprocs.json").write_text("[]")
os.environ.update(HOME=str(T/"home"), USERPROFILE=str(T/"home"), XDG_CONFIG_HOME=str(T/"cfg"), APPDATA=str(T/"cfg"), LOCALAPPDATA=str(T/"la"), CC_SESSIONSYNC_FAKE_PROCS=str(T/"noprocs.json"), CLAUDE_CONFIG_DIR=str(T/"home"/".claude"), CC_SESSIONSYNC_HOME=str(T/"ab"), CC_SESSIONSYNC_FAKE_APP=str(T/"desktop-running"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from cc_sessionsync import claude_sync as C
fails = []
def P(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
U = lambda: str(uuid.uuid4())
A1, O1, A2, O2 = U(), U(), U(), U()
root = T/"cfg"/"Claude"/"claude-code-sessions"; proj = T/"home"/".claude"/"projects"/"p"; proj.mkdir(parents=True)
(T/"home"/".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": A1, "organizationUuid": O1}}))
def sess(acct, org, n, title, last, arch=False):
    d = root/acct/org; d.mkdir(parents=True, exist_ok=True); cli = U(); (proj/f"{cli}.jsonl").write_text("{}\n")
    (d/f"local_{n}.json").write_text(json.dumps({"sessionId": f"local_{n}", "cliSessionId": cli, "title": title, "cwd": "/p", "lastActivityAt": last, "isArchived": arch, "error": "org disabled"}))
for i in range(3): sess(A1, O1, f"s{i}", f"老号会话{i}", 1000 + i)
(root/A2/O2).mkdir(parents=True)
ldb = T/"cfg"/"Claude"/"Local Storage"/"leveldb"; ldb.mkdir(parents=True)
def login(a, o, ts):
    with open(ldb/"000003.log", "ab") as f: f.write(b'{"eventName":"x","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), a.encode(), o.encode()))
login(A1, O1, "2026-10-01T00:00:00Z")
t0 = 1_900_000_000.0
# startup: desktop on A1 → nothing to do
P(C.auto_tick(now=t0) is None and not C.AUTO["pending"], "startup check: current account complete → nothing written")
# switch to A2 while desktop running → pending, no writes
(T/"desktop-running").write_text("1"); login(A2, O2, "2026-10-12T00:00:00Z")
P(C.auto_tick(now=t0 + 30) is None, "change seen → debounce")
P(C.auto_tick(now=t0 + 60) is None and C.AUTO["pending"] and C.AUTO["pending_n"] == 3 and not list((root/A2/O2).glob("local_*.json")), "desktop running → pending (3), nothing written")
# desktop quits → sync right away
(T/"desktop-running").unlink()
ev = C.auto_tick(now=t0 + 90)
got = sorted(f.name for f in (root/A2/O2).glob("local_*.json"))
P(ev and ev["added"] == 3 and got == ["local_s0.json", "local_s1.json", "local_s2.json"] and not C.AUTO["pending"], f"after desktop quit: added 3 {ev}")
P(all("error" not in json.loads((root/A2/O2/n).read_text()) for n in got), "stale error marks dropped")
P(not list((root/A2/O2).glob(".ccss-tmp-*")), "no temp files left (atomic writes)")
bk = C.list_backups(); P(bk and bk[0]["ledger"]["what"] == "auto-sync" and bk[0]["added"] == 3, "backup + ledger (auto-sync)")
# idempotent
n_bk = len(C.list_backups())
P(C.auto_tick(now=t0 + 1000) is None and len(C.list_backups()) == n_bk, "second run: nothing to do, no new backup (idempotent, no duplicates)")
# new session in old account + newer version of one → periodic pass refreshes
sess(A1, O1, "s9", "老号新会话", 5000)
d = json.loads((root/A1/O1/"local_s1.json").read_text()); d["lastActivityAt"] = 9000; (root/A1/O1/"local_s1.json").write_text(json.dumps(d))
c = json.loads((root/A2/O2/"local_s1.json").read_text()); c["isStarred"] = True; (root/A2/O2/"local_s1.json").write_text(json.dumps(c))
os.utime(root/A2/O2/"local_s1.json", (t0, t0))
C.AUTO["sig"] = C._auto_sig(); C.AUTO["changed"] = 0; C.AUTO["evaluated"] = t0 + 1000       # pretend nothing observable changed: rely on the 10-min period
P(C.auto_tick(now=t0 + 1300) is None, "period not reached → no work")
ev = C.auto_tick(now=t0 + 1700)
s1 = json.loads((root/A2/O2/"local_s1.json").read_text())
P(ev and ev["added"] == 1 and ev["updated"] == 1 and s1["lastActivityAt"] == 9000 and s1.get("isStarred"), f"periodic: new + refreshed, keeps star {ev}")
# tombstone respected
(root/A2/O2/"deleted_s0").write_text(""); (root/A2/O2/"local_s0.json").unlink(); C.AUTO["sig"] = C._auto_sig(); C.AUTO["changed"] = 0
C.auto_tick(now=t0 + 2400); P(not (root/A2/O2/"local_s0.json").exists(), "deleted in current account stays deleted")
# fresh writes in target → wait
sess(A1, O1, "s10", "x", 7000); os.utime(root/A2/O2/"local_s1.json", (t0 + 3100, t0 + 3100)); C.AUTO["sig"] = C._auto_sig(); C.AUTO["changed"] = 0
P(C.auto_tick(now=t0 + 3105) is None and not (root/A2/O2/"local_s10.json").exists(), "target written <15 s ago → wait")
P(C.auto_tick(now=t0 + 3200, force=True) and (root/A2/O2/"local_s10.json").exists(), "later → done")
# backup pruning
for i in range(25):
    sess(A1, O1, f"p{i}", "p", 8000 + i); C.auto_tick(now=t0 + 4000 + i * 700, force=True)
P(sum(1 for b in C.list_backups() if b["ledger"]["what"] == "auto-sync") == C.AUTO_KEEP, "auto backups capped at 20")
# restore of an auto-sync works
b0 = C.list_backups()[0]; r = C.restore(b0["id"]); P(r["ok"] and r["removed"] == b0["added"] >= 1, "restore undoes an auto-sync")
print("FAILS", fails)
print([ (b["id"], b["ledger"]["what"], b["added"]) for b in C.list_backups()][:30], C.AUTO["error"])
gone = b0["ledger"]["added"]
C.AUTO["sig"] = C._auto_sig(); C.AUTO["changed"] = 0
C.auto_tick(now=t0 + 90000, force=True)
P(not any((root/A2/O2/n).exists() for n in gone), "restored-away sessions are not re-added by auto-sync")
print("FAILS2", fails)
sys.exit(1 if [v for k, v in list(globals().items()) if k in ("fails", "FAILS") and v] else 0)

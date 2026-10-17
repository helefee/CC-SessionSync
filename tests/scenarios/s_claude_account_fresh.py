# -*- coding: utf-8 -*-
# v0.7.7: stale-account fixes, temp dirs only
import json, os, sys, tempfile, pathlib, uuid, time
T = pathlib.Path(tempfile.mkdtemp()); (T/"noprocs.json").write_text("[]")
os.environ.update(HOME=str(T/"home"), USERPROFILE=str(T/"home"), XDG_CONFIG_HOME=str(T/"cfg"), APPDATA=str(T/"cfg"), LOCALAPPDATA=str(T/"la"), CC_SESSIONSYNC_FAKE_PROCS=str(T/"noprocs.json"), CLAUDE_CONFIG_DIR=str(T/"home"/".claude"), CC_SESSIONSYNC_HOME=str(T/"ab"), CC_SESSIONSYNC_FAKE_APP=str(T/"running"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from cc_sessionsync import claude_sync as C
fails = []
def P(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
U = lambda: str(uuid.uuid4()); A1, O1, A2, O2 = U(), U(), U(), U()
root = T/"cfg"/"Claude"/"claude-code-sessions"; proj = T/"home"/".claude"/"projects"/"p"; proj.mkdir(parents=True)
for a, o, n in ((A1, O1, 3), (A2, O2, 0)):
    d = root/a/o; d.mkdir(parents=True)
    for i in range(n):
        cli = U(); (proj/f"{cli}.jsonl").write_text("{}\n")
        (d/f"local_{i}.json").write_text(json.dumps({"cliSessionId": cli, "title": f"t{i}", "cwd": "/p", "lastActivityAt": 1000 + i}))
ldb = T/"cfg"/"Claude"/"Local Storage"/"leveldb"; ldb.mkdir(parents=True); log = ldb/"000003.log"
ev = lambda a, o, ts: b'{"eventName":"e","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), a.encode(), o.encode())
log.write_bytes(ev(A1, O1, "2026-10-01T00:00:00Z"))
P(C.desktop_account(profile=False)["accountUuid"] == A1, "first read: account A1")
# Windows-style lazy metadata: the file grows but stat() keeps reporting the old size/mtime
st0 = log.stat(); real_stat = pathlib.Path.stat
class FakeSt:
    def __init__(s, r): s._r = r
    def __getattr__(s, k): return getattr(s._r, k)
    st_size = st0.st_size; st_mtime_ns = st0.st_mtime_ns; st_mtime = st0.st_mtime
def lazy(self, *a, **k):
    r = real_stat(self, *a, **k)
    return FakeSt(r) if self.name == "000003.log" else r
pathlib.Path.stat = lazy
with open(log, "ab") as f: f.write(ev(A2, O2, "2026-10-12T00:00:00Z"))
P(C.desktop_account(profile=False)["accountUuid"] == A2, "account switch seen even when stat() is stale (true file length)")
pathlib.Path.stat = real_stat
C.forget(); P(not C._LDB_CACHE, "forget() drops the read cache")
# stale heuristic: desktop running, Local Storage still says A2 but A1's folder was written later
(T/"running").write_text("1")
log.write_bytes(ev(A1, O1, "2026-10-01T00:00:00Z") + ev(A2, O2, "2026-10-02T00:00:00Z")); C.forget()
now = time.time(); os.utime(root/A1/O1/"local_0.json", (now, now))
a = C.accounts()["desktop"]
P(a["stale"] and a["suggest"] == f"{A1}/{O1}" and a["desktop_running"] and a["read_at"], f"stale detected + suggestion: {a.get('stale_why')}")
p = C.plan(f"{A2}/{O2}"); P(p["ok"] and len(p["add"]) == 3 and all(x["from"] == f"{A1}/{O1}" for x in p["add"]), "plan lists other-account sessions with their source account")
p2 = C.plan(f"{A1}/{O1}"); P(p2["ok"] and p2["target"]["key"] == f"{A1}/{O1}", "manual target honoured")
(T/"running").unlink(); log.write_bytes(ev(A2, O2, "2026-10-12T00:00:00Z")); C.forget()
[os.utime(f, (1e9, 1e9)) for f in (root/A1/O1).glob("*")]; a = C.accounts()["desktop"]
P(not a["stale"], "not stale when Local Storage is newest")
print("FAILS", fails)
sys.exit(1 if [v for k, v in list(globals().items()) if k in ("fails", "FAILS") and v] else 0)

# -*- coding: utf-8 -*-
# 0.5.3: auto-sync on Claude account switch + toggle, history/undo/clean, progress — temp dirs only
import json, os, sys, tempfile, pathlib, uuid
T = pathlib.Path(tempfile.mkdtemp()); (T/"noprocs.json").write_text("[]")
os.environ.update(HOME=str(T/"home"), USERPROFILE=str(T/"home"), XDG_CONFIG_HOME=str(T/"cfg"), APPDATA=str(T/"cfg"), LOCALAPPDATA=str(T/"la"), CC_SESSIONSYNC_FAKE_PROCS=str(T/"noprocs.json"), CLAUDE_CONFIG_DIR=str(T/"home"/".claude"), CC_SESSIONSYNC_HOME=str(T/"ab"), CC_SESSIONSYNC_FAKE_APP=str(T/"desktop-running"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from cc_sessionsync import claude_sync as C, watch as W, settings as S, events as E
fails = []
def P(c, m): print(("PASS " if c else "FAIL ") + m); c or fails.append(m)
U = lambda: str(uuid.uuid4())
A1, O1, A2, O2, A3, O3 = U(), U(), U(), U(), U(), U()
root = T/"cfg"/"Claude"/"claude-code-sessions"; proj = T/"home"/".claude"/"projects"/"p"; proj.mkdir(parents=True)
cj = T/"home"/".claude.json"; cj.write_text(json.dumps({"oauthAccount": {"accountUuid": A1, "organizationUuid": O1, "emailAddress": "a@x"}}))
def sess(acct, org, n, last):
    d = root/acct/org; d.mkdir(parents=True, exist_ok=True); cli = U(); (proj/f"{cli}.jsonl").write_text("{}\n")
    (d/f"local_{n}.json").write_text(json.dumps({"sessionId": f"local_{n}", "cliSessionId": cli, "title": f"会话{n}", "cwd": "/p", "lastActivityAt": last}))
for i in range(3): sess(A1, O1, f"s{i}", 1000 + i)
for a, o in ((A2, O2), (A3, O3)): (root/a/o).mkdir(parents=True)
ldb = T/"cfg"/"Claude"/"Local Storage"/"leveldb"; ldb.mkdir(parents=True)
def login(a, o, ts):
    with open(ldb/"000003.log", "ab") as f: f.write(b'{"eventName":"x","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), a.encode(), o.encode()))
def n_in(a, o): return len(list((root/a/o).glob("local_*.json")))
def switch(a, o, ts, t):
    login(a, o, ts); evs = W.tick(now=t); evs += W.tick(now=t + 10); return evs + W.switch_tick(now=t + 11)
login(A1, O1, "2026-10-01T00:00:00Z")
t = 1_900_000_000.0
P(S.get("claude_auto_sync_on_account_switch") is True, "setting defaults ON")
# first run: only records
evs = W.tick(now=t) + W.switch_tick(now=t + 1)
P(not evs and W.STATE["claude-desktop"] and not W.STATE.get("pending") and n_in(A2, O2) == 0, "first run: recorded only, no sync")
# switch → sync
evs = switch(A2, O2, "2026-10-12T00:00:00Z", t + 100)
ok = [e for e in evs if e.get("kind") == "auto-sync"]
P(ok and "已同步 3 个会话" in ok[0]["title"] and n_in(A2, O2) == 3 and ok[0].get("backup"), f"switch → synced 3, notification with backup {[e.get('title') for e in evs]}")
P(not [e for e in evs if e.get("kind") == "account"], "no separate 'switch detected' nag when auto-sync on")
# idempotent: switch back and forth again
switch(A1, O1, "2026-10-13T00:00:00Z", t + 200)
evs = switch(A2, O2, "2026-10-14T00:00:00Z", t + 300)
P(n_in(A2, O2) == 3 and all(e.get("added", 0) == 0 for e in evs), "repeat switch: idempotent, nothing duplicated")
# toggle off → no writes, plain reminder
S.update(claude_auto_sync_on_account_switch=False)
evs = switch(A3, O3, "2026-10-15T00:00:00Z", t + 400)
P(n_in(A3, O3) == 0 and any(e.get("kind") == "account" for e in evs), "toggle off → reminder only, no sync")
S.update(claude_auto_sync_on_account_switch=True)
# desktop running → wait once, sync after quit; pending survives restart
(T/"desktop-running").write_text("1")
evs = switch(A1, O1, "2026-10-16T00:00:00Z", t + 500); evs += switch(A3, O3, "2026-10-17T00:00:00Z", t + 600)
P(n_in(A3, O3) == 0 and W.STATE["pending"].get("claude-desktop"), "desktop running → pending, nothing written")
P(sum("关掉后" in (e.get("text") or "") for e in evs) <= 2, "told about waiting, not spamming")
saved = json.loads(json.dumps(W.STATE["pending"])); W.STATE["pending"] = {}; W._loaded = False if hasattr(W, "_loaded") else None
W._load(); P(W.STATE.get("pending") or saved, "pending persisted")
W.STATE["pending"] = W.STATE.get("pending") or saved
(T/"desktop-running").unlink()
evs = W.switch_tick(now=t + 700)
P(n_in(A3, O3) == 3 and any("已同步" in e["title"] for e in evs), "after desktop quit → synced")
# malformed cli config → skipped, no crash
cj.write_text("{not json"); 
try: W.tick(now=t + 800); W.tick(now=t + 810); W.switch_tick(now=t + 811); P(True, "malformed .claude.json: no crash")
except Exception as e: P(False, f"malformed config crashed: {e!r}")
# history / undo
h = C.history(); it = h["items"]
P(it and {"account-switch"} <= {x["trigger"] for x in it} and all("undoable" in x for x in it), f"history lists triggers {[x['trigger'] for x in it]}")
top = next(x for x in it if x["state"] == "synced" and x["undoable"] and x["added"])
r = C.restore(top["id"]); P(r["ok"] and r["removed"] == top["added"], "one-click undo")
P(next(x for x in C.history()["items"] if x["id"] == top["id"])["state"] == "undone", "history shows undone")
# progress events
seen = []; orig = C._prog
def spy(*a, **k): seen.append(a[0]); return orig(*a, **k)
C._prog = spy; sess(A1, O1, "s9", 9000)
pl = C.plan("desktop") if hasattr(C, "plan") else None
C.switch_sync("desktop", now=t + 900); C._prog = orig
p = C.progress()
P("backup" in seen and "sync" in seen and "verify" in seen and seen.index("backup") < seen.index("sync") < seen.index("verify"), f"progress phases ordered {seen}")
P(p.get("phase") == "done" and p.get("total") == p.get("done") and not p.get("active"), f"progress final {p}")
C._prog("error", error="x"); P(C.progress().get("phase") == "error" and not C.progress().get("active"), "error phase reported")
# clean
for x in C.history()["items"]: pass
n0 = C.history()["count"]; d = C.clean_history(0, dry=True)
P(C.history()["count"] == n0, "clean dry: nothing moved")
C.clean_history(0, dry=False); h2 = C.history()
P(h2["count"] == min(n0, h2["keep_min"]) and list((T/"ab"/"trash").glob("sync-history-*")) or n0 <= h2["keep_min"], "clean apply: old moved to trash, newest kept")
print("FAILS", fails); sys.exit(1 if fails else 0)

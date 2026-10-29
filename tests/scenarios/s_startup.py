# -*- coding: utf-8 -*-
"""0.4.0：启动先给上次的会话列表（快照），后台按文件头缓存增量刷新；没改过的文件不再打开；列表精简、点开再要详情；大会话只留最后几条；空闲放掉缓存。全在临时目录。"""
import os, sys, pathlib, json, tempfile, hashlib, sqlite3, subprocess, uuid, time
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-xs-")); H = T / "home"; H.mkdir()
OCD, ZH, DH = T / "xdg" / "opencode", H / ".zcode", T / "dshhome"
if os.name == "nt":
    cli = T / "opencode.cmd"; cli.write_text(f'@"{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" %*\n')
else:
    cli = T / "opencode"; cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" "$@"\n'); cli.chmod(0o755)
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
                  XDG_CONFIG_HOME=str(H / "AppData/Roaming"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_OPENCODE_DATA=str(OCD), CC_SESSIONSYNC_ZCODE_HOME=str(ZH), CC_SESSIONSYNC_OPENCODE_CLI=str(cli),
                  FAKE_OC_LOG=str(T / "oc.log"), CURSOR_USER_DIR=str(T / "cu"), CURSOR_CONFIG_DIR=str(H / ".cursor"),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_OPENCODE_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_ZCODE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_NO_WATCHER="1")
os.environ.pop("XDG_DATA_HOME", None)
(T / "procs.json").write_text("[]")
import builtins
import oc_fixture as OF, codex_fixture as CF, dsh_fixture as DF, cursor_fixture as CuF
from cc_sessionsync import sessions as S, listcache as L, ui, dsh, paths
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
CWD = str(T / "proj"); Path(CWD).mkdir()
OF.opencode(OCD, CWD); OF.zcode(ZH, CWD)
CF.rollout(H / ".codex", CWD)
DSID = str(uuid.uuid4()); DF.rich(DSID, CWD, DH).write(DH / "sessions")
CUD = T / "cu"; CuF.rich(CUD, CWD, CuF.ws_storage(CUD, CWD)).write(CUD)
NOW = time.time()
def claude(sid, title, ask, n=1, days=1.0):
    pj = H / ".claude" / "projects" / "-proj"; pj.mkdir(parents=True, exist_ok=True)
    recs = []
    for i in range(n):
        recs += [{"type": "user", "uuid": f"u{i}", "parentUuid": None, "sessionId": sid, "cwd": CWD, "timestamp": "2026-10-01T00:00:00.000Z",
                  "message": {"role": "user", "content": f"{ask} #{i}"}},
                 {"type": "assistant", "uuid": f"a{i}", "parentUuid": f"u{i}", "sessionId": sid, "cwd": CWD, "timestamp": "2026-10-01T00:00:01.000Z",
                  "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": f"答 {i}"}]}}]
    if title:
        recs.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    f = pj / f"{sid}.jsonl"; f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    t = NOW - days * 86400; os.utime(f, (t, t))
    return f
SIDS = [str(uuid.uuid4()) for _ in range(300)]
for i, sid in enumerate(SIDS):
    claude(sid, f"会话 {i}", "很长的第一句话 " * 30, days=1 + i / 100)
BIG = str(uuid.uuid4()); claude(BIG, "大会话", "问", n=1500, days=0.5)

# 读文件计数（sessions 里的 open）
READS = []
real_open = builtins.open
def counting_open(f, *a, **k):
    if str(f).endswith(".jsonl") and ".claude" in str(f):
        READS.append(str(f))
    return real_open(f, *a, **k)
S.open = counting_open
loads = []
real_load = dsh.load
import traceback
dsh.load = lambda f, *a, **k: loads.append("".join(traceback.format_stack(limit=6))) or real_load(f, *a, **k)

def relaunch():
    """当成程序重开：内存里的东西都清掉，磁盘上的缓存留着。"""
    S._LIST.update(bytes=None, at=0.0, gen=0, sig=None, refreshing=False, booted=False, snap=None)
    L.reset_memory(); dsh._CACHE.clear(); READS.clear(); loads.clear()
def wait_refresh(t=30):
    end = time.time() + t
    while time.time() < end and (S._LIST["refreshing"] or S._LIST["snap"] is not None):
        time.sleep(0.05)

# ---------- 第一次启动：没有缓存，现算 ----------
r = S.list_parsed()
n0 = len(r["items"])
ok(r["stale"] is False and n0 >= 306 and {x["agent"] for x in r["items"]} >= {"claude", "codex", "dsh", "cursor", "opencode", "zcode"}, f"first launch: computed list of {n0}, all six apps")
ok(len(READS) >= 301 and L.heads_file().exists() and L.list_file().exists(), f"first launch reads files ({len(READS)}) and writes caches")
ok(list(r)[0] == "stale", "first key of the list JSON is stale")
x = next(x for x in r["items"] if x["agent"] == "claude")
ok("file" not in x and "resume" not in x and len(x["first"]) <= S.FIRST_MAX, "list items are slim (no file / resume, first cut)")
it = ui.H._get(None, "/api/sessions/item", {"agent": "claude", "id": x["id"]})
ok(it["ok"] and it["item"]["resume"].endswith(x["id"]) and it["item"]["file"].endswith(".jsonl"), "details load on demand (/api/sessions/item)")
READS.clear()
r2 = S.list_parsed()
ok(r2["gen"] == r["gen"] and not READS, "second call in the same run: no file re-read, same gen")

# ---------- 重开：先给快照，后台刷新，没改过的文件一个都不读 ----------
relaunch()
t0 = time.time(); raw = ui.H._get(None, "/api/sessions", {}); dt = time.time() - t0
v = json.loads(raw["_raw"])
ok(v["stale"] is True and v.get("cached_at") and len(v["items"]) == n0, f"relaunch: snapshot served immediately ({dt*1000:.0f} ms), stale")
ok(ui.H._get(None, "/api/sessions/gen", {})["stale"] in (True, False), "gen endpoint")
wait_refresh()
g = S.list_gen()
ok(not g["stale"] and not g["refreshing"], "background refresh finished")
ok(not READS and not loads, f"refresh with nothing changed: 0 Claude files opened ({len(READS)}), 0 dsh sessions decoded ({len(loads)})")
v2 = S.list_parsed()
ok(v2["stale"] is False and len(v2["items"]) == n0 and v2["gen"] == g["gen"], "after refresh: fresh list, same content")

# ---------- 改了一个、加了一个：只读这两个 ----------
relaunch()
S.list_view()
claude(SIDS[5], "改过的标题", "改过", days=0.1)
NEW = str(uuid.uuid4()); claude(NEW, "新来的", "新的", days=0.05)
wait_refresh()
ok(sorted(Path(p).stem for p in READS) == sorted([SIDS[5], NEW]), f"incremental: only the changed + new file read ({len(READS)})")
v3 = S.list_parsed()
t = {x["id"]: x for x in v3["items"]}
ok(t[SIDS[5]]["title"] == "改过的标题" and NEW in t and len(v3["items"]) == n0 + 1, "changes show up")
# 程序开着的时候：马上就是新的（不是快照）
NEW2 = str(uuid.uuid4()); claude(NEW2, "又一个", "x", days=0.01)
READS.clear(); S.list_parsed(); S.list_parsed()
ok(not READS, "two requests within 2 s share one computation")
S._LIST["at"] -= S.REUSE_S + 0.1
ok(any(x["id"] == NEW2 for x in S.list_parsed()["items"]), "while running: new session visible once the 2 s window passed")
(H / ".claude" / "projects" / "-proj" / f"{NEW2}.jsonl").unlink()
ui.H._post(None, "/api/settings", {})            # 任何 POST 之后：下次一定重新看
ok(not any(x["id"] == NEW2 for x in S.list_parsed()["items"]), "after a POST the list is recomputed at once (deleted session gone)")

# ---------- 坏缓存 / 上限 / 空闲放内存 ----------
relaunch()
L.heads_file().write_text("{broken", encoding="utf-8"); L.list_file().write_bytes(b"garbage")
v4 = S.list_parsed()
ok(v4["stale"] is False and len(v4["items"]) == len(v3["items"]), "corrupt caches ignored, list rebuilt")
L.MAX_HEADS = 50; L._dirty = True; L.save()
ok(L.head_count() <= 50, f"head cache capped ({L.head_count()})")
L.MAX_HEADS = 60000
S._LIST["at"] = time.time() - 9999; L._used_at = time.time() - 9999
ok(S.release_idle(60) and S._LIST["bytes"] is None and L._heads is None, "idle: caches released from memory")
ok(len(S.list_parsed()["items"]) == len(v4["items"]), "list works again after release")

# ---------- 大会话：只留最后几条 ----------
rd = S.read("claude", BIG, 50)
ok(rd["ok"] and rd["total"] == 3000 and len(rd["entries"]) == 50 and rd["cut"] == 2950 and rd["entries"][-1]["text"] == "答 1499", "big session: last 50 entries only, totals right")

# ---------- 打码：快照里没有完整邮箱 ----------
b = ui._ser_list({"stale": False, "gen": 1, "groups": [{"key": "a", "email": "li.na@example.com", "name": "李娜"}], "items": [{"agent": "claude", "id": "x"}]})
ok(b.startswith(b'{"stale": false') and b"li.na@example.com" not in b and "李娜".encode() not in b, "serialized list masks accounts, stale first")
b = ui._ser_list({"stale": False, "gen": 1, "items": [{"agent": "claude", "id": "x", "acct": {"email": "li.na@example.com"}}]})
ok(b"li.na@example.com" not in b, "items with account dicts are masked too")
html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("/api/sessions/gen" in html and "/api/sessions/item" in html and "ssStale" in html, "UI: stale tag + background swap + detail on demand")
print("FAILS:", FAILS)
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""会话管理：列表 / 读 / 搜 / 导出 / 备份库（增量、去重）/ 改名（只改别名、写进工具标题）/ 归档 / 删除进回收站 / 还原（原位或新副本）/
彻底删除要确认 / 从备份库恢复 / 账本撤回 / 桌面端开着时排队。全在临时目录。"""
import hashlib, json, os, sqlite3, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-sess-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
info = demo_env.build(T)
H = T / "home"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def snap_tree(*roots):
    out = {}
    for r in roots:
        for f in Path(r).rglob("*"):
            if f.is_file():
                out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out
CL, CX, APPD = H / ".claude", H / ".codex", T / "cfg/Claude"
# Codex 的 state 库（和真的一样有 threads 表）
rolls = sorted((CX / "sessions").rglob("rollout-*.jsonl"))
db = CX / "state_5.sqlite"
c = sqlite3.connect(db)
c.execute("create table threads (id text primary key, rollout_path text, created_at integer, updated_at integer, source text, model_provider text, cwd text, title text, "
          "sandbox_policy text, approval_mode text, tokens_used integer, has_user_event integer, archived integer, archived_at integer, first_user_message text, name text)")
for f in rolls:
    tid = f.stem[-36:]
    c.execute("insert into threads (id, rollout_path, created_at, updated_at, model_provider, cwd, title, archived, first_user_message) values (?,?,?,?,?,?,?,0,?)",
              (tid, str(f), int(f.stat().st_mtime), int(f.stat().st_mtime), "openai", "/work/x", "t-" + tid[:4], "登录页点了没反应"))
c.commit(); c.close()

from cc_sessionsync import apps, sessions, ui
L = sessions.list_all()
ok(len([x for x in L["items"] if x["agent"] == "claude"]) == 6 and len([x for x in L["items"] if x["agent"] == "codex"]) == 4, "list_all: 6 Claude + 4 Codex")
ok("/work/shop" in L["projects"] and len(L["accounts"]) == 2, "projects + accounts for filters")
cl = next(x for x in L["items"] if x["title"] == "重构支付模块的回调")
cx = next(x for x in L["items"] if x["agent"] == "codex")
r = sessions.read("claude", cl["id"]); ok(r["ok"] and r["entries"][0]["role"] == "user" and "webhook" in r["entries"][0]["text"], "read Claude transcript")
r = sessions.read("codex", cx["id"]); ok(r["ok"] and any(e["role"] == "assistant" for e in r["entries"]) and not any("AGENTS.md" in e["text"] for e in r["entries"]), "read Codex transcript (skips injected context)")
ok(any(h["id"] == cl["id"] for h in sessions.fulltext("指数退避")["hits"]), "fulltext search")
e1, e2 = sessions.export("claude", cl["id"], "md"), sessions.export("codex", cx["id"], "jsonl")
ok(e1["ok"] and Path(e1["path"]).read_text(encoding="utf-8").count("webhook") and str(T / "ss") in e1["path"], "export md into ~/.cc-sessionsync/exports")
ok(e2["ok"] and Path(e2["path"]).read_bytes() == Path(cx["file"]).read_bytes(), "export jsonl = raw copy")

before = snap_tree(CL, CX, APPD)
b1 = sessions.backup(why="test"); ok(b1["snapshots"] == 10, f"backup: 10 snapshots ({b1['snapshots']})")
b2 = sessions.backup(why="test"); ok(b2["snapshots"] == 0 and b2["skipped"] == 10, "backup again: incremental (nothing new)")
f = Path(cl["file"]); data = f.read_bytes()
with open(f, "ab") as fh: fh.write(b'{"type":"user","message":{"role":"user","content":"more"}}\n')
b3 = sessions.backup(why="test"); ok(b3["snapshots"] == 1, "backup after a change: 1 new snapshot")
os.utime(f, (time.time() + 5, time.time() + 5)); b4 = sessions.backup(why="test")
ok(b4["snapshots"] == 0, "touch without content change: deduped by hash")
f.write_bytes(data); before[str(f)] = hashlib.sha256(data).hexdigest()
st = sessions.backup_status(); ok(st["snapshots"] == 11 and st["sessions"] == 10, "backup_status")
ok(snap_tree(CL, CX, APPD) == before, "backups never modify the source files")

# 改名：只改别名
r = sessions.rename("claude", cl["id"], "支付回调（新名字）")
ok(r["ok"] and snap_tree(CL, CX, APPD) == before, "rename (alias only) touches no tool files")
ok(next(x for x in sessions.list_all()["items"] if x["id"] == cl["id"])["title"] == "支付回调（新名字）", "alias shows in the list")
u = sessions.undo(r["op"]); ok(u["ok"] and next(x for x in sessions.list_all()["items"] if x["id"] == cl["id"])["title"] == "重构支付模块的回调", "undo rename → alias gone")
# 改名：写进 Claude 自己的标题（桌面端登记的 title + 记录末尾 custom-title）
reg = sessions._claude_registry()[cl["id"]]
r = sessions.rename("claude", cl["id"], "支付回调 v2", write_tool=True)
tail = Path(cl["file"]).read_text(encoding="utf-8").strip().splitlines()[-1]
idx_titles = [json.loads(Path(x["path"]).read_text(encoding="utf-8")).get("title") for x in sessions._claude_registry()[cl["id"]]]
ok(r["ok"] and ("支付回调 v2" in idx_titles or json.loads(tail).get("customTitle") == "支付回调 v2"), f"rename write_tool (Claude) {idx_titles} {tail[:80]}")
u = sessions.undo(r["op"]); ok(u["ok"] and snap_tree(CL, CX, APPD) == before, "undo Claude title rename → files byte-identical")
# Codex：session_index 追加 + 库里改 title
r = sessions.rename("codex", cx["id"], "Codex 新标题", write_tool=True)
last = json.loads((CX / "session_index.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
ok(r["ok"] and last["id"] == cx["id"] and last["thread_name"] == "Codex 新标题", "rename write_tool (Codex) appends session_index")
c = sqlite3.connect(db); t = c.execute("select name from threads where id=?", (cx["id"],)).fetchone()[0]; c.close()
ok(t == "Codex 新标题", "rename write_tool (Codex) updates threads.name (like codex-rs update_thread_name)")
u = sessions.undo(r["op"]); c = sqlite3.connect(db); t = c.execute("select name from threads where id=?", (cx["id"],)).fetchone()[0]; c.close()
ok(u["ok"] and t is None and (CX / "session_index.jsonl").read_bytes() == Path(before and CX / "session_index.jsonl").read_bytes(), "undo Codex rename")
ok(hashlib.sha256((CX / "session_index.jsonl").read_bytes()).hexdigest() == before[str(CX / "session_index.jsonl")], "session_index byte-identical after undo")

# 归档 / 取消归档
r = sessions.batch("archive", [{"agent": "claude", "id": cl["id"]}])
ok(r["ok"] and all(json.loads(Path(x["path"]).read_text(encoding="utf-8")).get("isArchived") for x in sessions._claude_registry()[cl["id"]]), "archive Claude → isArchived in the Desktop index")
sessions.undo(r["op"]); ok(not any(json.loads(Path(x["path"]).read_text(encoding="utf-8")).get("isArchived") for x in sessions._claude_registry()[cl["id"]]), "undo archive")
r = sessions.batch("archive", [{"agent": "codex", "id": cx["id"]}])
c = sqlite3.connect(db); row = c.execute("select archived, rollout_path from threads where id=?", (cx["id"],)).fetchone(); c.close()
ok(r["ok"] and row[0] == 1 and "archived_sessions" in row[1] and Path(row[1]).exists() and not Path(cx["file"]).exists(), "archive Codex → moved to archived_sessions + db")
r2 = sessions.batch("unarchive", [{"agent": "codex", "id": cx["id"]}])
c = sqlite3.connect(db); row = c.execute("select archived, rollout_path from threads where id=?", (cx["id"],)).fetchone(); c.close()
ok(r2["ok"] and row[0] == 0 and Path(row[1]).exists() and "archived_sessions" not in row[1], "unarchive Codex")
before = snap_tree(CL, CX, APPD)

# 删除：先备份，再挪进回收站（文件 + 桌面端登记）
regs = [x["path"] for x in sessions._claude_registry()[cl["id"]]]
n0 = sessions.backup_status()["snapshots"]
r = sessions.batch("delete", [{"agent": "claude", "id": cl["id"]}, ])
tr = T / "ss/trash" / r["trash"]
ok(r["ok"] and not Path(cl["file"]).exists() and not any(Path(p).exists() for p in regs), "delete → transcript + Desktop index entry moved away")
ok(len(list(tr.rglob(f"*{cl['id']}.jsonl"))) == 1 and (tr / "manifest.json").exists(), "…into the recycle bin with a manifest")
ok(any(s["sid"] == cl["id"] for s in sessions.snapshots()) and sessions.backup_status()["snapshots"] >= n0, "backed up to the DB before deleting")
ok(cl["id"] not in {x["id"] for x in sessions.list_all()["items"]}, "deleted session gone from the list")
ok(any(t["id"] == r["trash"] for t in sessions.trash_list()), "trash_list")
rr = sessions.trash_restore(r["trash"])
ok(rr["ok"] and rr["restored"] >= 1 and not rr["copies"] and snap_tree(CL, CX, APPD) == before, "restore from the bin → original paths, byte-identical")
# 再删一次，原位置又有了文件 → 还原成新副本（不覆盖）
r = sessions.batch("delete", [{"agent": "claude", "id": cl["id"]}])
Path(cl["file"]).write_text('{"type":"user","message":{"role":"user","content":"新的同名文件"}}\n', encoding="utf-8")
rr = sessions.trash_restore(r["trash"])
ok(rr["copies"] and Path(rr["copies"][0]["path"]).exists() and rr["copies"][0]["new_id"] != cl["id"] and "新的同名文件" in Path(cl["file"]).read_text(encoding="utf-8"),
   "restore when the original exists → new copy with a new session id, original untouched")
# Codex 删除 / 撤回（db 行也回来）
r = sessions.batch("delete", [{"agent": "codex", "id": cx["id"]}])
c = sqlite3.connect(db); gone = c.execute("select count(*) from threads where id=?", (cx["id"],)).fetchone()[0] == 0; c.close()
ok(r["ok"] and gone, "delete Codex → rollout to the bin + db row removed")
u = sessions.undo(r["op"])
c = sqlite3.connect(db); back = c.execute("select count(*) from threads where id=?", (cx["id"],)).fetchone()[0] == 1; c.close()
ok(u.get("ok") and back and sessions.codex_file(cx["id"]), "undo Codex delete → file + db row back")
# 彻底删除要输入 DELETE
r = sessions.batch("delete", [{"agent": "claude", "id": info["sessions"][5][0]}])
p = sessions.trash_purge(r["trash"]); ok(not p["ok"] and (T / "ss/trash" / r["trash"]).exists(), "purge without confirmation refused")
p = sessions.trash_purge(r["trash"], "DELETE"); ok(p["ok"] and not (T / "ss/trash" / r["trash"]).exists(), "purge with DELETE")
# 从备份库恢复：原位置没了 → 写回原处；还在 → 新副本
sid5 = info["sessions"][5][0]
snap = next(s for s in sessions.snapshots() if s["sid"] == sid5)
x = sessions.restore_snapshot(snap["id"]); ok(x["ok"] and x["how"] == "original" and Path(snap["path"]).exists(), "restore_snapshot → original path when missing")
y = sessions.restore_snapshot(snap["id"]); ok(y["ok"] and y["how"] == "copy" and y["path"] != snap["path"], "restore_snapshot again → new copy")
u = sessions.undo(y["op"]); ok(u["ok"] and not Path(y["path"]).exists() and list((T / "ss/trash").glob(f"undo-{y['op']}/*")), "undo a restore → the copy goes to the bin, not deleted")
ok(len(sessions.ops_list()) >= 10 and all(o["summary"] for o in sessions.ops_list()), "every write is in the ledger")

# 接口：桌面端开着 → 删除 / 归档 / 写进工具的改名都排队，文件不动
srv = ui.start(8799)
ok(srv is not None, "server on 8799")
tok = ui.TOKEN
def post(u, b):
    rq = urllib.request.Request("http://127.0.0.1:8799" + u, data=json.dumps(b).encode(), headers={"content-type": "application/json", "x-token": tok})
    try:
        return json.loads(urllib.request.urlopen(rq, timeout=30).read())
    except urllib.error.HTTPError as e:
        return dict(json.loads(e.read()), code=e.code)
(T / "desktop-running").write_text("1")
before = snap_tree(CL, CX, APPD)
sid2 = info["sessions"][2][0]
r = post("/api/sessions/batch", {"action": "delete", "items": [{"agent": "claude", "id": sid2}]})
ok(r["results"][0].get("deferred") and snap_tree(CL, CX, APPD) == before, "API delete while Desktop runs → queued, nothing touched")
r = post("/api/sessions/rename", {"agent": "claude", "id": sid2, "name": "x", "write_tool": True}); ok(r.get("deferred"), "API rename (write_tool) while Desktop runs → queued")
r = post("/api/sessions/rename", {"agent": "claude", "id": sid2, "name": "只改别名"}); ok(r.get("ok") and not r.get("deferred"), "API alias rename never needs to wait")
ok(post("/api/sessions/trash-purge", {"batch": "nope"}).get("ok") is False, "API purge of unknown batch refused")
q = [j for j in apps.pending() if j["state"] == "waiting"]; ok(len(q) == 2, "2 jobs waiting")
(T / "desktop-running").unlink()
apps.watch("claude")
q = apps.pending(); ok(all(j["state"] == "done" for j in q[-2:]) and not Path(next(x for x in info["sessions"] if x[0] == sid2) and sessions.claude_file(sid2) or "/nope").exists() if sessions.claude_file(sid2) else True, "queued jobs run after Desktop quits")
ok(sid2 not in {x["id"] for x in sessions.list_all()["items"]}, "queued delete happened")
srv.shutdown()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

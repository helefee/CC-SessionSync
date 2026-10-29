# -*- coding: utf-8 -*-
"""0.4.8 账号同步带上会话关系（桌面端登记里的 spawnedFrom：侧边会话 / 任务派生）：子会话的父会话一起补；
只挑子会话时自动带上父会话；父会话带不过来（不在 / 在当前号删过）照样补、预览里说明；任务记录（tasks.json）跟着合并；撤回全撤。
全在临时目录、假的登记文件夹。"""
import json, os, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-rel-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import claude_sync as CS

p0 = CS.plan()
ok(p0["ok"], "plan works on the demo folders")
tdir = Path(p0["target"]["path"])
src = next(Path(f["path"]) for f in p0["folders"] if f["key"] != p0["target"]["key"] and list(Path(f["path"]).glob("local_*.json")))
H = T / "home"
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)

def entry(folder, name, title, spawned=None, transcript=True, act=0):
    cli = str(uuid.uuid4())
    if transcript:
        demo_env.claude_transcript(H / ".claude/projects/-work-rel" / f"{cli}.jsonl", cli, "/work/rel", [(title + "？", "好的。")], NOW)
    d = {"sessionId": name, "cliSessionId": cli, "title": title, "cwd": "/work/rel", "createdAt": 1760000000000 + act, "lastActivityAt": 1760000600000 + act, "isArchived": False}
    if spawned:
        d["spawnedFrom"] = spawned
    (folder / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f"{name}.json", cli

P, _ = entry(src, "local_parent1", "主会话：重构订单")
C1, _ = entry(src, "local_side1", "侧边：查一下日志", {"name": "side", "sessionId": "local_parent1", "sharedFolder": "/work/rel", "sideSession": True, "taskId": None, "title": "主会话：重构订单"})
C2, _ = entry(src, "local_task1", "任务：补单元测试", {"sessionId": "local_parent1", "taskId": "task-1", "title": "主会话：重构订单"})
C3, _ = entry(src, "local_orphan", "孤儿：父会话不在", {"sessionId": "local_gone", "taskId": "task-9", "title": "早就删了的"})
TP, _ = entry(src, "local_deadparent", "在新号里删过的主会话")
(tdir / "deleted_deadparent").write_text("", encoding="utf-8")
C4, _ = entry(src, "local_child4", "子会话：父会话删过", {"sessionId": "local_deadparent", "taskId": None, "title": "在新号里删过的主会话"})
(src / "tasks.json").write_text(json.dumps({"tasks": [{"id": "task-1", "title": "补单元测试", "sessionId": "local_parent1"}, {"id": "task-2", "title": "别的任务"}]}, ensure_ascii=False), encoding="utf-8")
orig_tasks = json.dumps({"tasks": [{"id": "task-x", "title": "新号自己的任务"}]}, ensure_ascii=False)
(tdir / "tasks.json").write_text(orig_tasks, encoding="utf-8")

p = CS.plan()
by = {x["name"]: x for x in p["add"]}
ok(P in by and C1 in by and C2 in by and C3 in by and C4 in by and TP not in by, "plan: parent, children, orphan listed; the tombstoned parent is not revived")
ok(by[C1]["rel"]["state"] == "with" and by[C1]["rel"]["kind"] == "side" and by[C1]["rel"]["parent"] == P, "side session linked to its parent (synced together)")
ok(by[C2]["rel"]["kind"] == "task" and by[C2]["rel"]["task"] == "task-1" and by[C2]["rel"]["kind_label"] == "任务派生", "task child: kind + task id")
ok(by[C3]["rel"]["state"] == "missing" and "独立会话" in by[C3]["rel"]["warn"], "missing parent → kept, warned 「父会话不在，会显示为独立会话」")
ok(by[C4]["rel"]["state"] == "tomb" and "删过" in by[C4]["rel"]["warn"], "parent deleted in the target account → warned, not revived")
ok(p["children"] == 4 and p["rel_warns"] == 2 and "rel" not in by[P] and "_spawn" not in by[P], f"counts: {p['children']} children, {p['rel_warns']} warnings")
json.dumps(p)                                        # 能序列化（页面要用）

# 只挑一个子会话：父会话自动带上，任务记录合并
r = CS.run(only=[C2])
ok(r["done"] == 2 and r["auto_parents"] == [P], f"partial selection: parent auto-included ({r.get('auto_parents')})")
ok((tdir / P).exists() and (tdir / C2).exists() and not (tdir / C1).exists(), "only the child + its parent written")
ok(json.loads((tdir / C2).read_text(encoding="utf-8"))["spawnedFrom"]["sessionId"] == "local_parent1", "spawnedFrom kept as is")
tk = json.loads((tdir / "tasks.json").read_text(encoding="utf-8"))
ok([t["id"] for t in tk["tasks"]] == ["task-x", "task-1"] and r["tasks"]["merged"] == 1, "referenced task record merged into the target tasks.json (others untouched)")
led = json.loads((Path(r["backup"]) / "ledger.json").read_text(encoding="utf-8"))
ok(led["task_files_updated"] == ["tasks.json"] and led["tasks_merged"] == 1, "ledger records the task file change")
u = CS.restore(Path(r["backup"]).name)
ok(u["ok"] and not (tdir / P).exists() and not (tdir / C2).exists(), "undo removes the child and the auto-included parent")
ok((tdir / "tasks.json").read_text(encoding="utf-8") == orig_tasks, "undo restores tasks.json")

# 只挑孤儿：没有父会话可带
r = CS.run(only=[C3])
ok(r["done"] == 1 and r["auto_parents"] == [] and (tdir / C3).exists(), "orphan child synced alone (with the warning)")
CS.restore(Path(r["backup"]).name)

# 全部同步：没有 tasks.json 的目标会新建，撤回时挪走
(tdir / "tasks.json").unlink()
r = CS.run()
ok(all((tdir / n).exists() for n in (P, C1, C2, C3, C4)), "full sync writes parent + all children")
ok(json.loads((tdir / "tasks.json").read_text(encoding="utf-8")) == {"tasks": [{"id": "task-1", "title": "补单元测试", "sessionId": "local_parent1"}]}, "target without tasks.json: created with just the referenced task")
CS.restore(Path(r["backup"]).name)
ok(not (tdir / "tasks.json").exists() and not (tdir / C1).exists(), "undo moves the created tasks.json away too")

# 认不出的任务文件格式：不动，给提示
(src / "tasks.json").write_text('"weird"', encoding="utf-8")
r = CS.run(only=[C2])
ok(r["tasks"]["merged"] == 0 and r["tasks"]["warn"] and not (tdir / "tasks.json").exists(), "unknown tasks.json shape: left alone, warning returned")
CS.restore(Path(r["backup"]).name)

# 页面
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("子会话" in page and "kind_label" in page and "rel_warns" in page and "auto_parents" in page, "sync page shows children grouped under parents, warnings, auto-included parents")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

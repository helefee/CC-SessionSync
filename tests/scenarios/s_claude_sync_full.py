# -*- coding: utf-8 -*-
"""0.5.0 账号同步「完全同步 / 完全撤回」：
- 在别的号里删过（墓碑比最后一次使用晚）的会话不再被旧文件夹里的登记带回来；删了以后又用过的照样补
- 跟账号绑死的远程控制字段（bridgeSessionIds / remoteControlAutoEligible）不带过去；刷新时留新号自己的
- 账本逐个文件记同步前 / 同步后的指纹；同步完自动核对（verify）；再同步一次什么都不做（幂等）
- 撤回：新建的挪走、改过的按原样字节换回（同步后又被 Claude 改过也换回、结果里说明）；任务文件也换回；撤回后核对干净；
  再撤一次什么都不做；撤回前的备份不能当同步撤；老账本核对时说明核对不了
全在临时目录、假的登记文件夹。"""
import json, os, sys, tempfile, uuid, time
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-full-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import claude_sync as CS

p0 = CS.plan()
ok(p0["ok"], "plan works on the demo folders")
tdir = Path(p0["target"]["path"])
others = [Path(f["path"]) for f in p0["folders"] if f["key"] != p0["target"]["key"] and f.get("profile", "1p") == "1p"]
src = others[0]
third = CS.sessions_root() / "7e3c0a11-0000-4000-8000-000000000003" / "0e3c0a11-0000-4000-8000-000000000003"   # 第三个号（换过不止一次号）
third.mkdir(parents=True)
CS.forget()
H = T / "home"
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
ACT = 1790000000000                                     # 毫秒

def entry(folder, name, title, act=ACT, **extra):
    cli = str(uuid.uuid4())
    demo_env.claude_transcript(H / ".claude/projects/-work-full" / f"{cli}.jsonl", cli, "/work/full", [(title + "？", "好的。")], NOW)
    d = dict({"sessionId": name, "cliSessionId": cli, "title": title, "cwd": "/work/full", "createdAt": act - 600000, "lastActivityAt": act, "isArchived": False}, **extra)
    (folder / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f"{name}.json"

# 1) 在别的号里删过的
GONE = entry(src, "local_gone1", "在第三个号里删过的会话", act=ACT)
(third / "deleted_gone1").write_text(str(ACT + 5000), encoding="utf-8")            # 删除时间比最后使用晚
BACK = entry(src, "local_back1", "删了又接着用过的会话", act=ACT + 90000)
(third / "deleted_back1").write_text(str(ACT + 5000), encoding="utf-8")            # 删除之后又用过
RC = entry(src, "local_rc1", "开过远程控制的会话", bridgeSessionIds=["cse_x1", "cse_x2"], remoteControlAutoEligible=True, steeredByRemoteClient=True)
# 2) 刷新：新号里已有旧版本，带着新号自己的远程控制
UPD = entry(src, "local_upd1", "两边都有的会话（来源更新）", act=ACT + 100000, bridgeSessionIds=["cse_old_account"], isStarred=False)
mine = {"sessionId": "local_upd1", "cliSessionId": json.loads((src / UPD).read_text(encoding="utf-8"))["cliSessionId"], "title": "两边都有的会话（旧）",
        "cwd": "/work/full", "lastActivityAt": ACT, "isStarred": True, "bridgeSessionIds": ["cse_mine"], "remoteControlAutoEligible": False}
orig_upd = json.dumps(mine, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"     # 故意带缩进和换行：撤回要一个字节不差
(tdir / UPD).write_bytes(orig_upd)
orig_sched = b'{"scheduledTasks": [], "recordedSkips": {}}'
(tdir / "scheduled-tasks.json").write_bytes(orig_sched)

p = CS.plan()
names = {x["name"] for x in p["add"]}
ok(GONE not in names and p["skip_gone"] >= 1, f"deleted in another account after last use → not brought back (skip_gone={p['skip_gone']})")
ok(BACK in names, "deleted elsewhere but used again afterwards → still synced")
ok(UPD in {x["name"] for x in p["update"]}, "existing older entry → refresh")

r = CS.run()
bak = Path(r["backup"]).name
ok(r["ok"] and (tdir / RC).exists() and not (tdir / GONE).exists(), "sync wrote the entries, skipped the deleted one")
rc = json.loads((tdir / RC).read_text(encoding="utf-8"))
ok("bridgeSessionIds" not in rc and "remoteControlAutoEligible" not in rc and rc.get("steeredByRemoteClient") is True,
   "account-bound Remote Control fields dropped on add (other fields kept)")
up = json.loads((tdir / UPD).read_text(encoding="utf-8"))
ok(up["bridgeSessionIds"] == ["cse_mine"] and up["remoteControlAutoEligible"] is False and up["isStarred"] is True and up["title"] == "两边都有的会话（来源更新）",
   "refresh keeps the target account's own Remote Control + star, takes the newer content")
led = json.loads((CS.BAK_ROOT / bak / "ledger.json").read_text(encoding="utf-8"))
ok(led.get("v") == 2 and led["files"][RC]["before"] is None and led["files"][UPD]["before"] == CS._sha(CS.BAK_ROOT / bak / tdir.parent.name / tdir.name / UPD),
   "ledger v2 records per-file before/after fingerprints")
v = r["verify"]
ok(v["ok"] and v["state"] == "synced" and v["total"] == len(led["files"]) and not v["diffs"], "verify right after sync: " + v["summary"])
json.dumps(r)

# 幂等：再同步一次什么都不做、不多出备份
n_bak = len(list(CS.BAK_ROOT.iterdir()))
r2 = CS.run()
ok(r2["done"] == 0 and r2["updated"] == 0 and len(list(CS.BAK_ROOT.iterdir())) == n_bak, "second sync is a no-op (idempotent, no extra backup)")

# Claude 之后改了一个（点开过）、删了一个（墓碑）→ 核对分得清
d = json.loads((tdir / BACK).read_text(encoding="utf-8")); d["lastFocusedAt"] = ACT + 999999
(tdir / BACK).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
victim = next(n for n in led["added"] if n not in (BACK, RC))
(tdir / victim).unlink(); (tdir / ("deleted_" + victim[len("local_"):-len(".json")])).write_text(str(int(time.time() * 1000)), encoding="utf-8")
v = CS.verify(bak)
ok(not v["ok"] and [x["why"] for x in v["diffs"]] == ["同步后在 Claude 里删了"] and len(v["touched"]) == 1, "verify: deleted-in-Claude reported, opened-in-Claude counted as normal: " + v["summary"])
(tdir / ("deleted_" + victim[len("local_"):-len(".json")])).unlink()

# 撤回：被 Claude 改过的刷新文件也要换回原样字节
up["lastFocusedAt"] = ACT + 123; (tdir / UPD).write_text(json.dumps(up, ensure_ascii=False), encoding="utf-8")
u = CS.restore(bak)
ok(u["ok"] and (tdir / UPD).read_bytes() == orig_upd, "undo restores the refreshed entry byte-for-byte (even after Claude touched it)")
ok(UPD in u["changed_after"] and BACK in u["changed_after"], "undo reports entries changed after the sync")
ok(all(not (tdir / n).exists() for n in led["added"]), "undo removes every added entry")
ok((tdir / "scheduled-tasks.json").read_bytes() == orig_sched, "untouched task file stays byte-identical")
ok(u["verify"]["ok"] and u["verify"]["state"] == "undone", "verify after undo: " + u["verify"]["summary"])
lb = {b["id"]: b for b in CS.list_backups()}
ok(lb[bak]["restored"] and lb[Path(u["backup"]).name]["what"] == "restore", "backup list: sync marked undone, pre-undo backup labelled")

# 再撤一次：什么都不做
n_bak = len(list(CS.BAK_ROOT.iterdir()))
u2 = CS.restore(bak)
ok(u2["ok"] and u2.get("already") and u2["removed"] == 0 and u2["restored"] == 0 and len(list(CS.BAK_ROOT.iterdir())) == n_bak, "second undo is a no-op")
e = CS.restore(Path(u["backup"]).name)
ok(not e["ok"] and "撤回之前" in e["error"], "pre-undo backup can't be 'undone' as a sync")

# 撤回之后：自动同步不再补回来；手动再同步可以
CS.forget()
skip = set(CS._skip_load().get(led["key"], []))
ok(set(led["added"]) <= skip, "undone entries go on the auto-sync skip list")

# 任务文件：同步时新建 / 合并的，撤回换回
(src / "local_parent9.json").exists() or entry(src, "local_parent9", "父会话")
entry(src, "local_child9", "任务子会话", spawnedFrom={"sessionId": "local_parent9", "taskId": "task-9"})
(src / "tasks.json").write_text(json.dumps({"tasks": [{"id": "task-9", "title": "t"}]}), encoding="utf-8")
orig_tasks = b'{"tasks": [{"id": "mine"}]}'
(tdir / "tasks.json").write_bytes(orig_tasks)
r3 = CS.run(only=["local_child9.json"])
l3 = json.loads((Path(r3["backup"]) / "ledger.json").read_text(encoding="utf-8"))
ok("tasks.json" in l3["files"] and l3["files"]["tasks.json"]["before"] == CS._sha(Path(r3["backup"]) / tdir.parent.name / tdir.name / "tasks.json") and r3["verify"]["ok"],
   "merged task file is in the per-file journal; verify ok")
u3 = CS.restore(Path(r3["backup"]).name)
ok((tdir / "tasks.json").read_bytes() == orig_tasks and u3["verify"]["ok"], "undo restores tasks.json byte-for-byte")

# 老账本（没有逐个文件记录）：核对说明做不了，撤回照旧
old = CS.BAK_ROOT / "20200101-000000"; (old / tdir.parent.name / tdir.name).mkdir(parents=True)
(old / "ledger.json").write_text(json.dumps({"what": "sync", "key": led["key"], "added": [], "updated": []}), encoding="utf-8")
vo = CS.verify("20200101-000000")
ok(vo["ok"] and vo["state"] == "unknown" and "核对不了" in vo["summary"], "old ledger: verify says it can't check")
ok(not CS.verify("../x")["ok"], "bad backup id rejected")

# 页面 / 接口
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("/api/sync/verify" in page and "data-bkv" in page and "syVerifyDlg" in page and "skip_gone" in page and "撤回前的备份" in page, "UI: verify button + dialog, skipped-deleted note, pre-undo label")
ui = (ROOT / "cc_sessionsync" / "ui.py").read_text(encoding="utf-8")
ok('"/api/sync/verify"' in ui, "GET /api/sync/verify wired")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

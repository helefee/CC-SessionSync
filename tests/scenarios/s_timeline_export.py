# -*- coding: utf-8 -*-
"""0.2.1：备份库增量存（base + 追加段）/ 消息数 / 定时备份（间隔、最少 5 分钟、正在跑就跳过、失败才通知）/ 保留规则 /
时间线（版本、某一版的对话、和现在比、时间点）/ 恢复到 Claude（新副本 + 桌面端登记，开着排队）/ 替换当前（先备份、能撤回）/
Codex 版本 → Claude（转换器）/ 恢复到 Codex / 导出 md、html（带原始记录）→ 导回来原样 / 别处来的 md 尽量拆 /
账号分组（含未知账号、Codex 按供应商）/ 显示完整账号（只有 reveal 接口给，要口令，诊断里还是打码）。全在临时目录。"""
import hashlib, json, os, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-tl-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
info = demo_env.build(T)
H = T / "home"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import sessions as S, timeline as TL, exports as EX, settings, ui, apps, events
CL = H / ".claude"

# ---------- 增量存 ----------
f = sorted((CL / "projects").glob("*/*.jsonl"))[0]
sid = f.stem
r1 = S.backup(why="t1")
ok(r1["snapshots"] >= 6, f"first backup stores every session ({r1['snapshots']})")
v1 = TL.versions("claude", sid)["items"]
ok(len(v1) == 1 and v1[0]["msgs"] and v1[0]["current"], "one version, has message count, = current")
orig = f.read_bytes()
add = json.dumps({"type": "user", "uuid": "u-x", "parentUuid": None, "sessionId": sid, "cwd": "/w", "timestamp": "2026-10-17T10:00:00.000Z",
                  "message": {"role": "user", "content": "再加一个重试上限"}}, ensure_ascii=False) + "\n"
add += json.dumps({"type": "assistant", "uuid": "a-x", "parentUuid": "u-x", "sessionId": sid, "cwd": "/w", "timestamp": "2026-10-17T10:00:30.000Z",
                   "message": {"id": "m-x", "role": "assistant", "type": "message", "content": [{"type": "text", "text": "好，上限 5 次。"}]}},
                  ensure_ascii=False) + "\n"
with open(f, "ab") as fh:
    fh.write(add.encode("utf-8"))
os.utime(f, (time.time() + 5, time.time() + 5))
time.sleep(0.05)
t_mid = time.time()          # 第一次备份之后、第二次之前（Windows 的时钟粒度 ~15 ms，不能拿某一条的 at + 1 ms 当时间点）
time.sleep(0.05)
r2 = S.backup(why="t2")
ok(r2["snapshots"] == 1 and r2["skipped"] >= 5, "second backup: only the changed session")
import sqlite3
c = sqlite3.connect(S.DB_PATH)
v2 = TL.versions("claude", sid)["items"]
b = c.execute("select base, depth, length(data), size from blobs where sha=?", (v2[-1]["sha"],)).fetchone()
ok(b[0] == v1[0]["sha"] and b[1] == 1 and b[2] < 400 < b[3], f"appended version stored as base + delta ({b[2]} B compressed of {b[3]})")
ok(S._blob(v2[-1]["sha"]) == f.read_bytes() and S._blob(v1[0]["sha"]) == orig, "both versions reconstruct byte-identical")
ok(v2[-1]["msgs"] == v1[0]["msgs"] + 2 and "+2 条消息" in v2[-1]["change"], f"message count + change summary ({v2[-1]['change']})")
ok(v2[-1]["current"] and not v2[0]["current"], "current flag on the newest")
# 改写（不是追加）→ 存完整一份
f.write_bytes(f.read_bytes().replace("上限 5 次".encode(), "上限 3 次".encode()))
os.utime(f, (time.time() + 10, time.time() + 10))
S.backup(why="t3")
v3 = TL.versions("claude", sid)["items"]
ok(c.execute("select base from blobs where sha=?", (v3[-1]["sha"],)).fetchone()[0] is None, "rewritten file → full copy (no bogus delta)")

# ---------- 看一版 / 和现在比 ----------
vw = TL.view(v1[0]["id"])
ok(vw["ok"] and vw["diff"]["only_now"] == 2 and vw["diff"]["only_then"] == 0 and not vw["diff"]["identical"], "view old version: now has 2 more")
ok(any("重试上限" in e["text"] for e in vw["diff"]["now_samples"]), "diff samples show what was added since")
ok(TL.view(v3[-1]["id"])["diff"]["identical"], "current version → identical")
pt = TL.point(t_mid)
st = {x["id"]: x["state"] for x in pt["items"]}
ok(st.get(sid) == "changed" and list(st.values()).count("same") >= 4, "time point: this one changed later, others same")

# ---------- 恢复到 Claude（新副本） ----------
before = set((CL / "projects").rglob("*.jsonl"))
(T / "desktop-running").unlink(missing_ok=True)
res = TL.restore([v1[0]["id"]])
ok(res["ok"] and len(res["made"]) == 1, "restore as new Claude session")
m = res["made"][0]
nf = Path(m["file"])
ok(nf.exists() and nf not in before and nf.parent == f.parent, "new transcript in the same project dir")
lines = [json.loads(x) for x in nf.read_text(encoding="utf-8").splitlines()]
ok(all(x.get("sessionId") in (None, m["id"]) for x in lines) and lines[-1]["type"] == "custom-title" and "（恢复 @" in lines[-1]["customTitle"],
   "new session id everywhere + title '（恢复 @时间）'")
ok(f.read_bytes() != orig, "original untouched")
led = json.loads((S.OPS / res["op"] / "ledger.json").read_text(encoding="utf-8"))
regs = [s for s in led["steps"] if s["t"] == "created" and s["path"].endswith(".json")]
ok(res["desktop"] and not res["desktop"].get("deferred") and len(regs) == 1, "Desktop index entry written (Desktop not running)")
ent = json.loads(Path(regs[0]["path"]).read_text(encoding="utf-8"))
ok(ent["cliSessionId"] == m["id"] and "恢复 @" in ent["title"], "Desktop entry points at the restored session")
ok("claude --resume " + m["id"] in m["resume"], "resume command returned")
ok(any(x["id"] == m["id"] for x in S.list_all("claude")["items"]), "restored session shows in the list")
u = S.undo(res["op"])
ok(u["ok"] and not nf.exists() and not Path(regs[0]["path"]).exists(), "undo restore → copy + Desktop entry moved to bin")
# 桌面端开着：登记排队
(T / "desktop-running").write_text("1")
res2 = TL.restore([v1[0]["id"]])
ok(res2["ok"] and res2["desktop"]["deferred"] and Path(res2["made"][0]["file"]).exists(), "Desktop running → transcript now, index entry queued")
ok(any(j["op"] == "desktop-register" for j in apps.pending()), "queued job = desktop-register")
(T / "desktop-running").unlink()
apps.watch("claude", max_wait=5)
led2 = json.loads((S.OPS / res2["op"] / "ledger.json").read_text(encoding="utf-8"))
ok(led2.get("desktop", {}).get("entries"), "after Desktop quit → entry registered, ledger updated")

# ---------- 替换当前 ----------
cur_before = f.read_bytes()
rr = TL.restore([v1[0]["id"]], mode="replace")
ok(rr["ok"] and f.read_bytes() == orig, "replace current with the old version")
ok(TL.versions("claude", sid)["items"][-1]["why"] in ("before-restore", "t3") and S._blob(TL.versions("claude", sid)["items"][-1]["sha"]) in (cur_before, orig),
   "current was backed up first")
op = rr["results"][0]["op"]
S.undo(op)
ok(f.read_bytes() == cur_before, "undo replace → byte-identical to before")
(T / "desktop-running").write_text("1")
rq = TL.restore([v1[0]["id"]], mode="replace")
ok(rq["results"][0].get("deferred") and f.read_bytes() == cur_before, "replace while Desktop runs → queued, nothing written")
for j in apps.pending():
    apps.cancel(j["id"])
(T / "desktop-running").unlink()

# ---------- Codex 版本 ----------
cx = next(x for x in S.list_all("codex")["items"] if x.get("file"))
cv = TL.versions("codex", cx["id"])["items"]
rc = TL.restore([cv[-1]["id"]], desktop=False)
ok(rc["ok"] and rc["made"][0]["from_agent"] == "codex", "Codex version → Claude session via converter")
cl = Path(rc["made"][0]["file"]).read_text(encoding="utf-8")
ok("codex-import" in cl and "恢复 @" in cl, "converted records + restore title")
ok(TL.restore([v1[0]["id"]], target="codex")["ok"] is False, "Claude version can't go to Codex")
cxf = Path(cx["file"]); cxb = cxf.read_bytes()
cxf.write_bytes(cxb + b'{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"later"}]}}\n')
rt = TL.restore([cv[-1]["id"]], target="codex")
ok(rt["ok"] and cxf.read_bytes() == cxb, "restore to Codex = replace current (backed up)")
S.undo(rt["results"][0]["op"])
ok(cxf.read_bytes().endswith(b'"later"}]}}\n'), "undo Codex restore")

# ---------- 定时备份 ----------
s = dict(settings.load())
ok(s["session_backup_auto"] is True and s["session_backup_every"] == 10 and s["session_backup_unit"] == "min", "default ON every 10 min")
ok(S.interval_s({"session_backup_every": 1, "session_backup_unit": "min"}) == 300, "interval clamped to ≥ 5 min")
ok(S.interval_s({"session_backup_every": 2, "session_backup_unit": "hour"}) == 7200 and S.interval_s({"_legacy_hours": 6}) == 21600, "hours / legacy 0.2.0 hours")
for bad in ({"session_backup_every": 2, "session_backup_unit": "min"}, {"session_backup_unit": "week"}, {"session_backup_every": "x"}):
    try:
        ui._check_backup_settings(dict(bad)); ok(False, f"reject {bad}")
    except ValueError:
        ok(True, f"reject {bad}")
S.SCHED.update(last_run=time.time())
ok(S.auto_tick(s) is None and S.SCHED["next"] > time.time(), "not due → nothing; next run known")
S.SCHED.update(last_run=time.time() - 700)
r = S.auto_tick(s)
ok(r and r["ok"] and S.SCHED["last_result"] is not None, "overdue (startup / after sleep) → runs once")
ok(S.auto_tick(s) is None, "just ran → not again")
S._run_lock.acquire(); S.SCHED.update(last_run=0)
ok(S.auto_tick(s) is None, "a run is going → skip"); S._run_lock.release()
ok(S.auto_tick(dict(s, session_backup_auto=False)) is None and S.SCHED["next"] is None, "off → never")
n0 = len(events.recent(100))
real = S.backup
S.backup = lambda **k: (_ for _ in ()).throw(OSError("disk full"))
S.SCHED.update(last_run=0)
S.auto_tick(s)
S.backup = real
ev = events.recent(100)
ok(len(ev) == n0 + 1 and "失败" in ev[-1]["title"], "failure → one notification (success never notifies)")
legacy = T / "legacy.json"
settings.FILE.write_text(json.dumps({"session_backup_hours": 6}), encoding="utf-8")
ok(ui.backup_settings()["_legacy_hours"] == 6 and S.interval_s(ui.backup_settings()) == 21600, "0.2.0 explicit hours still honoured")
settings.FILE.write_text("{}", encoding="utf-8")
ok(ui.backup_settings()["session_backup_auto"] is True, "0.2.0 install without explicit setting → new default ON")

# ---------- 保留规则 ----------
D = (int(time.time() // 86400) - 40) * 86400
c.execute("update snaps set at = ? where agent='claude' and sid=? and why='t1'", (D + 1000, sid))
c.execute("update snaps set at = ? where agent='claude' and sid=? and why='t2'", (D + 2000, sid)); c.commit()
nb = c.execute("select count(*) from snaps where sid=?", (sid,)).fetchone()[0]
pr = S.prune({"retention_all_days": 7, "retention_daily_days": 90})
na = c.execute("select count(*) from snaps where sid=?", (sid,)).fetchone()[0]
ok(pr["snapshots_dropped"] >= 1 and na == nb - 1, f"retention: two versions on the same old day → keep the last ({nb}→{na})")
left = [x["sha"] for x in TL.versions("claude", sid)["items"]]
ok(all(S._blob(h) for h in left), "kept versions still reconstruct (bases kept)")
ok(S.prune({}, vacuum=True)["ok"], "cleanup + vacuum")

# ---------- 导出 / 导入 ----------
e = EX.export_many([{"agent": "claude", "id": sid}], "md")
md = Path(e["files"][0]).read_text(encoding="utf-8")
ok(md.startswith("---\ntitle:") and "### 你" in md and "ccss-raw:v1" in md, "md: front-matter, roles, embedded raw")
e2 = EX.export_many([{"agent": "claude", "id": sid}], "html")
ht = Path(e2["files"][0]).read_text(encoding="utf-8")
ok("<style>" in ht and "prefers-color-scheme:dark" in ht and 'id="ccss-raw"' in ht and "<details>" in ht or "<details>" not in md,
   "html: inline css light/dark, embedded raw")
cur = f.read_bytes()
for ff in (e["files"][0], e2["files"][0]):
    p = EX.parse_file(ff)
    ok(p["how"] == "lossless" and p["raw"] == cur, f"{Path(ff).suffix}: lossless round-trip")
dry = EX.import_files([e["files"][0]], dry=True)
ok(dry["dry"] and dry["rows"][0]["how"] == "lossless" and not dry.get("made"), "import dry-run writes nothing")
im = EX.import_files([e2["files"][0]], desktop=True, dry=False)
ok(im["ok"] and Path(im["made"][0]["file"]).exists() and im["desktop"], "import html → new Claude session + Desktop entry")
plain = T / "chat.md"
plain.write_text("# 别处的聊天\n\n## User\n怎么读 CSV？\n\n## Assistant\n用 `csv` 模块：\n\n```python\nimport csv\n```\n\n## User\n谢谢\n", encoding="utf-8")
pp = EX.parse_file(str(plain))
ok(pp["how"] == "plain" and pp["messages"] == 4 and "import csv" in pp["preview"][1]["text"], "plain md → 4 text turns (code kept)")
ip = EX.import_files([str(plain)], cwd=str(T / "proj"), desktop=False, dry=False)
ok(ip["ok"] and str(T / "proj") in ip["made"][0]["cwd"], "plain import into chosen project")
S.undo(ip["op"])
ok(not Path(ip["made"][0]["file"]).exists(), "undo import")
bx = EX.export_many([{"agent": "claude", "id": sid}, {"agent": "codex", "id": cx["id"]}, {"snap": TL.versions("claude", sid)["items"][0]["id"]}], "html", "我的项目")
ok(bx["ok"] and len(bx["files"]) == 3 and "我的项目" in bx["dir"], "batch / group / version export into one folder")

# ---------- 分组 + 显示完整账号 ----------
L = S.list_all()
g = {x["key"]: x for x in L["groups"]}
ok(sum(1 for x in L["groups"] if x["kind"] == "claude") >= 2 and any(x["current"] for x in L["groups"]), "all Claude accounts listed, current marked")
ok(any(k.startswith("codex:") for k in g) and all("gacct" in x for x in L["items"]), "Codex grouped by provider; every session grouped")
ok(all("@" not in (x.get("masked") or "") or "***" in x["masked"] for x in L["groups"]), "group labels masked")
import threading
srv = ui.start(8799)
assert srv, "port 8799 busy"
def get(path, tok=True):
    rq = urllib.request.Request("http://127.0.0.1:8799" + path, headers={"x-token": ui.TOKEN} if tok else {})
    try:
        return json.loads(urllib.request.urlopen(rq, timeout=10).read())
    except urllib.error.HTTPError as e:
        return {"_code": e.code}
stt = get("/api/status")
raw = json.dumps(stt, ensure_ascii=False)
ok("zhang.wei@example.com" not in raw and "z***@example.com" in raw and stt["accounts"]["desktop"].get("rid"), "status: masked only, rid given")
ok("zhang.wei@example.com" not in json.dumps(get("/api/diag"), ensure_ascii=False), "diag (copy report) stays masked")
ok("li.na@example.com" not in json.dumps(get("/api/sessions"), ensure_ascii=False), "sessions groups masked")
ok(get("/api/accounts/reveal", tok=False).get("_code") == 403, "reveal needs the token")
rv = get("/api/accounts/reveal")["accounts"]
ok(any(v.get("email") == "zhang.wei@example.com" for v in rv.values()), "reveal → full Claude email")
ok(any("li.na@example.com" in (v.get("email") or "") for k, v in rv.items() if k.startswith("codex:")), "reveal → full Codex email")
ok(not any("FAKE-NOT-A-TOKEN" in json.dumps(v) for v in rv.values()), "reveal never returns tokens")
tl = get(f"/api/timeline/versions?agent=claude&id={sid}")
ok(tl.get("items") and get(f"/api/timeline/view?snap={tl['items'][0]['id']}")["ok"], "timeline API")
ok(get("/api/sessions/backup-status")["sched"]["auto"] is True, "backup status API has schedule")
page = (Path(ui.__file__).parent / "index.html").read_text(encoding="utf-8")
ok(all(k in page for k in ('id="side"', 'id="tree"', 'id="trMode"', 'id="pg-timeline"', 'id="sideGrip"', "/api/accounts/reveal",
                           "/api/timeline/restore", "/api/import", "/api/export", "/api/backup/now", "恢复到 Claude", "按账号", "按项目")),
   "UI: left panel, tree, reveal eye, timeline, import/export wired")
ok('class="tabs"' not in page, "UI: old top tabs gone")
# 0.2.2：左侧只放会话列表；其余页面在顶部按钮栏；用量 / 供应商是页面不是弹窗；设置在右上角；顶栏不再有眼睛
side = page[page.index('<aside class="side"'):page.index('</aside>')]
top = page[page.index('<header class="top">'):page.index('</header>')]
ok('data-p=' not in side and 'id="gQ"' in side and 'id="tree"' in side and 'id="sdAg"' in side, "0.2.2: left panel = session list + search/filters + tree only")
ok(all(f'data-p="{p}"' in top for p in ("timeline", "sync", "convert", "usage", "models", "diag", "settings")) and 'data-p="backups"' not in top and 'id="tbUpd"' in top and 'id="tbTheme"' in top,
   "0.2.2: all other pages in the top button bar")
ok(top.rstrip().endswith("</div>") and top.rindex('id="tbSet"') > top.rindex('id="tbTheme"'), "0.2.2: settings gear is the last (top-right) button")
ok('id="tbEye"' not in page and 'id="trEye"' in side, "0.2.2: no duplicate top-bar eye; the list keeps its own")
ok('id="pg-usage"' in page and 'id="pg-models"' in page and 'openDlg("用量统计"' not in page and 'openDlg("模型与 API"' not in page
   and 'openDlg((isNew' not in page and 'openDlg("数据来源"' not in page and 'openDlg("从 CC Switch 导入供应商"' not in page, "0.2.2: usage / providers are in-page views, not dialogs")
srv.shutdown()
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

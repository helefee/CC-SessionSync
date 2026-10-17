# -*- coding: utf-8 -*-
"""「关闭并同步（完成后重启）」：桌面端开着时先正常关掉 → 做排队的改动 → 按原来的方式重新打开。
进程层全用假的（CC_SESSIONSYNC_FAKE_PROCTABLE 指向一个 JSON 进程表，关 / 杀 / 启动都只改这个文件），不碰任何真进程。"""
import json, os, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-close-"))
FP = T / "procs.json"
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_NO_WATCHER="1", CC_SESSIONSYNC_FAKE_PROCTABLE=str(FP), CC_SESSIONSYNC_CLOSE_WAIT="1.5",
                  CC_SESSIONSYNC_CLOSE_TAIL="0.1", CC_SESSIONSYNC_LAUNCH_WAIT="1")
demo_env.build(T)
os.environ.pop("CC_SESSIONSYNC_FAKE_APP", None)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import apps, appclose, events, ui

MSIX = r"C:\Program Files\WindowsApps\Claude_1.0.3218.0_x64__pzs8sxrjxfjjc\app\claude.exe"
SQ_DIR = r"C:\Users\me\AppData\Local\AnthropicClaude"
SQ = SQ_DIR + r"\app-0.14.10\claude.exe"
def procs(exe=MSIX, **kw):
    d = {"procs": [
        {"pid": 100, "ppid": 4, "name": "claude.exe", "exe": exe, "cmd": ""},
        {"pid": 101, "ppid": 100, "name": "claude.exe", "exe": exe, "cmd": "--type=renderer"},
        {"pid": 102, "ppid": 100, "name": "claude.exe", "exe": exe, "cmd": "--type=gpu-process"},
        {"pid": 200, "ppid": 100, "name": "claude.exe", "exe": r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.0.14\claude.exe", "cmd": "claude"},
        {"pid": 300, "ppid": 50, "name": "claude.exe", "exe": r"C:\Users\me\.local\bin\claude.exe", "cmd": "claude --resume"},
        {"pid": 301, "ppid": 50, "name": "node.exe", "exe": r"C:\nodejs\node.exe", "cmd": r"node C:\npm\node_modules\@anthropic-ai\claude-code\cli.js"},
        {"pid": 400, "ppid": 4, "name": "ChatGPT.exe", "exe": r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0_x64__abc\app\ChatGPT.exe", "cmd": ""},
    ], "aumid": "Claude_pzs8sxrjxfjjc!Claude", "exit_after": 0.4, "log": []}
    d.update(kw); FP.write_text(json.dumps(d)); return d
def fp(): return json.loads(FP.read_text())
def pids(): return sorted(p["pid"] for p in fp()["procs"])
def wait_run(rid, until=("done", "failed", "stopped"), t=20):
    t0 = time.time()
    while time.time() - t0 < t:
        v = appclose.view(rid)
        if v["state"] in until:
            return v
        time.sleep(0.1)
    return appclose.view(rid)

# ---------- 认进程 / 打开方式 ----------
procs()
pl = appclose.plan("claude")
ok(pl["running"] and pl["pids"] == [100, 101, 102] and pl["mains"] == [100], "desktop processes found (main + helpers), CLI not counted as desktop")
ok(pl["launch"]["how"] == "msix" and pl["launch"]["aumid"] == "Claude_pzs8sxrjxfjjc!Claude" and "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude" in pl["launch_text"],
   "MSIX: AUMID recorded for shell:AppsFolder relaunch")
ok([p["pid"] for p in pl["cli_children"]] == [200], "Claude Code session started by Desktop → listed as will be interrupted")
ok(sorted(p["pid"] for p in pl["cli_other"]) == [300, 301], "CLI-only claude / node claude-code processes → listed as not touched")
ok(all("cmd" not in p for p in pl["cli_children"] + pl["cli_other"]), "command lines are never shown")
procs(aumid=None)
ok(appclose.plan("claude")["launch"] == dict(appclose.plan("claude")["launch"], aumid="Claude_pzs8sxrjxfjjc!Claude", via="guess"), "AUMID lookup failed → default Claude_pzs8sxrjxfjjc!Claude")
procs(SQ)
li = appclose.plan("claude")["launch"]
ok(li["how"] == "squirrel" and li["exe"] == SQ_DIR + r"\claude.exe", "Squirrel install: relaunch via the outer AnthropicClaude\\claude.exe")
procs(SQ, missing=[SQ_DIR + r"\claude.exe"])
ok(appclose.plan("claude")["launch"] == {"how": "exe", "exe": SQ, "source": SQ}, "no Squirrel stub → the exe itself")
# 2026-10-18 那台电脑上的真实布局（只读看到的）：商店版主进程叫 Claude.exe（大写），父进程 explorer.exe；它开的 Claude Code 在 claude-code\<版本>\<hash>\claude.exe
d = procs(); d["procs"] = [dict(p, name="Claude.exe", exe=p["exe"].replace("Claude_1.0.3218.0_x64__pzs8sxrjxfjjc\\app\\claude.exe", "Claude_2.42304.0.0_x64__pzs8sxrjxfjjc\\app\\Claude.exe")) if p["pid"] in (100, 101, 102) else p for p in d["procs"]]
d["procs"][3]["exe"] = r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.299\f0584be0fbaf\claude.exe"; FP.write_text(json.dumps(d))
pl = appclose.plan("claude")
ok(pl["pids"] == [100, 101, 102] and pl["launch"]["package"] == "Claude_2.42304.0.0_x64__pzs8sxrjxfjjc" and [p["pid"] for p in pl["cli_children"]] == [200],
   "real-world layout (Claude.exe, claude-code\\<ver>\\<hash>) recognised")
ok(appclose.plan("codex")["running"] and appclose.plan("codex")["pids"] == [400], "Codex desktop recognised separately")
ok(appclose._pkg_family("Claude_1.0_x64__pzs8sxrjxfjjc") == ("Claude", "Claude_pzs8sxrjxfjjc") and appclose._pkg_family("x'; rm") == (None, None),
   "package family parsed; odd names rejected (no PowerShell injection)")

# ---------- 关闭并同步：正常退出 ----------
procs()
r = ui.sync_now()
ok(r["deferred"] and r["kind"] == "claude" and r["verb"] == "同步" and r["job"], "sync while Desktop runs → queued, response says kind + verb")
q = apps._load_q()[-1]
ok(q["relaunch"]["aumid"] == "Claude_pzs8sxrjxfjjc!Claude", "queued job records how Desktop was launched")
events.poll_jobs()
seq0 = events.since(-1)[0]
v = appclose.start("claude", [r["job"]])
ok(v["state"] == "closing" and apps._load_q()[-1]["state"] == "closing", "start → jobs claimed (the after-quit watcher won't run them)")
try:
    appclose.start("claude", [r["job"]]); ok(False, "second start refused")
except ValueError:
    ok(True, "second start while closing → refused")
v = wait_run(v["id"])
lg = fp()["log"]
ok(v["state"] == "done" and v["relaunched"] is True and v["relaunch_error"] is None, "done; Desktop relaunched and verified running")
ok([x[0] for x in lg] == ["close", "launch"] and lg[0][1] == [100, 101, 102] and lg[0][2] == [100], "graceful close only (no kill), then launch")
ok(lg[1][1]["aumid"] == "Claude_pzs8sxrjxfjjc!Claude", "relaunched the same way (MSIX AUMID)")
ok(300 in pids() and 301 in pids() and 400 in pids(), "CLI-only claude processes and Codex untouched")
j = next(x for x in apps._load_q() if x["id"] == r["job"])
ok(j["state"] == "done" and "done=4" in j["result"], "the sync ran after Desktop exited (4 sessions added)")
ev = [e for e in events.since(seq0)[1] if e["kind"] == "job"]
ok(len(ev) == 1 and "已重新打开" in ev[0]["text"] and ev[0]["ok"], "one notification with the relaunch result")
ok(not [e for e in events.poll_jobs() if "同步" in e["title"]], "no duplicate notification from the queue watcher")

# ---------- 不肯退：问要不要强制结束 ----------
def queue_sync():
    return apps.run_or_defer("claude", "claude-sync", {"target": None, "source": "desktop"}, "同步 Claude 会话到当前账号")
procs(stubborn=True)
r = queue_sync()
v = wait_run(appclose.start("claude", [r["job"]])["id"], until=("ask_force",), t=6)
ok(v["state"] == "ask_force" and v["remaining"] == 3 and v["elapsed"] >= 1.5, "not gone after the wait → asks before force-killing (shows what's left)")
ok([x[0] for x in fp()["log"]] == ["close"], "nothing force-killed without the second confirmation")
time.sleep(0.5); ok(appclose.view(v["id"])["state"] == "ask_force", "keeps waiting while the user decides")
appclose.force(v["id"])
v = wait_run(v["id"])
lg = fp()["log"]
ok([x[0] for x in lg] == ["close", "kill", "launch"] and lg[1][1] == [100, 101, 102] and v["forced"], "force only after confirm; only Desktop's processes")
ok(not {100, 101, 102, 200} & set(pids()) and {300, 301, 400} <= set(pids()) and len(pids()) == 4, "Desktop + its own Claude Code child gone; CLI-only processes kept; relaunched (401)")
ok(v["state"] == "done" and v["relaunched"], "forced path still syncs and relaunches")

# 用户自己从托盘退出了：继续等的过程中退干净 → 照常做
procs(stubborn=True)
r = queue_sync()
rid = wait_run(appclose.start("claude", [r["job"]])["id"], until=("ask_force",), t=6)["id"]
d = fp(); d["procs"] = [p for p in d["procs"] if p["pid"] not in (100, 101, 102, 200)]; FP.write_text(json.dumps(d))
v = wait_run(rid)
ok(v["state"] == "done" and "kill" not in [x[0] for x in fp()["log"]], "quit from the tray while asked → proceeds without killing")

# 改成等它退出
procs(stubborn=True)
r = queue_sync()
rid = wait_run(appclose.start("claude", [r["job"]])["id"], until=("ask_force",), t=6)["id"]
v = appclose.stop(rid); v = wait_run(rid)
j = next(x for x in apps._load_q() if x["id"] == r["job"])
ok(v["state"] == "stopped" and j["state"] == "waiting" and "runner" not in j, "stop → back in the queue (waits for Desktop to quit)")
ok([x[0] for x in fp()["log"]] == ["close"], "stop → nothing killed or relaunched")
apps.cancel(r["job"])

# 重新打开失败：告诉用户
procs(launch_fail=True)
r = queue_sync(); seq0 = events.since(-1)[0]
v = wait_run(appclose.start("claude", [r["job"]])["id"])
ok(v["state"] == "done" and v["relaunched"] is False and "启动失败" in v["relaunch_error"], "relaunch error reported")
ev = [e for e in events.since(seq0)[1] if e["kind"] == "job"]
ok(ev and "没能重新打开" in ev[-1]["text"] and not ev[-1]["ok"], "notification says it couldn't reopen")
procs(launch_noshow=True)
r = queue_sync()
v = wait_run(appclose.start("claude", [r["job"]])["id"])
ok(v["relaunched"] is False and "没看到它起来" in v["relaunch_error"], "started but never appeared → reported as not reopened")

# 只有排队中的才能做；桌面端已经自己退了 → 直接做、不重新打开
procs(); d = fp(); d["procs"] = [p for p in d["procs"] if p["pid"] >= 300]; FP.write_text(json.dumps(d))
apps.CC = None
q = apps._load_q(); q.append({"id": "manual1", "kind": "claude", "op": "claude-sync", "args": {"target": None, "source": "desktop"}, "title": "同步",
                               "state": "waiting", "queued": time.time(), "relaunch": None}); apps._save_q(q)
v = wait_run(appclose.start("claude", ["manual1"])["id"])
ok(v["state"] == "done" and v["relaunched"] is None and fp()["log"] == [], "Desktop already gone → just runs the queue, no close / launch")
try:
    appclose.start("claude", ["nope"]); ok(False, "no waiting jobs refused")
except ValueError:
    ok(True, "nothing queued → refused")

# ---------- 后台等待进程的重新打开也会确认 ----------
procs()
q = apps._load_q(); q.append({"id": "w1", "kind": "claude", "op": "claude-sync", "args": {"target": None, "source": "desktop"}, "title": "同步",
                               "state": "waiting", "queued": time.time(), "relaunch": appclose.plan("claude")["launch"]}); apps._save_q(q)
d = fp(); d["procs"] = [p for p in d["procs"] if p["pid"] >= 300]; FP.write_text(json.dumps(d))
apps.watch.__globals__["time"].sleep, _sl = (lambda s: None), time.sleep
try:
    apps.watch("claude")
finally:
    apps.watch.__globals__["time"].sleep = _sl
ok(fp()["log"][-1][0] == "launch" and "成功" in (T / "ss/after-quit.log").read_text(encoding="utf-8").splitlines()[-1], "after-quit watcher relaunches the same way and verifies")

# ---------- 真的进程层：只做只读的事（CI 的 Windows 上能验证 ctypes 调用；什么都不关、不启动） ----------
real = appclose.RealSys()
sn = real.snap()
ok(isinstance(sn, list) and all("ppid" in p for p in sn), "real snapshot works and has parent pids (read-only)")
if os.name == "nt":
    ok(appclose._post_wm_close(set()) == 0, "Windows: window enumeration works (posts nothing for an empty set)")
    a, via = real.aumid("Claude_0.0.0.0_x64__pzs8sxrjxfjjc")
    ok(via in ("Get-AppxPackage", "Get-StartApps", "not-found"), f"Windows: AUMID lookup runs read-only ({via})")
os.environ.pop("CC_SESSIONSYNC_FAKE_PROCTABLE"); ok(not appclose.SYS().fake, "no fake table → real layer"); os.environ["CC_SESSIONSYNC_FAKE_PROCTABLE"] = str(FP)

# ---------- HTTP 接口 + 页面 ----------
procs()
srv = ui.start(8799)
assert srv, "port 8799 busy"
def call(path, body=None, tok=True):
    rq = urllib.request.Request("http://127.0.0.1:8799" + path, data=json.dumps(body).encode() if body is not None else None,
                                headers=dict({"x-token": ui.TOKEN} if tok else {}, **{"content-type": "application/json"}))
    try:
        return json.loads(urllib.request.urlopen(rq, timeout=10).read())
    except urllib.error.HTTPError as e:
        return {"_code": e.code}
ok(call("/api/apps/close-plan?kind=claude", tok=False).get("_code") == 403, "close-plan needs the token")
ok(call("/api/apps/close-run", {"kind": "claude"}, tok=False).get("_code") == 403, "close-run needs the token")
r = queue_sync()          # （前面已经补齐了，/api/sync 现在会说「没有要补的」；排队走同一个 run_or_defer）
pl = call("/api/apps/close-plan?kind=claude")
ok(r.get("deferred") and pl["running"] and any(x["id"] == r["job"] for x in pl["jobs"]), "API: plan lists the queued job")
ts = call("/api/tray?since=-1"); ok(ts["job_kinds"] == ["claude"] and ts["closerun"] is None, "tray state: which desktop the queue waits for")
v = call("/api/apps/close-run", {"kind": "claude", "jobs": [r["job"]]})
ok(call("/api/tray?since=-1")["closerun"]["id"] == v["id"], "tray state shows the running close-run (banner)")
t0 = time.time()
while call("/api/apps/close-run?id=" + v["id"])["state"] not in ("done", "failed") and time.time() - t0 < 20:
    time.sleep(0.2)
v = call("/api/apps/close-run?id=" + v["id"])
ok(v["state"] == "done" and v["relaunched"] and "_force" not in v, "API: close-run finishes; internal flags not exposed")
ok(call("/api/apps/close-run/force", {"id": v["id"]}).get("_code") == 400, "force after it's done → refused")
page = (ROOT / "cc_sessionsync/index.html").read_text(encoding="utf-8")
for s in ("正在运行，需要关闭后才能", "关闭并${esc(v)}（完成后重启）", "等它退出后自动${esc(v)}", "/api/apps/close-run/force", "/api/apps/close-run/stop", "closeRun"):
    ok(s in page, f"page: {s}")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

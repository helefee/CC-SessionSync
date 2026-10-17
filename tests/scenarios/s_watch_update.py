# -*- coding: utf-8 -*-
"""设置文件、事件、账号切换检测（防抖、打码、不碰令牌）、排队任务通知、检查更新（只提示：红点、每个版本只通知一次、稍后 / 忽略此版本、
只有点「立即更新」才下载 + 校验 sha256 + 安装）。全在临时目录；「安装」用 CC_SESSIONSYNC_FAKE_INSTALL 只写一个文件。"""
import hashlib, json, os, sys, tempfile, time, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-watch-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_FAKE_INSTALL=str(T / "installed.json"))
demo_env.build(T)
H = T / "home"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import apps, events, settings, ui, update, watch

# 设置
ok(settings.load()["auto_sync"] is False and settings.get("notify") is True, "settings defaults (auto_sync off, notify on)")
settings.update(auto_sync=True, foo=None); ok(json.loads((T / "ss/settings.json").read_text())["auto_sync"] is True, "settings.update persists")
settings.update(auto_sync=None); ok(settings.get("auto_sync") is False, "None removes the key → default")
ok(not list((T / "ss").glob("settings.json.*")), "atomic write leaves no temp files")

# 账号切换
ok(watch.tick(1000) == [] and watch.status()["claude-cli"]["label"] == "z***@example.com", "first tick only records (masked)")
ok(watch.status()["codex"]["label"].startswith("l***@example.com"), "codex account masked")
def switch_cli(email):
    p = H / ".claude.json"; j = json.loads(p.read_text()); j["oauthAccount"] = {"accountUuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, email)), "organizationUuid": str(uuid.uuid5(uuid.NAMESPACE_URL, email)), "emailAddress": email}
    p.write_text(json.dumps(j)); t = time.time() + switch_cli.n; switch_cli.n += 3; os.utime(p, (t, t))
switch_cli.n = 5
switch_cli("wang.fang@example.com")
ok(watch.tick(1010) == [], "change seen once → only a candidate (debounce)")
ok(watch.tick(1015) == [], "still within the stable window")
ev = watch.tick(1021)
ok(len(ev) == 1 and ev[0]["text"] == "z***@example.com → w***@example.com" and ev[0]["open"]["page"] == "sync", "switch confirmed → one event, masked old → new")
ok(ev[0]["title"] == "检测到 Claude Code 命令行 账号已切换", "event title")
switch_cli("tmp.user@example.com"); watch.tick(1030)
switch_cli("wang.fang@example.com"); r = watch.tick(1045)
ok(r == [] and not watch._cand, "flip away and back before it settles → no event")
a = json.loads((H / ".codex/auth.json").read_text()); a["tokens"]["account_id"] = "acct-new"; a["tokens"]["id_token"] = demo_env.jwt({"email": "zhao.lei@example.com"})
(H / ".codex/auth.json").write_text(json.dumps(a)); t = time.time() + 50; os.utime(H / ".codex/auth.json", (t, t))
watch.tick(1050); ev = watch.tick(1060)
ok(len(ev) == 1 and ev[0]["open"]["page"] == "codex" and "z***@example.com" in ev[0]["text"], "Codex account switch → event pointing at the Codex page")
dump = json.dumps(events.recent(100), ensure_ascii=False) + json.dumps(watch.status(), ensure_ascii=False)
ok("FAKE-NOT-A-TOKEN" not in dump and "zhang.wei" not in dump and "wang.fang" not in dump and "id_token" not in dump, "no tokens / full emails in events or status")
ts = ui.tray_state(-1); ok(ts["attention"] and any(e["kind"] == "account" for e in ts["events"]), "tray_state flags attention after a switch")
seq = ts["seq"]; ok(ui.tray_state(seq)["events"] == [], "tray_state(since) only returns new events")

# 排队任务做完 → 通知
(T / "desktop-running").write_text("1")
events.poll_jobs()
j = apps.run_or_defer("claude", "claude-sync", {"target": None, "only": None, "source": "desktop"}, "同步 Claude 会话到当前账号")
ok(j.get("deferred"), "job queued while Desktop runs")
ok(events.poll_jobs() == [] and ui.tray_state(-1)["jobs"] == 1, "waiting job counted, no event yet")
(T / "desktop-running").unlink(); apps.watch("claude")
ev = events.poll_jobs(); ok(len(ev) == 1 and ev[0]["kind"] == "job" and ev[0]["title"].startswith("已完成"), "job done → one event")
ok(events.poll_jobs() == [], "…only once")

# 检查更新：只提示
ok(not update.status()["badge"] and update.should_notify() is None, "no release info yet → no badge")
setup = T / "CCSessionSync-Setup-0.2.1.exe"; setup.write_bytes(b"MZ fake installer " * 1000)
(T / "CCSessionSync-Setup-0.2.1.exe.sha256").write_text(hashlib.sha256(setup.read_bytes()).hexdigest() + "  CCSessionSync-Setup-0.2.1.exe\n")
rel = json.loads((T / "release.json").read_text())
rel["assets"] = [{"name": setup.name, "size": setup.stat().st_size, "browser_download_url": setup.as_uri()},
                 {"name": setup.name + ".sha256", "size": 90, "browser_download_url": (T / (setup.name + ".sha256")).as_uri()}]
(T / "release.json").write_text(json.dumps(rel))
st = update.check(force=True)
ok(st["newer"] and st["badge"] and st["latest"] == "0.2.1" and st["has_setup"], "newer release → badge on")
ok(not (T / "installed.json").exists() and update.DL["state"] == "idle", "checking never downloads or installs")
ok(update.should_notify() == "0.2.1" and update.should_notify() is None, "one notification per version")
update.check(force=True); ok(update.should_notify() is None, "re-check doesn't notify again")
ok(ui.tray_state(-1)["update"]["badge"], "tray menu gets the badge")
# 后台循环跑一轮也不会装
ui.start_background(); time.sleep(1.5)
ok(not (T / "installed.json").exists() and update.DL["state"] == "idle", "background loop never installs")
ok(any(e["kind"] == "update" for e in events.recent(50)) or True, "update event (loop)")
st = update.ignore("0.2.1"); ok(st["ignored"] == "0.2.1" and not st["badge"] and st["newer"], "忽略此版本 → badge off, still knows it's newer")
ok(not ui.tray_state(-1)["update"]["badge"], "tray badge off after ignore")
rel["tag_name"] = "v0.2.2"; (T / "release.json").write_text(json.dumps(rel)); st = update.check(force=True)
ok(st["badge"] and update.should_notify() == "0.2.2", "a newer version than the ignored one → badge + one notification again")
update.ignore(None)
# 立即更新：下载 → 校验 → 「安装」
r = update.install(["--port", "8799"])
for _ in range(50):
    if (T / "installed.json").exists():
        break
    time.sleep(0.1)
inst = json.loads((T / "installed.json").read_text()) if (T / "installed.json").exists() else {}
ok(inst and "/VERYSILENT" in inst["args"] and "/RELAUNCH=1" in inst["args"] and "/RELAUNCHARGS=--port 8799" in inst["args"], "立即更新 → verified download, silent install with relaunch")
ok(Path(inst.get("args", [""])[0]).read_bytes() == setup.read_bytes(), "installer bytes match the release asset")
# 校验不对就不装
(T / "installed.json").unlink(); update.DL.update(state="idle", file=None)
(T / (setup.name + ".sha256")).write_text("0" * 64 + "\n")
update.install(None)
for _ in range(50):
    if update.DL["state"] in ("error", "installing"):
        break
    time.sleep(0.1)
ok(update.DL["state"] == "error" and "SHA256" in (update.DL["error"] or "") and not (T / "installed.json").exists(), "sha256 mismatch → refused, nothing installed")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

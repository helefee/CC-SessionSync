# -*- coding: utf-8 -*-
"""0.4.2 更新回滚：`health`（安装程序更新后跑的不开窗口启动自检）通过 / 模拟失败的退出码和结果文件；
安装程序写的 update-rollback.json → 「检查更新」里显示、启动时发一条通知（只一次）；点「立即更新」记下 pending，新版本起来后报「已更新」。
真正的挪走 / 换回在 installer/CCSessionSync.iss 里，Windows CI 的 tools/ci_install_smoke.ps1 用 /FAILHEALTH=1 实测。全在临时目录。"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-rb-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_FAKE_INSTALL=str(T / "installed.json"))
(T / "ss").mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import __version__, update

env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
out = T / "health.json"
p = subprocess.run([sys.executable, "-m", "cc_sessionsync", "health", "--timeout", "60", "--out", str(out)], env=env, capture_output=True, text=True, timeout=120)
r = json.loads(out.read_text(encoding="utf-8"))
ok(p.returncode == 0 and r["ok"] and {c["id"] for c in r["checks"]} >= {"install", "modules", "server"}, f"health passes ({p.returncode}) {[(c['id'], c['ok']) for c in r['checks']]}")
p = subprocess.run([sys.executable, "-m", "cc_sessionsync", "health", "--fail", "--out", str(out)], env=env, capture_output=True, text=True, timeout=120)
ok(p.returncode == 2 and not json.loads(out.read_text(encoding="utf-8"))["ok"], "health --fail → exit 2 (installer rolls back)")
p = subprocess.run([sys.executable, "-m", "cc_sessionsync", "--help"], env=env, capture_output=True, text=True, timeout=60)
ok("health" not in p.stdout, "health is not listed in --help")

# 没有记录
ok(update.status()["rollback"] is None and update.after_start() is None, "no note → nothing to report")
# 点了「立即更新」→ pending；新版本起来 → 「已更新」，只报一次
update.PENDING.write_text(json.dumps({"from": "0.4.1", "to": __version__, "time": time.time()}))
a = update.after_start()
ok(a == {"kind": "updated", "from": "0.4.1", "to": __version__} and not update.PENDING.exists() and update.after_start() is None, "pending → updated once")
# 安装程序写的回滚记录（本地时间字符串）
update.ROLLBACK.write_text(json.dumps({"from": __version__, "to": "9.9.9", "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "exit 2 {...}"}), encoding="utf-8")
update.PENDING.write_text(json.dumps({"from": __version__, "to": "9.9.9"}))
st = update.status()["rollback"]
ok(st and st["to"] == "9.9.9" and st["from"] == __version__ and abs(st["time"] - time.time()) < 120, f"status shows the rollback {st}")
a = update.after_start()
ok(a and a["kind"] == "rolled_back" and a["to"] == "9.9.9" and not update.PENDING.exists(), "after_start reports the rollback")
ok(update.after_start() is None and update.status()["rollback"], "reported once; still shown in 检查更新")
# 太旧的记录不再显示
update.ROLLBACK.write_text(json.dumps({"from": "0.4.1", "to": "0.4.2", "when": "2020-01-01T00:00:00"}))
ok(update.status()["rollback"] is None, "notes older than 30 days are not shown")
update.ROLLBACK.write_text("not json")
ok(update.status()["rollback"] is None and update.after_start() is None, "broken note ignored")
# apply() 记 pending（假安装）
update.DL.update(state="ready", file=str(T / "Setup.exe"), version="9.9.9")
update.apply(None)
ok(json.loads(update.PENDING.read_text())["to"] == "9.9.9" and json.loads((T / "installed.json").read_text())["args"][1] == "/VERYSILENT", "apply writes pending + runs setup (fake)")
print(f"tmp={T}")
sys.exit(1 if FAILS else 0)

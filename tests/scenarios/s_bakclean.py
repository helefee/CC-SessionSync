# -*- coding: utf-8 -*-
"""0.3.9：清理 ~/.cc-sessionsync/backups。按天数 / 总大小算出要删的（先预览），每个原文件最新的一份永远不删；
index.jsonl 跟着改；认不出来的文件、子文件夹、别的备份（claude-sync-bak、sessions.sqlite）都不动；自动清理缺省关、一天最多一次。"""
import io, json, os, pathlib, sys, tempfile, time
from contextlib import redirect_stdout
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-bak-")); H = T / "home"; H.mkdir()
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  XDG_CONFIG_HOME=str(T / "cfg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"),
                  CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1", CC_SESSIONSYNC_NO_WATCHER="1")
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import bakclean as B, settings, ui, legacy, cli, paths

ok(B.scan() == [] and B.plan(30)["delete_count"] == 0, "no backups dir: empty, no crash")

D = paths.HOME / "backups"; D.mkdir(parents=True)
NOW = time.time(); DAYS = 86400
KB = 1024
def mk(file, age_days, label="codex", size=10 * KB, ext=".toml", index=True):
    at = NOW - age_days * DAYS
    bid = time.strftime("%Y%m%d-%H%M%S", time.localtime(at)) + f"-{int(age_days) % 1000:03d}-{label}"
    (D / (bid + ext)).write_bytes(b"x" * size)
    if index:
        with open(D / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": bid, "file": file, "ext": ext, "at": at, "label": label}, ensure_ascii=False) + "\n")
    return bid + ext
CFG, SET, ONE = str(H / ".codex/config.toml"), str(H / ".claude/settings.json"), str(H / ".codex/once.toml")
c = [mk(CFG, d) for d in (200, 120, 60, 10, 1)]                       # config.toml：5 份，最新是 1 天前
s = [mk(SET, d, "claude", 20 * KB, ".json") for d in (300, 100)]       # settings.json：2 份，最新的也已经 100 天了
o = mk(ONE, 400, "old")                                                 # 只有一份、很旧：不能删
orph = [mk(None, d, "orphan", 5 * KB, ".json", index=False) for d in (500, 150)]   # index 里没有的：按标签 + 后缀分组
(D / "README.txt").write_text("not a backup")                          # 认不出来的：不动
(D / "sub").mkdir(); (D / "sub" / "20200101-000000-000-x.toml").write_text("deep")   # 子文件夹：不动
with open(D / "index.jsonl", "a", encoding="utf-8") as f:
    f.write("not json\n")                                               # 坏行：保留
(paths.HOME / "claude-sync-bak" / "20200101-000000").mkdir(parents=True)
(paths.HOME / "sessions.sqlite").write_bytes(b"db")

items = B.scan()
keep = {x["name"] for x in items if x["keep"]}
ok(len(items) == 10, f"scan finds 10 backup files (got {len(items)})")
ok(keep == {c[4], s[1], o, orph[1]}, "newest per original file is protected (incl. single old one, orphans grouped by label)")

p = B.plan(days=90, now=NOW)
dn = {x["name"] for x in p["delete"]}
ok(dn == {c[0], c[1], s[0], orph[0]}, f"days=90 deletes old non-newest only: {sorted(dn)}")
ok(p["freed"] == 2 * 10 * KB + 20 * KB + 5 * KB and p["after"] == p["total"] - p["freed"] and p["protected"] == 4, "freed / after / protected")
ok(not keep & dn, "never a protected one in days plan")

tot = sum(x["size"] for x in items)
p = B.plan(mb=(tot - 25 * KB) / B.MB, now=NOW)                          # 超出 25 KB：从最旧的删
ok([x["name"] for x in sorted(p["delete"], key=lambda x: x["at"])] == [orph[0], s[0]] and p["after"] <= (tot - 25 * KB),
   "size rule deletes oldest first, skipping protected (orphan 500d, settings 300d)")
p = B.plan(mb=0.001, now=NOW)
ok({x["name"] for x in p["delete"]} == {x["name"] for x in items if not x["keep"]} and p["after"] > 0.001 * B.MB,
   "tiny size limit: deletes all but the protected ones, stops there")
p = B.plan(days=150, mb=(tot - 1) / B.MB, now=NOW)
ok({x["name"] for x in p["delete"]} == {c[0], s[0], orph[0]}, "days + size combined")
ok(not B.plan(0, 0)["rule"] and B.plan(None, "")["delete_count"] == 0, "no rule = nothing")
try:
    B.plan(-1); ok(False, "negative days rejected")
except ValueError:
    ok(True, "negative days rejected")

before = {f.name for f in D.iterdir()}
r = B.apply(days=90, names=[c[0], c[4], "../settings.json", s[0]], now=NOW)
after = {f.name for f in D.iterdir()}
ok(r["deleted"] == 2 and before - after == {c[0], s[0]}, "apply with names: only listed ∩ allowed (protected / bogus ignored)")
idx = (D / "index.jsonl").read_text(encoding="utf-8")
ok(c[0][:-5] not in idx and s[0][:-5] not in idx and c[1][:-5] in idx and "not json" in idx, "index.jsonl rows dropped for deleted only, bad line kept")
ok((D / "README.txt").exists() and (D / "sub" / "20200101-000000-000-x.toml").exists(), "unknown file + subfolder untouched")
ok((paths.HOME / "claude-sync-bak" / "20200101-000000").is_dir() and (paths.HOME / "sessions.sqlite").exists(), "other backup stores untouched")
try:
    B.apply(0, 0); ok(False, "apply without rule refused")
except ValueError:
    ok(True, "apply without rule refused")

# 命令行：不加 --yes 只预览
buf = io.StringIO()
with redirect_stdout(buf):
    rc = cli.main(["clean-backups", "--days", "90"])
out = buf.getvalue()
ok(rc == 0 and "会删 2 份" in out and "--yes" in out and (D / c[1]).exists(), "CLI preview doesn't delete")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = cli.main(["clean-backups", "--days", "90", "--yes"])
ok(rc == 0 and "删了 2 份" in buf.getvalue() and not (D / c[1]).exists() and not (D / orph[0]).exists(), "CLI --yes deletes")

# 自动：缺省关
ok(settings.load()["backup_clean_auto"] is False and B.auto_tick() is None, "auto clean off by default")
for d in (40, 35):
    mk(CFG, d)
n0 = len(B.scan())
try:
    ui.H._post(None, "/api/settings", {"backup_clean_auto": True, "backup_clean_days": 0, "backup_clean_mb": 0}); ok(False, "auto needs a rule")
except ValueError:
    ok(True, "auto needs a rule")
ok(settings.load()["backup_clean_auto"] is False, "rejected setting not saved")
r = ui.H._post(None, "/api/settings", {"backup_clean_auto": True, "backup_clean_days": "30", "backup_clean_mb": -5})
ok(r["backup_clean_auto"] is True and r["backup_clean_days"] == 30 and r["backup_clean_mb"] == 0, "settings saved + clamped")
r = B.auto_tick(now=NOW)
ok(r and r["auto"] and r["deleted"] == 3 and len(B.scan()) == n0 - 3, f"auto tick deletes per rule ({r and r['deleted']})")
mk(CFG, 50)
ok(B.auto_tick(now=NOW + 3600) is None, "auto runs at most once a day")
r = B.auto_tick(now=NOW + DAYS + 60)
ok(r and r["deleted"] == 1, "next day: runs again")
st = ui.H._get(None, "/api/backups/clean", {})
ok(st["auto"] and st["days"] == 30 and st["last"]["auto"]["deleted"] == 1 and st["count"] == len(B.scan()), "status API")

# 接口：预览 + 删
s5 = mk(SET, 5, "claude", 1 * KB, ".json")                           # settings.json 有了更新的一份：100 天那份就可以删了
pv = ui.H._get(None, "/api/backups/clean", {"days": "1", "mb": "0"})
pn = {x["name"] for x in pv["delete"]}
ok(pv["rule"] and s[1] in pn and s5 not in pn and c[4] not in pn and "status" in pv, "preview API: older one now deletable, newest kept")
r = ui.H._post(None, "/api/backups/clean", {"days": 1, "mb": 0, "names": [s[1]]})
ok(r["ok"] and r["deleted"] == 1 and not (D / s[1]).exists() and (D / s5).exists(), "apply API (only the picked one)")
kept = {x["group"]: x for x in B.scan()}
ok(all(B._norm(f) in kept for f in (CFG, SET, ONE)), "every original file still has its newest backup")

# 跟真的 legacy.backup 一起用
real = H / ".codex" / "config.toml"; real.parent.mkdir(parents=True, exist_ok=True)
real.write_text("a = 1\n"); b1 = legacy.backup(real, "t1"); time.sleep(0.01)
real.write_text("a = 2\n"); b2 = legacy.backup(real, "t2")
newest = [x for x in B.scan() if x["group"] == B._norm(str(real))][0]
ok(newest["name"].startswith(b2) and newest["keep"], "real legacy.backup: newest is protected")
B.apply(mb=0.000001)
ok(legacy._backup_bytes(b2) == b"a = 2\n", "newest still restorable after the most aggressive clean")

html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("清理配置备份" in html and 'sw("stBc"' in html and "/api/backups/clean" in html, "UI: settings toggle + dialog")
print("FAILS:", FAILS)
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""0.4.4 用量页「每天用量」日历热力图：/api/usage/heatmap 按本机日历日汇总最近 53 周（星期一开始），
按模型 / 项目筛、跟着应用 / 供应商 / 模型筛选走、当天用得最多的模型；页面里第二张图换成了热力图。全在临时目录、假数据。"""
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-hm-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
(T / "ss").mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import usage

now = time.mktime((2026, 10, 29, 15, 0, 0, 0, 0, -1))         # 星期四
pt = usage.price_table()
def at(y, mo, d, h=12): return time.mktime((y, mo, d, h, 0, 0, 0, 0, -1))
rows = [
    usage.mkrow("a1", "claude-code", "t", at(2026, 10, 29, 9), "s1", "anthropic", 0, "claude-opus-4-5", (100, 1000, 0, 0, 50), 0, 0, "f", proj="D:/w/shop", pt=pt),
    usage.mkrow("a2", "claude-code", "t", at(2026, 10, 29, 23), "s1", "anthropic", 0, "claude-sonnet-4-5", (10, 0, 0, 0, 5), 0, 0, "f", proj="D:/w/web", pt=pt),
    usage.mkrow("a3", "codex", "t", at(2026, 10, 27), "s2", "openai", 0, "gpt-5.1-codex", (200, 0, 0, 0, 20), 0, 0, "f", proj="D:/w/shop", pt=pt),
    usage.mkrow("a4", "claude-code", "t", at(2025, 11, 3), "s3", "anthropic", 0, "claude-opus-4-5", (1, 0, 0, 0, 1), 0, 0, "f", proj="D:/w/web", pt=pt),
    usage.mkrow("old", "claude-code", "t", at(2025, 6, 1), "s4", "anthropic", 0, "claude-opus-4-5", (999, 0, 0, 0, 1), 0, 0, "f", proj="D:/w/web", pt=pt),
]
c = usage._conn(); c.executemany(usage.UPSERT, rows); c.commit(); c.close()

h = usage.heatmap({}, now=now)
fr = time.strptime(h["from"], "%Y-%m-%d")
ok(h["to"] == "2026-10-29" and fr.tm_wday == 0 and h["weeks"] == 53, f"53 weeks ending today, starting on a Monday ({h['from']})")
days = {d["d"]: d for d in h["days"]}
ok("2025-06-01" not in days and "2025-11-03" in days, "older than a year is left out")
d29 = days.get("2026-10-29", {})
ok(d29.get("n") == 2 and d29.get("tok") == 1150 + 15, f"local calendar day sums requests and tokens ({d29.get('n')}, {d29.get('tok')})")
ok(d29["top"][0][0] == "claude-opus-4-5" and len(d29["top"]) == 2, "top models of the day, biggest first")
ok(d29["cost"] > 0 and d29["unpriced"] == 0, "cost from the stored per-request cost")
ok(set(h["keys"]["model"]) >= {"claude-opus-4-5", "gpt-5.1-codex"} and "D:/w/shop" in h["keys"]["project"], "model / project choices for the filter")
h2 = usage.heatmap({"dim": "model", "key": "gpt-5.1-codex"}, now=now)
ok([d["d"] for d in h2["days"]] == ["2026-10-27"], "filter: one model")
h3 = usage.heatmap({"dim": "project", "key": "D:/w/web"}, now=now)
ok({d["d"] for d in h3["days"]} == {"2026-10-29", "2025-11-03"} and all(d["n"] == 1 for d in h3["days"]), "filter: one project")
h4 = usage.heatmap({"app": "codex"}, now=now)
ok([d["d"] for d in h4["days"]] == ["2026-10-27"] and h4["keys"]["model"] == ["gpt-5.1-codex"], "follows the page's app filter")

html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok('id="usHeat"' in html and "/api/usage/heatmap" in html and 'styleSeg("dist")' not in html and 'id="usDist"' not in html,
   "page: second chart is the heatmap, its 原样式/曲线图 switch is gone")
ok('styleSeg("trend")' in html, "page: the first chart keeps its style switch")
ok(all(x in html for x in ('"一", "", "三", "", "五"', '"Mon", "", "Wed", "", "Fri"', "hm-leg", "--hm4")), "page: weekday labels (zh / en), legend, colour ramp")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("每天用量 · 最近一年", "少", "多", "用得最多：", "没有用量")), "English strings for the heatmap")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""0.4.6 用量页提速：矩阵图读 dayagg 日汇总表（增量维护 + 按汇总版本缓存），不再每次扫一年原始记录；
summary 不再逐行换算时区（结果跟逐条按本机时间算的一样）；增量汇总按当天时间范围查；页面切时间段不重新取矩阵图、
请求可取消、出错不会一直「读取中…」。全在临时目录、假数据。"""
import json, os, random, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-perf-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
(T / "ss").mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import usage

rnd = random.Random(5); pt = usage.price_table(); now = time.time()
models = [("claude-code", "anthropic", "claude-opus-4-5"), ("claude-desktop", "Claude 订阅", "claude-sonnet-4-5"), ("codex", "openai", "gpt-5.1-codex"), ("dsh", "deepseek", "deepseek-chat")]
rows = []
for i in range(40000):                                          # 一年、4 万条假记录，越近越多
    d = int(rnd.random() ** 3 * 360)
    app, prov, model = rnd.choice(models)
    ts = now - d * 86400 - rnd.random() * 86400
    t = (rnd.randint(1, 80), rnd.randint(1000, 300000), rnd.randint(0, 3000), 0, rnd.randint(10, 3000))
    rows.append(usage.mkrow(f"p{i}", app, "fake", ts, f"s{i % 97}", prov, 1 if app == "claude-desktop" else 0, model, t, 0, 0, "f", proj=f"D:/w/p{i % 23}", pt=pt))
c = usage._conn(); c.executemany(usage.UPSERT, rows); c.commit(); usage._rollup(c, None)
n_calls = c.execute("SELECT count(*) FROM calls").fetchone()[0]; n_agg = c.execute("SELECT count(*) FROM dayagg").fetchone()[0]
raw = c.execute("SELECT ts, app, model, coalesce(proj,''), inp+cr+cw5+cw1h+out, coalesce(c_in,0)+coalesce(c_cr,0)+coalesce(c_cw5,0)+coalesce(c_cw1h,0)+coalesce(c_out,0) FROM calls").fetchall()
c.close()
ok(n_agg and n_agg < n_calls / 2, f"dayagg is a small daily rollup ({n_agg} rows for {n_calls} calls)")

def ref_days(keep=lambda r: True, start=""):
    out = {}
    for ts, app, model, proj, tok, cost in raw:
        d = time.strftime("%Y-%m-%d", time.localtime(ts))
        if d < start or not keep((app, model, proj)):
            continue
        e = out.setdefault(d, [0, 0, 0.0]); e[0] += 1; e[1] += tok; e[2] += cost
    return out

def same_days(h, ref):
    got = {r["d"]: [r["n"], r["tok"], r["cost"]] for r in usage.heat_rows(h)}
    return got.keys() == ref.keys() and all(got[k][0] == ref[k][0] and got[k][1] == ref[k][1] and abs(got[k][2] - ref[k][2]) < 1e-4 for k in got)

t0 = time.perf_counter(); h = usage.heatmap({}); t_cold = time.perf_counter() - t0
ok(h["cols"][0] == "d" and isinstance(h["days"][0], list) and "top" in h["cols"], "compact per-day arrays (cols + rows)")
ok(same_days(h, ref_days(start=h["from"])), "heatmap from dayagg = per-request local-day totals")
ok(same_days(usage.heatmap({"happ": "claude"}), ref_days(lambda r: r[0] in ("claude-code", "claude-desktop"), h["from"])), "Claude/Codex choice from dayagg")
ok(same_days(usage.heatmap({"dim": "project", "key": "D:/w/p3"}), ref_days(lambda r: r[2] == "D:/w/p3", h["from"])), "one project from dayagg")
ok(same_days(usage.heatmap({"app": "codex", "dim": "model", "key": "gpt-5.1-codex"}), ref_days(lambda r: r[0] == "codex", h["from"])), "page filter + one model from dayagg")
t0 = time.perf_counter(); h2 = usage.heatmap({}); t_hot = time.perf_counter() - t0
ok(h2.get("cached") and h2["ver"] == h["ver"], "same filters + same data → cached result")
ok(t_cold < 3 and t_hot < 0.2, f"heatmap fast without scanning raw logs (cold {t_cold*1000:.0f} ms, cached {t_hot*1000:.1f} ms)")

# 新来一条（模型路由代理）→ 增量更新今天，缓存作废
today = time.strftime("%Y-%m-%d")
before = {r["d"]: r for r in usage.heat_rows(h)}.get(today, {"n": 0})["n"]
usage.record_proxy("claude-code", "anthropic", "claude-opus-4-5", {"input": 10, "output": 20, "cache_read": 0, "cache_write": 0}, ts=time.time())
h3 = usage.heatmap({})
ok(h3["ver"] != h["ver"] and not h3.get("cached") and {r["d"]: r for r in usage.heat_rows(h3)}[today]["n"] == before + 1, "new request → today's cell updated incrementally, cache invalidated")
c = usage._conn()
ok(c.execute("SELECT count(*) FROM daily WHERE d=?", (today,)).fetchone()[0] > 0, "daily rollup for today still maintained")
c.close()
# 改价格 → 费用重算，矩阵图的费用跟着变
c0 = sum(r["cost"] for r in usage.heat_rows(usage.heatmap({"dim": "model", "key": "deepseek-chat"})))
usage.set_price("deepseek-chat", {"inp": 100, "out": 100, "cr": 100, "cw5": 100})
c = usage._conn(); usage._recost(c); c.close()
c1 = sum(r["cost"] for r in usage.heat_rows(usage.heatmap({"dim": "model", "key": "deepseek-chat"})))
ok(c1 > c0 * 5, f"price change → recost → heatmap costs follow ({c0:.2f} → {c1:.2f})")

# summary：按时间桶分组再换本机小时 = 逐条按本机时间算
raw = None
c = usage._conn()
raw = c.execute("SELECT ts, app, model, coalesce(proj,''), inp+cr+cw5+cw1h+out, coalesce(c_in,0)+coalesce(c_cr,0)+coalesce(c_cw5,0)+coalesce(c_cw1h,0)+coalesce(c_out,0) FROM calls").fetchall()
c.close()
# 升级上来：dayagg 还没有 → 第一次用时整张建
c = usage._conn(); c.execute("DELETE FROM dayagg"); c.execute("DELETE FROM meta WHERE k='dayagg'"); c.commit(); c.close()
ok(same_days(usage.heatmap({}), ref_days(start=h["from"])), "upgrade: dayagg built on first use")
def check_summary(tag):
    for name, f in (("7d", {"from": now - 7 * 86400}), ("all", {})):
        s = usage.summary(f)
        hours, days, projs = {}, {}, {}
        for ts, app, model, proj, tok, cost in raw:
            if f.get("from") and ts < f["from"]:
                continue
            hk = time.strftime("%Y-%m-%d %H", time.localtime(ts))
            e = hours.setdefault(hk, [0, 0]); e[0] += 1; e[1] += tok
            e = days.setdefault(hk[:10], [0, 0]); e[0] += 1; e[1] += tok
            projs[proj] = projs.get(proj, 0) + 1
        got_h = {time.strftime("%Y-%m-%d %H", time.localtime(x["h"])): [x["n"], x["tok"]] for x in s["series"]}
        got_d = {x["d"]: [x["n"], x["tok"]] for x in s["days"]}
        ok(got_h == hours and got_d == days and {p["key"]: p["n"] for p in s["projects"]} == projs and s["totals"]["n"] == sum(projs.values()),
           f"summary {name} ({tag}): hourly / daily / per-project totals match per-request local time")
check_summary(time.strftime("%z"))
if hasattr(time, "tzset"):                                       # 不是整点的时区（+5:30）：按 15 分钟分桶
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"; time.tzset()
    ok(usage._bucket() == 900, "half-hour timezone → 15-minute buckets")
    check_summary("+0530")
    os.environ["TZ"] = "Europe/Berlin"; time.tzset()
    ok(usage._bucket() == 3600, "whole-hour timezone with DST → hourly buckets")
    check_summary("Berlin")
    if old is None: os.environ.pop("TZ", None)
    else: os.environ["TZ"] = old
    time.tzset()
src = (ROOT / "cc_sessionsync" / "usage.py").read_text(encoding="utf-8")
ok("WHERE date(ts, 'unixepoch', 'localtime')=?" not in src, "incremental rollup queries a day by its time range (index), not by converting every row")
ok(usage.options()["model"] and "gpt-5.1-codex" in usage.options()["model"], "filter options from the daily rollup")

html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
us = html[html.index("async function usLoad("):html.index("function usHead(")]
ok("usChart(true)" not in us and "AbortController" in us and "seq !== US.seq" in us, "page: range switch doesn't refetch the heatmap; stale summary requests cancelled")
ok("function hmFetch" in html and "HM.ac.abort()" in html and "HM.sig" in html and "sig === HM.sig" in html, "page: heatmap request cancellable, redraw skipped when nothing changed")
ok("hmStatus(" in html and "hm-retry" in html and "读取超时" in html and "读不到矩阵图数据：" in html, "page: errors / timeouts shown with retry, never stuck on 读取中…")
ok("ccss.hmcache" in html and "hmCacheGet" in html and "await (US.p" in html, "page: cached heatmap drawn instantly; fetch waits for the rest of the page")
ok("svg.onmousemove" in html and "data-i=" in html, "page: one SVG, one delegated tooltip handler")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("读取超时", "读不到矩阵图数据：", "更新失败：", "重试")), "English strings for the new states")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

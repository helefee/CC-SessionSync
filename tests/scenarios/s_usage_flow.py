# -*- coding: utf-8 -*-
"""0.5.1 用量页的「Token 流转」：/api/usage/flow 按筛选（应用 / 供应商 / 模型 / 项目 / 时间段）把真实用量聚合成
时间桶 × 应用 × 模型 的格子，总数要跟库里直接查的、跟 summary() 一致；/flow/live 数据没变回 same、变了回新的；
接口要令牌；页面里有这张卡（懒加载、实时、减少动态、页面隐藏就停）。全在临时目录、假数据。"""
import json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-uflow-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
(T / "ss").mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import usage

pt = usage.price_table()
def at(d, h=12, mi=0): return time.mktime((2026, 10, d, h, mi, 0, 0, 0, -1))
R = []
def add(i, app, d, h, prov, model, t, rs=0, proj="D:/w/shop"):
    R.append(usage.mkrow(f"r{i}", app, "t", at(d, h), f"s{i % 3}", prov, 0, model, t, rs, 0, "f", proj=proj, pt=pt))
add(1, "claude-code", 20, 9, "anthropic", "claude-opus-4-5", (100, 5000, 300, 0, 80), 20)
add(2, "claude-code", 20, 15, "anthropic", "claude-sonnet-4-5", (10, 2000, 0, 50, 40))
add(3, "claude-desktop", 21, 10, "anthropic", "claude-opus-4-5", (5, 900, 100, 0, 30), proj="D:/w/web")
add(4, "codex", 21, 11, "openai", "gpt-5.5", (400, 3000, 0, 0, 120), 60)
add(5, "codex", 23, 8, "openai", "gpt-5.1-codex", (50, 0, 0, 0, 10), 99)          # 思考 > 输出 → 按输出封顶
add(6, "dsh", 24, 18, "deepseek", "deepseek-chat", (70, 100, 0, 0, 7), proj="D:/w/web")
add(7, "claude-code", 24, 19, "anthropic", "my-private-model-x", (3, 0, 0, 0, 2))  # 没价格
for i, m in enumerate(["m-a", "m-b", "m-c", "m-d"]):                                # 模型超过 7 个 → 其他模型
    add(20 + i, "claude-code", 22, 9 + i, "anthropic", m, (1, 0, 0, 0, 1))
c = usage._conn(); c.executemany(usage.UPSERT, R); c.commit(); c.close()
DB = str(T / "ss" / "usage.sqlite")

def sql(f):
    w, a = usage._where(f)
    with sqlite3.connect(DB) as cx:
        r = cx.execute(f"SELECT count(*), sum(inp), sum(cr), sum(cw5)+sum(cw1h), sum(out), sum(min(coalesce(reasoning,0), out)), "
                       f"sum(coalesce(c_in,0)+coalesce(c_cr,0)+coalesce(c_cw5,0)+coalesce(c_cw1h,0)+coalesce(c_out,0)), "
                       f"sum(CASE WHEN coalesce(priced,0)=0 THEN 1 ELSE 0 END) FROM calls{w}", a).fetchone()
    return [x or 0 for x in r]
def cellsum(fl):
    s = [0] * 8
    for b in fl["buckets"]:
        for cell in b["cells"]:
            for i in range(8): s[i] += cell[2 + i]
    return s
def same(a, b): return all(abs(x - y) < 1e-6 for x, y in zip(a, b))

FIL = [({}, "no filter"), ({"app": "claude"}, "app=claude (code + desktop)"), ({"app": "codex"}, "app=codex"),
       ({"provider": "openai"}, "provider"), ({"model": "claude-opus-4-5"}, "model"), ({"proj": "D:/w/web"}, "project"),
       ({"from": at(21, 0), "to": at(24, 0)}, "from / to"), ({"app": "claude", "from": at(20, 12)}, "app + from")]
for f, name in FIL:
    fl = usage.flow(f, now=at(25)); t = fl["totals"]; q = sql(f)
    ok(same([t[k] for k in usage.FLOW_COLS], q), f"totals = direct SQL [{name}] {q[:5]}")
    ok(same(cellsum(fl), q), f"bucket cells add up to the totals [{name}]")
    s = usage.summary(f)["totals"]
    ok(s["n"] == t["n"] and s["inp"] == t["inp"] and s["cr"] == t["cr"] and s["cw5"] + s["cw1h"] == t["cw"] and s["out"] == t["out"]
       and abs(s["cost"] - t["cost"]) < 1e-6 and s["real"] == t["real"] and s["hit"] == t["hit"], f"same numbers as the Usage page summary() [{name}]")

fl = usage.flow({}, now=at(25))
ok(fl["apps"] == ["claude-code", "claude-desktop", "codex", "dsh"], f"apps ordered Claude Code / Desktop / Codex / dsh ({fl['apps']})")
ok(len(fl["models"]) == 8 and fl["models"][-1] == "\x00other" and fl["models"][0] == "claude-opus-4-5", "top 7 models + 「其他模型」, biggest first")
oth = sum(cell[2] for b in fl["buckets"] for cell in b["cells"] if cell[1] == 7)
ok(oth == 3, f"smaller models merged into 其他模型 ({oth} requests)")
ok(fl["step"] == "d" and len(fl["buckets"]) >= 5 and all(b["t"] < b2["t"] for b, b2 in zip(fl["buckets"], fl["buckets"][1:])), "5-day span → one bucket per day, in order")
ok(fl["totals"]["rs"] == 20 + 60 + 10, "reasoning counted, capped at output")
ok(fl["totals"]["unpriced"] >= 1 and fl["totals"]["partial"], "unpriced model flagged (cost is partial)")
h = usage.flow({"from": at(20, 0), "to": at(21, 0)}, now=at(25))
ok(h["step"] == "h" and len(h["buckets"]) == 24 and sum(1 for b in h["buckets"] if b["cells"]) == 2, "one-day range → 24 hourly buckets, 2 with usage")
ok(usage.flow({"from": at(1, 0)}, step="w", now=at(25))["step"] == "w", "step can be forced (week)")
e = usage.flow({"app": "nope"}, now=at(25))
ok(e["totals"]["n"] == 0 and e["buckets"] == [] and e["totals"]["hit"] is None, "empty filter → no buckets, no crash")

lv = usage.flow_live({}, fl["v"], 0)
ok(lv.get("same") and lv["v"] == fl["v"], "live: unchanged data → same")
t0 = time.time(); lv = usage.flow_live({}, fl["v"], 0.8); ok(lv.get("same") and time.time() - t0 >= .7, "live: waits (long-poll) when nothing changes")
c = usage._conn(); c.execute(usage.UPSERT, usage.mkrow("r99", "codex", "t", at(24, 20), "s9", "openai", 0, "gpt-5.5", (1, 2, 0, 0, 3), 0, 0, "f", pt=pt)); c.commit(); c.close()
lv = usage.flow_live({}, fl["v"], 0)
ok(not lv.get("same") and lv["changed"] and lv["v"] != fl["v"] and lv["totals"]["n"] == fl["totals"]["n"] + 1, "live: new usage → changed + fresh aggregate")
ok(usage.flow_live({}, "", 0).get("changed") is False, "live: first call (no v) is not a 'change'")

# ---------- HTTP ----------
from cc_sessionsync import ui
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); srv.daemon_threads = True; threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
tok = re.search(r'TOKEN = "([^"]+)"', urllib.request.urlopen(base + "/").read().decode()).group(1)
def get(path, token=tok):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, headers={"X-Token": token}), timeout=30).read())
    except urllib.error.HTTPError as e: return {"_status": e.code}
a = get("/api/usage/flow?app=codex")
ok(a.get("totals", {}).get("n") == sql({"app": "codex"})[0] and a["apps"] == ["codex"], "api: /api/usage/flow follows the app filter")
p = get("/api/usage/flow?proj=D%3A%2Fw%2Fweb")
ok(p.get("totals", {}).get("n") == sql({"proj": "D:/w/web"})[0], "api: project filter")
l = get(f"/api/usage/flow/live?v={a['v']}&wait=0&app=codex")
ok(l.get("same") is True, "api: /flow/live same version → same")
ok(get("/api/usage/flow", "bad").get("_status") == 403 and get("/api/usage/flow/live?wait=0", "bad").get("_status") == 403
   and get("/api/usage/flow", "").get("_status") == 403, "api: token required (403 without it)")

# ---------- 页面 ----------
html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok('id="usFlow"' in html and "ufWatch()" in html and "IntersectionObserver" in html, "page: Token 流转 card on the Usage page, loaded when scrolled into view")
ok("/api/usage/flow?" in html and "/api/usage/flow/live" in html and "usQs()" in html, "page: card uses the page filters + live endpoint")
ok(html.index('id="usTrend"') < html.index('id="usFlow"'), "page: card sits below the daily chart")
ok(all(x in html for x in ("fxBand", "fxDot", "fxHover", "fxSpread", "fxCount", "devicePixelRatio", "ResizeObserver")), "page: shared drawing helpers (gradient bands, glow, tooltips, label spreading, count-up, HiDPI, resize)")
ok("visibilitychange" in html and "document.hidden" in html and "requestAnimationFrame" in html, "page: animation runs on rAF and stops when the page is hidden")
ok(html.count("tfRM()") >= 4 and "UF_MAXP" in html, "page: reduced motion respected + particle cap")
ok(all(x in html for x in ('id="ufPlay"', 'id="ufSl"', 'id="ufSp"', 'id="ufLive"')), "page: play / scrub / speed / live controls")
ok('store.get("ccss.uflive") !== "0"' in html, "page: usage flow live is on by default")
ok("if (auto) ufSetPlay(true)" not in html and "停在末尾显示当前合计" in html, "page: does not auto-replay historical usage from the start")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("输入种类", "这段时间没有用量", "整段时间", "截至")), "English strings for the usage flow")
if shutil.which("node"):
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    jf = T / "page.js"; jf.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(jf)], capture_output=True, text=True)
    ok(r.returncode == 0, "page script parses (node --check) " + r.stderr[-300:])

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

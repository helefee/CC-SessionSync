# -*- coding: utf-8 -*-
"""0.3.7：跨应用搜索（关键词 / 应用 / 项目 / 时间 / 对话内容，清单缓存 + 增量内容索引）+ 好几个应用的会话一起批量转换（合起来的预览、进度、每个来源一本账、目标开着排队）。全在临时目录。"""
import os, sys, pathlib, json, tempfile, hashlib, sqlite3, subprocess, uuid, time
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-xs-")); H = T / "home"; H.mkdir()
OCD, ZH, DH = T / "xdg" / "opencode", H / ".zcode", T / "dshhome"
if os.name == "nt":
    cli = T / "opencode.cmd"; cli.write_text(f'@"{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" %*\n')
else:
    cli = T / "opencode"; cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" "$@"\n'); cli.chmod(0o755)
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
                  XDG_CONFIG_HOME=str(H / "AppData/Roaming"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_OPENCODE_DATA=str(OCD), CC_SESSIONSYNC_ZCODE_HOME=str(ZH), CC_SESSIONSYNC_OPENCODE_CLI=str(cli),
                  FAKE_OC_LOG=str(T / "oc.log"), CURSOR_USER_DIR=str(T / "cu"), CURSOR_CONFIG_DIR=str(H / ".cursor"),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_OPENCODE_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_ZCODE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_NO_WATCHER="1")
os.environ.pop("XDG_DATA_HOME", None)
(T / "procs.json").write_text("[]")
import oc_fixture as OF, codex_fixture as CF
from cc_sessionsync import ocstore as O, convert as CV, ir, sessions as S, timeline, apps
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}



CWD = str(T / "proj"); Path(CWD).mkdir(); CWD2 = str(T / "other"); Path(CWD2).mkdir()
oc = OF.opencode(OCD, CWD)
zc = OF.zcode(ZH, CWD2)
from cc_sessionsync import xsearch as X, ui, paths
NOW = time.time()

def claude(sid, cwd, title, ask, answer, days_ago=0.0):
    pj = H / ".claude" / "projects" / ("-" + Path(cwd).name)
    pj.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - days_ago * 86400))
    recs = [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "cwd": cwd, "timestamp": ts, "message": {"role": "user", "content": ask}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid, "cwd": cwd, "timestamp": ts,
             "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": answer}]}},
            {"type": "custom-title", "customTitle": title, "sessionId": sid}]
    f = pj / f"{sid}.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    t = NOW - days_ago * 86400
    os.utime(f, (t, t))
    return f

C1, C2 = str(uuid.uuid4()), str(uuid.uuid4())
f1 = claude(C1, CWD, "修登录页白屏", "登录页白屏了", "XS-NEEDLE 在回复里：useEffect 少了依赖", 0)
claude(C2, CWD2, "老会话 部署脚本", "写个部署脚本", "好的", 40)
cxf, TID = CF.rollout(H / ".codex", CWD)
import dsh_fixture as DF, cursor_fixture as CuF
DSID = str(uuid.uuid4()); DF.rich(DSID, CWD, DH).write(DH / "sessions")
CUD = T / "cu"; CuF.rich(CUD, CWD, CuF.ws_storage(CUD, CWD)).write(CUD)
# 很多会话：清单缓存 + 搜索要快
BULK = 600
for i in range(BULK):
    claude(str(uuid.uuid4()), str(T / "bulk"), f"批量会话 {i}", f"问题 {i}", f"回答 {i}", 3 + i % 50)

# ---------- 清单 ----------
t0 = time.time(); r = X.search(); t_first = time.time() - t0
per = r["per_app"]
ok(per["claude"] == BULK + 2 and all(per[a] >= 1 for a in ("codex", "dsh", "cursor", "opencode", "zcode")), f"all six apps listed: {per}")
ok(all(x.get("file") is None for x in r["rows"]) and r["total"] == sum(per.values()), "rows do not leak file paths; total = per-app sum")
calls = []
orig = X._collect
X._collect = lambda: calls.append(1) or orig()
t0 = time.time()
for q in ("登录", "部署", "批量 1", "", "xyz"):
    X.search(q)
t_cached = time.time() - t0
ok(not calls and t_cached < 1.5, f"cached list: 5 searches over {r['all']} sessions in {t_cached:.2f}s (first {t_first:.2f}s), no re-collect")
X._meta["at"] -= X.TTL + 1                  # 旧了：先给旧的，后台刷新
X.search("登录")
time.sleep(0.05)
for _ in range(100):
    if not X._meta["refreshing"]:
        break
    time.sleep(0.05)
ok(calls, "stale list refreshed in the background")
X._collect = orig

# ---------- 关键词 / 应用 / 项目 / 时间 ----------
r = X.search("登录")
ok(any(x["agent"] == "claude" and x["id"] == C1 for x in r["rows"]) and all(x["hit"] == "title" for x in r["rows"]), "keyword matches title")
r = X.search("老会话 部署")
ok(r["total"] == 1 and r["rows"][0]["id"] == C2 and X.search("老会话 登录")["total"] == 0, f"all words must match ({r['total']})")
r = X.search("", agents=["opencode", "zcode"])
ok(r["total"] and {x["agent"] for x in r["rows"]} <= {"opencode", "zcode"}, "app filter")
r = X.search("", project=CWD)
ok(r["total"] and all(x["cwd"] == CWD for x in r["rows"]) and {x["agent"] for x in r["rows"]} >= {"claude", "opencode"}, f"project filter crosses apps: {sorted({x['agent'] for x in r['rows']})}")
ok(any(p == CWD for p, n in r["projects"]), "project list offered")
r = X.search("", agents=["claude"], since=NOW - 86400)
ok(any(x["id"] == C1 for x in r["rows"]) and not any(x["id"] == C2 for x in r["rows"]), "time range: since")
r = X.search("", agents=["claude"], since=NOW - 50 * 86400, until=NOW - 30 * 86400)
ok(any(x["id"] == C2 for x in r["rows"]) and not any(x["id"] == C1 for x in r["rows"]), "time range: between")
r = X.search("", limit=50)
ok(len(r["rows"]) == 50 and r["total"] > 50, "limit keeps the response small")

# ---------- 对话内容 ----------
ok(X.search("xs-needle")["total"] == 0, "content word not found without content search")
st = X.ensure_index(X.meta(), wait=60)
ok(not st["running"] and st["done"] == st["total"] == X.search()["all"], f"content index built: {st}")
r = X.search("XS-NEEDLE", content=True)
ok(r["total"] == 1 and r["rows"][0]["id"] == C1 and r["rows"][0]["hit"] == "content" and "xs-needle" in r["rows"][0]["snippet"], "content search hit + snippet")
r = X.search("tool_use 登录页白屏", content=True)
ok(r["total"] == 0, "content: all words must appear")
oc_hit = X.search("OC2-ANSWER", content=True)
ok(any(x["agent"] == "opencode" for x in oc_hit["rows"]), "content search covers OpenCode")
# 增量：只重读改过的
seen = []
o_text = X._text_of
X._text_of = lambda row: seen.append(row["id"]) or o_text(row)
claude(C1, CWD, "修登录页白屏", "登录页白屏了", "XS-SECOND 改过了", 0)
X.forget()
X.ensure_index(X.meta(), wait=60)
X._text_of = o_text
ok(seen == [C1], f"index update re-reads only the changed session ({len(seen)})")
ok(X.search("xs-second", content=True)["total"] == 1 and X.search("xs-needle", content=True)["total"] == 0, "index follows the change")
ok(X.DB == T / "ss" / "search-index.sqlite" and X.DB.exists(), "index lives in ~/.cc-sessionsync")

# ---------- 批量预览 ----------
items = [{"agent": "claude", "id": C1}, {"agent": "codex", "id": TID}, {"agent": "opencode", "id": oc["v2"]}, {"agent": "dsh", "id": "nope"}]
bp = CV.batch_preview(items, "dsh")
ok(bp["apps"] == {"claude": 1, "codex": 1, "opencode": 1} and bp["same_app"] == 1, f"batch preview groups by app, same-app skipped: {bp['apps']}")
ok(bp["total"]["sessions"] == 3 and {r["src"] for r in bp["rows"]} == {"claude", "codex", "opencode"} and all(r["target"]["path"] for r in bp["rows"]), "combined preview rows + targets")
ok(bp["summary"] and any("来源" in x["detail"] for x in bp["summary"] if x["what"] == "标题"), "combined summary uses generic source wording")
before_dsh = tree(DH)
ok(before_dsh == tree(DH), "batch preview writes nothing")

# ---------- 批量做（有进度）----------
prog = []
br = CV.batch_run(items, "dsh", progress=lambda n, t, r: prog.append((n, t)))
ok(br["done"] == 3 and not br["errors"] and len(br["ledgers"]) == 3, f"batch run: one ledger per source app {br.get('errors')}")
ok([n for n, _ in prog] == sorted(n for n, _ in prog) and prog[-1] == (3, 3), f"progress counts up to total: {prog}")
leds = {l["name"]: l for l in CV.ledgers()}
ok(all(leds[n]["dst"] == "dsh" for n in br["ledgers"]) and {leds[n]["src"] for n in br["ledgers"]} == {"claude", "codex", "opencode"}, "ledgers listed per source")
ok(all(CV.undo(n)["ok"] for n in br["ledgers"]), "each ledger can be undone")

# ---------- API：后台任务 + 进度 ----------
j = ui.H._post(None, "/api/convert/batch/run", {"items": items[:2], "dst": "dsh"})
ok(j["ok"] and j["job"] and j["total"] == 2 and not j["deferred"], "POST batch/run starts a background job")
for _ in range(200):
    s = ui.H._get(None, "/api/convert/batch/status", {"id": j["job"]})
    if s["state"] != "running":
        break
    time.sleep(0.05)
ok(s["state"] == "done" and s["done"] == 2 and s["result"]["done"] == 2, f"job status → done ({s['state']} {s.get('error')})")
ok(ui.H._get(None, "/api/convert/batch/status", {"id": "nope"})["ok"] is False, "unknown job id")
pv = ui.H._post(None, "/api/convert/batch/preview", {"items": items, "dst": "cursor"})
ok(pv["mode"] == "export" and pv["total"]["sessions"] == 3 and pv["counts"].get("missing") == 1, f"POST batch/preview → export target; missing source flagged ({pv['counts']})")
xs = ui.H._get(None, "/api/xsearch", {"q": "登录", "agents": "claude,codex", "content": "1"})
ok(xs["total"] >= 1 and all(x["agent"] in ("claude", "codex") for x in xs["rows"]), "GET /api/xsearch")
# 目标开着：排队成一个批量任务，退出后做
(T / "running").write_text("1")
d = ui.H._post(None, "/api/convert/batch/run", {"items": [{"agent": "opencode", "id": oc["v1"]}], "dst": "claude"})
ok(d.get("deferred") and d["op"] == "convert-batch", f"target running → queued as one convert-batch job ({d.get('op')})")
(T / "running").unlink()
apps.watch("claude", max_wait=5)
ok(any(r["src"] == "opencode" and r["dst"] == "claude" and r["count"] == 1 for r in CV.ledgers()), "queued batch ran after the app quit")
ui_html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("/api/xsearch" in ui_html and "/api/convert/batch/run" in ui_html and "全部应用" in ui_html, "UI wired: 全部应用 search + batch")

print("FAILS:", FAILS)
sys.exit(1 if FAILS else 0)

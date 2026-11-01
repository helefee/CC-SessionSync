# -*- coding: utf-8 -*-
"""0.5.1：会话详情里的「Token 流转」（按记录里真实的每次调用用量回放）和「关于」页。临时目录，不碰真实数据，不调模型。
- 后端 tokenflow.flow：Claude（流式拆行去重、同名工具调两次算两次、工具结果 / isMeta 不算轮次、子代理按时间归到轮次）、Codex（token_count 差值、
  函数调用挂到下一次用量上、AGENTS.md / environment_context 不算轮次）、DeepSeek Harness（usage）；合计和「用量」页的库逐项对得上
- 前端 tfModel（用 node 跑）：粒子个数按单位换算、整段加起来正好等于 floor(合计 / 单位)；输出放大倍数是 1/2/5 序列
- /api/sessions/tokenflow、/api/about（版本 = __version__、作者 / 仓库 / 许可证和 LICENSE、pyproject 一致）、托盘「关于…」、顶栏按钮"""
import os, sys, pathlib, json, tempfile, time, re, shutil, subprocess, threading, urllib.request
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-tf-")); H = T / "home"; H.mkdir(); DH = T / "dsh"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
                  XDG_CONFIG_HOME=str(H / "AppData/Roaming"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCHER="1")
for k in ("CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_APP", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
import cc_sessionsync
from cc_sessionsync import tokenflow as TF, usage as U
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
W = lambda f, L: (f.parent.mkdir(parents=True, exist_ok=True), f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in L) + "\n", encoding="utf-8"))

# ---------- Claude Code：两轮，第一轮里工具循环 + 一个子代理 ----------
SID = "11111111-2222-4333-8444-555555555555"
def a(mid, ts, u, content=(), stop=None, model="claude-sonnet-4-5", rid=None, side=False):
    return {"type": "assistant", "uuid": mid + ts, "sessionId": SID, "cwd": "/w", "timestamp": ts, "requestId": rid or "req_" + mid, "isSidechain": side,
            "message": {"id": mid, "model": model, "role": "assistant", "content": list(content), "stop_reason": stop, "usage": u}}
def usr(ts, content, **kw):
    return dict({"type": "user", "uuid": "u" + ts, "sessionId": SID, "cwd": "/w", "timestamp": ts, "message": {"role": "user", "content": content}}, **kw)
UA = {"input_tokens": 1000, "output_tokens": 300, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 2000,
      "cache_creation": {"ephemeral_5m_input_tokens": 1500, "ephemeral_1h_input_tokens": 500}}
L = [usr("2026-10-20T08:00:00.000Z", "第一轮：查一下重复扣款"),
     # 流式写法：同一条回复拆成三行（同一个 message.id + requestId），usage 一行比一行全；两次 Read 是两次调用工具
     a("mA", "2026-10-20T08:00:05.000Z", dict(UA, output_tokens=5), [{"type": "text", "text": "看看"}]),
     a("mA", "2026-10-20T08:00:06.000Z", UA, [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
     a("mA", "2026-10-20T08:00:07.000Z", UA, [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}], stop="tool_use"),
     usr("2026-10-20T08:00:09.000Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}, {"type": "tool_result", "tool_use_id": "t2", "content": "y"}]),
     usr("2026-10-20T08:00:09.500Z", "<system-reminder>注入的</system-reminder>"),
     usr("2026-10-20T08:00:09.600Z", "meta", isMeta=True),
     a("mB", "2026-10-20T08:00:20.000Z", {"input_tokens": 10, "output_tokens": 800, "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 300},
       [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {}}, {"type": "tool_use", "id": "t4", "name": "Edit", "input": {}}], stop="tool_use"),
     usr("2026-10-20T08:00:30.000Z", [{"type": "tool_result", "tool_use_id": "t3", "content": "ok"}]),
     a("mC", "2026-10-20T08:00:40.000Z", {"input_tokens": 5, "output_tokens": 1200, "cache_read_input_tokens": 8300, "cache_creation_input_tokens": 0}, [], stop="end_turn"),
     usr("2026-10-20T09:00:00.000Z", [{"type": "text", "text": "第二轮：补单测"}]),
     a("mD", "2026-10-20T09:00:10.000Z", {"input_tokens": 400, "output_tokens": 100, "cache_read_input_tokens": 8300, "cache_creation_input_tokens": 0}, [], stop="end_turn",
       model="claude-opus-4-1")]
CF = H / ".claude/projects/-w" / f"{SID}.jsonl"; W(CF, L)
W(CF.parent / SID / "subagents" / "agent-abc123.jsonl",
  [a("mS", "2026-10-20T08:00:15.000Z", {"input_tokens": 700, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
     [{"type": "tool_use", "id": "s1", "name": "Grep", "input": {}}], stop="end_turn", model="claude-haiku-4-5-20251001", side=True)])
W(CF.parent / SID / "subagents" / "agent-..%2Fevil.jsonl", [])          # 名字不对的不读

d = TF.flow("claude", SID)
ok(d["ok"] and len(d["calls"]) == 5, f"claude: 5 calls (streamed lines deduped, subagent included) ({len(d.get('calls', []))})")
c = {x["id"].split(":")[1]: x for x in d["calls"]}
ok(sorted(c) == ["mA", "mB", "mC", "mD", "mS"], f"claude: call ids {sorted(c)}")
A_ = c["mA"]
ok((A_["inp"], A_["cr"], A_["cw"], A_["cw5"], A_["cw1h"], A_["out"]) == (1000, 5000, 2000, 1500, 500, 300), f"claude: fullest usage kept for streamed reply ({A_})")
ok(A_["prompt"] == 8000 and A_["tot"] == 8300, "claude: prompt = input + cache read + cache write; tot adds output")
ok(A_["tools"] == ["Read", "Read"] and A_["ntools"] == 2, f"claude: two Read calls counted twice (by tool_use id) ({A_['tools']})")
ok(c["mB"]["tools"] == ["Bash", "Edit"] and c["mC"]["tools"] == [], "claude: tool names per call")
ok([t["text"] for t in d["turns"]] == ["第一轮：查一下重复扣款", "第二轮：补单测"], f"claude: only real user prompts are turns ({d['turns']})")
ok([c[k]["turn"] for k in ("mA", "mS", "mB", "mC", "mD")] == [0, 0, 0, 0, 1], "claude: calls (and subagent) assigned to turns")
ok(c["mS"]["sub"] == "abc123" and c["mS"]["model"].startswith("claude-haiku") and d["subagents"] == 1, "claude: subagent call marked, bad file name skipped")
ok([x["id"].split(":")[1] for x in d["calls"]] == ["mA", "mS", "mB", "mC", "mD"] and [x["i"] for x in d["calls"]] == list(range(5)), "claude: calls sorted by time")
t = d["totals"]
ok((t["inp"], t["cr"], t["cw"], t["out"], t["calls"], t["tools"], t["sub_calls"]) == (2115, 29600, 2300, 2450, 5, 5, 1), f"claude: totals ({t})")
ok(abs(t["hit"] - 29600 / (2115 + 29600 + 2300)) < 1e-12, "claude: cache hit rate")
ok(all(x["cost"] and x["cost"] > 0 for x in d["calls"]) and abs(t["cost"] - sum(x["cost"] for x in d["calls"])) < 1e-12 and not t["cost_partial"], "claude: every call priced, cost = sum")
ok(d["models"][0] == "claude-sonnet-4-5" and set(d["models"]) == {"claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5-20251001"}, f"claude: models by tokens ({d['models']})")

# ---------- Codex：两轮，函数调用挂到下一次用量上 ----------
from codex_fixture import rollout
CXF, TID = rollout(H / ".codex", "/w", title="缓存", secret_marker="")
def ev(ts, p, typ="event_msg"): return {"timestamp": ts, "type": typ, "payload": p}
def tc(ts, tot, last): return ev(ts, {"type": "token_count", "info": {"total_token_usage": tot, "last_token_usage": last}})
tu = lambda i, c, o, r: {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}
cx = [ev("2026-10-15T08:00:10.000Z", {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c9"}, "response_item"),
      ev("2026-10-15T08:00:11.000Z", {"type": "web_search_call", "status": "completed"}, "response_item"),
      tc("2026-10-15T08:00:12.000Z", tu(10000, 4000, 900, 300), tu(10000, 4000, 900, 300)),
      ev("2026-10-15T08:05:00.000Z", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "再加个过期时间"}]}, "response_item"),
      ev("2026-10-15T08:05:05.000Z", {"type": "custom_tool_call", "name": "apply_patch", "input": "", "call_id": "c10"}, "response_item"),
      tc("2026-10-15T08:05:09.000Z", tu(25000, 16000, 1500, 400), tu(15000, 12000, 600, 100))]
with open(CXF, "a", encoding="utf-8") as fh: fh.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in cx))
d = TF.flow("codex", TID)
cs = d["calls"]
ok(d["ok"] and len(cs) == 2, f"codex: one call per token_count ({len(cs)})")
ok(cs and (cs[0]["inp"], cs[0]["cr"], cs[0]["out"], cs[0]["reasoning"]) == (6000, 4000, 900, 300), f"codex: first call fresh input = input - cached ({cs[:1]})")
ok(len(cs) > 1 and (cs[1]["inp"], cs[1]["cr"], cs[1]["out"], cs[1]["reasoning"]) == (3000, 12000, 600, 100), f"codex: second call from the delta ({cs[1:]})")
ok(cs and "exec_command" in cs[0]["tools"] and "web_search" in cs[0]["tools"] and cs[1]["tools"] == ["apply_patch"], f"codex: tool calls attached to the next usage ({[x['tools'] for x in cs]})")
ok([x["turn"] for x in cs] == [1, 2] and [t["text"] for t in d["turns"]] == ["登录页点了没反应，帮我看看", "再加个单测", "再加个过期时间"], f"codex: AGENTS.md / environment_context aren't turns ({d['turns']})")
ok(all(x["reasoning"] <= x["out"] for x in cs), "codex: reasoning is part of output (never more than it)")

# ---------- DeepSeek Harness ----------
import dsh_fixture as FX
DSID = "session-00000000-0000-4000-8000-0000000000aa"
fx = FX.Sess(DSID, "/w")
fx.add("turn/start", {}); fx.user("导出 CSV")
fx.assistant([{"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{}"}],
             usage={"inputTokens": 2100, "outputTokens": 900, "cacheReadTokens": 18000, "cacheWriteTokens": 0, "reasoningTokens": 300, "totalTokens": 21000})
fx.assistant([{"type": "text", "text": "好了"}], usage={"inputTokens": 300, "outputTokens": 200, "cacheReadTokens": 20000, "cacheWriteTokens": 0, "totalTokens": 20500})
fx.add("turn/end", {"reason": {"kind": "completed"}})
(DH / "profiles/desktop").mkdir(parents=True, exist_ok=True); fx.write(DH / "sessions")
d = TF.flow("dsh", DSID)
ok(d["ok"] and len(d["calls"]) == 2 and d["calls"][0]["tools"] == ["bash"] and d["calls"][1]["tools"] == [], f"dsh: 2 calls, tool names ({d.get('calls')})")
ok(d["calls"] and d["calls"][0]["out"] == 900 and d["calls"][0]["reasoning"] == 300 and len(d["turns"]) == 1, "dsh: usage + turns")

# ---------- 合计和「用量」页同一套口径：扫进库里逐项比 ----------
U.scan(full=True)
db = U._conn()
for ag, sid in (("claude", SID), ("codex", TID), ("dsh", DSID)):
    f = TF.flow(ag, sid)["totals"]
    r = db.execute("SELECT count(*), sum(inp), sum(cr), sum(cw5 + cw1h), sum(out), sum(tot), sum(c_in + c_cr + c_cw5 + c_cw1h + c_out) FROM calls WHERE sid = ?",
                   (sid,)).fetchone()
    ok(r[0] == f["calls"] and tuple(r[1:6]) == (f["inp"], f["cr"], f["cw"], f["out"], f["tot"]) and abs((r[6] or 0) - (f["cost"] or 0)) < 1e-9,
       f"{ag}: totals match the usage database ({tuple(r)} vs {f})")
db.close()

# ---------- 没有逐次用量的应用 / 不认识的 / 找不到 ----------
for ag in ("cursor", "opencode", "zcode"):
    d = TF.flow(ag, "x")
    ok(d["ok"] and d["calls"] == [] and d["reason"], f"{ag}: no per-call usage → empty with a reason")
ok(not TF.flow("nope", "x")["ok"] and not TF.flow("claude", "../../etc")["ok"] and not TF.flow("codex", "no-such-thread")["ok"], "unknown app / bad id → error")

# ---------- 前端 tfModel（node）：粒子按单位换算，整段之和 = floor(合计 / 单位) ----------
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
node = shutil.which("node")
if node:
    src = page[page.index("function tfNice("):page.index("// 第 i 次调用放到 t")]
    calls = TF.flow("claude", SID)["calls"]
    big = [{"inp": 3 + i % 7, "cr": 1000 * i + 17, "cw": (i * 37) % 900, "out": 50 + (i * 13) % 400, "reasoning": 0, "tot": 0, "cost": None, "ntools": i % 3,
            "turn": i // 10} for i in range(120)]
    js = src + "\nconst R = {};\nfor (const [n, d] of Object.entries(" + json.dumps({"claude": {"calls": calls}, "big": {"calls": big}}) + ")) {" + """
  const m = tfModel(d), sum = {cr: 0, cw: 0, inp: 0, out: 0}, tot = {cr: 0, cw: 0, inp: 0, out: 0};
  m.cnt.forEach((n, i) => { for (const k in sum) { sum[k] += n[k]; tot[k] += d.calls[i][k]; } });
  R[n] = {uIn: m.uIn, uOut: m.uOut, k: m.k, maxP: m.maxP, maxO: m.maxO, sum, tot, turnAt: m.turnAt, neg: m.cnt.some(n => Object.values(n).some(v => v < 0)),
          cum: m.total, maxIn: Math.max(...m.cnt.map(n => n.cr + n.cw + n.inp))};
}
console.log(JSON.stringify(R));"""
    (T / "tf.js").write_text(js, encoding="utf-8")
    p = subprocess.run([node, str(T / "tf.js")], capture_output=True, text=True, encoding="utf-8")
    ok(p.returncode == 0, "node: tfModel runs " + p.stderr[-300:])
    R = json.loads(p.stdout or "{}")
    for n, r in R.items():
        ok(all(r["sum"][k] == int(r["tot"][k] / (r["uOut"] if k == "out" else r["uIn"]) + 1e-7) for k in r["sum"]), f"{n}: particles sum to floor(total / unit) ({r['sum']} {r['tot']})")
        lead = lambda x: float(("%e" % x).split("e")[0])
        ok(lead(r["uIn"]) in (1, 2, 5) and lead(r["k"]) in (1, 2, 5) and abs(r["uOut"] * r["k"] - r["uIn"]) < 1e-9, f"{n}: units / magnification are 1-2-5 numbers ({r['uIn']}, ×{r['k']})")
        ok(r["k"] * r["maxO"] <= .6 * r["maxP"] + 1e-9 or r["k"] == 1, f"{n}: output side magnified at most to 60% of the widest input")
        ok(r["maxIn"] <= 240 + 3 and not r["neg"], f"{n}: at most ~240 input particles per call, none negative ({r['maxIn']})")
    ok(R["big"]["turnAt"] == list(range(0, 120, 10)) and R["claude"]["turnAt"] == [0, 4], f"turn markers ({R['claude']['turnAt']})")
    ok(R["claude"]["cum"]["tot"] == sum(x["tot"] for x in calls), "running total at the end = session total")
else:
    print("SKIP node not found")

# ---------- 页面接线 ----------
ok('id="tbAbout" data-p="about"' in page and 'id="pg-about"' in page and "R.about = async" in page and '"/api/about"' in page, "about: top-bar button, page and renderer")
ok('$$("#nav [data-p], #tbSet, #tbAbout")' in page and page.count("#tbSet, #tbAbout") == 2, "about: top-bar button highlighted like settings")
ok('id="stAbout"' in page, "settings links to about")
ok('id="svMode"' in page and 'store.set("ccss.svmode"' in page and "tfView(agent, id" in page, "session view: 对话 | Token 流转 toggle, remembered")
ok("/api/sessions/tokenflow?agent=" in page and "prefers-reduced-motion" in page, "token flow: fetches real usage, respects reduced motion")
ok("cdn" not in page.lower() and "<script src=" not in page, "no CDN / external script")

# ---------- 托盘 ----------
tray = (ROOT / "cc_sessionsync" / "tray.py").read_text(encoding="utf-8")
ok('(23, "关于…"' in tray and re.search(r'23: "about"\}\[cmd\]', tray) is not None, "tray: 关于… opens the about page")

# ---------- HTTP ----------
from cc_sessionsync import ui
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
tok = re.search(r'TOKEN = "([^"]+)"', urllib.request.urlopen(base + "/").read().decode()).group(1)
def get(path, token=tok):
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, headers={"X-Token": token}), timeout=30).read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code}
r = get(f"/api/sessions/tokenflow?agent=claude&id={SID}")
ok(r.get("ok") and len(r["calls"]) == 5 and r["totals"]["out"] == 2450, "api: /api/sessions/tokenflow")
ok(get(f"/api/sessions/tokenflow?agent=claude&id={SID}", "bad").get("_status") == 403 and get("/api/about", "bad").get("_status") == 403, "api: token required")
ab = get("/api/about")
lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
pyp = (ROOT / "pyproject.toml").read_text(encoding="utf-8") if (ROOT / "pyproject.toml").exists() else ""
ok(ab.get("version") == cc_sessionsync.__version__ and ab["product"] == "CC Session Sync", f"about: version = __version__ ({ab.get('version')})")
ok(ab["author"] == "helefee" and ab["author_url"] == "https://github.com/helefee" and ab["repo_url"] == "https://github.com/helefee/CC-SessionSync"
   and ab["issues_url"].endswith("/issues") and ab["releases_url"].endswith("/releases"), "about: author / repo / issues / releases")
ok(ab["license"] == "MIT" and lic.startswith("MIT License") and ab["copyright"] in lic, "about: license + copyright match LICENSE")
ok(not pyp or ("helefee/CC-SessionSync" in pyp or "CC-SessionSync" in pyp), "about: repo matches pyproject")
ok(all(u.startswith("https://github.com/") for u in (ab["author_url"], ab["repo_url"], ab["issues_url"], ab["releases_url"], ab["license_url"])),
   "about: every link is a GitHub page (desktop open_url only opens those)")
srv.shutdown()
shutil.rmtree(T, ignore_errors=True)
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

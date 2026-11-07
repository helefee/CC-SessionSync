# -*- coding: utf-8 -*-
"""0.5.1：Token 流转的「实时」。临时目录，不碰真实数据，不调模型。
- 往会话记录里追加行，tokenflow.live 只回新增 / 变过的调用，数字和整份 flow 一致；没变就回空
- 还没写完的半行先不算，写完才算；流式同一条回复拆成几行时只在 usage 变大时回一次
- Claude 子代理 agent-*.jsonl 新建 / 追加也跟得上；Codex rollout 追加；DeepSeek Harness（zstd 整份重读）追加
- 文件被截短 / 换了（新 inode）→ gen 变、reset=True 整份重发；gen 对不上也整份
- 长轮询：wait 秒内有追加马上回，没有就等满再回空；HTTP /api/sessions/tokenflow/live 和 /active 要 token
- active() 找到最近在动的会话（子代理也算主会话）；页面接线（实时按钮、等待新调用…、回到实时、减少动态效果）"""
import os, sys, pathlib, json, tempfile, time, re, shutil, threading, urllib.request
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-tfl-")); H = T / "home"; H.mkdir(); DH = T / "dsh"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
                  XDG_CONFIG_HOME=str(H / "AppData/Roaming"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCHER="1")
for k in ("CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_APP", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
from cc_sessionsync import tokenflow as TF
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
J = lambda x: json.dumps(x, ensure_ascii=False) + "\n"
def app(f, L, end=True):
    with open(f, "a", encoding="utf-8") as h: h.write("".join(J(x) for x in L) if end else "".join(J(x) for x in L).rstrip("\n"))
def ids(r): return [c["id"].split(":")[1] for c in r.get("calls", [])]

# ---------- Claude Code ----------
SID = "aaaaaaaa-2222-4333-8444-555555555555"
def a(mid, ts, u, content=(), stop=None, model="claude-sonnet-4-5", side=False):
    return {"type": "assistant", "uuid": mid + ts, "sessionId": SID, "cwd": "/w", "timestamp": ts, "requestId": "req_" + mid, "isSidechain": side,
            "message": {"id": mid, "model": model, "role": "assistant", "content": list(content), "stop_reason": stop, "usage": u}}
def usr(ts, text): return {"type": "user", "uuid": "u" + ts, "sessionId": SID, "cwd": "/w", "timestamp": ts, "message": {"role": "user", "content": text}}
U = lambda i, o, cr=0, cw=0: {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
CF = H / ".claude/projects/-w" / f"{SID}.jsonl"; CF.parent.mkdir(parents=True)
app(CF, [usr("2026-10-20T08:00:00.000Z", "第一轮"), a("m1", "2026-10-20T08:00:05.000Z", U(100, 50, 1000, 200), stop="end_turn")])
d = TF.flow("claude", SID)
ok(d["ok"] and ids(d) == ["m1"] and d["reset"] and d["gen"] and d["v"] >= 1 and len(d["turns"]) == 1, f"claude: flow → 1 call, gen/v ({ids(d)}, {d.get('gen')}, {d.get('v')})")
gen, v, nt = d["gen"], d["v"], len(d["turns"])
r = TF.live("claude", SID, gen, v, nt)
ok(r["ok"] and not r["reset"] and r["calls"] == [] and r["turns"] == [] and r["gen"] == gen, "claude: nothing appended → empty delta")
app(CF, [a("m2", "2026-10-20T08:00:20.000Z", U(10, 400, 1300, 0), [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], stop="tool_use")])
r = TF.live("claude", SID, gen, v, nt)
c2 = r["calls"][0] if r.get("calls") else {}
ok(ids(r) == ["m2"] and not r["reset"], f"claude: only the new call comes back ({ids(r)})")
ok((c2.get("inp"), c2.get("cr"), c2.get("cw"), c2.get("out"), c2.get("tools"), c2.get("turn")) == (10, 1300, 0, 400, ["Bash"], 0), f"claude: new call numbers ({c2})")
t = r["totals"]
ok((t["inp"], t["cr"], t["cw"], t["out"], t["calls"], t["tools"]) == (110, 2300, 200, 450, 2, 1), f"claude: running totals updated ({t})")
ok(abs(t["hit"] - 2300 / (110 + 2300 + 200)) < 1e-12 and t["cost"] and t["cost"] > d["totals"]["cost"], "claude: hit rate + cost updated")
v = r["v"]
# 流式：同一条回复拆成几行；半行先不算
app(CF, [usr("2026-10-20T09:00:00.000Z", "第二轮"), a("m3", "2026-10-20T09:00:05.000Z", U(5, 10, 2000, 0))])
part = J(a("m3", "2026-10-20T09:00:06.000Z", U(5, 900, 2000, 0), stop="end_turn"))
with open(CF, "a", encoding="utf-8") as h: h.write(part[:40])
r = TF.live("claude", SID, gen, v, nt)
ok(ids(r) == ["m3"] and r["calls"][0]["out"] == 10 and [x["text"] for x in r["turns"]] == ["第二轮"] and r["nt0"] == 1, f"claude: half-written line held back; new turn ({r.get('calls')}, {r.get('turns')})")
v, nt = r["v"], nt + len(r["turns"])
with open(CF, "a", encoding="utf-8") as h: h.write(part[40:])
r = TF.live("claude", SID, gen, v, nt)
ok(ids(r) == ["m3"] and r["calls"][0]["out"] == 900 and r["turns"] == [] and r["totals"]["calls"] == 3, f"claude: completed line → same call updated, not duplicated ({r.get('calls')})")
v = r["v"]
app(CF, [a("m3", "2026-10-20T09:00:07.000Z", U(5, 900, 2000, 0), stop="end_turn")])     # 一样的又写一遍：不算变
r = TF.live("claude", SID, gen, v, nt)
ok(r["calls"] == [] and r["totals"]["calls"] == 3, "claude: duplicate streamed line → no change sent")
v = r["v"]
# 子代理文件新建 + 追加
SA = CF.parent / SID / "subagents" / "agent-sub1.jsonl"; SA.parent.mkdir(parents=True)
app(SA, [a("s1", "2026-10-20T09:00:06.500Z", U(700, 40), stop="end_turn", model="claude-haiku-4-5-20251001", side=True)])
r = TF.live("claude", SID, gen, v, nt)
ok(ids(r) == ["s1"] and r["calls"][0]["sub"] == "sub1" and r["calls"][0]["turn"] == 1 and r["subagents"] == 1, f"claude: new subagent file picked up ({r.get('calls')})")
v = r["v"]
app(SA, [a("s2", "2026-10-20T09:00:06.800Z", U(30, 20), stop="end_turn", model="claude-haiku-4-5-20251001", side=True)])
r = TF.live("claude", SID, gen, v, nt)
ok(ids(r) == ["s2"] and r["totals"]["sub_calls"] == 2 and r["totals"]["calls"] == 5, f"claude: subagent append ({ids(r)})")
v = r["v"]
full = TF.flow("claude", SID)
ok(ids(full) == ["m1", "m2", "m3", "s1", "s2"] and full["totals"] == {**r["totals"], "cost": full["totals"]["cost"]}
   and abs(full["totals"]["cost"] - r["totals"]["cost"]) < 1e-12, "claude: incremental totals == fresh full read")
gen, v, nt = full["gen"], full["v"], len(full["turns"])
# 截短（重写成更短的）→ reset、新 gen
CF.write_text(J(usr("2026-10-20T10:00:00.000Z", "重来")) + J(a("n1", "2026-10-20T10:00:05.000Z", U(1, 2), stop="end_turn")), encoding="utf-8")
r = TF.live("claude", SID, gen, v, nt)
ok(r["reset"] and r["gen"] != gen and ids(r) == ["s1", "s2", "n1"] and [x["text"] for x in r["turns"]] == ["重来"], f"claude: truncated file → reset with new gen ({r.get('reason')}, {ids(r)})")
gen, v, nt = r["gen"], r["v"], len(r["turns"])
# 换了个文件（新 inode，同样长）→ reset
tmp = CF.with_suffix(".new"); tmp.write_bytes(CF.read_bytes().replace(b'"n1"', b'"n9"')); os.replace(tmp, CF)
r = TF.live("claude", SID, gen, v, nt)
ok(r["reset"] and r["gen"] != gen and "n9" in ids(r) and "n1" not in ids(r), f"claude: replaced file (rotation) → reset ({ids(r)})")
gen, v, nt = r["gen"], r["v"], len(r["turns"])
ok(TF.live("claude", SID, "stale-gen", 0, 0)["reset"], "claude: unknown gen → full reset")
# 长轮询：有追加马上回
threading.Timer(0.8, lambda: app(CF, [a("n2", "2026-10-20T10:01:00.000Z", U(3, 4), stop="end_turn")])).start()
t0 = time.time(); r = TF.live("claude", SID, gen, v, nt, wait=10); dt = time.time() - t0
ok(ids(r) == ["n2"] and 0.5 < dt < 4, f"claude: long-poll returns as soon as a line is appended ({dt:.2f}s, {ids(r)})")
v = r["v"]
t0 = time.time(); r = TF.live("claude", SID, gen, v, nt, wait=1.0); dt = time.time() - t0
ok(r["calls"] == [] and 0.9 < dt < 3, f"claude: long-poll times out empty ({dt:.2f}s)")
ok(r["age"] is not None and r["age"] < 60 and r["live_s"] == TF.LIVE_S, "claude: age / live_s let the page auto-enable live")

# ---------- Codex ----------
from codex_fixture import rollout
CXF, TID = rollout(H / ".codex", "/w", title="缓存", secret_marker="")
d = TF.flow("codex", TID); n0 = len(d["calls"])
def ev(ts, p, typ="event_msg"): return {"timestamp": ts, "type": typ, "payload": p}
tu = lambda i, c, o, r: {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}
tot = (d["totals"]["inp"] + d["totals"]["cr"], d["totals"]["cr"], d["totals"]["out"], d["totals"].get("reasoning", 0))
app(CXF, [ev("2026-10-15T09:00:00.000Z", {"type": "function_call", "name": "apply_patch", "arguments": "{}", "call_id": "cz"}, "response_item"),
          ev("2026-10-15T09:00:01.000Z", {"type": "token_count", "info": {"total_token_usage": tu(tot[0] + 5000, tot[1] + 4000, tot[2] + 700, tot[3] + 100),
                                                                           "last_token_usage": tu(5000, 4000, 700, 100)}})])
r = TF.live("codex", TID, d["gen"], d["v"], len(d["turns"]))
cc = r["calls"][0] if r.get("calls") else {}
ok(len(r.get("calls", [])) == 1 and not r["reset"] and r["totals"]["calls"] == n0 + 1, f"codex: only the appended token_count comes back ({len(r.get('calls', []))})")
ok((cc.get("inp"), cc.get("cr"), cc.get("out"), cc.get("reasoning"), (cc.get("tools") or [None])[-1]) == (1000, 4000, 700, 100, "apply_patch"), f"codex: numbers + pending tool ({cc})")
ok(r["totals"]["out"] == d["totals"]["out"] + 700, "codex: totals updated")

# ---------- DeepSeek Harness（zstd：变了才整份重读，没变的调用不重发）----------
import dsh_fixture as FX
DSID = "session-00000000-0000-4000-8000-0000000000bb"
fx = FX.Sess(DSID, "/w"); fx.add("turn/start", {}); fx.user("导出 CSV")
fx.assistant([{"type": "text", "text": "a"}], usage={"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 1000, "cacheWriteTokens": 0, "totalTokens": 1110})
(DH / "profiles/desktop").mkdir(parents=True, exist_ok=True); DF = fx.write(DH / "sessions")
d = TF.flow("dsh", DSID)
ok(d["ok"] and len(d["calls"]) == 1, "dsh: flow 1 call")
r = TF.live("dsh", DSID, d["gen"], d["v"], len(d["turns"]))
ok(r["calls"] == [] and not r["reset"], "dsh: unchanged file → empty")
fx.assistant([{"type": "text", "text": "b"}], usage={"inputTokens": 20, "outputTokens": 300, "cacheReadTokens": 1100, "cacheWriteTokens": 0, "totalTokens": 1420})
time.sleep(0.02); fx.write(DH / "sessions")
r = TF.live("dsh", DSID, d["gen"], d["v"], len(d["turns"]))
ok(len(r.get("calls", [])) == 1 and r["calls"][0]["out"] == 300 and r["totals"]["calls"] == 2 and not r["reset"], f"dsh: only the new call ({r.get('calls')})")

# ---------- active ----------
time.sleep(0.05); app(SA, [a("s3", "2026-10-20T10:02:00.000Z", U(1, 1), stop="end_turn", side=True)])
ac = TF.active()
ok(ac.get("agent") == "claude" and ac.get("id") == SID and ac["age"] < 60, f"active: newest = claude session via its subagent file ({ac})")
time.sleep(0.05); app(CXF, [ev("2026-10-15T09:10:00.000Z", {"type": "agent_message", "message": "x"})])
ac = TF.active()
ok(ac.get("agent") == "codex" and ac.get("id") == TID, f"active: codex rollout touched last ({ac})")

# ---------- HTTP ----------
from cc_sessionsync import ui
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); srv.daemon_threads = True; threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
tok = re.search(r'TOKEN = "([^"]+)"', urllib.request.urlopen(base + "/").read().decode()).group(1)
def get(path, token=tok):
    try: return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, headers={"X-Token": token}), timeout=30).read())
    except urllib.error.HTTPError as e: return {"_status": e.code}
f = get(f"/api/sessions/tokenflow?agent=claude&id={SID}")
r = get(f"/api/sessions/tokenflow/live?agent=claude&id={SID}&gen={f['gen']}&v={f['v']}&nt={len(f['turns'])}&wait=0")
ok(r.get("ok") and r["calls"] == [] and r["gen"] == f["gen"], "api: /live empty delta")
threading.Timer(0.6, lambda: app(CF, [a("n3", "2026-10-20T10:03:00.000Z", U(8, 9), stop="end_turn")])).start()
r = get(f"/api/sessions/tokenflow/live?agent=claude&id={SID}&gen={f['gen']}&v={f['v']}&nt={len(f['turns'])}&wait=8")
ok(ids(r) == ["n3"], f"api: /live long-poll gets the appended call ({ids(r)})")
ok(get(f"/api/sessions/tokenflow/live?agent=claude&id={SID}", "bad").get("_status") == 403 and get("/api/sessions/tokenflow/active", "bad").get("_status") == 403, "api: token required")
ok(get("/api/sessions/tokenflow/active").get("agent") == "claude", "api: /active")
srv.shutdown()

# ---------- 页面 ----------
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("/api/sessions/tokenflow/live?agent=" in page and "AbortController" in page and "wait=${TF_WAIT}" in page, "page: long-polls /live with abort")
ok('id="tfLive"' in page and "回到实时" in page and "等待新调用…" in page and "/api/sessions/tokenflow/active" in page, "page: session-flow live helpers still in the script (UI entry removed)")
ok('id="ssLive"' not in page, "page: sessions toolbar no longer has a Token 流转 live button")
ok(re.search(r"prefers-reduced-motion:reduce\)\{\.tflive", page) is not None and "@keyframes tfpulse" in page, "page: pulsing dot off under reduced motion")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("实时", "回到实时", "等待新调用…", "等待第一次调用…")), "i18n: live strings translated")
shutil.rmtree(T, ignore_errors=True)
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""0.2.4：用量口径（缓存写 5m/1h、流式快照去重、Codex 累计差值、长上下文档、fast 档、价格表、本地日历日、费用拆分、老库迁移、API 模式）。临时目录。"""
import os, sys, pathlib, json, tempfile, time, uuid, sqlite3, base64
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="ccss-usage-")); H = T / "home"; H.mkdir()
AD = H / "AppData/Roaming"; LA = H / "AppData/Local"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(AD), XDG_CONFIG_HOME=str(AD), LOCALAPPDATA=str(LA),
                  CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"))
for k in ("CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_APP", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
if hasattr(time, "tzset"):          # Windows 的 C 运行库不认 IANA 时区名：只在能 tzset 的系统上固定成马普托
    os.environ["TZ"] = "Africa/Maputo"; time.tzset()
(T / "procs.json").write_text("[]")
from cc_sessionsync import usage as U, claude_sync as C
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
near = lambda a, b: a is not None and abs(a - b) < 1e-9
PJ = H / ".claude/projects/-w"; PJ.mkdir(parents=True)
def cl(f, mid, rid, model, u, ts="2026-10-17T10:00:00.000Z", stop=None, sid="s1", cwd="/work/a", **kw):
    d = {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": ts, "message": {"id": mid, "model": model, "usage": u, "stop_reason": stop}}
    if rid is not None: d["requestId"] = rid
    d.update(kw)
    with open(f, "a", encoding="utf-8") as fh: fh.write(json.dumps(d) + "\n")
def row(i):
    c = sqlite3.connect(U.DB_PATH); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM calls WHERE id=?", (i,)).fetchone(); c.close(); return dict(r) if r else None
def rcost(r): return sum(r[k] or 0 for k in U.CPARTS)
F = PJ / "a.jsonl"
# 1 缓存写：拆分 5m / 1h（1h 按 2 倍输入价，5m 按 1.25 倍）；没拆分全按 5m；拆分比合计大 → 封顶
cl(F, "m1", "r1", "claude-opus-5-5", {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000,
                                     "cache_creation": {"ephemeral_5m_input_tokens": 400_000, "ephemeral_1h_input_tokens": 600_000}}, stop="end_turn")
cl(F, "m2", "r2", "claude-opus-5-5", {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000}, stop="end_turn")
cl(F, "m3", "r3", "claude-opus-5-5", {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 500_000,
                                     "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 900_000}}, stop="end_turn")
# 2 流式快照：同一 message.id + requestId 三行（最后一行最完整、有 stop_reason），完整的先出现、残缺的后出现也一样
for out, st in ((5, None), (40, None), (120, "tool_use")):
    cl(F, "m4", "r4", "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 1000}, stop=st)
cl(F, "m5", "r5", "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 300, "cache_read_input_tokens": 0}, stop="end_turn")
cl(F, "m5", "r5", "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 30, "cache_read_input_tokens": 0})
cl(F, "m6", "r6", "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 30})
cl(F, "m6", "r6", "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 30}, stop="end_turn")
# 没 requestId 的不丢；同一 message.id 有的行有 requestId 有的没有 → 一条
cl(F, "m7", None, "claude-sonnet-5-5", {"input_tokens": 7, "output_tokens": 7}, stop="end_turn")
cl(F, "m8", None, "claude-sonnet-5-5", {"input_tokens": 8, "output_tokens": 1})
cl(PJ / "b.jsonl", "m8", "r8", "claude-sonnet-5-5", {"input_tokens": 8, "output_tokens": 9}, stop="end_turn")
# 5 长上下文：Haiku 5.5 >100K 全部 ×5；Sonnet 4.5 >200K 输入 ×2 输出 ×1.5；4.6 以后不分档
cl(F, "m9", "r9", "claude-haiku-5-5", {"input_tokens": 1000, "cache_read_input_tokens": 120_000, "output_tokens": 1000}, stop="end_turn")
cl(F, "m10", "r10", "claude-sonnet-4-5-20250929[1m]", {"input_tokens": 250_000, "output_tokens": 1000}, stop="end_turn")
cl(F, "m11", "r11", "claude-opus-4-6-thinking", {"input_tokens": 250_000, "output_tokens": 1000}, stop="end_turn")
# 7 fast 档 / US 推理地区
cl(F, "m12", "r12", "claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0, "speed": "fast"}, stop="end_turn")
cl(F, "m13", "r13", "claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0, "inference_geo": "us"}, stop="end_turn")
# 8 没价格 / 模型名规范化
cl(F, "m14", "r14", "my-private-model", {"input_tokens": 100, "output_tokens": 1}, stop="end_turn")
cl(F, "m15", "r15", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", {"input_tokens": 100_000, "output_tokens": 0}, stop="end_turn")
# 9 本地日历日：UTC 23:30 = 马普托次日 01:30
cl(F, "m16", "r16", "claude-sonnet-5-5", {"input_tokens": 1, "output_tokens": 1}, ts="2026-10-16T23:30:00.000Z", stop="end_turn")
# Codex
X = H / ".codex/sessions/2026/10/17"; X.mkdir(parents=True)
def cx(f, typ, payload, ts):
    with open(f, "a", encoding="utf-8") as fh: fh.write(json.dumps({"type": typ, "timestamp": ts, "payload": payload}) + "\n")
def tc(f, tot, last, ts): cx(f, "event_msg", {"type": "token_count", "info": {"total_token_usage": tot, "last_token_usage": last}}, ts)
def u3(i, ca, o, cw=0, r=0): return {"input_tokens": i, "cached_input_tokens": ca, "cache_write_input_tokens": cw, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}
A = X / "rollout-2026-10-17T10-00-00-aaaa.jsonl"
cx(A, "session_meta", {"id": "th-a", "model_provider": "openai", "cwd": "/work/cx"}, "2026-10-17T10:00:00.000Z")
cx(A, "turn_context", {"model": "gpt-5.5"}, "2026-10-17T10:00:01.000Z")
# CodexBar 复现：累计 100/0/20 → 同一份快照再写一次 → 累计 110/0/23；只加 last 会得 210+，正确是 110/0/23 = 133
tc(A, u3(100, 0, 20), u3(100, 0, 20), "2026-10-17T10:00:02.000Z")
tc(A, u3(100, 0, 20), u3(100, 0, 20), "2026-10-17T10:00:03.000Z")
tc(A, u3(110, 0, 23), u3(100, 0, 20), "2026-10-17T10:00:04.000Z")      # last 是错的（又报了一遍上一条）：按累计差值 10/0/3
B = X / "rollout-2026-10-17T11-00-00-bbbb.jsonl"
cx(B, "session_meta", {"id": "th-b", "model_provider": "openai"}, "2026-10-17T11:00:00.000Z")
cx(B, "turn_context", {"model": "gpt-5.5", "service_tier": "priority"}, "2026-10-17T11:00:01.000Z")
# 4 口径：输入 1000 = 新 300 + 缓存读 600 + 缓存写 100；reasoning 20 在 output 50 里
tc(B, u3(1000, 600, 50, 100, 20), u3(1000, 600, 50, 100, 20), "2026-10-17T11:00:02.000Z")
# 累计变小（压缩）→ 退回 last，并以这次为新基线
tc(B, u3(400, 0, 10), u3(400, 0, 10), "2026-10-17T11:00:03.000Z")
tc(B, u3(700, 0, 30), u3(300, 0, 20), "2026-10-17T11:00:04.000Z")
# 长上下文：一次 300K 提示（>272K：输入 ×2、输出 ×1.5），priority = fast（gpt-5.5 ×2.5）
tc(B, u3(300_700, 0, 1030), u3(300_000, 0, 1000), "2026-10-17T11:00:05.000Z")
# 续接 / 分叉：新文件把 A 的 token_count 原样重放，然后自己的一轮（第一条累计里含着以前的 → 用 last）
Cf = X / "rollout-2026-10-17T12-00-00-cccc.jsonl"
cx(Cf, "session_meta", {"id": "th-c", "model_provider": "openai"}, "2026-10-17T12:00:00.000Z")
cx(Cf, "turn_context", {"model": "gpt-5.5"}, "2026-10-17T12:00:01.000Z")
tc(Cf, u3(100, 0, 20), u3(100, 0, 20), "2026-10-17T10:00:02.000Z")
tc(Cf, u3(110, 0, 23), u3(100, 0, 20), "2026-10-17T10:00:04.000Z")
D = X / "rollout-2026-10-17T13-00-00-dddd.jsonl"
cx(D, "session_meta", {"id": "th-d", "model_provider": "openai"}, "2026-10-17T13:00:00.000Z")
cx(D, "turn_context", {"model": "gpt-5.5", "service_tier": "flex"}, "2026-10-17T13:00:01.000Z")
tc(D, u3(50_000, 0, 500), u3(1000, 0, 50), "2026-10-17T13:00:02.000Z")       # 续接：累计里有以前的 49K，只算这次 1000/50
# 12 API 模式：Cowork 在 Claude-3p 里自带的 .claude/projects；以及在 API 模式会话列表里登记过的命令行会话
DEV = str(uuid.uuid4()); P3 = LA / "Claude-3p"; P3.mkdir(parents=True); (P3 / "ant-did").write_text(base64.b64encode(DEV.encode()).decode())
(P3 / "configLibrary").mkdir(); (P3 / "configLibrary/_meta.json").write_text('{"appliedId":"x1"}'); (P3 / "configLibrary/x1.json").write_text('{"inferenceProvider":"gateway","inferenceGatewayApiKey":"sk-NO"}')
CW = P3 / "local-agent-mode-sessions" / DEV[:8] / "00000000" / "s1" / ".claude/projects/session"; CW.mkdir(parents=True)
cl(CW / f"{uuid.uuid4()}.jsonl", "m20", "r20", "grok-4.6", {"input_tokens": 1000, "output_tokens": 10}, stop="end_turn", entrypoint="local-agent")
B3 = P3 / "claude-code-sessions" / DEV / C.PLACEHOLDER_ORG; B3.mkdir(parents=True)
(B3 / "local_x.json").write_text(json.dumps({"cliSessionId": "sid-3p", "priorCliSessionIds": ["sid-3p-old"], "title": "t"}))
cl(F, "m21", "r21", "claude-opus-4-6-thinking", {"input_tokens": 10, "output_tokens": 10}, sid="sid-3p-old", stop="end_turn")

r = U.scan(full=True)
m1, m2, m3 = row("cl:m1:r1"), row("cl:m2:r2"), row("cl:m3:r3")
ok(m1["cw5"] == 400_000 and m1["cw1h"] == 600_000 and near(m1["c_cw5"], 0.4 * 5) and near(m1["c_cw1h"], 0.6 * 8), "1 缓存写拆分：5m 按 1.25×、1h 按 2× 输入价")
ok(m2["cw5"] == 1_000_000 and m2["cw1h"] == 0 and near(rcost(m2), 5.0), "1 没拆分：全按 5m")
ok(m3["cw5"] + m3["cw1h"] == 500_000 and m3["cw1h"] == 500_000, "1 拆分比合计大：封顶到合计")
ok(row("cl:m4:r4")["out"] == 120 and row("cl:m4:r4")["stop"] == 1, "2 流式快照：留最完整（有 stop_reason）的那份")
ok(row("cl:m5:r5")["out"] == 300, "2 完整的先出现、残缺的后出现：还是留完整的")
ok(row("cl:m6:r6")["stop"] == 1, "2 一样大时留有 stop_reason 的")
ok(row("cl:m7:") is not None, "2 没 requestId 的行不丢（按 message.id）")
ok(row("cl:m8:") is None and row("cl:m8:r8") is not None, "2 同一 message.id 有 / 没 requestId：只算一条")
m9 = row("cl:m9:r9"); ok(near(rcost(m9), 5 * (1000 * 0.1 + 120_000 * 0.01 + 1000 * 0.5) / 1e6), "5 Haiku 5.5 提示 >100K：整条 ×5")
m10 = row("cl:m10:r10"); ok(near(m10["c_in"], 250_000 * 3 * 2 / 1e6) and near(m10["c_out"], 1000 * 15 * 1.5 / 1e6), "5 Sonnet 4.5 [1m] >200K：输入 ×2、输出 ×1.5")
m11 = row("cl:m11:r11"); ok(near(m11["c_in"], 250_000 * 5 / 1e6), "5 Opus 4.6（-thinking 规范化）不分档")
ok(near(rcost(row("cl:m12:r12")), 8.0) and row("cl:m12:r12")["tier"] == "fast", "7 Claude fast（speed=fast）：Opus 5.5 $8/M")
ok(near(rcost(row("cl:m13:r13")), 4.4), "7 inference_geo=us：×1.1")
m14 = row("cl:m14:r14"); ok(m14["priced"] == 0 and m14["c_in"] is None, "8 没价格：不算 0")
ok(near(rcost(row("cl:m15:r15")), 0.3), "8 Bedrock 模型名规范化后找到价格")
s = U.summary({})
t = s["totals"]
ok(t["partial"] and t["unpriced"] == 1, "8 总数里有没价格的部分：partial")
days = {d["d"]: d["n"] for d in s["days"]}
if hasattr(time, "tzset"):
    ok(days.get("2026-10-17", 0) >= 1 and not any(k == "2026-10-16" for k in days), f"9 UTC 23:30 记在马普托的次日（{sorted(days)}）")
cxr = {k: v for k, v in ((r_["id"], r_) for r_ in U.logs({"app": "codex"}, 0, 50)["rows"])}
A_rows = [v for v in cxr.values() if v["sid"] == "th-a"]
ok(sum(v["inp"] + v["cr"] + v["cw5"] for v in A_rows) == 110 and sum(v["out"] for v in A_rows) == 23
   and sum(v["inp"] + v["cr"] + v["cw5"] + v["out"] for v in A_rows) == 133, f"3 CodexBar 复现：110/0/23 = 133（{[(v['inp'], v['out']) for v in A_rows]}）")
ok(len(A_rows) == 2, "3 累计不变的那条算 0（不出行）")
ok(not [v for v in cxr.values() if v["sid"] == "th-c"], "3 分叉 / 续接重放的 token_count 不重复算")
B_rows = sorted([v for v in cxr.values() if v["sid"] == "th-b"], key=lambda v: v["ts"])
b0 = B_rows[0]
ok((b0["inp"], b0["cr"], b0["cw5"], b0["out"]) == (300, 600, 100, 50), f"4 Codex 口径：新输入 = 输入 − 缓存读 − 缓存写；reasoning 不另加（{b0['inp'], b0['cr'], b0['cw5'], b0['out']}）")
ok([v["inp"] for v in B_rows[1:3]] == [400, 300], f"3 累计变小 → 用 last，再按新基线算差值（{[v['inp'] for v in B_rows]}）")
b3 = B_rows[3]
ok(b3["prompt"] == 300_000 and near(b3["c_in"], 300_000 * 5 * 2 * 2.5 / 1e6) and near(b3["c_out"], 1000 * 30 * 1.5 * 2.5 / 1e6), "5 + 7 gpt-5.5 提示 >272K（输入 ×2、输出 ×1.5）+ priority（fast ×2.5）")
d0 = [v for v in cxr.values() if v["sid"] == "th-d"][0]
ok(d0["inp"] == 1000 and d0["out"] == 50 and near(d0["c_in"], 1000 * 5 * 0.5 / 1e6), "3 续接的第一条用 last；flex ×0.5")
m20 = [x for x in U.logs({}, 0, 100)["rows"] if x["model"] == "grok-4.6"]
ok(m20 and m20[0]["provider"] == "API 模式 · gateway" and m20[0]["sub"] == 0, "12 Claude-3p 里 Cowork 的日志也扫，算 API 模式")
ok(row("cl:m21:r21")["provider"] == "API 模式 · gateway" and row("cl:m21:r21")["sub"] == 0, "12 在 API 模式会话列表里登记过的会话：不算订阅")
ok("sk-NO" not in json.dumps(s), "12 不带出 key")
# 6 价格表：核对日期、缓存读比例
b = U.builtin()
ok(b["claude-opus-5-5"]["verified"] and abs(b["claude-opus-5-5"]["p"][2] / b["claude-opus-5-5"]["p"][0] - 0.05) < 1e-9
   and abs(b["claude-fable-5-1"]["p"][2] / b["claude-fable-5-1"]["p"][0] - 0.025) < 1e-9 and "claude-sonnet-5-5" in b, "6 价格表：Opus 5.5 缓存读 0.05×、Fable 5.1 0.025×、有 Sonnet 5.5、带核对日期")
# 10 费用拆分在日志里、按模型 / 项目分布
lg = U.logs({}, 0, 5)["rows"][0]
ok(all(k in lg for k in U.CPARTS) and "proj" in lg, "10 每条记录带费用拆分")
ok(any(p["key"] == "/work/a" for p in s["projects"]) and any(m["key"] == "claude-opus-5-5" for m in s["models"]) and s["series"][0].get("models"), "10 按项目 / 模型分布")
# 改价格 → 重算
U.set_price("my-private-model", {"inp": 1, "out": 1})
ok(row("cl:m14:r14")["priced"] == 1 and not U.summary({})["totals"]["partial"], "填了价格 → 已存的记录重算")
# 11 老库迁移
T2 = T / "old"; T2.mkdir()
old = sqlite3.connect(T2 / "usage.sqlite")
old.executescript("""CREATE TABLE calls(id TEXT PRIMARY KEY, app TEXT, src TEXT, ts REAL, sid TEXT, provider TEXT, sub INT, model TEXT, inp INT, cr INT, cw5 INT, cw1h INT, out INT, reasoning INT, tot INT, agent INT, fid TEXT);
CREATE TABLE files(fid TEXT PRIMARY KEY, path TEXT, kind TEXT, size INT, mtime REAL, off INT, state TEXT);""")
old.execute("INSERT INTO calls VALUES('cl:gone:r','claude-code','log',1760000000,'s','Claude 订阅',1,'claude-sonnet-5-5',100,0,0,0,10,0,110,0,'fid-gone')")
old.execute("INSERT INTO calls VALUES('cl:m4:r4','claude-code','log',1760000000,'s','Claude 订阅',1,'claude-sonnet-5-5',10,1000,0,0,5,0,1015,0,'x')")
old.execute("INSERT INTO calls VALUES('cx:th-a:0:120','codex','log',1760000000,'th-a','OpenAI API',0,'gpt-5.5',100,0,0,0,20,0,120,0,'fid-a')")
old.execute("INSERT INTO files VALUES('fid-gone',?, 'claude', 1, 1, 1, '{}')", (str(T / "deleted.jsonl"),))
old.execute("INSERT INTO files VALUES('fid-a',?, 'codex', 1, 1, 1, '{}')", (str(A),))
old.commit(); old.close()
U.DB_PATH = T2 / "usage.sqlite"
r2 = U.scan()
ok(r2.get("migrated") and r2["migrated"]["from"] == 1, "11 老库：升级时迁移 + 重读")
g = row("cl:gone:r")
ok(g and g["ver"] == U.SCHEMA and g["priced"] == 1 and near(rcost(g), (100 * 2 + 10 * 10) / 1e6), "11 文件已经删掉的老记录留着、按新价格表重算费用")
ok(row("cx:th-a:0:120") is None and sum(v["inp"] + v["out"] for v in U.logs({"app": "codex"}, 0, 50)["rows"] if v["sid"] == "th-a") == 133, "11 Codex 老键换成新键，不重复")
ok(row("cl:m4:r4")["out"] == 120, "11 重读后取最完整的快照")
ok(U.scan().get("migrated") is None, "11 只迁移一次")
print("FAILS", len(FAILS)); sys.exit(1 if FAILS else 0)

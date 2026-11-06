# -*- coding: utf-8 -*-
"""Antigravity：transcript 发现/去重、对话读取、真实 token 用量、导出-only 转换。临时目录。
对照 CC Switch session_manager/providers/agy.rs + session_usage_agy.rs。"""
import json, os, sys, tempfile, pathlib, sqlite3
T = pathlib.Path(tempfile.mkdtemp())
os.environ.update(HOME=str(T/"home"), USERPROFILE=str(T/"home"), XDG_CONFIG_HOME=str(T/"cfg"),
                  APPDATA=str(T/"cfg"), LOCALAPPDATA=str(T/"la"),
                  GEMINI_HOME=str(T/"gemini"), CC_SESSIONSYNC_HOME=str(T/"ss"),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T/"noprocs.json"))
(T/"noprocs.json").write_text("[]")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from cc_sessionsync import antigravity as AG, convert, usage as U
fails=[]
def P(c,m): print(("PASS " if c else "FAIL ")+m); c or fails.append(m)

cli = T/"gemini"/"antigravity-cli"
ide = T/"gemini"/"antigravity"
sid = "sess-demo-1"
logs = cli/"brain"/sid/".system_generated"/"logs"
logs.mkdir(parents=True)
lines = [
  {"step_index":0,"type":"USER_INPUT","content":"changed setting `Model Selection` from None to Gemini 3.8 Flash (High). ok","created_at":"2026-10-13T17:26:11Z"},
  {"step_index":1,"type":"USER_INPUT","content":"<USER_REQUEST>\nbuild the app\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>","created_at":"2026-10-13T17:26:12Z"},
  {"step_index":2,"type":"PLANNER_RESPONSE","content":"draft"},
  {"step_index":2,"type":"PLANNER_RESPONSE","status":"DONE","content":"Done building.","input_tokens":11830,"output_tokens":137,"cache_read_tokens":25,"cache_write_tokens":10,"created_at":"2026-10-13T17:26:20Z"},
  {"step_index":3,"type":"USER_INPUT","content":"<USER_REQUEST>\nadd tests\n</USER_REQUEST>","created_at":"2026-10-13T17:27:00Z"},
  {"step_index":4,"type":"PLANNER_RESPONSE","status":"DONE","content":"Tests added.","thinking":"plan","input_tokens":200,"output_tokens":50,"created_at":"2026-10-13T17:27:10Z"},
]
(logs/"transcript_full.jsonl").write_text("\n".join(json.dumps(x) for x in lines)+"\n", encoding="utf-8")
ide.mkdir(parents=True, exist_ok=True)
c=sqlite3.connect(str(ide/"conversation_summaries.db"))
c.execute("CREATE TABLE conversation_summaries(conversation_id TEXT,title TEXT,workspace_uris TEXT,preview TEXT,last_modified_time TEXT)")
c.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", ("sess-demo-1","CLI title","[]","p","2026-10-13T17:26:12Z"))
c.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", ("opaque-only","Index only","[]","p","2026-10-13T17:26:12Z"))
c.commit(); c.close()

found = AG.discover()
ids = [x["id"] for x in found]
P("sess-demo-1" in ids and "opaque-only" in ids, f"discover both transcript + index-only {ids}")
demo = next(x for x in found if x["id"]=="sess-demo-1")
P(bool(demo.get("transcript")) and not demo.get("opaque"), "transcript session readable")
opaque = next(x for x in found if x["id"]=="opaque-only")
P(opaque.get("opaque") and not opaque.get("transcript"), "index-only marked opaque")

rows = list(AG.records(pathlib.Path(demo["transcript"])))
P(len(rows)==5 and any(r.get("content")=="Done building." for r in rows), f"step_index dedup latest wins ({len(rows)})")
h, items = AG.read_session(demo)
texts = [i.get("text") for i in items]
P("build the app" in texts and "Done building." in texts, f"USER_REQUEST unwrap + assistant text {texts}")
us = list(AG.iter_usage(demo))
P(len(us)==2 and us[0]["inp"]==11830 and us[0]["cr"]==25 and us[0]["cw"]==10 and us[0]["out"]==137, f"usage from PLANNER_RESPONSE {us}")
P(us[0]["model"]=="gemini-3.8-flash-high" and us[0]["display"].startswith("Gemini"), f"model from settings change {us[0]}")

AG.refresh()
srcs = convert.sources("antigravity")
P(any(r["id"]=="sess-demo-1" for r in srcs), "convert sources lists session")
mat = convert.matrix()
P(mat["targets"]["antigravity"]["mode"]=="export", "antigravity is export-only target")
plan = convert.plan("antigravity", ["sess-demo-1"], "claude")
P(bool(plan.get("ok") and plan.get("rows")), f"plan antigravity→claude {plan.get('counts')}")
r = convert.run("antigravity", ["sess-demo-1"], "claude", dry=True)
P(bool(r.get("ok")), f"dry convert ok={r.get('ok')}")

U.scan(full=True)
c = U._conn()
n = c.execute("SELECT count(*), coalesce(sum(inp),0), coalesce(sum(out),0) FROM calls WHERE app='antigravity'").fetchone()
c.close()
P(n[0]>=2 and n[1]==12030 and n[2]==187, f"usage DB has antigravity calls {n}")
P(not list(AG.iter_usage(opaque)), "opaque yields no usage")

# 会话列表分组字段齐全（缺 current 会把整页列表拖垮）
from cc_sessionsync import sessions as SS
lv = SS.list_all("antigravity")
P(all("current" in g and "tags" in g and "folders" in g for g in lv.get("groups") or []), "account_groups fields for AG")
P(any(x.get("agent")=="antigravity" for x in lv.get("items") or []), "list_all includes antigravity session")
from cc_sessionsync import sessions as SSread
rr = SSread.read("antigravity", "sess-demo-1")
P(rr.get("ok") and len(rr.get("entries") or []) >= 2, f"sessions.read antigravity entries {len(rr.get('entries') or [])}")
print("FAILS", fails)
sys.exit(1 if fails else 0)

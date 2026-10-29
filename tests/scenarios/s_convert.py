# -*- coding: utf-8 -*-
"""0.2.7：OpenCode / ZCode 读取 + 任意 → 任意的会话转换。全在临时目录，不碰真实数据，不调模型。
读：OpenCode 1.x（session/message/part）和 2.x（session_v2/session_message）同库、WAL 里没合并的也读得到、原库一个字节不变、
凭据表不碰；ZCode（cli/db/db.sqlite + v2/tasks-index.sqlite）：提醒 / 通知不算对话、删了的不列、改过的标题用桌面端的、子任务标出来。
转：方向表（原生 / 导出）、演练不写、每个原生目标真写一次（Claude Code / Codex / dsh / OpenCode（假命令行））、导出上下文文件 + 粘贴提示、
再做一次是 same、来源变了是 changed（again 再导一份）、转回原来的应用是 loop、账本 + 撤回（改过的不删）；会话管理 / 备份 / 时间线里有它们。"""
import os, sys, pathlib, json, tempfile, hashlib, sqlite3, subprocess, uuid, time
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-conv-")); H = T / "home"; H.mkdir()
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

CWD = str(T / "proj"); Path(CWD).mkdir()
oc = OF.opencode(OCD, CWD)
zc = OF.zcode(ZH, CWD)
# 应用「开着」：-wal 里留一个没合并的新会话
hold = sqlite3.connect(str(OCD / "opencode.db")); hold.execute("pragma wal_autocheckpoint=0")
WAL_SID = "ses_0000walonly00aaaaaaaaaaaaaa"
hold.execute("insert into session_v2 (id, project_id, slug, directory, title, version, time_created, time_updated) values (?, 'global', 's', ?, 'WAL 里的', '2.0.32', 1, 2)", (WAL_SID, CWD))
hold.execute("insert into session_message values ('msg_w1', ?, 'user', 0, 1, 1, ?)", (WAL_SID, json.dumps({"text": "WAL-MARK"})))
hold.execute("insert into session_message values ('msg_w2', ?, 'assistant', 1, 2, 2, ?)", (WAL_SID, json.dumps({"content": [{"type": "text", "text": "好"}]})))
hold.commit()
ok((OCD / "opencode.db-wal").exists() and (OCD / "opencode.db-wal").stat().st_size > 0, "fixture: opencode db has a non-empty -wal")
before_oc, before_zc = tree(OCD), tree(ZH)

# ---------- OpenCode 读 ----------
d = O.detect("opencode")
ok(d["db_exists"] and d["by_source"] == {"opencode2": 3, "opencode1": 1} and d["cli"] == str(cli), f"opencode detect: {d['by_source']} cli")
rows = {r["id"]: r for r in O.refresh("opencode")}
ok(WAL_SID in rows and rows[WAL_SID]["msgs"] == 2, "opencode: uncheckpointed WAL rows are read (via temp copy)")
r1, r2 = rows[oc["v1"]], rows[oc["v2"]]
ok(r1["title"] == "OC1 修复构建" and r1["msgs"] == 2 and r1["tools"] == 1 and r1["cwd"] == CWD, "opencode 1.x: title / msgs / tools / cwd")
ok(r2["title"] == "OC2 写单测" and r2["msgs"] == 2 and r2["tools"] == 1 and r2["model"] == "opencode/deepseek-v4.1-flash", "opencode 2.x: title / msgs / tools / model")
ok(rows[oc["v2_sub"]]["parent"] == oc["v2"], "opencode 2.x: subagent session linked to parent")
h1, it1 = ir.load(Path(r1["file"]).read_bytes())
txt1 = json.dumps(it1, ensure_ascii=False)
ok("OC1-MARK" in txt1 and "system-reminder" not in txt1 and it1[0].get("images") == 1, "opencode 1.x: synthetic part skipped, image counted")
ok(any(i["kind"] == "thinking" and "OC1-THINK" in i["text"] for i in it1) and any(i["kind"] == "tool" and "OC1-TOOL-OUT" in i["output"] for i in it1), "opencode 1.x: reasoning + tool output")
h2, it2 = ir.load(Path(r2["file"]).read_bytes())
ok([i["kind"] for i in it2] == ["text", "thinking", "tool", "text"] and "OC2-TOOL-OUT" in it2[2]["output"] and it2[0]["images"] == 1, "opencode 2.x: items (idle / error messages skipped)")
mir = b"".join(p.read_bytes() for p in (T / "ss" / "mirror").rglob("*.jsonl"))
ok(OF.SECRET.encode() not in mir, "credential / account tables never read into mirrors")
ok(tree(OCD) == before_oc, "opencode data dir byte-identical after reading (db, -wal, -shm)")
ok(not list(Path(tempfile.gettempdir()).glob("ccss-oc-*")), "temp copies removed")

# ---------- ZCode 读 ----------
z = {r["id"]: r for r in O.refresh("zcode")}
ok(zc["gone"] not in z and len(z) == 3, "zcode: deleted session not listed")
ok(z[zc["renamed"]]["title"] == "用户改的名字", "zcode: title overridden in desktop tasks index wins")
ok(z[zc["sub"]]["subagent"] and z[zc["sub"]]["parent"] == zc["main"], "zcode: subagent_child flagged")
hz, iz = ir.load(Path(z[zc["main"]]["file"]).read_bytes())
tz = json.dumps(iz, ensure_ascii=False)
ok("ZC-MARK" in tz and "ZC-ANSWER" in tz and "ZC-REMINDER" not in tz and "ZC-NOTIFY" not in tz, "zcode: reminders / notifications are not conversation")
ok(any(i["kind"] == "tool" and i["tool"] == "Bash" and "ZC-TOOL-OUT" in i["output"] for i in iz) and z[zc["main"]]["msgs"] == 2, "zcode: tool + msgs")
ok(any(i.get("model") == "glm/glm-5.3" for i in iz), "zcode: providerId/modelId → model")
ok(tree(ZH) == before_zc, "zcode data byte-identical after reading")
dz = O.detect("zcode")
ok(dz["sessions"] == 3 and dz["stat"]["tasks"] == 2 and dz["stat"]["tasks_matched"] == 2 and "cli" not in dz, "zcode detect: counts + tasks matched")

# ---------- 会话管理 ----------
la = S.list_all()
ags = {}
for x in la["items"]:
    ags[x["agent"]] = ags.get(x["agent"], 0) + 1
ok(ags.get("opencode") == 4 and ags.get("zcode") == 3, f"sessions list includes opencode/zcode: {ags}")
grp = [g for g in S.account_groups(la["items"]) if g["key"].startswith(("opencode", "zcode"))]
ok(any(g["key"].startswith("opencode") for g in grp) and any(g["key"].startswith("zcode") for g in grp), f"grouped by app+project: {[g['key'] for g in grp]}")
rd = S.read("opencode", oc["v2"])
ok(rd["ok"] and [e["role"] for e in rd["entries"]] == ["user", "tool", "assistant"], "sessions.read opencode (thinking hidden)")
ok(S.read("zcode", zc["main"])["ok"], "sessions.read zcode")
ft = S.fulltext("OC2-ANSWER")
ok(any(h["agent"] == "opencode" for h in ft["hits"]), "fulltext finds opencode content")

# ---------- 方向表 ----------
m = CV.matrix()
ok(m["targets"]["opencode"]["mode"] == "native" and m["targets"]["zcode"]["mode"] == "export" and m["targets"]["cursor"]["mode"] == "export"
   and all(m["targets"][a]["mode"] == "native" for a in ("claude", "codex", "dsh")), "matrix: native claude/codex/dsh/opencode, export cursor/zcode")
ok(m["cells"]["zcode"]["zcode"] == "same" and m["cells"]["zcode"]["opencode"] == "native", "matrix cells")

# ---------- 来源：Claude / Codex ----------
CSID = str(uuid.uuid4())
pj = H / ".claude" / "projects" / "-proj"; pj.mkdir(parents=True)
recs = [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:00.000Z",
         "message": {"role": "user", "content": "CC-MARK 加个暗色模式"}},
        {"type": "user", "uuid": "m1", "isMeta": True, "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:00.000Z", "message": {"role": "user", "content": "<command-name>/x</command-name>"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "thinking", "thinking": "CC-THINK"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:02.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "CC-TOOL-OUT"}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:03.000Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "CC-ANSWER 加好了"}]}},
        {"type": "custom-title", "customTitle": "暗色模式", "sessionId": CSID}]
CF_ = pj / f"{CSID}.jsonl"; CF_.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
hc, ic = CV.load_ir("claude", CSID)
ok(hc["title"] == "暗色模式" and [i["kind"] for i in ic] == ["text", "thinking", "tool", "text"] and ic[2]["output"] == "CC-TOOL-OUT", "claude → IR (meta skipped, tool_result attached)")
cxf, TID = CF.rollout(H / ".codex", CWD)
hx, ix = CV.load_ir("codex", TID)
ok(any(i["kind"] == "tool" for i in ix) and any("登录页" in (i.get("text") or "") for i in ix) and hx["cwd"] == CWD, "codex → IR")
src_before = {"claude": CF_.read_bytes(), "codex": cxf.read_bytes()}

# ---------- 演练 ----------
p = CV.run("opencode", [oc["v2"]], "claude", dry=True)
ok(p["dry"] and p["rows"][0]["state"] == "new" and not list((H / ".claude" / "projects").glob("*/*.jsonl"))[1:], "dry-run opencode→claude writes nothing")

# ---------- 原生：→ Claude Code ----------
r = CV.run("opencode", [oc["v2"]], "claude", dry=False)
ok(r["done"] == 1 and not r["errors"], f"opencode→claude done {r.get('errors')}")
new_cc = r["rows"][0]["target_id"]
jf = Path(r["rows"][0]["file"])
recs2 = [json.loads(l) for l in jf.read_text(encoding="utf-8").splitlines()]
body = json.dumps(recs2, ensure_ascii=False)
ok("OC2-MARK" in body and "OC2-ANSWER" in body and "OC2-TOOL-OUT" in body and "OC2-THINK" not in body, "claude record: text + tool summary, thinking dropped")
ok(recs2[-1]["customTitle"] == "（OpenCode）OC2 写单测" and all(x.get("sessionId") == new_cc for x in recs2), "claude record: title prefix, session id")
roles = [x["type"] for x in recs2 if x["type"] in ("user", "assistant")]
ok(all(a != b for a, b in zip(roles, roles[1:])) and roles[0] == "user" and roles[-1] == "assistant", "claude record: alternating turns")
ok(CV.plan("opencode", [oc["v2"]], "claude")["rows"][0]["state"] == "same", "again → same")
# 回去：这份 Claude 会话 → OpenCode 是 loop
pl = CV.plan("claude", [new_cc], "opencode")
ok(pl["rows"][0]["state"] == "loop" and pl["rows"][0]["origin"] == "opencode", "claude session that came from opencode → opencode is a loop")
ok(CV.plan("claude", [new_cc], "codex")["rows"][0]["state"] == "new", "…but → codex is fine")

# ---------- 原生：→ Codex ----------
r = CV.run("zcode", [zc["main"]], "codex", dry=False)
ok(r["done"] == 1, f"zcode→codex {r.get('errors')}")
rf = Path(r["rows"][0]["file"]); tid = r["rows"][0]["target_id"]
L = [json.loads(l) for l in rf.read_text(encoding="utf-8").splitlines()]
ok(L[0]["type"] == "session_meta" and L[0]["payload"]["id"] == tid and L[0]["payload"]["cwd"] == CWD and rf.name.endswith(tid + ".jsonl"), "codex rollout meta / name")
ok(any(x["type"] == "response_item" and "ZC-ANSWER" in json.dumps(x, ensure_ascii=False) for x in L), "codex rollout has the conversation")
idx = (H / ".codex" / "session_index.jsonl").read_text(encoding="utf-8")
ok(f'"{tid}"' in idx and "（ZCode）ZC 修登录" in idx, "codex title appended to session_index.jsonl")
cs = {x["id"]: x for x in S.codex_sessions()}
ok(tid in cs and cs[tid]["title"] == "（ZCode）ZC 修登录", "new codex thread listed in sessions")
hb, ib = CV.load_ir("codex", tid)
ok(any("ZC-MARK" in (i.get("text") or "") for i in ib), "codex writer output reads back through the codex reader")

# ---------- 原生：→ dsh ----------
r = CV.run("claude", [CSID], "dsh", dry=False)
ok(r["done"] == 1, f"claude→dsh {r.get('errors')}")
from cc_sessionsync import dsh
df = Path(r["rows"][0]["file"]); did = r["rows"][0]["target_id"]
v = dsh.validate_bytes(df.read_bytes(), "zstd" if df.name.endswith(".zstd") else "none")
ok(v["ok"], f"dsh session valid {v['errors'][:2]}")
ev = json.dumps(dsh.load(df)["events"], ensure_ascii=False)
ok("CC-MARK" in ev and "CC-TOOL-OUT" in ev and "（Claude Code）暗色模式" in ev and "从 Claude Code 导入" in ev, "dsh session: content + title + one intro")
ok(CV.plan("dsh", [did], "claude")["rows"][0]["state"] == "loop", "dsh session from claude → claude is a loop")

# ---------- 原生：→ OpenCode（假命令行） ----------
r = CV.run("codex", [TID], "opencode", dry=False)
ok(r["done"] == 1, f"codex→opencode {r.get('errors')}")
oid = r["rows"][0]["target_id"]
lg = [json.loads(l) for l in (T / "oc.log").read_text().splitlines()]
imp = [x for x in lg if x["args"][:2] == ["session", "import"]][-1]
ok(imp["args"][:3] == ["session", "import", "--standalone"] and "--directory" in imp["args"] and imp["cwd"] == CWD, "opencode CLI: session import --standalone --directory <cwd>")
ok(lg[-1]["args"][:3] == ["session", "export", "--standalone"] and r["rows"][0]["check"]["ok"], f"after import: checked with `opencode session export` {r['rows'][0].get('check')}")
orow = {x["id"]: x for x in O.refresh("opencode")}
ok(oid in orow and orow[oid]["title"].startswith("（Codex）") and orow[oid]["cwd"] == CWD, "imported session listed in OpenCode (as read back from its db)")
ok(r["rows"][0]["resume"].endswith(f"opencode --session {oid}"), "resume command")
ok(CV.plan("opencode", [oid], "codex")["rows"][0]["state"] == "loop", "opencode session from codex → codex is a loop")
# OpenCode 开着 → 要先关
(T / "ocprocs.json").write_text(json.dumps([{"pid": 9, "name": "OpenCode.exe"}])); os.environ["CC_SESSIONSYNC_FAKE_OPENCODE_PROCS"] = str(T / "ocprocs.json")
(T / "running").write_text("1")
ok(CV.needs_close("opencode") == "opencode" and CV.needs_close("zcode") is None and CV.needs_close("cursor") is None, "needs_close: opencode when running, export targets never")
(T / "running").unlink(); os.environ["CC_SESSIONSYNC_FAKE_OPENCODE_PROCS"] = str(T / "procs.json")
# 1.x 命令行
os.environ["FAKE_OC_MAJOR"] = "1"
r = CV.run("claude", [CSID], "opencode", dry=False)
os.environ.pop("FAKE_OC_MAJOR")
lg1 = [json.loads(l)["args"] for l in (T / "oc.log").read_text().splitlines()]
ok(r["done"] == 1 and lg1[-2][0] == "import" and lg1[-1][0] == "export" and r["rows"][0]["check"]["ok"], f"opencode 1.x CLI: `opencode import`, checked with `opencode export` {r.get('errors')} {r['rows'][0].get('check')}")
o1 = r["rows"][0]["target_id"]
ok(any(x["id"] == o1 and x["source"] == "opencode1" and x["msgs"] >= 2 for x in O.refresh("opencode")), "1.x import read back")

# ---------- 导出：→ Cursor / ZCode ----------
r = CV.run("opencode", [oc["v1"]], "zcode", dry=False)
ok(r["mode"] == "export" and r["done"] == 1 and "ZCode" in r["paste"], "→ zcode is an export with a paste prompt")
md = Path(r["rows"][0]["file"]).read_text(encoding="utf-8")
ok("OC1-MARK" in md and "OC1-ANSWER" in md and "〔工具〕" in md and "OC1-THINK" not in md, "context md: conversation + tool one-liners, no thinking")
ok(tree(ZH) == before_zc, "zcode store untouched by export")
r = CV.run("zcode", [zc["main"]], "cursor", dry=False, project=True)
ok(r["done"] == 1 and Path(r["rows"][0]["file"]).parent == Path(CWD) / ".cc-sessionsync" and "@" in r["paste"], "→ cursor export into project folder")
r = CV.run("claude", [CSID], "codex", dry=False, export=True)
ok(r["mode"] == "export" and Path(r["rows"][0]["file"]).suffix == ".md", "export=True forces a context file for a native target")

# ---------- 来源变了 ----------
# （同一个进程里开关库文件会丢掉 POSIX 锁，hold 那个连接留着的 -wal 早被别的连接合并掉了，所以这里新开一个连接写）
c2 = sqlite3.connect(str(OCD / "opencode.db")); c2.execute("insert into session_message values ('msg_x9', ?, 'user', 9, 9, 9, ?)", (oc["v2"], json.dumps({"text": "再加一个测试"}))); c2.commit(); c2.close()
pl = CV.plan("opencode", [oc["v2"]], "claude")
ok(pl["rows"][0]["state"] == "changed", "source got new messages → changed")
ok(CV.plan("opencode", [oc["v2"]], "claude", again=True)["rows"][0]["state"] == "new", "again → new copy")

# ---------- 撤回 ----------
leds = CV.ledgers()
ok(len(leds) >= 7 and leds[0]["count"] == 1, f"ledgers listed ({len(leds)})")
by = {l["what"]: l for l in leds}
u = CV.undo(by["opencode-to-claude"]["name"])
ok(u["ok"] and u["removed"] == 1 and not jf.exists(), "undo opencode→claude removes the new session")
ok(CV.plan("opencode", [oc["v2"]], "claude")["rows"][0]["state"] == "new", "after undo the map entry is gone")
with open(rf, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"timestamp": "x", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "接着聊"}]}}) + "\n")
u = CV.undo(by["zcode-to-codex"]["name"])
ok(u["kept"] and rf.exists(), "undo keeps a codex thread you continued")
u = CV.undo(by["codex-to-opencode"]["name"])
ok(u["ok"] and u["removed"] == 1 and oid not in {x["id"] for x in O.refresh("opencode", force=True)}, "undo codex→opencode deletes via `opencode session delete`")
ok(json.loads((T / "oc.log").read_text().splitlines()[-1])["args"][:2] == ["session", "delete"], "…using the CLI")
u = CV.undo(by["claude-to-dsh"]["name"])
ok(u["ok"] and not df.exists(), "undo claude→dsh removes the session dir")
ok(CV.undo(by["claude-to-dsh"]["name"])["ok"] is False, "undo twice refused")
ok(CF_.read_bytes() == src_before["claude"] and cxf.read_bytes() == src_before["codex"], "sources (claude / codex) never modified")

# ---------- 备份 / 时间线 ----------
os.environ.pop("CC_SESSIONSYNC_NO_BACKUP", None)
b = S.backup(full=True)
snaps = S.snapshots("opencode") + S.snapshots("zcode")
ok(len({(s["agent"], s["id"]) for s in snaps}) >= 6, f"backups include opencode/zcode ({len(snaps)})")
tl = timeline.sessions() if hasattr(timeline, "sessions") else None
gr = timeline.growth() if hasattr(timeline, "growth") else None
ok(gr is None or any(k in json.dumps(gr) for k in ("opencode", "zcode")), "timeline growth knows opencode/zcode")
sn = [x for x in S.snapshots("zcode") if x.get("sid", x.get("id")) == zc["main"] or x.get("sid") == zc["main"]]
if sn:
    rr = timeline.restore([sn[0]["id"]], mode="copy", desktop=False)
    ok(rr["ok"] and rr["made"] and Path(rr["made"][0]["file"] if "file" in rr["made"][0] else rr["made"][0].get("path", "")).exists(), f"timeline: zcode snapshot → new Claude Code session {rr.get('errors')}")
    ok(timeline.restore([sn[0]["id"]], mode="replace")["ok"] is False, "timeline: replace refused for zcode")
else:
    ok(False, "zcode snapshot found")
hold.close()
print("FAILS", len(FAILS)); sys.exit(1 if FAILS else 0)

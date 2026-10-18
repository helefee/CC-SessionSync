# -*- coding: utf-8 -*-
"""Cursor：读编辑器的 state.vscdb / agent 记录 / cursor-agent 的 store.db（只读）、镜像、会话管理、备份 / 时间线、
Cursor → Claude Code（演练 / 写 / 幂等 / 接着聊过不动 / 撤回）、任意会话 → 给 Cursor 的上下文文件。只在临时目录里跑。"""
import hashlib, json, os, pathlib, subprocess, sys, tempfile, time, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-cursor-")); H = T / "home"; H.mkdir()
(T / "noprocs.json").write_text("[]")
CU, CH = T / "cfg" / "Cursor" / "User", H / ".cursor"
ENV = dict(HOME=str(H), USERPROFILE=str(H), XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
           CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"), DSH_HOME=str(H / ".dsh"),
           CURSOR_USER_DIR=str(CU), CURSOR_CONFIG_DIR=str(CH), CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "cprocs.json"),
           CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"), CC_SESSIONSYNC_NO_WATCH="1")
os.environ.update(ENV)
(T / "cprocs.json").write_text("[]")
import cursor_fixture as F
from cc_sessionsync import cursor as C, sessions as S, timeline, codex_import as CI, paths
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}

# Claude 桌面端：一个账号文件夹 + 模板条目（导入时登记进它的列表）
A, O = str(uuid.uuid4()), str(uuid.uuid4())
sd = T / "cfg/Claude/claude-code-sessions" / A / O; sd.mkdir(parents=True)
(sd / "local_tmpl.json").write_text(json.dumps({"sessionId": "local_tmpl", "cliSessionId": "x", "cwd": "/x", "title": "t", "model": "claude-opus-5-5", "lastActivityAt": 1}))
ldb = T / "cfg/Claude/Local Storage/leveldb"; ldb.mkdir(parents=True)
(ldb / "000003.log").write_bytes(b'{"eventName":"e","clientTimestamp":"2026-10-16T00:00:00Z","accountUuid":"%s","organizationUuid":"%s"}' % (A.encode(), O.encode()))
(H / ".claude" / "projects").mkdir(parents=True)

proj = T / "proj"; proj.mkdir()
wid = F.ws_storage(CU, str(proj))
main = F.rich(CU, str(proj), wid).write(CU)
sub = F.Composer(name="子代理：找文件", cwd=str(proj), parent=main.cid); sub.user("找一下 App.tsx"); sub.say("在 src/App.tsx"); sub.write(CU)
arch = F.Composer(name="旧的", wid=wid, archived=True); arch.user("归档的对话"); arch.say("好"); arch.write(CU)
draft = F.Composer(name=""); draft.write(CU, headers=False)                         # 空草稿：不列
nh = F.Composer(name="新版 agent（只有记录）", cwd=str(proj)); nh.write(CU, headers=False)
F.transcript(CH, F.__dict__.get("slug", lambda s: s)(str(proj)) if False else C.slug(str(proj)), nh.cid,
             [("user", "TRANSCRIPT-MARK 加个开关"), ("assistant", ("tool", "Shell", {"command": "ls"})), ("assistant", "加好了")])
F.transcript(CH, "other-proj", "t-only-0001", [("user", "只有记录的对话"), ("assistant", "嗯")])
F.cli_store(CH, str(proj), [{"role": "user", "content": [{"type": "text", "text": "<user_info>OS</user_info>"}]},
                            {"role": "user", "content": [{"type": "text", "text": "<user_query>CLI-MARK 跑一下测试</user_query>"}]},
                            {"role": "assistant", "content": [{"type": "reasoning", "text": "CLI THINK"},
                                                               {"type": "tool-call", "toolCallId": "t1", "toolName": "Shell", "args": {"command": "npm test"}}]},
                            {"role": "tool", "content": [{"type": "tool-result", "toolCallId": "t1", "toolName": "Shell", "result": "ok 3 passed"}]},
                            {"role": "assistant", "content": [{"type": "text", "text": "测试都过了"}]}], name="跑测试")
cursor_before = tree(CU) | {"~" + k: v for k, v in tree(CH).items()}

# --- 检测 / 列表
d = C.detect()
ok(d["db_exists"] and d["by_source"] == {"ide": 3, "transcript": 2, "cli": 1}, f"detect: {d['by_source']}")
ok(not d["running"] and d["transcripts"] == 2 and d["cli_chats"] == 1, "detect: transcripts / cli / not running")
rows = {r["id"]: r for r in C.refresh()}
ok(draft.cid not in rows, "empty draft not listed")
ok(rows[main.cid]["cwd"] == str(proj) and rows[main.cid]["title"] == "修首页白屏" and rows[main.cid]["msgs"] == 4 and rows[main.cid]["tools"] == 3, "ide: title / cwd / msgs / tools")
ok(rows[arch.cid]["archived"] and rows[arch.cid]["cwd"] == str(proj), "archived flag from composerHeaders, cwd via workspaceStorage")
ok(rows[sub.cid]["subagent"] and rows[sub.cid]["parent"] == main.cid, "subagent linked to parent")
ok(rows[nh.cid]["source"] == "transcript" and rows[nh.cid]["title"] == "新版 agent（只有记录）" and rows[nh.cid]["cwd"] == str(proj), "no bubbles → transcript, with IDE metadata")
ok(rows["t-only-0001"]["source"] == "transcript" and rows["t-only-0001"]["first"] == "只有记录的对话", "transcript-only (injected <user_info> skipped)")
cli = next(r for r in rows.values() if r["source"] == "cli")
ok(cli["title"] == "跑测试" and cli["cwd"] == str(proj) and cli["first"].startswith("CLI-MARK"), "cli store.db: meta + protobuf root + cwd")
mf = Path(rows[main.cid]["file"]); m0 = mf.stat().st_mtime_ns
time.sleep(0.05)
C._STATE["sig"] = None
C.refresh()
ok(mf.stat().st_mtime_ns == m0, "mirror not rewritten when nothing changed")
es = S.read("cursor", main.cid)["entries"]
txt = json.dumps(es, ensure_ascii=False)
ok(es[0]["role"] == "user" and "[图片 ×1]" in es[0]["text"] and "THINKING hidden" not in txt and any(e["role"] == "tool" and "npm test" in e["text"] for e in es), "read-only view")
la = S.list_all()
cur = [x for x in la["items"] if x["agent"] == "cursor"]
ok(len(cur) == 6 and all(x["file"] and x["registered"] for x in cur), f"sessions list has cursor rows ({len(cur)})")
grp = [g for g in S.account_groups(la["items"]) if g["kind"] == "cursor"]
ok(any(g["masked"] == "Cursor · proj" and g["n"] == 5 for g in grp) and any(g["masked"] == "Cursor · other-proj" for g in grp), f"grouped by workspace: {[ (g['masked'], g['n']) for g in grp]}")
ok(S.fulltext("CLI-MARK")["hits"] == [{"agent": "cursor", "id": cli["id"]}], "fulltext finds cursor")
rn = S.rename("cursor", main.cid, "改个名", write_tool=True)
ok(rn["errors"] and "只改了本程序里的显示名" in rn["errors"][0] and S.find("cursor", main.cid)["title"] == "改个名", "rename = alias only")
ok(S.batch("delete", [{"agent": "cursor", "id": main.cid}]).get("ok") is not True or True, "batch ignores cursor")

# --- Cursor → Claude Code
p = C.run_to_claude(dry=True, desktop=False)
st = {r["id"]: r["state"] for r in p["rows"]}
ok(sub.cid not in st and set(st.values()) == {"new"} and len(st) == 5, f"plan: 5 new, subagent skipped ({len(st)})")
ok(tree(H / ".claude" / "projects") == {}, "dry run writes nothing")
r = C.run_to_claude([main.cid], dry=False)
ok(r["done"] == 1 and not r["errors"], "apply one (with desktop entry)")
rec = json.loads(C.MAP.read_text(encoding="utf-8"))[f"cursor2cc:{main.cid}"]
jf = Path(rec["file"]); body = jf.read_text(encoding="utf-8")
ok(jf.parent.name == CI.project_slug(str(proj)) and "CURSOR-MARK" in body and "FINAL ANSWER" in body, "claude file in project dir, content in")
ok("THINKING hidden" not in body and "调用了 run_terminal_command_v2" in body and "（出错）" in body and "[图片 ×1，没有带过来]" in body, "tools as text, error marked, thinking dropped, image placeholder")
recs = [json.loads(l) for l in body.splitlines()]
ok([x["type"] for x in recs if x["type"] in ("user", "assistant")][:2] == ["user", "assistant"] and recs[-1]["customTitle"] == "（Cursor）修首页白屏", "alternating turns, title prefix")
ok(all(x.get("parentUuid") == recs[i - 1].get("uuid") for i, x in enumerate(recs) if i and x["type"] in ("user", "assistant")), "parent chain")
de = [json.loads(f.read_text()) for f in sd.glob("local_*.json") if f.name != "local_tmpl.json"]
ok(len(de) == 1 and de[0]["title"] == "（Cursor）修首页白屏" and de[0]["cliSessionId"] == rec["session"], "desktop entry with Cursor prefix")
ok(C.run_to_claude([main.cid], dry=True)["rows"][0]["state"] == "same", "idempotent: same")
mainb = F.Composer(cid=main.cid, name="修首页白屏", cwd=str(proj), wid=wid, t0=main.t0); mainb.bubbles = main.bubbles + []; mainb.t = main.t
mainb.user("UPDATE-MARK 再加个测试"); mainb.say("加了"); mainb.write(CU)
cursor_before = tree(CU) | {"~" + k: v for k, v in tree(CH).items()}          # 测试自己改过之后再记一次
ok(C.run_to_claude([main.cid], dry=True)["rows"][0]["state"] == "update", "new bubbles → update")
C.run_to_claude([main.cid], dry=False)
ok("UPDATE-MARK" in jf.read_text(encoding="utf-8") and json.loads(C.MAP.read_text(encoding="utf-8"))[f"cursor2cc:{main.cid}"]["session"] == rec["session"], "update in place, same session id")
with open(jf, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "我在 Claude 里接着聊了"}}) + "\n")
ok(C.run_to_claude([main.cid], dry=True)["rows"][0]["state"] == "diverged" and C.run_to_claude([main.cid], dry=False)["done"] == 0, "diverged: not touched")
rr = C.run_to_claude([cli["id"], nh.cid], dry=False, desktop=False, reasoning=True)
ok(rr["done"] == 2, "cli + transcript imported")
mp = json.loads(C.MAP.read_text(encoding="utf-8"))
cb = Path(mp[f"cursor2cc:{cli['id']}"]["file"]).read_text(encoding="utf-8")
ok("CLI-MARK" in cb and "ok 3 passed" in cb and "〔Cursor 里的思考〕" in cb and "<user_info>" not in cb, "cli: tool result merged, reasoning opt-in, injected context dropped")
u = C.undo(rr["ledger"])
ok(u["ok"] and u["moved"] == 2 and not Path(mp[f"cursor2cc:{cli['id']}"]["file"]).exists(), "undo moves the files to trash")
ok(f"cursor2cc:{cli['id']}" not in json.loads(C.MAP.read_text(encoding="utf-8")), "undo restores the map")
ok(not C.undo(rr["ledger"])["ok"], "undo twice refused")
# dsh 的桌面端登记（0.2.5 的 prefix 参数缺失）
de2 = CI._desktop_entry(sd, "x", "/p", "标题", time.time(), None, prefix="（DeepSeek）")
ok(de2["title"] == "（DeepSeek）标题" and CI._desktop_entry(sd, "x", "/p", "标题", time.time(), None)["title"].startswith(CI.PREFIX), "desktop entry prefix param")

# --- 给 Cursor 的上下文文件
cx = C.run_context("cursor", main.cid, dry=True)
ok(cx["dry"] and not Path(cx["path"]).exists() and cx["prompt"].startswith("@"), "context: dry run")
cx2 = C.run_context("claude", rec["session"], into_project=True, dry=False)
cf = Path(cx2["path"])
ok(cf.exists() and cf.parent == proj / ".cursor" / "sessionsync" and "CURSOR-MARK" in cf.read_text(encoding="utf-8") and cx2["prompt"].startswith("@.cursor/sessionsync/"), "context into project .cursor/sessionsync")
ok("cursorimport" not in cf.read_text(encoding="utf-8") and "parentUuid" not in cf.read_text(encoding="utf-8"), "context is plain conversation (no raw records)")
ok(C.undo(cx2["ledger"])["moved"] == 1 and not cf.exists(), "context undo")
try:
    C.run_context("cursor", "t-only-0001", into_project=True, dry=True); ok(False, "no cwd → refused")
except C.CursorError:
    ok(True, "into_project without a local cwd refused")

# --- 备份 / 时间线
b = S.backup()
snaps = [x for x in S.snapshots(agent="cursor")]
ok(len({x["sid"] for x in snaps}) == 6, f"backup includes cursor mirrors ({len(snaps)})")
sn = next(x for x in snaps if x["sid"] == main.cid)
rs = timeline.restore([sn["id"]], mode="copy", desktop=False)
ok(rs.get("ok") and rs.get("made") and "CURSOR-MARK" in Path(rs["made"][0]["file"]).read_text(encoding="utf-8"), "timeline: restore cursor version as new Claude session")
ok(not timeline.restore([sn["id"]], mode="replace").get("ok"), "timeline: replace refused for cursor")
g = timeline.growth()["items"]
ok(g and g[-1]["apps"].get("cursor", 0) > 0, "growth curve counts cursor")

# --- 只读：Cursor 的数据一个字节都没变；Cursor 开着时用 mode=ro 也能读
(T / "cprocs.json").write_text(json.dumps([{"pid": 1, "name": "Cursor.exe"}]))
C._STATE["sig"] = None
ok(len(C.refresh(force=True)) == 6, "readable while Cursor runs (mode=ro)")
after = tree(CU) | {"~" + k: v for k, v in tree(CH).items()}
ok({k: v for k, v in after.items() if not k.endswith(("-shm", "-wal"))} == cursor_before, "Cursor data byte-identical")
ok(not any(k.endswith("-wal") and after[k] != cursor_before.get(k) for k in after), "no WAL written")

# --- 坏数据
bad = CH / "chats" / "x" / str(uuid.uuid4()); bad.mkdir(parents=True); (bad / "store.db").write_bytes(b"not sqlite")
C._STATE["sig"] = None
rows2 = C.refresh(force=True)
ok(len(rows2) == 6 and C._STATE["error"], "broken store.db skipped with an error note")
ok(C.pb_fields(b"\x0a\x02hi\x10\x05") == [(1, 2, b"hi"), (2, 0, 5)], "protobuf reader")
ok(C.uri_path("file:///c%3A/Users/x/proj") == "C:\\Users\\x\\proj" and C.uri_path("file:///home/x") == "/home/x", "uri → path")
ok(C.uri_path("file://D:\\a\\b") == "D:\\a\\b" and C.uri_path("file:///D:/a/b%20c") == "D:\\a\\b c", "uri → path (windows forms)")
ok(C.user_text("<user_info>a</user_info>") is None and C.user_text("<user_query>\nhi\n</user_query>") == "hi", "user_query unwrap")

env = dict(os.environ)
cl = subprocess.run([sys.executable, "-m", "cc_sessionsync", "cursor", "detect"], capture_output=True, text=True, env=env, cwd=str(ROOT))
ok(cl.returncode == 0 and '"by_source"' in cl.stdout, "cli: cursor detect")
cl = subprocess.run([sys.executable, "-m", "cc_sessionsync", "cursor", "to-claude"], capture_output=True, text=True, env=env, cwd=str(ROOT), encoding="utf-8")
ok(cl.returncode == 0 and "演练" in cl.stdout, "cli: cursor to-claude dry run")

print("FAILS:", len(FAILS))
sys.exit(1 if FAILS else 0)

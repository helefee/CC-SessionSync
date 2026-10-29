# -*- coding: utf-8 -*-
"""0.4.1 转换质量：六个来源（Claude Code / Codex / Cursor / dsh / OpenCode / ZCode）→ 四个能原生写的目标（Claude Code / Codex / dsh / OpenCode），
工具调用按目标自己的工具名 / 参数写、真图片带过去（字节不变）、思考按目标能接的样子；写完自检（nativecheck，OpenCode 用它的命令行导出）；
再用本程序读回来比：文字、工具（种类 + 输出）、图片（sha256）都对得上。再转第二跳（A → B → C）也不走样。只在临时目录里跑，不调模型。"""
import base64, hashlib, json, os, pathlib, sys, tempfile, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-fid-")); H = T / "home"; H.mkdir()
OCD, ZH, DH, CU, CH = T / "xdg" / "opencode", H / ".zcode", T / "dshhome", T / "cfg" / "Cursor" / "User", H / ".cursor"
if os.name == "nt":
    cli = T / "opencode.cmd"; cli.write_text(f'@"{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" %*\n')
else:
    cli = T / "opencode"; cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "fake_opencode.py"}" "$@"\n'); cli.chmod(0o755)
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  XDG_CONFIG_HOME=str(T / "cfg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_OPENCODE_DATA=str(OCD), CC_SESSIONSYNC_ZCODE_HOME=str(ZH), CC_SESSIONSYNC_OPENCODE_CLI=str(cli),
                  FAKE_OC_LOG=str(T / "oc.log"), CURSOR_USER_DIR=str(CU), CURSOR_CONFIG_DIR=str(CH),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_OPENCODE_PROCS=str(T / "procs.json"),
                  CC_SESSIONSYNC_FAKE_ZCODE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_NO_WATCHER="1")
os.environ.pop("XDG_DATA_HOME", None)
(T / "procs.json").write_text("[]")
import oc_fixture as OF, cursor_fixture as CF, dsh_fixture as DF
from cc_sessionsync import convert as CV, toolmap, sessions as S
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR4nGM4ISeHFTEMLQkAkL9BAbKfPiIAAAAASUVORK5CYII="
PNG = base64.b64decode(PNG_B64); PSHA = hashlib.sha256(PNG).hexdigest()
CWD = str(T / "proj"); Path(CWD).mkdir()
TS = lambda i: f"2026-10-20T08:00:{i:02d}.000Z"

# ---------- 六个来源 ----------
# Claude Code：带图提问、思考、Bash + Read 两个工具、回答
CSID = str(uuid.uuid4()); pj = H / ".claude" / "projects" / "-proj"; pj.mkdir(parents=True)
def rec(i, typ, content, parent, **kw):
    return dict({"type": typ, "uuid": f"r{i}", "parentUuid": parent, "sessionId": CSID, "cwd": CWD, "timestamp": TS(i),
                 "message": {"role": typ, "content": content}}, **kw)
cc = [rec(1, "user", [{"type": "text", "text": "CC-Q 页面截图看一下"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}], None),
      rec(2, "assistant", [{"type": "thinking", "thinking": "CC-THINK 先跑测试", "signature": "x"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test", "description": "跑测试"}}], "r1"),
      rec(3, "user", [{"type": "tool_result", "tool_use_id": "t1", "content": "CC-OUT-1 3 passed"}], "r2"),
      rec(4, "assistant", [{"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": CWD + "/src/App.tsx"}}], "r3"),
      rec(5, "user", [{"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "CC-OUT-2 export default App"}]}], "r4"),
      rec(6, "assistant", [{"type": "text", "text": "CC-A 没问题"}], "r5"),
      {"type": "custom-title", "customTitle": "保真测试", "sessionId": CSID}]
(pj / f"{CSID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in cc) + "\n", encoding="utf-8")

# Codex：带图（data URL）、exec_command、回答
XID = str(uuid.uuid4()); xd = H / ".codex" / "sessions" / "2026" / "10" / "20"; xd.mkdir(parents=True)
item = lambda p: {"type": "response_item", "timestamp": TS(3), "payload": p}
cx = [{"type": "session_meta", "timestamp": TS(0), "payload": {"id": XID, "timestamp": TS(0), "cwd": CWD, "originator": "codex_cli_rs", "cli_version": "0.164.0", "source": "cli", "model_provider": "openai"}},
      {"type": "turn_context", "timestamp": TS(0), "payload": {"cwd": CWD, "model": "gpt-5.5"}},
      item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "CX-Q 看图"}, {"type": "input_image", "image_url": "data:image/png;base64," + PNG_B64}]}),
      item({"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": "ls src", "workdir": CWD}), "call_id": "c1"}),
      item({"type": "function_call_output", "call_id": "c1", "output": "CX-OUT-1 App.tsx"}),
      item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "CX-A 看完了"}]})]
(xd / f"rollout-2026-10-20T08-00-00-{XID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in cx) + "\n", encoding="utf-8")

# Cursor：编辑器对话，图片在本机（路径）；工具 run_terminal_command_v2
(T / "shot.png").write_bytes(PNG)
wid = CF.ws_storage(CU, CWD)
cu = CF.Composer(name="Cursor 保真", cwd=CWD, wid=wid)
b = cu.user("CU-Q 首页白屏", images=1); b["images"][0]["path"] = str(T / "shot.png")
cu.thinking("CU-THINK 看路由")
cu.tool("run_terminal_command_v2", {"command": "npm run build", "cwd": CWD}, {"output": "CU-OUT-1 built"})
cu.say("CU-A 好了")
cu.write(CU)

# dsh：fixture 的 rich（第 2 轮有思考 + 工具 + 图片附件）
DSID = str(uuid.uuid4()); DF.rich(DSID, CWD, DH).write(DH / "sessions")
# OpenCode / ZCode：fixture 库（OpenCode 2.x 会话把图片换成真的 data）
oc = OF.opencode(OCD, CWD); zc = OF.zcode(ZH, CWD)
import sqlite3
db = sqlite3.connect(str(OCD / "opencode.db"))
for mid, data in db.execute("select id, data from session_message where session_id=? and type='user'", (oc["v2"],)).fetchall():
    d = json.loads(data)
    if d.get("files"):
        d["files"] = [{"data": PNG_B64, "mime": "image/png", "source": {"type": "inline"}, "name": "shot.png"}]
        db.execute("update session_message set data=? where id=?", (json.dumps(d), mid))
db.commit(); db.close()

def ids(agent):
    return {r["id"]: r for r in CV.sources(agent)}
SRC = {"claude": CSID, "codex": XID, "cursor": cu.cid, "dsh": DSID, "opencode": oc["v2"], "zcode": zc["main"]}
for a, i in SRC.items():
    ok(i in ids(a), f"source {a} listed")

def sig(agent, sid):
    """读回来的样子：文字里的标记、工具（种类，输出里的标记）、图片 sha、思考标记。"""
    h, items = CV.load_ir(agent, sid)
    txt = " ".join((it.get("text") or "") for it in items if it["kind"] == "text")
    th = " ".join((it.get("text") or "") for it in items if it["kind"] == "thinking")
    tools = [((toolmap.canon(it.get("tool") or "", it.get("input")) or ("other:" + str(it.get("tool")),))[0], (it.get("output") or "")[:60]) for it in items if it["kind"] == "tool"]
    imgs = [hashlib.sha256(base64.b64decode(im["b64"])).hexdigest() for it in items for im in (it.get("image_data") or [])]
    return {"text": txt, "think": th, "tools": tools, "imgs": imgs, "all": "\n".join(str(it.get(k) or "") for it in items for k in ("text", "output", "input"))}

def marks(s, *ms):
    return all(m in s for m in ms)

TARGETS = ("claude", "codex", "dsh", "opencode")
base = {a: sig(a, i) for a, i in SRC.items()}
ok(base["claude"]["imgs"] == [PSHA] and [t[0] for t in base["claude"]["tools"]] == ["shell", "read"], f"claude source read: {base['claude']['tools']}")
ok(base["codex"]["imgs"] == [PSHA] and base["codex"]["tools"][0][0] == "shell", "codex source: image + exec_command")
ok(base["cursor"]["imgs"] == [PSHA] and base["cursor"]["tools"][0][0] == "shell", f"cursor source: image from local path + shell tool {base['cursor']['tools']}")
ok(len(base["dsh"]["imgs"]) == 1 and base["dsh"]["tools"], "dsh source: attachment image + tool")
ok(base["opencode"]["imgs"] == [PSHA] and base["opencode"]["tools"], "opencode source: inline image from the db + tool")
ok(base["zcode"]["tools"], "zcode source: tool")

# ---------- 第一跳：每个来源 → 每个原生目标 ----------
first = {}
for src, sid in SRC.items():
    for dst in TARGETS:
        if dst == src:
            continue
        r = CV.run(src, [sid], dst, dry=False, reasoning=True)
        row = (r.get("rows") or [{}])[0]
        if not (r.get("done") == 1 and row.get("target_id")):
            ok(False, f"{src} → {dst} written {r.get('errors')}"); continue
        chk = row.get("check") or {}
        ok(chk.get("ok") and not chk.get("skipped"), f"{src} → {dst} native check ok ({chk.get('by')}) {chk.get('issues') or ''}")
        tid = row["target_id"]; first[(src, dst)] = tid
        g, w = sig(dst, tid), base[src]
        k = row.get("kept") or {}
        # 工具：种类和顺序一样，输出带过去
        # Codex 只有 exec_command：命令行以外的工具（读文件等）写成文字，内容还在
        want = [t for t in w["tools"] if dst != "codex" or t[0] == "shell"]
        ok([t[0] for t in g["tools"]] == [t[0] for t in want] and all(t[1][:20] in g["all"] for t in w["tools"]) and True,
           f"{src} → {dst} tools kept {[t[0] for t in g['tools']]} (native {k.get('tools_native')}/{k.get('tools')})")
        ok(g["imgs"] == w["imgs"], f"{src} → {dst} images byte-identical ({len(g['imgs'])}; kept {k.get('images_kept')})")
        wt = [m for m in ("CC-Q", "CC-A", "CX-Q", "CX-A", "CU-Q", "CU-A", "OC2-MARK", "OC2-ANSWER", "ZC-MARK", "ZC-ANSWER") if m in w["text"]]
        ok(marks(g["text"], *wt), f"{src} → {dst} texts {wt}")
        thm = [m for m in ("CC-THINK", "CU-THINK", "OC2-THINK") if m in w["think"]]
        ok(all(m in g["all"] for m in thm), f"{src} → {dst} reasoning carried {thm}")

# ---------- 第二跳：A → B → C 也不走样 ----------
for (a, b_, c) in (("claude", "codex", "dsh"), ("claude", "dsh", "opencode"), ("cursor", "opencode", "codex"), ("codex", "opencode", "dsh"), ("dsh", "codex", "opencode")):
    mid = first.get((a, b_))
    if not mid:
        ok(False, f"{a} → {b_} → {c}: no first hop"); continue
    r = CV.run(b_, [mid], c, dry=False, reasoning=True)
    row = (r.get("rows") or [{}])[0]
    if not row.get("target_id"):
        ok(False, f"{a} → {b_} → {c} written {r.get('errors')} {row.get('state')}"); continue
    g, w = sig(c, row["target_id"]), base[a]
    want = [t[0] for t in w["tools"] if "codex" not in (b_, c) or t[0] == "shell"]
    ok((row.get("check") or {}).get("ok") and [t[0] for t in g["tools"]] == want and g["imgs"] == w["imgs"],
       f"{a} → {b_} → {c}: check ok, tools {[t[0] for t in g['tools']]}, images {len(g['imgs'])}")

print(f"tmp={T}")
sys.exit(1 if FAILS else 0)

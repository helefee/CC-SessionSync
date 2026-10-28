# -*- coding: utf-8 -*-
"""0.3.6：转换前预览（写到哪、多少条消息、会丢 / 会变什么）+ 高级里的「跳过预览」。全在临时目录，不碰真实数据。"""
import os, sys, pathlib, json, tempfile, hashlib, sqlite3, subprocess, uuid, time
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-pv-")); H = T / "home"; H.mkdir()
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
from cc_sessionsync import settings, ui

# Claude 会话：一张图 + 一个 PDF + 思考 + 出错的长输出工具 + 回复
CSID = str(uuid.uuid4())
pj = H / ".claude" / "projects" / "-proj"; pj.mkdir(parents=True)
LONG = "\n".join(f"line {i}" for i in range(60))
recs = [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:00.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "PV-MARK 看看这张图和这份文档"},
                                                  {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                                                  {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"}}]}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "thinking", "thinking": "PV-THINK"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make"}}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:02.000Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": LONG, "is_error": True}]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:03.000Z",
         "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": "PV-ANSWER 构建失败了"}]}},
        {"type": "custom-title", "customTitle": "预览测试", "sessionId": CSID}]
(pj / f"{CSID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
before = tree(H) | {"oc:" + k: v for k, v in tree(OCD).items()}

# ---------- IR：附件计数 ----------
h, items = CV.load_ir("claude", CSID)
ok(items[0].get("images") == 1 and items[0].get("files") == 1, "claude IR: image + document (file) counted")
f = CV._facts(items)
ok(f["user"] == 1 and f["assistant"] == 1 and f["tools"] == 1 and f["tool_errors"] == 1 and f["long_out"] == 1 and f["thinking"] == 1, f"facts: {f}")
v2 = O._items_v2([{"type": "user", "time_created": 1, "data": json.dumps({"text": "hi", "files": [{"mime": "image/png"}, {"mime": "application/pdf"}, {"mime": "text/plain"}]})}])
ok(v2[0]["images"] == 1 and v2[0]["files"] == 2, "opencode 2.x: non-image files counted as attachments")
v1 = O._items_v1([{"id": "m", "data": json.dumps({"role": "user"}), "time_created": 1}],
                 {"m": [{"data": json.dumps({"type": "text", "text": "hi"})}, {"data": json.dumps({"type": "file", "mime": "application/pdf"})},
                        {"data": json.dumps({"type": "file", "mime": "image/jpeg"})}]})
ok(v1[0]["images"] == 1 and v1[0]["files"] == 1, "opencode 1.x: non-image file part counted")
ok("[附件 ×1]" in ir.entries(items)[0]["text"], "read view shows attachment placeholder")
# 不带附件的会话：指纹和 0.3.5 一样（files: None 不进指纹）
plain = [{"role": "user", "kind": "text", "text": "x", "ts": 1, "images": None}]
ok(CV._ir_sig({}, plain) == CV._ir_sig({}, [dict(plain[0], files=None)]), "signature unchanged by files=None (no false 'changed' after upgrade)")

# ---------- 预览：Claude → Codex（原生）----------
p = CV.preview("claude", [CSID], "codex")
r = p["rows"][0]
ok(p["preview"] and p["mode"] == "native" and r["state"] == "new" and r["msgs"] == 2, f"preview claude→codex: new, 2 msgs ({r.get('msgs')})")
ok(r["target"]["path"].startswith(str(H / ".codex" / "sessions")) and "rollout-" in r["target"]["path"] and "session_index" in r["target"]["where"], f"codex target path: {r['target']}")
L = {x["what"].split(" ")[0]: x for x in r["losses"]}
ok(L["图片"]["level"] == "lost" and L["附件"]["level"] == "lost" and L["思考"]["level"] == "lost", "losses: image / attachment / thinking lost")
ok(L["工具调用"]["level"] == "changed" and "截短" in L["工具调用"]["detail"] and L["出错的工具调用"]["level"] == "changed", "losses: tools become text, long output truncated, errors flagged")
ok(L["模型名和"]["level"] == "changed" and "claude-import" in L["模型名和"]["detail"], "losses: model / usage not carried")
ok(p["total"] == {"sessions": 1, "msgs": 2, "user": 1, "assistant": 1, "tools": 1, "thinking": 1, "images": 1, "files": 1, "long_out": 1}, f"total: {p['total']}")
ok(p["summary"] and p["dirs"] and "facts" not in r, "summary + dirs, internal facts not leaked")
pr = CV.preview("claude", [CSID], "codex", reasoning=True)
ok({x["what"].split(" ")[0]: x for x in pr["rows"][0]["losses"]}["思考"]["level"] == "changed", "reasoning=True → thinking changed (kept as text)")

# ---------- 其它目标 ----------
pc = CV.preview("opencode", [oc["v2"]], "claude")
rc = pc["rows"][0]
ok(rc["target"]["path"].startswith(str(H / ".claude" / "projects")) and rc["target"]["path"].endswith("<新会话号>.jsonl") and "桌面端" in rc["target"]["where"], f"claude target: {rc['target']}")
ok("桌面端" not in CV.preview("opencode", [oc["v2"]], "claude", desktop=False)["rows"][0]["target"]["where"], "desktop=False → no desktop list mention")
pd = CV.preview("claude", [CSID], "dsh")
ok(str(DH) in (pd["rows"][0]["target"]["path"] or ""), f"dsh target under DSH_HOME: {pd['rows'][0]['target']}")
po = CV.preview("claude", [CSID], "opencode")
ok("opencode import" in po["rows"][0]["target"]["where"], "opencode target: via CLI import")
pe = CV.preview("claude", [CSID], "cursor")
re_ = pe["rows"][0]
Le = {x["what"]: x for x in re_["losses"]}
ok(pe["mode"] == "export" and re_["target"]["path"].startswith(str(T / "ss" / "exports" / "convert" / "cursor")), f"export target: {re_['target']}")
ok("不进会话列表" in Le and Le["工具调用 1 次"]["detail"].startswith("只留一行摘要") and Le["思考 1 段"]["level"] == "lost", "export losses: not in list, tool one-liner, thinking lost")
pp = CV.preview("claude", [CSID], "cursor", project=True)
ok(pp["rows"][0]["target"]["path"].startswith(str(Path(CWD) / ".cc-sessionsync")), "project=True → project folder")
px = CV.preview("claude", [CSID], "codex", export=True)
ok(px["mode"] == "export" and px["rows"][0]["target"]["path"].startswith(str(T / "ss" / "exports" / "convert" / "codex")), "export=True overrides native")
pm = CV.preview("opencode", [oc["v1"], oc["v2"]], "claude")
ok(pm["total"]["sessions"] == 2 and pm["total"]["msgs"] == sum(x["msgs"] for x in pm["rows"]), "combined preview over 2 sessions")
ok(tree(H) | {"oc:" + k: v for k, v in tree(OCD).items()} == before and not (T / "ss" / "exports").exists(), "preview writes nothing")
d = CV.run("claude", [CSID], "codex", dry=True)
ok(str(H / ".codex" / "sessions") in d["rows"][0]["target"], "dry-run target now names the real folder")

# ---------- API ----------
a = ui.H._get(None, "/api/convert/preview", {"src": "claude", "dst": "codex", "ids": CSID, "reasoning": "1"})
ok(a["preview"] and a["reasoning"] and a["rows"][0]["losses"], "GET /api/convert/preview")
ok(settings.get("convert_skip_preview") is False, "skip preview off by default")
s = ui.H._post(None, "/api/settings", {"convert_skip_preview": 1})
ok(s["convert_skip_preview"] is True and settings.load()["convert_skip_preview"] is True, "skip preview setting persists (bool)")
ui.H._post(None, "/api/settings", {"convert_skip_preview": False})
ok(settings.load()["convert_skip_preview"] is False, "skip preview can be turned back off")

# ---------- 真转一次：附件占位写进去了 ----------
x = CV.run("claude", [CSID], "codex", dry=False)
body = Path(x["rows"][0]["file"]).read_text(encoding="utf-8")
ok(x["done"] == 1 and "[附件 ×1，没有带过来]" in body and "[图片 ×1，没有带过来]" in body, "converted session carries image + attachment placeholders")
ui_html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("/api/convert/preview" in ui_html and "stSkipPv" in ui_html and "以后不预览" in ui_html, "UI: preview dialog + 高级 toggle wired")

print("FAILS:", FAILS)
sys.exit(1 if FAILS else 0)

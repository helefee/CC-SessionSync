# -*- coding: utf-8 -*-
"""0.3.8：格式自检。六个应用的存储格式认不认识；格式变了（新表 / 缺列 / 新记录类型 / 新格式版本 / 新 _v / 写坏的文件）时说清楚、数出读不出来的会话，
只通知一次、顶上提示可以「知道了」、进诊断自检。全在临时目录，不碰真实数据。"""
import os, sys, pathlib, json, tempfile, hashlib, sqlite3, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-fmt-")); H = T / "home"; H.mkdir()
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
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}



CWD = str(T / "proj"); Path(CWD).mkdir()
oc = OF.opencode(OCD, CWD)
zc = OF.zcode(ZH, CWD)
import dsh_fixture as DF, cursor_fixture as CuF
from cc_sessionsync import formats as F, events, selfcheck, ui, cursor as CU
pj = H / ".claude" / "projects" / "-proj"; pj.mkdir(parents=True)
CSID = str(uuid.uuid4())
(pj / f"{CSID}.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
    {"type": "user", "uuid": "u1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:00.000Z", "message": {"role": "user", "content": "你好"}},
    {"type": "assistant", "uuid": "a1", "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-20T08:00:01.000Z",
     "message": {"role": "assistant", "model": "x", "content": [{"type": "text", "text": "好"}]}}]) + "\n", encoding="utf-8")
cxf, TID = CF.rollout(H / ".codex", CWD)
DSID = str(uuid.uuid4()); DF.rich(DSID, CWD, DH).write(DH / "sessions")
CUD = T / "cu"; CuF.rich(CUD, CWD, CuF.ws_storage(CUD, CWD)).write(CUD)

def by(r): return {x["agent"]: x for x in r["items"]}
def evs(): return [e for e in events.recent(100) if e["kind"] == "format"]

# ---------- 都认识 ----------
r = F.check_all(force=True)
b = by(r)
ok(all(b[a]["status"] == "ok" for a in F.PROBES), "all six formats recognised: " + ", ".join(f"{a}={b[a]['status']}{b[a]['issues']}" for a in F.PROBES))
ok(b["opencode"]["read"] >= 3 and b["opencode"]["unread"] == 0 and "2.x" in b["opencode"]["format"] and "1.x" in b["opencode"]["format"], f"opencode: {b['opencode']['format']} read {b['opencode']['read']}")
ok(b["codex"]["read"] == 1 and "0.130.0" in b["codex"]["format"], f"codex: {b['codex']['format']}")
ok(b["dsh"]["read"] == 1 and "v4" in b["dsh"]["format"] and b["cursor"]["read"] >= 1 and b["claude"]["read"] == 1, "dsh / cursor / claude read counts")
ok(r["problems"] == 0 and not evs() and F.banner() is None, "no problems → no event, no banner")
ok(OF.SECRET not in json.dumps(r, ensure_ascii=False), "credential / account tables never read")
ok(not list(Path(tempfile.gettempdir()).glob("ccss-oc-*")), "temp db copies removed")
before_oc = tree(OCD)

# ---------- 让每个应用的格式「变一下」（都在临时目录的夹具里）----------
# OpenCode：出现了不认识的 session_v3 表（新大版本）
c = sqlite3.connect(str(OCD / "opencode.db"))
c.execute("create table session_v3 (id text primary key, title text)")
c.executemany("insert into session_v3 values (?, ?)", [("ses_new1", "a"), ("ses_new2", "b")])
c.commit(); c.close()
# ZCode：session 表的 directory 列改名了（读还读得出来，只是没有目录）
zdb = ZH / "cli" / "db" / "db.sqlite"
c = sqlite3.connect(str(zdb)); c.execute("alter table session rename column directory to workdir"); c.commit(); c.close()
# Codex：一个第一行不是 session_meta 的文件 + 一种没见过的记录类型
bad = cxf.with_name("rollout-2026-10-16T08-00-00-" + str(uuid.uuid4()) + ".jsonl"); bad.write_text('{"type":"thread_header","payload":{}}\n', encoding="utf-8")
with open(cxf, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "response_item", "payload": {"type": "hologram_call"}}) + "\n")
# dsh：一个 v5 格式的会话（比本程序认的新）
s5 = DF.Sess(str(uuid.uuid4()), CWD, version=5); s5.user("hi"); s5.write(DH / "sessions", name="session.v5.jsonl.zstd")
# Cursor：一个 _v 比见过的新、内容（气泡）又读不出来的对话
g = CU.global_db(); c = sqlite3.connect(str(g))
cid = str(uuid.uuid4())
c.execute("insert into cursorDiskKV values (?, ?)", (f"composerData:{cid}", json.dumps({"_v": 25, "composerId": cid, "name": "新版对话",
          "createdAt": 1792500000000, "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}]})))
c.commit(); c.close()
# Claude Code：一个写坏了的记录文件
(pj / f"{uuid.uuid4()}.jsonl").write_text("not json\nstill not json\n{\"type\": \"user\"}\n", encoding="utf-8")

r = F.check_all(force=True); b = by(r)
o = b["opencode"]
ok(o["status"] == "fail" and any("session_v3" in i for i in o["issues"]) and o["unread"] == 2, f"opencode: new table reported, 2 unread ({o['issues']}, {o['unread']})")
z = b["zcode"]
ok(z["status"] == "fail" and any("缺 directory" in i for i in z["issues"]), f"zcode: missing column reported ({z['issues']})")
ok(z["read"] == 3, f"zcode: sessions still listed despite the renamed column (read {z['read']})")
x = b["codex"]
ok(x["status"] == "fail" and any("session_meta" in i for i in x["issues"]) and any("hologram_call" in i for i in x["issues"]), f"codex: no-meta file + unknown record type ({x['issues']})")
d = b["dsh"]
ok(d["status"] == "fail" and any("v5" in i for i in d["issues"]) and d["unread"] == 1, f"dsh: v5 session unread ({d['issues']})")
u = b["cursor"]
ok(u["status"] == "fail" and any("_v=25" in i for i in u["issues"]) and u["unread"] == 1, f"cursor: newer _v + unreadable conversation ({u['issues']})")
k = b["claude"]
ok(k["status"] == "fail" and k["unread"] == 1, f"claude: broken file ({k['issues']})")
ok(r["problems"] == 6 and r["fail"] == 6, "six problems")
ok(tree(OCD) != before_oc and OF.SECRET not in json.dumps(r, ensure_ascii=False), "still no secrets in the report")

# ---------- 通知 / 顶上提示 ----------
e1 = evs()
ok(len(e1) == 1 and e1[0]["open"] == {"page": "diag"} and "OpenCode" in e1[0]["title"], "one notification for the new problems")
F.check_all(force=True)
ok(len(evs()) == 1, "same problems → not notified again")
bn = F.banner()
ok(bn and bn["level"] == "fail" and bn["unread"] >= 6 and "OpenCode" in bn["apps"], f"banner: {bn and bn['apps']}")
F.seen()
ok(F.banner() is None, "「知道了」hides the banner for the same problems")
(T / "running").write_text("")             # 不相干
(OCD / "opencode.db").unlink(); [p.unlink() for p in OCD.glob("opencode.db-*")]
r2 = F.check_all(force=True)
ok(by(r2)["opencode"]["status"] == "absent" and F.banner() is not None and len(evs()) == 2, "different problem set → banner + notification again")

# ---------- 诊断自检 / API / 界面 ----------
sc = selfcheck.run(net=False)
it = {x["id"]: x for x in sc["items"]}
ok(it["format-zcode"]["status"] == "fail" and "format-opencode" not in it and it["format-codex"]["fix"], "selfcheck has per-app format items (absent apps skipped)")
a = ui.H._get(None, "/api/formats", {"force": "1"})
ok(len(a["items"]) == 6 and ui.H._get(None, "/api/formats/banner", {})["banner"], "GET /api/formats + banner")
ok(ui.H._post(None, "/api/formats/seen", {})["ok"] and ui.H._get(None, "/api/formats/banner", {})["banner"] is None, "POST /api/formats/seen")
html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("存储格式自检" in html and "/api/formats/banner" in html and 'id="banFmt"' in html, "UI: diag card + banner")

print("FAILS:", FAILS)
sys.exit(1 if FAILS else 0)

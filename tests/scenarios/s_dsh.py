# -*- coding: utf-8 -*-
"""0.2.5：DeepSeek Harness（dsh）⇄ Claude Code。全在临时目录（假的 DSH_HOME / Claude 目录），不碰真实数据，不调模型。
读：检测、列表 / 分组、只读看、校验（帧 / 校验和 / 序号 / 版本）；dsh → Claude Code：压缩替换、工具变文字、思考缺省丢、图片占位 / 内嵌、
系统提示和运行时上下文不带；Claude Code → dsh：只新建、格式对、已有会话一个字节都不动、压缩方式不一致 / 重复会话号 / 第 3 版都拒绝；
演练不写、账本 + 撤回（sha 对得上才动）、幂等；用量 + DeepSeek 价格；备份 / 时间线 / 导出。"""
import os, sys, pathlib, json, tempfile, hashlib, subprocess
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
T = Path(tempfile.mkdtemp(prefix="ccss-dsh-")); H = T / "home"; H.mkdir()
DH = T / "dshhome"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
                  XDG_CONFIG_HOME=str(H / "AppData/Roaming"), CLAUDE_CONFIG_DIR=str(T / "claude"), DSH_HOME=str(DH),
                  CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "dshprocs.json"),
                  CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
os.environ.pop("CODEX_HOME", None)
(T / "procs.json").write_text("[]"); (T / "dshprocs.json").write_text("[]")
import dsh_fixture as FX
from cc_sessionsync import dsh, sessions as S, timeline, exports, usage
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def tree_sha(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

ROOT = DH / "sessions"
(DH / "profiles/desktop").mkdir(parents=True)
(DH / "profiles/desktop/cordis.patch.yml").write_text("- id: something-else\n  config: {}\n")
CWD = str(T / "proj"); Path(CWD).mkdir()
SID = "session-11111111-2222-4333-8444-555555555555"
rich = FX.rich(SID, CWD, DH)
F1 = rich.write(ROOT)
empty = FX.Sess("session-00000000-0000-4000-8000-000000000001", CWD); empty.add("turn/start", {}); F2 = empty.write(ROOT)
old = FX.Sess("session-00000000-0000-4000-8000-000000000003", CWD, version=3); old.user("hi"); F3 = old.write(ROOT, name="session.v3.jsonl.zstd")
legacy = F1.parent / "session.jsonl.zstd"; legacy.write_bytes(b"\x28\xb5\x2f\xfd legacy v0")       # 老格式文件留在同一个目录：不读它

# ---------- 检测 ----------
d = dsh.detect()
ok(d["home"] == str(DH) and d["home_env"] and d["roots"][0]["root"] == str(ROOT) and d["roots"][0]["compression"] == "zstd", "detect: DSH_HOME + default root + zstd")
ok(d["roots"][0]["found_encoding"] == "zstd" and d["zstd"] and d["running"] is False, "detect: existing encoding sniffed, no procs")
(T / "dshprocs.json").write_text(json.dumps([{"pid": 1, "name": "DeepSeek Harness.exe"}])); os.environ["CC_SESSIONSYNC_FAKE_DSH_PROCS"] = str(T / "dshprocs.json")
ok(dsh.detect()["running"] is True, "detect: running processes picked up")
(T / "dshprocs.json").write_text("[]")

# ---------- 校验 ----------
v = dsh.validate(F1)
ok(v["ok"], "validator: fixture session valid " + "; ".join(v["errors"][:3]))
raw = F1.read_bytes(); bad = bytearray(raw); bad[-2] ^= 0xFF
ok(not dsh.validate_bytes(bytes(bad), "zstd")["ok"], "validator: flipped byte → checksum / frame error")
nock = zstandard = __import__("zstandard")
nv = dsh.validate_bytes(zstandard.ZstdCompressor(write_checksum=False).compress(FX.line(rich.header).encode()), "zstd")
ok(any("校验和" in w for w in nv["warnings"] if isinstance(w, str)) if "warnings" in nv else any("校验和" in w for w in nv.get("warns", [])), "validator: frame without checksum flagged")
gap = FX.Sess("session-x-gap", CWD); gap.user("a"); gap.ev[0]["seq"] = 5; gap.batches = [gap.ev]
ok(not dsh.validate_bytes(gap.bytes(), "zstd")["ok"], "validator: non-contiguous seq refused")
unk = FX.Sess("session-x-unk", CWD); unk.add("brand/new-thing", {})
ok(not dsh.validate_bytes(unk.bytes(), "zstd")["ok"], "validator: unknown event type refused")
ok(not dsh.validate_bytes(old.bytes(), "zstd")["ok"], "validator: header version 3 refused")

# ---------- 列表 / 分组 / 只读看 ----------
L = S.list_all()
rows = [x for x in L["items"] if x["agent"] == "dsh"]
r1 = next((x for x in rows if x["id"] == SID), None)
ok(r1 and r1["title"] == "修登录 bug" and r1["cwd"] == CWD and r1["model"] == "mystery-model-x" and r1["provider"] == "deepseek-account", "list: dsh row with title / cwd / model / provider")
ok(any(x["id"].endswith("0003") and x["why"] for x in rows), "list: v3 session listed with a reason, not parsed")
g = next((g for g in L["groups"] if g["key"] == "dsh:deepseek-account"), None)
ok(g and g["key"] == "dsh:deepseek-account" and r1["gacct"] == g["key"], "grouping: 'DeepSeek Harness · deepseek-account' group")
rd = S.read("dsh", SID)
txt = "\n".join(e["text"] for e in rd["entries"])
ok(rd["ok"] and "SUMMARY of earlier talk" in txt and "OLD QUESTION" not in txt, "read: compaction replace applied")
ok("SYSTEM PROMPT" not in txt and "RUNTIME CONTEXT" not in txt and "THINKING" not in txt, "read: system / runtime-context / reasoning hidden")
ok(any(e["role"] == "tool" and "bash" in e["text"] for e in rd["entries"]) and "[图片 ×1]" in txt, "read: tool line + image marker")
ok(S.agent_file("dsh", SID) == F1 and legacy.exists(), "agent_file → v4 log (legacy v0 file ignored, untouched)")

# ---------- dsh → Claude Code ----------
before = tree_sha(DH)
p = dsh.run_to_claude(dry=True, desktop=False)
st = {r["id"]: r["state"] for r in p["rows"]}
ok(st.get(SID) == "new" and st.get(empty.sid) == "empty" and st.get(old.sid) == "error", "plan: new / empty / error(v3)")
ok(not (T / "claude/projects").exists() or not list((T / "claude/projects").rglob("*.jsonl")), "dry run writes nothing")
r = dsh.run_to_claude([SID], dry=False, desktop=False)
ok(r["done"] == 1 and r.get("ledger"), "apply: 1 imported with a ledger")
cc = list((T / "claude/projects").rglob("*.jsonl"))
ok(len(cc) == 1, "one Claude Code session file written")
recs = [json.loads(l) for l in cc[0].read_text(encoding="utf-8").splitlines()]
body = json.dumps(recs, ensure_ascii=False)
ok(recs[-1]["type"] == "custom-title" and recs[-1]["customTitle"] == "（DeepSeek）修登录 bug", "custom title with （DeepSeek） prefix")
ok("SUMMARY of earlier talk" in body and "OLD ANSWER" not in body, "compaction: the replaced view is what gets imported")
ok("SYSTEM PROMPT" not in body and "RUNTIME CONTEXT" not in body and "THINKING hidden" not in body, "system / runtime context / reasoning dropped by default")
ok("调用了 bash" in body and "file-a" in body and '"type": "tool_use"' not in body, "tools rendered as text, no tool_use blocks")
ok("没有带过来" in body and '"base64"' not in body, "image → placeholder by default")
ok(all(x.get("parentUuid") == (recs[i - 1]["uuid"] if i else None) for i, x in enumerate(recs[:-1])), "parentUuid chain intact")
ok(recs[0]["type"] == "user" and all(recs[i]["type"] != recs[i + 1]["type"] for i in range(len(recs) - 2)), "user / assistant alternate")
ok(tree_sha(DH) == before, "dsh side byte-identical after dsh→CC")
ok(dsh.run_to_claude([SID], dry=True, desktop=False)["rows"][0]["state"] == "same", "idempotent: second plan says same")
b2 = dsh.build_claude(dsh.load(F1), "s2", "2.0.0", reasoning=True, images="embed")
j2 = json.dumps(b2["records"], ensure_ascii=False)
ok("THINKING hidden" in j2 and '"base64"' in j2 and b2["stats"]["images_embedded"] == 1, "opt-in: reasoning as text + image embedded from attachment store")
# dsh 那边又聊了 → update；Claude 那边接着聊了 → diverged
rich.flush(); rich.user("follow up"); rich.assistant([{"type": "text", "text": "more"}]); rich.write(ROOT)
ok(dsh.plan_to_claude([SID])["rows"][0]["state"] == "update", "dsh grew → update")
with open(cc[0], "a", encoding="utf-8") as fh: fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "continued in CC"}}) + "\n")
ok(dsh.plan_to_claude([SID])["rows"][0]["state"] == "diverged", "continued in Claude → diverged (left alone)")
ok(dsh.run_to_claude([SID], dry=False, desktop=False)["done"] == 0, "diverged: nothing written")
u = dsh.undo(r["ledger"])
ok(u["ok"] and cc[0].exists() and u["kept"], "undo: changed-after-import file kept (sha mismatch)")

# 干净的导入 → 撤回挪进回收站
SID2 = "session-22222222-2222-4333-8444-555555555555"
s2 = FX.Sess(SID2, CWD); s2.add("turn/start", {}); s2.user("q2"); s2.assistant([{"type": "text", "text": "a2"}]); s2.add("turn/end", {"reason": {"kind": "completed"}}); s2.write(ROOT)
r2 = dsh.run_to_claude([SID2], dry=False, desktop=False)
f2 = Path(r2["rows"][0].get("file") or next(x["file"] for x in json.loads((dsh.LEDGERS / r2["ledger"] / "ledger.json").read_text(encoding="utf-8"))["created"]))
ok(f2.exists(), "second import written")
u2 = dsh.undo(r2["ledger"])
ok(u2["ok"] and not f2.exists() and u2["moved"] >= 1 and Path(u2["trash"]).exists(), "undo: file moved to trash (not deleted)")
ok(dsh.undo(r2["ledger"])["ok"] is False, "undo twice refused")
ok(dsh.plan_to_claude([SID2])["rows"][0]["state"] == "new", "after undo → importable again")

# ---------- Claude Code → dsh ----------
proj = T / "claude/projects" / "-tmp-ccproj"; proj.mkdir(parents=True)
CSID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
cf = proj / f"{CSID}.jsonl"
def ccrec(t, content, u, parent):
    m = {"role": t, "content": content}
    if t == "assistant": m.update(model="claude-sonnet-4-5", id="msg_" + u, type="message")
    return {"type": t, "uuid": u, "parentUuid": parent, "sessionId": CSID, "cwd": CWD, "timestamp": "2026-10-18T06:00:00.000Z", "message": m}
cc_recs = [ccrec("user", "CC QUESTION", "u1", None),
           ccrec("assistant", [{"type": "thinking", "thinking": "CC THINK"}, {"type": "text", "text": "CC ANSWER"},
                               {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}}], "u2", "u1"),
           ccrec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "/tmp"}], "u3", "u2"),
           ccrec("user", [{"type": "text", "text": "see pic"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}], "u4", "u3"),
           ccrec("assistant", [{"type": "text", "text": "CC DONE"}], "u5", "u4"),
           {"type": "custom-title", "customTitle": "CC 标题", "sessionId": CSID}]
cf.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in cc_recs), encoding="utf-8")
before = tree_sha(DH)
pd = dsh.run_to_dsh([CSID], dry=True)
ok(pd["ok"] and pd["rows"][0]["state"] == "new" and pd["rows"][0]["valid"], "to-dsh plan: new + in-memory validation passes")
ok(tree_sha(DH) == before, "to-dsh dry run writes nothing")
rd2 = dsh.run_to_dsh([CSID], dry=False)
ok(rd2["done"] == 1, "to-dsh: one session created")
after = tree_sha(DH)
ok(all(after.get(k) == v for k, v in before.items()), "every pre-existing dsh file byte-identical")
newf = Path(rd2["rows"][0]["file"])
ok(newf.name == "session.v4.jsonl.zstd" and newf.parent.parent.name == dsh.project_key(CWD) and newf.parent.name == dsh.encode_segment(rd2["rows"][0]["dsh"]),
   "path: <projectKey>/<encodeSegment(id)>/session.v4.jsonl.zstd")
vv = dsh.validate(newf)
ok(vv["ok"] and all(ck for _, _, ck in dsh._frames(newf.read_bytes())), "written session valid; every zstd frame checksummed")
Ld = dsh.load(newf)
seqs = [e["seq"] for e in Ld["events"]]
ok(seqs == list(range(len(seqs))) and Ld["header"]["version"] == 4, "contiguous seq, format 4 header")
jd = json.dumps(Ld["events"], ensure_ascii=False)
ok("CC QUESTION" in jd and "CC ANSWER" in jd and "CC THINK" not in jd and "AAAA" not in jd and "[图片" in jd, "text only: thinking dropped, image placeholder")
ok(not any(b.get("type") in ("tool-call", "image", "reasoning") for e in Ld["events"] for b in ((e.get("data") or {}).get("message") or e.get("data") or {}).get("content") or [] if isinstance(b, dict)),
   "no tool-call / image / reasoning blocks in imported dsh session")
ok(Ld["events"][next(i for i, e in enumerate(Ld["events"]) if e["type"] in dsh.SURFACE)]["type"] == "system/message", "first surface node is an empty system message (system prompt stays at the head on resume)")
ok(any(x["id"] == rd2["rows"][0]["dsh"] and x["title"].startswith("（Claude Code）") and x["imported"] for x in S.dsh_sessions()), "imported dsh session listed with （Claude Code） title")
ok(dsh.plan_to_dsh([CSID])["rows"][0]["state"] == "same", "to-dsh idempotent: same")
with open(cf, "a", encoding="utf-8") as fh: fh.write(json.dumps(ccrec("user", "more", "u6", "u5")) + "\n")
ok(dsh.plan_to_dsh([CSID])["rows"][0]["state"] == "changed" and dsh.run_to_dsh([CSID], dry=False)["done"] == 0, "Claude changed → 'changed', never writes into the existing dsh session")
r3 = dsh.run_to_dsh([CSID], dry=False, again=True)
ok(r3["done"] == 1 and Path(r3["rows"][0]["file"]) != newf and newf.read_bytes() and tree_sha(DH).get(str(newf.relative_to(DH))) == after[str(newf.relative_to(DH))],
   "again=True → a second, new dsh session; first one untouched")
# 拒绝：重复会话号 / 第 3 版 / 压缩方式不一致
try:
    dsh.write_session(ROOT, "zstd", dict(rich.header), [{"type": "turn/start", "seq": 0, "time": 1, "data": {}}]); dup = False
except dsh.DshError:
    dup = True
ok(dup, "write_session refuses an id that already exists")
try:
    dsh.write_session(ROOT, "zstd", dict(rich.header, version=3, id="session-new-v3"), []); v3 = False
except dsh.DshError:
    v3 = True
ok(v3, "write_session refuses format != 4")
DH2 = T / "dshhome2"; os.environ["DSH_HOME"] = str(DH2)
FX.Sess("session-plain-1", CWD).write(DH2 / "sessions", compression="none")
pm = dsh.plan_to_dsh([CSID])
ok(pm["ok"] is False and "none" in (pm["error"] or "") and dsh.run_to_dsh([CSID], dry=False)["done"] == 0, "root holds uncompressed sessions but config says zstd → refused")
(DH2 / "profiles/web").mkdir(parents=True)
(DH2 / "profiles/web/cordis.patch.yml").write_text("- id: session-persistence-jsonl\n  config:\n    compression: none\n")
pn = dsh.plan_to_dsh([CSID])
ok(pn["ok"] and pn["compression"] == "none", "profile patch compression: none honoured")
rn = dsh.run_to_dsh([CSID], dry=False)
ok(rn["done"] == 1 and Path(rn["rows"][0]["file"]).name == "session.v4.jsonl" and dsh.validate(Path(rn["rows"][0]["file"]))["ok"], "uncompressed root → session.v4.jsonl, valid")
os.environ["DSH_HOME"] = str(DH)
# 撤回：dsh 正开着（锁着）的不动；没开的挪走
lk = newf.parent / "session.lock"; lk.write_text("")
if os.name == "nt":                     # dsh 在 Windows 上用不共享的句柄占着 session.lock
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True); k32.CreateFileW.restype = ctypes.c_void_p
    hnd = k32.CreateFileW(str(lk), 0x80000000, 0, None, 3, 0, None)
    def release(): k32.CloseHandle(ctypes.c_void_p(hnd))
else:                                   # POSIX：flock
    import fcntl
    fd = os.open(str(lk), os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX)
    def release(): fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
held = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(pathlib.Path(__file__).resolve().parents[2])!r}); from cc_sessionsync import dsh; from pathlib import Path; print(dsh.locked(Path({str(newf.parent)!r})))"],
                      capture_output=True, text=True, env=os.environ).stdout.strip()
ok(held == "True", "locked(): lock held by another process detected")
ud = dsh.undo(rd2["ledger"])
ok(ud["ok"] and newf.exists() and str(newf.parent) in ud["kept"], "undo keeps a session dsh has open (locked)")
release()
ok(dsh.locked(newf.parent) is False, "lock file left behind but not held → not locked (dsh leaves session.lock after exit)")
ud3 = dsh.undo(r3["ledger"])
ok(ud3["ok"] and not Path(r3["rows"][0]["file"]).exists() and ud3["moved"] >= 1, "undo moves an unlocked imported session dir to trash")

# ---------- 用量 ----------
usage.scan(full=True)
c = usage._conn()
rowsu = c.execute("SELECT model, provider, inp, cr, out, priced, sid FROM calls WHERE app='dsh' ORDER BY ts").fetchall()
c.close()
ok(len(rowsu) >= 3 and all(r[6] == SID for r in rowsu[:3]), "usage: dsh calls recorded (imported sessions skipped)")
ok(rowsu[0][:5] == ("deepseek-flash", "DeepSeek（账号登录）", 1000, 5000, 200) and rowsu[0][5] == 1, "usage: tokens mapped (uncached input / cache read / output), deepseek-flash priced")
ok(any(r[0] == "mystery-model-x" and r[5] == 0 for r in rowsu), "usage: unknown model left unpriced (shows —)")
sm = usage.summary({"app": "dsh"})
ok(sm["totals"].get("n") == len(rowsu) and "DeepSeek（账号登录）" in json.dumps(sm["providers"], ensure_ascii=False), "usage summary filters app=dsh, provider labelled")

# ---------- 备份 / 时间线 / 导出 ----------
bk = S.backup()
vs = timeline.versions("dsh", SID)
ok(vs.get("items") or vs.get("versions"), "backup + timeline versions include dsh")
items = vs.get("items") or vs.get("versions")
sid_snap = items[-1]["id"]
gr = timeline.growth()["items"]
ok(gr and gr[-1]["apps"].get("dsh", 0) > 0 and gr[-1]["total"] == sum(gr[-1]["apps"].values()), "growth curve: total messages over backups, per-app split incl. dsh")
rr = timeline.restore([sid_snap], mode="replace")
ok(rr["ok"] is False, "timeline replace refused for dsh")
rc = timeline.restore([sid_snap], mode="copy", desktop=False)
ok(rc["ok"] and rc["made"] and Path(rc["made"][0]["file"]).exists(), "timeline: dsh version → new Claude Code session")
rs = S.restore_snapshot(sid_snap)
ok(rs["ok"] and rs["how"] == "export" and str(DH) not in rs["path"], "restore_snapshot for dsh exports, never writes into DSH_HOME")
md = exports.to_md("dsh", F1.read_bytes(), {"title": "t", "cwd": CWD})
ok("DeepSeek Harness" in md and "SUMMARY of earlier talk" in md and "SYSTEM PROMPT" not in md, "export md: dsh entries, labelled, no system prompt")
ex = exports.export_many([{"agent": "dsh", "id": SID}], "md")
ok(ex.get("files"), "export_many handles dsh")
ok(S.rename("dsh", SID, "别名", write_tool=True)["errors"] and tree_sha(DH).get(str(F1.relative_to(DH))), "rename with write_tool on dsh: alias only, dsh file untouched")
ok(S.batch("delete", [{"agent": "dsh", "id": SID}])["done"] == 0 if "done" in S.batch("archive", [{"agent": "dsh", "id": SID}]) else F1.exists(), "batch archive/delete skip dsh")
ok(F1.exists(), "dsh file still there")

# ---------- 命令行 ----------
env = dict(os.environ)
cl = subprocess.run([sys.executable, "-m", "cc_sessionsync", "dsh", "validate", str(F1)], capture_output=True, text=True, env=env, cwd=str(pathlib.Path(__file__).resolve().parents[2]))
ok(cl.returncode == 0, "cli: dsh validate <file> passes " + cl.stdout[-200:] + cl.stderr[-300:])
cl2 = subprocess.run([sys.executable, "-m", "cc_sessionsync", "dsh", "detect"], capture_output=True, text=True, env=env, cwd=str(pathlib.Path(__file__).resolve().parents[2]))
ok(cl2.returncode == 0, "cli: dsh detect runs")

print("FAILS:", len(FAILS))
sys.exit(1 if FAILS else 0)

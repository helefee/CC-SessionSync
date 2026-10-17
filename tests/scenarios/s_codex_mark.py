# -*- coding: utf-8 -*-
"""--mark-codex-imported：登记进 Codex 的 external_agent_session_imports.json（临时目录）。"""
import hashlib, json, os, pathlib, subprocess, sys, tempfile, time, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="cxmark-")); H = T / "home"; H.mkdir(); (T / "noprocs.json").write_text("[]")
os.environ.update(HOME=str(H), USERPROFILE=str(H), XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"),
                  CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"), CC_SESSIONSYNC_NO_WATCHER="1")
from codex_fixture import rollout, append_turn
from cc_sessionsync import codex_import as X
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
sha = lambda f: hashlib.sha256(Path(f).read_bytes()).hexdigest()
CX, CL = H / ".codex", H / ".claude"
IMP = CX / "external_agent_session_imports.json"
def others():   # 除导入记录以外的 Codex 文件
    return {str(f.relative_to(CX)): sha(f) for f in sorted(CX.rglob("*")) if f.is_file() and f.name != IMP.name}
def cl_tree():
    return {str(f.relative_to(CL)): sha(f) for f in sorted(CL.rglob("*")) if f.is_file()}
def cli(*a):
    r = subprocess.run([sys.executable, "-m", "cc_sessionsync", *a], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ))
    return r.returncode, r.stdout + r.stderr
p1, p2 = T / "p1", T / "p2"; p1.mkdir(); p2.mkdir()
f1, t1 = rollout(CX, str(p1), title="一号"); f2, t2 = rollout(CX, str(p2), title="二号")
(CL / "projects").mkdir(parents=True)
codex_rec = {"source_path": "/somewhere/else.jsonl", "content_sha256": "ab" * 32, "imported_thread_id": str(uuid.uuid4()),
             "imported_at": 1700000000, "source_modified_at": 1, "connector_names": ["github"], "title": "Codex 自己导的"}
orig = {"records": [codex_rec], "detected_connector_records": [{"source_path": "/x", "connector_names": ["slack"]}], "future_key": 1}
IMP.write_text(json.dumps(orig, indent=2), encoding="utf-8"); orig_bytes = IMP.read_bytes()
base_others = others()
ok(X.mark_setting() is False, "setting defaults to OFF")
# --- 缺省关：不碰
r = X.run(days=0, dry=False, desktop=False)
ok(r["done"] == 2 and not r["mark"] and r["marked"] is None and IMP.read_bytes() == orig_bytes, "default off → Codex imports file untouched")
X.undo(r["ledger"])
# --- 开：演练报出来，不写
r = X.run(days=0, dry=True, desktop=False, mark=True)
ok(r["mark"] and r["mark_check"]["ok"] and r["mark_check"]["exists"] and IMP.read_bytes() == orig_bytes, "dry run with mark: reported, nothing written")
rc, out = cli("codex", "import", "--days", "0", "--no-desktop", "--mark-codex-imported")
ok(rc == 0 and "登记进 Codex 导入记录：开" in out and IMP.read_bytes() == orig_bytes, "CLI dry run shows the switch")
# --- 开：真导入
r = X.run(days=0, dry=False, desktop=False, mark=True)
ok(r["done"] == 2 and r["marked"]["ok"] and r["marked"]["marked"] == 2, f"import + mark 2 ({r.get('errors')})")
d = json.loads(IMP.read_text(encoding="utf-8")); mp = json.loads(X.MAP.read_text(encoding="utf-8"))
ok(d["records"][0] == codex_rec and d["detected_connector_records"] == orig["detected_connector_records"] and d["future_key"] == 1, "existing records / other keys preserved")
mine = {x["imported_thread_id"]: x for x in d["records"][1:]}
j1 = Path(mp[t1]["file"]); m1 = mine.get(t1, {})
ok(set(mine) == {t1, t2}, "one record per imported thread, pointing at the ORIGINAL Codex thread")
ok(m1.get("source_path") == X.codex_src_path(j1) and m1["content_sha256"] == sha(j1) and m1["source_modified_at"] == j1.stat().st_mtime_ns
   and isinstance(m1["imported_at"], int) and m1["connector_names"] == [] and m1["title"] == "（Codex）一号", f"record fields match Codex's format: {m1}")
if os.name == "nt":
    ok(m1["source_path"].startswith("\\\\?\\"), "Windows path written like Rust canonicalize (\\\\?\\C:\\...)")
else:
    ok(m1["source_path"] == os.path.realpath(j1), "POSIX path = realpath")
led = json.loads((X.LEDGERS / r["ledger"] / "ledger.json").read_text(encoding="utf-8"))
ok(led["codex_imports"]["existed"] and (X.LEDGERS / r["ledger"] / "before" / IMP.name).read_bytes() == orig_bytes and led["codex_imports"]["after_sha"] == sha(IMP),
   "original file backed up in the ledger, after-sha recorded")
ok(not [f for f in CX.iterdir() if f.name.endswith(".tmp")], "atomic write (no temp file left)")
ok(others() == base_others, "no other Codex file touched")
# 我们登记的那条不会让原线程被当成「从 Claude 导进来的」
ok(t1 not in X.imported_from_claude() and {x["thread"] for x in X.threads(days=0)} == {t1, t2}, "our own marks don't hide the original threads")
rr = X.run(days=0, dry=True, desktop=False, mark=True); ok(rr["counts"]["same"] == 2, "re-run: both 'same'")
# Codex 真把导过去那份导成一个新线程（重复的）→ 这个重复线程不会再被导回 Claude
dup = str(uuid.uuid4()); dd = json.loads(IMP.read_text(encoding="utf-8"))
dd["records"].append({"source_path": X.codex_src_path(j1), "content_sha256": "00", "imported_thread_id": dup, "imported_at": 1})
ok(dup in (lambda: (IMP.write_text(json.dumps(dd), encoding="utf-8"), X.imported_from_claude())[1])(), "a Codex-made duplicate of our file is still skipped")
d2 = json.loads(IMP.read_text(encoding="utf-8")); d2["records"].pop(); IMP.write_text(json.dumps(d2, indent=2), encoding="utf-8")
# 先把文件恢复成我们写完时的样子（上面为了测试改过），再测更新
x = json.loads((X.LEDGERS / r["ledger"] / "ledger.json").read_text(encoding="utf-8")); x["codex_imports"]["after_sha"] = sha(IMP)
(X.LEDGERS / r["ledger"] / "ledger.json").write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")
# --- Codex 那边又聊了 → 更新，记录里的 sha 跟着变，还是一条
append_turn(f2, "再问", "再答"); os.utime(f2, (time.time() + 5,) * 2)
before_upd = IMP.read_bytes()
ru = X.run(days=0, dry=False, desktop=False, mark=True)
d = json.loads(IMP.read_text(encoding="utf-8")); j2 = Path(mp[t2]["file"])
recs2 = [x for x in d["records"] if x["imported_thread_id"] == t2]
ok(ru["done"] == 1 and len(recs2) == 1 and recs2[0]["content_sha256"] == sha(j2), "update: same single record, new sha")
u = X.undo(ru["ledger"])
ok(u["ok"] and u["codex_imports"]["how"] == "restored" and IMP.read_bytes() == before_upd, "undo update → file restored byte-for-byte")
# --- 在 Claude 里接着聊过的那份：undo 时登记留着
with open(j1, "a", encoding="utf-8") as fh: fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "接着聊"}, "sessionId": j1.stem}) + "\n")
u = X.undo(r["ledger"])
d = json.loads(IMP.read_text(encoding="utf-8"))
ok(u["ok"] and u["kept"] == [str(j1)] and u["codex_imports"]["how"] == "entries", "undo with a continued session → per-entry revert")
ok(d["records"][0] == codex_rec and [x["imported_thread_id"] for x in d["records"][1:]] == [t1] and d["future_key"] == 1, "kept session keeps its mark; the other mark removed; Codex's own record kept")
j1.unlink(); X.MAP.unlink()
IMP.write_bytes(orig_bytes)
# --- 导入后 Codex 自己又加了记录 → undo 只撤我们那几条
r = X.run(days=0, dry=False, desktop=False, mark=True)
later = {"source_path": "/later.jsonl", "content_sha256": "cd" * 32, "imported_thread_id": str(uuid.uuid4()), "imported_at": 1800000000}
d = json.loads(IMP.read_text(encoding="utf-8")); d["records"].append(later); IMP.write_text(json.dumps(d, indent=2), encoding="utf-8")
u = X.undo(r["ledger"]); d = json.loads(IMP.read_text(encoding="utf-8"))
ok(u["ok"] and u["codex_imports"]["how"] == "entries" and d["records"] == [codex_rec, later], "file changed by Codex after import → undo removes only our records")
X.MAP.unlink() if X.MAP.exists() else None
# --- 文件不在 → 新建；undo 挪进回收站
IMP.unlink()
r = X.run(days=0, dry=False, desktop=False, mark=True)
d = json.loads(IMP.read_text(encoding="utf-8"))
ok(r["marked"]["ok"] and set(d) == {"records", "detected_connector_records"} and len(d["records"]) == 2 and d["detected_connector_records"] == [], "missing file → created in Codex's shape")
u = X.undo(r["ledger"])
ok(u["ok"] and u["codex_imports"]["how"] == "removed" and not IMP.exists() and list((T / "ss/trash").rglob(IMP.name.join(("replaced-", "")))), "undo → created file moved to trash")
# --- 格式不对：整个不动、报错、什么都不写
for bad, why in (("{not json", "invalid JSON"), ('{"records": {"a": 1}}', "records not a list"), ('[1, 2]', "top level not an object"), ('{"records": [1]}', "record not an object")):
    IMP.write_text(bad, encoding="utf-8"); b0, c0 = IMP.read_bytes(), cl_tree(); nled = len(X.ledgers())
    dr = X.run(days=0, dry=True, desktop=False, mark=True)
    r = X.run(days=0, dry=False, desktop=False, mark=True)
    ok(not dr["mark_check"]["ok"] and r["ok"] is False and "没有动它" in r["error"] and IMP.read_bytes() == b0 and cl_tree() == c0 and len(X.ledgers()) == nled,
       f"malformed ({why}) → refused, file and Claude dir untouched: {r.get('error', '')[:60]}")
rc, out = cli("codex", "import", "--days", "0", "--no-desktop", "--mark-codex-imported", "--apply")
ok(rc == 1 and "没有动它" in out and IMP.read_bytes() == b0, "CLI: malformed → exit 1 with a clear message")
rc, out = cli("codex", "import", "--days", "0", "--no-desktop", "--no-mark-codex-imported", "--apply")
ok(rc == 0 and "导入 2 个" in out and IMP.read_bytes() == b0, "--no-mark-codex-imported → import works, malformed file still untouched")
# --- 设置
rc, out = cli("settings", "--codex-mark-imported", "on"); ok(rc == 0 and X.mark_setting() and "开" in out, "settings on (persistent)")
IMP.unlink()
rc, out = cli("codex", "import", "--days", "0", "--no-desktop"); ok("登记进 Codex 导入记录：开" in out and not IMP.exists(), "setting used by default (dry run shows ON)")
rc, out = cli("codex", "import", "--days", "0", "--no-desktop", "--no-mark-codex-imported"); ok("登记进 Codex 导入记录：关" in out, "flag overrides the setting")
cli("settings", "--codex-mark-imported", "off"); ok(not X.mark_setting(), "settings off")
ok(others() == {k: v for k, v in others().items()} and set(base_others) <= set(others()), "other Codex files never touched (only fixture appends)")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

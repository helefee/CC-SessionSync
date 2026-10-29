# -*- coding: utf-8 -*-
"""0.4.8 大文件 / 批量导入 + 子会话：超过 200 MB 的文件能导；选文件夹；17 个一批（后台、进度、一个坏的不影响别的、取消、一笔撤回）；
内存有上限（子进程里量峰值）；导出原始 jsonl / md / html 带着子代理会话，导回来层级还在（Claude Code、Codex）；标题不加「（导入）」；
分块上传接口；桌面版选文件 / 文件夹对话框（假的 webview）；页面 / 英文。全在临时目录、假数据。"""
import json, os, subprocess, sys, tempfile, threading, time, types, urllib.request, urllib.parse, re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env, bigfake
T = Path(tempfile.mkdtemp(prefix="ccss-ibig-"))
ENV = dict(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
os.environ.update(ENV)
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import sessions as S, exports as EX, bigimport as BI, bundle, ui, nativecheck as NC
PROJ = T / "home" / ".claude" / "projects"
BIG_MB = float(os.environ.get("CCSS_BIG_MB") or 210)          # CI 里可以调

def recs(f):
    return [json.loads(x) for x in Path(f).read_text(encoding="utf-8").splitlines() if x.strip()]

# ---------- 1. 超过 200 MB 的单个文件：子进程里导入，量内存峰值 ----------
X = T / "x"; X.mkdir()
big = bigfake.write(X / "claude-big-20261029-000001.jsonl", int(BIG_MB * 1024 * 1024), seed=7)
ok(big["size"] > 200 * 1024 * 1024 or BIG_MB < 200, f"fake file {big['size'] / 2**20:.0f} MB")
child = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
from cc_sessionsync import bigimport as BI, sessions as S
def peak():
    if os.name == "nt":
        import ctypes, ctypes.wintypes as w
        class PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        m = PMC(); m.cb = ctypes.sizeof(PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(m), m.cb)
        return m.PeakWorkingSetSize / 2**20
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 2**20 if sys.platform == "darwin" else r / 1024
base = peak(); t = time.time()
sc = BI.scan([sys.argv[2]])
c = BI.count(sys.argv[2])
r = BI.run([x["file"] for x in sc["rows"]], desktop=False, rows={x["file"]: x for x in sc["rows"]})
print(json.dumps({"base": base, "peak": peak(), "secs": time.time() - t, "row": {k: sc["rows"][0].get(k) for k in ("size", "agent", "messages", "error", "type")},
                  "count": c["messages"], "made": r["made"], "errors": r["errors"], "op": r["op"]}, ensure_ascii=False))
'''
p = subprocess.run([sys.executable, "-c", child, str(ROOT), big["path"]], capture_output=True, text=True, encoding="utf-8", env=dict(os.environ), timeout=600)
try:
    out = json.loads(p.stdout.strip().splitlines()[-1])
except Exception:
    out = {}; print(p.stdout[-2000:], p.stderr[-2000:])
ok(out.get("row", {}).get("agent") == "claude" and not out["row"].get("error") and out["row"]["type"] == "main", f"{BIG_MB:.0f} MB file accepted and previewed (no size limit)")
ok(out.get("count") == big["messages"], f"streamed message count {out.get('count')} == {big['messages']}")
ok(len(out.get("made") or []) == 1 and not out["errors"], f"imported in {out.get('secs', 0):.1f}s")
LIMIT = 300
ok(out.get("peak", 1e9) - out.get("base", 0) < LIMIT, f"memory bound: peak {out.get('peak', 0):.0f} MB (start {out.get('base', 0):.0f} MB) for a {big['size'] / 2**20:.0f} MB file, limit +{LIMIT} MB")
if out.get("made"):
    m = out["made"][0]; nf = Path(m["file"])
    ok(nf.stat().st_size >= big["size"] - 1000 and m["check"]["ok"], f"output complete, native self-check ok ({m['check'].get('issues')})")
    ok(m["title"] == big["title"], f"title kept as is, no 「（导入）」 ({m['title']})")
    first = recs_head = [json.loads(x) for x in open(nf, encoding="utf-8").readlines()[:3]]
    ok(all(r.get("sessionId") == nf.stem for r in first if "sessionId" in r), "sessionId rewritten to the new id")
    u = S.undo(out["op"]); ok(u.get("ok") is not False and not nf.exists(), "undo removes the big import")
os.unlink(big["path"])

# ---------- 2. 文件夹 + 17 个一批（缩小版的同样大小比例），后台任务、进度、一个坏的不拦别的 ----------
F = T / "exports17"
files = bigfake.write_set(F, scale=0.01)
(F / "broken.jsonl").write_text('{"foo": 1}\n', encoding="utf-8")
(F / "notes.txt").write_text("x", encoding="utf-8")
(F / "sub").mkdir(); bigfake.write(F / "sub" / "deep.jsonl", 20000, seed=99)
sc = BI.scan(folder=str(F))
ok(len(sc["rows"]) == 18 and sum(1 for r in sc["rows"] if r.get("error")) == 1, "folder: 17 sessions + 1 unknown listed, .txt and sub-folder skipped (non-recursive)")
ok(len(BI.scan(folder=str(F), recursive=True)["rows"]) == 19, "recursive folder listing includes sub-folders")
ok(all(r.get("size") for r in sc["rows"]) and all(r.get("type") == "main" for r in sc["rows"] if not r.get("error")), "every row has size and type")
good = [r for r in sc["rows"] if not r.get("error")]
j = BI.start([r["file"] for r in good] + [str(F / "broken.jsonl")], desktop=False, rows=good)
seen_i = set()
for _ in range(600):
    st = BI.job(j["job"]); seen_i.add(st.get("i"))
    if st["state"] in ("done", "cancelled", "failed"):
        break
    time.sleep(0.05)
r = st["result"]
ok(st["state"] == "done" and st["n"] == 18 and st["i"] == 18, f"background batch finished: {st['state']} {st.get('i')}/{st.get('n')}")
ok(len(r["made"]) == 17 and len(r["errors"]) == 1 and "broken.jsonl" in r["errors"][0], "one bad file reported, the other 17 imported")
ok(all(m["check"]["ok"] for m in r["made"]), "all 17 pass the native self-check")
ok(sum(1 for o in S.ops_list(80) if o["id"] == r["op"]) == 1 and len(seen_i) >= 2, "one undo entry for the whole batch; progress moved")
try:
    BI.start([], desktop=False); ok(False, "empty start refused")
except ValueError:
    ok(True, "empty start refused")
S.undo(r["op"])
ok(not any(Path(m["file"]).exists() for m in r["made"]), "one undo removes the whole batch")

# ---------- 3. 取消 ----------
big2 = bigfake.write(T / "x" / "claude-cancel.jsonl", 40 * 1024 * 1024, seed=11)
BI.CHUNK_LINES = 50
j = BI.start([big2["path"]] + [g["file"] for g in good[:3]], desktop=False)
for _ in range(200):
    if BI.job(j["job"]).get("cur_done"):
        break
    time.sleep(0.01)
BI.cancel(j["job"])
for _ in range(600):
    st = BI.job(j["job"])
    if st["state"] in ("done", "cancelled", "failed"):
        break
    time.sleep(0.05)
ok(st["state"] == "cancelled" and len(st["result"]["made"]) == 0, f"cancel stops the batch ({st['state']}, made {len(st['result']['made'])})")
ok(not list(PROJ.rglob("*.ccss-tmp")), "no half-written temp file left after cancel")
led = json.loads((S.OPS / st["result"]["op"] / "ledger.json").read_text(encoding="utf-8"))
ok(led.get("cancelled") and not [s for s in led["steps"] if s["t"] == "created"], "ledger records the cancel, nothing half-made")
BI.CHUNK_LINES = 2000

# ---------- 4. 子代理：导出原始 jsonl 带着子会话，导回来层级还在 ----------
mf = bigfake.write(T / "x" / "m.jsonl", 60000, seed=21, cwd="/home/demo/proj-sub", title="有子代理的会话")
sid = mf["sid"]; pdir = PROJ / "-home-demo-proj-sub"; pdir.mkdir(parents=True, exist_ok=True)
main = pdir / f"{sid}.jsonl"; os.replace(mf["path"], main)
aids = bigfake.add_subagents(main, n=2, legacy=1)
S.invalidate()
ex = S.export("claude", sid, "jsonl")
side = Path(ex["path"] + ".ccss")
man = json.loads((side / "manifest.json").read_text(encoding="utf-8"))
ok(ex["subs"] == 3 and side.is_dir(), f"raw export bundles subagents next to the .jsonl ({ex['subs']} in {side.name})")
ok(man["format"] == "ccss-bundle/1" and man["main"]["type"] == "main" and man["main"]["id"] == sid and {c["agentId"] for c in man["children"]} == set(aids)
   and all(c["type"] == "subagent" and c["parent"] == sid for c in man["children"]), "manifest: type main/subagent, parent id, agentIds")
ok(any(c.get("agentType") == "general-purpose" and c.get("title") for c in man["children"]) and (side / "tool-results" / "toolu_big.txt").exists(), "meta files and tool-results bundled too")
sc = BI.scan([ex["path"]])
ok(sc["rows"][0]["type"] == "main" and sc["rows"][0]["subs"] == 3, "preview: main session with 3 subagents")
im = EX.import_files([ex["path"]], desktop=False, dry=False)
m = im["made"][0]; nf = Path(m["file"]); nd = nf.parent / nf.stem / "subagents"
subs = sorted(nd.glob("agent-*.jsonl"))
ok(m["subs"] == 3 and len(subs) == 3, f"import writes 3 subagent files under <new id>/subagents/ ({m['subs']})")
ok(all(r["sessionId"] == nf.stem and r["isSidechain"] and r["agentId"] in aids for s in subs for r in recs(s)), "subagent sessionId rewritten to the new id, isSidechain + agentId kept")
ok({r.get("toolUseResult", {}).get("agentId") for r in recs(nf)} - {None} == set(aids), "main session still links the same agentIds")
ok(sorted(x.name for x in nd.glob("*.meta.json")) == sorted(f"agent-{a}.meta.json" for a in aids[:2]) and (nf.parent / nf.stem / "tool-results" / "toolu_big.txt").exists(), "meta + tool-results restored")
ok(m["title"] == "有子代理的会话", f"title kept ({m['title']})")
ok(NC.check_claude(nf)["ok"], "main session passes self-check")
S.undo(im["op"]); ok(not nf.exists() and not (nf.parent / nf.stem).exists(), "undo removes the main file and its subagent folder")

# 文件夹导入：.ccss 文件夹跟着主文件走，不会单独出现
FD = T / "subfolder"; FD.mkdir()
import shutil
shutil.copy2(ex["path"], FD / Path(ex["path"]).name); shutil.copytree(side, FD / side.name)
for rec in (False, True):
    sc = BI.scan(folder=str(FD), recursive=rec)
    ok(len(sc["rows"]) == 1 and sc["rows"][0]["subs"] == 3, f"folder import ({'recursive' if rec else 'flat'}): 1 main with 3 subagents, sidecar files not listed on their own")
# 直接选 Claude 的项目目录（原始布局，递归）：主会话 + 子代理文件归到一起
sc = BI.scan(folder=str(pdir), recursive=True)
mine = [r for r in sc["rows"] if r.get("sid") == sid]
ok(len(mine) == 1 and mine[0]["subs"] == 3 and not any(r.get("type") == "subagent" for r in sc["rows"]) and sc["grouped"] >= 3,
   f"raw project folder: subagent files grouped under their main session (grouped {sc['grouped']})")
orphan = BI.scan([str(pdir / sid / "subagents" / f"agent-{aids[0]}.jsonl")])["rows"][0]
ok("子代理" in (orphan.get("error") or "") and orphan.get("type") == "subagent", "a subagent file alone is not imported as a standalone session")
both = BI.scan([str(main), str(pdir / f"agent-{aids[2]}.jsonl")])
ok(len(both["rows"]) == 1 and both["rows"][0]["subs"] == 3, "legacy top-level agent file picked together with its main: grouped, not standalone")

# md / html：子会话也藏在文件里
for fmt in ("md", "html"):
    e = EX.export_many([{"agent": "claude", "id": sid}], fmt)
    txt = Path(e["files"][0]).read_text(encoding="utf-8")
    ok(txt.count("ccss-sub") >= 3, f"{fmt} export carries subagent payloads")
    r = BI.scan([e["files"][0]])["rows"][0]
    ok(r["subs"] == 3 and r["how"] == "lossless" and "kids" not in r, f"{fmt} preview: 3 subagents, lossless")
    im = EX.import_files([e["files"][0]], desktop=False, dry=False)
    nf = Path(im["made"][0]["file"]); subs = sorted((nf.parent / nf.stem / "subagents").glob("agent-*.jsonl"))
    ok(len(subs) == 3 and all(r["sessionId"] == nf.stem and r.get("agentId") in aids for s in subs for r in recs(s)), f"{fmt} import restores 3 subagents with the new sessionId")
    ok(im["made"][0]["title"] == "有子代理的会话", f"{fmt}: title kept")
    S.undo(im["op"])

# ---------- 5. Codex 主线程 + 子代理线程 ----------
from codex_fixture import rollout
ch = T / "home" / ".codex"
pf, ptid = rollout(ch, "/work/shop", title="主线程")
cf, ctid = rollout(ch, "/work/shop", title="子代理线程", thread_source=None)
L = cf.read_text(encoding="utf-8").splitlines(); h = json.loads(L[0])
h["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": ptid, "depth": 1}}}
cf.write_text("\n".join([json.dumps(h, ensure_ascii=False)] + L[1:]) + "\n", encoding="utf-8")
S.invalidate()
ex = S.export("codex", ptid, "jsonl")
ok(ex.get("subs") == 1, f"Codex raw export bundles its subagent thread ({ex.get('subs')})")
rows = BI.scan([ex["path"], str(cf)])["rows"]
ok(len(rows) == 1 and rows[0]["agent"] == "codex" and rows[0]["subs"] == 1, "Codex: preview main + 1 subagent, child rollout grouped")
im = EX.import_files([ex["path"]], desktop=False, dry=False)
nf = Path(im["made"][0]["file"]); subs = list((nf.parent / nf.stem / "subagents").glob("agent-*.jsonl"))
ok(im["made"][0]["subs"] == 1 and len(subs) == 1 and all(r["isSidechain"] and r["sessionId"] == nf.stem for r in recs(subs[0])), "Codex subagent thread → Claude Code subagent file under the new session")
S.undo(im["op"])

# ---------- 6. 接口：分块上传 / scan / start / job / cancel ----------
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
html = urllib.request.urlopen(base + "/").read().decode()
tok = re.search(r'TOKEN = "([^"]+)"', html).group(1)
def call(path, body=None, raw=None, method=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    rq = urllib.request.Request(base + path, data=data, method=method, headers={"X-Token": tok, "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
    try:
        return json.loads(urllib.request.urlopen(rq).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
src = Path(files[3]["path"]).read_bytes()
up, off, CH = None, 0, 256 * 1024
while off < len(src):
    q = f"/api/import/upload?name=c.jsonl&off={off}" + (f"&up={up}" if up else "")
    r = call(q, raw=src[off:off + CH]); up = r["up"]; off += CH
ok(Path(r["path"]).read_bytes() == src and r["size"] == len(src), f"chunked upload: {len(src)} bytes in {(len(src) + CH - 1) // CH} raw chunks, identical on disk")
ok("断了" in call(f"/api/import/upload?name=c.jsonl&off=5&up={up}", raw=b"x").get("error", ""), "upload refuses a gap (offset mismatch)")
ok(".jsonl" in call("/api/import/upload?name=evil.exe&off=0", raw=b"x").get("error", ""), "upload refuses other file types")
mainx = side.with_name(side.name[:-len(".ccss")])                     # 浏览器里选文件夹：主文件 + 旁边的 .ccss 子会话文件夹一起上传
r = call(f"/api/import/upload?name={mainx.name}&off=0", raw=mainx.read_bytes()); up2 = r["up"]
for f in sorted(x for x in side.rglob("*") if x.is_file()):
    rr = call(f"/api/import/upload?name={urllib.parse.quote(side.name + '/' + f.relative_to(side).as_posix())}&off=0&up={up2}", raw=f.read_bytes())
ok(rr.get("ok") and BI.scan([r["path"]])["rows"][0]["subs"] == 3, "browser folder upload keeps the .ccss sidecar → 3 subagents found")
ok("只认" in call(f"/api/import/upload?name={urllib.parse.quote('x/../../evil.jsonl')}&off=0", raw=b"x").get("error", ""), "upload refuses paths outside a .ccss sidecar")
sc = call("/api/import/scan", {"folder": str(F)})
ok(len(sc["rows"]) == 18, "API scan of a folder")
c = call("/api/import/count", {"file": files[0]["path"]})
ok(c["messages"] == files[0]["messages"], "API lazy message count")
j = call("/api/import/start", {"files": [files[-1]["path"], files[-2]["path"]], "desktop": False})
for _ in range(400):
    st = call(f"/api/import/job?id={j['job']}")
    if st["state"] in ("done", "cancelled", "failed"):
        break
    time.sleep(0.05)
ok(st["state"] == "done" and len(st["result"]["made"]) == 2, "API start + job polling")
ok(call("/api/import/cancel", {"id": "nope"})["ok"] is False, "API cancel of an unknown job")
S.undo(st["result"]["op"])
srv.shutdown()

# ---------- 7. 桌面版：系统对话框（假的 webview） ----------
calls = []
class W:
    def create_file_dialog(self, kind, **kw):
        calls.append((kind, kw)); return ("/a/x.jsonl", "/a/y.md") if kind == "open" else ("/a",)
fake = types.SimpleNamespace(FileDialog=types.SimpleNamespace(OPEN="open", FOLDER="folder"), windows=[W()])
sys.modules["webview"] = fake
from cc_sessionsync import desktop
api = desktop.Api("http://127.0.0.1:1/")
ok(api.pick_import_files() == ["/a/x.jsonl", "/a/y.md"] and calls[-1][1]["allow_multiple"] and "*.jsonl" in calls[-1][1]["file_types"][0], "desktop: native multi-file dialog returns paths")
ok(api.pick_import_folder() == "/a" and calls[-1][0] == "folder", "desktop: native folder dialog")
fake2 = types.SimpleNamespace(OPEN_DIALOG=10, FOLDER_DIALOG=20, windows=[W()]); sys.modules["webview"] = fake2
calls.clear(); api.pick_import_files(); ok(calls[-1][0] == 10, "desktop: older pywebview constants work too")
ok(not [a for a in dir(api) if not a.startswith("_") and a not in ("open_browser", "open_url", "pick_import_files", "pick_import_folder")], "desktop: no extra attributes exposed to the page")
del sys.modules["webview"]

# ---------- 8. 页面 / 英文 ----------
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("pick_import_files" in page and "pick_import_folder" in page and "/api/import/upload" in page and "/api/import/start" in page, "page uses native dialogs, chunked upload, background job")
ok("200 * 1024 * 1024" not in page and "b64Bytes" not in page and "f.text()" not in page, "page no longer reads whole files / has a 200 MB limit")
ok("导入文件夹" in page and "个子会话" in page and "全选" in page and "取消导入" in page, "page: folder button, subagent count, select all, cancel")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("导入文件夹…", "取消导入", "侧边会话", "父会话不在，会显示为独立会话")), "English strings for the new import dialog")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

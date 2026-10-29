# -*- coding: utf-8 -*-
"""0.4.7 导入 .jsonl：「导出原始 jsonl」得到的文件能导回来（自动认出 Claude Code / Codex / DeepSeek Harness / Cursor / OpenCode / ZCode），
变成新的 Claude Code 会话；先预览、记账本能撤回、写完自检；认不出的给中文说明；混着 .md 一起导；（0.4.8 起不限大小）；页面 / 接口 / 英文。全在临时目录、假数据。"""
import base64, json, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-ijl-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import sessions as S, exports as EX, ir, ui

def msgs(agent, raw):
    return [(e["role"], e["text"]) for e in S.parse_bytes(agent, raw) if e["role"] in ("user", "assistant")]

def claude_texts(f):
    return [t for r, t in msgs("claude", Path(f).read_bytes())]

# ---------- Claude Code：导出原始 jsonl → 导回来 ----------
cl = sorted((T / "home" / ".claude" / "projects").glob("*/*.jsonl"))[0]
ex = S.export("claude", cl.stem, "jsonl")
ok(ex["ok"] and ex["path"].endswith(".jsonl"), "export raw jsonl (Claude)")
p = EX.parse_file(ex["path"])
ok(p["agent"] == "claude" and p["format"] == "jsonl" and p["how"] == "lossless", "Claude Code session jsonl detected")
ok(p["messages"] == len(msgs("claude", cl.read_bytes())) and p["preview"] and p["title"], f"preview: {p['messages']} messages, title {p['title']!r}")
dry = EX.import_files([ex["path"]], dry=True)
ok(dry["dry"] and dry["ok"] and "raw" not in dry["rows"][0], "dry run writes nothing, returns preview rows")
im = EX.import_files([ex["path"]], desktop=False, dry=False)
ok(im["ok"] and len(im["made"]) == 1, "imported as a new Claude Code session")
m = im["made"][0]
nf = Path(m["file"])
ok(nf.exists() and nf.stem != cl.stem and m["agent_from"] == "claude", "new session id, original untouched")
ok(claude_texts(nf) == claude_texts(cl), "Claude roundtrip: every user / assistant message identical")
ok("check" in m and isinstance(m["check"].get("issues"), list), f"native self-check ran after write ({'ok' if m['check']['ok'] else m['check']['issues'][:2]})")
op = next(o for o in S.ops_list(50) if o["id"] == im["op"])
ok(".jsonl" in op["summary"], f"undo ledger summary mentions .jsonl ({op['summary']})")
u = S.undo(im["op"])
ok(not nf.exists(), "undo removes the imported session")

# ---------- Codex rollout ----------
cx = sorted((T / "home" / ".codex" / "sessions").rglob("rollout-*.jsonl"))[0]
cid = json.loads(cx.read_text(encoding="utf-8").splitlines()[0])["payload"]["id"]
exc = S.export("codex", cid, "jsonl")
pc = EX.parse_file(exc["path"])
ok(pc["agent"] == "codex" and pc["cwd"], f"Codex rollout jsonl detected (cwd {pc['cwd']})")
imc = EX.import_files([exc["path"]], desktop=False, dry=False)
ok(imc["ok"] and imc["made"][0]["agent_from"] == "codex", "Codex rollout imported via the Codex → Claude Code converter")
want_u = [t for r, t in msgs("codex", cx.read_bytes()) if r == "user"]
got = claude_texts(imc["made"][0]["file"])
ok(want_u and all(any(w in g for g in got) for w in want_u), "Codex roundtrip: every user message is in the new Claude session")
ok(imc["made"][0]["check"]["ok"], f"Codex import passes the Claude Code self-check {imc['made'][0]['check']['issues'][:2]}")

# ---------- DeepSeek Harness（.jsonl，有 zstandard 时也试 .jsonl.zstd）----------
import dsh_fixture as DF
ds = DF.Sess("dsh-sess-1", str(T / "proj"))
ds.user("dsh 里问的第一句：帮我看看构建脚本")
ds.assistant([{"type": "text", "text": "dsh 的回答：构建脚本缺了清理步骤。"}])
(T / "x").mkdir(exist_ok=True)
fd = T / "x" / "dsh-dsh-sess-1-20261029-070000.jsonl"
fd.write_bytes(ds.bytes("none"))
pd = EX.parse_file(str(fd))
ok(pd["agent"] == "dsh" and pd["messages"] == 2, "DeepSeek Harness session jsonl detected")
from cc_sessionsync import dsh
if dsh._zstd_ok():
    fz = T / "x" / "dsh-dsh-sess-1.jsonl.zstd"
    fz.write_bytes(ds.bytes("zstd"))
    ok(EX.parse_file(str(fz))["agent"] == "dsh", "compressed DeepSeek Harness .jsonl.zstd detected")
else:
    print("SKIP zstandard not installed")

# ---------- OpenCode / ZCode 镜像（ccss-ir/1）、Cursor 镜像（ccss-cursor/1）----------
items = [{"role": "user", "kind": "text", "text": "镜像里的问题：首页白屏", "ts": 1792500000000},
         {"role": "assistant", "kind": "text", "text": "镜像里的回答：少了一个 polyfill", "ts": 1792500005000}]
for agent in ("opencode", "zcode"):
    f = T / "x" / f"{agent}-ses1.jsonl"
    f.write_bytes(ir.dumps({"agent": agent, "id": "ses1", "title": f"{agent} 的会话", "cwd": str(T / "proj")}, items))
    pa = EX.parse_file(str(f))
    ok(pa["agent"] == agent and pa["title"] == f"{agent} 的会话" and pa["messages"] == 2, f"{agent} mirror jsonl detected")
fcur = T / "x" / "cursor-c1.jsonl"
fcur.write_text(json.dumps({"type": "cursor", "format": "ccss-cursor/1", "id": "c1", "title": "Cursor 对话", "cwd": str(T / "proj"), "source": "ide"}, ensure_ascii=False) + "\n"
                + "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
ok(EX.parse_file(str(fcur))["agent"] == "cursor", "Cursor mirror jsonl detected")
ia = EX.import_files([str(fd), str(T / "x" / "opencode-ses1.jsonl"), str(T / "x" / "zcode-ses1.jsonl"), str(fcur)], desktop=False, dry=False)
ok(ia["ok"] and [x["agent_from"] for x in ia["made"]] == ["dsh", "opencode", "zcode", "cursor"], f"dsh / OpenCode / ZCode / Cursor jsonl imported {ia['errors'][:1]}")
ok(any("dsh 里问的第一句" in t for t in claude_texts(ia["made"][0]["file"])) and all(any("镜像里的问题" in t for t in claude_texts(x["file"])) for x in ia["made"][1:]),
   "their messages carried into the new Claude sessions")
ok(all(x["check"]["ok"] for x in ia["made"]), f"all pass the Claude Code self-check {[x['check']['issues'][:1] for x in ia['made']]}")
S.undo(ia["op"])

# ---------- 认不出 / 坏的 ----------
def err(f):
    try:
        EX.parse_file(str(f)); return ""
    except ValueError as e:
        return str(e)
unk = T / "x" / "random.jsonl"; unk.write_text('{"foo": 1}\n{"bar": [2]}\n', encoding="utf-8")
e1 = err(unk)
ok("认不出" in e1 and "Claude Code" in e1 and "Codex" in e1, f"unknown jsonl → clear Chinese error ({e1[:40]}…)")
junk = T / "x" / "junk.jsonl"; junk.write_text("not json at all\n", encoding="utf-8")
ok("没有能读的 JSON" in err(junk), "non-JSON lines → error")
empty = T / "x" / "noturns.jsonl"; empty.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x"}}) + "\n", encoding="utf-8")
ok("一条对话消息都没有" in err(empty), "recognised but no messages → error")
ok("只认" in err(T / "x" / "a.txt") if (T / "x" / "a.txt").write_text("x") else False, "other suffixes refused")
ok(EX.MAX_IMPORT is None and err(fd) == "", "0.4.8: no 200 MB size limit any more")

# ---------- 混在一起导 ----------
md = EX.export_many([{"agent": "claude", "id": cl.stem}], "md")["files"][0]
batch = [ex["path"], exc["path"], md, str(unk), str(fd)]
bd = EX.import_files(batch, dry=True)
ok(not bd["ok"] and sum(1 for r in bd["rows"] if "error" in r) == 1 and [r.get("agent") for r in bd["rows"] if "error" not in r] == ["claude", "codex", "claude", "dsh"],
   "mixed batch preview: 4 recognised (jsonl Claude / Codex / dsh + md), 1 error")
bi = EX.import_files(batch, desktop=False, dry=False)
ok(len(bi["made"]) == 4 and len(bi["errors"]) == 1 and "random.jsonl" in bi["errors"][0], "mixed batch import: 4 made, the unknown one reported")
ok(all(Path(x["file"]).exists() for x in bi["made"]) and bi["made"][2]["how"] == "lossless", "all written; md still lossless")
S.undo(bi["op"])
ok(not any(Path(x["file"]).exists() for x in bi["made"]), "undo the whole mixed batch")

# ---------- 接口：文字 / 二进制（base64）----------
r = ui._import({"files": [{"name": "a.jsonl", "text": Path(exc["path"]).read_text(encoding="utf-8")},
                          {"name": "b.jsonl", "b64": base64.b64encode(fd.read_bytes()).decode()}], "dry": True})
ok(r["ok"] and [x["agent"] for x in r["rows"]] == ["codex", "dsh"], "API accepts .jsonl as text and as base64")
try:
    ui._import({"files": [{"name": "c.txt", "text": "x"}], "dry": True}); ok(False, "API refuses .txt")
except ValueError as e:
    ok(".jsonl" in str(e), "API refuses other files, message lists .jsonl")

# ---------- 页面 / 英文 ----------
html = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok('accept=".md,.markdown,.html,.htm,.jsonl,.zstd"' in html and "导入 .md / .html / .jsonl" in html, "page: picker accepts .jsonl, button renamed")
ok("原始 jsonl 也能用「导入 .md / .html / .jsonl」导回来" in html and "b64Bytes" in html and "自检通过" in html, "page: export hint, binary upload, self-check shown")
en = json.loads((ROOT / "cc_sessionsync" / "i18n" / "en.json").read_text(encoding="utf-8"))
ok(all(en.get(k) for k in ("导入 .md / .html / .jsonl", "原始 jsonl ·", "自检通过", "自检提示", "看着像")), "English strings for the import dialog")

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

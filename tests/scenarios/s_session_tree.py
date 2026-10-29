# -*- coding: utf-8 -*-
"""0.4.9 会话列表按主会话 / 子会话缩进：
后端——Claude 桌面端登记里 spawnedFrom.sessionId（local_… 登记号 / 不带 local_ / 直接写 cliSessionId）→ 列表里的 parent；
Codex 子线程（session_meta.source 里的 parent_thread_id）→ parent。
前端——index.html 里的 ssNest / ssFlat（用 node 跑）：多层、父会话不在、有环、搜索带出父会话、折叠、排序。全是假数据。"""
import json, os, shutil, subprocess, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-tree-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import claude_sync as CS, sessions as S
from codex_fixture import rollout

p0 = CS.plan()
tdir = Path(p0["target"]["path"])
H = T / "home"
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)

def entry(name, title, spawned=None, fname=None, act=0):
    cli = str(uuid.uuid4())
    demo_env.claude_transcript(H / ".claude/projects/-work-tree" / f"{cli}.jsonl", cli, "/work/tree", [(title + "？", "好的。")], NOW)
    d = {"sessionId": name, "cliSessionId": cli, "title": title, "cwd": "/work/tree", "createdAt": 1760000000000 + act, "lastActivityAt": 1760000600000 + act}
    if spawned:
        d["spawnedFrom"] = spawned
    (tdir / f"{fname or name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return cli

P = entry("local_p", "副本（主）")
K1 = entry("local_k1", "侧边 1", {"name": "side", "sessionId": "local_p", "sharedFolder": "/work/tree", "sideSession": True, "taskId": None, "title": "副本（主）"})
K2 = entry("local_k2", "任务 2", {"sessionId": "local_p", "taskId": "t2", "title": "副本（主）"})
G1 = entry("local_g1", "孙会话", {"sessionId": "k1", "taskId": "t3", "title": "侧边 1"})                    # 不带 local_ 的写法
K3 = entry("local_k3", "按 cli 号指的", {"sessionId": P, "taskId": "t4"})                                 # 直接写 cliSessionId
FN = entry("local_reg9", "文件名和登记号不一样", {"sessionId": "local_p", "taskId": "t5"}, fname="local_file9")
K4 = entry("local_k4", "父会话不在", {"sessionId": "local_gone", "taskId": "t6", "title": "早没了"})
SELF = entry("local_self", "指向自己", {"sessionId": "local_self", "taskId": "t7"})
PLAIN = entry("local_plain", "普通会话")

S.invalidate()
items = {x["id"]: x for x in S.list_all("claude")["items"]}
ok(items[K1].get("parent") == "claude:" + P and items[K1].get("rel") == "side", "side session → parent via local_ registry id")
ok(items[K2].get("parent") == "claude:" + P and items[K2].get("rel") == "task", "task child → parent")
ok(items[G1].get("parent") == "claude:" + K1, "grandchild: spawnedFrom without local_ prefix resolves")
ok(items[K3].get("parent") == "claude:" + P, "spawnedFrom.sessionId = parent's cliSessionId resolves")
ok(items[FN].get("parent") == "claude:" + P, "child whose registry file name differs from its sessionId")
ok(not items[K4].get("parent") and items[K4].get("parent_missing") and items[K4].get("parent_title") == "早没了", "missing parent → parent_missing + parent_title")
ok(not items[SELF].get("parent"), "self reference ignored")
ok(not items[PLAIN].get("parent") and not items[PLAIN].get("parent_missing") and not items[P].get("parent"), "plain / parent sessions have no parent")
ok(len(items) == len({x["id"] for x in S.list_all("claude")["items"]}), "session count unchanged (children still listed)")

# Codex：父线程 / 子线程
CX = H / ".codex"
fp, tp = rollout(CX, "/work/cx", title="Codex 主线程")
fc, tc = rollout(CX, "/work/cx", title="Codex 子线程", when=datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc))
lines = fc.read_text(encoding="utf-8").splitlines()
m = json.loads(lines[0]); m["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": tp, "depth": 1}}}
fc.write_text("\n".join([json.dumps(m, ensure_ascii=False)] + lines[1:]) + "\n", encoding="utf-8")
fo, to = rollout(CX, "/work/cx", title="Codex 父线程不在", when=datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc))
lines = fo.read_text(encoding="utf-8").splitlines()
m = json.loads(lines[0]); m["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": str(uuid.uuid4())}}}
fo.write_text("\n".join([json.dumps(m, ensure_ascii=False)] + lines[1:]) + "\n", encoding="utf-8")
S.invalidate()
cx = {x["id"]: x for x in S.list_all("codex")["items"]}
ok(cx[tc].get("parent") == "codex:" + tp and cx[tc].get("rel") == "thread", "codex child thread → parent")
ok(not cx[tp].get("parent"), "codex parent has no parent")
ok(cx[to].get("parent_missing") and not cx[to].get("parent"), "codex child with missing parent")

# 前端：把 index.html 里的 ssNest / ssFlat 拿出来用 node 跑
node = shutil.which("node")
if not node:
    print("SKIP node not found: front-end tree tests skipped")
else:
    html = (ROOT / "cc_sessionsync/index.html").read_text(encoding="utf-8")
    a = html.index("const FOLD = new Set("); b = html.index("const kcHtml")
    js = """
const mem = {}; const store = {get: k => mem[k] ?? null, set: (k, v) => { mem[k] = v; }};
const skey = x => x.agent + ":" + x.id; const SS = {d: null};
""" + html[a:b] + """
const it = (id, last, parent, extra) => Object.assign({agent: "claude", id, last, parent: parent ? "claude:" + parent : undefined}, extra || {});
const out = {};
const show = roots => ssFlat(roots).map(n => "  ".repeat(n.d) + n.x.id + (n.ctx ? "*" : "") + (n.lone ? "!" : "") + (n.kids.length ? "(" + n.n + ")" : ""));
// 多层 + 排序：顶层按组里最近的时间，子会话按自己的时间
let xs = [it("P", 10), it("A", 50), it("B", 30, "P"), it("C", 40, "P"), it("D", 60, "B"), it("Q", 20)];
let all = new Map(xs.map(x => [skey(x), x]));
out.multi = show(ssNest(xs, {all}));
// 父会话被筛掉：子会话放顶层并标出来
out.missing = show(ssNest([it("B", 30, "P"), it("Q", 20), it("M", 5, null, {parent_missing: true})], {all}));
// 有环
const cyc = [it("X", 1, "Y"), it("Y", 2, "Z"), it("Z", 3, "X"), it("W", 4, "W")];
out.cycle = show(ssNest(cyc, {all: new Map(cyc.map(x => [skey(x), x]))}));
// 搜索命中孙会话：父会话、祖父会话淡色带出来；ok() 不让带的就不带
out.ctx = show(ssNest([all.get("claude:D")], {all, ctx: true}));
out.ctxNo = show(ssNest([all.get("claude:D")], {all, ctx: true, ok: p => p.id !== "P"}));
// 折叠
FOLD.add("claude:P");
out.fold = show(ssNest(xs, {all}));
out.foldSaved = JSON.parse(mem["ccss.fold"] || "[]");
foldSave(); out.foldSaved = JSON.parse(mem["ccss.fold"]);
// 大量：5000 个、深链不卡
const big = []; for (let i = 0; i < 5000; i++) big.push(it("n" + i, i, i % 50 ? "n" + (i - 1) : null));
const t0 = Date.now(); const r = ssNest(big, {all: new Map(big.map(x => [skey(x), x]))}); out.bigRoots = r.length; out.bigMs = Date.now() - t0;
console.log(JSON.stringify(out));
"""
    f = T / "tree.js"; f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8")
    ok(r.returncode == 0, "node runs the tree code " + r.stderr[-500:])
    o = json.loads(r.stdout or "{}")
    print(json.dumps(o, ensure_ascii=False))
    ok(o.get("multi") == ["P(3)", "  C", "  B(1)", "    D", "A", "Q"], "multi-level nesting; top level by newest in group, children by own time")
    ok(o.get("missing") == ["B!", "Q", "M!"], "child whose parent is filtered out / missing → top level, tagged")
    cy = o.get("cycle") or []
    ok(len(cy) == 4 and sum(1 for l in cy if not l.startswith(" ")) == 2 and "W!" in cy, "cycle broken, nothing lost, self-loop is top level")
    ok(o.get("ctx") == ["P*(2)", "  B*(1)", "    D"], "search hit on grandchild brings dimmed ancestors")
    ok(o.get("ctxNo") == ["B*!(1)", "  D"], "context parent not allowed by filter → stop there")
    ok(o.get("fold") == ["P(3)", "A", "Q"], "collapsed parent hides its children")
    ok(o.get("foldSaved") == ["claude:P"], "collapsed state saved to localStorage")
    ok(o.get("bigRoots") == 100 and o.get("bigMs", 9999) < 1500, f"5000 sessions in 100 deep chains: {o.get('bigMs')} ms")

# 页面里有用到 + 英文
html = (ROOT / "cc_sessionsync/index.html").read_text(encoding="utf-8")
ok("ssNest(xs, {ctx: ssCtxOn()" in html and "ssFlat(ssNest(vis" in html, "tree and table both nest")
en = json.loads((ROOT / "cc_sessionsync/i18n/en.json").read_text(encoding="utf-8"))
ok(all(k in en for k in ("子会话", "侧边会话", "子线程")), "en strings")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

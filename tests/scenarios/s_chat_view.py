# -*- coding: utf-8 -*-
"""0.4.9 会话详情的对话样式：后端 chatview.read 给出结构化的记录（你的话 / 回复 / 思考 / 工具调用带结果 / 子代理连到自己的记录），
大会话只留最后 limit 条；Codex 的命令和输出也对上；/api/sessions/read?rich=1 / sub= 都能用；
前端 chatHtml（用 node 跑）：气泡、Markdown（代码块高亮、列表、表格）、连着的工具合成一组、思考收起、子代理按钮、不放原始 HTML。全是假数据。"""
import json, os, shutil, subprocess, sys, tempfile, time, uuid
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-chat-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
demo_env.build(T)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import chatview as CV, sessions as S
from chat_fixture import claude_rich
from codex_fixture import rollout
H = T / "home"
PD = H / ".claude/projects/-work-chat"
sid = str(uuid.uuid4())
claude_rich(PD, sid, "/work/chat")
r = CV.read("claude", sid)
es = r["entries"]
roles = [e["role"] for e in es]
ok(r["ok"] and r["rich"], "rich read ok")
ok(roles == ["user", "thinking", "assistant", "tool", "tool", "tool", "tool", "tool", "assistant"], f"entry order {roles}")
tools = [e for e in es if e["role"] == "tool"]
ok([t["name"] for t in tools] == ["Grep", "Read", "Bash", "Edit", "Task"], "tool names")
ok(tools[0]["brief"] == "onSubmit" and tools[1]["brief"].endswith("src/login.tsx") and tools[2]["brief"] == "npm test -- login", "tool briefs (first line of command)")
ok(tools[2].get("err") and "FAIL" in tools[2]["out"] and not tools[0].get("err"), "tool results attached, error flagged")
ok("--- 原来" in tools[3]["input"] and "+++ 改成" in tools[3]["input"], "Edit input shows old / new")
ok(tools[4]["sub"] == {"type": "Explore", "desc": "检查其它表单", "agent_id": "a1b2c3d4e5f6a7b8"}, "Task call linked to subagent transcript")
ok(not any("agent_id" in e for e in es), "internal agent_id not leaked on entries")
# 子代理记录
rs = CV.read("claude", sid, sub="a1b2c3d4e5f6a7b8")
ok(rs["ok"] and [e["role"] for e in rs["entries"]] == ["user", "tool", "assistant"] and rs["entries"][1]["out"].startswith("src/signup"), "subagent transcript readable")
ok(not CV.read("claude", sid, sub="../../etc")["ok"] and not CV.read("claude", sid, sub="nope")["ok"], "bad / unknown subagent id refused")
# 没有 agentId 的老记录：按 meta 里的描述对上；两个都没有就不连
sid2 = str(uuid.uuid4()); claude_rich(PD, sid2, "/work/chat", agent_id="feedbeef00000001", result_agent_id=False)
ok([e for e in CV.read("claude", sid2)["entries"] if e["role"] == "tool"][-1]["sub"].get("agent_id") == "feedbeef00000001", "subagent matched by meta description")
sid3 = str(uuid.uuid4()); claude_rich(PD, sid3, "/work/chat", agent_id="feedbeef00000002", result_agent_id=False, with_meta=False)
ok("agent_id" not in [e for e in CV.read("claude", sid3)["entries"] if e["role"] == "tool"][-1]["sub"], "no link when nothing matches")
# 大会话：只留最后 limit 条，内存不跟着涨
sid4 = str(uuid.uuid4()); f4 = claude_rich(PD, sid4, "/work/chat", rounds=1500)
t0 = time.time(); r4 = CV.read("claude", sid4, limit=300); dt = time.time() - t0
ok(len(r4["entries"]) == 300 and r4["cut"] == r4["total"] - 300 and r4["total"] == 1500 * 9, f"big session keeps last 300 of {r4['total']} ({f4.stat().st_size // 1024} KB, {dt:.2f}s)")
ok(r4["entries"][-1]["role"] == "assistant" and "找到原因了" in r4["entries"][-1]["text"], "big session tail correct")
ok(len(CV.read("claude", sid4, limit=99999)["entries"]) == 5000, "limit capped at 5000")
# Codex：命令 + 输出 + 出错
CX = H / ".codex"
fc, tid = rollout(CX, "/work/cx", title="Codex 对话")
L = fc.read_text(encoding="utf-8").splitlines()
L.append(json.dumps({"type": "response_item", "timestamp": "2026-10-15T08:09:00Z", "payload": {"type": "function_call", "name": "shell", "call_id": "c9", "arguments": json.dumps({"command": ["bash", "-lc", "false"]})}}))
L.append(json.dumps({"type": "response_item", "timestamp": "2026-10-15T08:09:01Z", "payload": {"type": "function_call_output", "call_id": "c9", "output": json.dumps({"output": "boom", "metadata": {"exit_code": 1}})}}))
fc.write_text("\n".join(L) + "\n", encoding="utf-8")
rc = CV.read("codex", tid)
ct = [e for e in rc["entries"] if e["role"] == "tool"]
ok(rc["ok"] and any(e["role"] == "user" for e in rc["entries"]) and ct, "codex rich read")
ok(ct[0]["name"] == "exec_command" and "rg -n onSubmit" in ct[0]["brief"] and "onSubmit" in ct[0].get("out", ""), "codex command + output paired")
ok(ct[-1]["brief"] == "bash -lc false" and ct[-1].get("err") and ct[-1]["out"] == "boom", "codex failing command flagged")
ok(not any(e["role"] == "user" and e["text"].startswith("# AGENTS.md") for e in rc["entries"]), "codex boilerplate user messages skipped")
# 其它应用：老格式照旧
ok(CV.read("dsh", "nope")["ok"] is False, "other agents fall back to plain read")
# HTTP
from cc_sessionsync import ui
import re, threading, urllib.request
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
tok = re.search(r'TOKEN = "([^"]+)"', urllib.request.urlopen(base + "/").read().decode()).group(1)
def get(path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(base + path, headers={"X-Token": tok}), timeout=30).read())
g = get(f"/api/sessions/read?agent=claude&id={sid}&rich=1&limit=50")
ok(g.get("rich") and len(g["entries"]) == 9, "API rich read")
g = get(f"/api/sessions/read?agent=claude&id={sid}&sub=a1b2c3d4e5f6a7b8")
ok(g.get("ok") and g.get("sub") == "a1b2c3d4e5f6a7b8", "API subagent read")
g = get(f"/api/sessions/read?agent=claude&id={sid}&limit=50")
ok(not g.get("rich") and any(e["role"] == "tool" for e in g["entries"]), "API plain read unchanged")
srv.shutdown()

# 前端：chatHtml 用 node 跑
node = shutil.which("node")
html = (ROOT / "cc_sessionsync/index.html").read_text(encoding="utf-8")
if not node:
    print("SKIP node not found")
else:
    a = html.index("const CHAT = {seq: 0"); b = html.index("function chatFill(")
    js = """
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const ic = (n, c) => `<svg class="ic ${c || ""}" data-i="${n}"></svg>`; const logo = a => ic(a, "logo"); const when = ts => "T" + ts;
""" + html[a:b] + """
const es = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
const out = {h: chatHtml(es, "claude"), plain: chatHtml([{role: "user", text: "hi", ts: 1}, {role: "tool", text: "Bash  ls -la", ts: 2}, {role: "assistant", text: "<script>alert(1)</script> **b**", ts: 3}], "dsh"),
  empty: chatHtml([], "claude"), md: mdHtml("- a\\n- b\\n\\n1. x\\n2. y\\n\\n[l](javascript:alert(1)) [ok](https://e.x)\\n\\n<img src=x onerror=alert(1)>")};
console.log(JSON.stringify(out));
"""
    (T / "e.json").write_text(json.dumps(es, ensure_ascii=False), encoding="utf-8")
    (T / "chat.js").write_text(js, encoding="utf-8")
    p = subprocess.run([node, str(T / "chat.js"), str(T / "e.json")], capture_output=True, text=True, encoding="utf-8")
    ok(p.returncode == 0, "node runs chatHtml " + p.stderr[-600:])
    o = json.loads(p.stdout or "{}"); h = o.get("h", "")
    ok('class="cm u"' in h and 'class="cbub' in h, "user bubble")
    ok(h.count('class="chd"') == 1, "assistant name / avatar once per turn")
    ok('<details class="cthink"' in h, "thinking collapsed")
    ok('<details class="ctools">' in h and "运行了 1 个命令" in h and "读了 1 个文件" in h and "改了 1 个文件" in h and "派了 1 个子代理" in h and "1 个出错" in h, "consecutive tools grouped with summary")
    ok('data-csub="a1b2c3d4e5f6a7b8"' in h, "subagent button")
    ok('<div class="cb">' in h and '<i class="hk">const</i>' in h and '<i class="hc">// 不刷新页面</i>' in h, "code block highlighted")
    ok("<table>" in h and "<ol>" in h and "<blockquote>" in h and "<h3>找到原因了</h3>" in h, "markdown: heading / list / table / quote")
    ok('class="ccp"' in h, "copy buttons")
    pl = o.get("plain", "")
    ok('class="tnm">Bash<' in pl and "ls -la" in pl and "&lt;script&gt;" in pl and "<script>" not in pl and "<strong>b</strong>" in pl, "plain entries (other apps) render, HTML escaped")
    md = o.get("md", "")
    ok("<ul>" in md and "<ol>" in md and 'href="https://e.x"' in md and "javascript:" not in md.split("href=")[-1] and "<img" not in md, "markdown safe links, no raw HTML")
    ok("没有对话内容" in o.get("empty", ""), "empty")
ok("&rich=1" in html and "svMoreE" in html and "chatSub(" in html, "page wiring: rich read, load earlier, subagent dialog")
en = json.loads((ROOT / "cc_sessionsync/i18n/en.json").read_text(encoding="utf-8"))
ok(all(k in en for k in ("思考过程", "子代理记录", "加载更早的")), "en strings")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

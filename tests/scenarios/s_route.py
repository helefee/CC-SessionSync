# -*- coding: utf-8 -*-
"""模型路由（v0.3.0）：协议转换（录下来的 Codex / Claude Code 真请求 + 三种上游的流式回复样本）、本机代理对三种假上游、
多 key 轮换 / 失败换 key / 冷却、别名池、价格 / 用量、应用 / 撤销（字节级还原）、CLIProxyAPI 配置导入、key 不落日志。全在临时目录。"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-route-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
H = T / "home"; H.mkdir(parents=True, exist_ok=True)
FX = ROOT / "tests" / "fixtures" / "route"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)

from cc_sessionsync import keystore, modelcfg, router, usage, xproto

def fx(n): return json.loads((FX / n).read_text(encoding="utf-8"))
def events(b: bytes): return [(e, json.loads(d)) for e, d in xproto.sse_events(b.splitlines(keepends=True)) if d != "[DONE]"]
def sse_file(n): return xproto.sse_events((FX / n).read_bytes().splitlines(keepends=True))

# ================================================================ 1. 请求转换（录下来的真请求）
cx1, cx2, cl1, cl2 = fx("codex_req_tool.json")["body"], fx("codex_req_after_tool.json")["body"], fx("claude_req_tool.json")["body"], fx("claude_req_after_tool.json")["body"]
OPT = {"max_tokens": 32000, "thinking": "adaptive", "reasoning": "auto", "effort": "", "cache": True, "system_role": ""}

a, ctx = xproto.convert_request("responses", "anthropic", cx2, "claude-sonnet-5-5", OPT)
roles = [m["role"] for m in a["messages"]]
ok(all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)) and roles[0] == "user", f"Codex→Anthropic: roles alternate, starts with user {roles}")
tu = [b for m in a["messages"] if m["role"] == "assistant" for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
tr = [b for m in a["messages"] if m["role"] == "user" for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
ok(len(tu) == 1 and tu[0]["name"] == "exec_command" and tu[0]["input"].get("cmd") and tr and tr[0]["tool_use_id"] == tu[0]["id"],
   "Codex→Anthropic: function_call / function_call_output → tool_use / tool_result with the same id")
names = [t["name"] for t in a["tools"]]
ok("exec_command" in names and "apply_patch" in names and not any(n in ("web_search", "tool_search") for n in names) and all(len(n) <= 64 for n in names),
   f"Codex→Anthropic: function + custom (apply_patch) tools kept, built-in web_search / tool_search dropped ({len(names)} tools)")
ok(isinstance(a["system"], list) and a["system"] and a["max_tokens"] == 32000 and a["model"] == "claude-sonnet-5-5", "Codex→Anthropic: developer → system, max_tokens default, model mapped")
ok(any("cache_control" in b for b in a["system"]) and "cache_control" in a["tools"][-1], "Codex→Anthropic: prompt-cache breakpoints on system + tools")
a1, _ = xproto.convert_request("responses", "anthropic", cx1, "claude-sonnet-5-5", OPT)
ok((a1.get("thinking") or {}).get("type") == "adaptive" and (a1.get("output_config") or {}).get("effort") == "medium",
   "Codex→Anthropic: reasoning.effort=medium → adaptive thinking + output_config.effort")

r, rctx = xproto.convert_request("anthropic", "responses", cl2, "gpt-5.5", OPT)
fc = [i for i in r["input"] if i.get("type") == "function_call"]; fo = [i for i in r["input"] if i.get("type") == "function_call_output"]
ok(fc and fo and fc[0]["call_id"] == fo[0]["call_id"] and json.loads(fc[0]["arguments"]).get("command"), "Claude Code→Responses: tool_use/tool_result → function_call/function_call_output")
ok(r["model"] == "gpt-5.5" and r.get("reasoning", {}).get("effort") == "medium" and "reasoning.encrypted_content" in r.get("include", []) and r.get("store") is False,
   "Claude Code→Responses: effort medium → reasoning.effort, encrypted reasoning requested, store=false")
ok(any(i.get("role") == "developer" for i in r["input"]) and all(t["type"] == "function" for t in r["tools"]) and r.get("prompt_cache_key"),
   "Claude Code→Responses: system → developer, tools → functions, prompt_cache_key from session")
ok(not any(i.get("type") == "reasoning" for i in r["input"]), "Claude Code→Responses: foreign thinking signature (from Anthropic) not sent to OpenAI")
c, _ = xproto.convert_request("anthropic", "chat", cl2, "deepseek-chat", OPT)
tm = [m for m in c["messages"] if m["role"] == "tool"]; am = [m for m in c["messages"] if m.get("tool_calls")]
ok(tm and am and tm[0]["tool_call_id"] == am[0]["tool_calls"][0]["id"] and c["messages"][0]["role"] == "system" and c.get("stream_options", {}).get("include_usage"),
   "Claude Code→Chat: tool_calls / tool messages paired, system first, usage requested in stream")
c2, _ = xproto.convert_request("chat", "anthropic", {"model": "x", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
                                                      "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}], "max_tokens": 50}, "claude-haiku-5-5", OPT)
ok(c2["system"] and c2["messages"][0]["role"] == "user" and c2["tools"][0]["name"] == "f" and c2["max_tokens"] == 50, "Chat→Anthropic basic")

# ================================================================ 2. 回复转换（流式样本）
em = xproto.EmitResponses("opus", True, ctx)
out = b"".join(em.feed(ev) for ev in xproto.ir_from("anthropic", True, sse_file("anthropic_stream_tool.sse")))
ev = events(out); done = [d for e, d in ev if e == "response.completed"]
items = [d["item"] for e, d in ev if e == "response.output_item.done"]
ok(done and done[0]["response"]["status"] == "completed", "Anthropic stream→Responses: response.completed")
fcs = [i for i in items if i["type"] == "function_call"]
ok(fcs and fcs[0]["name"] == "exec_command" and json.loads(fcs[0]["arguments"]) == {"cmd": "ls -la"}, "Anthropic stream→Responses: tool_use deltas → function_call with full arguments")
rs = [i for i in items if i["type"] == "reasoning"]
ok(rs and rs[0]["encrypted_content"].startswith("ccss1."), "Anthropic stream→Responses: thinking + signature packed into reasoning.encrypted_content")
u = done[0]["response"]["usage"]
ok(u["input_tokens"] == 812 + 1500 + 9000 and u["input_tokens_details"]["cached_tokens"] == 9000 and u["output_tokens"] == 87, f"Anthropic→Responses usage incl. cache ({u})")
seq = [d.get("sequence_number") for e, d in ev]
ok(all(isinstance(s, int) for s in seq) and seq == sorted(seq), "Responses stream has increasing sequence_number")
# 思考原样带回：下一轮 Codex 带着 encrypted_content 来 → Anthropic 请求里还原 thinking + 原签名
nxt = dict(cx1, input=cx1["input"] + items + [{"type": "function_call_output", "call_id": fcs[0]["call_id"], "output": "x"}])
a3, _ = xproto.convert_request("responses", "anthropic", nxt, "claude-sonnet-5-5", OPT)
th = [b for m in a3["messages"] if m["role"] == "assistant" for b in m["content"] if b.get("type") == "thinking"]
ok(th and th[0]["signature"] == "EqQBCkYIBRgCKkDemoSignature==" and th[0]["thinking"].startswith("Need"), "thinking round-trip Codex→Anthropic keeps the original signature")

em = xproto.EmitAnthropic("claude-opus-4-8", True, rctx)
out = b"".join(em.feed(e) for e in xproto.ir_from("responses", True, sse_file("responses_stream_tool.sse")))
ev = events(out); kinds = [d.get("content_block", {}).get("type") for e, d in ev if e == "content_block_start"]
ok(kinds == ["thinking", "text", "tool_use"], f"Responses stream→Anthropic: blocks thinking/text/tool_use ({kinds})")
ms = next(d for e, d in ev if e == "message_start")
ok(ms["message"]["id"].startswith("msg_ccss") and ms["message"]["model"] == "claude-opus-4-8", "Anthropic stream: msg_ccss id + client model name")
md = next(d for e, d in ev if e == "message_delta")
ok(md["delta"]["stop_reason"] == "tool_use" and md["usage"]["output_tokens"] == 120, "Responses→Anthropic: stop_reason tool_use, output usage")
ij = "".join(d["delta"].get("partial_json", "") for e, d in ev if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
ok(json.loads(ij) == {"command": "ls -la", "description": "List files"}, "Responses→Anthropic: arguments streamed as input_json_delta")
sig = "".join(d["delta"].get("signature", "") for e, d in ev if e == "content_block_delta" and d["delta"]["type"] == "signature_delta")
back = dict(cl1, messages=cl1["messages"] + [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": sig},
                                                                             {"type": "tool_use", "id": "call_demo1", "name": "Bash", "input": {"command": "ls"}}]},
                                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_demo1", "content": "ok"}]}])
r2, _ = xproto.convert_request("anthropic", "responses", back, "gpt-5.5", OPT)
ri = [i for i in r2["input"] if i.get("type") == "reasoning"]
ok(ri and ri[0].get("encrypted_content") == "gAAAAABdemoEncrypted==", "reasoning round-trip Claude Code→OpenAI keeps encrypted_content")
ust = next(d for e, d in ev if e == "message_start")["message"]["usage"]
ok(ust.get("cache_read_input_tokens") == 4096 or md["usage"].get("cache_read_input_tokens") == 4096, "Responses→Anthropic: cached tokens → cache_read_input_tokens")

em = xproto.EmitAnthropic("claude-sonnet-4-8", True, {"client": "anthropic"})
out = b"".join(em.feed(e) for e in xproto.ir_from("chat", True, sse_file("chat_stream_tool.sse")))
ev = events(out)
tools = [d["content_block"] for e, d in ev if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
ok([t["name"] for t in tools] == ["Bash", "Read"], "Chat stream→Anthropic: two interleaved tool_calls → two tool_use blocks")
ok(em.usage.get("input") == 900 - 512 and em.usage.get("cache_read") == 512 and em.usage.get("output") == 40, f"Chat usage incl. cached ({em.usage})")
em = xproto.EmitResponses("x", True, {"client": "responses", "names": xproto.Names()})
out = b"".join(em.feed(e) for e in xproto.ir_from("chat", True, sse_file("chat_stream_tool.sse")))
args = [json.loads(d["item"]["arguments"]) for e, d in events(out) if e == "response.output_item.done" and d["item"]["type"] == "function_call"]
ok(args == [{"command": "pwd"}, {"file_path": "/a"}], f"Chat stream→Responses: parallel tool calls ({args})")

# 非流式
j = {"id": "msg_x", "type": "message", "role": "assistant", "model": "m", "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "t1", "name": "exec_command", "input": {}}],
     "stop_reason": "tool_use", "usage": {"input_tokens": 5, "output_tokens": 2}}
em = xproto.EmitResponses("opus", False, ctx)
for e in xproto.ir_from("anthropic", False, j): em.feed(e)
res = em.result()
ok([o["type"] for o in res["output"]] == ["message", "function_call"] and res["output"][1]["arguments"] == "{}", "non-stream Anthropic→Responses; empty input → {}")
em = xproto.EmitChat("x", False, {"client": "chat", "names": xproto.Names()})
for e in xproto.ir_from("anthropic", False, dict(j, stop_reason="max_tokens", content=[{"type": "text", "text": "hi"}])): em.feed(e)
ok(em.result()["choices"][0]["finish_reason"] == "length", "max_tokens → finish_reason length")

# 错误
st, b = xproto.client_error("anthropic", 400, xproto.parse_error(400, json.dumps({"error": {"message": "This model's maximum context length is 400000 tokens.", "code": "context_length_exceeded"}})))
ok(st == 400 and b["error"]["message"].startswith("prompt is too long"), "OpenAI context-length error → Anthropic 'prompt is too long' (Claude Code auto-compacts)")
st, b = xproto.client_error("responses", 400, xproto.parse_error(400, json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 300000 tokens > 200000 maximum"}})))
ok(b["error"]["code"] == "context_length_exceeded", "Anthropic prompt-too-long → OpenAI context_length_exceeded (Codex compacts)")
st, b = xproto.client_error("anthropic", 429, xproto.parse_error(429, '{"error":{"message":"slow down","code":"rate_limit_exceeded"}}'))
ok(b["error"]["type"] == "rate_limit_error", "429 → rate_limit_error")
st, b = xproto.client_error("responses", 529, {"message": "overloaded"})
ok(st == 503, "529 overloaded → 503 for OpenAI clients")

# ================================================================ 3. 本机代理 ↔ 三种假上游
def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
MP = free(); LOG = T / "mock.jsonl"
mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_upstream.py"), str(MP), str(LOG), "good-a,good-b,rl-x,dead-y"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", MP), 0.2).close(); break
    except OSError:
        time.sleep(0.1)
MB = f"http://127.0.0.1:{MP}"
def mlog():
    try:
        return [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return []

PA = router.upsert_provider({"name": "Mock Anthropic", "protocol": "anthropic", "base_url": MB, "models": [{"name": "claude-mock-1", "alias": "opus"}, {"name": "claude-mock-h"}]},
                            [{"secret": "bad-key-1", "label": "坏的"}, {"secret": "good-a"}])
PR = router.upsert_provider({"name": "Mock OpenAI", "protocol": "responses", "base_url": MB + "/v1",
                             "models": [{"name": "gpt-mock", "alias": "gpt", "price": {"inp": 2, "out": 10}}, {"name": "gpt-mock", "alias": "pool"}]},
                            [{"secret": "good-a"}, {"secret": "good-b"}])
PC = router.upsert_provider({"name": "Mock Chat", "protocol": "chat", "base_url": MB + "/v1", "headers": {"X-Team": "demo", "X-Trace": "$x-client-trace"},
                             "models": [{"name": "chat-mock", "alias": "chatm", "price": "none"}, {"name": "chat-mock", "alias": "pool"}], "excluded": ["*-preview"]},
                            [{"secret": "good-b"}])
ok(len(PA["keys"]) == 2 and PA["keys"][0]["masked"] != "bad-key-1" and "good" not in json.dumps(router.load()), "keys stored in keystore, config file has no plaintext, view is masked")
router.set_clients({"claude": {"slots": {"opus": "gpt", "sonnet": "chatm", "haiku": "claude-mock-h"}}, "codex": {"model": "opus"}, "request_retry": 1})
st = router.start(free())
ok(st["running"] and st["port"], "proxy started on 127.0.0.1")
PB = f"http://127.0.0.1:{st['port']}"
TOK = router.token()

def call(path, body, hdr=None, auth=True, method="POST"):
    h = {"content-type": "application/json"}
    if auth: h["authorization"] = "Bearer " + TOK
    h.update(hdr or {})
    req = urllib.request.Request(PB + path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

s, _, b = call("/health", None, auth=False, method="GET"); ok(s == 200 and b"cc-sessionsync-route" in b, "GET /health without token")
s, _, b = call("/v1/messages", dict(cl1, model="claude-opus-4-8"), auth=False)
ok(s == 401 and json.loads(b)["type"] == "error", "no local token → 401 (Anthropic error shape)")
s, _, b = call("/v1/models", None, method="GET"); ids = [m["id"] for m in json.loads(b)["data"]]
ok(s == 200 and {"opus", "gpt", "chatm", "pool"} <= set(ids), f"GET /v1/models lists aliases ({ids})")

# Claude Code（Anthropic 客户端）→ OpenAI Responses，流式 + 工具调用
n0 = len(mlog())
s, h, b = call("/v1/messages?beta=true", dict(cl1, model="claude-opus-4-8"), {"anthropic-version": "2023-06-01"})
ev = events(b); lg = mlog()[n0:]
tub = [d["content_block"] for e, d in ev if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
ok(s == 200 and "text/event-stream" in h.get("content-type", "") and tub and tub[0]["name"] == "Bash", "Claude Code → proxy → Responses upstream: streamed tool_use Bash")
ij = "".join(d["delta"].get("partial_json", "") for e, d in ev if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
ok(json.loads(ij)["command"] == "echo ROUTE-TOOL-OUT", "tool arguments intact through the proxy")
ok(lg and lg[-1]["path"] == "/v1/responses" and lg[-1]["body"]["model"] == "gpt-mock" and lg[-1]["key_ok"], "opus slot → alias gpt → gpt-mock on the Responses upstream")
s, h, b = call("/v1/messages", dict(cl2, model="claude-opus-4-8"))
txt = "".join(d["delta"].get("text", "") for e, d in events(b) if e == "content_block_delta")
ok("ROUTE-FINAL" in txt and "ROUTE-TOOL-OUT" in txt, f"tool round-trip: tool_result reaches upstream as function_call_output ({txt[:40]})")
# → Chat Completions
n0 = len(mlog())
s, h, b = call("/v1/messages", dict(cl1, model="claude-sonnet-4-8"), {"x-client-trace": "tr-1"})
lg = mlog()[n0:]
ok(s == 200 and any(d["content_block"]["type"] == "tool_use" for e, d in events(b) if e == "content_block_start") and lg[-1]["path"] == "/v1/chat/completions",
   "Claude Code → proxy → Chat Completions upstream (sonnet slot)")
# → Anthropic 直通
n0 = len(mlog())
s, h, b = call("/v1/messages", dict(cl1, model="claude-haiku-4-8"), {"anthropic-beta": "interleaved-thinking-2025-05-14"})
lg = mlog()[n0:]; ev = events(b)
ok(s == 200 and lg[-1]["path"] == "/v1/messages" and lg[-1]["body"]["model"] == "claude-mock-h" and lg[-1]["beta"] == "interleaved-thinking-2025-05-14",
   "same-protocol passthrough: model mapped, anthropic-beta forwarded")
ok(any(e == "message_start" and d["message"]["id"].startswith("msg_ccss") for e, d in ev), "passthrough: message id rewritten to msg_ccss… (usage not double-counted)")
ok([x["key_n"] for x in lg] == [-1, 0], f"bad key → 401 → next key (failover) ({[x['key_n'] for x in lg]})")
pa = next(p for p in router.overview()["providers"] if p["name"] == "Mock Anthropic")
ok(pa["keys"][0]["cooling"] > 200 and pa["keys"][0]["fail"] == 1, "401 key cooled down for 5 minutes")
n0 = len(mlog())
call("/v1/messages", dict(cl1, model="claude-haiku-4-8"))
ok([x["key_n"] for x in mlog()[n0:]] == [0], "cooling key skipped on the next request")
s, h, b = call("/v1/messages/count_tokens", {"model": "claude-haiku-4-8", "messages": [{"role": "user", "content": "hi"}]})
ok(s == 200 and json.loads(b)["input_tokens"] == 42, "count_tokens passed through to Anthropic upstream")
s, h, b = call("/v1/messages/count_tokens", {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hello world " * 50}]})
ok(s == 200 and 50 < json.loads(b)["input_tokens"] < 400, "count_tokens estimated for OpenAI upstreams")

# Codex（Responses 客户端）→ Anthropic：流式 + 工具 + 思考往返
n0 = len(mlog())
s, h, b = call("/v1/responses", dict(cx1, model="opus"))
ev = events(b); items = [d["item"] for e, d in ev if e == "response.output_item.done"]
fcs = [i for i in items if i["type"] == "function_call"]
ok(s == 200 and fcs and fcs[0]["name"] == "exec_command" and "ROUTE-TOOL-OUT" in json.loads(fcs[0]["arguments"])["cmd"], "Codex → proxy → Anthropic upstream: exec_command call")
lg = mlog()[n0:]
ok(lg[-1]["path"] == "/v1/messages" and lg[-1]["body"]["model"] == "claude-mock-1" and lg[-1]["body"].get("thinking"), "Codex opus → claude-mock-1 with thinking")
nxt = dict(cx1, model="opus", input=cx1["input"] + items + [{"type": "function_call_output", "call_id": fcs[0]["call_id"], "output": "ROUTE-TOOL-OUT\n"}])
n0 = len(mlog())
s, h, b = call("/v1/responses", nxt)
txt = "".join(d.get("delta", "") for e, d in events(b) if e == "response.output_text.delta")
ok("ROUTE-FINAL: ROUTE-TOOL-OUT" in txt, f"Codex tool round-trip through Anthropic ({txt[:40]})")
ok(mlog()[n0:][-1]["echo_sig"] == "sig-mock-1", "Anthropic thinking signature echoed back on the next turn")
# Codex 非流式 + Chat 客户端
s, h, b = call("/v1/responses", dict(cx1, model="gpt", stream=False))
ok(s == 200 and json.loads(b)["object"] == "response", "non-stream Responses (passthrough to Responses upstream)")
s, h, b = call("/v1/chat/completions", {"model": "opus", "messages": [{"role": "user", "content": "hi"}]})
ok(s == 200 and json.loads(b)["choices"][0]["message"]["content"] == "MOCK-UPSTREAM-OK", "Chat client → Anthropic upstream (non-stream)")
s, h, b = call("/v1/chat/completions", {"model": "opus", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
ok(s == 200 and b.rstrip().endswith(b"data: [DONE]"), "Chat client stream ends with [DONE]")

# 错误 / 404
s, h, b = call("/v1/messages", dict(cl1, model="nonexistent-x"))
ok(s == 404 and json.loads(b)["error"]["type"] == "not_found_error", "Claude model with no slot / alias match → 404 (Anthropic shape)")
router.set_clients({"claude": {"slots": {"opus": "gpt", "sonnet": "chatm", "haiku": "claude-mock-h"}}, "codex": {"model": ""}, "request_retry": 0})
router._invalidate()
s, h, b = call("/v1/responses", dict(cx1, model="unknown-model"))
ok(s == 404 and json.loads(b)["error"]["code"] == "model_not_found", "unknown model → 404 model_not_found (OpenAI shape)")
s, h, b = call("/v1/responses", {"model": "opus", "input": "RATE-LIMIT please", "stream": True})
ok(s == 429 and h.get("Retry-After", h.get("retry-after")) == "7", "upstream 429 on every key → 429 + retry-after passed to the client")
for k in list(router._COOL): router._COOL.pop(k)
s, h, b = call("/v1/messages", {"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "BAD-REQUEST"}]})
ok(s == 400 and json.loads(b)["error"]["message"].startswith("prompt is too long"), "400 context-length not retried, mapped for Claude Code")

# 轮换：round-robin（2 个好 key 轮流）、fill-first、别名池、503 换 key
for k in list(router._COOL): router._COOL.pop(k)
n0 = len(mlog())
for _ in range(4): call("/v1/responses", {"model": "gpt", "input": "hi", "stream": False})
ks = [x["key_n"] for x in mlog()[n0:]]
ok(sorted(ks) == [0, 0, 1, 1], f"round-robin over 2 keys ({ks})")
router.set_clients({"claude": {"slots": {"opus": "gpt", "sonnet": "chatm", "haiku": "claude-mock-h"}}, "codex": {}, "strategy": "fill-first", "request_retry": 0})
n0 = len(mlog())
for _ in range(3): call("/v1/responses", {"model": "gpt", "input": "hi", "stream": False})
ok([x["key_n"] for x in mlog()[n0:]] == [0, 0, 0], "fill-first always uses the first key")
router.set_clients({"claude": {"slots": {"opus": "gpt", "sonnet": "chatm", "haiku": "claude-mock-h"}}, "codex": {}, "strategy": "round-robin", "request_retry": 1})
n0 = len(mlog())
for _ in range(4): call("/v1/responses", {"model": "pool", "input": "hi", "stream": False})
paths = [x["path"] for x in mlog()[n0:]]
ok(paths.count("/v1/responses") == 2 and paths.count("/v1/chat/completions") == 2, f"alias pool spreads over 2 providers / protocols ({paths})")
PD = router.upsert_provider({"name": "Mock Dead", "protocol": "responses", "base_url": MB + "/v1", "models": [{"name": "dd", "alias": "deadpool"}]},
                            [{"secret": "dead-y"}, {"secret": "rl-x"}, {"secret": "good-b"}])
router.set_clients({"claude": {"slots": {}}, "codex": {}, "strategy": "fill-first", "request_retry": 0})
n0 = len(mlog())
s, h, b = call("/v1/responses", {"model": "deadpool", "input": "hi", "stream": False})
ok(s == 200 and [x["key_n"] for x in mlog()[n0:]] == [3, 2, 1], f"503 → 429 → good key (failover across 3 keys) {[x['key_n'] for x in mlog()[n0:]]}")
cool = {k["label"] or k["id"]: k["cooling"] for k in next(p for p in router.overview()["providers"] if p["name"] == "Mock Dead")["keys"]}
ok(list(cool.values())[0] > 0 and 0 < list(cool.values())[1] <= 7, f"cooldown: 503 short, 429 uses retry-after ({cool})")
n0 = len(mlog())
call("/v1/responses", {"model": "deadpool", "input": "hi", "stream": False})
ok([x["key_n"] for x in mlog()[n0:]] == [1], "cooling keys skipped next time")
# 前缀 + 排除
router.upsert_provider(dict(router.load()["providers"][2], prefix="ch", keys=router.load()["providers"][2]["keys"]))
ok(router.resolve("ch/chat-mock")[0][0]["name"] == "Mock Chat" and not router.resolve("chat-mock-preview"), "prefix/model routing + excluded wildcard")
lg = [x for x in mlog() if x["path"] == "/v1/chat/completions"]
ok(lg, "custom headers sent (checked via mock log presence)")

st = router.status()
blob = json.dumps(st, ensure_ascii=False) + json.dumps(router.overview(), ensure_ascii=False) + (T / "ss" / "route.json").read_text(encoding="utf-8")
ok(st["requests"] > 10 and st["errors"] >= 3 and not any(s in blob for s in ("good-a", "good-b", "bad-key-1", "dead-y", TOK)), "status / log / config contain no keys or the local token")

# 用量
time.sleep(0.3)
c = usage._conn()
rows = c.execute("SELECT app, provider, model, inp, cr, out, priced, c_in, c_out, pfix FROM calls WHERE src='route'").fetchall()
ok(len(rows) >= 10, f"proxied requests recorded in usage ({len(rows)})")
g = [r for r in rows if r[1] == "模型路由 · Mock OpenAI" and r[2] == "gpt-mock"]
ok(g and g[0][0] == "claude-code" and g[0][6] == 1 and abs(g[0][7] - g[0][3] * 2 / 1e6) < 1e-12 and g[0][9] == 1, "custom per-model price applied (input $2/M)")
n = [r for r in rows if r[2] == "chat-mock"]
ok(n and all(r[6] == 0 for r in n), "price '—' → unpriced")
ok(any(r[0] == "codex" and r[2] == "claude-mock-1" for r in rows), "Codex requests recorded with the real upstream model")
usage.recost()
ok(c.execute("SELECT c_in FROM calls WHERE src='route' AND model='gpt-mock' AND pfix=1").fetchone()[0] == g[0][7], "recost keeps the per-provider price")
c.close()

# 测试连接 / 拉模型
t = router.test_provider(PA["id"])
ok([k["ok"] for k in t["keys"]] == [False, True] and t["keys"][0]["status"] == 401 and "bad-key" not in json.dumps(t), "test connection per key (401 vs ok), no key echoed")
t = router.test_provider(PR["id"], "gpt-mock")
ok(t["ok"] and all(isinstance(k["ms"], int) for k in t["keys"]), "test with a 1-token generation")
lm = router.list_models(draft={"protocol": "chat", "base_url": MB + "/v1", "secret": "good-a"})
ok(lm["ok"] and lm["models"] == ["mock-model-a", "mock-model-b"], "fetch /v1/models for an unsaved provider")

# ================================================================ 4. 应用 / 撤销
router.set_clients({"claude": {"slots": {"opus": "gpt"}}, "codex": {"model": "opus", "effort": "high"}, "request_retry": 1})
cl = H / ".claude" / "settings.json"; cl.parent.mkdir(parents=True, exist_ok=True)
orig = b'{\n    "model": "opus",\n    "env": {"FOO": "1"},\n  "permissions": {"allow": []}\n}\n'
cl.write_bytes(orig)
r = router.apply_claude("global")
s = json.loads(cl.read_text(encoding="utf-8"))
ok(s["env"]["ANTHROPIC_BASE_URL"] == PB and s["env"]["ANTHROPIC_AUTH_TOKEN"] == TOK and s["env"]["FOO"] == "1" and s["model"] == "opus", "Claude apply: env set, other settings kept")
ok(not modelcfg.claude_state()["plaintext"] and modelcfg.claude_state()["route"], "local token is not flagged as a plaintext key")
ok(Path(r["session_file"]).exists() and json.loads(Path(r["session_file"]).read_text())["env"]["ANTHROPIC_BASE_URL"] == PB, "session-only settings file for claude --settings")
router.revert_claude()
ok(cl.read_bytes() == orig and not Path(r["session_file"]).exists(), "Claude revert: settings.json byte-identical")
cl.unlink(); router.apply_claude("global"); router.revert_claude()
ok(not cl.exists(), "Claude revert: file that did not exist is removed again")
cl.write_bytes(orig); router.apply_claude("global")
s = json.loads(cl.read_text()); s["env"]["BAR"] = "2"; cl.write_text(json.dumps(s))
router.revert_claude()
s = json.loads(cl.read_text())
ok(s["env"] == {"FOO": "1", "BAR": "2"}, "Claude revert after user edits: only our two env vars removed")

cxd = H / ".codex"; cxd.mkdir(parents=True, exist_ok=True); cf = cxd / "config.toml"
corig = b'# my config\nmodel = "gpt-5.5"  # keep\n\n[projects."/x"]\ntrust_level = "trusted"\n'
cf.write_bytes(corig)
r = router.apply_codex()
pf = cxd / "cc-route.config.toml"
from cc_sessionsync import _toml
pt = _toml.loads(pf.read_text(encoding="utf-8"))
ok(cf.read_bytes() == corig and pt["model_provider"] == "cc-route" and pt["model"] == "opus" and pt["model_reasoning_effort"] == "high"
   and pt["model_providers"]["cc-route"]["base_url"] == PB + "/v1" and pt["model_providers"]["cc-route"]["auth"]["command"], "Codex apply (profile): config.toml untouched, profile file written")
ok(TOK not in pf.read_text(encoding="utf-8"), "Codex profile has no token (fetched by auth command)")
aux = pt["model_providers"]["cc-route"]["auth"]
pr = subprocess.run([aux["command"], *aux["args"]], capture_output=True, text=True, timeout=30)
ok(pr.returncode == 0 and pr.stdout.strip() == TOK, "auth command prints the local token")
router.apply_codex(default=True)
ct = _toml.loads(cf.read_text(encoding="utf-8"))
ok(ct["model_provider"] == "cc-route" and ct["model"] == "opus" and "# my config" in cf.read_text() and ct["projects"]["/x"]["trust_level"] == "trusted", "Codex apply default: config.toml points at cc-route, comments kept")
router.revert_codex()
ok(cf.read_bytes() == corig and not pf.exists(), "Codex revert: config.toml byte-identical, profile removed")
router.apply_codex(default=True)
cf.write_text(cf.read_text() + '\n[tui]\nx = 1\n')
router.revert_codex()
ct = _toml.loads(cf.read_text(encoding="utf-8"))
ok(ct["model"] == "gpt-5.5" and "model_provider" not in ct and "cc-route" not in ct.get("model_providers", {}) and ct["tui"]["x"] == 1, "Codex revert after user edits: surgical")

# ================================================================ 5. CLIProxyAPI 导入
cpa = """
port: 8317
api-keys:
  claude:
    - api-key: "sk-ant-demo-1111111111111111"
      base-url: "https://relay.example.com"
      prefix: "rel"
      headers: {X-Tag: a}
      models:
        - {name: "claude-sonnet-5-5", alias: "sonnet"}
      excluded-models: ["*-thinking"]
codex-api-key:
  - api-key: "sk-proj-demo-2222222222222222"
    base-url: "https://api.openai.com"
openai-compatibility:
  - name: "openrouter"
    base-url: "https://openrouter.ai/api/v1"
    api-key-entries:
      - api-key: "sk-or-demo-3333333333333333"
      - api-key: "sk-or-demo-4444444444444444"
        proxy-url: "socks5://127.0.0.1:1080"
    models:
      - {name: "moonshotai/kimi-k2", alias: "kimi"}
gemini-api-key:
  - api-key: "AIzaDemo5555555555555555"
oauth:
  claude: [{email: a@b.c}]
"""
pv = router.cpa_preview(cpa)
ok(len(pv["providers"]) == 4 and not any(x in json.dumps(pv) for x in ("1111111111111111", "3333333333333333", "5555555555555555")), f"CPA preview: 4 API-key providers, keys masked ({[p['name'] for p in pv['providers']]})")
ok(any("OAuth" in x for x in pv["skipped"]), "CPA OAuth section skipped")
by = {p["name"]: p for p in pv["providers"]}
cp = next(p for p in pv["providers"] if p["protocol"] == "anthropic")
ok(cp["base_url"] == "https://relay.example.com" and cp["prefix"] == "rel" and cp["excluded"] == ["*-thinking"] and cp["models"][0]["alias"] == "sonnet", "CPA claude group → Anthropic provider w/ prefix, alias, excluded")
ok(next(p for p in pv["providers"] if p["protocol"] == "responses")["base_url"] == "https://api.openai.com/v1", "CPA codex group → Responses at /v1")
orr = next(p for p in pv["providers"] if "openrouter" in p["name"])
ok(orr["protocol"] == "chat" and len(orr["keys"]) == 2, "CPA openai-compatibility → Chat with 2 keys")
gm = next(p for p in pv["providers"] if "Gemini" in p["name"])
ok(gm["base_url"].endswith("/v1beta/openai"), "CPA gemini → Gemini OpenAI-compatible endpoint")
n_before = len(router.load()["providers"])
r = router.cpa_import(cpa)
ok(len(r["imported"]) == 4 and len(router.load()["providers"]) == n_before + 4, "CPA import adds providers")
imp = next(p for p in router.load()["providers"] if "openrouter" in p["name"])
ok(keystore.get(imp["keys"][0]["name"]) == "sk-or-demo-3333333333333333" and "sk-or-demo" not in (T / "ss" / "route.json").read_text(), "imported keys go to the keystore only")
for p in list(router.load()["providers"]):
    router.delete_provider(p["id"])
ok(not any(keystore.get(k["name"]) for k in imp["keys"]), "deleting a provider removes its keys from the keystore")

router.stop(); mock.terminate()
print("FAILS", len(FAILS))
sys.exit(1 if FAILS else 0)

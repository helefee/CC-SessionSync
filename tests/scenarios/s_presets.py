# -*- coding: utf-8 -*-
"""0.3.2：卡片能选任意供应商（预设地址 + 自定义），每次请求的模型 / 推理强度按那家的写法传过去。全部打本机假上游。"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-presets-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1", CC_SESSIONSYNC_NO_LOGIN_ITEM="1")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["PYTHONPATH"] = str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
H = T / "home"; H.mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)

from cc_sessionsync import cards, keystore, router, selfcheck
from cc_sessionsync import _toml as tomllib

def free():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
MP = free(); LOG = T / "mock.jsonl"
mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_upstream.py"), str(MP), str(LOG), "sk-ant-card-good,sk-oa-card-good"],
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

CX = H / ".codex"; CX.mkdir(parents=True)
CFG = CX / "config.toml"; AUTH = CX / "auth.json"
ORIG = b'# my codex config\nmodel = "gpt-5.5"\napproval_policy = "on-request"\n\n[features]\nweb_search = true\n\n[mcp_servers.docs]\ncommand = "npx"\nargs = ["-y", "docs-mcp"]\n'
CFG.write_bytes(ORIG); AUTH.write_bytes(b'{"auth_mode":"chatgpt","tokens":{"id_token":"x"}}')
CL = H / ".claude"; CL.mkdir(parents=True)
SET = CL / "settings.json"
SORIG = b'{\n  "env": {\n    "FOO": "1"\n  },\n  "apiKeyHelper": "my-helper.sh",\n  "permissions": {"allow": ["Bash(ls:*)"]}\n}\n'
SET.write_bytes(SORIG)
router.save(dict(router.load(), port=free()))

from cc_sessionsync import effort, presets

# ================================================================ 1. 预设：地址 / 协议
for p in presets.PRESETS:
    for cl in ("codex", "claude"):
        pr, url = presets.pick(p["id"], cl)
        ok(url.startswith("https://") and pr in ("anthropic", "responses", "chat"), f"preset {p['id']} / {cl} → {pr} {url}")
ok(presets.pick("deepseek", "claude") == ("anthropic", "https://api.deepseek.com/anthropic") and presets.pick("deepseek", "codex") == ("chat", "https://api.deepseek.com"),
   "DeepSeek: Claude Code → its Anthropic endpoint (passthrough), Codex → Chat")
ok(presets.pick("kimi", "codex", "intl") == ("responses", "https://api.moonshot.ai/v1") and presets.pick("glm", "claude", "intl")[1] == "https://api.z.ai/api/anthropic",
   "regions: Kimi intl Responses, Z.ai Anthropic")
ok(presets.pick("openai", "claude")[0] == "responses" and presets.pick("xai", "claude")[0] == "responses" and presets.pick("gemini", "claude")[0] == "chat", "Claude Code → OpenAI/xAI Responses, Gemini Chat")
for u, want in [("https://x.example.com/anthropic", "anthropic"), ("https://relay.example.com/v1/chat/completions", "chat"), ("https://relay.example.com/v1/responses", "responses"),
                ("https://generativelanguage.googleapis.com/v1beta", "chat"), ("https://relay.example.com/v1", "chat"), ("https://relay.example.com/v1/messages", "anthropic")]:
    got = presets.detect(u)
    ok(got[0] == want, f"自定义 auto-detect {u} → {got}")
ok(presets.detect("https://generativelanguage.googleapis.com/v1beta")[1] == "https://generativelanguage.googleapis.com/v1beta/openai", "Gemini native URL → its OpenAI-compatible endpoint")

# ================================================================ 2. 推理强度：每家的写法
def A(style, up, e, model, base=None):
    return effort.adapt(style, up, dict(base or {"model": model}), e, model)
ok(A("deepseek", "chat", "medium", "deepseek-v4-pro") == {"model": "deepseek-v4-pro", "thinking": {"type": "enabled"}, "reasoning_effort": "high"}, "DeepSeek medium → thinking on, high")
ok(A("deepseek", "chat", "none", "deepseek-v4-pro") == {"model": "deepseek-v4-pro", "thinking": {"type": "disabled"}}, "DeepSeek none → thinking off")
ok(A("deepseek", "chat", "max", "x")["reasoning_effort"] == "max" and A("deepseek", "chat", "minimal", "x")["reasoning_effort"] == "low", "DeepSeek max/minimal")
ok(A("glm", "chat", "xhigh", "glm-5.3")["reasoning_effort"] == "max" and A("glm", "chat", "none", "glm-5.3")["thinking"] == {"type": "enabled"}, "GLM-5.3: xhigh→max, can't turn off")
ok(A("glm", "chat", "none", "glm-5.2")["thinking"] == {"type": "disabled"} and A("glm", "chat", "medium", "glm-5.2")["reasoning_effort"] == "medium", "GLM-5.2 none → off, medium passed")
ok("reasoning_effort" not in A("glm", "chat", "high", "glm-4.7") and A("glm", "chat", "high", "glm-4.7")["thinking"] == {"type": "enabled"}, "GLM-4.7: only thinking on/off")
ok(A("kimi", "responses", "medium", "kimi-k3.1-pro")["reasoning"] == {"effort": "high"} and A("kimi", "chat", "none", "kimi-k3")["reasoning_effort"] == "low", "Kimi K3: low/high/max")
ok(A("kimi", "chat", "none", "kimi-k2.6")["thinking"] == {"type": "disabled"}, "Kimi K2.6: thinking switch")
q = A("qwen", "chat", "high", "qwen3.7-plus"); ok(q.get("enable_thinking") is True and q["thinking_budget"] == 32768, f"Qwen 3.7 high → thinking_budget 32768 ({q})")
ok(A("qwen", "chat", "none", "qwen3.7-plus") == {"model": "qwen3.7-plus", "enable_thinking": False}, "Qwen none → enable_thinking false")
ok(A("qwen", "chat", "high", "qwen3.8-max")["reasoning_effort"] == "xhigh", "Qwen 3.8 high → reasoning_effort xhigh")
ok(A("gemini", "chat", "none", "gemini-3.8-flash")["reasoning_effort"] == "low" and A("gemini", "chat", "none", "gemini-2.5-flash")["reasoning_effort"] == "none"
   and A("gemini", "chat", "xhigh", "gemini-3.8-pro")["reasoning_effort"] == "high", "Gemini: none only on 2.5 Flash, xhigh→high")
ok(A("xai", "responses", "none", "grok-4.7")["reasoning"] == {"effort": "low"} and A("xai", "responses", "max", "grok-4.7")["reasoning"] == {"effort": "xhigh"}, "Grok: none→low, max→xhigh")
ok(A("openrouter", "chat", "max", "anthropic/claude-opus-5.5")["reasoning"] == {"effort": "xhigh"}, "OpenRouter: reasoning.effort")
ok(A("openai", "chat", "high", "gpt-4.1") == {"model": "gpt-4.1"}, "OpenAI non-reasoning model: nothing added")
ok(A("deepseek", "chat", None, "deepseek-v4-pro") == {"model": "deepseek-v4-pro"}, "client didn't ask → nothing sent (provider default)")
ok(effort.requested("anthropic", {"thinking": {"type": "disabled"}}) == "none" and effort.requested("anthropic", {"thinking": {"type": "enabled", "budget_tokens": 30000}}) == "high"
   and effort.requested("responses", {"reasoning": {"effort": "xhigh"}}) == "xhigh" and effort.requested("anthropic", {}) is None, "requested() reads each client's format")
orig = {"model": "m", "reasoning": {"effort": "high", "summary": "auto"}}
effort.adapt("xai", "responses", dict(orig), "none", "grok-4.7"); ok(orig["reasoning"]["effort"] == "high", "client body not mutated (retries re-read it)")

TOK = router.token()
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def call(path, body, hdr=None):
    h = {"content-type": "application/json", "authorization": "Bearer " + TOK}; h.update(hdr or {})
    req = urllib.request.Request(router.base_url() + path, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with op.open(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
TOOL_R = [{"type": "function", "name": "get_weather", "description": "w", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}]
HIST_R = [{"role": "user", "content": [{"type": "input_text", "text": "weather?"}]},
          {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{\"city\":\"Paris\"}"},
          {"type": "function_call_output", "call_id": "call_1", "output": "sunny"}]
def rq(model, eff=None, tools=False):
    b = {"model": model, "input": HIST_R if tools else "hi", "stream": False}
    if tools: b["tools"] = TOOL_R
    if eff: b["reasoning"] = {"effort": eff}
    st, out = call("/v1/responses", b); return st, out, mlog()[-1]
AH = {"anthropic-version": "2023-06-01"}
TOOL_A = [{"name": "get_weather", "description": "w", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
HIST_A = [{"role": "user", "content": "weather?"},
          {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}}]},
          {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"}]}]
def cm(model, eff=None, thinking=True, tools=False):
    b = {"model": model, "max_tokens": 64, "messages": HIST_A if tools else [{"role": "user", "content": "hi"}]}
    if tools: b["tools"] = TOOL_A
    if thinking == "off": b["thinking"] = {"type": "disabled"}
    elif thinking: b["thinking"] = {"type": "adaptive"}
    if eff: b["output_config"] = {"effort": eff}
    st, out = call("/v1/messages", b, AH); return st, out, mlog()[-1]

# ================================================================ 3. Codex 使用 DeepSeek（Responses → Chat）
cards.save_card("codex", {"kind": "deepseek", "secret": "sk-oa-card-good", "base_url": MB, "models": ["deepseek-v4-pro", "deepseek-flash"]})
v = cards.view("codex")
ok(v["model"] == "deepseek-v4-pro" and v["protocol_used"] == "chat" and v["needs_proxy"], f"DeepSeek card: default model {v['model']}, chat, via background service")
r = cards.run("codex", "enable"); ok(r["ok"] and r["proxy_ok"], "enabled")
st, out, lg = rq("deepseek-v4-pro", "high")
ok(st == 200 and lg["path"] == "/chat/completions" and lg["body"]["model"] == "deepseek-v4-pro" and lg["body"]["reasoning_effort"] == "high" and lg["body"]["thinking"] == {"type": "enabled"},
   f"Codex effort high → DeepSeek chat thinking on + reasoning_effort high ({lg['body'].get('reasoning_effort')})")
st, out, lg = rq("deepseek-flash", "none"); ok(lg["body"]["model"] == "deepseek-flash" and lg["body"]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in lg["body"], "-m deepseek-flash, effort none → thinking off")
st, out, lg = rq("gpt-5.5", "low"); ok(lg["body"]["model"] == "deepseek-v4-pro", "Codex built-in gpt-5.5 → card default model")
st, out, lg = rq("deepseek-v4-pro", "medium", tools=True)
msgs = lg["body"]["messages"]
ok(st == 200 and any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in msgs) and any(m.get("tool_calls") for m in msgs)
   and lg["body"]["tools"][0]["function"]["name"] == "get_weather", "tool round-trip history converted to Chat tool_calls / tool messages")
cat = json.loads(op.open(urllib.request.Request(router.base_url() + "/v1/models?client_version=0.164.0", headers={"authorization": "Bearer " + TOK})).read())["models"]
lst = [m["slug"] for m in cat if m["visibility"] == "list"]
ok(lst == ["deepseek-v4-pro", "deepseek-flash"], f"Codex /model list = DeepSeek's models ({lst})")
cards.run("codex", "disable")

# ================================================================ 4. Codex 使用 Kimi（Responses 原样转发，换模型 / 强度写法）
cards.save_card("codex", {"kind": "kimi", "secret": "sk-oa-card-good", "base_url": MB + "/v1", "models": ["kimi-k3.1-pro", "kimi-k2.6"]})
cards.run("codex", "enable")
st, out, lg = rq("kimi-k3.1-pro", "medium")
ok(st == 200 and lg["path"] == "/v1/responses" and lg["body"]["reasoning"]["effort"] == "high" and lg["body"]["model"] == "kimi-k3.1-pro", "Kimi K3.1: Responses passthrough, medium → high")
st, out, lg = rq("kimi-k3.1-pro", "low", tools=True)
ok(st == 200 and lg["body"]["input"] == HIST_R and lg["body"]["reasoning"]["effort"] == "low", "tool history passed through unchanged")
cards.run("codex", "disable")

# ================================================================ 5. Claude Code 使用 GLM（Anthropic 原样转发，换模型名）
cards.save_card("claude", {"kind": "glm", "secret": "sk-ant-card-good", "base_url": MB + "/api/anthropic", "model": "glm-5.3", "sonnet_model": "glm-5.2", "small_model": "glm-4.7"})
v = cards.view("claude"); ok(v["protocol_used"] == "anthropic" and v["needs_proxy"], "GLM card for Claude Code: Anthropic endpoint via background service")
r = cards.run("claude", "enable"); ok(r["ok"], "enabled")
st, out, lg = cm("claude-sonnet-5-5", "high")
ok(st == 200 and lg["path"] == "/api/anthropic/v1/messages" and lg["body"]["model"] == "glm-5.2" and lg["body"]["output_config"] == {"effort": "high"} and lg["body"]["thinking"] == {"type": "adaptive"},
   "sonnet slot → glm-5.2, thinking/effort passed as-is")
st, out, lg = cm("claude-haiku-5-5", None, thinking=False); ok(lg["body"]["model"] == "glm-4.7", "haiku slot → small model")
st, out, lg = cm("glm-5.1", "low", tools=True); ok(st == 200 and lg["body"]["model"] == "glm-5.1" and lg["body"]["messages"] == HIST_A, "/model glm-5.1 passes through; tool round-trip unchanged")
cards.run("claude", "disable")

# ================================================================ 6. Claude Code 使用 Gemini（Anthropic → Chat）
cards.save_card("claude", {"kind": "gemini", "secret": "sk-oa-card-good", "base_url": MB + "/v1beta/openai", "model": "gemini-3.8-pro", "small_model": "gemini-3.8-flash"})
cards.run("claude", "enable")
st, out, lg = cm("claude-opus-5-5", "high")
ok(st == 200 and lg["path"] == "/v1beta/openai/chat/completions" and lg["body"]["model"] == "gemini-3.8-pro" and lg["body"]["reasoning_effort"] == "high", "opus + /effort high → Gemini chat reasoning_effort high")
st, out, lg = cm("claude-haiku-5-5", None, thinking="off"); ok(lg["body"]["model"] == "gemini-3.8-flash" and lg["body"]["reasoning_effort"] == "low", "thinking off on Gemini 3 → low (can't turn off)")
st, out, lg = cm("claude-opus-5-5", "max", tools=True)
msgs = lg["body"]["messages"]
ok(st == 200 and lg["body"]["reasoning_effort"] == "high" and any(m.get("role") == "tool" for m in msgs) and lg["body"]["tools"][0]["function"]["name"] == "get_weather",
   "tool round-trip → Chat tool messages; max → high")
cards.run("claude", "disable")

# ================================================================ 7. Claude Code 使用 Grok（Anthropic → Responses）
cards.save_card("claude", {"kind": "xai", "secret": "sk-oa-card-good", "base_url": MB + "/v1", "model": "grok-4.7"})
cards.run("claude", "enable")
st, out, lg = cm("claude-opus-5-5", "max"); ok(st == 200 and lg["path"] == "/v1/responses" and lg["body"]["reasoning"]["effort"] == "xhigh", "Grok: max → xhigh")
st, out, lg = cm("grok-4.5", None, thinking="off"); ok(lg["body"]["model"] == "grok-4.5" and lg["body"]["reasoning"]["effort"] == "low", "/model grok-4.5 passthrough, off → low")
st, out, lg = cm("claude-opus-5-5", "medium", tools=True)
ok(st == 200 and any(x.get("type") == "function_call_output" for x in lg["body"]["input"]), "tool round-trip → Responses function_call_output")
cards.run("claude", "disable")

# ================================================================ 8. 自定义：地址认协议
v = cards.save_card("codex", {"kind": "custom", "base_url": MB + "/v1/chat/completions", "secret": "sk-oa-card-good", "model": "my-model"})
ok(v["protocol"] == "chat" and v["base_url"] == MB + "/v1", f"custom URL …/chat/completions → Chat, base trimmed ({v['base_url']})")
v = cards.save_card("codex", {"base_url": MB + "/anthropic"}); ok(v["protocol"] == "anthropic", "custom …/anthropic → Anthropic")

router.shutdown_background()
mock.terminate()
print(f"\n{len(FAILS)} failed")
sys.exit(1 if FAILS else 0)

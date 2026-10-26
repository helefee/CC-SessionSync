# -*- coding: utf-8 -*-
"""端到端验证模型路由（v0.3.0）：真的 codex / claude 命令行 → 本机路由代理 → 本机假上游（tools/mock_upstream.py）。
不联网、不登录、不用真钥匙（假上游只认 up-anth / up-oai 这两个假 key）。全在临时目录（HOME / CODEX_HOME / CLAUDE_CONFIG_DIR 都指过去）。

  1. `route add` 三个供应商（Anthropic Messages / OpenAI Responses / OpenAI Chat Completions，key 从环境变量读）
  2. `route serve` 起代理；`route apply codex` 写 cc-route profile；`codex exec --profile cc-route` 让假模型调一次 shell 工具
     → 工具结果回到假 Anthropic 上游 → 最终回复里有 ROUTE-FINAL: ROUTE-TOOL-OUT
  3. `route apply claude` 改 settings.json；`claude -p` 让假模型调一次 Bash → 上游是 OpenAI Responses，再换成 Chat Completions 各来一遍
  4. 用量里有路由记的行（真实上游模型名）；会话日志扫描不重复算
  5. `route revert` 后 config.toml / settings.json 一个字节都没变

    python tools/verify_route.py [codex 可执行文件] [claude 可执行文件] [端口基数]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def wait_port(port, t=10.0):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    codex = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("codex") or "codex"
    codex = shutil.which(codex) or codex
    claude = (sys.argv[2] if len(sys.argv) > 2 else "") or shutil.which("claude") or "claude"
    claude = shutil.which(claude) or claude
    base = int(sys.argv[3]) if len(sys.argv) > 3 else 8780
    up, rp = base, base + 1
    T = Path(tempfile.mkdtemp(prefix="route-verify-"))
    H, CH, CL = T / "home", T / "home" / ".codex", T / "home" / ".claude"
    CH.mkdir(parents=True)
    CL.mkdir(parents=True)
    proj = T / "proj"
    proj.mkdir()
    cx_orig = b'# user config (must survive byte-identical)\napproval_policy = "never"\n\n[projects."/somewhere"]\ntrust_level = "trusted"\n'
    (CH / "config.toml").write_bytes(cx_orig)
    cl_orig = b'{\n  "permissions": {\n    "allow": []\n  },\n  "env": {\n    "DISABLE_AUTOUPDATER": "1"\n  }\n}\n'
    (CL / "settings.json").write_bytes(cl_orig)
    (H / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True}), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_", "OPENAI", "ANTHROPIC", "XDG_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
               CLAUDE_CONFIG_DIR=str(CL), CODEX_HOME=str(CH), CC_SESSIONSYNC_HOME=str(T / "ss"),
               CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_WATCHER="1", CC_SESSIONSYNC_NO_USAGE="1",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1", RUST_LOG="error", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
               UP_ANTH="up-anth", UP_OAI="up-oai", DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    log = T / "mock.jsonl"
    procs = [subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_upstream.py"), str(up), str(log), "up-anth,up-oai"])]
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m, flush=True)
        c or fails.append(m)

    def ccss(*args, check=True):
        r = subprocess.run([sys.executable, "-m", "cc_sessionsync", *args], cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        if check and r.returncode:
            print(r.stdout[-1500:], r.stderr[-1500:])
        return r

    def mlog():
        try:
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    try:
        ok(wait_port(up), "mock upstream up")
        U = f"http://127.0.0.1:{up}"
        r1 = ccss("route", "add", "--name", "Mock Anthropic", "--protocol", "anthropic", "--base-url", U, "--model", "claude-mock-1=opus", "--key-env", "UP_ANTH")
        r2 = ccss("route", "add", "--name", "Mock OpenAI", "--protocol", "responses", "--base-url", U + "/v1", "--model", "gpt-mock=gpt", "--key-env", "UP_OAI")
        r3 = ccss("route", "add", "--name", "Mock Chat", "--protocol", "chat", "--base-url", U + "/v1", "--model", "chat-mock=chatm", "--key-env", "UP_OAI")
        ok(all(r.returncode == 0 for r in (r1, r2, r3)) and "up-anth" not in r1.stdout + r2.stdout + r3.stdout, "route add ×3 (keys from env, output masked)")
        r = ccss("route", "clients", json.dumps({"claude": {"slots": {"opus": "gpt", "sonnet": "gpt", "haiku": "gpt", "default": "gpt"}},
                                                 "codex": {"model": "opus", "effort": "medium"}, "port": rp}))
        ok(r.returncode == 0, "route clients")
        sv = subprocess.Popen([sys.executable, "-m", "cc_sessionsync", "route", "serve"], cwd=str(ROOT), env=env)
        procs.append(sv)
        ok(wait_port(rp, 20), f"route serve listening on {rp}")
        r = ccss("route", "list")
        print(r.stdout)
        ok("up-anth" not in r.stdout and "up-oai" not in r.stdout, "route list masks keys")

        # ---- Codex → Anthropic
        r = ccss("route", "apply", "codex")
        ok(r.returncode == 0 and (CH / "cc-route.config.toml").exists() and (CH / "config.toml").read_bytes() == cx_orig,
           "apply codex: profile written, config.toml untouched")
        n0 = len(mlog())
        c = subprocess.run([codex, "exec", "--profile", "cc-route", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                            "USE-TOOL: run the shell command the tool asks for"], cwd=str(proj), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240)
        lg = mlog()[n0:]
        print(c.stdout[-800:], c.stderr[-1200:])
        ok(c.returncode == 0 and "ROUTE-FINAL" in c.stdout + c.stderr and "ROUTE-TOOL-OUT" in c.stdout + c.stderr,
           f"codex exec --profile cc-route: tool round-trip through Anthropic upstream ({c.returncode})")
        ok(lg and all(x["path"] == "/v1/messages" and x["key_ok"] for x in lg) and any(x["kind"] == "tool" for x in lg) and len(lg) >= 2,
           f"upstream saw Anthropic Messages requests with the right key ({[x['path'] for x in lg]})")
        ok(any(x["body"].get("model") == "claude-mock-1" for x in lg), "Codex model opus → claude-mock-1")

        # ---- Claude Code → OpenAI Responses / Chat
        r = ccss("route", "apply", "claude")
        st = json.loads((CL / "settings.json").read_text(encoding="utf-8"))
        ok(r.returncode == 0 and st["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{rp}" and st["env"]["DISABLE_AUTOUPDATER"] == "1",
           "apply claude: settings.json env points at the proxy, other env kept")
        for alias, path in (("gpt", "/v1/responses"), ("chatm", "/v1/chat/completions")):
            ccss("route", "clients", json.dumps({"claude": {"slots": {"opus": alias, "sonnet": alias, "haiku": alias, "default": alias}}}))
            time.sleep(0.2)                  # 代理按 route.json 的修改时间重读配置
            n0 = len(mlog())
            c = subprocess.run([claude, "-p", "USE-TOOL: run the echo command", "--allowedTools", "Bash(echo:*)"], cwd=str(proj), env=env,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
            lg = mlog()[n0:]
            print(c.stdout[-600:], c.stderr[-1200:])
            ok(c.returncode == 0 and "ROUTE-FINAL: ROUTE-TOOL-OUT" in c.stdout, f"claude -p → proxy → {path}: Bash tool round-trip ({c.returncode})")
            ok(lg and any(x["path"] == path and x["key_ok"] and x["kind"] == "tool" for x in lg), f"upstream saw {path} with the right key")

        # ---- 用量
        time.sleep(1)
        db = sqlite3.connect(str(T / "ss" / "usage.sqlite"))
        rows = db.execute("SELECT app, provider, model FROM calls WHERE src='route'").fetchall()
        db.close()
        ok(any(a == "codex" and m == "claude-mock-1" for a, p, m in rows) and any(a == "claude-code" and m == "gpt-mock" for a, p, m in rows)
           and any(m == "chat-mock" for a, p, m in rows), f"usage rows recorded by the proxy ({len(rows)})")
        r = ccss("usage")
        db = sqlite3.connect(str(T / "ss" / "usage.sqlite"))
        dup = db.execute("SELECT count(*) FROM calls WHERE src='log'").fetchone()[0]
        db.close()
        ok(r.returncode == 0 and dup == 0, f"log scanner skips proxied sessions (no double counting, {dup} log rows)")
        r = ccss("selfcheck", "--offline", "--json", check=False)
        items = {i["id"]: i for i in json.loads(r.stdout)["items"]}
        ok(items.get("route", {}).get("status") == "pass" and items.get("route-keys", {}).get("status") == "pass", "selfcheck: route proxy + keys pass")

        # ---- 撤销
        r1, r2 = ccss("route", "revert", "codex"), ccss("route", "revert", "claude")
        ok((CH / "config.toml").read_bytes() == cx_orig and not (CH / "cc-route.config.toml").exists(), "revert codex: byte-identical, profile removed")
        ok((CL / "settings.json").read_bytes() == cl_orig, "revert claude: settings.json byte-identical")

        # ================= 0.3.1 卡片：普通 codex（不带 --profile）→ 后台代理（没预先起，取口令时自己拉起）→ 假 Anthropic
        sv.terminate()
        sv.wait(10)
        procs.remove(sv)
        r = ccss("route", "card", "codex", "set", json.dumps({"kind": "claude", "base_url": U, "model": "claude-opus-5-5", "key_env": "UP_ANTH",
                                                                  "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]}))
        ok(r.returncode == 0, "card codex set (Claude API key via env, relay base URL = mock)")
        r = ccss("route", "card", "codex", "enable")
        print(r.stdout[-400:])
        ok(r.returncode == 0 and json.loads(r.stdout)["proxy_ok"], "card codex enable: config.toml default provider + background proxy")
        cfg = (CH / "config.toml").read_text(encoding="utf-8")
        ok('model_provider = "cc-route"' in cfg and "approval_policy" in cfg and "up-anth" not in cfg, "config.toml: default provider set, user keys kept, no key")
        ccss("route", "stop")
        ok(not wait_port(rp, 1), "background proxy stopped (simulates: app closed, no login item)")
        n0 = len(mlog())
        c = subprocess.run([codex, "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                            "USE-TOOL: run the shell command the tool asks for"], cwd=str(proj), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240)
        lg = mlog()[n0:]
        print(c.stdout[-800:], c.stderr[-1200:])
        ok(c.returncode == 0 and "ROUTE-FINAL" in c.stdout + c.stderr and "ROUTE-TOOL-OUT" in c.stdout + c.stderr,
           f"plain `codex exec` (no --profile): proxy auto-started by the token helper, tool round-trip via Anthropic ({c.returncode})")
        ok(lg and all(x["path"] == "/v1/messages" and x["key_ok"] for x in lg) and any(x["kind"] == "tool" for x in lg)
           and all(x["body"].get("model") == "claude-opus-5-5" for x in lg), f"upstream: Anthropic Messages, card key, card model ({len(lg)} requests)")
        ok("model_reasoning_effort" not in cfg, "no default effort picked → config.toml has no model_reasoning_effort (Codex decides)")

        # Codex 自己切推理等级 / 模型（-c model_reasoning_effort=…、-m …，和 /model 里选是一回事）
        dm = subprocess.run([codex, "debug", "models"], cwd=str(proj), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        try:
            listed = [m["slug"] for m in json.loads(dm.stdout)["models"] if m.get("visibility") == "list"]
        except Exception:
            listed = []
        print("codex /model list:", listed)
        ok(listed[:3] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"], "codex model catalog (what /model shows) = the card's Claude models, built-in GPT hidden")
        for args, want_model, want in ((["-c", "model_reasoning_effort=high"], "claude-opus-5-5", ("adaptive", "high")),
                                        (["-m", "claude-sonnet-5-5", "-c", "model_reasoning_effort=low"], "claude-sonnet-5-5", ("adaptive", "low")),
                                        (["-m", "claude-haiku-5-5", "-c", "model_reasoning_effort=none"], "claude-haiku-5-5", None)):
            n0 = len(mlog())
            c = subprocess.run([codex, "exec", *args, "--skip-git-repo-check", "say hi"], cwd=str(proj), env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=240)
            lg = mlog()[n0:]
            b = lg[-1]["body"] if lg else {}
            got = ((b.get("thinking") or {}).get("type"), (b.get("output_config") or {}).get("effort")) if b.get("thinking") else None
            ok(c.returncode == 0 and b.get("model") == want_model and got == want, f"codex exec {' '.join(args)} → {b.get('model')} thinking={got}")
        sh = json.loads(ccss("route", "card", "codex", "show").stdout)
        ok(sh["enabled"] and sh["proxy_running"] and (sh["login_item"] or os.name != "nt"), f"card show: enabled, proxy running, login item {sh['login_item']}")

        # ---- 0.3.2：Codex 使用 DeepSeek（Responses → Chat Completions），工具来回 + 每次请求的模型 / 推理强度
        r = ccss("route", "card", "codex", "set", json.dumps({"kind": "deepseek", "base_url": U, "key_env": "UP_OAI", "model": "",
                                                                  "models": ["deepseek-v4-pro", "deepseek-flash"]}))
        r = ccss("route", "card", "codex", "enable")
        ok(r.returncode == 0 and json.loads(r.stdout)["proxy_ok"], "card codex → DeepSeek preset (base URL = mock) enabled")
        n0 = len(mlog())
        c = subprocess.run([codex, "exec", "-c", "model_reasoning_effort=high", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                            "USE-TOOL: run the shell command the tool asks for"], cwd=str(proj), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=240)
        lg = mlog()[n0:]
        print(c.stdout[-500:], c.stderr[-800:])
        ok(c.returncode == 0 and "ROUTE-TOOL-OUT" in c.stdout + c.stderr and lg and all(x["path"] == "/chat/completions" for x in lg) and any(x["kind"] == "tool" for x in lg),
           f"codex exec → DeepSeek Chat Completions: tool round-trip ({c.returncode}, {[x['path'] for x in lg]})")
        ok(all(x["body"].get("model") == "deepseek-v4-pro" and x["body"].get("reasoning_effort") == "high" and x["body"].get("thinking") == {"type": "enabled"} for x in lg),
           f"-c model_reasoning_effort=high → reasoning_effort high + thinking on {[(x['body'].get('model'), x['body'].get('reasoning_effort')) for x in lg]}")
        n0 = len(mlog())
        c = subprocess.run([codex, "exec", "-m", "deepseek-flash", "-c", "model_reasoning_effort=none", "--skip-git-repo-check", "say hi"], cwd=str(proj), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        b = (mlog()[n0:] or [{}])[-1].get("body") or {}
        ok(c.returncode == 0 and b.get("model") == "deepseek-flash" and b.get("thinking") == {"type": "disabled"} and "reasoning_effort" not in b,
           f"codex exec -m deepseek-flash -c model_reasoning_effort=none → {b.get('model')} thinking {b.get('thinking')}")
        dm = subprocess.run([codex, "debug", "models"], cwd=str(proj), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        try:
            listed = [m["slug"] for m in json.loads(dm.stdout)["models"] if m.get("visibility") == "list"]
        except Exception:
            listed = []
        ok(listed[:2] == ["deepseek-v4-pro", "deepseek-flash"], f"codex /model list = DeepSeek models ({listed})")

        # ---- Claude Code 使用 OpenAI API Key
        r = ccss("route", "card", "claude", "set", json.dumps({"kind": "openai", "base_url": U + "/v1", "model": "gpt-5.5", "small_model": "gpt-5.4-mini",
                                                                   "sonnet_model": "gpt-5.4", "key_env": "UP_OAI"}))
        r = ccss("route", "card", "claude", "enable")
        ok(r.returncode == 0 and json.loads(r.stdout)["proxy"], "card claude (OpenAI key) enable")
        n0 = len(mlog())
        c = subprocess.run([claude, "-p", "USE-TOOL: run the echo command", "--allowedTools", "Bash(echo:*)"], cwd=str(proj), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        lg = mlog()[n0:]
        print(c.stdout[-600:], c.stderr[-1200:])
        ok(c.returncode == 0 and "ROUTE-FINAL: ROUTE-TOOL-OUT" in c.stdout, f"claude -p with the OpenAI-key card: Bash tool round-trip ({c.returncode})")
        ok(lg and all(x["path"] == "/v1/responses" and x["key_ok"] for x in lg) and any(x["kind"] == "tool" for x in lg)
           and any(x["body"].get("model") == "gpt-5.5" for x in lg), f"upstream: OpenAI Responses, card key, card model {[x['body'].get('model') for x in lg]}")
        for slot, want in (("sonnet", "gpt-5.4"), ("haiku", "gpt-5.4-mini"), ("gpt-5.2", "gpt-5.2")):
            n0 = len(mlog())
            c = subprocess.run([claude, "-p", "say hi", "--model", slot], cwd=str(proj), env=dict(env, CLAUDE_CODE_EFFORT_LEVEL="high"),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
            lg = [x for x in mlog()[n0:] if x["path"] == "/v1/responses"]
            main = [x for x in lg if x["body"].get("model") == want]
            eff = [(x["body"].get("reasoning") or {}).get("effort") for x in main]
            print(f"claude --model {slot}:", [x["body"].get("model") for x in lg], eff)
            ok(c.returncode == 0 and main and "high" in eff, f"claude -p --model {slot} → {want} (effort {eff})")

        # ---- 0.3.2：Claude Code 使用 Gemini（Anthropic → Chat Completions）
        r = ccss("route", "card", "claude", "set", json.dumps({"kind": "gemini", "base_url": U + "/v1beta/openai", "model": "gemini-3.8-pro",
                                                                   "small_model": "gemini-3.8-flash", "sonnet_model": "", "key_env": "UP_OAI"}))
        r = ccss("route", "card", "claude", "enable")
        ok(r.returncode == 0, "card claude → Gemini preset enabled")
        n0 = len(mlog())
        c = subprocess.run([claude, "-p", "USE-TOOL: run the echo command", "--allowedTools", "Bash(echo:*)", "--model", "sonnet"], cwd=str(proj),
                           env=dict(env, CLAUDE_CODE_EFFORT_LEVEL="low"), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        lg = [x for x in mlog()[n0:] if x["path"].endswith("/chat/completions")]
        main = [x for x in lg if x["body"].get("model") == "gemini-3.8-pro"]
        print(c.stdout[-400:], c.stderr[-800:], [(x["body"].get("model"), x["body"].get("reasoning_effort")) for x in lg])
        ok(c.returncode == 0 and "ROUTE-FINAL: ROUTE-TOOL-OUT" in c.stdout and any(x["kind"] == "tool" for x in main),
           f"claude -p --model sonnet → Gemini Chat Completions: Bash tool round-trip ({c.returncode})")
        ok(main and all(x["body"].get("reasoning_effort") == "low" for x in main), "CLAUDE_CODE_EFFORT_LEVEL=low → reasoning_effort low")
        n0 = len(mlog())
        c = subprocess.run([claude, "-p", "say hi", "--model", "gemini-3.7-pro"], cwd=str(proj), env=dict(env, CLAUDE_CODE_EFFORT_LEVEL="high"),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
        main = [x for x in mlog()[n0:] if x["body"].get("model") == "gemini-3.7-pro"]
        ok(c.returncode == 0 and main and main[-1]["body"].get("reasoning_effort") == "high", "claude --model gemini-3.7-pro → passed through, effort high")

        # ---- 停用：字节还原
        r1, r2 = ccss("route", "card", "codex", "disable"), ccss("route", "card", "claude", "disable")
        ok(r1.returncode == 0 and (CH / "config.toml").read_bytes() == cx_orig and not (CH / "cc-route.config.toml").exists(), "card codex disable: config.toml byte-identical")
        ok(r2.returncode == 0 and (CL / "settings.json").read_bytes() == cl_orig, "card claude disable: settings.json byte-identical")
        time.sleep(1.5)
        sh = json.loads(ccss("route", "card", "codex", "show").stdout)
        ok(not sh["proxy_running"] and not sh["login_item"], "after disabling both: background proxy stopped, login item removed")
    finally:
        ccss("route", "stop", check=False)
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except Exception:
                p.kill()
    print("FAILS", len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

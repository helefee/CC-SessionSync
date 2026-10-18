# -*- coding: utf-8 -*-
"""端到端验证 DeepSeek Harness（dsh）⇄ Claude Code 导入。全部在临时目录（HOME / DSH_HOME / CLAUDE_CONFIG_DIR 都指过去），
模型全是本机的假服务，不联网、不用登录、不用任何真钥匙。

  1. dsh → Claude Code：造一个 dsh 第 4 版会话（压缩替换 / 思考 / 工具 / 图片 / 运行时上下文都有）→ `cc-sessionsync dsh to-claude --apply`
     → 真的 `claude -p --resume` 打开（tools/mock_anthropic.py），检查发给 API 的历史：有导入的内容、压缩前的原话不在、系统提示 / 思考不在；
     新的一轮写回同一个文件；再 resume 一次。
  2. Claude Code → dsh（设了 DSH_BIN 才跑）：造一个 Claude Code 会话 → `cc-sessionsync dsh from-claude --apply` → 离线校验
     → 真的 dsh 命令行 `dsh --profile headless --session-id <id>` 接着聊（tools/mock_openai.py；临时 DSH_HOME 里的 profile
     把模型指到假服务、关掉会话日志上传 / 遥测）→ 检查假模型收到了导入的历史、dsh 把新的一轮接着写进同一个文件且还能通过校验；再 resume 一次。

    python tools/verify_dsh.py [claude 可执行文件] [端口]          DSH_BIN=/path/to/dsh  NODE_DIR=/path/to/node/bin（可选）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
MARK = "DSH-VERIFY-9c1e"
PATCH = """# 临时 DSH_HOME：本地假模型，关掉所有往外发的插件
- id: llm-pi-ai
  config:
    providers:
      mock:
        displayName: Mock
        api: openai-completions
        baseURL: http://127.0.0.1:{port}/v1
        apiKeyEnv: MOCK_KEY
        models:
          - id: mock-1
            name: Mock 1
            contextWindow: 128000
- id: agent-default-model
  config:
    provider: mock
    model: mock-1
- id: session-log-deepseek
  disabled: true
- id: otel
  disabled: true
- id: session-telemetry-otel
  disabled: true
"""


def wait_port(port):
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    claude = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("claude") or "claude"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8799
    T = Path(tempfile.mkdtemp(prefix="dsh-verify-"))
    H, proj, DH = T / "home", T / "proj", T / "dshhome"
    H.mkdir()
    proj.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "DSH_", "CC_SESSIONSYNC_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(T / "appdata"), LOCALAPPDATA=str(T / "localappdata"),
               XDG_CONFIG_HOME=str(T / "xdg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), DSH_HOME=str(DH),
               CC_SESSIONSYNC_HOME=str(T / "ss"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"),
               ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY="sk-ant-dummy-for-local-mock",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
               DSH_TELEMETRY_MODE="OFF", MOCK_KEY="mock-not-a-key", PYTHONPATH=str(ROOT))
    (T / "procs.json").write_text("[]")
    os.environ.update({k: env[k] for k in ("DSH_HOME", "HOME", "CC_SESSIONSYNC_HOME")})
    import dsh_fixture as FX
    nw = 0x08000000 if os.name == "nt" else 0
    fails = []

    def run(args, **kw):
        return subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=nw, **kw)

    # ---------- 1. dsh → Claude Code ----------
    sid = "session-9c1e0000-1111-4222-8333-444444444444"
    s = FX.rich(sid, str(proj), DH)
    s.flush()
    s.user(f"{MARK} 最后一句：把测试也跑一下")
    s.assistant([{"type": "text", "text": "测试都过了"}])
    s.write(DH / "sessions")
    r = run([sys.executable, "-m", "cc_sessionsync", "dsh", "to-claude", "--apply", "--no-desktop"])
    print("to-claude exit", r.returncode, r.stderr[-800:])
    mp = json.loads((T / "ss" / "dsh-import-map.json").read_text(encoding="utf-8"))
    rec = mp[f"dsh2cc:{sid}"]
    csid, jf = rec["session"], Path(rec["file"])
    n0 = len(jf.read_text(encoding="utf-8").splitlines())
    log = T / "anthropic.log"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_anthropic.py"), str(port), str(log)], creationflags=nw)
    try:
        wait_port(port)
        ver = run([claude, "--version"], timeout=60)
        print("claude", ver.stdout.strip())
        c = run([claude, "-p", "上一步做了什么？一句话。", "--resume", csid], cwd=proj, timeout=180, stdin=subprocess.DEVNULL)
        print("resume exit", c.returncode, "stdout:", c.stdout.strip()[-300:], "stderr:", c.stderr.strip()[-600:])
        if c.returncode != 0 or "MOCK-OK" not in c.stdout:
            fails.append("claude --resume did not open the imported dsh session")
        reqs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        main_req = [q for q in reqs if q["path"].startswith("/v1/messages") and "count_tokens" not in q["path"]
                    and MARK in json.dumps(q["body"].get("messages", []), ensure_ascii=False)]
        if not main_req:
            fails.append("imported dsh history not sent to the Claude API")
        else:
            msgs = main_req[-1]["body"]["messages"]
            blob = json.dumps(msgs, ensure_ascii=False)
            print(f"Claude API request: {len(msgs)} messages, roles {[m['role'] for m in msgs][:12]}")
            for want in (MARK, "SUMMARY of earlier talk", "NEW QUESTION", "调用了 bash", "file-a", "FINAL ANSWER", "上一步做了什么"):
                if want not in blob:
                    fails.append(f"missing in Claude API history: {want}")
            for bad in ("OLD QUESTION", "SYSTEM PROMPT", "RUNTIME CONTEXT", "THINKING hidden"):
                if bad in blob:
                    fails.append(f"should not be in Claude API history: {bad}")
        n1 = len(jf.read_text(encoding="utf-8").splitlines())
        print(f"Claude session file lines {n0} → {n1}")
        if n1 <= n0:
            fails.append("new turn not appended to the imported Claude session")
        c2 = run([claude, "-p", "再说一次", "--resume", csid], cwd=proj, timeout=180, stdin=subprocess.DEVNULL)
        if c2.returncode != 0 or "MOCK-OK" not in c2.stdout:
            fails.append("second claude resume failed")
    finally:
        mock.kill()

    # ---------- 2. Claude Code → dsh（要真的 dsh 命令行）----------
    dsh_bin = os.environ.get("DSH_BIN")
    if dsh_bin:
        dsh_bin = shutil.which(dsh_bin, path=(os.environ.get("NODE_DIR", "") + os.pathsep + os.environ.get("PATH", ""))) or dsh_bin
    if not dsh_bin:
        print("DSH_BIN not set: skipped the live dsh resume (offline validation of written sessions is in tests/scenarios/s_dsh.py)")
    else:
        if os.environ.get("NODE_DIR"):
            env["PATH"] = os.environ["NODE_DIR"] + os.pathsep + env.get("PATH", "")
        oport = port + 1
        cc_dir = H / ".claude" / "projects" / "-verify-cc"
        cc_dir.mkdir(parents=True, exist_ok=True)
        ccsid = "c0c0c0c0-1111-4222-8333-444444444444"
        recs = [
            {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": ccsid, "cwd": str(proj), "timestamp": "2026-10-18T06:00:00.000Z",
             "message": {"role": "user", "content": f"{MARK} 帮我看一下 README"}},
            {"type": "assistant", "uuid": "u2", "parentUuid": "u1", "sessionId": ccsid, "cwd": str(proj), "timestamp": "2026-10-18T06:00:05.000Z",
             "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
                         "content": [{"type": "thinking", "thinking": "CC THINK"}, {"type": "text", "text": "README 里缺安装步骤"},
                                     {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}},
            {"type": "user", "uuid": "u3", "parentUuid": "u2", "sessionId": ccsid, "cwd": str(proj), "timestamp": "2026-10-18T06:00:06.000Z",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "# demo"}]}},
            {"type": "assistant", "uuid": "u4", "parentUuid": "u3", "sessionId": ccsid, "cwd": str(proj), "timestamp": "2026-10-18T06:00:09.000Z",
             "message": {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5", "content": [{"type": "text", "text": "补好了安装步骤"}]}}]
        (cc_dir / f"{ccsid}.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in recs), encoding="utf-8")
        prof = DH / "profiles" / "headless"
        prof.mkdir(parents=True, exist_ok=True)
        (prof / "cordis.patch.yml").write_text(PATCH.format(port=oport), encoding="utf-8")
        r = run([sys.executable, "-m", "cc_sessionsync", "dsh", "from-claude", "--only", ccsid, "--apply"])
        print("from-claude exit", r.returncode, r.stderr[-800:])
        mp = json.loads((T / "ss" / "dsh-import-map.json").read_text(encoding="utf-8"))
        rec = mp.get(f"cc2dsh:{ccsid}") or {}
        did, df = rec.get("dsh"), Path(rec.get("file") or T / "missing")
        from cc_sessionsync import dsh
        v = dsh.validate(df) if df.exists() else {"ok": False, "errors": ["not written"]}
        print("written dsh session valid:", v["ok"], v["errors"][:3])
        if not v["ok"]:
            fails.append("written dsh session fails validation")
        ev0 = v.get("events")
        olog = T / "openai.log"
        om = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_openai.py"), str(oport), str(olog)], creationflags=nw)
        try:
            wait_port(oport)
            for i, prompt in enumerate(("接着说：下一步做什么？", "再说一次")):
                d = run([dsh_bin, "--profile", "headless", "--session-id", did, prompt], cwd=proj, timeout=240, stdin=subprocess.DEVNULL)
                print(f"dsh resume {i + 1} exit", d.returncode, "stdout:", d.stdout.strip()[-200:], "stderr:", d.stderr.strip()[-600:])
                if d.returncode != 0 or "MOCK-REPLY" not in d.stdout:
                    fails.append(f"dsh resume {i + 1} failed")
            reqs = [json.loads(l) for l in olog.read_text(encoding="utf-8").splitlines()] if olog.exists() else []
            hit = [q for q in reqs if MARK in json.dumps(q.get("msgs"), ensure_ascii=False)]
            if not hit:
                fails.append("imported Claude history not sent to the dsh model")
            else:
                roles = [m["role"] for m in hit[0]["msgs"]]
                blob = json.dumps(hit[0]["msgs"], ensure_ascii=False)
                print(f"dsh model request: {len(roles)} messages, roles {roles}")
                if roles[0] != "system":
                    fails.append("system prompt not at the head of the dsh request")
                if "README 里缺安装步骤" not in blob or "CC THINK" in blob:
                    fails.append("dsh history content wrong")
            v2 = dsh.validate(df)
            print(f"after resume: events {ev0} → {v2.get('events')}, valid {v2['ok']}")
            if not v2["ok"] or (v2.get("events") or 0) <= (ev0 or 0):
                fails.append("dsh did not append to the same session / file no longer valid")
        finally:
            om.kill()
    print("RESULT", "OK" if not fails else "FAIL", fails, "\ntemp:", T)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

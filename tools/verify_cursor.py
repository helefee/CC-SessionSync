# -*- coding: utf-8 -*-
"""端到端验证 Cursor → Claude Code 导入。全部在临时目录（HOME / CLAUDE_CONFIG_DIR / CURSOR_USER_DIR / CURSOR_CONFIG_DIR 都指过去），
模型是本机的假服务（tools/mock_anthropic.py），不联网、不用登录、不用任何真钥匙。

  造 Cursor 的三种数据（编辑器 state.vscdb 的对话、agent 记录、cursor-agent 的 store.db）→ `cc-sessionsync cursor to-claude --apply`
  → 每个都真的 `claude -p --resume` 打开，检查发给 API 的历史：有导入的内容、工具变成文字、思考 / Cursor 塞的上下文不在；
  新的一轮写回同一个文件；最后确认 Cursor 的数据一个字节都没变。

    python tools/verify_cursor.py [claude 可执行文件] [端口]
"""
from __future__ import annotations

import hashlib
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


def wait_port(port):
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)


def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    claude = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("claude") or "claude"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8796
    T = Path(tempfile.mkdtemp(prefix="cursor-verify-"))
    H, CU = T / "home", T / "cursor-user"
    CH = H / ".cursor"
    H.mkdir()
    from cc_sessionsync import dsh
    proj = Path(dsh.long_path(str(T / "proj")))
    proj.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "CURSOR_", "CC_SESSIONSYNC_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(T / "appdata"), LOCALAPPDATA=str(T / "localappdata"),
               XDG_CONFIG_HOME=str(T / "xdg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"),
               CURSOR_USER_DIR=str(CU), CURSOR_CONFIG_DIR=str(CH), CC_SESSIONSYNC_HOME=str(T / "ss"),
               CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "procs.json"),
               ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY="sk-ant-dummy-for-local-mock",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1", PYTHONPATH=str(ROOT))
    (T / "procs.json").write_text("[]")
    import cursor_fixture as F
    from cc_sessionsync import cursor as C
    wid = F.ws_storage(CU, str(proj))
    main_c = F.rich(CU, str(proj), wid, marker="CURSOR-IDE-MARK").write(CU)
    nh = F.Composer(name="只有 agent 记录", cwd=str(proj))
    nh.write(CU, headers=False)
    F.transcript(CH, C.slug(str(proj)), nh.cid, [("user", "CURSOR-TR-MARK 加个开关"), ("assistant", ("tool", "Shell", {"command": "ls"})),
                                                 ("assistant", "加好了")])
    F.cli_store(CH, str(proj), [{"role": "user", "content": [{"type": "text", "text": "<user_info>INJECTED ENV</user_info>"}]},
                                {"role": "user", "content": [{"type": "text", "text": "<user_query>CURSOR-CLI-MARK 跑测试</user_query>"}]},
                                {"role": "assistant", "content": [{"type": "reasoning", "text": "CLI THINK hidden"},
                                                                   {"type": "tool-call", "toolCallId": "t1", "toolName": "Shell", "args": {"command": "npm test"}}]},
                                {"role": "tool", "content": [{"type": "tool-result", "toolCallId": "t1", "toolName": "Shell", "result": "3 passed"}]},
                                {"role": "assistant", "content": [{"type": "text", "text": "测试都过了"}]}], name="跑测试")
    before = tree(CU) | {"~" + k: v for k, v in tree(CH).items()}
    nw = 0x08000000 if os.name == "nt" else 0
    fails = []

    def run(args, **kw):
        return subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=nw, **kw)

    r = run([sys.executable, "-m", "cc_sessionsync", "cursor", "to-claude", "--apply", "--no-desktop"])
    print("to-claude exit", r.returncode, r.stderr[-800:])
    mp = json.loads((T / "ss" / "cursor-import-map.json").read_text(encoding="utf-8"))
    print("imported", len(mp))
    if len(mp) != 3:
        fails.append(f"expected 3 imported conversations, got {len(mp)}")
    log = T / "anthropic.log"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_anthropic.py"), str(port), str(log)], creationflags=nw)
    try:
        wait_port(port)
        print("claude", run([claude, "--version"], timeout=60).stdout.strip())
        for mark, want, bad in (("CURSOR-IDE-MARK", ["调用了 run_terminal_command_v2", "FINAL ANSWER", "（出错）"], ["THINKING hidden"]),
                                ("CURSOR-TR-MARK", ["调用了 Shell", "加好了"], []),
                                ("CURSOR-CLI-MARK", ["3 passed", "测试都过了"], ["CLI THINK hidden", "INJECTED ENV"])):
            rec = next(v for v in mp.values() if mark in Path(v["file"]).read_text(encoding="utf-8"))
            jf = Path(rec["file"])
            n0 = len(jf.read_text(encoding="utf-8").splitlines())
            log.unlink(missing_ok=True)
            c = run([claude, "-p", "上一步做了什么？一句话。", "--resume", rec["session"]], cwd=proj, timeout=180, stdin=subprocess.DEVNULL)
            print(mark, "resume exit", c.returncode, "stdout:", c.stdout.strip()[-200:], "stderr:", c.stderr.strip()[-400:])
            if c.returncode != 0 or "MOCK-OK" not in c.stdout:
                fails.append(f"{mark}: claude --resume failed")
            reqs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            hit = [q for q in reqs if q["path"].startswith("/v1/messages") and "count_tokens" not in q["path"]
                   and mark in json.dumps(q["body"].get("messages", []), ensure_ascii=False)]
            if not hit:
                fails.append(f"{mark}: imported history not sent to the Claude API")
            else:
                msgs = hit[-1]["body"]["messages"]
                blob = json.dumps(msgs, ensure_ascii=False)
                print(f"  API request: {len(msgs)} messages, roles {[m['role'] for m in msgs][:10]}")
                fails += [f"{mark}: missing {w}" for w in want if w not in blob]
                fails += [f"{mark}: should not contain {b}" for b in bad if b in blob]
            n1 = len(jf.read_text(encoding="utf-8").splitlines())
            print(f"  Claude session file lines {n0} → {n1}")
            if n1 <= n0:
                fails.append(f"{mark}: new turn not appended")
    finally:
        mock.kill()
    after = tree(CU) | {"~" + k: v for k, v in tree(CH).items()}
    if after != before:
        fails.append("Cursor data changed")
    print("Cursor data unchanged:", after == before)
    print("RESULT", "OK" if not fails else "FAIL", fails, "\ntemp:", T)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""端到端验证 任意 → OpenCode（真的 opencode 命令行）。全在临时目录（HOME / XDG_* 都指过去），模型是本机的假服务
（tools/mock_openai.py，OpenAI 兼容），不联网、不登录、不用任何真钥匙。

  造一个 Claude Code 会话和一个 ZCode 会话（tests/oc_fixture.py）→ `cc-sessionsync convert run --to opencode --apply`
  → `opencode session list` 里有它们 → `opencode run -s <id>` 真的接着聊：假模型收到的历史里有导入的内容（工具变成文字）
  → 新的一轮写进同一个会话（本程序读回来看得到）→ `convert undo` 用 `opencode session delete` 删掉、列表里没了。
  1.x（opencode-ai）和 2.x（@opencode/cli）的命令行都认（看 --version）。

    python tools/verify_opencode.py <opencode 可执行文件> [端口]
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
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def wait_port(port):
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    oc = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("opencode") or "opencode"
    oc = shutil.which(oc) or oc                     # Windows：npm 装的是 opencode.cmd
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8797
    T = Path(tempfile.mkdtemp(prefix="oc-verify-"))
    H = T / "home"
    H.mkdir()
    proj = T / "proj"
    proj.mkdir()
    (proj / "README.md").write_text("demo\n")
    xdg = {k: str(T / v) for k, v in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"))}
    (T / "cfg" / "opencode").mkdir(parents=True)
    (T / "cfg" / "opencode" / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled", "model": "mock/mock-1",
        "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock", "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "x"},
                              "models": {"mock-1": {"name": "Mock 1"}}}}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_", "OPENCODE", "XDG_"))}
    env.update(xdg, HOME=str(H), USERPROFILE=str(H), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
               CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"),
               CC_SESSIONSYNC_OPENCODE_DATA=str(T / "data" / "opencode"), CC_SESSIONSYNC_ZCODE_HOME=str(H / ".zcode"),
               CC_SESSIONSYNC_OPENCODE_CLI=oc, CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_WATCHER="1", OPENCODE_DISABLE_AUTOUPDATE="1",
               OPENCODE_DISABLE_LSP_DOWNLOAD="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    log = T / "mock.jsonl"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_openai.py"), str(port), str(log)])
    wait_port(port)
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m, flush=True)
        c or fails.append(m)

    def ocrun(*args, timeout=180):
        return subprocess.run([oc, *args], cwd=str(proj), env=dict(env, PWD=str(proj)), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=timeout)

    def ccss(*args):
        r = subprocess.run([sys.executable, "-m", "cc_sessionsync", *args], cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        print(r.stdout[-1500:] + r.stderr[-1500:])
        return r

    try:
        v = ocrun("--version").stdout.strip().splitlines()[-1]
        import re
        major = int(re.search(r"(\d+)\.\d+", v).group(1))
        print(f"opencode {v}")
        sa = ["--standalone"] if major >= 2 else []
        # 初始化 OpenCode 的库（第一次跑会建表 / 迁移）
        r = ocrun("session", "list", *sa, "--format", "json")
        ok(r.returncode == 0, f"opencode session list works ({r.returncode}) {r.stderr[-300:]}")
        # 来源 1：Claude Code 会话（带工具调用和结果）
        sid = str(uuid.uuid4())
        pj = H / ".claude" / "projects" / ("-" + str(proj).strip("/\\").replace("/", "-").replace("\\", "-").replace(":", "-"))
        pj.mkdir(parents=True)
        recs = [{"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": sid, "cwd": str(proj), "timestamp": "2026-10-20T08:00:00.000Z",
                 "message": {"role": "user", "content": "IMPORT-MARK 帮我看看 README"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": sid, "cwd": str(proj), "timestamp": "2026-10-20T08:00:01.000Z",
                 "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "README.md"}}]}},
                {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": sid, "cwd": str(proj), "timestamp": "2026-10-20T08:00:02.000Z",
                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "TOOL-RESULT-MARK demo"}]}},
                {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": sid, "cwd": str(proj), "timestamp": "2026-10-20T08:00:03.000Z",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "ANSWER-MARK README 只有一行"}]}},
                {"type": "custom-title", "customTitle": "看 README", "sessionId": sid}]
        (pj / f"{sid}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in recs) + "\n", encoding="utf-8")
        # 来源 2：ZCode 会话
        import oc_fixture as OF
        zc = OF.zcode(H / ".zcode", str(proj))
        r = ccss("convert", "run", "--from", "claude", "--to", "opencode", "--id", sid)
        ok(r.returncode == 0 and "演练" in r.stdout, "dry-run")
        new = []
        for src, i in (("claude", sid), ("zcode", zc["main"])):
            r = ccss("convert", "run", "--from", src, "--to", "opencode", "--id", i, "--apply")
            ok(r.returncode == 0 and "✓ 做了 1 个" in r.stdout, f"{src} → opencode applied")
            import re
            m = re.search(r"opencode --session (ses_\w+)", r.stdout)
            new.append(m.group(1) if m else None)
        ok(all(new), f"new ids {new}")
        r = ocrun("session", "list", *sa, "--format", "json")
        listed = r.stdout
        ok(all(n and n in listed for n in new), "both imported sessions appear in `opencode session list`")
        ok("（Claude Code）看 README" in listed and "（ZCode）" in listed, "titles carry the source prefix")
        # 接着聊
        n0 = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
        args = ["run", *sa, "--format", "json", "-s", new[0], "-m", "mock/mock-1", "RESUME-MARK 继续"] if major >= 2 else \
            ["run", "-s", new[0], "-m", "mock/mock-1", "RESUME-MARK 继续"]
        r = ocrun(*args, timeout=300)
        ok(r.returncode == 0, f"opencode run -s <imported> ({r.returncode}) {r.stderr[-400:]}")
        reqs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()[n0:]]
        req = next((q for q in reversed(reqs) if any("RESUME-MARK" in (m.get("text") or "") for m in q["msgs"])), None)
        ok(req is not None, "mock model received the resumed turn")
        if req:
            txt = " ".join(m.get("text") or "" for m in req["msgs"])
            roles = [m["role"] for m in req["msgs"]]
            ok("IMPORT-MARK" in txt and "ANSWER-MARK" in txt and "TOOL-RESULT-MARK" in txt, "imported history (incl. tool output as text) was sent to the model")
            ok("assistant" in roles and roles[-1] == "user", f"history roles {roles}")
        r = ccss("convert", "list", "opencode")
        ok(new[0] in r.stdout, "cc-sessionsync lists the resumed session")
        sys.path.insert(0, str(ROOT))
        os.environ.update(env)
        from cc_sessionsync import ocstore
        rows = {x["id"]: x for x in ocstore.refresh("opencode", force=True)}
        h_items = Path(rows[new[0]]["file"]).read_text(encoding="utf-8")
        ok("RESUME-MARK" in h_items and "MOCK-REPLY" in h_items, "the new turn was written into the same session (read back by cc-sessionsync)")
        # 撤回：接着聊过的那个留着，没动过的删掉
        r = ccss("convert", "ledgers")
        leds = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
        for l in leds:
            ccss("convert", "undo", l)
        listed = ocrun("session", "list", *sa, "--format", "json").stdout
        ok(new[1] not in listed, "undo removed the untouched import (`opencode session delete`)")
        ok(new[0] in listed, "undo kept the session you continued in OpenCode")
    finally:
        mock.terminate()
    print(f"\n{'OK' if not fails else 'FAILED'}  ({len(fails)} failures)  tmp={T}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

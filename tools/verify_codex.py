# -*- coding: utf-8 -*-
"""端到端验证 任意 → Codex（真的 codex 命令行）。全在临时目录（HOME / CODEX_HOME 都指过去），模型是本机的假服务
（tools/mock_openai.py 的 Responses API），不联网、不登录、不用任何真钥匙。

  造一个 OpenCode 2.x 会话和一个 ZCode 会话（tests/oc_fixture.py）→ `cc-sessionsync convert run --to codex --apply`
  → `codex exec resume <新线程号>` 真的接着聊：假模型收到的历史里有导入的内容（工具变成文字）→ 新的一轮写进同一个 rollout
  → Codex 自己的线程库里有它 → `convert undo`：接着聊过的留着，没动过的删掉；来源一个字节都没变。

    python tools/verify_codex.py <codex 可执行文件> [端口]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
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


def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    codex = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("codex") or "codex"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8798
    T = Path(tempfile.mkdtemp(prefix="codex-verify-"))
    H, CH = T / "home", T / "home" / ".codex"
    CH.mkdir(parents=True)
    proj = T / "proj"
    proj.mkdir()
    (CH / "config.toml").write_text(f'''model = "mock-1"
model_provider = "mock"
approval_policy = "never"
sandbox_mode = "read-only"
[model_providers.mock]
name = "Mock"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
env_key = "MOCK_API_KEY"
''', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_", "OPENAI", "XDG_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"),
               CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(CH), CC_SESSIONSYNC_HOME=str(T / "ss"), MOCK_API_KEY="x",
               CC_SESSIONSYNC_OPENCODE_DATA=str(T / "oc" / "opencode"), CC_SESSIONSYNC_ZCODE_HOME=str(H / ".zcode"),
               CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_WATCHER="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1", RUST_LOG="error")
    log = T / "mock.jsonl"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_openai.py"), str(port), str(log)])
    wait_port(port)
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m, flush=True)
        c or fails.append(m)

    def ccss(*args):
        r = subprocess.run([sys.executable, "-m", "cc_sessionsync", *args], cwd=str(ROOT), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        print(r.stdout[-1500:] + r.stderr[-1500:])
        return r

    try:
        import oc_fixture as OF
        oc = OF.opencode(T / "oc" / "opencode", str(proj), v1=False)
        zc = OF.zcode(H / ".zcode", str(proj))
        before = {"oc": tree(T / "oc"), "zc": tree(H / ".zcode")}
        new = []
        for src, i in (("opencode", oc["v2"]), ("zcode", zc["main"])):
            r = ccss("convert", "run", "--from", src, "--to", "codex", "--id", i, "--apply")
            m = re.search(r"codex resume ([0-9a-f-]{36})", r.stdout)
            ok(r.returncode == 0 and m is not None, f"{src} → codex applied")
            new.append(m.group(1) if m else None)
        r = subprocess.run([codex, "--version"], capture_output=True, text=True, env=env)
        print(r.stdout.strip())
        r = subprocess.run([codex, "exec", "--skip-git-repo-check", "resume", new[0], "RESUME-MARK 继续"], cwd=str(proj),
                           env=dict(env, PWD=str(proj)), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=300)
        ok(r.returncode == 0, f"codex exec resume <imported> ({r.returncode}) {r.stderr[-600:]}")
        reqs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        req = next((q for q in reversed(reqs) if any("RESUME-MARK" in (m.get("text") or "") for m in q["msgs"])), None)
        ok(req is not None and req["path"].endswith("/responses"), "mock model received the resumed turn (Responses API)")
        if req:
            txt = " ".join(m.get("text") or "" for m in req["msgs"])
            ok("OC2-MARK" in txt and "OC2-ANSWER" in txt and "OC2-TOOL-OUT" in txt, "imported history (incl. tool output as text) was sent to the model")
            ok("OC2-THINK" not in txt, "thinking not carried over by default")
        f = next(CH.glob(f"sessions/**/rollout-*{new[0]}.jsonl"), None)
        body = f.read_text(encoding="utf-8") if f else ""
        ok("RESUME-MARK" in body and "MOCK-REPLY" in body, "the new turn was appended to the same rollout")
        dbs = list(CH.glob("state_*.sqlite"))
        if dbs:
            c = sqlite3.connect(f"file:{dbs[0]}?mode=ro", uri=True)
            ids = {r[0] for r in c.execute("select id from threads")}
            c.close()
            ok(new[0] in ids, "Codex's own thread db lists the imported thread")
        else:
            print("（这个版本的 codex 没有 state_*.sqlite，跳过线程库检查）")
        r = ccss("convert", "list", "codex")
        ok(all(n and n in r.stdout for n in new), "cc-sessionsync lists both threads")
        r = ccss("convert", "ledgers")
        for l in [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]:
            ccss("convert", "undo", l)
        ok(f is not None and f.exists(), "undo kept the thread you continued in Codex")
        ok(not list(CH.glob(f"sessions/**/rollout-*{new[1]}.jsonl")), "undo removed the untouched import")
        ok(tree(T / "oc") == before["oc"] and tree(H / ".zcode") == before["zc"], "sources byte-identical")
    finally:
        mock.terminate()
    print(f"\n{'OK' if not fails else 'FAILED'}  ({len(fails)} failures)  tmp={T}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""端到端验证新来源（OpenCode 1.x / 2.x、ZCode）→ Claude Code 和 → DeepSeek Harness。全在临时目录，模型都是本机的假服务
（tools/mock_anthropic.py / mock_openai.py），不联网、不登录、不用任何真钥匙。

  1. `cc-sessionsync convert run --from opencode|zcode --to claude --apply --no-desktop`
     → 每个都真的 `claude -p --resume` 接着聊：发给 API 的历史里有导入的内容、工具变成文字、思考 / ZCode 的提醒不在；新的一轮写回同一个文件。
     → 转回去（Claude → OpenCode）被认成 loop，不转。
  2. （设了 DSH_BIN 才跑）OpenCode → dsh：`dsh --profile headless --session-id <id>` 接着聊，假模型收到导入的历史。
  最后确认 OpenCode / ZCode 的数据一个字节都没变。

    python tools/verify_convert.py [claude 可执行文件] [端口]        DSH_BIN=/path/to/dsh（可选）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))


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
    claude = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("claude") or "claude"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8795
    T = Path(tempfile.mkdtemp(prefix="convert-verify-"))
    H = T / "home"
    H.mkdir()
    from cc_sessionsync import dsh
    proj = Path(dsh.long_path(str(T / "proj")))
    proj.mkdir()
    DH = T / "dshhome"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "CURSOR_", "CC_SESSIONSYNC_", "XDG_", "DSH_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(T / "appdata"), LOCALAPPDATA=str(T / "localappdata"),
               CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"), DSH_HOME=str(DH),
               CC_SESSIONSYNC_OPENCODE_DATA=str(T / "oc" / "opencode"), CC_SESSIONSYNC_ZCODE_HOME=str(H / ".zcode"),
               CC_SESSIONSYNC_OPENCODE_CLI=str(T / "no-opencode-cli"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"),
               CC_SESSIONSYNC_FAKE_OPENCODE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_ZCODE_PROCS=str(T / "procs.json"),
               CC_SESSIONSYNC_FAKE_DSH_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_WATCHER="1",
               ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY="sk-ant-dummy-for-local-mock", MOCK_KEY="x",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1", PYTHONPATH=str(ROOT),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    (T / "procs.json").write_text("[]")
    nw = 0x08000000 if os.name == "nt" else 0
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m, flush=True)
        c or fails.append(m)

    def run(args, **kw):
        return subprocess.run(args, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=nw, **kw)

    import oc_fixture as OF
    oc = OF.opencode(T / "oc" / "opencode", str(proj))
    zc = OF.zcode(H / ".zcode", str(proj))
    before = tree(T / "oc") | {"~" + k: v for k, v in tree(H / ".zcode").items()}
    log = T / "anthropic.log"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_anthropic.py"), str(port), str(log)], creationflags=nw)
    try:
        wait_port(port)
        print("claude", run([claude, "--version"], timeout=60).stdout.strip())
        cases = (("opencode", oc["v1"], "OC1-MARK", ["调用了 bash", "OC1-TOOL-OUT", "OC1-ANSWER"], ["OC1-THINK", "<system-reminder>x<"]),
                 ("opencode", oc["v2"], "OC2-MARK", ["调用了 shell", "OC2-TOOL-OUT", "OC2-ANSWER"], ["OC2-THINK"]),
                 ("zcode", zc["main"], "ZC-MARK", ["调用了 Bash", "ZC-TOOL-OUT", "ZC-ANSWER"], ["ZC-THINK", "ZC-REMINDER", "ZC-NOTIFY"]))
        made = {}
        for src, sid, mark, want, bad in cases:
            r = run([sys.executable, "-m", "cc_sessionsync", "convert", "run", "--from", src, "--to", "claude", "--id", sid, "--apply", "--no-desktop"])
            m = re.search(r"claude --resume ([0-9a-f-]{36})", r.stdout)
            ok(r.returncode == 0 and m is not None, f"{mark}: {src} → claude applied {r.stderr[-300:]}")
            if not m:
                continue
            new = m.group(1)
            made[mark] = new
            jf = next((H / ".claude" / "projects").glob(f"*/{new}.jsonl"))
            n0 = len(jf.read_text(encoding="utf-8").splitlines())
            log.unlink(missing_ok=True)
            c = run([claude, "-p", "上一步做了什么？一句话。", "--resume", new], cwd=proj, timeout=180, stdin=subprocess.DEVNULL)
            ok(c.returncode == 0 and "MOCK-OK" in c.stdout, f"{mark}: claude --resume ({c.returncode}) {c.stderr.strip()[-300:]}")
            reqs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
            hit = [q for q in reqs if q["path"].startswith("/v1/messages") and "count_tokens" not in q["path"]
                   and mark in json.dumps(q["body"].get("messages", []), ensure_ascii=False)]
            ok(bool(hit), f"{mark}: imported history sent to the Claude API")
            if hit:
                blob = json.dumps(hit[-1]["body"]["messages"], ensure_ascii=False)
                miss = [w for w in want if w not in blob]
                extra = [b for b in bad if b in blob]
                ok(not miss and not extra, f"{mark}: tools as text, thinking / reminders dropped (missing {miss}, extra {extra})")
            n1 = len(jf.read_text(encoding="utf-8").splitlines())
            ok(n1 > n0, f"{mark}: new turn appended to the same session ({n0} → {n1})")
        if "OC2-MARK" in made:
            r = run([sys.executable, "-m", "cc_sessionsync", "convert", "run", "--from", "claude", "--to", "dsh", "--id", made["OC2-MARK"]])
            ok("本来就是从目标应用来的" not in r.stdout, "claude(from opencode) → dsh is allowed")
            r = run([sys.executable, "-m", "cc_sessionsync", "convert", "run", "--from", "claude", "--to", "opencode", "--id", made["OC2-MARK"], "--export"])
            ok(r.returncode == 0, "claude(from opencode) → opencode export works")
            from cc_sessionsync import convert  # noqa: F401  (loop 的判定在 s_convert 里测；这里只确认命令行不崩)
    finally:
        mock.kill()

    dsh_bin = os.environ.get("DSH_BIN")
    if dsh_bin:
        dsh_bin = shutil.which(dsh_bin, path=(os.environ.get("NODE_DIR", "") + os.pathsep + os.environ.get("PATH", ""))) or dsh_bin
    if not dsh_bin:
        print("DSH_BIN not set: skipped the live dsh resume")
    else:
        from verify_dsh import PATCH
        oport = port + 1
        prof = DH / "profiles" / "headless"
        prof.mkdir(parents=True, exist_ok=True)
        (prof / "cordis.patch.yml").write_text(PATCH.format(port=oport), encoding="utf-8")
        r = run([sys.executable, "-m", "cc_sessionsync", "convert", "run", "--from", "opencode", "--to", "dsh", "--id", oc["v2"], "--apply"])
        m = re.search(r"--session-id (session-[0-9a-f-]+)", r.stdout)
        ok(r.returncode == 0 and m is not None, f"opencode → dsh applied {r.stdout[-300:]}")
        if m:
            olog = T / "openai.log"
            om = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_openai.py"), str(oport), str(olog)], creationflags=nw)
            try:
                wait_port(oport)
                d = run([dsh_bin, "--profile", "headless", "--session-id", m.group(1), "接着说：下一步做什么？"], cwd=proj, timeout=240,
                        stdin=subprocess.DEVNULL)
                ok(d.returncode == 0 and "MOCK-REPLY" in d.stdout, f"dsh --session-id resumes the converted session ({d.returncode}) {d.stderr[-300:]}")
                reqs = [json.loads(l) for l in olog.read_text(encoding="utf-8").splitlines()] if olog.exists() else []
                blob = json.dumps([q.get("msgs") for q in reqs], ensure_ascii=False)
                ok("OC2-MARK" in blob and "OC2-TOOL-OUT" in blob and "OC2-THINK" not in blob, "dsh model received the imported history")
            finally:
                om.kill()
    after = tree(T / "oc") | {"~" + k: v for k, v in tree(H / ".zcode").items()}
    ok(after == before, "OpenCode / ZCode data byte-identical")
    print(f"\n{'OK' if not fails else 'FAILED'}  ({len(fails)} failures)  tmp={T}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

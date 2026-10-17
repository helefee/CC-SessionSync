# -*- coding: utf-8 -*-
"""端到端验证：造一个 Codex 对话 → cc-sessionsync 导入 → 真的 Claude Code 命令行 `claude --resume` 打开它。

全部在临时目录里（HOME / CLAUDE_CONFIG_DIR / CODEX_HOME 都指过去），Claude Code 连的是本机的假 API（tools/mock_anthropic.py），
不联网、不用登录、不花额度。检查：
  1. claude -p --resume <会话号> 能打开导入的会话（退出码 0、拿到假 API 的回复）；
  2. 发给 API 的历史消息里真有导入的内容（用户原话、助手回复、工具摘要）；
  3. Claude Code 把新的一轮接着写进了同一个会话文件（以后还能接着 resume）；
  4. 0.2.1：时间线「恢复到 Claude」（Claude 版本 + Codex 版本经转换）和「导出 .md / .html → 导入」得到的新会话也都能 resume，
     历史里带着原来的内容。

    python tools/verify_resume.py [claude 可执行文件] [端口]
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
from codex_fixture import rollout  # noqa: E402

MARK = "VERIFY-MARKER-7f3a"


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    claude = (sys.argv[1] if len(sys.argv) > 1 else "") or shutil.which("claude") or "claude"   # Windows 上找到 claude.cmd / claude.exe
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8799
    T = Path(tempfile.mkdtemp(prefix="cc-verify-"))
    H, proj = T / "home", T / "proj"
    H.mkdir()
    proj.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_"))}
    env.update(HOME=str(H), USERPROFILE=str(H), APPDATA=str(T / "appdata"), LOCALAPPDATA=str(T / "localappdata"),
               XDG_CONFIG_HOME=str(T / "xdg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"),
               CC_SESSIONSYNC_HOME=str(T / "ss"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"),
               ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_API_KEY="sk-ant-dummy-for-local-mock",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
               PYTHONPATH=str(ROOT))
    (T / "procs.json").write_text("[]")
    _, tid = rollout(H / ".codex", str(proj), title="验证用的 Codex 对话", secret_marker=MARK)
    nw = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run([sys.executable, "-m", "cc_sessionsync", "codex", "import", "--days", "0", "--apply", "--no-desktop"],
                       env=env, capture_output=True, text=True, encoding="utf-8", creationflags=nw)
    print(r.stdout[-1500:], r.stderr[-1500:])
    mp = json.loads((T / "ss" / "codex-import-map.json").read_text(encoding="utf-8"))
    sid, jf = mp[tid]["session"], Path(mp[tid]["file"])
    n0 = len(jf.read_text(encoding="utf-8").splitlines())
    log = T / "requests.log"
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_anthropic.py"), str(port), str(log)], creationflags=nw)
    fails = []
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        ver = subprocess.run([claude, "--version"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=nw)
        print("claude", ver.stdout.strip())
        c = subprocess.run([claude, "-p", "上一步做了什么？一句话。", "--resume", sid], cwd=proj, env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=180, stdin=subprocess.DEVNULL, creationflags=nw)
        print("exit", c.returncode, "stdout:", c.stdout.strip()[-400:], "stderr:", c.stderr.strip()[-800:])
        if c.returncode != 0 or "MOCK-OK" not in c.stdout:
            fails.append("claude --resume did not open the imported session")
        reqs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        main_req = [q for q in reqs if q["path"].startswith("/v1/messages") and "count_tokens" not in q["path"]
                    and any(MARK in json.dumps(m, ensure_ascii=False) for m in q["body"].get("messages", []))]
        if not main_req:
            fails.append("imported history not sent to the API")
        else:
            msgs = main_req[-1]["body"]["messages"]
            blob = json.dumps(msgs, ensure_ascii=False)
            roles = [m["role"] for m in msgs]
            print(f"API request: {len(msgs)} messages, roles {roles[:12]}{'…' if len(roles) > 12 else ''}")
            for want in (MARK, "修好了：onSubmit", "npm test -- login", "Begin Patch", "加好了 login.test.tsx", "上一步做了什么"):
                if want not in blob:
                    fails.append(f"missing in API history: {want}")
            if roles.count("assistant") < 2:
                fails.append("assistant turns missing")
        n1 = len(jf.read_text(encoding="utf-8").splitlines())
        print(f"session file lines {n0} → {n1}")
        if n1 <= n0 or "MOCK-OK" not in jf.read_text(encoding="utf-8"):
            fails.append("new turn not appended to the same session file")
        # 再 resume 一次（导入 + 一轮新对话之后还能接着开）
        c2 = subprocess.run([claude, "-p", "再说一次", "--resume", sid], cwd=proj, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=180, stdin=subprocess.DEVNULL, creationflags=nw)
        if c2.returncode != 0 or "MOCK-OK" not in c2.stdout:
            fails.append("second resume failed")
        # 4. 时间线恢复 + 导出 / 导入出来的新会话
        helper = (
            "import json,sys\n"
            "from cc_sessionsync import sessions as S, timeline as TL, exports as E\n"
            "S.backup(why='verify')\n"
            "snaps={r['agent']:r['id'] for r in S.snapshots(limit=50)}\n"
            "out={}\n"
            "r=TL.restore([snaps['claude']],'copy','claude',desktop=False); out['restore-claude']=r['made'][0]['id']\n"
            "r=TL.restore([snaps['codex']],'copy','claude',desktop=False); out['restore-codex']=r['made'][0]['id']\n"
            f"sid={sid!r}\n"
            "for fmt in ('md','html'):\n"
            "    ex=E.export_many([{'agent':'claude','id':sid}],fmt)\n"
            "    im=E.import_files(ex['files'],None,False,False)\n"
            "    assert im['ok'], im\n"
            "    out['import-'+fmt]=im['made'][0]['id']\n"
            "print('JSON'+json.dumps(out))\n")
        h = subprocess.run([sys.executable, "-c", helper], env=env, capture_output=True, text=True, encoding="utf-8", creationflags=nw)
        made = {}
        for line in h.stdout.splitlines():
            if line.startswith("JSON"):
                made = json.loads(line[4:])
        if not made:
            fails.append("restore / import helper failed: " + (h.stderr or h.stdout)[-800:])
        for kind, nsid in made.items():
            before = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
            c3 = subprocess.run([claude, "-p", "接着说", "--resume", nsid], cwd=proj, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=180, stdin=subprocess.DEVNULL, creationflags=nw)
            reqs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()[before:]] if log.exists() else []
            hit = any(MARK in json.dumps(q["body"].get("messages", []), ensure_ascii=False) for q in reqs if q["path"].startswith("/v1/messages"))
            print(f"{kind}: {nsid} exit {c3.returncode} history-has-marker {hit}")
            if c3.returncode != 0 or "MOCK-OK" not in c3.stdout:
                fails.append(f"{kind}: claude --resume failed")
            elif not hit:
                fails.append(f"{kind}: original content not in resumed history")
    finally:
        mock.kill()
    print("RESULT", "OK" if not fails else "FAIL", fails, "\ntemp:", T)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

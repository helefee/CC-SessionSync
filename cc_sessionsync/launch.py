# -*- coding: utf-8 -*-
"""找 Claude Code / Codex 命令行，在新终端里接着跑某个会话（「会话」页的「在终端继续」）。"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _newest(pat: str) -> str | None:
    c = sorted(glob.glob(pat), key=lambda x: os.path.getmtime(x), reverse=True)
    return c[0] if c else None


_EXE: dict = {}


def _exe_cached(k, f, ttl=60):
    v = _EXE.get(k)
    if v and time.time() - v[0] < ttl and (v[1] is None or Path(v[1]).exists()):
        return v[1]
    r = f()
    _EXE[k] = (time.time(), r)
    return r


def claude_exe() -> str | None:
    return _exe_cached("claude", _claude_exe)


def codex_exe() -> str | None:
    return _exe_cached("codex", _codex_exe)


def _claude_exe() -> str | None:
    p = os.environ.get("CLAUDE_CLI_PATH")
    if p and Path(p).exists():
        return p
    w = shutil.which("claude")
    if w:
        return w
    if os.name == "nt":
        for pat in (os.path.expandvars(r"%USERPROFILE%\.local\bin\claude.exe"),
                    os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\*\claude.exe"),
                    os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\claude.exe")):
            x = _newest(pat)
            if x:
                return x
    return None


def _codex_exe() -> str | None:
    p = os.environ.get("CODEX_CLI_PATH")
    if p and Path(p).exists():
        return p
    if os.name == "nt":
        x = _newest(os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe"))
        if x:
            return x
    return shutil.which("codex")


def session_cmd(tool: str, root: str, resume: str | None = None, prompt: str | None = None) -> list[str]:
    """拼命令行：就用 Claude Code / Codex 自己的配置和登录，什么都不加。"""
    exe = claude_exe() if tool == "claude" else codex_exe()
    if not exe:
        raise RuntimeError(f"找不到 {tool} 命令行（Claude Code / Codex 没装命令行版；可设 {'CLAUDE' if tool == 'claude' else 'CODEX'}_CLI_PATH）")
    extra: list[str] = []
    if resume and not re.match(r"^[A-Za-z0-9-]{6,80}$", resume):
        raise ValueError("会话号不对")
    if tool == "claude":
        cmd = [exe, *extra]
        if resume:
            cmd += ["--resume", resume]
        if prompt is not None:
            cmd += ["-p", prompt]
    else:
        if prompt is not None:
            cmd = [exe, "exec", *(["resume", resume] if resume else []), "--skip-git-repo-check", *extra, prompt]
        else:
            cmd = [exe, *extra, "-C", root, *(["resume", resume] if resume else [])]
    return cmd


def launch(tool: str, root: str, resume: str | None = None, dry: bool = False) -> dict:
    """开一个新终端窗口跑命令行会话（用户点了才开，是可见窗口）。dry=True 只返回命令行。"""
    if tool not in ("claude", "codex"):
        raise ValueError("只支持 claude / codex")
    if not Path(root).is_dir():
        raise ValueError("项目目录不存在")
    cmd = session_cmd(tool, root, resume)
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    if dry:
        return {"ok": True, "cmd": shown, "argv": cmd}
    env = dict(os.environ)
    for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    if os.name == "nt":
        subprocess.Popen(cmd, cwd=root, env=env, creationflags=0x00000010)        # CREATE_NEW_CONSOLE：用户要看的终端
    elif sys.platform == "darwin":
        script = "cd " + _sh(root) + " && " + " ".join(_sh(a) for a in cmd)
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(script)}'])
    else:
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("xterm")
        if not term:
            raise RuntimeError("找不到终端程序；命令行在这里，可以自己复制去跑：" + shown)
        subprocess.Popen([term, "-e", *cmd], cwd=root, env=env)
    return {"ok": True, "cmd": shown}


def _sh(a: str) -> str:
    return "'" + a.replace("'", "'\\''") + "'"


# ---------------- 从 CC Switch 导入（只读） ----------------

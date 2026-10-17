# -*- coding: utf-8 -*-
"""数据目录。缺省 ~/.cc-sessionsync：设置（settings.json）、同步备份（claude-sync-bak/）、Codex 导入账本（codex-import/）、
排队（pending-jobs.json）、回收站（trash/）都在这里。

    CC_SESSIONSYNC_HOME              改数据目录
    CLAUDE_CONFIG_DIR / CODEX_HOME   和 Claude Code / Codex 自己认的一样
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
HOME = Path(os.environ.get("CC_SESSIONSYNC_HOME") or Path.home() / ".cc-sessionsync")
HOME.mkdir(parents=True, exist_ok=True)


def _set_dir(k: str) -> str | None:
    """settings.json 里手动指定的 Claude / Codex 数据目录（UI 的「检测 / 目录」里选的）。环境变量优先。"""
    try:
        v = json.loads((HOME / "settings.json").read_text(encoding="utf-8")).get(k) or ""
        return v if v and Path(v).is_dir() else None
    except Exception:
        return None


def _codex_store_home() -> str | None:
    """~/.codex 里没有会话、而商店版 Codex 的虚拟化目录里有 → 用那边（见 detect.codex_store_homes）。"""
    if os.name != "nt" or (Path.home() / ".codex" / "sessions").is_dir():
        return None
    try:
        from .detect import codex_store_homes
        h = [x for x in codex_store_homes() if (x / "sessions").is_dir()]
        return str(h[0]) if h else None
    except Exception:
        return None


CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or _set_dir("claude_dir") or Path.home() / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or _set_dir("codex_dir") or _codex_store_home() or Path.home() / ".codex")

FROZEN = "__compiled__" in globals() or getattr(sys, "frozen", False)


def self_cmd(*args: str) -> list[str]:
    """再起一个自己跑子命令（排队等桌面端退出的后台进程用）。嵌进别的程序时可以替换掉这个函数。"""
    if FROZEN:
        return [sys.argv[0] if os.path.isabs(sys.argv[0]) else os.path.abspath(sys.argv[0]), *args]
    return [sys.executable, "-m", "cc_sessionsync", *args]


def self_cwd() -> str:
    return str(PKG.parent)

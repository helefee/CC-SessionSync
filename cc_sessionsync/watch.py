# -*- coding: utf-8 -*-
"""账号切换检测：看 Claude 桌面端（Local Storage 里的当前账号）、Claude Code 命令行（~/.claude.json 的 oauthAccount）、
Codex（auth.json 的 ChatGPT 账号 + config.toml 的 model_provider）有没有变。

- 便宜：每 10 秒只看几个文件 / 目录的修改时间，变了才去读内容；
- 防抖：新值要连续两次（≥ 10 秒）一样才算切换，切过去又切回来不报；启动时那次只记下来不报；
- 只出打码后的标识（邮箱 a***@x.com / 账号号前 8 位），不读、不存、不传任何令牌；
- 发事件（events.add("account", …)），托盘按「通知」设置决定弹不弹；点通知打开窗口的「账号同步」页，上面有「立即同步」。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import events, paths

STATE = {"claude-desktop": None, "claude-cli": None, "codex": None, "last": [], "error": None}
_cand: dict[str, tuple] = {}
_sig: dict[str, tuple] = {}
NAMES = {"claude-desktop": "Claude 桌面端", "claude-cli": "Claude Code 命令行", "codex": "Codex"}


def mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return ""
    u, _, d = e.partition("@")
    return (u[:1] + "***@" + d) if u else "***@" + d


def _mt(p: Path) -> float:
    try:
        if p.is_dir():
            return max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=p.stat().st_mtime)
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _claude_json() -> Path:
    """Claude Code 的登录信息：设了 CLAUDE_CONFIG_DIR 且那里有 .claude.json 就用那个，否则 ~/.claude.json（和 claude_sync 一致）。"""
    c = os.environ.get("CLAUDE_CONFIG_DIR")
    if c and (Path(c) / ".claude.json").exists():
        return Path(c) / ".claude.json"
    return Path.home() / ".claude.json"


def _read_claude_cli() -> dict | None:
    try:
        o = json.loads(_claude_json().read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        return None
    a = o.get("accountUuid")
    if not a:
        return {"id": "", "label": "未登录"}
    return {"id": f"{a}/{o.get('organizationUuid') or ''}", "label": mask_email(o.get("emailAddress")) or a[:8]}


def _read_claude_desktop() -> dict | None:
    from . import claude_sync
    try:
        d = claude_sync.desktop_account(profile=True)
    except Exception:
        return None
    if not d.get("key"):
        return None
    return {"id": d["key"], "label": mask_email(d.get("email")) or (d.get("accountUuid") or "")[:8]}


def _read_codex() -> dict | None:
    import base64
    h = paths.CODEX_HOME
    prov = "openai"
    try:
        from . import _toml as tomllib
        prov = str(tomllib.loads((h / "config.toml").read_text(encoding="utf-8")).get("model_provider") or "openai")
    except Exception:
        pass
    acct, email, mode = None, None, None
    try:
        a = json.loads((h / "auth.json").read_text(encoding="utf-8"))
        mode = a.get("auth_mode")
        t = a.get("tokens") or {}
        acct = t.get("account_id")
        part = (t.get("id_token") or "").split(".")[1] if t.get("id_token") else ""
        if part:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            email = claims.get("email")
        elif a.get("OPENAI_API_KEY"):
            mode = mode or "apikey"
    except Exception:
        pass
    who = mask_email(email) or ((acct or "")[:8] if acct else ("API key" if mode == "apikey" else "未登录"))
    return {"id": f"{acct or mode or ''}|{prov}", "label": f"{who} · {prov}"}


def _sources():
    from . import claude_sync
    try:
        ldb = claude_sync.app_path("ldb")
    except Exception:
        ldb = None
    return {"claude-desktop": ([ldb] if ldb else [], _read_claude_desktop),
            "claude-cli": ([_claude_json()], _read_claude_cli),
            "codex": ([paths.CODEX_HOME / "auth.json", paths.CODEX_HOME / "config.toml"], _read_codex)}


def tick(now: float | None = None, stable: float = 9) -> list[dict]:
    """查一轮；返回这轮确认的切换事件。"""
    now = now or time.time()
    out = []
    for k, (files, read) in _sources().items():
        sig = tuple(_mt(f) for f in files)
        if _sig.get(k) == sig and k not in _cand:
            continue
        _sig[k] = sig
        try:
            v = read()
        except Exception as e:
            STATE["error"] = f"{k}: {type(e).__name__}"
            continue
        if v is None:
            continue
        cur = STATE.get(k)
        if cur is None:
            STATE[k] = v                      # 第一次：只记下来
            continue
        if v["id"] == cur["id"]:
            _cand.pop(k, None)
            continue
        c = _cand.get(k)
        if not c or c[0]["id"] != v["id"]:
            _cand[k] = (v, now)               # 先记成候选，下一轮还是它才算
            continue
        if now - c[1] < stable:
            continue
        _cand.pop(k, None)
        STATE[k] = v
        ev = events.add("account", f"检测到 {NAMES[k]} 账号已切换", f"{cur['label']} → {v['label']}",
                        {"page": "sync" if k.startswith("claude") else "codex", "account_switch": k},
                        who=k, old=cur["label"], new=v["label"])
        STATE["last"] = (STATE["last"] + [ev])[-10:]
        out.append(ev)
    return out


def status() -> dict:
    return {k: STATE[k] for k in NAMES} | {"last": STATE["last"], "error": STATE["error"]}


_started = False


def start(interval: float = 10) -> None:
    global _started
    if _started or os.environ.get("CC_SESSIONSYNC_NO_WATCH"):
        return
    _started = True

    def loop():
        while True:
            try:
                tick()
            except Exception as e:
                STATE["error"] = f"{type(e).__name__}: {e}"
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="account-watch").start()

# -*- coding: utf-8 -*-
"""账号切换检测：看 Claude 桌面端（Local Storage 里的当前账号）、Claude Code 命令行（~/.claude.json 的 oauthAccount）、
Codex（auth.json 的 ChatGPT 账号 + config.toml 的 model_provider）有没有变。

- 便宜：每 10 秒只看几个文件 / 目录的修改时间，变了才去读内容；
- 防抖：新值要连续两次（≥ 10 秒）一样才算切换，切过去又切回来不报；
- 比的是稳定的标识（来源 + 账号号 + 组织号；桌面端再加模式 1p / 3p），不比打码后的邮箱；
- 上次认出的每个来源都记在 <数据目录>/watch-state.json，重启后接着比（不会因为重启把旧值当新基线）；
- 桌面端：Local Storage 里只剩很早以前的旧事件时（当前账号的事件发完被删了 / 压缩中），旧事件的时间戳比记下的旧，
  不算换号（0.2.5 修：以前会在「当前号 ↔ 几个月前的旧号」之间来回报）；
- 桌面端在订阅登录和 API 模式（3P）之间切换算「模式切换」，单独说，不当成换号；
- 换号后当前账号已经什么都不缺（没有要补的会话），或者已经同步过，就算处理完了，不再提醒；
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

STATE = {"claude-desktop": None, "claude-cli": None, "codex": None, "last": [], "error": None, "handled": {}, "notes": []}
_cand: dict[str, tuple] = {}
_sig: dict[str, tuple] = {}
NAMES = {"claude-desktop": "Claude 桌面端", "claude-cli": "Claude Code 命令行", "codex": "Codex"}


# 完整账号（邮箱 / 名字 / 组织）只放在本进程内存里，给「显示完整账号」用（GET /api/accounts/reveal，带口令）；
# 不写文件、不进事件、不进通知、不进日志。key = rid（Claude 是账号号，Codex 是 "codex:<账号号>|<供应商>"）。
FULL: dict[str, dict] = {}


def _keep(rid: str, email: str | None, name: str | None = None, org: str | None = None) -> None:
    if rid and (email or name or org):
        FULL[rid] = {k: v for k, v in (("email", email), ("name", name), ("org", org)) if v}


def mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return ""
    u, _, d = e.partition("@")
    return (u[:1] + "***@" + d) if u else "***@" + d


def _mt(p: Path) -> float:
    try:
        if p.is_dir():
            return max([p.stat().st_mtime] + [f.stat().st_mtime for f in p.iterdir() if f.is_file()])
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
        return {"id": "", "label": "未登录", "rid": ""}
    _keep(a, o.get("emailAddress"), o.get("displayName") or o.get("fullName"), o.get("organizationName"))
    return {"id": f"{a}/{o.get('organizationUuid') or ''}", "label": mask_email(o.get("emailAddress")) or a[:8], "rid": a}


def _read_claude_desktop() -> dict | None:
    from . import claude_sync
    try:
        mode = claude_sync.desktop_mode()
    except Exception:
        mode = {"mode": "1p", "why": ""}
    if mode["mode"] == "3p":                  # API 模式：没有「账号」，按设备号 + 会话列表文件夹认，只给供应商类型
        try:
            i = claude_sync.api_info()
        except Exception:
            return None
        b = i.get("bucket") or {}
        bid = b.get("key") or ("3p:" + (i.get("device") or ""))
        return {"id": bid if bid.startswith("3p:") else "3p:" + bid, "label": "API 模式" + (f" · {i['provider']}" if i.get("provider") else ""),
                "rid": "", "mode": "3p", "seen": None}
    try:
        d = claude_sync.desktop_account(profile=True)
    except Exception:
        return None
    if not d.get("key"):
        return None
    a = d.get("accountUuid") or ""
    _keep(a, d.get("email"), d.get("name"), d.get("org_name"))
    return {"id": d["key"], "label": mask_email(d.get("email")) or a[:8], "rid": a, "mode": "1p", "seen": d.get("seen")}


def _read_codex() -> dict | None:
    import base64
    h = paths.CODEX_HOME
    prov = "openai"
    try:
        from . import _toml as tomllib
        prov = str(tomllib.loads((h / "config.toml").read_text(encoding="utf-8")).get("model_provider") or "openai")
    except Exception:
        pass
    acct, email, mode, cname = None, None, None, None
    try:
        a = json.loads((h / "auth.json").read_text(encoding="utf-8"))
        mode = a.get("auth_mode")
        t = a.get("tokens") or {}
        acct = t.get("account_id")
        part = (t.get("id_token") or "").split(".")[1] if t.get("id_token") else ""
        if part:
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            email = claims.get("email")
            cname = claims.get("name")
        elif a.get("OPENAI_API_KEY"):
            mode = mode or "apikey"
    except Exception:
        pass
    who = mask_email(email) or ((acct or "")[:8] if acct else ("API key" if mode == "apikey" else "未登录"))
    rid = f"codex:{acct or mode or ''}|{prov}"
    _keep(rid, email and f"{email} · {prov}", cname, None)
    return {"id": f"{acct or mode or ''}|{prov}", "label": f"{who} · {prov}", "rid": rid}


def _sources():
    from . import claude_sync
    try:
        ldb = claude_sync.app_path("ldb")
    except Exception:
        ldb = None
    try:                                      # 桌面端切 API 模式 / 订阅登录记在 Claude-3p 的 claude_desktop_config.json
        cfg = [Path(c["path"]) / "claude_desktop_config.json" for c in claude_sync.api_dir_candidates()]
    except Exception:
        cfg = []
    return {"claude-desktop": (([ldb] if ldb else []) + cfg, _read_claude_desktop),
            "claude-cli": ([_claude_json()], _read_claude_cli),
            "codex": ([paths.CODEX_HOME / "auth.json", paths.CODEX_HOME / "config.toml"], _read_codex)}


def _state_file() -> Path:
    return paths.HOME / "watch-state.json"


_loaded = False


def _load() -> None:
    """上次认出的各来源（打码标签 + 账号号 / 组织号，不含令牌）和已经处理过的切换。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        j = json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return
    for k in NAMES:
        if isinstance(j.get(k), dict) and j[k].get("id") is not None and STATE.get(k) is None:
            STATE[k] = j[k]
    STATE["handled"] = j.get("handled") or {}


def _save() -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({**{k: STATE.get(k) for k in NAMES}, "handled": STATE.get("handled") or {}}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception:
        pass


def mark_handled(key: str | None = None, source: str | None = None) -> list[str]:
    """同步做完（或者看过发现没什么要补的）：把指向这个账号的切换算处理完，提示条收起、不再提醒。"""
    _load()
    out = []
    for k in ("claude-desktop", "claude-cli"):
        cur = STATE.get(k)
        if not cur:
            continue
        if (key and cur.get("id") in (key, "3p:" + key)) or (not key and source and {"desktop": "claude-desktop", "api": "claude-desktop", "cli": "claude-cli"}.get(source) == k):
            STATE.setdefault("handled", {})[k] = {"id": cur["id"], "at": time.time()}
            out.append(k)
    if out:
        _save()
    return out


def _nothing_to_sync(k: str) -> bool:
    if not k.startswith("claude"):
        return False
    try:
        from . import claude_sync
        claude_sync.forget()
        p = claude_sync.plan(source="cli" if k == "claude-cli" else claude_sync.default_source())
        return bool(p.get("ok")) and not p.get("add") and not p.get("update")
    except Exception:
        return False


def _ts(x: str) -> float:
    from . import claude_sync
    return claude_sync._iso(x)


def tick(now: float | None = None, stable: float = 9) -> list[dict]:
    """查一轮；返回这轮确认的切换事件。"""
    _load()
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
            STATE[k] = v                      # 第一次（以前也没记过）：只记下来
            _save()
            continue
        if v["id"] == cur["id"]:
            _cand.pop(k, None)
            if v.get("seen") and (v.get("seen") or "") > (cur.get("seen") or "") or v.get("label") != cur.get("label"):
                STATE[k] = v                  # 同一个号：时间戳 / 标签更新一下
                _save()
            continue
        mode_switch = (cur.get("mode") or "1p") != (v.get("mode") or "1p")
        if not mode_switch and v.get("seen") and cur.get("seen") and _ts(v["seen"]) <= _ts(cur["seen"]):
            _cand.pop(k, None)                # 认出的「新号」的事件比记下的还旧：是旧文件里残留的，不算换号
            continue
        c = _cand.get(k)
        if not c or c[0]["id"] != v["id"]:
            _cand[k] = (v, now)               # 先记成候选，下一轮还是它才算
            continue
        if now - c[1] < stable:
            continue
        _cand.pop(k, None)
        STATE[k] = v
        _save()
        if mode_switch:
            title = "Claude 桌面端切换到 API 模式（3P）" if v.get("mode") == "3p" else "Claude 桌面端切回订阅登录模式"
            text = f"{cur['label']} → {v['label']}（这是模式切换，不是换账号；两边的会话列表分开放）"
        else:
            title, text = f"检测到 {NAMES[k]} 账号已切换", f"{cur['label']} → {v['label']}"
        if not mode_switch and _nothing_to_sync(k):
            STATE.setdefault("handled", {})[k] = {"id": v["id"], "at": now}
            STATE["notes"] = (STATE.get("notes", []) + [{"time": now, "who": k, "text": text + "：当前账号已经不缺会话，不提醒"}])[-10:]
            _save()
            continue
        ev = events.add("account", title, text,
                        {"page": "sync" if k.startswith("claude") else "codex", "account_switch": k},
                        who=k, old=cur["label"], new=v["label"], old_rid=cur.get("rid", ""), new_rid=v.get("rid", ""),
                        mode_switch=mode_switch, new_id=v["id"])
        STATE["last"] = (STATE["last"] + [ev])[-10:]
        out.append(ev)
    return out


def status() -> dict:
    _load()
    return {k: STATE[k] for k in NAMES} | {"last": STATE["last"], "error": STATE["error"], "handled": STATE.get("handled") or {},
                                         "notes": STATE.get("notes", [])}


def handled_since() -> dict:
    return {k: v.get("at") for k, v in (STATE.get("handled") or {}).items()}


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

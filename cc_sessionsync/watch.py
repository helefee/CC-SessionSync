# -*- coding: utf-8 -*-
"""账号切换检测：看 Claude 桌面端（Local Storage 里的当前账号）、Claude Code 命令行（.claude.json 的 oauthAccount）、
Codex（auth.json 的 ChatGPT 账号 + config.toml 的 model_provider）有没有变。

信号（0.5.5 起补齐）：
- ~/.claude.json 与 CLAUDE_CONFIG_DIR/.claude.json（设了环境变量时优先，文件尚未出现也盯着）
- 桌面端 Local Storage（leveldb）+ API 模式配置文件
- 凭据文件路径 / 是否存在 / mtime / size（~/.claude/.credentials.json、CLAUDE_CONFIG_DIR、CLAUDE_SECURESTORAGE_CONFIG_DIR；
  **绝不读文件内容或 Keychain 密文**）——SECURESTORAGE 换号时 json 身份可能不变，凭据指纹变了会强制重读并标 reason
- 父目录 mtime：.claude.json 原子替换（删再建）不会漏

规则：
- 便宜：多数时候只比 mtime 指纹；变了 / 解析失败过 / 有候选 / 凭据指纹变了才读内容；
- 防抖：默认 ~2.5s 内同一新 id 连续两次解析成功才确认（有候选时轮询加快到 ~1.5s）；两路 Claude 源已同 id 则立刻确认；
- 来回抖（A→B→A）在确认前丢掉；半截 JSON 解析失败不更新指纹，下轮继续读（避免卡死）；
- 登出（有号→空）与登录（空→有号）都算变化；首次见到某源只记基线不同步；
- 桌面端开着写不了时 pending.phase=waiting_desktop，UI 明确「已检测到，在等桌面端退出」；
- 只出打码标识；发事件；托盘按「通知」设置弹不弹。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import events, paths

STATE = {"claude-desktop": None, "claude-cli": None, "codex": None, "last": [], "error": None, "handled": {}, "notes": [], "pending": {},
         "detect": {}, "cred": None}
_cand: dict[str, tuple] = {}
_sig: dict[str, tuple] = {}
_force: set[str] = set()
NAMES = {"claude-desktop": "Claude 桌面端", "claude-cli": "Claude Code 命令行", "codex": "Codex"}

# 完整账号（邮箱 / 名字 / 组织）只放在本进程内存里，给「显示完整账号」用；不写文件、不进事件。
FULL: dict[str, dict] = {}

STABLE_S = 2.5          # 确认切换：同一新 id 至少稳住这么久（且至少两次成功解析）
FAST_INTERVAL = 1.5     # 有候选 / pending 时加快轮询
SLOW_INTERVAL = 10.0


def _keep(rid: str, email: str | None, name: str | None = None, org: str | None = None) -> None:
    if rid and (email or name or org):
        FULL[rid] = {k: v for k, v in (("email", email), ("name", name), ("org", org)) if v}


def mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return ""
    u, _, d = e.partition("@")
    return (u[:1] + "***@" + d) if u else "***@" + d


def _mt(p: Path):
    """目录 / 文件指纹：mtime_ns + size + inode（同秒同尺寸覆写 / 原子替换都能区分）。"""
    try:
        if p.is_dir():
            st = p.stat()
            parts = [(st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))]
            for f in p.iterdir():
                if f.is_file():
                    try:
                        s = f.stat()
                        parts.append((s.st_mtime_ns, s.st_size, getattr(s, "st_ino", 0), f.name))
                    except OSError:
                        pass
            return tuple(parts)
        st = p.stat()
        return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
    except OSError:
        return (0, 0, 0)


def _file_fp(p: Path) -> tuple:
    """路径指纹：存在？mtime？size？不读内容。"""
    try:
        st = p.stat()
        return (str(p), True, int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (str(p), False, 0, 0)


def _claude_json() -> Path:
    from . import claude_sync
    return claude_sync.claude_json_path()


def _cred_paths() -> list[Path]:
    """可能放 .credentials.json 的目录（去重）。不读内容。"""
    out: list[Path] = []
    seen: set[str] = set()

    def add(d: Path) -> None:
        p = d / ".credentials.json"
        k = str(p)
        if k not in seen:
            seen.add(k)
            out.append(p)

    ss = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if ss is not None:
        if ss.strip():
            add(Path(ss.strip()))
        else:
            add(Path.home() / ".claude")
    c = os.environ.get("CLAUDE_CONFIG_DIR")
    if c:
        add(Path(c))
    add(Path.home() / ".claude")
    return out


def _cred_fp() -> tuple:
    return tuple(_file_fp(p) for p in _cred_paths())


def _read_claude_cli() -> dict | None:
    p = _claude_json()
    if not p.exists():
        return {"id": "", "label": "未登录", "rid": "", "logged_out": True, "path": str(p)}
    try:
        o = json.loads(p.read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        return None                                   # 半截 JSON：让上层别更新指纹，下轮再读
    a = o.get("accountUuid")
    if not a:
        return {"id": "", "label": "未登录", "rid": "", "logged_out": True, "path": str(p)}
    _keep(a, o.get("emailAddress"), o.get("displayName") or o.get("fullName"), o.get("organizationName"))
    return {"id": f"{a}/{o.get('organizationUuid') or ''}", "label": mask_email(o.get("emailAddress")) or a[:8], "rid": a, "path": str(p)}


def _read_claude_desktop() -> dict | None:
    from . import claude_sync
    try:
        mode = claude_sync.desktop_mode()
    except Exception:
        mode = {"mode": "1p", "why": ""}
    if mode["mode"] == "3p":
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
        return {"id": "", "label": "未登录", "rid": "", "logged_out": True, "mode": "1p", "seen": None}
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
    try:
        cfg = [Path(c["path"]) / "claude_desktop_config.json" for c in claude_sync.api_dir_candidates()]
    except Exception:
        cfg = []
    from . import claude_sync as _CS
    cands = _CS.claude_json_candidates()
    # 每个候选文件 + 父目录（原子替换：删再建时父目录 mtime 会变）
    cli_files = []
    for cj in cands:
        cli_files.extend([cj, cj.parent])
    desk_files = ([ldb, ldb.parent] if ldb else []) + cfg
    return {"claude-desktop": (desk_files, _read_claude_desktop),
            "claude-cli": (cli_files, _read_claude_cli),
            "codex": ([paths.CODEX_HOME / "auth.json", paths.CODEX_HOME / "config.toml", paths.CODEX_HOME], _read_codex)}


def _state_file() -> Path:
    return paths.HOME / "watch-state.json"


_loaded = False


def _load() -> None:
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
    STATE["pending"] = {k: v for k, v in (j.get("pending") or {}).items() if k in ("claude-desktop", "claude-cli") and isinstance(v, dict)}


def _save() -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({**{k: STATE.get(k) for k in NAMES}, "handled": STATE.get("handled") or {},
                                   "pending": STATE.get("pending") or {}}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except Exception:
        pass


def mark_handled(key: str | None = None, source: str | None = None) -> list[str]:
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


_SELF: dict = {}
SELF_WINDOW = 90


def note_self(client: str) -> None:
    _SELF["codex" if client == "codex" else "claude-cli"] = time.time()


def _split_codex(i: str) -> tuple:
    acct, _, prov = (i or "").rpartition("|")
    return acct, prov


OWN_PROVIDERS = ("cc-route",)


def _ts(x: str) -> float:
    from . import claude_sync
    return claude_sync._iso(x)


def _set_reason(k: str, reason: str, **extra) -> None:
    row = {"reason": reason, "at": time.time(), **extra}
    STATE.setdefault("detect", {})[k] = row


def _other_claude(k: str) -> str | None:
    if k == "claude-desktop":
        return "claude-cli"
    if k == "claude-cli":
        return "claude-desktop"
    return None


def _sources_agree(k: str, new_id: str) -> bool:
    o = _other_claude(k)
    if not o or not new_id:
        return False
    ov = STATE.get(o) or {}
    return ov.get("id") == new_id


def _confirm_candidate(k: str, v: dict, now: float, stable: float) -> bool:
    """同一新 id：两次成功解析且间隔 ≥ stable，或另一路 Claude 源已经是这个 id → 确认。"""
    c = _cand.get(k)
    if not c or c[0]["id"] != v["id"]:
        _cand[k] = (v, now, 1)
        _set_reason(k, "candidate", new=v.get("label"), new_id=v.get("id"))
        return False
    n = (c[2] if len(c) > 2 else 1) + 1
    _cand[k] = (c[0], c[1], n)
    if _sources_agree(k, v["id"]):
        _set_reason(k, "corroborated", new=v.get("label"), new_id=v.get("id"), via="other_source")
        return True
    if now - c[1] >= stable and n >= 2:
        _set_reason(k, "debounced", new=v.get("label"), new_id=v.get("id"), waited=round(now - c[1], 2), reads=n)
        return True
    _set_reason(k, "debouncing", new=v.get("label"), new_id=v.get("id"), waited=round(now - c[1], 2), reads=n, need=stable)
    return False


def _check_credentials(now: float) -> None:
    """凭据文件指纹变了（不读内容）→ 强制重读 Claude 两路；身份没变则标 credentials_hint。"""
    fp = _cred_fp()
    old = STATE.get("cred")
    if old is None:
        STATE["cred"] = fp
        return
    if fp == old:
        return
    STATE["cred"] = fp
    _force.update(("claude-cli", "claude-desktop"))
    # 身份文件可能还没变（SECURESTORAGE 换号）：记下提示，不擅自当换号同步
    cli = STATE.get("claude-cli") or {}
    _set_reason("credentials", "credentials_changed", paths=[x[0] for x in fp if x[1]], identity_id=cli.get("id") or "")
    STATE["notes"] = (STATE.get("notes", []) + [{"time": now, "who": "credentials",
        "text": "凭据文件已更新（未读内容）；已强制重读账号信号。若用了 CLAUDE_SECURESTORAGE_CONFIG_DIR 换号而 .claude.json 身份未变，请以实际登录为准"}])[-10:]


def tick(now: float | None = None, stable: float | None = None) -> list[dict]:
    """查一轮；返回这轮确认的切换事件。"""
    _load()
    now = now or time.time()
    stable = STABLE_S if stable is None else stable
    out = []
    try:
        _check_credentials(now)
    except Exception as e:
        STATE["error"] = f"cred: {type(e).__name__}"
    for k, (files, read) in _sources().items():
        sig = tuple(_mt(f) for f in files)
        forced = k in _force
        if not forced and _sig.get(k) == sig and k not in _cand:
            if STATE.get("detect", {}).get(k, {}).get("reason") not in ("parse_error", "debouncing", "candidate"):
                _set_reason(k, "stable", id=(STATE.get(k) or {}).get("id"), label=(STATE.get(k) or {}).get("label"))
            continue
        try:
            v = read()
        except Exception as e:
            STATE["error"] = f"{k}: {type(e).__name__}"
            _set_reason(k, "parse_error", error=type(e).__name__)
            # 不更新 _sig：半截写入后还能再读
            continue
        if v is None:
            _set_reason(k, "parse_error", error="unreadable")
            continue
        # 成功读到内容才认指纹（含 force）
        _sig[k] = sig
        _force.discard(k)
        cur = STATE.get(k)
        if cur is None:
            STATE[k] = v
            _set_reason(k, "first_run", id=v.get("id"), label=v.get("label"))
            _save()
            continue
        if v["id"] == cur["id"]:
            _cand.pop(k, None)
            if v.get("seen") and (v.get("seen") or "") > (cur.get("seen") or "") or v.get("label") != cur.get("label") or v.get("logged_out") != cur.get("logged_out"):
                STATE[k] = v
                _save()
            # 凭据刚变但身份相同
            if STATE.get("detect", {}).get("credentials", {}).get("reason") == "credentials_changed" and (time.time() - STATE["detect"]["credentials"].get("at", 0) < 30):
                _set_reason(k, "credentials_hint", id=v.get("id"), label=v.get("label"))
            else:
                _set_reason(k, "stable", id=v.get("id"), label=v.get("label"))
            continue
        if k == "codex":
            (a0, p0), (a1, p1) = _split_codex(cur["id"]), _split_codex(v["id"])
            if a0 == a1 and (p0 in OWN_PROVIDERS or p1 in OWN_PROVIDERS or now - _SELF.get(k, 0) < SELF_WINDOW):
                _cand.pop(k, None)
                STATE[k] = v
                _save()
                continue
        mode_switch = (cur.get("mode") or "1p") != (v.get("mode") or "1p")
        if not mode_switch and v.get("seen") and cur.get("seen") and _ts(v["seen"]) <= _ts(cur["seen"]):
            _cand.pop(k, None)
            _set_reason(k, "stale_event", seen=v.get("seen"))
            continue
        # 候选抖回旧 id：丢掉
        c = _cand.get(k)
        if c and c[0]["id"] != v["id"] and v["id"] == cur["id"]:
            _cand.pop(k, None)
            _set_reason(k, "flicker_ignored", tried=c[0].get("id"))
            continue
        if not _confirm_candidate(k, v, now, stable):
            continue
        _cand.pop(k, None)
        old = cur
        STATE[k] = v
        _save()
        is_login = (not old.get("id")) and bool(v.get("id"))
        is_logout = bool(old.get("id")) and (not v.get("id"))
        if mode_switch:
            title = "Claude 桌面端切换到 API 模式（3P）" if v.get("mode") == "3p" else "Claude 桌面端切回订阅登录模式"
            text = f"{old['label']} → {v['label']}（这是模式切换，不是换账号；两边的会话列表分开放）"
        elif is_login:
            title = f"检测到 {NAMES[k]} 已登录"
            text = f"未登录 → {v['label']}"
        elif is_logout:
            title = f"检测到 {NAMES[k]} 已登出"
            text = f"{old['label']} → 未登录"
        else:
            prov_only = k == "codex" and _split_codex(old["id"])[0] == _split_codex(v["id"])[0]
            title = f"检测到 {NAMES[k]} 供应商已切换" if prov_only else f"检测到 {NAMES[k]} 账号已切换"
            text = f"{old['label']} → {v['label']}"
        _set_reason(k, "login" if is_login else ("logout" if is_logout else "confirmed"),
                    old=old.get("label"), new=v.get("label"), new_id=v.get("id"))
        if is_logout:
            STATE["notes"] = (STATE.get("notes", []) + [{"time": now, "who": k, "text": text}])[-10:]
            _save()
            continue
        if not mode_switch and k.startswith("claude") and _auto_on():
            STATE.setdefault("pending", {})[k] = {"id": v["id"], "label": v["label"], "old": old["label"], "at": now,
                                                   "phase": "detected", "reason": "detected"}
            STATE["notes"] = (STATE.get("notes", []) + [{"time": now, "who": k, "text": text + "：自动同步"}])[-10:]
            _save()
            continue
        if not mode_switch and _nothing_to_sync(k):
            STATE.setdefault("handled", {})[k] = {"id": v["id"], "at": now}
            STATE["notes"] = (STATE.get("notes", []) + [{"time": now, "who": k, "text": text + "：当前账号已经不缺会话，不提醒"}])[-10:]
            _save()
            continue
        ev = events.add("account", title, text,
                        {"page": "sync", "account_switch": k} if k.startswith("claude") else {"page": "sessions", "account_switch": k},
                        who=k, old=old["label"], new=v["label"], old_rid=old.get("rid", ""), new_rid=v.get("rid", ""),
                        mode_switch=mode_switch, new_id=v["id"], login=is_login)
        STATE["last"] = (STATE["last"] + [ev])[-10:]
        out.append(ev)
    # 两路 Claude 不一致时标一下（不自动同步错的那边）
    d, c = STATE.get("claude-desktop"), STATE.get("claude-cli")
    if d and c and d.get("id") and c.get("id") and d["id"] != c["id"]:
        STATE.setdefault("detect", {})["disagree"] = {"reason": "sources_disagree", "desktop": d.get("label"), "cli": c.get("label"), "at": now}
    elif "disagree" in STATE.get("detect", {}):
        STATE["detect"].pop("disagree", None)
    return out


def _auto_on() -> bool:
    try:
        from . import settings
        return bool(settings.get("claude_auto_sync_on_account_switch", True))
    except Exception:
        return False


def switch_tick(now: float | None = None) -> list[dict]:
    """把待同步的 Claude 换号补完。pending.phase 区分 detected / waiting_desktop / waiting_lock / done。"""
    _load()
    now = now or time.time()
    out = []
    for k in list((STATE.get("pending") or {}).keys()):
        pd = STATE["pending"][k]
        cur = STATE.get(k) or {}
        if cur.get("id") != pd.get("id"):
            pd.update(id=cur.get("id"), label=cur.get("label", pd.get("label")))
        who = NAMES[k]
        if not _auto_on():
            STATE["pending"].pop(k)
            _save()
            out.append(events.add("account", f"检测到 {who} 账号已切换", f"{pd.get('old')} → {pd['label']}", {"page": "sync", "account_switch": k},
                                  who=k, old=pd.get("old"), new=pd["label"], new_id=pd["id"]))
            continue
        from . import claude_sync
        r = claude_sync.switch_sync("cli" if k == "claude-cli" else "desktop", now=now)
        if r["state"] == "wait":
            why = r.get("why") or "running"
            phase = "waiting_desktop" if why == "running" else "waiting_lock"
            pd["phase"] = phase
            pd["reason"] = phase
            first_tell = not pd.get("told")
            if first_tell:
                pd["told"] = now
            _save()
            _set_reason(k, phase, label=pd.get("label"))
            # 每次 phase 变成 waiting_desktop 都保证 UI 能读到 pending；事件只发一次防刷屏
            if first_tell and why == "running":
                out.append(events.add("auto-sync", f"已检测到账号切换为 {pd['label']}，等待 Claude 桌面端退出后再同步",
                                      "账号切换已经认出；Claude 桌面端还开着，关掉后会自动把别的账号的会话同步过来（不是没检查到）",
                                      {"page": "sync"}, who=k, new=pd["label"], waiting="desktop"))
            elif first_tell:
                out.append(events.add("auto-sync", f"已检测到账号切换为 {pd['label']}，等待同步锁释放",
                                      "另一轮自动同步还在进行，稍后会继续",
                                      {"page": "sync"}, who=k, new=pd["label"], waiting="lock"))
            continue
        STATE["pending"].pop(k)
        if r["state"] == "done":
            STATE.setdefault("handled", {})[k] = {"id": pd["id"], "at": now}
            n = r["added"]
            body = ("、".join(r.get("titles") or [])[:180] or f"账号 {r.get('account')}") if n or r.get("updated") else "新账号已经不缺会话"
            if r.get("updated"):
                body = f"另外刷新了 {r['updated']} 个；" + body
            _save()
            _set_reason(k, "synced", added=n)
            out.append(events.add("auto-sync", f"检测到 Claude 账号切换为 {pd['label']}，已同步 {n} 个会话", body, {"page": "sync"},
                                  who=k, new=pd["label"], added=n, updated=r.get("updated", 0), backup=r.get("backup")))
        else:
            _save()
            _set_reason(k, "sync_error", error=r.get("error"))
            out.append(events.add("account", f"检测到 {who} 账号已切换", f"{pd.get('old')} → {pd['label']}（自动同步没做成：{r.get('error')}，请在「账号同步」页看看）",
                                  {"page": "sync", "account_switch": k}, who=k, old=pd.get("old"), new=pd["label"], new_id=pd["id"]))
    return out


def status() -> dict:
    _load()
    return {k: STATE[k] for k in NAMES} | {"last": STATE["last"], "error": STATE["error"], "handled": STATE.get("handled") or {},
                                         "notes": STATE.get("notes", []), "pending": STATE.get("pending") or {},
                                         "detect": STATE.get("detect") or {},
                                         "cred_watched": [p[0] for p in (STATE.get("cred") or ())]}


def handled_since() -> dict:
    return {k: v.get("at") for k, v in (STATE.get("handled") or {}).items()}


_started = False


def start(interval: float | None = None) -> None:
    global _started
    if _started or os.environ.get("CC_SESSIONSYNC_NO_WATCH"):
        return
    _started = True
    slow = SLOW_INTERVAL if interval is None else interval

    def loop():
        while True:
            try:
                tick()
                try:
                    switch_tick()
                except Exception:
                    pass
            except Exception as e:
                STATE["error"] = f"{type(e).__name__}: {e}"
            # 有候选或在等桌面端时加快，让真实换号几秒内落地、等待状态及时刷新
            busy = bool(_cand) or any((v or {}).get("phase") in ("detected", "waiting_desktop", "waiting_lock")
                                      for v in (STATE.get("pending") or {}).values())
            time.sleep(FAST_INTERVAL if busy else slow)
    threading.Thread(target=loop, daemon=True, name="account-watch").start()

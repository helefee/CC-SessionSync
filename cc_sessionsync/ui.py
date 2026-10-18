# -*- coding: utf-8 -*-
"""本地服务 + 界面（桌面窗口和 `cc-sessionsync ui` 共用）。

安全：只听 127.0.0.1；Host 头必须是 127.0.0.1 / localhost（挡 DNS rebinding）；每次启动随机一个口令写进页面，
除 /api/ping 以外的 /api/* 都要带 x-token（别的网页既读不到数据，也没法替你点）。返回里的 key 只有打码后的样子。
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, apps, claude_sync, codex_import, detect, events, paths, settings

TOKEN = secrets.token_urlsafe(18)
PAGE = Path(__file__).with_name("index.html")
STATIC = {"/icon.png": paths.PKG / "icon.png", "/favicon.ico": paths.PKG / "icon.ico"}
MODE = {"desktop": False, "port": 8766, "started": time.time()}


# ---------- 给托盘 / 页面用的汇总 ----------

def _status() -> dict:
    claude_sync.forget()
    acc = claude_sync.accounts()
    return {"version": __version__, "home": str(paths.HOME), "claude_home": str(paths.CLAUDE_HOME),
            "codex_home": str(paths.CODEX_HOME), "accounts": _mask_accounts(acc), "folders": claude_sync.folders(),
            "app_dirs": claude_sync.app_dirs_brief(), "api": api_brief(), "default_source": claude_sync.default_source(), "running": apps.status(),
            "jobs": [j for j in apps.pending() if j["state"] == "waiting"],
            "settings": settings_view(), "auto": claude_sync.auto_status(), "desktop": MODE["desktop"],
            "platform": os.name, "frozen": paths.FROZEN}


def _mask_accounts(acc: dict) -> dict:
    from .watch import mask_email
    out = {}
    for k, a in acc.items():
        a = dict(a)
        if a.get("email"):
            a["email_masked"] = mask_email(a["email"])
        out[k] = a
    return out


def backup_settings() -> dict:
    """自动备份用的设置；0.2.0 设过 session_backup_hours、又没设新键的，按原来的小时数。"""
    s = settings.load()
    try:
        raw = json.loads(settings.FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if isinstance(raw, dict) and "session_backup_every" not in raw and raw.get("session_backup_hours") is not None:
        s["_legacy_hours"] = raw["session_backup_hours"]
    return s


def _check_backup_settings(ch: dict) -> None:
    from . import sessions
    if "session_backup_unit" in ch and ch["session_backup_unit"] not in sessions.UNITS:
        raise ValueError("单位只能是 分钟 / 小时 / 天")
    if "session_backup_every" in ch or "session_backup_unit" in ch:
        s = settings.load()
        every = ch.get("session_backup_every", s.get("session_backup_every"))
        unit = ch.get("session_backup_unit", s.get("session_backup_unit") or "min")
        try:
            every = float(every)
        except (TypeError, ValueError):
            raise ValueError("间隔要填数字")
        if every <= 0 or every * sessions.UNITS[unit] < sessions.MIN_INTERVAL:
            raise ValueError("间隔最少 5 分钟")
        ch["session_backup_every"] = int(every) if every == int(every) else every
        ch["session_backup_unit"] = unit
        ch["session_backup_hours"] = None                      # 换成新键了，老的删掉
    for k in ("retention_all_days", "retention_daily_days"):
        if k in ch:
            try:
                ch[k] = max(1, int(ch[k]))
            except (TypeError, ValueError):
                raise ValueError("保留天数要填整数")


def settings_view() -> dict:
    s = settings.load()
    try:
        from . import tray
        s["autostart"] = tray.autostart_get()
    except Exception:
        s["autostart"] = False
    s["autostart_supported"] = os.name == "nt"
    s["tray_supported"] = os.name == "nt"
    return s


def tray_state(since: int = -1) -> dict:
    from . import update, watch
    seq, evs = events.since(since)
    s = settings.load()
    u = update.status()
    from . import appclose
    wq = [j for j in apps.pending() if j["state"] == "waiting"]
    jobs = len(wq)
    return {"seq": seq, "events": evs, "settings": s, "auto_sync": bool(s.get("auto_sync")), "jobs": jobs,
            "job_kinds": sorted({j.get("kind") or "claude" for j in wq}), "closerun": appclose._active(),
            "update": {"badge": u.get("badge"), "latest": u.get("latest")},
            "acct_handled": watch.handled_since(),
            "attention": any(e["kind"] == "account" and time.time() - e["time"] < 600
                             and not (watch.handled_since().get(e.get("who")) or 0) >= e["time"] for e in events.recent(10))}


def api_brief() -> dict:
    """给页面 / 诊断：API 模式（3P）的数据目录、桌面端当前模式、用哪个目录、供应商类型（只给类型，不给 key / 地址）。"""
    try:
        i = claude_sync.api_info()
        m = claude_sync.desktop_mode()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    b = i.get("bucket")
    return {"mode": m["mode"], "mode_why": m["why"], "mode_file": i.get("mode_file"), "base": i.get("base"), "why": i.get("why"),
            "provider": i.get("provider"), "device": (i.get("device") or "")[:8] or None,
            "bucket": {"key": b["key"], "count": b["count"], "path": b["path"], "placeholder": b["placeholder"]} if b else None,
            "cands": [{"label": c["label"], "path": c["base"], "exists": c["exists"], "sessions": c["has_sessions"],
                       "buckets": len(c["buckets"]), "active": c["base"] == i.get("base")} for c in i["cands"]]}


def sync_now(to: str | None = None, only=None, source: str | None = None) -> dict:
    claude_sync.forget()
    source = source or claude_sync.default_source()
    pl = claude_sync.run(to or None, True, None, source)
    if not pl.get("ok"):
        return pl
    if not (pl.get("add") or pl.get("update")):
        claude_sync._switch_handled(pl)        # 当前账号不缺会话：「检测到切换账号」算处理完
        return dict(pl, done=0, updated=0, deferred=False, note="没有要补的会话")
    r = apps.run_or_defer("claude", "claude-sync", {"target": to or pl["target"]["key"], "only": only, "source": source},
                          "同步 Claude 会话到 API 模式" if source == "api" else "同步 Claude 会话到当前账号")
    if not r.get("deferred"):
        events.add("auto-sync", "同步完成", f"补了 {r.get('done', 0)} 个、刷新 {r.get('updated', 0)} 个会话", {"page": "sync"}, manual=True)
    return r


def _settings_dir(k: str, v) -> dict:
    if v and not Path(v).is_dir():
        return {"ok": False, "error": f"目录不存在：{v}"}
    settings.update(**{k: str(v) if v else None})
    claude_sync.forget()
    detect.forget()
    return {"ok": True}


def _codex_kw(q: dict) -> dict:
    only = q.get("only")
    if isinstance(only, str):
        only = [x for x in only.split(",") if x.strip()]
    return {"days": float(q.get("days") or 30), "cwd": q.get("cwd") or None, "only": only or None,
            "include_all": bool(q.get("all")) and q.get("all") not in ("0", "false"),
            "desktop": q.get("desktop") not in (False, "0", "false"), "target": q.get("to") or None,
            "reasoning": bool(q.get("reasoning")) and q.get("reasoning") not in ("0", "false"),
            "mark": None if q.get("mark") in (None, "") else q.get("mark") not in (False, "0", "false")}


def diag() -> dict:
    detect.forget()
    r = detect.report()
    acc = _mask_accounts(claude_sync.accounts())
    from . import watch
    w = watch.status()
    lines = [detect.text(r), "", "账号（打码）："]
    for k, lab in (("desktop", "Claude 桌面端"), ("cli", "Claude Code 命令行")):
        a = acc.get(k) or {}
        lines.append(f"  {lab}：{a.get('email_masked') or (a.get('accountUuid') or '')[:8] or '—'}"
                     f"{'（' + a['via'] + '）' if a.get('via') else ''}")
    cx = w.get("codex") or {}
    lines.append(f"  Codex：{cx.get('label') or '—'}")
    lines += ["", "数据目录：", f"  本程序：{paths.HOME}", f"  Claude Code：{paths.CLAUDE_HOME}", f"  Codex：{paths.CODEX_HOME}"]
    ad = claude_sync.app_dirs_brief()
    for x in ad.get("parts") or []:
        lines.append(f"  桌面端 {x.get('name')}：{x.get('path')}（{x.get('why') or ''}）")
    ab = api_brief()
    lines += ["", f"桌面端模式：{'API 模式（3P）' if ab.get('mode') == '3p' else '订阅登录'}（{ab.get('mode_why') or ''}）",
              f"  API 模式数据目录：{ab.get('base') or '没找到'}（{ab.get('why') or ''}）  供应商：{ab.get('provider') or '—'}"]
    if ab.get("bucket"):
        lines.append(f"  API 模式会话列表：{ab['bucket']['key'][:14]}…  {ab['bucket']['count']} 个（所有供应商共用）")
    for c in ab.get("cands") or []:
        lines.append(f"    {'→' if c['active'] else ' '} {c['label']}：{'有' if c['exists'] else '没有'}" + (f"，{c['buckets']} 个会话列表文件夹" if c["buckets"] else ""))
    return {"report": r, "accounts": acc, "watch": w, "app_dirs": ad, "api": ab, "text": "\n".join(lines),
            "claude_home": str(paths.CLAUDE_HOME), "codex_home": str(paths.CODEX_HOME),
            "version": __version__, "frozen": paths.FROZEN, "exe": paths.EXE if paths.FROZEN else None}


def _scrub(o):
    """回给页面的东西里，账号的完整邮箱 / 名字 / 组织名一律换成打码的（rid 留着，点「显示完整账号」时按它去
    /api/accounts/reveal 取）。只认带 email 键的字典（就是账号）。"""
    from .watch import mask_email
    if isinstance(o, list):
        return [_scrub(x) for x in o]
    if not isinstance(o, dict):
        return o
    if "email" in o and not isinstance(o.get("email"), (dict, list)):
        o = dict(o)
        em, nm, og = o.pop("email", None), o.pop("name", None), o.pop("org_name", None)
        o["email_masked"] = mask_email(em) or None
        o["name_masked"] = (nm[:1] + "**") if nm else None
        o["has_full"] = bool(em or nm or og)
        o.setdefault("rid", o.get("accountUuid") or "")
    return {k: _scrub(v) for k, v in o.items()}


def _clean(o, reveal: bool = False):
    o = json.loads(json.dumps(o, ensure_ascii=False, default=str))
    return o if reveal else _scrub(o)


def reveal_accounts() -> dict:
    """完整账号：{rid: {email, name, org, label}}。只在页面点了「显示完整账号」（或开了「总是显示完整账号」）时来取；
    只从本机文件读，不联网；不写日志、不进诊断报告。"""
    from . import watch
    out: dict[str, dict] = {}

    def put(rid, email=None, name=None, org=None, over=True):
        if not rid or not (email or name or org) or (rid in out and not over):
            return
        out[rid] = {k: v for k, v in (("email", email), ("name", name), ("org", org)) if v}
    try:
        acc = claude_sync.accounts()
        for k in ("cli", "desktop"):
            a = acc.get(k) or {}
            put(a.get("accountUuid"), a.get("email"), a.get("name"), a.get("org_name"))
    except Exception:
        pass
    try:
        for uuid, m in claude_sync._load_names().items():
            put(uuid, m.get("email"), m.get("name"), None, over=False)
    except Exception:
        pass
    try:
        watch.tick()                                  # 把 Codex（和刚换的号）也读一遍
    except Exception:
        pass
    for rid, m in watch.FULL.items():
        put(rid, m.get("email"), m.get("name"), m.get("org"), over=rid.startswith("codex:"))
    for v in out.values():
        v["label"] = " · ".join(x for x in (v.get("email"), v.get("name"), v.get("org")) if x)
    return {"ok": True, "accounts": out}


class H(BaseHTTPRequestHandler):
    server_version = "cc-sessionsync"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, reveal=False):
        b = json.dumps(_clean(obj, reveal), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _file(self, f: Path, ctype: str | None = None, cache: bool = True):
        b = f.read_bytes()
        self.send_response(200)
        self.send_header("content-type", ctype or mimetypes.guess_type(str(f))[0] or "application/octet-stream")
        self.send_header("cache-control", "max-age=86400" if cache else "no-store")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self) -> bool:
        h = (self.headers.get("host") or "").rsplit(":", 1)[0]
        return h in ("127.0.0.1", "localhost")

    def _tok_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("x-token") or "", TOKEN)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p in ("/", "/index.html"):
                b = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__VERSION__", __version__).encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("cache-control", "no-store")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if p in STATIC:
                return self._file(STATIC[p])
            if p.startswith("/fonts/") and p.count("/") == 2 and ".." not in p:
                f = paths.PKG / "fonts" / p[7:]
                if f.is_file():
                    return self._file(f, "font/woff2" if f.suffix == ".woff2" else "text/plain; charset=utf-8")
            if p == "/api/ping":
                return self._json({"ok": True, "app": "cc-sessionsync", "version": __version__})
            if p.startswith("/api/") and not self._tok_ok():
                return self._json({"ok": False, "error": "口令不对（页面过期了？刷新一下）"}, 403)
            if p == "/api/accounts/reveal":           # 唯一会回完整账号的地方（要口令）
                return self._json(reveal_accounts(), reveal=True)
            r = self._get(p, q)
            if r is not None:
                return self._json(r)
        except Exception as e:
            return self._err(e)
        self._json({"error": "not found"}, 404)

    def _err(self, e: Exception):
        code = 400 if isinstance(e, (ValueError, KeyError, FileNotFoundError, PermissionError)) else 500
        return self._json({"ok": False, "error": f"{e}" if code == 400 else f"{type(e).__name__}: {e}"}, code)

    def _get(self, p: str, q: dict):
        if p == "/api/status":
            return _status()
        if p == "/api/sync/plan":
            claude_sync.forget()
            return claude_sync.run(q.get("to") or None, True, None, q.get("source") or claude_sync.default_source())
        if p == "/api/backups":
            return claude_sync.list_backups()
        if p == "/api/codex/plan":
            return codex_import.run(dry=True, **_codex_kw(q))
        if p == "/api/codex/preview":
            return codex_import.preview(q.get("id") or "", reasoning=q.get("reasoning") in ("1", "true"))
        if p == "/api/codex/ledgers":
            return codex_import.ledgers()
        if p == "/api/diag":
            return diag()
        if p == "/api/jobs":
            return apps.pending()
        if p == "/api/apps/close-plan":              # 「关闭并执行」之前给用户看：怎么开的、几个进程、会中断哪些命令行会话
            from . import appclose
            return appclose.plan(q.get("kind") or "claude")
        if p == "/api/apps/close-run":
            from . import appclose
            return appclose.view(q.get("id") or "")
        if p == "/api/settings":
            return settings_view()
        if p == "/api/events":
            return {"events": events.recent(50)}
        if p == "/api/tray":
            return tray_state(int(q.get("since", -1)))
        if p == "/api/update":
            from . import update
            return update.status()
        if p == "/api/selfcheck":
            from . import selfcheck
            r = selfcheck.last()
            return dict(r, text=selfcheck.text(r)) if r else {"never": True}
        if p == "/api/watch":
            from . import watch
            return watch.status()
        if p == "/api/modelcfg":
            from . import modelcfg
            return modelcfg.overview(None)
        if p == "/api/modelcfg/proxies":
            from . import modelcfg
            return modelcfg.proxies(wait=True)
        if p == "/api/providers":
            from . import providers
            return providers.overview(None)
        if p.startswith("/api/usage/"):
            from . import usage
            f = {k: q.get(k) for k in ("app", "provider", "model", "from", "to")}
            if p == "/api/usage/summary":
                return usage.summary(f)
            if p == "/api/usage/logs":
                return usage.logs(f, int(q.get("offset") or 0), min(500, int(q.get("limit") or 100)))
            if p == "/api/usage/prices":
                return usage.prices_view()
            if p == "/api/usage/status":
                return usage.status()
        if p.startswith("/api/sessions"):
            from . import sessions
            if p == "/api/sessions":
                return sessions.list_all(q.get("agent") or None)
            if p == "/api/sessions/read":
                return sessions.read(q["agent"], q["id"], int(q.get("limit") or 400))
            if p == "/api/sessions/snapshots":
                return {"items": sessions.snapshots(q.get("agent") or None, q.get("id") or None)}
            if p == "/api/sessions/trash":
                return {"items": sessions.trash_list()}
            if p == "/api/sessions/ops":
                return {"items": sessions.ops_list()}
            if p == "/api/sessions/backup-status":
                return dict(sessions.backup_status(), sched=sessions.sched_status(backup_settings()))
        if p.startswith("/api/timeline/"):
            from . import timeline
            if p == "/api/timeline/versions":
                return timeline.versions(q["agent"], q["id"])
            if p == "/api/timeline/view":
                return timeline.view(int(q["snap"]), int(q.get("limit") or 400))
            if p == "/api/timeline/runs":
                return {"items": timeline.runs()}
            if p == "/api/timeline/point":
                return timeline.point(float(q["t"]))
        return None

    def do_POST(self):
        if not self._host_ok() or not self._tok_ok():
            return self._json({"ok": False, "error": "口令不对（页面过期了？刷新一下）"}, 403)
        n = int(self.headers.get("content-length") or 0)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            b = {}
        p = urlparse(self.path).path
        try:
            r = self._post(p, b)
            if r is not None:
                code = 200
                if isinstance(r, tuple):
                    r, code = r
                return self._json(r, code)
        except Exception as e:
            return self._err(e)
        self._json({"error": "not found"}, 404)

    def _post(self, p: str, b: dict):
        if p == "/api/sync":
            return sync_now(b.get("to") or None, b.get("only"), b.get("source") or None)
        if p == "/api/restore":
            return apps.run_or_defer("claude", "claude-restore", {"backup": b["backup"]}, f"撤回 Claude 同步 {b['backup']}")
        if p == "/api/codex/import":
            kw = _codex_kw(b)
            if kw["mark"] is None:
                kw["mark"] = codex_import.mark_setting()
            if kw["desktop"] and apps.running("claude"):
                return apps.run_or_defer("claude", "codex-import", kw, "把 Codex 对话导进 Claude Code")
            r = dict(codex_import.run(dry=False, **kw), deferred=False)
            if r.get("done"):
                events.add("import", f"已把 {r['done']} 个 Codex 对话导进 Claude Code", "在 Claude Code 里 /resume 能看到（标题带「（Codex）」）",
                           {"page": "codex"})
            return r
        if p == "/api/codex/undo":
            led = codex_import._load(codex_import.LEDGERS / str(b.get("ledger")) / "ledger.json", {}) or {}
            if led.get("desktop"):
                return apps.run_or_defer("claude", "codex-undo", {"ledger": b["ledger"]}, f"撤回 Codex 导入 {b['ledger']}")
            return codex_import.undo(str(b.get("ledger")))
        if p == "/api/settings":
            ch = {k: b[k] for k in ("auto_sync", "tray_close", "start_min", "notify", "update_auto", "codex_mark_imported",
                                    "session_backup_auto", "session_backup_every", "session_backup_unit", "selfcheck_done",
                                    "show_full_accounts", "retention_all_days", "retention_daily_days") if k in b}
            _check_backup_settings(ch)
            if ch:
                settings.update(**ch)
            if "autostart" in b and os.name == "nt":
                from . import tray
                tray.autostart_set(bool(b["autostart"]), MODE["port"])
            return dict(settings_view(), ok=True)
        if p == "/api/appdir":
            return _settings_dir("claude_app_dir", b.get("path") or "")
        if p == "/api/datadir":
            if b.get("which") not in ("claude_dir", "codex_dir"):
                raise ValueError("which 只能是 claude_dir / codex_dir")
            return dict(_settings_dir(b["which"], b.get("path") or ""), note="重启 CC Session Sync 后全面生效")
        if p == "/api/pickdir":
            return detect.pick_folder(b.get("start") or "")
        if p == "/api/jobs/cancel":
            return {"ok": apps.cancel(str(b.get("id")))}
        if p == "/api/apps/close-run":                 # 用户在对话框里点了「关闭并…（完成后重启）」
            from . import appclose
            return appclose.start(str(b.get("kind") or "claude"), [str(x) for x in (b.get("jobs") or [])])
        if p == "/api/apps/close-run/force":           # 用户第二次确认了「强制结束」
            from . import appclose
            return appclose.force(str(b.get("id")))
        if p == "/api/apps/close-run/stop":            # 不关了，改成等它自己退出
            from . import appclose
            return appclose.stop(str(b.get("id")))
        if p == "/api/update/check":
            from . import update
            return update.check(force=True)
        if p == "/api/update/install":                 # 只有用户点「立即更新」才会走到这里
            from . import update
            return update.install(["--port", str(MODE["port"])] if MODE["port"] != 8766 else None)
        if p == "/api/update/ignore":
            from . import update
            return update.ignore(b.get("version"))
        if p == "/api/selfcheck":
            from . import selfcheck
            r = selfcheck.run(net=b.get("net", True) is not False)
            settings.update(selfcheck_done=True)
            return dict(r, text=selfcheck.text(r))
        if p == "/api/reveal":
            return _reveal(b.get("path") or "")
        if p.startswith("/api/modelcfg/"):
            from . import keystore, modelcfg
            act = p.rsplit("/", 1)[1]
            if act == "claude":
                return modelcfg.set_claude("user", None, b.get("changes") or {})
            if act == "codex":
                return modelcfg.set_codex("user", None, b.get("changes") or {}, b.get("provider_auth"), add_provider=b.get("add_provider"))
            if act == "key-set":
                keystore.put(b["name"], b["secret"], b.get("comment") or "")
                return {"ok": True, "name": keystore.check_name(b["name"]), "masked": keystore.mask(b["secret"])}
            if act == "key-delete":
                return {"ok": keystore.delete(b["name"])}
            if act == "restore":
                return modelcfg.restore(str(b["id"]))
            if act == "migrate":
                return modelcfg.migrate(b["tool"], b["field"], b["key"])
            if act == "test":
                return modelcfg.test_connection(b.get("tool") or "claude", b.get("base_url"), b.get("key"), b.get("model"),
                                                bool(b.get("use_settings_key")))
        if p.startswith("/api/providers/"):
            from . import providers
            act = p.rsplit("/", 1)[1]
            tool = b.get("tool") or "claude"
            try:
                if act == "save":
                    return providers.upsert(tool, b.get("provider") or {}, b.get("secret") or None)
                if act == "delete":
                    return providers.delete(tool, str(b["id"]), bool(b.get("drop_key")))
                if act == "switch":
                    return providers.apply_global(tool, str(b["id"]), bool(b.get("drop_plaintext")))
                if act == "slot":
                    return providers.set_slot(str(b["slot"]))
                if act == "ccs-preview":
                    return {"items": providers.ccswitch_preview()}
                if act == "ccs-import":
                    return providers.ccswitch_import([str(x) for x in b.get("ids") or []])
                if act == "test":
                    return providers.test(tool, str(b["id"]))
                if act == "models":
                    return providers.models(tool, b.get("id") or None, b.get("base_url"), b.get("secret") or None, bool(b.get("cached_only")))
            except providers.PlaintextError as e:
                return {"error": str(e), "plaintext": e.items}, 409
        if p.startswith("/api/usage/"):
            from . import usage
            if p == "/api/usage/sync":
                return usage.scan(full=bool(b.get("full")))
            if p == "/api/usage/price":
                usage.set_price(str(b["model"]), b.get("price"))
                return {"ok": True}
            if p == "/api/usage/mult":
                m = b.get("m")
                usage.set_mult(str(b["provider"]), None if m in (None, "") else float(m))
                return {"ok": True}
            if p == "/api/usage/modelsdev":
                return usage.refresh_models_dev()
            if p == "/api/usage/import_ccswitch":
                return usage.import_ccswitch()
        if p.startswith("/api/sessions/"):
            return self._sessions(p.rsplit("/", 1)[1], b)
        if p == "/api/timeline/restore":
            from . import timeline
            return timeline.restore([int(x) for x in b.get("snaps") or []], b.get("mode") or "copy", b.get("target") or "claude",
                                    b.get("desktop", True) is not False)
        if p == "/api/backup/now":
            from . import sessions
            return sessions.backup_now()
        if p == "/api/backup/prune":
            from . import sessions
            return sessions.prune(backup_settings(), vacuum=bool(b.get("vacuum", True)))
        if p == "/api/export":
            from . import exports
            return exports.export_many(list(b.get("items") or []), b.get("fmt") or "md", str(b.get("label") or ""))
        if p == "/api/import":
            return _import(b)
        return None

    def _sessions(self, act: str, b: dict):
        from . import providers, sessions
        if act == "fulltext":
            return sessions.fulltext(b.get("q") or "")
        if act == "export":
            if (b.get("fmt") or "md") in ("md", "html"):
                from . import exports
                return exports.export_many([{"agent": b["agent"], "id": b["id"]}], b.get("fmt") or "md")
            return sessions.export(b["agent"], b["id"], b.get("fmt") or "md")
        if act == "backup":
            return sessions.backup_now() if not b.get("full") else sessions.backup(full=True, why="manual")
        if act == "open":
            x = sessions.find(b["agent"], b["id"])
            if not x or not x.get("file"):
                raise FileNotFoundError("找不到这个会话的记录文件")
            cwd = x.get("cwd") if x.get("cwd") and Path(x["cwd"]).is_dir() else str(Path.home())
            return providers.launch(b["agent"], None, cwd, b["id"], bool(b.get("dry")))
        if act == "restore-snapshot":
            return sessions.restore_snapshot(int(b["snap"]))
        if act == "trash-purge":
            return sessions.trash_purge(str(b["batch"]), str(b.get("confirm") or ""))
        # 下面这些会改桌面端的数据：开着就排队
        if act == "batch":
            items, action = list(b.get("items") or []), str(b["action"])
            out = {"ok": True, "results": []}
            for agent in ("claude", "codex"):
                part = [i for i in items if i.get("agent") == agent]
                if part:
                    r = apps.run_or_defer(agent, "sessions-batch", {"action": action, "items": part},
                                          f"{ {'archive': '归档', 'unarchive': '取消归档', 'delete': '删除'}.get(action, action) } {len(part)} 个{ 'Claude' if agent == 'claude' else 'Codex' }会话")
                    out["results"].append(dict(r, agent=agent))
                    out["ok"] = out["ok"] and r.get("ok", True) is not False
            return out
        if act == "rename":
            args = {"agent": b["agent"], "id": b["id"], "name": b.get("name") or "", "write_tool": bool(b.get("write_tool"))}
            if args["write_tool"] and (args["agent"] == "codex" or sessions._claude_registry().get(args["id"])):
                return apps.run_or_defer(args["agent"], "sessions-rename", args, f"改名：{args['name'][:30]}")
            return dict(sessions.rename(args["agent"], args["id"], args["name"], args["write_tool"]), deferred=False)
        if act == "trash-restore":
            m = next((t for t in sessions.trash_list() if t["id"] == b["batch"]), None)
            kinds = {i["agent"] for i in (m or {}).get("items", [])}
            kind = "codex" if kinds == {"codex"} else "claude"
            return apps.run_or_defer(kind, "sessions-trash-restore", {"batch": b["batch"], "only": b.get("only")}, f"从回收站还原 {b['batch']}")
        if act == "undo":
            op = next((o for o in sessions.ops_list(500) if o["id"] == b["op"]), None)
            kind = "codex" if op and op["items"] and all(i["agent"] == "codex" for i in op["items"]) else "claude"
            if op and op["kind"] == "rename" and not op.get("write_tool"):
                return dict(sessions.undo(b["op"]), deferred=False)
            return apps.run_or_defer(kind, "sessions-undo", {"op": b["op"]}, f"撤回 {b['op']}")
        return None


def _import(b: dict) -> dict:
    """导入 .md / .html：页面把文件内容传过来（浏览器拿不到路径），先存进 ~/.cc-sessionsync/imports/ 再读。"""
    from . import exports
    d = paths.HOME / "imports" / time.strftime("%Y%m%d-%H%M%S")
    files = []
    for i, f in enumerate(b.get("files") or []):
        name = Path(str(f.get("name") or f"file{i}.md")).name
        if Path(name).suffix.lower() not in (".md", ".markdown", ".html", ".htm"):
            raise ValueError(f"只认 .md / .html：{name}")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{i:02d}-{name}"
        p.write_text(str(f.get("text") or ""), encoding="utf-8")
        files.append(str(p))
    if not files:
        raise ValueError("没选文件")
    return exports.import_files(files, b.get("cwd") or None, b.get("desktop", True) is not False, bool(b.get("dry", True)))


def _reveal(path: str) -> dict:
    """在资源管理器里打开（只限本程序数据目录下的东西：导出、备份、回收站）。"""
    p = Path(path or paths.HOME).resolve()
    home = paths.HOME.resolve()
    if p != home and home not in p.parents:
        raise PermissionError("只能打开 CC Session Sync 数据目录里的东西")
    target = p if p.is_dir() else p.parent
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        import subprocess
        subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "path": str(target)}


# ---------- 后台 ----------

_bg_started = False


def start_background(auto_sync: bool | None = None) -> None:
    """自动同步、排队活的通知、账号切换检测、用量扫描、会话自动备份、检查更新（只提示）、第一次启动的自检。"""
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    from . import sessions, update, usage, watch

    def loop():
        events.poll_jobs()
        while True:
            try:
                on = settings.get("auto_sync", False) if auto_sync is None else auto_sync
                if on:
                    ev = claude_sync.auto_tick()
                    if ev and (ev.get("added") or ev.get("updated")):
                        events.add("auto-sync", f"已自动同步 {ev['added']} 个会话到当前账号",
                                   "、".join(ev.get("titles") or [])[:180] or f"账号 {ev.get('account')}", {"page": "sync"})
                events.poll_jobs()
                v = update.should_notify()
                if v:
                    events.add("update", f"CC Session Sync 有新版本 {v}", "点这里查看；不会自动安装，点「立即更新」才装", {"page": "update"})
            except Exception:
                pass
            time.sleep(15)
    threading.Thread(target=loop, daemon=True, name="ccss-bg").start()
    watch.start()
    if not os.environ.get("CC_SESSIONSYNC_NO_USAGE"):
        usage.start_background()
    sessions.start_auto(backup_settings)
    update.auto_check_later(lambda: settings.get("update_auto", True))
    if MODE["desktop"] and not settings.get("selfcheck_done") and not os.environ.get("CC_SESSIONSYNC_NO_SELFCHECK"):
        def first():
            time.sleep(5)
            try:
                from . import selfcheck
                r = selfcheck.run()
                settings.update(selfcheck_done=True)
                if r["counts"]["fail"]:
                    events.add("selfcheck", f"自检有 {r['counts']['fail']} 项没通过", "点这里查看建议", {"page": "selfcheck"})
            except Exception:
                pass
        threading.Thread(target=first, daemon=True, name="first-selfcheck").start()


def start(port: int = 8766, desktop: bool = False):
    """在后台线程里起服务；端口被占返回 None。"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        return None
    MODE.update(desktop=desktop, port=port)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="ccss-http").start()
    start_background()
    return srv


def serve(port: int = 8766, open_browser: bool = True, auto_sync: bool = False) -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    MODE.update(port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"CC Session Sync {__version__}：{url}（Ctrl+C 退出）", flush=True)
    start_background(auto_sync=True if auto_sync else None)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        claude_sync.auto_release()
    return 0

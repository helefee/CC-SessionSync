# -*- coding: utf-8 -*-
"""本地小界面：只听 127.0.0.1；每次启动随机一个口令写进页面，写操作要带上它（别的网页没法替你点）。"""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, apps, claude_sync, codex_import, detect, paths

TOKEN = secrets.token_urlsafe(18)
PAGE = Path(__file__).with_name("index.html")


def _status() -> dict:
    claude_sync.forget()
    acc = claude_sync.accounts()
    return {"version": __version__, "home": str(paths.HOME), "claude_home": str(paths.CLAUDE_HOME),
            "codex_home": str(paths.CODEX_HOME), "accounts": acc, "folders": claude_sync.folders(),
            "app_dirs": claude_sync.app_dirs_brief(), "running": apps.status(),
            "jobs": [j for j in apps.pending() if j["state"] == "waiting"],
            "settings": {"codex_mark_imported": codex_import.mark_setting()}}


def _settings_set(k: str, v) -> dict:
    f = paths.HOME / "settings.json"
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    if v:
        if not Path(v).is_dir():
            return {"ok": False, "error": f"目录不存在：{v}"}
        s[k] = str(v)
    else:
        s.pop(k, None)
    f.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
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


def _clean(o):
    return json.loads(json.dumps(o, ensure_ascii=False, default=str))


class H(BaseHTTPRequestHandler):
    server_version = "cc-sessionsync"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(_clean(obj), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self) -> bool:
        h = (self.headers.get("host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                b = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__VERSION__", __version__).encode()
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("cache-control", "no-store")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/api/status":
                return self._json(_status())
            if u.path == "/api/sync/plan":
                claude_sync.forget()
                return self._json(claude_sync.run(q.get("to") or None, True, None, q.get("source") or "desktop"))
            if u.path == "/api/backups":
                return self._json(claude_sync.list_backups())
            if u.path == "/api/codex/plan":
                return self._json(codex_import.run(dry=True, **_codex_kw(q)))
            if u.path == "/api/codex/ledgers":
                return self._json(codex_import.ledgers())
            if u.path == "/api/diag":
                detect.forget()
                r = detect.report()
                return self._json({"report": r, "text": detect.text(r)})
            if u.path == "/api/jobs":
                return self._json(apps.pending())
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("x-token") != TOKEN:
            return self._json({"ok": False, "error": "口令不对（页面过期了？刷新一下）"}, 403)
        n = int(self.headers.get("content-length") or 0)
        try:
            b = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            b = {}
        p = urlparse(self.path).path
        try:
            if p == "/api/sync":
                src = b.get("source") or "desktop"
                pl = claude_sync.run(b.get("to") or None, True, None, src)
                if not pl.get("ok"):
                    return self._json(pl)
                return self._json(apps.run_or_defer("claude", "claude-sync", {"target": b.get("to") or pl["target"]["key"],
                                                    "only": b.get("only"), "source": src}, "同步 Claude 会话到当前账号"))
            if p == "/api/restore":
                return self._json(apps.run_or_defer("claude", "claude-restore", {"backup": b["backup"]}, f"撤回 Claude 同步 {b['backup']}"))
            if p == "/api/codex/import":
                kw = _codex_kw(b)
                if kw["mark"] is None:
                    kw["mark"] = codex_import.mark_setting()
                if kw["desktop"] and apps.running("claude"):
                    return self._json(apps.run_or_defer("claude", "codex-import", kw, "把 Codex 对话导进 Claude Code"))
                return self._json(dict(codex_import.run(dry=False, **kw), deferred=False))
            if p == "/api/codex/undo":
                led = codex_import._load(codex_import.LEDGERS / str(b.get("ledger")) / "ledger.json", {}) or {}
                if led.get("desktop"):
                    return self._json(apps.run_or_defer("claude", "codex-undo", {"ledger": b["ledger"]}, f"撤回 Codex 导入 {b['ledger']}"))
                return self._json(codex_import.undo(str(b.get("ledger"))))
            if p == "/api/settings":
                if "codex_mark_imported" in b:
                    codex_import.set_mark_setting(bool(b["codex_mark_imported"]))
                return self._json({"ok": True, "codex_mark_imported": codex_import.mark_setting()})
            if p == "/api/appdir":
                return self._json(_settings_set("claude_app_dir", b.get("path") or ""))
            if p == "/api/pickdir":
                return self._json(detect.pick_folder(b.get("start") or ""))
            if p == "/api/jobs/cancel":
                return self._json({"ok": apps.cancel(str(b.get("id")))})
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        self._json({"error": "not found"}, 404)


def serve(port: int = 8766, open_browser: bool = True, auto_sync: bool = False) -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"CC Session Sync {__version__}：{url}（Ctrl+C 退出）", flush=True)
    stop = threading.Event()

    def auto():                                    # 界面开着时顺便做自动同步（桌面端没开时补齐）
        while not stop.wait(30):
            try:
                claude_sync.auto_tick()
            except Exception:
                pass
    if auto_sync:
        threading.Thread(target=auto, daemon=True).start()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        claude_sync.auto_release()
    return 0

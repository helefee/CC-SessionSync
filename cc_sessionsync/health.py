# -*- coding: utf-8 -*-
"""更新后的启动自检（0.4.2）：安装程序装完新版本、打开它之前先跑 `CCSessionSync.exe health`。
不开窗口、不动数据：程序文件和清单一致、要用的模块都能加载（窗口、托盘、会话、转换…）、本机服务能起来并回应。
通过 → 退出码 0；不通过 / 卡住超时 → 非 0，安装程序就把上一个版本换回来（见 installer/CCSessionSync.iss）。"""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

MODULES = ("cc_sessionsync.ui", "cc_sessionsync.sessions", "cc_sessionsync.convert", "cc_sessionsync.listcache", "cc_sessionsync.update",
           "cc_sessionsync.selfcheck", "cc_sessionsync.tray", "cc_sessionsync.desktop", "sqlite3", "ssl", "zstandard", "yaml", "tomlkit")


def run(timeout: float = 40, out: str | None = None, fail: bool = False) -> int:
    t0 = time.time()
    res = {"version": __version__, "ok": False, "checks": [], "time": t0}

    def done(code: int):
        res.update(ok=code == 0, seconds=round(time.time() - t0, 2), exit=code)
        if out:
            try:
                Path(out).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        print(json.dumps(res, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(code)

    def dog():                                  # 卡住也要有结果：超时就当失败
        time.sleep(timeout)
        res["checks"].append({"id": "timeout", "ok": False, "detail": f"{timeout:.0f} 秒内没做完"})
        done(3)
    threading.Thread(target=dog, daemon=True).start()

    def step(i, fn):
        try:
            d = fn()
            res["checks"].append({"id": i, "ok": True, "detail": d or ""})
            return True
        except Exception as e:
            res["checks"].append({"id": i, "ok": False, "detail": f"{type(e).__name__}: {e}"[:400]})
            return False

    good = True
    if fail or os.environ.get("CC_SESSIONSYNC_HEALTH_FAIL"):   # 测试用：模拟新版本坏了
        res["checks"].append({"id": "simulated", "ok": False, "detail": "按要求模拟失败"})
        good = False

    def install():
        from . import selfcheck
        r = selfcheck.check_install()
        if r["status"] == "fail":
            raise RuntimeError(r["detail"])
        return r["detail"]
    good &= step("install", install)

    def mods():
        for m in MODULES:
            importlib.import_module(m)
        return f"{len(MODULES)} 个模块"
    good &= step("modules", mods)

    def webview():
        if os.name != "nt":
            return "（不是 Windows，跳过）"
        from . import paths
        try:
            import webview                      # noqa: F401  窗口要用的库能加载
        except ImportError:
            if paths.FROZEN:                    # 安装版必须带着它；源码版没装 pywebview 时用浏览器界面
                raise
            return "（源码版没装 pywebview，跳过）"
        return "pywebview " + str(getattr(webview, "__version__", ""))
    good &= step("webview", webview)

    def server():
        from http.server import ThreadingHTTPServer
        from . import ui
        srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=10) as r:
                body = r.read().decode("utf-8", "replace")
            if "cc-sessionsync" not in body:
                raise RuntimeError("ping 回应不对：" + body[:80])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
                page = r.read().decode("utf-8", "replace")
            if "CC Session Sync" not in page:
                raise RuntimeError("界面页面不对")
            return f"本机服务 OK（{len(page) // 1024} KB 页面）"
        finally:
            srv.shutdown()
    good &= step("server", server)
    done(0 if good else 2)
    return 0

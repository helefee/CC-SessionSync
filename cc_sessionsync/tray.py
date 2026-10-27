# -*- coding: utf-8 -*-
"""Windows 托盘图标 + 右键菜单 + 通知（Shell_NotifyIcon，纯 ctypes，不加依赖）。从 CC Msg 的 tray.py 移植。

- 左键：窗口在前面 → 藏起来；否则显示并拉到前面。双击：打开窗口。
- 右键菜单：显示 / 隐藏窗口、立即同步、自动同步（勾选）、会话管理…、用量…、
  开机自启 / 关窗口藏到托盘 / 启动时最小化 / 通知、检查更新（有新版本时带红点「●」）、退出。
- 图标：平时是程序图标；有排队等桌面端退出的活加橙点，有新版本 / 账号切换没处理加红点。
- 通知（托盘气泡，Win10 / 11 上显示成系统通知；15 秒最多一条，多的合并）：自动同步补了会话、排队的活做完、
  Codex 导入完成、检测到账号切换、有新版本（每个版本只一条）、自检有失败项。点通知打开对应的页面。
  气泡不能带按钮：点了打开窗口，窗口顶上有「立即同步 / 打开」「立即更新 / 稍后 / 忽略此版本」。
- 资源管理器重启（TaskbarCreated）后自动把图标加回来；注销 / 关机 / 安装程序关程序时干净退出。
- 只开一个：再启动一次时，新进程找到这个托盘窗口发「显示」；CCSessionSync.exe --quit 让正在运行的那个退出（安装程序用）。

实例名（测试版不和真装的撞）：环境变量 CC_SESSIONSYNC_INSTANCE 或 --instance，或程序目录里的 instance.txt。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from . import paths


def instance() -> str:
    v = os.environ.get("CC_SESSIONSYNC_INSTANCE", "")
    if not v and paths.APP_DIR and (paths.APP_DIR / "instance.txt").exists():
        try:
            v = (paths.APP_DIR / "instance.txt").read_text(encoding="utf-8").strip()
        except OSError:
            v = ""
    return "".join(c for c in v if c.isalnum())[:16]


def names() -> dict:
    i = instance()
    return {"title": "CC Session Sync" + (" " + i if i else ""), "mutex": "Local\\CCSessionSyncDesktop" + ("-" + i if i else ""),
            "cls": "CCSessionSyncTrayWnd" + ("-" + i if i else ""), "run": "CC Session Sync" + (" " + i if i else ""),
            "aumid": "helefee.CCSessionSync" + ("." + i if i else "")}


# ---------- 开机自启（当前用户 Run 键，不要管理员） ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


DEFAULT_PORT = 8766


def autostart_cmd(port: int = DEFAULT_PORT) -> str:
    extra = (f" --port {port}" if port != DEFAULT_PORT else "") + (f" --instance {instance()}" if instance() else "")
    if paths.FROZEN:
        return f'"{paths.EXE}" desktop --autostart{extra}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    root = str(paths.PKG.parent).replace("'", "")
    return (f'"{pyw}" -c "import sys;sys.path.insert(0,r\'{root}\');from cc_sessionsync.cli import main;'
            f'sys.exit(main([\'desktop\',\'--autostart\'' + "".join(f",'{a}'" for a in extra.split()) + ']))"')


def autostart_value() -> str | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, names()["run"])[0]
    except OSError:
        return None


def autostart_get() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, names()["run"])
        return bool(v)
    except OSError:
        return False


def autostart_set(on: bool, port: int = DEFAULT_PORT) -> None:
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, names()["run"], 0, winreg.REG_SZ, autostart_cmd(port))
        else:
            try:
                winreg.DeleteValue(k, names()["run"])
            except OSError:
                pass


# ---------- 给别的进程用：找正在运行的那个 ----------
WM_APP = 0x8000
WM_TRAYCB = WM_APP + 1
CMD_SHOW = WM_APP + 10
CMD_QUIT = WM_APP + 11
CMD_MENU = WM_APP + 12          # 测试用：在 (x, y) 弹出菜单
CMD_NOTE = WM_APP + 13          # 测试用：弹一条示例通知


def find_running():
    """正在运行的托盘窗口 (hwnd, pid)；没有 → (None, None)。"""
    if os.name != "nt":
        return None, None
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    u32.FindWindowW.restype = wintypes.HWND
    h = u32.FindWindowW(names()["cls"], None)
    if not h:
        return None, None
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return h, pid.value


def post(cmd: int, wp: int = 0, lp: int = 0) -> bool:
    import ctypes
    h, pid = find_running()
    if not h:
        return False
    u32 = ctypes.windll.user32
    if cmd == CMD_SHOW and pid:
        u32.AllowSetForegroundWindow(pid)       # 我是用户刚启动的进程，有前台权；交给它
    u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    return bool(u32.PostMessageW(h, cmd, wp, lp))


def mutex_exists() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.OpenMutexW.restype = ctypes.c_void_p
    h = k32.OpenMutexW(0x00100000, False, names()["mutex"])     # SYNCHRONIZE
    if h:
        k32.CloseHandle(ctypes.c_void_p(h))
        return True
    return False


# ---------- 托盘本体 ----------

class Tray:
    """app 要提供：show() / hide() / visible() / front_hwnd() / open(target: dict) / browser() / quit() / restart() / check_update()
    / hard_quit()，以及 port。"""

    def __init__(self, app):
        self.app = app
        self.n = names()
        self.hwnd = None
        self.state = {}
        self.seq = -1
        self.icon_kind = "base"
        self.icons = {}
        self.last_note = 0.0
        self.pending: list[dict] = []
        self.note_target = None
        self.was_front = False
        self.ignore_select_until = 0.0
        self.ending = False
        self.ready = threading.Event()
        self._added = False

    # --- 数据（同一进程里直接调 ui 的函数，不走 HTTP） ---
    def refresh(self) -> dict:
        from . import ui
        try:
            d = ui.tray_state(self.seq)
        except Exception as e:
            print("[tray] 取状态出错：", e)
            return self.state
        first = self.seq < 0
        self.seq = d.get("seq", self.seq)
        self.state = d
        if not first:
            for ev in d.get("events") or []:
                self._queue_note(ev)
        self._update_icon()
        return d

    def tooltip(self) -> str:
        d = self.state
        t = self.n["title"]
        if d.get("auto_sync"):
            t += " · 自动同步开"
        if d.get("jobs"):
            t += f" · {d['jobs']} 个活在等桌面端退出"
        if (d.get("update") or {}).get("badge"):
            t += f"\n有新版本 {d['update'].get('latest')}"
        return t[:127]

    # --- Win32 ---
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="tray").start()
        threading.Thread(target=self._poll, daemon=True, name="tray-poll").start()
        self.ready.wait(5)

    def _poll(self):
        self.ready.wait(10)
        while True:
            self.refresh()
            for _ in range(8):           # 4 秒一轮；中间顺便记一下前台窗口（判断左键是「藏」还是「显示」）
                self._track_front()
                time.sleep(0.5)
            self._flush_notes()

    def _track_front(self):
        import ctypes
        u32 = ctypes.windll.user32
        u32.GetForegroundWindow.restype = ctypes.c_void_p
        fg = u32.GetForegroundWindow()
        if not fg:
            return
        buf = ctypes.create_unicode_buffer(64)
        u32.GetClassNameW(ctypes.c_void_p(fg), buf, 64)
        if buf.value in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
                         "#32768", "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"):
            return                      # 点托盘 / 弹菜单时前台是任务栏，不算
        own = self.app.front_hwnd()
        self.was_front = bool(own) and fg == own

    def _run(self):
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        u32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u32.DefWindowProcW.restype = LRESULT
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        k32.GetModuleHandleW.restype = wintypes.HMODULE

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        self.WM_TASKBAR = u32.RegisterWindowMessageW("TaskbarCreated")
        self._proc = WNDPROC(self._wndproc)          # 留着引用，免得被回收
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=self.n["cls"])
        u32.RegisterClassW(ctypes.byref(wc))
        # 隐藏的顶层窗口（不用 message-only：那种收不到 TaskbarCreated 广播）
        self.hwnd = u32.CreateWindowExW(0, self.n["cls"], self.n["title"], 0, 0, 0, 0, 0, None, None, hinst, None)
        try:   # 别的完整性级别的进程也能发 TaskbarCreated 给我们
            u32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.DWORD, ctypes.c_void_p]
            u32.ChangeWindowMessageFilterEx(self.hwnd, self.WM_TASKBAR, 1, None)
        except Exception:
            pass
        self._load_icons()
        self._nid_add()
        self.ready.set()
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _load_icons(self):
        import ctypes
        u32 = ctypes.windll.user32
        u32.LoadImageW.restype = ctypes.c_void_p
        cx = u32.GetSystemMetrics(49) or 16            # SM_CXSMICON（随 DPI）
        try:
            dpi = u32.GetDpiForWindow(self.hwnd) or 96
            cx = max(cx, round(16 * dpi / 96))
        except Exception:
            pass
        for k, f in (("base", "icon.ico"), ("active", "tray-active.ico"), ("mail", "tray-badge.ico")):
            p = paths.PKG / f
            h = u32.LoadImageW(None, str(p), 1, cx, cx, 0x10) if p.exists() else None   # IMAGE_ICON, LR_LOADFROMFILE
            self.icons[k] = h or self.icons.get("base")

    def _nid(self, flags: int, info: tuple | None = None):
        import ctypes
        from ctypes import wintypes

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                        ("uCallbackMessage", ctypes.c_uint), ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
                        ("uVersion", ctypes.c_uint), ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p)]
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd = self.hwnd
        n.uID = 1
        n.uFlags = flags
        n.uCallbackMessage = WM_TRAYCB
        n.hIcon = self.icons.get(self.icon_kind) or self.icons.get("base")
        n.szTip = self.tooltip()
        if info:
            n.szInfoTitle, n.szInfo = info[0][:63], info[1][:255]
            n.dwInfoFlags = 0x4 | 0x20                  # NIIF_USER（用托盘图标）| NIIF_LARGE_ICON
            n.hBalloonIcon = self.icons.get("base")
        return n

    def _shell(self, op: int, n) -> bool:
        import ctypes
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(op, ctypes.byref(n)))

    def _nid_add(self):
        n = self._nid(0x1 | 0x2 | 0x4 | 0x80)            # MESSAGE | ICON | TIP | SHOWTIP
        self._added = self._shell(0, n)                  # NIM_ADD
        n.uVersion = 4                                   # NOTIFYICON_VERSION_4
        self._shell(4, n)                                # NIM_SETVERSION

    def _update_icon(self):
        d = self.state
        self.icon_kind = "mail" if (d.get("update") or {}).get("badge") or d.get("attention") else "active" if d.get("jobs") else "base"
        if self.hwnd:
            if not self._added:
                self._nid_add()
            self._shell(1, self._nid(0x2 | 0x4 | 0x80))  # NIM_MODIFY

    def remove(self):
        if self.hwnd and self._added:
            self._shell(2, self._nid(0))                 # NIM_DELETE
            self._added = False

    # --- 通知 ---
    def _queue_note(self, ev: dict):
        if not (self.state.get("settings") or {}).get("notify", True):
            return
        if self.was_front and ev.get("kind") not in ("update", "account"):
            return                                       # 窗口就在前面看着：不打扰
        self.pending.append(ev)
        self._flush_notes()

    def _flush_notes(self):
        if not self.pending or time.time() - self.last_note < 15:
            return
        evs, self.pending = self.pending, []
        if len(evs) == 1:
            ev = evs[0]
            self.notify(ev["title"], ev.get("text") or "", ev.get("open"))
        else:
            self.notify(f"{len(evs)} 条新通知", "；".join(e["title"] for e in evs[:4]), evs[-1].get("open") or {})

    def notify(self, title: str, text: str, target: dict | None = None):
        self.last_note = time.time()
        self.note_target = target
        if self.hwnd:
            self._shell(1, self._nid(0x10 | 0x80, (title, text or " ")))   # NIF_INFO

    # --- 消息 ---
    def _wndproc(self, hwnd, msg, wp, lp):
        import ctypes
        u32 = ctypes.windll.user32
        try:
            if msg == WM_TRAYCB:
                ev = lp & 0xFFFF
                if ev == 0x400:                          # NIN_SELECT（单击）
                    if time.time() > self.ignore_select_until:
                        self._toggle()
                elif ev == 0x203:                        # WM_LBUTTONDBLCLK
                    self.ignore_select_until = time.time() + 0.6
                    self.app.show()
                elif ev == 0x7B:                         # WM_CONTEXTMENU
                    x, y = ctypes.c_short(wp & 0xFFFF).value, ctypes.c_short((wp >> 16) & 0xFFFF).value
                    self.menu(x, y)
                elif ev == 0x405:                        # NIN_BALLOONUSERCLICK
                    self.app.open(self.note_target or {})
                return 0
            if msg == self.WM_TASKBAR:                   # 资源管理器重启了：重新加图标
                self._added = False
                self._load_icons()
                self._nid_add()
                return 0
            if msg == CMD_SHOW:
                self.app.show()
                return 0
            if msg == CMD_QUIT:
                threading.Thread(target=self.app.hard_quit, daemon=True).start()
                return 0
            if msg == CMD_MENU:
                self.menu(wp, lp)
                return 0
            if msg == CMD_NOTE:
                self.notify("测试通知：检测到 Claude Code 账号已切换", "a***@example.com → b***@example.com（点这里打开「账号同步」）", {"page": "sync"})
                return 0
            if msg == 0x11:                              # WM_QUERYENDSESSION（关机 / 注销 / 安装程序要关我们）
                self.ending = True
                return 1
            if msg == 0x16:                              # WM_ENDSESSION
                if wp:
                    self.ending = True
                    self.remove()
                    threading.Thread(target=self.app.hard_quit, daemon=True).start()
                return 0
            if msg == 0x10:                              # WM_CLOSE 发到托盘窗口 = 退出
                self.ending = True
                threading.Thread(target=self.app.hard_quit, daemon=True).start()
                return 0
        except Exception as e:
            print("[tray]", e)
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def _toggle(self):
        if self.app.visible() and self.was_front:
            self.app.hide()
            self.was_front = False
        else:
            self.app.show()
            self.was_front = True

    # --- 菜单 ---
    def menu_model(self) -> list:
        """[(id, 文字, 选项)] / ("-",) / ("sub", 文字, [...])；测试也用它。"""
        d = self.state
        s = d.get("settings") or {}
        u = d.get("update") or {}
        vis = self.app.visible()
        items = [(1, "隐藏窗口" if vis else "显示窗口", {"default": True}),
                 ("-",),
                 (5, "立即同步 Claude 会话", {}),
                 (6, "自动同步（切号后补齐会话）", {"check": bool(s.get("auto_sync"))}),
                 (7, "会话转换（Codex → Claude Code）…", {}),
                 (8, "会话管理…", {}),
                 (14, "时间线与备份…", {}),
                 (3, "用量…", {})]
        if d.get("jobs"):
            items.append((9, f"{d['jobs']} 个活在等桌面端退出…", {}))
        items += [("-",),
                  (10, "开机自启", {"check": autostart_get()}),
                  (11, "关闭窗口时最小化到托盘", {"check": s.get("tray_close", True)}),
                  (12, "启动时最小化到托盘", {"check": bool(s.get("start_min"))}),
                  (13, "通知", {"check": s.get("notify", True)}),
                  ("-",),
                  (20, f"● 有新版本 {u.get('latest')}…" if u.get("badge") else "检查更新", {}),
                  (22, "诊断自检…", {}),
                  ("-",), (30, "退出", {})]
        return items

    def menu(self, x: int, y: int):
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        self.refresh()
        u32.CreatePopupMenu.restype = wintypes.HMENU
        u32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_size_t, wintypes.LPCWSTR]
        u32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_uint]
        u32.DestroyMenu.argtypes = [wintypes.HMENU]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

        def build(items):
            hm = u32.CreatePopupMenu()
            for it in items:
                if it[0] == "-":
                    u32.AppendMenuW(hm, 0x800, 0, None)                     # MF_SEPARATOR
                elif it[0] == "sub":
                    u32.AppendMenuW(hm, 0x10, build(it[2]), it[1])          # MF_POPUP
                else:
                    i, t, o = it
                    f = (0x8 if o.get("check") else 0) | (0x1 if o.get("gray") else 0)   # MF_CHECKED / MF_GRAYED
                    u32.AppendMenuW(hm, f, i, t)
                    if o.get("default"):
                        u32.SetMenuDefaultItem(hm, i, 0)
            return hm
        hm = build(self.menu_model())
        u32.SetForegroundWindow(self.hwnd)                                  # 不然点别处菜单不消失
        u32.TrackPopupMenu.argtypes = [wintypes.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        cmd = u32.TrackPopupMenu(hm, 0x100 | 0x20 | 0x8, x, y, 0, self.hwnd, None)   # RETURNCMD | BOTTOMALIGN | RIGHTALIGN
        u32.PostMessageW(self.hwnd, 0, 0, 0)
        u32.DestroyMenu(hm)
        if cmd:
            threading.Thread(target=self._do, args=(cmd,), daemon=True).start()

    def _do(self, cmd: int):
        from . import settings, ui
        d = self.state
        try:
            if cmd == 1:
                self.app.hide() if self.app.visible() else self.app.show()
            elif cmd == 5:
                r = ui.sync_now()
                if r.get("deferred"):           # 桌面端开着：打开窗口问用户「关闭并同步（完成后重启）/ 等它退出 / 取消」
                    self.app.open({"page": "sync", "closeRun": {"kind": "claude", "jobs": [r.get("job")], "verb": "同步"}})
                elif r.get("ok") is False:
                    self.notify("同步没做成", (r.get("error") or "")[:200], {"page": "sync"})
                else:
                    self.notify("同步完成", f"补了 {r.get('done', 0)} 个、刷新 {r.get('updated', 0)} 个会话", {"page": "sync"})
            elif cmd == 6:
                settings.update(auto_sync=not settings.get("auto_sync", False))
                self.refresh()
            elif cmd in (3, 7, 8, 9, 14, 22):
                self.app.open({"page": "convert", "src": "codex", "dst": "claude"} if cmd == 7 else
                              {"page": "timeline", "tab": "tl"} if cmd == 14 else
                              {"page": {3: "usage", 8: "sessions", 9: "jobs", 22: "selfcheck"}[cmd]})
            elif cmd == 10:
                autostart_set(not autostart_get(), self.app.port)
            elif cmd in (11, 12, 13):
                k = {11: "tray_close", 12: "start_min", 13: "notify"}[cmd]
                settings.update(**{k: not settings.get(k, k != "start_min")})
                self.refresh()
            elif cmd == 20:
                self.app.check_update()
            elif cmd == 30:
                self.app.quit()
        except Exception as e:
            print("[tray] 菜单操作出错：", e)

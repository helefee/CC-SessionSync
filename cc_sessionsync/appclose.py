# -*- coding: utf-8 -*-
"""「关闭并执行（完成后重启）」：桌面端开着时，帮用户把它正常关掉 → 做排队的改动 → 按原来的方式重新打开。

为什么要关：桌面端把会话登记放在内存里，退出时写回磁盘，开着的时候改会被盖回去（见 apps.py）。以前只能排队等用户自己退出；
0.2.3 起可以让本程序代劳：

1. 关之前记下它是怎么开的：商店版（MSIX）记 AUMID（Get-AppxPackage + 清单里的 Application Id，不行再 Get-StartApps），
   安装版（Squirrel，…\\AnthropicClaude\\app-x.y.z\\claude.exe）记外层的 …\\AnthropicClaude\\claude.exe，别的就记 exe 路径。
2. 正常关闭：给它的主窗口发 WM_CLOSE，再 taskkill /PID（不带 /F，等于请它自己退出）。最多等 15 秒（界面上有进度）；
   还没退干净就问用户：继续等 / 改成排队等它退出 / 强制结束（要再确认一次）。只有强制结束才用 taskkill /T /F，
   而且只结束桌面端自己和它开的子进程（关之前列给用户看过）；不是桌面端开的命令行 claude 进程永远不碰，只列出来。
3. 退干净后再等几秒（退出时写盘有个尾巴），依次做排队的改动（和后台等待进程做的一样）。
4. 按记下的方式重新打开（商店版：explorer.exe shell:AppsFolder\\<AUMID>；安装版：外层 claude.exe），
   确认真的起来了，结果写进界面和系统通知；没能打开就明说，请用户手动打开。

进程层（快照 / 关 / 强杀 / 启动 / 查 AUMID）都在 RealSys 里；测试设 CC_SESSIONSYNC_FAKE_PROCTABLE 指向一个 JSON 文件就换成 FakeSys，
什么真进程都不碰。
"""
from __future__ import annotations

import json
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

NOWIN = 0x08000000 if os.name == "nt" else 0
APP = {"claude": "Claude", "codex": "Codex"}
WAIT = 15.0          # 正常关闭最多等多久，再问要不要强制结束
TAIL = 5.0           # 退干净后再等一会儿（退出时写盘有个尾巴）
LAUNCH_WAIT = 30.0   # 重新打开后最多等多久确认它起来了
MSIX_DEFAULT = "Claude_pzs8sxrjxfjjc!Claude"


def _envf(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k) or d)
    except ValueError:
        return d


# ---------- 进程层 ----------

class RealSys:
    """真的进程操作。Windows 用进程快照 / WM_CLOSE / taskkill；别的系统用 ps / 信号。"""
    fake = False

    @property
    def win(self) -> bool:
        return os.name == "nt"

    def snap(self) -> list[dict]:
        if os.name == "nt":
            from . import detect
            st = {"total": 0, "denied": 0, "cmd_denied": 0, "errors": []}
            return [{"pid": p["pid"], "ppid": p.get("ppid"), "name": p["name"], "exe": p["exe"] or "", "cmd": p["cmd"] or ""}
                    for p in detect._win_procs(st)]
        try:
            r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, timeout=10)
        except Exception:
            return []
        out = []
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            cmd = parts[2]
            out.append({"pid": int(parts[0]), "ppid": int(parts[1]) if parts[1].isdigit() else None,
                        "name": Path(cmd.split(" ")[0]).name, "exe": cmd.split(" ")[0], "cmd": cmd})
        return out

    def exists(self, p: str) -> bool:
        return Path(p).exists()

    def close(self, pids: list[int], mains: list[int]) -> dict:
        """请它自己退出：主窗口 WM_CLOSE + taskkill（不带 /F）。不强杀。"""
        if os.name == "nt":
            n = _post_wm_close(set(pids))
            for m in mains:
                try:
                    subprocess.run(["taskkill", "/PID", str(m)], capture_output=True, timeout=15, creationflags=NOWIN)
                except Exception:
                    pass
            return {"windows": n}
        import signal
        for m in mains:
            try:
                os.kill(m, signal.SIGTERM)
            except Exception:
                pass
        return {"windows": 0}

    def kill(self, pids: list[int], mains: list[int]) -> None:
        """强制结束：只在用户第二次确认后调。/T 只带上桌面端自己的子进程（关之前列给用户看过）。"""
        if os.name == "nt":
            for m in mains:
                subprocess.run(["taskkill", "/PID", str(m), "/T", "/F"], capture_output=True, timeout=15, creationflags=NOWIN)
            for p in pids:
                subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True, timeout=15, creationflags=NOWIN)
            return
        import signal
        for p in list(mains) + list(pids):
            try:
                os.kill(p, signal.SIGKILL)
            except Exception:
                pass

    def launch(self, info: dict) -> None:
        if info.get("aumid"):
            subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + info["aumid"]], creationflags=NOWIN, close_fds=True)
        elif info.get("app"):
            subprocess.Popen(["open", info["app"]])
        else:
            exe = info["exe"]
            if os.name == "nt":
                subprocess.Popen([exe], cwd=str(Path(exe).parent), creationflags=0x00000008 | 0x00000200, close_fds=True)
            else:
                subprocess.Popen([exe], start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def aumid(self, pkg_full: str) -> tuple[str | None, str]:
        """商店版包全名（Claude_1.2.3.0_x64__pzs8sxrjxfjjc）→ AUMID。先读包清单，不行再查开始菜单。"""
        if os.name != "nt":
            return None, "not-windows"
        name, fam = _pkg_family(pkg_full)
        if not fam:
            return None, "bad-package"
        ps1 = (f"$p = Get-AppxPackage -Name '{name}' | Where-Object {{ $_.PackageFamilyName -eq '{fam}' }} | Select-Object -First 1; "
               "if ($p) { $m = Get-AppxPackageManifest $p; $id = @($m.Package.Applications.Application)[0].Id; "
               "if ($id) { $p.PackageFamilyName + '!' + $id } }")
        ps2 = f"(Get-StartApps | Where-Object {{ $_.AppID -like '{fam}!*' }} | Select-Object -First 1).AppID"
        for via, cmd in (("Get-AppxPackage", ps1), ("Get-StartApps", ps2)):
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                                   timeout=30, stdin=subprocess.DEVNULL, creationflags=NOWIN)
                a = r.stdout.decode("utf-8", "replace").strip().splitlines()
                if a and a[-1].strip().startswith(fam + "!"):
                    return a[-1].strip(), via
            except Exception:
                pass
        return None, "not-found"


def _post_wm_close(pids: set) -> int:
    """给这些进程的主窗口（可见、没有 owner 的顶层窗口）发 WM_CLOSE。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    hits = []
    PROTO = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _l):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in pids and u32.IsWindowVisible(h) and not u32.GetWindow(h, 4):     # GW_OWNER
            hits.append(h)
        return True
    u32.EnumWindows(PROTO(cb), 0)
    for h in hits:
        u32.PostMessageW(h, 0x0010, 0, 0)                        # WM_CLOSE
    return len(hits)


class FakeSys:
    """测试用：进程表是一个 JSON 文件，关 / 杀 / 启动都只改这个文件并记进 log。
    {"procs": [{"pid","ppid","name","exe","cmd"}], "stubborn": false（不肯正常退）, "exit_after": 秒,
     "launch_fail": false（启动报错）, "launch_noshow": false（启动了但没起来）, "aumid": "...", "missing": [路径], "log": []}"""
    fake = True
    win = True
    _lk = threading.Lock()

    def __init__(self, path: str):
        self.p = Path(path)

    def _ld(self) -> dict:
        d = json.loads(self.p.read_text(encoding="utf-8"))
        at = d.get("closing_at")
        if at and time.time() >= at:
            gone = _tree(d["procs"], set(d.get("closing") or []))
            d["procs"] = [p for p in d["procs"] if p["pid"] not in gone]
            d["closing_at"], d["closing"] = None, None
        return d

    def _sv(self, d: dict) -> None:
        tmp = self.p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.p)

    def _do(self, fn):
        with self._lk:
            d = self._ld()
            r = fn(d)
            self._sv(d)
            return r

    def snap(self) -> list[dict]:
        return self._do(lambda d: [dict(p) for p in d["procs"]])

    def exists(self, p: str) -> bool:
        return self._do(lambda d: p not in (d.get("missing") or []))

    def close(self, pids, mains) -> dict:
        def f(d):
            d.setdefault("log", []).append(["close", sorted(pids), sorted(mains)])
            if not d.get("stubborn"):
                d["closing"], d["closing_at"] = sorted(pids), time.time() + float(d.get("exit_after", 0.5))
            return {"windows": len(mains)}
        return self._do(f)

    def kill(self, pids, mains) -> None:
        def f(d):
            d.setdefault("log", []).append(["kill", sorted(pids), sorted(mains)])
            gone = _tree(d["procs"], set(mains)) | set(pids)
            d["procs"] = [p for p in d["procs"] if p["pid"] not in gone]
        self._do(f)

    def launch(self, info: dict) -> None:
        def f(d):
            d.setdefault("log", []).append(["launch", info])
            if d.get("launch_fail"):
                raise OSError("启动失败（测试）")
            if not d.get("launch_noshow"):
                pid = max([p["pid"] for p in d["procs"]] + [1000]) + 1
                d["procs"].append({"pid": pid, "ppid": 1, "name": "claude.exe", "exe": info.get("source") or info.get("exe") or "", "cmd": ""})
        self._do(f)

    def aumid(self, pkg_full: str):
        return self._do(lambda d: (d.get("aumid"), "fake"))


_REAL = RealSys()


def SYS():
    f = os.environ.get("CC_SESSIONSYNC_FAKE_PROCTABLE")
    return FakeSys(f) if f else _REAL


# ---------- 认进程 ----------

def _pkg_family(pkg_full: str) -> tuple[str | None, str | None]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", pkg_full or "") or "__" not in pkg_full:
        return None, None
    name = pkg_full.split("_")[0]
    return name, name + "_" + pkg_full.split("__")[-1]


def is_app(kind: str, p: dict, win: bool) -> bool:
    exe = (p.get("exe") or "").lower().replace("/", "\\")
    name = (p.get("name") or "").lower()
    if kind == "claude":
        # 桌面端：商店版在 WindowsApps\Claude_…，安装版在 AnthropicClaude；Claude Code 命令行（…\claude-code\…）不算
        if not win:
            return "/claude.app/contents/macos/claude" in (p.get("exe") or "").lower()
        return name == "claude.exe" and "claude-code" not in exe and ("\\windowsapps\\claude_" in exe or "anthropicclaude" in exe)
    if kind == "codex":
        if not win:
            return "codex.app/contents/macos" in (p.get("exe") or "").lower() or "app-server" in (p.get("cmd") or "")
        return ("openai.codex" in exe and name in ("chatgpt.exe", "codex.exe")) or (name == "codex.exe" and "app-server" in (p.get("cmd") or ""))
    return False


def is_cli(kind: str, p: dict, win: bool) -> bool:
    """命令行会话（Claude Code 的 claude / Codex 的 codex），不是桌面端本身。"""
    if is_app(kind, p, win):
        return False
    name = (p.get("name") or "").lower()
    name = name[:-4] if name.endswith(".exe") else name
    cmd = (p.get("cmd") or "").lower()
    if kind == "claude":
        return name == "claude" or (name in ("node", "bun") and ("claude-code" in cmd or "@anthropic-ai" in cmd))
    return name == "codex" and "app-server" not in cmd


def _tree(procs: list[dict], roots: set) -> set:
    """roots 和它们所有的子孙进程。"""
    kids: dict = {}
    for p in procs:
        kids.setdefault(p.get("ppid"), []).append(p["pid"])
    out, todo = set(roots), list(roots)
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in out:
                out.add(c)
                todo.append(c)
    return out


def _brief(p: dict) -> dict:
    exe = p.get("exe") or ""
    home = str(Path.home())
    if home and exe.lower().startswith(home.lower()):
        exe = "~" + exe[len(home):]
    return {"pid": p["pid"], "name": p.get("name") or "", "exe": exe}


# ---------- 怎么开的、怎么重新打开 ----------

def launch_info(kind: str, procs: list[dict]) -> dict | None:
    """在它还开着时记下怎么重新打开。"""
    s = SYS()
    ids = {p["pid"] for p in procs}
    main = [p for p in procs if p.get("ppid") not in ids] or procs
    exe = next((p["exe"] for p in main if p.get("exe") and (p.get("name") or "").lower() != "codex.exe"), None) or \
        next((p["exe"] for p in procs if p.get("exe")), None)
    if not exe:
        return None
    low = exe.lower().replace("/", "\\")
    if s.win and "\\windowsapps\\" in low:
        parts = re.split(r"[\\/]", exe)
        i = [x.lower() for x in parts].index("windowsapps")
        pkg = parts[i + 1] if i + 1 < len(parts) else ""
        aid, via = s.aumid(pkg)
        if not aid:
            name, fam = _pkg_family(pkg)
            if kind == "claude" and fam and fam.lower().startswith("claude_"):
                aid, via = fam + "!Claude", "guess"
        if aid:
            return {"how": "msix", "aumid": aid, "package": pkg, "via": via, "source": exe}
        return {"how": "exe", "exe": exe, "source": exe, "note": "商店版没查到 AUMID，按 exe 路径打开"}
    if s.win:
        m = re.match(r"^(.*\\anthropicclaude)\\app-[^\\]+\\claude\.exe$", low)
        if m:
            stub = exe[:len(m.group(1))] + "\\claude.exe"
            if s.exists(stub):
                return {"how": "squirrel", "exe": stub, "source": exe}
        return {"how": "exe", "exe": exe, "source": exe}
    if sys.platform == "darwin" and ".app/" in exe:
        return {"how": "app", "app": exe[: exe.index(".app/") + 4], "source": exe}
    return {"how": "exe", "exe": exe, "source": exe}


def describe(info: dict | None) -> str:
    if not info:
        return "没找到它的程序路径，关掉后需要你手动打开"
    if info.get("aumid") or info.get("how") == "msix":
        return f"商店版（MSIX）：shell:AppsFolder\\{info.get('aumid')}" + ("（按缺省名猜的）" if info.get("via") == "guess" else "")
    if info.get("how") == "squirrel":
        return f"安装版：{info['exe']}"
    if info.get("app"):
        return f"应用：{info['app']}"
    return f"程序：{info.get('exe')}"


def relaunch(info: dict | None) -> tuple[bool, str | None]:
    if not info:
        return False, "不知道它是怎么开的"
    try:
        SYS().launch(info)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def app_procs(kind: str) -> list[dict]:
    s = SYS()
    return [p for p in s.snap() if is_app(kind, p, s.win)]


def verify(kind: str, timeout: float | None = None) -> bool:
    t0, timeout = time.time(), _envf("CC_SESSIONSYNC_LAUNCH_WAIT", LAUNCH_WAIT) if timeout is None else timeout
    while True:
        if app_procs(kind):
            return True
        if time.time() - t0 >= timeout:
            return False
        time.sleep(0.5)


# ---------- 关之前给用户看的 ----------

def plan(kind: str) -> dict:
    if kind not in APP:
        raise ValueError("kind 只能是 claude / codex")
    from . import apps
    s = SYS()
    ps = s.snap()
    app = [p for p in ps if is_app(kind, p, s.win)]
    jobs = [{"id": j["id"], "title": j.get("title") or j["op"], "verb": j.get("verb")} for j in apps._load_q()
            if j.get("kind") == kind and j.get("state") == "waiting"]
    base = {"kind": kind, "app": APP[kind], "name": apps.NAMES[kind], "jobs": jobs, "busy": _active(kind)}
    if not app:
        return dict(base, running=False)
    ids = {p["pid"] for p in app}
    mains = sorted(p["pid"] for p in app if p.get("ppid") not in ids)
    tree = _tree(ps, ids)
    cli = [p for p in ps if is_cli(kind, p, s.win) and p["pid"] not in ids]
    li = launch_info(kind, app)
    return dict(base, running=True, pids=sorted(ids), mains=mains, launch=li, launch_text=describe(li),
                cli_children=[_brief(p) for p in cli if p["pid"] in tree],
                cli_other=[_brief(p) for p in cli if p["pid"] not in tree])


# ---------- 关 → 做 → 重新打开（后台线程） ----------

RUNS: dict[str, dict] = {}
_LK = threading.Lock()
LIVE = ("closing", "ask_force", "forcing", "tail", "running", "relaunching")


def _active(kind: str | None = None) -> dict | None:
    for r in RUNS.values():
        if r["state"] in LIVE and (kind is None or r["kind"] == kind):
            return view(r["id"])
    return None


def view(rid: str) -> dict:
    r = RUNS.get(rid)
    if not r:
        raise ValueError("没有这个任务")
    return {k: v for k, v in r.items() if not k.startswith("_")}


def start(kind: str, job_ids: list | None = None) -> dict:
    from . import apps
    if kind not in APP:
        raise ValueError("kind 只能是 claude / codex")
    with _LK:
        if _active(kind):
            raise ValueError(f"已经在关闭 {APP[kind]} 了")
        pl = plan(kind)
        rid = uuid.uuid4().hex[:8]
        q = apps._load_q()
        mine = [j for j in q if j.get("kind") == kind and j.get("state") == "waiting"]      # 同一个桌面端排队的一起做
        if not mine:
            raise ValueError("没有排队中的改动")
        if job_ids and not any(j["id"] in job_ids for j in mine):
            raise ValueError("这个改动已经不在队列里了")
        for j in mine:
            j["state"], j["runner"] = "closing", rid
        apps._save_q(q)
        r = {"id": rid, "kind": kind, "app": APP[kind], "state": "closing" if pl["running"] else "tail", "t0": time.time(),
             "wait": _envf("CC_SESSIONSYNC_CLOSE_WAIT", WAIT), "elapsed": 0.0, "remaining": len(pl.get("pids") or []),
             "was_running": pl["running"], "launch": pl.get("launch"), "launch_text": pl.get("launch_text"),
             "cli_children": len(pl.get("cli_children") or []), "jobs": [{"id": j["id"], "title": j.get("title"), "state": "closing"} for j in mine],
             "forced": False, "relaunched": None, "relaunch_error": None, "error": None, "note": None, "_force": False, "_stop": False}
        RUNS[rid] = r
    threading.Thread(target=_run, args=(r,), daemon=True, name=f"closerun-{rid}").start()
    return view(rid)


def force(rid: str) -> dict:
    r = RUNS.get(rid)
    if not r or r["state"] not in ("closing", "ask_force"):
        raise ValueError("现在不能强制结束")
    r["_force"] = True
    return view(rid)


def stop(rid: str) -> dict:
    r = RUNS.get(rid)
    if not r or r["state"] not in ("closing", "ask_force"):
        raise ValueError("已经关掉了，停不下来了")
    r["_stop"] = True
    return view(rid)


def _set_jobs(r: dict, state_from: str, state_to: str) -> None:
    from . import apps
    q = apps._load_q()
    for j in q:
        if j.get("runner") == r["id"] and j.get("state") == state_from:
            j["state"] = state_to
            if state_to == "waiting":
                j.pop("runner", None)
    apps._save_q(q)


def _run(r: dict) -> None:
    from . import apps, events
    kind, s = r["kind"], SYS()
    try:
        if r["was_running"]:
            ps = app_procs(kind)
            ids = {p["pid"] for p in ps}
            s.close(sorted(ids), sorted(p["pid"] for p in ps if p.get("ppid") not in ids))
            apps._log(f"关闭并执行 {r['id']}：请 {apps.NAMES[kind]} 正常退出（{len(ids)} 个进程）")
            t0 = time.time()
            while True:
                left = app_procs(kind)
                r["remaining"], r["elapsed"] = len(left), round(time.time() - t0, 1)
                if not left:
                    break
                if r["_stop"]:
                    _set_jobs(r, "closing", "waiting")
                    apps._spawn_watcher(kind)
                    r.update(state="stopped", note=f"没关：改动留在队列里，{apps.NAMES[kind]} 退出后自动做")
                    apps._log(f"关闭并执行 {r['id']}：用户停止，改回排队")
                    return
                if r["_force"] and not r["forced"]:
                    lid = {p["pid"] for p in left}
                    s.kill(sorted(lid), sorted(p["pid"] for p in left if p.get("ppid") not in lid))
                    r.update(forced=True, state="forcing")
                    apps._log(f"关闭并执行 {r['id']}：用户确认后强制结束 {len(lid)} 个进程")
                elif not r["forced"] and r["elapsed"] >= r["wait"]:
                    r["state"] = "ask_force"
                time.sleep(0.3)
            r["state"] = "tail"
            time.sleep(_envf("CC_SESSIONSYNC_CLOSE_TAIL", TAIL))
        r["state"] = "running"
        q, ops, ok = apps._load_q(), apps._ops(), True
        for j in q:
            if j.get("runner") != r["id"] or j.get("state") != "closing":
                continue
            if app_procs(kind):                    # 中途又被打开了：剩下的改回排队
                r["note"] = f"{apps.NAMES[kind]} 中途又被打开了，没做完的改回排队"
                break
            try:
                res = ops[j["op"]](**j["args"])
                j["state"], j["result"] = "done", apps._brief(res)
                ok = ok and not (isinstance(res, dict) and res.get("ok") is False)
            except Exception as e:
                j["state"], j["result"], ok = "failed", f"{type(e).__name__}: {e}", False
            j["finished"] = time.time()
            apps._log(f"关闭并执行 {r['id']}：{j['title']} → {j['state']} {j['result']}")
            apps._save_q(q)
            for x in r["jobs"]:
                if x["id"] == j["id"]:
                    x.update(state=j["state"], result=j["result"])
        apps._save_q(q)
        if r["note"]:
            _set_jobs(r, "closing", "waiting")
            apps._spawn_watcher(kind)
        if r["was_running"] and not app_procs(kind):
            r["state"] = "relaunching"
            started, err = relaunch(r["launch"])
            if started and not verify(kind):
                started, err = False, f"等了 {int(_envf('CC_SESSIONSYNC_LAUNCH_WAIT', LAUNCH_WAIT))} 秒没看到它起来"
            r.update(relaunched=started, relaunch_error=err)
            apps._log(f"关闭并执行 {r['id']}：重新打开 {apps.NAMES[kind]} " + ("成功" if started else f"失败：{err}"))
        r["state"] = "done"
        titles = "、".join(x["title"] for x in r["jobs"] if x.get("state") == "done") or "（没有做成的）"
        tail = "" if r["relaunched"] is None else f"；{APP[kind]} 已重新打开" if r["relaunched"] else f"；没能重新打开 {APP[kind]}，请手动打开"
        bad = [x for x in r["jobs"] if x.get("state") == "failed"]
        events.add("job", ("已完成：" if ok and not bad else "没全做成：") + titles[:80], (f"{len(bad)} 个失败" if bad else "") + tail.lstrip("；"),
                   {"page": "jobs"}, ok=ok and not bad and r["relaunched"] is not False)
    except Exception as e:
        r.update(state="failed", error=f"{type(e).__name__}: {e}")
        try:
            _set_jobs(r, "closing", "waiting")
            apps._spawn_watcher(kind)
            if r["was_running"] and r.get("remaining") == 0 and not app_procs(kind):     # 已经关掉了：至少把它打开回来
                started, err = relaunch(r["launch"])
                r.update(relaunched=started and verify(kind), relaunch_error=err)
            apps._log(f"关闭并执行 {r['id']} 出错：{r['error']}（改回排队）")
            events.add("job", f"关闭 {APP[kind]} 并执行没做成", r["error"][:200], {"page": "jobs"}, ok=False)
        except Exception:
            pass

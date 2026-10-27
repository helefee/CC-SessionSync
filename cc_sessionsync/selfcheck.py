# -*- coding: utf-8 -*-
"""自检：装得全不全、数据目录能不能写、凭据管理器能不能用、Claude / Codex 找不找得到、它们的数据目录读不读得了、
更新地址通不通。每项 pass / warn / fail，带建议。第一次启动自动跑一次（结果有失败项时发一条通知），设置 / 诊断里随时再跑。

只读检查为主；会写的只有两处，都是本程序自己的地方，用完就删：
- 数据目录 ~/.cc-sessionsync 里写一个临时文件；
- 凭据管理器里写一条 CCSessionSync/key/selftest-<进程号>，读回核对后删掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import __version__, paths

LAST = paths.HOME / "selfcheck.json"
REQUIRED = ("index.html", "icon.ico", "icon.png", "usage_prices.json", "fonts/inter-regular.woff2")


def _item(i: str, name: str, status: str, detail: str = "", fix: str = "") -> dict:
    return {"id": i, "name": name, "status": status, "detail": detail, "fix": fix}


def check_install() -> dict:
    base = paths.PKG
    miss = [f for f in REQUIRED if not (base / f).exists()]
    if miss:
        return _item("install", "程序文件完整", "fail", "缺：" + "、".join(miss), "重新安装（从 GitHub 发布页下载 CCSessionSync-Setup）")
    man = (paths.APP_DIR / "files.sha256") if paths.APP_DIR else None
    if man and man.exists():                     # 安装版：按打包时生成的清单逐个核对
        bad, n = [], 0
        for line in man.read_text(encoding="utf-8").splitlines():
            h, _, rel = line.partition("  ")
            if not rel:
                continue
            n += 1
            f = paths.APP_DIR / rel
            try:
                if hashlib.sha256(f.read_bytes()).hexdigest() != h:
                    bad.append(rel)
            except OSError:
                bad.append(rel)
        if bad:
            return _item("install", "程序文件完整", "fail", f"{len(bad)} 个文件和安装包里的不一样：" + "、".join(bad[:5]),
                         "重新安装同一版本；杀毒软件隔离过文件的话先恢复")
        return _item("install", "程序文件完整", "pass", f"安装版 {__version__}，{n} 个文件和清单一致")
    return _item("install", "程序文件完整", "pass", f"{'安装版' if paths.FROZEN else '源码 / pip 版'} {__version__}，必需文件都在")


def check_home() -> dict:
    try:
        paths.HOME.mkdir(parents=True, exist_ok=True)
        f = paths.HOME / f".selfcheck-{os.getpid()}.tmp"
        f.write_text("ok", encoding="utf-8")
        ok = f.read_text(encoding="utf-8") == "ok"
        f.unlink()
        if not ok:
            raise OSError("读回来不对")
        return _item("home", "数据目录可写", "pass", str(paths.HOME))
    except Exception as e:
        return _item("home", "数据目录可写", "fail", f"{paths.HOME}：{type(e).__name__}: {e}",
                     "检查这个目录的权限 / 磁盘空间；或设环境变量 CC_SESSIONSYNC_HOME 换一个目录")


def check_agents(r: dict | None = None) -> list[dict]:
    from . import detect, launch
    r = r or detect.report()
    out = []
    for a, exe in (("claude", launch.claude_exe), ("codex", launch.codex_exe)):
        x = r[a]
        try:
            e = exe()
        except Exception:
            e = None
        lab = detect.LABEL[a]
        if x["available"]:
            out.append(_item(f"{a}-found", f"找到 {lab}", "pass",
                             "、".join(detect.KIND_LABEL.get(k, k) for k in x.get("kinds") or []) or x.get("why") or "数据目录在",
                             ""))
        else:
            out.append(_item(f"{a}-found", f"找到 {lab}", "warn", x.get("why") or "没检测到",
                             "没装就不用管；装在别处的话到「诊断」里手动指定数据目录"))
        out.append(_item(f"{a}-cli", f"{lab} 命令行", "pass" if e else "warn", e or "找不到命令行版（「在终端里继续」用不了）",
                         "" if e else f"装命令行版，或设环境变量 {'CLAUDE' if a == 'claude' else 'CODEX'}_CLI_PATH"))
    return out


def check_dirs() -> list[dict]:
    from . import claude_sync
    out = []
    for i, name, p, need in (("claude-projects", "Claude Code 会话目录可读", paths.CLAUDE_HOME / "projects", False),
                             ("claude-desktop", "Claude 桌面端会话登记可读", claude_sync.sessions_root(), False),
                             ("codex-sessions", "Codex 会话目录可读", paths.CODEX_HOME / "sessions", False)):
        if not p.exists():
            out.append(_item(i, name, "warn", f"{p} 不在", "没用过这个工具就不用管；位置不对到「诊断」里手动指定"))
            continue
        try:
            n = sum(1 for _ in zip(range(2000), p.iterdir()))
            out.append(_item(i, name, "pass", f"{p}（{n}{'+' if n >= 2000 else ''} 项）"))
        except Exception as e:
            out.append(_item(i, name, "fail", f"{p}：{type(e).__name__}: {e}", "检查目录权限"))
    try:                                   # API 模式（3P）：数据目录、桌面端当前模式、用哪个目录
        i = claude_sync.api_info()
        m = claude_sync.desktop_mode()
        mode = "API 模式（3P）" if m["mode"] == "3p" else "订阅登录"
        if not i["base"]:
            out.append(_item("claude-api-mode", "Claude 桌面端 API 模式", "pass", f"桌面端：{mode}；没用过 API 模式（没有 Claude-3p 数据目录）"))
        else:
            b = i["bucket"]
            det = (f"桌面端：{mode}（{m['why']}）；数据目录 {i['base']}（{i['why']}）；供应商 {i['provider'] or '—'}；"
                   + (f"会话列表 {b['count']} 个（所有供应商共用）" if b else "还没有会话列表文件夹"))
            warn = m["mode"] == "3p" and not b
            out.append(_item("claude-api-mode", "Claude 桌面端 API 模式", "warn" if warn else "pass", det,
                             "在桌面端的 API 模式里开一个 Claude Code 会话后才能往里同步" if warn else ""))
    except Exception as e:
        out.append(_item("claude-api-mode", "Claude 桌面端 API 模式", "warn", f"{type(e).__name__}: {e}", ""))
    return out


def check_update(timeout: float = 8) -> dict:
    from . import update
    try:
        with update._get(update.API, timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        return _item("update", "更新地址可达", "pass", f"GitHub 发布：最新 {j.get('tag_name') or '—'}")
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 404:
            return _item("update", "更新地址可达", "warn", "连上了，但还没有发布", "")
        return _item("update", "更新地址可达", "warn", f"连不上 GitHub（{type(e).__name__}{': ' + str(code) if code else ''}）",
                     "断网 / 代理时检查不了更新，其它功能不受影响")


def check_webview() -> dict | None:
    if os.name != "nt":
        return None
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")]
    for root, k in keys:
        try:
            with winreg.OpenKey(root, k) as h:
                v, _ = winreg.QueryValueEx(h, "pv")
                if v and v != "0.0.0.0":
                    return _item("webview2", "WebView2 运行库", "pass", f"版本 {v}")
        except OSError:
            continue
    return _item("webview2", "WebView2 运行库", "warn", "没找到（窗口可能打不开）",
                 "Win10 / 11 一般自带；没有的话装微软的 Evergreen WebView2 Runtime")


def check_autostart() -> dict | None:
    if os.name != "nt":
        return None
    from . import tray
    if not tray.autostart_get():
        return _item("autostart", "开机自启", "pass", "没开")
    cmd = tray.autostart_value() or ""
    exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
    if exe and Path(exe).exists():
        return _item("autostart", "开机自启", "pass", "开着，启动命令有效")
    return _item("autostart", "开机自启", "warn", f"开着，但指向的程序不在：{exe}", "在设置里把「开机自启」关掉再打开")


def check_legacy() -> list[dict]:
    """0.3.5 拿掉了供应商（模型路由）：确认配置里没有残留指向 cc-route / 本机代理。没用过就不出现。"""
    from . import legacy
    r = legacy.state() or {}
    left = []
    try:
        from . import _toml
        f = paths.CODEX_HOME / "config.toml"
        cfg = _toml.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}
        if cfg.get("model_provider") == legacy.PROFILE or legacy.PROFILE in (cfg.get("model_providers") or {}):
            left.append("Codex config.toml 还有 cc-route")
        if legacy._is_ours_catalog(cfg.get("model_catalog_json")):
            left.append("Codex model_catalog_json 还指着 ccss-models-*.json")
    except Exception:
        pass
    if left and r.get("codex_pending"):
        return [_item("legacy", "供应商功能残留", "warn", "Codex 桌面端开着，恢复 Codex 配置排在队里：" + "；".join(left), "完全退出 Codex 桌面端（托盘里也退），或在顶上点「关闭 Codex 并执行」")]
    if left:
        return [_item("legacy", "供应商功能残留", "fail", "；".join(left), "重启 CC Session Sync 会再收拾一遍；还在就手动删掉这几项")]
    if r.get("errors"):
        return [_item("legacy", "供应商功能残留", "warn", "；".join(r["errors"])[:400], "重启 CC Session Sync 会再试一遍")]
    if r.get("done"):
        return [_item("legacy", "供应商功能已移除", "pass", "已把 Codex / Claude Code 配置恢复原样（" + str(len(r["done"])) + " 项）")]
    return []


def run(net: bool = True) -> dict:
    t0 = time.time()
    items = [check_install(), check_home()]
    for f in (check_webview, check_autostart):
        try:
            x = f()
        except Exception as e:
            x = _item(f.__name__, f.__name__, "warn", f"{type(e).__name__}: {e}")
        if x:
            items.append(x)
    try:
        items += check_agents()
    except Exception as e:
        items.append(_item("agents", "找 Claude / Codex", "warn", f"{type(e).__name__}: {e}"))
    items += check_dirs()
    try:
        items += check_legacy()
    except Exception as e:
        items.append(_item("legacy", "供应商功能残留", "warn", f"{type(e).__name__}: {e}"))
    if net:
        items.append(check_update())
    n = {s: sum(1 for x in items if x["status"] == s) for s in ("pass", "warn", "fail")}
    r = {"ok": n["fail"] == 0, "time": time.time(), "ms": int((time.time() - t0) * 1000), "counts": n, "items": items,
         "version": __version__}
    try:
        LAST.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return r


def last() -> dict | None:
    try:
        return json.loads(LAST.read_text(encoding="utf-8"))
    except Exception:
        return None


def text(r: dict) -> str:
    mark = {"pass": "✓", "warn": "!", "fail": "✗"}
    lines = [f"CC Session Sync {r.get('version')} 自检：通过 {r['counts']['pass']}，提醒 {r['counts']['warn']}，失败 {r['counts']['fail']}"]
    for x in r["items"]:
        lines.append(f"[{mark[x['status']]}] {x['name']}：{x['detail']}" + (f"\n    建议：{x['fix']}" if x["fix"] and x["status"] != "pass" else ""))
    return "\n".join(lines)

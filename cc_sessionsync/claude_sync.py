# -*- coding: utf-8 -*-
"""Claude 桌面端会话列表跨账号同步。

桌面端的会话列表按「账号 / 组织」分文件夹记：<Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_*.json，
只显示当前登录的那个文件夹；对话内容本身在 ~/.claude/projects/**/<会话号>.jsonl，本地、不分账号。
切换账号后旧会话「不见了」只是列表文件在别的文件夹里。这里把别的文件夹里的会话列表文件补齐复制到目标文件夹：

- 只补缺的（同名文件已有就跳过），不覆盖、不删除；
- 对话记录文件已经不在的会话不复制（点开也续不上）；
- 写之前把目标文件夹整个备份到 <数据目录>/claude-sync-bak/<时间>/（数据目录见 paths.py）。

注意：桌面端和命令行（Claude Code CLI）是两套登录，可以登的不是同一个号：
- 桌面端：从它自己的 Local Storage（<Claude 数据目录>/Local Storage/leveldb）里找最近一条带账号 / 组织的记录
  （表文件里 snappy 压过的块先解开再找；每次都重新看文件，切号后刷新就能认出来）；
- 命令行：~/.claude.json 的 oauthAccount。
桌面端的邮箱 / 名字从它的 IndexedDB（claude.ai 页面缓存的账号信息）里读，读不到就借命令行的（同一个号时）或以前记下的（推测）。
所以有两个「当前账号」，source='desktop' / 'cli' 分别对应，缺省按桌面端算（会话列表本来就是给桌面端看的）。
老实说：命令行自己（claude --resume）直接读 ~/.claude/projects，根本不看这里按账号分的登记；
「同步到 CLI 账号」只是把会话补进 CLI 那个号的文件夹，只有桌面端登的也是这个号时才看得到效果。

目标缺省是对应来源的当前账号 / 组织；都认不出来就退回「最近有会话在写的那个文件夹」。桌面端要重启才会看到新补进来的会话。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from . import paths


def _real_app_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Claude"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude"


# ---------- 桌面端数据目录：%APPDATA%\Claude 还是商店版（MSIX）的虚拟化目录 ----------
# 商店版桌面端看到的是「合并视图」：%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude 里有的文件 / 文件夹盖过 %APPDATA%\Claude 里的。
# 哪些写进了虚拟化目录因机器而异（2026-10-17 实测：会话列表和 Local Storage 还在 %APPDATA%，config.json 等在 LocalCache），
# 所以按子目录分别判断：只有一边有 → 用那边；两边都有 → 用最近写过的（打平时桌面端是商店版就用虚拟化目录）。设置里可手动指定。
APP_PARTS = {"sessions": "claude-code-sessions", "ldb": "Local Storage/leveldb", "idb": "IndexedDB"}
_APP_RES: dict = {"t": 0.0, "v": None}


def _activity(p: Path, part: str) -> float:
    try:
        if part == "sessions":
            return max((f.stat().st_mtime for f in p.glob("*/*/*.json")), default=0.0) or p.stat().st_mtime
        if part == "ldb":
            return max((f.stat().st_mtime for f in p.iterdir() if f.is_file()), default=0.0)
        return max((f.stat().st_mtime for f in p.glob("*/*") if f.is_file()), default=0.0) or p.stat().st_mtime
    except OSError:
        return 0.0


def _desktop_msix() -> bool | None:
    """桌面端在跑、而且是从 WindowsApps（商店版）起的？没在跑 → None。"""
    try:
        from . import detect
        ps = [p for p in detect.processes() if detect.classify(p.get("name", ""), p.get("exe", ""), p.get("cmd", "")) == [("claude", "desktop")]]
    except Exception:
        return None
    if not ps:
        return None
    return any("/windowsapps/claude_" in (p.get("exe") or "").replace("\\", "/").lower() for p in ps)


def _env_dir() -> Path | None:
    """CLAUDE_USER_DATA_DIR：设了就把它也当一个候选（两种模式共用这一个目录；注意签名版桌面端会忽略这个变量）。"""
    v = os.environ.get("CLAUDE_USER_DATA_DIR") or ""
    return Path(v) if v and Path(v).is_dir() else None


def app_dir_candidates(profile: str = "1p") -> list[dict]:
    """profile='1p'：订阅登录用的数据目录；'3p'：API 模式（第三方供应商 / API key）用的另一套数据目录 Claude-3p。"""
    if profile == "3p":
        return api_dir_candidates()
    from . import detect
    out = [{"id": "appdata", "label": "%APPDATA%\\Claude", "path": _real_app_dir()}]
    if sys.platform == "win32" or os.environ.get("LOCALAPPDATA"):
        for d in detect.msix_claude_dirs():
            out.append({"id": "msix:" + d.parent.parent.parent.name, "label": "商店版虚拟化目录（" + d.parent.parent.parent.name + "）", "path": d})
    e = _env_dir()
    if e:
        out.append({"id": "env", "label": "CLAUDE_USER_DATA_DIR（签名版桌面端会忽略）", "path": e})
    return out


# ---------- API 模式（3P：API key / 第三方供应商）----------
# 桌面端切到 API 模式后用另一套数据目录（代码里叫 3p）：Windows 上是 %LOCALAPPDATA%\Claude-3p（老版本在 %APPDATA%\Claude-3p，
# 会被桌面端挪过去；商店版可能写进 Packages\Claude_*\LocalCache\{Local,Roaming}\Claude-3p）。会话列表一样是
# claude-code-sessions\<账号>\<组织>\local_*.json，只是「账号」是这台机器的设备号（数据目录里 ant-did 文件，base64 的 UUID），
# 「组织」没配部署组织号时是固定的占位号 00000000-0000-4000-8000-000000000001。换供应商（gateway / Bedrock / Vertex …）
# 设备号和组织号都不变：所有供应商共用这一个列表。桌面端当前用哪种模式记在 Claude-3p\claude_desktop_config.json 的 deploymentMode。
# 这里只读：不建 ant-did、不建文件夹；供应商只读 configLibrary 里当前那条的 inferenceProvider（不碰 key / 地址）。
PLACEHOLDER_ORG = "00000000-0000-4000-8000-000000000001"
API_PREFIX = "3p:"
_UUID_S = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_API_RES: dict = {"t": 0.0, "v": None}


def api_dir_candidates() -> list[dict]:
    out = []
    e = _env_dir()
    if e:
        out.append({"id": "env", "label": "CLAUDE_USER_DATA_DIR（签名版桌面端会忽略）", "path": e})
    la, ad = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if sys.platform == "win32" or la or ad:
        la = Path(la or Path.home() / "AppData" / "Local")
        ad = Path(ad or Path.home() / "AppData" / "Roaming")
        out.append({"id": "local3p", "label": "%LOCALAPPDATA%\\Claude-3p", "path": la / "Claude-3p"})
        out.append({"id": "appdata3p", "label": "%APPDATA%\\Claude-3p（旧位置）", "path": ad / "Claude-3p"})
        try:
            pks = sorted((la / "Packages").glob("Claude_*")) + sorted((la / "Packages").glob("AnthropicPBC.Claude_*"))
        except OSError:
            pks = []
        for pk in pks:
            for sub in ("Local", "Roaming"):
                out.append({"id": f"msix3p:{pk.name}:{sub}", "label": f"商店版虚拟化目录（{pk.name}\\{sub}）",
                            "path": pk / "LocalCache" / sub / "Claude-3p"})
    else:
        b = _real_app_dir()
        out.append({"id": "local3p", "label": str(b) + "-3p", "path": b.with_name(b.name + "-3p")})
    return out


def device_id(base: Path) -> str | None:
    """数据目录里 ant-did 记的设备号（base64 的 UUID）；没有 / 读不了 → None。只读，不建。"""
    import base64
    try:
        t = base64.b64decode((Path(base) / "ant-did").read_text(encoding="utf-8").strip()).decode("utf-8").strip()
    except Exception:
        return None
    return t.lower() if _UUID_S.fullmatch(t) else None


def _api_provider(base: Path) -> str | None:
    """configLibrary 里当前生效那条的 inferenceProvider（gateway / bedrock / vertex …）。只取这一个字段。"""
    try:
        meta = json.loads((Path(base) / "configLibrary" / "_meta.json").read_text(encoding="utf-8"))
        aid = str(meta.get("appliedId") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", aid):
            return None
        v = json.loads((Path(base) / "configLibrary" / f"{aid}.json").read_text(encoding="utf-8")).get("inferenceProvider")
        return str(v)[:40] if isinstance(v, str) and v else None
    except Exception:
        return None


def _deployment_mode(cands: list[dict]) -> tuple[str | None, str | None]:
    """('3p' / '1p' / None, 从哪个文件读的)。桌面端读的是 Claude-3p 数据目录里的 claude_desktop_config.json。"""
    best = None
    for c in cands:
        f = Path(c["path"]) / "claude_desktop_config.json"
        try:
            v = json.loads(f.read_text(encoding="utf-8")).get("deploymentMode")
            mt = f.stat().st_mtime
        except Exception:
            continue
        if v in ("1p", "3p") and (best is None or mt > best[2]):
            best = (v, str(f), mt)
    return (best[0], best[1]) if best else (None, None)


def _bucket_latest(d: Path) -> float:
    try:
        return max((f.stat().st_mtime for f in d.glob("local_*.json")), default=0.0)
    except OSError:
        return 0.0


def api_info(max_age: float = 20) -> dict:
    """API 模式的数据目录 / 设备号 / 会话列表文件夹（bucket）/ 桌面端当前模式 / 供应商类型。20 秒缓存（forget() 清）。"""
    now = time.time()
    if _API_RES["v"] is not None and now - _API_RES["t"] < max_age:
        return _API_RES["v"]
    cands = api_dir_candidates()
    rows = []
    for c in cands:
        b = Path(c["path"])
        ses = b / "claude-code-sessions"
        ex = b.is_dir()
        dev = device_id(b) if ex else None
        buckets = []
        if ses.is_dir():
            try:
                for a in ses.iterdir():
                    if not a.is_dir():
                        continue
                    for o in a.iterdir():
                        if o.is_dir():
                            buckets.append({"key": API_PREFIX + f"{a.name}/{o.name}", "acct": a.name, "org": o.name, "path": str(o),
                                            "count": len(list(o.glob("local_*.json"))), "latest": _bucket_latest(o),
                                            "device_match": bool(dev and a.name.lower() == dev), "placeholder": o.name == PLACEHOLDER_ORG})
            except OSError:
                pass
        # 和 1p 共用一个目录（CLAUDE_USER_DATA_DIR）时，只认设备号 / 占位组织那几个文件夹
        if c["id"] == "env":
            buckets = [x for x in buckets if x["device_match"] or x["placeholder"]]
        rows.append({"id": c["id"], "label": c["label"], "base": str(b), "exists": ex, "sessions": str(ses), "has_sessions": ses.is_dir(),
                     "device": dev, "buckets": buckets, "activity": max((x["latest"] for x in buckets), default=0.0)})
    have = [r for r in rows if r["buckets"]]
    pick = max(have, key=lambda r: r["activity"]) if have else next((r for r in rows if r["exists"] and r["device"]), None)
    mode, mode_file = _deployment_mode(cands)
    out: dict = {"cands": rows, "mode": mode, "mode_file": mode_file, "base": None, "device": None, "buckets": [], "bucket": None,
                 "provider": None, "why": None}
    if pick:
        bs = sorted(pick["buckets"], key=lambda x: (not x["device_match"], -x["latest"]))
        out.update(base=pick["base"], device=pick["device"], buckets=bs, provider=_api_provider(Path(pick["base"])),
                   why=("只有这边有" if len(have) <= 1 else "这边最近写过") if pick["buckets"] else "还没有会话列表文件夹")
        if bs:
            out["bucket"] = bs[0]
    _API_RES.update(t=now, v=out)
    return out


def api_folders() -> list[dict]:
    """API 模式的会话列表文件夹（和 folders() 同样的字段，外加 profile='3p'）。"""
    return [{"key": b["key"], "path": b["path"], "count": b["count"], "latest": b["latest"], "profile": "3p"} for b in api_info()["buckets"]]


def api_account() -> dict:
    i = api_info()
    b = i.get("bucket")
    return {"accountUuid": b["acct"] if b else i.get("device"), "organizationUuid": b["org"] if b else None,
            "key": b["key"] if b else None, "exists": bool(b), "count": b["count"] if b else 0, "path": b["path"] if b else None,
            "provider": i.get("provider"), "mode": i.get("mode"), "base": i.get("base"), "profile": "3p",
            "via": "api", "email": None, "name": None, "label": "API 模式（所有供应商共用）"}


def desktop_mode() -> dict:
    """桌面端现在用哪种模式：{'mode': '1p'|'3p', 'why': …}。按 deploymentMode；没记就看哪边的会话列表最近写过。"""
    i = api_info()
    if i["mode"] in ("1p", "3p"):
        return {"mode": i["mode"], "why": "桌面端记的 deploymentMode"}
    t3 = max((b["latest"] for b in i["buckets"]), default=0.0)
    t1 = max((f["latest"] for f in folders()), default=0.0)
    if t3 > t1 + 60:
        return {"mode": "3p", "why": "没记 deploymentMode，API 模式的会话列表最近写过"}
    return {"mode": "1p", "why": "没记 deploymentMode，按订阅登录算" if not t3 else "没记 deploymentMode，订阅登录的会话列表最近写过"}


def default_source() -> str:
    """同步的缺省目标：桌面端在 API 模式 → 'api'，否则 'desktop'。"""
    try:
        return "api" if desktop_mode()["mode"] == "3p" else "desktop"
    except Exception:
        return "desktop"


def api_session_ids() -> set[str]:
    """在 API 模式会话列表里登记过的命令行会话号（含 priorCliSessionIds）：用量里这些会话按 API 模式的供应商算，不算订阅。"""
    out: set[str] = set()
    for b in api_info()["buckets"]:
        for f in Path(b["path"]).glob("local_*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for x in [d.get("cliSessionId")] + list(d.get("priorCliSessionIds") or []):
                if isinstance(x, str) and x:
                    out.add(x)
    return out


def _app_override() -> Path | None:
    try:
        v = json.loads((paths.HOME / "settings.json").read_text(encoding="utf-8")).get("claude_app_dir") or ""
    except Exception:
        return None
    return Path(v) if v and Path(v).is_dir() else None


def app_resolve(max_age: float = 20) -> dict:
    """{part: {path, base, why, cands: [{label, path, exists, activity}]}, override, msix_running}，20 秒内用缓存（forget() 清掉）。"""
    now = time.time()
    if _APP_RES["v"] is not None and now - _APP_RES["t"] < max_age:
        return _APP_RES["v"]
    ov = _app_override()
    cands = app_dir_candidates()
    msix = _desktop_msix() if len(cands) > 1 else None
    res: dict = {"override": str(ov) if ov else None, "msix_running": msix, "parts": {}}
    for part, rel in APP_PARTS.items():
        rows = []
        for c in cands:
            p = c["path"] / rel
            ex = p.exists()
            rows.append({"id": c["id"], "label": c["label"], "base": str(c["path"]), "path": str(p), "exists": ex,
                         "activity": _activity(p, part) if ex else 0.0})
        if ov:
            pick, why = {"base": str(ov), "path": str(ov / rel)}, "设置里手动指定的"
        else:
            have = [r for r in rows if r["exists"]]
            if not have:
                pick, why = rows[0], "两边都还没有，用缺省位置"
            elif len(have) == 1:
                pick, why = have[0], "只有这边有"
            else:
                best = max(have, key=lambda r: r["activity"])
                tie = [r for r in have if best["activity"] - r["activity"] < 2]
                if len(tie) > 1:
                    pick = next((r for r in tie if r["id"].startswith("msix:")), tie[0]) if msix else next((r for r in tie if r["id"] == "appdata"), tie[0])
                    why = "两边一样新，" + ("桌面端是商店版，用虚拟化目录" if msix else "用 %APPDATA%")
                else:
                    pick, why = best, "两边都有，这边最近写过"
        res["parts"][part] = {"path": pick["path"], "base": pick["base"], "why": why, "cands": rows}
    _APP_RES.update(t=now, v=res)
    return res


def app_path(part: str) -> Path:
    try:
        return Path(app_resolve()["parts"][part]["path"])
    except Exception:
        return _real_app_dir() / APP_PARTS[part]


def app_dirs_brief() -> dict:
    """给页面 / 诊断：每个子目录用的哪边、为什么，和可选的目录。"""
    r = app_resolve()
    names = {"sessions": "会话列表", "ldb": "Local Storage（账号）", "idb": "IndexedDB（头像 / 名字）"}
    return {"override": r["override"], "msix_running": r["msix_running"],
            "choices": [{"id": c["id"], "label": c["label"], "path": str(c["path"]), "exists": Path(c["path"]).exists()} for c in app_dir_candidates()],
            "parts": [{"part": k, "name": names[k], "path": v["path"], "base": v["base"], "why": v["why"],
                       "cands": [{"label": c["label"], "exists": c["exists"], "activity": c["activity"] or None} for c in v["cands"]]}
                      for k, v in r["parts"].items()]}


def claude_app_dir() -> Path:
    """桌面端数据目录（以 Local Storage 所在的那边为准；会话列表 / IndexedDB 各自用 app_path() 单独判断）。"""
    try:
        r = app_resolve()
        if r["override"]:
            return Path(r["override"])
        b = Path(r["parts"]["ldb"]["base"])
        return b if b.exists() else next((Path(c["path"]) for c in app_dir_candidates() if Path(c["path"]).exists()), _real_app_dir())
    except Exception:
        return _real_app_dir()


def sessions_root() -> Path:
    return app_path("sessions")


def folders() -> list[dict]:
    """所有「账号 / 组织」文件夹：会话数、最近一次写入时间。"""
    root = sessions_root()
    out = []
    if not root.exists():
        return out
    dev = device_id(root.parent)                 # 和 API 模式共用一个目录时（CLAUDE_USER_DATA_DIR），设备号那个文件夹归 API 模式
    for acct in root.iterdir():
        if not acct.is_dir() or (dev and acct.name.lower() == dev):
            continue
        for org in acct.iterdir():
            if not org.is_dir():
                continue
            files = list(org.glob("local_*.json"))
            latest = max((f.stat().st_mtime for f in files), default=0)
            out.append({"key": f"{acct.name}/{org.name}", "path": str(org), "count": len(files), "latest": latest, "profile": "1p"})
    return sorted(out, key=lambda x: -x["latest"])


def all_folders() -> list[dict]:
    """订阅登录的「账号 / 组织」文件夹 + API 模式的文件夹（key 带 3p: 前缀）。"""
    return sorted(folders() + api_folders(), key=lambda x: -x["latest"])


def _short(a: str | None) -> str:
    return (a or "")[:8]


def cli_account() -> dict:
    """命令行（Claude Code CLI）登录的号：~/.claude.json 的 oauthAccount。"""
    try:
        o = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("oauthAccount") or {}
    except Exception:
        o = {}
    a, g = o.get("accountUuid"), o.get("organizationUuid")
    name = o.get("displayName") or o.get("fullName")
    _remember(a, o.get("emailAddress"), name, "cli")      # 记下来：以后桌面端登这个号、又认不出邮箱时拿来推测
    return {"accountUuid": a, "organizationUuid": g, "email": o.get("emailAddress"), "name": name,
            "org_name": o.get("organizationName"),
            "key": f"{a}/{g}" if a and g else None, "via": "claude.json" if a and g else None}


_UUID = rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ACC_RE = re.compile(rb'"(?:accountUuid|account_uuid)"\s*:\s*"(' + _UUID + rb')"')
_ORG_RE = re.compile(rb'"(?:organizationUuid|organization_uuid)"\s*:\s*"(' + _UUID + rb')"')
# eventTimestamp 在压缩块里常被截成「…Timestamp」，只认后半截
_TS_RE = re.compile(rb'Timestamp"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"')
_CUT_RE = re.compile(rb'"eventName"|"(?:accountUuid|account_uuid)"')


# ---------- 读 Chromium 的 LevelDB（只读，不加锁） ----------
# *.log 是没压缩的写入日志，直接扫字节；*.ldb 是整理过的表文件，数据块大多用 snappy 压过，
# 压过的块里「accountUuid」这类字段名会被拆碎，硬扫字节就认不出来（桌面端整理一次 Local Storage 后就认不出当前账号了），
# 所以按表格式把每个数据块解开再扫。值可能是 UTF-16 存的，统一去掉 \x00 再按 ASCII 找。
# 按（文件、修改时间、大小）缓存，文件一变就重新读。

_LDB_CACHE: dict = {}


def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7


def _unsnappy(b: bytes) -> bytes:
    n, i = _varint(b, 0)
    out = bytearray()
    L = len(b)
    while i < L:
        tag = b[i]
        i += 1
        t = tag & 3
        if t == 0:                                  # 原样的一段
            ln = tag >> 2
            if ln >= 60:
                k = ln - 59
                ln = int.from_bytes(b[i:i + k], "little")
                i += k
            ln += 1
            out += b[i:i + ln]
            i += ln
            continue
        if t == 1:
            ln = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | b[i]
            i += 1
        elif t == 2:
            ln = (tag >> 2) + 1
            off = int.from_bytes(b[i:i + 2], "little")
            i += 2
        else:
            ln = (tag >> 2) + 1
            off = int.from_bytes(b[i:i + 4], "little")
            i += 4
        if off <= 0 or off > len(out):
            raise ValueError("bad snappy offset")
        st = len(out) - off
        if off >= ln:
            out += out[st:st + ln]
        else:                                       # 重叠复制
            for k in range(ln):
                out.append(out[st + k])
    if len(out) != n:
        raise ValueError("bad snappy length")
    return bytes(out)


def _ldb_blocks(b: bytes) -> list[bytes]:
    """LevelDB 表文件 → 解开的各个数据块（解不开的块跳过）。"""
    if len(b) < 48 or b[-8:] != bytes.fromhex("57fb808b247547db"):
        return [b]
    foot = b[-48:]
    _, i = _varint(foot, 0)
    _, i = _varint(foot, i)                         # metaindex
    ioff, i = _varint(foot, i)
    isz, i = _varint(foot, i)

    def block(off: int, sz: int) -> bytes:
        raw, typ = b[off:off + sz], b[off + sz]
        return _unsnappy(raw) if typ == 1 else raw

    idx = block(ioff, isz)
    nres = int.from_bytes(idx[-4:], "little")
    end = len(idx) - 4 - 4 * nres
    out, i = [], 0
    while i < end:
        _, i = _varint(idx, i)                      # shared
        nk, i = _varint(idx, i)
        nv, i = _varint(idx, i)
        i += nk
        v = idx[i:i + nv]
        i += nv
        off, j = _varint(v, 0)
        sz, _ = _varint(v, j)
        try:
            out.append(block(off, sz))
        except Exception:
            continue
    return out


def _ldb_text(f: Path) -> list[bytes]:
    """一个 .log / .ldb 文件里能扫的字节（去掉 \x00）；读不了返回 []。"""
    try:
        st = f.stat()
    except Exception:
        return []
    # Windows 上别的进程开着写的文件（桌面端的 .log），目录里记的修改时间 / 大小会迟迟不更新，
    # 只看 stat 会一直用旧缓存 → 换号后还认成旧号。所以按真正打开后的文件长度来判断有没有变。
    size = st.st_size
    if f.suffix == ".log":
        try:
            with open(f, "rb") as fh:
                size = fh.seek(0, 2)
        except Exception:
            pass
    key = (str(f), st.st_mtime_ns, size)
    hit = _LDB_CACHE.get(str(f))
    if hit and hit[0] == key:
        return hit[1]
    try:
        b = f.read_bytes()
    except Exception:              # 桌面端开着时有的文件锁着
        return []
    try:
        parts = _ldb_blocks(b) if f.suffix == ".ldb" else [b]
    except Exception:
        parts = [b]
    parts = [p.replace(b"\x00", b"") for p in parts]
    _LDB_CACHE[str(f)] = (key, parts)
    return parts


def forget() -> None:
    """丢掉所有读缓存：下一次一定重新读桌面端 Local Storage（刷新按钮 / 打开同步弹窗时用）。"""
    _LDB_CACHE.clear()
    _APP_RES["v"] = None
    _API_RES["v"] = None


def _leveldb_pairs() -> list[tuple[str, float, str, str]]:
    """桌面端 Local Storage 里所有（时间戳, 文件修改时间, 账号, 组织）。"""
    d = app_path("ldb")
    out = []
    try:
        files = [f for f in d.iterdir() if f.suffix in (".log", ".ldb")]
    except Exception:
        return out
    for f in files:
        try:
            mt = f.stat().st_mtime
        except Exception:
            continue
        for b in _ldb_text(f):
            for m in _ACC_RE.finditer(b):
                # 组织号在账号号前后 300 字节内找，碰到下一条事件 / 下一个账号字段就停，免得串到别的事件上
                after = b[m.end():m.end() + 300]
                c = _CUT_RE.search(after)
                after = after[:c.start()] if c else after
                before = b[max(0, m.start() - 300):m.start()]
                cuts = list(_CUT_RE.finditer(before))
                if cuts and cuts[-1].group(0) != b'"eventName"':
                    before = before[cuts[-1].end():]
                og = _ORG_RE.search(after) or (list(_ORG_RE.finditer(before)) or [None])[-1]
                if not og:
                    continue
                # 时间戳：同一条事件里，在账号字段前面（eventName 之后）
                ev = before
                k = ev.rfind(b'"eventName"')
                ev = ev[k:] if k >= 0 else ev
                ts = (list(_TS_RE.finditer(ev)) or [None])[-1]
                out.append((ts.group(1).decode() if ts else "", mt, m.group(1).decode(), og.group(1).decode()))
    return out


def desktop_account(profile: bool = True) -> dict:
    """桌面端登录的号：Local Storage 里时间戳最新的那条账号 / 组织；都没时间戳就取最近改过的文件里的。
    认不出来 key 为 None（调用方自己退回）。profile=True 时再补邮箱 / 名字（见 _profile）。"""
    ps = _leveldb_pairs()
    if not ps:
        return {"accountUuid": None, "organizationUuid": None, "email": None, "name": None, "key": None, "via": None}
    with_ts = [p for p in ps if p[0]]
    ts, _, a, g = max(with_ts) if with_ts else max(ps, key=lambda p: p[1])
    d = {"accountUuid": a, "organizationUuid": g, "email": None, "name": None,
         "key": f"{a}/{g}", "via": "leveldb", "seen": ts or None}
    d = _last_known(d)
    return dict(d, **_profile(d["accountUuid"])) if profile else d


# Local Storage 里的这些记录是桌面端的遥测事件：发出去以后会被删掉，leveldb 压缩时也会搬到别的文件里。
# 当前账号的事件暂时都没了（发完了 / 桌面端没开 / 正在压缩）时，「时间戳最新的那条」会落到很早以前一直没删掉的
# 旧文件（比如几个月前别的账号留下的 000005.ldb）上 → 看起来像换了号，等新事件写进来又「换回来」。
# 所以记下认出来的账号和它最新的时间戳：新认出的号只有在它的时间戳比记下的更新时才算换号，否则还是记下的那个。
def _last_file() -> Path:
    return paths.HOME / "desktop-account.json"


def _last_known(d: dict) -> dict:
    f = _last_file()
    try:
        k = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        k = None
    seen = d.get("seen") or ""
    try:
        here = str(app_path("ldb") or "")
    except Exception:
        here = ""
    if k and (k.get("dir") or "") != here:
        k = None                              # 换了数据目录（MSIX / 普通安装 / 别的位置）：上次记的不算数

    def t(x):
        return _iso(x) or 0.0
    if k and k.get("key") and k["key"] != d["key"] and seen and t(k.get("seen")) >= t(seen):
        return dict(d, accountUuid=k["accountUuid"], organizationUuid=k["organizationUuid"], key=k["key"],
                    seen=k.get("seen"), via="leveldb-last-known", stale={"key": d["key"], "seen": seen})
    if seen and (not k or k.get("key") != d["key"] or t(k.get("seen")) < t(seen)):
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps({"key": d["key"], "accountUuid": d["accountUuid"], "organizationUuid": d["organizationUuid"],
                                       "seen": seen, "dir": here, "at": time.time()}), encoding="utf-8")
            os.replace(tmp, f)
        except Exception:
            pass
    return d


# ---------- 账号的邮箱 / 名字 ----------
# 桌面端里 claude.ai 页面把账号信息（react-query 缓存）存在 IndexedDB：
#   <Claude 数据目录>/IndexedDB/https_claude.ai_0.indexeddb.blob/** 和 …indexeddb.leveldb/*.log|*.ldb
# 是 V8 序列化的对象：…"uuid" <账号号> "email_address" <邮箱> "full_name" <名字> "display_name" <名字>…
# 字段名后面紧跟类型字节（'"' 单字节串 / 'c' 双字节 UTF-16 / 'S' UTF-8）和长度，按这个读；
# 被 snappy 压过、对不上的就跳过。只认紧跟在账号号后面的这几个字段，不碰 cookie / 令牌。
# 认出来的（还有命令行登过的号）记进 <数据目录>/claude-accounts.json，以后认不出来时拿来推测。

_EMAIL_OK = re.compile(r"[^@\s\x00-\x1f]+@[^@\s\x00-\x1f]+\.[A-Za-z]{2,}")
NAMES_CACHE = paths.HOME / "claude-accounts.json"


def _v8_str(b: bytes, i: int) -> str | None:
    """b[i] 是类型字节，后面 varint 长度 + 内容。"""
    if i >= len(b):
        return None
    tag, i = b[i:i + 1], i + 1
    n = shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            break
    if not 0 < n <= 200:
        return None
    raw = b[i:i + n]
    try:
        t = {b'"': lambda r: r.decode("latin-1"), b"c": lambda r: r.decode("utf-16-le"),
             b"S": lambda r: r.decode("utf-8")}[tag](raw)
    except Exception:
        return None
    return t if t.isprintable() and len(raw) == n else None


def _field(b: bytes, start: int, name: bytes, span: int = 400) -> str | None:
    k = b.find(name, start, start + span)
    return _v8_str(b, k + len(name)) if k >= 0 else None


def _idb_profile(acct: str) -> dict | None:
    d = app_path("idb")
    files = []
    for sub in ("https_claude.ai_0.indexeddb.blob", "https_claude.ai_0.indexeddb.leveldb"):
        try:
            files += [f for f in (d / sub).rglob("*") if f.is_file() and (sub.endswith(".blob") or f.suffix in (".log", ".ldb"))]
        except Exception:
            pass
    needle = b'uuid"$' + acct.encode()
    best = None
    for f in files:
        try:
            if f.stat().st_size > 50_000_000:
                continue
            mt = f.stat().st_mtime
            # 表文件按块解开（V8 序列化里本来就有 \x00，不能去掉，所以这里自己解）；读不了就算了
            bs = [f.read_bytes()]
            if f.suffix == ".ldb":
                bs = _ldb_blocks(bs[0])
        except Exception:          # 桌面端开着时有的文件锁着，读不了就算了
            continue
        for b in bs:
            i = b.find(needle)
            while i >= 0:
                j = i + len(needle)
                email = _field(b, j, b"email_address", 40)
                if email and _EMAIL_OK.fullmatch(email):
                    name = _field(b, j, b"full_name") or _field(b, j, b"display_name")
                    if not best or mt > best[0]:
                        best = (mt, email, name)
                i = b.find(needle, j)
    return {"email": best[1], "name": best[2]} if best else None


def _load_names() -> dict:
    try:
        return json.loads(NAMES_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember(acct: str, email: str | None, name: str | None, src: str) -> None:
    if not acct or not email:
        return
    c = _load_names()
    old = c.get(acct) or {}
    if old.get("email") == email and old.get("name") == (name or old.get("name")):
        return
    c[acct] = {"email": email, "name": name or old.get("name"), "from": src,
               "seen": datetime.now().isoformat(timespec="seconds")}
    try:
        NAMES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NAMES_CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _profile(acct: str | None) -> dict:
    """账号号 → {email, name, email_from, email_guess}。
    先看桌面端 IndexedDB（就是它现在登的号，可靠）；再看命令行是不是同一个号；都没有就查以前记下的（推测）。"""
    if not acct:
        return {"email": None, "name": None, "email_from": None, "email_guess": False}
    p = _idb_profile(acct)
    if p:
        _remember(acct, p["email"], p["name"], "desktop")
        return dict(p, email_from="desktop", email_guess=False)
    c = cli_account()
    if c["accountUuid"] == acct and c["email"]:
        return {"email": c["email"], "name": c.get("name"), "email_from": "cli", "email_guess": False}
    m = _load_names().get(acct)
    if m and m.get("email"):
        return {"email": m["email"], "name": m.get("name"), "email_from": "cache", "email_guess": True}
    return {"email": None, "name": None, "email_from": None, "email_guess": False}


def _with_folder(acc: dict) -> dict:
    k = acc.get("key")
    f = next((x for x in folders() if x["key"] == k), None) if k else None
    return dict(acc, exists=bool(f), count=f["count"] if f else 0, path=f["path"] if f else None)


def accounts() -> dict:
    """两套登录各自的当前账号：{"desktop": {...}, "cli": {...}}，带 key / exists（文件夹在不在）/ count / email / name。
    桌面端认不出来时按 current_key('desktop') 的退回规则补上，via 写明是怎么来的。"""
    d = desktop_account()
    if not d["key"]:
        k = current_key("desktop")
        if k:
            a, _, g = k.partition("/")
            c = cli_account()
            d = dict({"accountUuid": a, "organizationUuid": g, "key": k,
                      "via": "cli" if c["key"] == k else "newest-folder"}, **_profile(a))
    c = cli_account()
    if c["key"]:
        c.update(email_from="cli", email_guess=False)
    try:
        c["read_from"], c["file_time"] = str(Path.home() / ".claude.json"), (Path.home() / ".claude.json").stat().st_mtime
    except Exception:
        pass
    d = dict(d, **desktop_freshness(d))
    if not d.get("org_name") and c.get("org_name") and c.get("key") and c["key"] == d.get("key"):
        d["org_name"] = c["org_name"]                    # 同一个号同一个组织：组织名借命令行那边的
    return {"desktop": _with_folder(d), "cli": _with_folder(c), "api": api_account()}


def _iso(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def desktop_freshness(d: dict) -> dict:
    """桌面端的号可信不可信：Local Storage 里最新那条记录的时间、桌面端开没开、有没有别的号的文件夹更新得更晚。
    桌面端开着时它的 Local Storage 不一定马上写盘，最近一次写会话的是另一个号 → 多半是刚换了号还没落盘：标 stale，建议按那个号。"""
    out = {"read_at": __import__("time").time()}
    ldb = app_path("ldb")
    try:
        out["ldb_time"] = max((f.stat().st_mtime for f in ldb.iterdir() if f.suffix in (".log", ".ldb")), default=0) or None
    except Exception:
        out["ldb_time"] = None
    try:
        from . import apps
        out["desktop_running"] = bool(apps.running("claude"))
    except Exception:
        out["desktop_running"] = None
    seen = _iso(d.get("seen"))
    fs = folders()
    newest = fs[0] if fs else None
    if newest and d.get("key") and newest["key"] != d["key"] and newest["latest"] > max(seen, 1) + 60:
        out.update(stale=True, suggest=newest["key"],
                   stale_why=("桌面端开着，它的登录信息可能还没写到磁盘" if out["desktop_running"] else "认出的号比最近一次写会话的号旧")
                   + f"；最近有会话写入的是另一个号（{_short(newest['key'].partition('/')[0])}）")
    else:
        out["stale"] = False
    return out


def current_key(source: str = "desktop") -> str | None:
    """当前登录的账号 / 组织。
    source='desktop'（缺省，会话列表都是给桌面端看的）：桌面端 Local Storage 里认出来的号；
        认不出来就退回命令行的号（它的文件夹得在），再不行退回最近有写入的文件夹。
    source='cli'：~/.claude.json 的 oauthAccount，没登就 None。
    认出来的号哪怕还没有文件夹也照样返回（那就是「这个号在桌面端还没开过会话」）。"""
    if source == "cli":
        return cli_account()["key"]
    k = desktop_account(profile=False)["key"]
    if k:
        return k
    c = cli_account()["key"]
    if c and (sessions_root() / c).is_dir():
        return c
    fs = folders()
    return fs[0]["key"] if fs else None


def visible_key() -> str | None:
    """桌面端现在显示的是哪个会话列表文件夹：API 模式 → API 模式那个（3p: 前缀），否则 current_key('desktop')。"""
    try:
        if desktop_mode()["mode"] == "3p":
            b = api_info()["bucket"]
            if b:
                return b["key"]
    except Exception:
        pass
    return current_key("desktop")


def _transcript_exists(cli_sid: str | None) -> bool:
    if not cli_sid:
        return False
    return any((paths.CLAUDE_HOME / "projects").glob(f"*/{cli_sid}.jsonl"))

_KEEP = ("isArchived", "isStarred", "lastFocusedAt")
# 旧账号被停用时留下的报错（「organization has disabled …」），带到新账号里会一直挂着
_DROP = ("error", "errorAt", "priorErrorMark")


def _fresh(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _DROP}


SOURCES = {"desktop": "桌面端", "cli": "命令行（CLI）", "api": "API 模式"}
# 订阅登录那边的模型名长这样；跨模式同步时，会话里记的别的模型名在另一边多半没有
_1P_MODEL = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-[0-9][0-9a-z.-]*(\[1m\])?$")


def _model_warn(model: str | None, to_profile: str, from_profile: str) -> str | None:
    if not model or to_profile == from_profile:
        return None
    if to_profile == "1p" and _1P_MODEL.match(model) and not model.endswith("-thinking"):
        return None
    where = "订阅登录" if to_profile == "1p" else "API 模式的供应商"
    return f"这个会话记的模型 {model} 在{where}那边可能没有，打开后可能要换个模型"


def plan(target: str | None = None, source: str = "desktop") -> dict:
    if source not in SOURCES:
        return {"ok": False, "error": f"不认识的来源：{source}（只能是 desktop / cli / api）", "source": source}
    acc = accounts()[source]
    base = {"source": source, "account": acc, "mode": desktop_mode()}
    fs = all_folders()
    if not fs:
        return dict(base, ok=False, error=f"没找到 Claude 桌面端的会话列表目录：{sessions_root()}")
    target = target or acc.get("key")
    if not target and source == "api":
        return dict(base, ok=False, error="API 模式还没有会话列表文件夹——先在 Claude 桌面端切到 API 模式开一个 Claude Code 会话，"
                    "再回来同步（本程序不会自己建设备号文件或文件夹）。")
    if not target:
        return dict(base, ok=False, error=f"认不出{SOURCES[source]}现在登的是哪个号"
                    + ("（~/.claude.json 里没有登录信息，命令行先 claude 登一下）" if source == "cli" else ""))
    tgt = next((f for f in fs if f["key"] == target), None)
    if not tgt:
        if target.startswith(API_PREFIX):
            return dict(base, ok=False, error="API 模式的这个会话列表文件夹不在了——先在 Claude 桌面端的 API 模式里开一个 Claude Code 会话。")
        who = f"{SOURCES[source]}的号 {_short(target)}/{_short(target.partition('/')[2])}"
        return dict(base, ok=False, error=f"{who} 还没有会话列表文件夹——这个号还没在 Claude 桌面端里开过 Claude Code 会话。"
                    "先用这个号在桌面端开一个 Claude Code 会话，再回来补齐。")
    prof = {f["key"]: f.get("profile", "1p") for f in fs}
    tdir = Path(tgt["path"])
    have = {f.name for f in tdir.glob("local_*.json")}
    # 在当前账号里删过的（桌面端留 deleted_<会话号> 墓碑）不复活
    tomb = {f.name[len("deleted_"):] for f in tdir.glob("deleted_*")}
    have_cli = set()
    for f in tdir.glob("local_*.json"):
        try:
            have_cli.add(json.loads(f.read_text(encoding="utf-8")).get("cliSessionId"))
        except Exception:
            pass
    # 同一个会话可能在好几个旧账号文件夹里都有登记（换过不止一次号），挑 lastActivityAt 最新的那份；
    # 当前账号里已有、但别处有更新版本的，记进 update（之前按文件夹顺序取第一份，会补进旧版本）
    best: dict[str, tuple[Path, dict, str]] = {}
    for f in fs:
        if f["key"] == tgt["key"]:
            continue
        try:   # 归档有两处记：登记里的 isArchived 和文件夹里的 archived-sessions.idx
            idx = set(json.loads((Path(f["path"]) / "archived-sessions.idx").read_text(encoding="utf-8")).get("archived") or [])
        except Exception:
            idx = set()
        for src in Path(f["path"]).glob("local_*.json"):
            try:
                d = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            if src.stem in idx:
                d["isArchived"] = True
            old = best.get(src.name)
            if old is None or d.get("lastActivityAt", 0) > old[1].get("lastActivityAt", 0):
                best[src.name] = (src, d, f["key"])
    add, update, skip_missing, skip_tomb, skip_have = [], [], 0, 0, 0
    for name, (src, d, frm) in best.items():
        item = {"src": str(src), "name": name, "title": d.get("title") or "（无标题）",
                "cwd": d.get("cwd"), "from": frm, "archived": bool(d.get("isArchived")),
                "last": d.get("lastActivityAt", 0) / 1000, "from_profile": prof.get(frm, "1p"),
                "model": d.get("model"), "warn": _model_warn(d.get("model"), tgt.get("profile", "1p"), prof.get(frm, "1p"))}
        if name in have:
            try:
                cur = json.loads((tdir / name).read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("lastActivityAt", 0) > cur.get("lastActivityAt", 0):
                update.append(item)
            continue
        if d.get("cliSessionId") in tomb or name[len("local_"):-len(".json")] in tomb:
            skip_tomb += 1
            continue
        if d.get("cliSessionId") in have_cli:
            skip_have += 1
            continue
        if not _transcript_exists(d.get("cliSessionId")):
            skip_missing += 1
            continue
        add.append(item)
    update.sort(key=lambda x: -x["last"])
    add.sort(key=lambda x: -x["last"])
    tgt = dict(tgt, label=("API 模式（所有供应商共用）" + (f" · {acc.get('provider')}" if acc.get("provider") else ""))
               if tgt.get("profile") == "3p" else None)
    return dict(base, ok=True, target=tgt, folders=fs, add=add, update=update, skip_missing=skip_missing,
                skip_tomb=skip_tomb, skip_have=skip_have, tombstones=len(tomb), warns=sum(1 for x in add + update if x.get("warn")))


def _atomic_write(dst: Path, text: str) -> None:
    """先写同目录的临时文件（名字不以 local_ 开头，桌面端不会当成会话），再一步换上去。"""
    tmp = dst.with_name(f".ccss-tmp-{os.getpid()}-{dst.name}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)


def run(target: str | None = None, dry: bool = False, only: list[str] | None = None,
        source: str = "desktop", what: str = "sync") -> dict:
    """only：只补这几个（local_*.json 文件名）；不给就全补。source：补到桌面端（缺省）还是命令行登的那个号。"""
    p = plan(target, source)
    if p.get("ok") and only is not None:
        keep = set(only)
        p["add"] = [a for a in p["add"] if a["name"] in keep]
    if not p["ok"] or dry or not (p["add"] or p["update"]):
        if p.get("ok") and not dry and only is None:
            _switch_handled(p)
        return dict(p, done=0, updated=0, dry=dry)
    tdir = Path(p["target"]["path"])
    bak = _backup(tdir)
    done = 0
    for a in p["add"]:
        dst = tdir / a["name"]
        if dst.exists():
            continue
        _atomic_write(dst, json.dumps(_fresh(json.loads(Path(a["src"]).read_text(encoding="utf-8"))), ensure_ascii=False))
        done += 1
    updated = 0
    for a in p["update"]:
        dst = tdir / a["name"]
        cur = json.loads(dst.read_text(encoding="utf-8"))
        new = _fresh(json.loads(Path(a["src"]).read_text(encoding="utf-8")))
        for k in _KEEP:                      # 在当前账号里做过的收藏 / 归档 / 点开时间，留着
            if k in cur:
                new[k] = cur[k]
        _atomic_write(dst, json.dumps(new, ensure_ascii=False))
        updated += 1
    _write_ledger(bak, tdir, what, source=source, added=[a["name"] for a in p["add"]], updated=[a["name"] for a in p["update"]])
    if only is None or not [x for x in plan(target, source).get("add", [])]:
        _switch_handled(p)
    return dict(p, done=done, updated=updated, dry=False, backup=str(bak))


def _switch_handled(p: dict) -> None:
    """同步到了现在的账号（或者看过发现不缺什么）：「检测到切换账号」算处理完，提示条收起、不再提醒。"""
    try:
        from . import watch
        watch.mark_handled(key=(p.get("target") or {}).get("key"), source=p.get("source"))
    except Exception:
        pass


# ---------- 备份账本与还原 ----------
# 每次写之前把目标文件夹整个拷进 claude-sync-bak/<时间>/<账号>/<组织>/，旁边 ledger.json 记这次补了哪些、刷新了哪些。
# 还原：补进来的挪进回收站，刷新过的换回备份里的版本；别的（之后你自己新开的会话）不动。

BAK_ROOT = paths.HOME / "claude-sync-bak"


def _backup(tdir: Path) -> Path:
    bak = BAK_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    while bak.exists():
        bak = bak.with_name(bak.name + "x")
    shutil.copytree(tdir, bak / tdir.parent.name / tdir.name)
    return bak


def _write_ledger(bak: Path, tdir: Path, what: str, **kw) -> None:
    key = f"{tdir.parent.name}/{tdir.name}"
    if any(Path(b["path"]) == tdir for b in api_info()["buckets"]):
        key = API_PREFIX + key
    led = {"what": what, "time": datetime.now().isoformat(timespec="seconds"), "target": str(tdir),
           "key": key, "profile": "3p" if key.startswith(API_PREFIX) else "1p", **kw}
    (bak / "ledger.json").write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")


def list_backups() -> list[dict]:
    out = []
    if not BAK_ROOT.exists():
        return out
    for b in sorted(BAK_ROOT.iterdir(), reverse=True):
        if not b.is_dir() or not b.name[:8].isdigit():
            continue
        try:
            led = json.loads((b / "ledger.json").read_text(encoding="utf-8"))
        except Exception:
            led = None
        dirs = [d for a in b.iterdir() if a.is_dir() for d in a.iterdir() if d.is_dir()]
        out.append({"id": b.name, "ledger": led, "key": led["key"] if led else (f"{dirs[0].parent.name}/{dirs[0].name}" if dirs else None),
                    "count": sum(1 for d in dirs for _ in d.glob("local_*.json")),
                    "added": len(led.get("added", [])) if led else None, "updated": len(led.get("updated", [])) if led else None})
    return out


def restore(backup_id: str) -> dict:
    """把某次同步撤回去。没有账本的老备份：只补回现在缺的登记，不覆盖、不删。"""
    b = BAK_ROOT / backup_id
    if not b.is_dir() or "/" in backup_id or "\\" in backup_id or ".." in backup_id:
        return {"ok": False, "error": f"没有这个备份：{backup_id}"}
    dirs = [d for a in b.iterdir() if a.is_dir() for d in a.iterdir() if d.is_dir()]
    if len(dirs) != 1:
        return {"ok": False, "error": "备份目录结构不对（应该正好一个 账号/组织）"}
    src = dirs[0]
    try:
        led = json.loads((b / "ledger.json").read_text(encoding="utf-8"))
    except Exception:
        led = None
    tdir = sessions_root() / src.parent.name / src.name
    if led and str(led.get("key") or "").startswith(API_PREFIX):     # API 模式那边的：按账本记的文件夹还原（得是现在认得的那个）
        hit = next((Path(f["path"]) for f in api_folders() if f["key"] == led["key"]), None)
        if not hit:
            return {"ok": False, "error": "这次同步写的是 API 模式的会话列表，那个文件夹现在找不到了"}
        tdir = hit
    pre = _backup(tdir)                       # 还原之前也先备份一次，还原错了还能再还原回来
    trash = paths.HOME / "trash" / f"claude-restore-{datetime.now():%Y%m%d-%H%M%S}"
    removed = restored = 0
    if led:
        if led.get("what") in ("sync", "auto-sync") and led.get("added"):
            _skip_add(led.get("key") or f"{tdir.parent.name}/{tdir.name}", led["added"])   # 撤回过的：自动同步别再补回来
        for n in led.get("added", []):
            f = tdir / n
            if f.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), trash / n)
                removed += 1
        names = led.get("updated", [])
    else:
        # 没账本的老备份不知道那次改了哪些：只补回现在缺的登记，不覆盖现有的（覆盖会把之后的新进度改回旧的）
        names = [f.name for f in src.glob("local_*.json") if not (tdir / f.name).exists()]
    for n in names:
        if (src / n).exists():
            shutil.copy2(src / n, tdir / n)
            restored += 1
    _write_ledger(pre, tdir, "restore", source=backup_id, removed=removed, restored=restored)
    return {"ok": True, "removed": removed, "restored": restored, "backup": str(pre),
            "trash": str(trash) if removed else None}


def _acc_line(name: str, a: dict) -> str:
    if not a.get("key"):
        return f"{name}：认不出来"
    if a.get("profile") == "3p":
        return f"{name}：{a['key']}  API 模式（所有供应商共用）  已有 {a['count']} 个会话"
    via = {"leveldb": "", "cli": "（认不出来，按命令行的号算）", "newest-folder": "（认不出来，按最近有写入的文件夹算）"}.get(a.get("via"), "")
    fold = f"已有 {a['count']} 个会话" if a.get("exists") else "还没有文件夹"
    who = " ".join(x for x in (a.get("name"), a.get("email")) if x)
    if who and a.get("email_guess"):
        who += "（推测：以前记下的）"
    return f"{name}：{a['key']}{'  ' + who if who else ''}  {fold}{via}"


def _api_line() -> str:
    i, m = api_info(), desktop_mode()
    head = f"桌面端模式：{'API 模式' if m['mode'] == '3p' else '订阅登录'}（{m['why']}）"
    if not i["base"]:
        return head + "\nAPI 模式：没找到数据目录（Claude-3p）"
    b = i["bucket"]
    return (head + f"\nAPI 模式：{i['base']}  供应商 {i['provider'] or '—'}  "
            + (f"{b['key']}  已有 {b['count']} 个会话（所有供应商共用）" if b else "还没有会话列表文件夹"))


def _print_plan(r: dict) -> None:
    print(f"目标（{SOURCES[r['source']]}{'' if r['source'] == 'api' else '当前账号 / 组织'}）：{r['target']['key']}（已有 {r['target']['count']} 个会话）")
    print(f"可以补进来 {len(r['add'])} 个；已有但别处更新、要刷新 {len(r['update'])} 个；"
          f"对话记录已不在、跳过 {r['skip_missing']} 个；在这个号里删过（墓碑）不复活 {r['skip_tomb']} 个；"
          f"同一会话已在、跳过 {r['skip_have']} 个")
    for a in r["add"][:40]:
        t = datetime.fromtimestamp(a["last"]).strftime("%m-%d %H:%M") if a["last"] else "—"
        print(f"  {t}  {a['title'][:40]}{'（已归档）' if a['archived'] else ''}  ·  {a['cwd']}" + (f"  ⚠ {a['warn']}" if a.get("warn") else ""))
    if len(r["add"]) > 40:
        print(f"  … 还有 {len(r['add']) - 40} 个")


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry-run" in argv
    tgt = None
    if "--to" in argv:
        tgt = argv[argv.index("--to") + 1]
    src = argv[argv.index("--source") + 1] if "--source" in argv else None
    if src and src not in (*SOURCES, "both"):
        print(f"✗ --source 只能是 desktop / cli / api / both，不认识：{src}")
        return 2
    if "--list" in argv:
        acc = accounts()
        print(_acc_line("桌面端当前", acc["desktop"]))
        print(_acc_line("命令行当前", acc["cli"]))
        print(_api_line())
        for f in all_folders():
            t = datetime.fromtimestamp(f["latest"]).strftime("%m-%d %H:%M") if f["latest"] else "—"
            tag = "、".join(n for n, s in (("桌面端", "desktop"), ("CLI", "cli")) if acc[s].get("key") == f["key"])
            print(f"{f['key']}  会话 {f['count']:>3}  最近 {t}{'  ← ' + tag + '当前' if tag else ''}")
        return 0
    if "--backups" in argv:
        for b in list_backups():
            what = (f"补 {b['added']} / 刷新 {b['updated']}" if b["ledger"] and b["ledger"]["what"] == "sync"
                    else "还原前的备份" if b["ledger"] else "老备份（没账本）")
            print(f"{b['id']}  {b['key'][:17] if b['key'] else '?'}  会话 {b['count']:>3}  {what}")
        return 0
    from . import apps
    if "--restore" in argv:
        bid = argv[argv.index("--restore") + 1]
        r = apps.run_or_defer("claude", "claude-restore", {"backup": bid}, f"撤回 Claude 同步 {bid}")
        print(r.get("hint") or json.dumps({k: v for k, v in r.items() if k != "deferred"}, ensure_ascii=False))
        return 0
    only = argv[argv.index("--only") + 1].split(",") if "--only" in argv else None
    if dry:
        # 演练缺省两边都看；--to 指定了文件夹就只看一次（来源只影响显示）
        srcs = ["desktop", "cli"] + (["api"] if api_info()["bucket"] else []) if (src or "both") == "both" else [src]
        if tgt:
            srcs = srcs[:1]
        seen, bad = {}, 0
        for s in srcs:
            r = run(tgt, True, only, s)
            print(f"===== {SOURCES[s]} =====")
            print(_acc_line("当前账号", r["account"]))
            if not r["ok"]:
                print("✗ " + r["error"])
                bad += 1
            elif r["target"]["key"] in seen:
                print(f"（跟{SOURCES[seen[r['target']['key']]]}是同一个号 / 组织，结果同上）")
            else:
                seen[r["target"]["key"]] = s
                _print_plan(r)
            print()
        print("（演练，没写文件；去掉 --dry-run 真正补齐，真补时用 --source desktop 或 --source cli 选一边，缺省桌面端）")
        return 1 if bad == len(srcs) else 0
    if src == "both":
        print("✗ 真正补齐一次只能选一边：--source desktop / cli / api")
        return 2
    src = src or default_source()
    r = run(tgt, True, only, src)
    print(_acc_line(f"{SOURCES[src]}当前账号", r["account"]))
    if not r["ok"]:
        print("✗ " + r["error"])
        return 1
    _print_plan(r)
    if not (r["add"] or r["update"]):
        return 0
    # 桌面端开着时直接写，会在它退出时被内存里的旧登记盖回去：排到它退出之后
    r = apps.run_or_defer("claude", "claude-sync", {"target": tgt or r["target"]["key"], "only": only, "source": src},
                          f"同步 Claude 会话到{SOURCES[src]}当前账号")
    if r.get("deferred"):
        print(r["hint"])
    elif not r.get("ok"):
        print("✗ " + r.get("error", "失败"))
    else:
        print(f"✓ 已补齐 {r['done']} 个、刷新 {r['updated']} 个，原目录备份在 {r['backup']}（撤回：--restore {Path(r['backup']).name}）。"
              "打开 Claude 桌面端就能在列表里看到。")
    return 0


# ---------- 自动同步（无感） ----------
# 为什么要同步：桌面端的会话列表按「账号 / 组织」分文件夹（claude-code-sessions/<账号>/<组织>/local_*.json），
# 换号后新文件夹里没有别的号下开过的会话；对话内容（~/.claude/projects）本来就在。同步 = 把别的号文件夹里的登记补 / 刷新到当前号。
# 自动同步只做和手动一样的事（plan + run，备份 + 账本），另外：
# - 只在 Claude 桌面端没开时写（开着时它会在退出时把列表盖回去，也可能正写着）；开着就记成「等它退出」，退出后马上补
# - 账号文件（~/.claude.json、桌面端 Local Storage）或会话文件夹有变化 → 防抖 20 秒后检查；没变化也每 10 分钟查一次
# - 目标文件夹 15 秒内有写入就先不动；没东西要补就什么都不写（也不备份）
# - 自动同步的备份只留最近 20 份（手动同步的不动）
AUTO = {"pending_n": 0, "sig": None, "changed": 0.0, "evaluated": 0.0, "was_running": None, "pending": False, "events": [], "error": None, "last_check": None}
AUTO_KEEP = 20


def _auto_sig() -> tuple:
    out = []
    for f in [Path.home() / ".claude.json"]:
        try:
            st = f.stat()
            out.append((f.name, st.st_mtime_ns, st.st_size))   # 加上大小：同一个时钟刻度里改了两次也认得出
        except Exception:
            out.append((f.name, 0, 0))
    ldb = app_path("ldb")
    try:
        out += sorted((f.name, st.st_mtime_ns, st.st_size) for f in ldb.iterdir() if f.suffix in (".log", ".ldb")
                      for st in [f.stat()])
    except Exception:
        pass
    try:
        out += sorted((f"{a.name}/{o.name}", o.stat().st_mtime_ns, 0) for a in sessions_root().iterdir() if a.is_dir()
                      for o in a.iterdir() if o.is_dir())
    except Exception:
        pass
    try:                                                 # API 模式：会话列表文件夹、deploymentMode（切模式也要重新算目标）
        _API_RES["v"] = None
        i = api_info()
        out += sorted((b["key"], Path(b["path"]).stat().st_mtime_ns, 0) for b in i["buckets"])
        out.append(("mode", i["mode"] or "", 0))
    except Exception:
        pass
    return tuple(out)


def _prune_auto() -> None:
    try:
        autos = [b for b in sorted(BAK_ROOT.iterdir(), reverse=True)
                 if b.is_dir() and (b / "ledger.json").exists()
                 and json.loads((b / "ledger.json").read_text(encoding="utf-8")).get("what") == "auto-sync"]
        for b in autos[AUTO_KEEP:]:
            shutil.rmtree(b, ignore_errors=True)
    except Exception:
        pass


def auto_tick(now: float | None = None, force: bool = False, debounce: float = 20, period: float = 600,
              quiet: float = 15) -> dict | None:
    """后台每 ~30 秒调一次。返回这次做了的事件（补了 / 刷新了几个），没做事返回 None。"""
    import time
    from . import apps
    now = now or time.time()
    if not sessions_root().exists() and not api_info()["buckets"]:
        return None
    if not _auto_lock(now):                              # 另一个进程（比如另开的 cc-sessionsync auto）正在做自动同步
        AUTO["other"] = True
        return None
    AUTO["other"] = False
    sig = _auto_sig()
    if sig != AUTO["sig"]:
        first = AUTO["sig"] is None
        AUTO["sig"] = sig
        AUTO["changed"] = 0.0 if first else now          # 启动时那次不用等防抖：马上查
        if not first and not force:
            return None
    try:
        running = bool(apps.running("claude"))
    except Exception:
        running = True                                   # 认不出来就当开着：宁可不写
    quit_now = AUTO["was_running"] is True and not running
    AUTO["was_running"] = running
    if AUTO["changed"] and now - AUTO["changed"] < debounce and not force:
        return None
    due = force or quit_now or AUTO["changed"] or not AUTO["evaluated"] or now - AUTO["evaluated"] >= period
    if not due:
        return None
    if running:
        if AUTO["changed"] or not AUTO["evaluated"]:     # 有变化但桌面端开着：只读算一下，有要补的就等它退出
            try:
                p = plan(source=default_source())
                if p.get("ok"):
                    skip = set(_skip_load().get(p["target"]["key"], []))
                    p["add"] = [x for x in p["add"] if x["name"] not in skip]
                AUTO["pending"] = bool(p.get("ok") and (p["add"] or p["update"]))
                AUTO["pending_n"] = len(p.get("add") or []) + len(p.get("update") or []) if AUTO["pending"] else 0
            except Exception:
                pass
            AUTO["evaluated"] = AUTO["evaluated"] or now
        AUTO["changed"] = 0.0
        return None
    AUTO["changed"], AUTO["evaluated"], AUTO["last_check"] = 0.0, now, now
    try:
        p = plan(source=default_source())
        if not p.get("ok"):
            AUTO["error"], AUTO["pending"] = p.get("error"), False
            return None
        AUTO["error"] = None
        skip = set(_skip_load().get(p["target"]["key"], []))
        p["add"] = [a for a in p["add"] if a["name"] not in skip]
        if not (p["add"] or p["update"]):
            AUTO["pending"] = False
            return None
        tdir = Path(p["target"]["path"])
        newest = max((f.stat().st_mtime for f in tdir.glob("local_*.json")), default=0)
        if now - newest < quiet:
            AUTO["changed"] = now                        # 刚有写入：过一会儿再来
            return None
        r = run(p["target"]["key"], source=p["source"], what="auto-sync", only=[a["name"] for a in p["add"]])
        _prune_auto()
        AUTO["pending"] = False
        ev = {"id": f"{int(now)}", "time": now, "added": r.get("done", 0), "updated": r.get("updated", 0),
              "account": _short(p["target"]["key"].partition("/")[0]), "backup": Path(r["backup"]).name if r.get("backup") else None,
              "titles": [a["title"] for a in p["add"][:5]]}
        AUTO["events"] = (AUTO["events"] + [ev])[-20:]
        return ev
    except Exception as e:
        AUTO["error"] = f"{type(e).__name__}: {e}"
        return None


SKIP_FILE = paths.HOME / "claude-autosync-skip.json"
AUTO_LOCK = paths.HOME / "claude-autosync.lock"
AUTO_LOCK_TTL = 120          # 秒：持有者每次 tick（~30 秒）刷新；超过这么久没刷新就算它不在了


def _auto_lock(now: float) -> bool:
    """同一时间只能有一个进程在做自动同步：谁先占住锁文件谁做，另一个跳过。"""
    me = {"pid": os.getpid(), "ts": now, "who": sys.modules[__package__].PRODUCT}
    try:
        cur = json.loads(AUTO_LOCK.read_text(encoding="utf-8"))
        if cur.get("pid") != me["pid"] and now - float(cur.get("ts") or 0) < AUTO_LOCK_TTL:
            return False
    except Exception:
        pass
    try:
        AUTO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        tmp = AUTO_LOCK.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(me), encoding="utf-8")
        os.replace(tmp, AUTO_LOCK)
    except Exception:
        pass
    return True


def auto_release() -> None:
    try:
        if json.loads(AUTO_LOCK.read_text(encoding="utf-8")).get("pid") == os.getpid():
            AUTO_LOCK.unlink()
    except Exception:
        pass


def _skip_load() -> dict:
    try:
        return json.loads(SKIP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _skip_add(key: str, names: list[str]) -> None:
    d = _skip_load()
    d[key] = sorted(set(d.get(key, [])) | set(names))
    SKIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SKIP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SKIP_FILE)


def auto_status() -> dict:
    return dict({k: AUTO[k] for k in ("pending", "pending_n", "events", "error", "last_check")}, other=AUTO.get("other", False))

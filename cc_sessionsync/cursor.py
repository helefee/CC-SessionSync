# -*- coding: utf-8 -*-
"""Cursor 的对话（只读）+ Cursor → Claude Code 导入 + 任意会话 → 给 Cursor 用的上下文文件。

Cursor 把对话放在三个地方（本机 Cursor 3.25 实测的结构，只看键名 / 字段名）：

1. 编辑器（IDE）：<User>/globalStorage/state.vscdb（SQLite，<User> = %APPDATA%\\Cursor\\User、~/Library/Application Support/Cursor/User、
   ~/.config/Cursor/User）。表 cursorDiskKV(key, value)：
       composerData:<对话号>            一个对话的元数据（_v 18）：name、createdAt / lastUpdatedAt（毫秒）、unifiedMode（agent / chat / …）、
                                        modelConfig.modelName、workspaceIdentifier{id, uri.fsPath}、subagentInfo.parentComposerId、
                                        fullConversationHeadersOnly = [{bubbleId, type, …}]（按顺序）、conversationState（"~…" 服务端用的状态）
       bubbleId:<对话号>:<气泡号>       一条消息（_v 3）：type 1 = 你说的，2 = 助手；text / richText、thinking{text, signature}、
                                        toolFormerData{name, params, rawArgs, result, status, error}（每次工具调用一个气泡）、
                                        images、codeBlocks、tokenCount、modelInfo、createdAt（ISO）
       agentKv:blob:<sha256>            新版 agent 的内容寻址块（protobuf / JSON；和命令行 store.db 里的 blobs 一样）—— 不读
   表 composerHeaders(composerId, workspaceId, isArchived, isSubagent, …)：侧边栏列表（归档标记从这里取）。
   <User>/workspaceStorage/<hash>/workspace.json：{"folder": "file:///…"} 或 {"workspace": "…code-workspace"}，<hash> = workspaceIdentifier.id。
2. agent 记录：~/.cursor/projects/<项目>/agent-transcripts/<对话号>/<对话号>.jsonl（子代理在 subagents/ 里）：
   每行 {"role": "user"|"assistant", "message": {"content": [{"type": "text"} | {"type": "tool_use", "name", "input"}]}}，
   没有时间、没有工具结果。气泡读不到（只有 agentKv 块）的对话用它补。
3. 命令行 cursor-agent：~/.cursor/chats/<md5(工作目录)>/<对话号>/store.db：meta('0') = 十六进制的 JSON
   {agentId, latestRootBlobId, name, createdAt, mode, lastUsedModel}；blobs(id = sha256, data)：根块是 protobuf，
   字段 1 重复 = 按顺序的消息块号（32 字节），消息块是 JSON {role, content}。

只读：Cursor 没开、WAL 是空的时用 immutable=1（一个字节都不碰）；开着时用 mode=ro（和别的读者一样，只读共享内存索引）。
读出来的对话转成本程序自己的一份「镜像」（~/.cc-sessionsync/cursor-mirror/<来源>/<对话号>.jsonl，一行头 + 每条一行），
会话管理 / 备份 / 时间线 / 导出 / 导入都用这份镜像，和 Claude / Codex / dsh 的记录文件一样处理。

Claude Code → Cursor 不写 Cursor 的数据库（原因见 README「Cursor」一节）：导出成一份 Markdown 上下文文件，
在 Cursor 里新开对话 @ 它接着聊。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths

NAME = "Cursor"
NORM = "ccss-cursor/1"
TO_CC_PREFIX = "（Cursor）"
MIRROR = paths.HOME / "cursor-mirror"
LEDGERS = paths.HOME / "cursor-import"
MAP = paths.HOME / "cursor-import-map.json"
EXPORTS = paths.HOME / "exports" / "cursor"
OUT_CLIP = 20000                     # 镜像里每个工具结果最多留多少字


class CursorError(Exception):
    pass


# ---------- 目录 ----------

def _setting(k: str) -> str | None:
    try:
        from . import settings
        v = settings.get(k)
        return v if isinstance(v, str) and v.strip() else None
    except Exception:
        return None


def user_dir() -> Path:
    """Cursor 的 User 目录（globalStorage / workspaceStorage 在里面）。CURSOR_USER_DIR > 设置 cursor_user_dir > 系统缺省。"""
    e = os.environ.get("CURSOR_USER_DIR") or _setting("cursor_user_dir")
    if e:
        return Path(e).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Cursor" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Cursor" / "User"


def cursor_home() -> Path:
    """~/.cursor（agent 记录、命令行的 chats）。CURSOR_CONFIG_DIR > 设置 cursor_home > ~/.cursor。"""
    e = os.environ.get("CURSOR_CONFIG_DIR") or _setting("cursor_home")
    return Path(e).expanduser() if e else Path.home() / ".cursor"


def global_db() -> Path:
    return user_dir() / "globalStorage" / "state.vscdb"


def install_info() -> dict | None:
    fake = os.environ.get("CC_SESSIONSYNC_CURSOR_INSTALL")
    cands = [Path(fake)] if fake else []
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(Path(la) / "Programs" / "cursor")
        cands += [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Cursor"]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Cursor.app/Contents"), Path.home() / "Applications/Cursor.app/Contents"]
    else:
        cands += [Path("/usr/share/cursor"), Path("/opt/Cursor"), Path("/opt/cursor")]
    for d in cands:
        pj = d / "resources" / "app" / "product.json"
        if d.name == "Contents":
            pj = d / "Resources" / "app" / "product.json"
        if pj.exists():
            try:
                j = json.loads(pj.read_text(encoding="utf-8"))
                return {"path": str(d), "version": j.get("version"), "name": j.get("nameLong") or "Cursor"}
            except Exception:
                return {"path": str(d), "version": None, "name": "Cursor"}
    return None


def procs() -> list[dict]:
    """在跑的 Cursor（编辑器 / cursor-agent 命令行）。只看进程名 / 路径。"""
    fake = os.environ.get("CC_SESSIONSYNC_FAKE_CURSOR_PROCS")
    if fake:
        try:
            return json.loads(Path(fake).read_text())
        except Exception:
            return []
    out = []
    try:
        if os.name == "nt":
            from . import detect
            for p in detect._win_procs({"total": 0, "denied": 0, "cmd_denied": 0, "errors": []}):
                n, exe = (p.get("name") or "").lower(), (p.get("exe") or "").lower()
                if n in ("cursor.exe", "cursor-agent.exe") or "\\programs\\cursor\\" in exe:
                    out.append({"pid": p["pid"], "name": p["name"]})
        elif sys.platform.startswith("linux"):
            for d in Path("/proc").iterdir():
                if not d.name.isdigit():
                    continue
                try:
                    cmd = (d / "cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                first = cmd[0].decode("utf-8", "replace").rsplit("/", 1)[-1].lower() if cmd else ""
                if first in ("cursor", "cursor-agent") or (first == "node" and len(cmd) > 1 and b"cursor-agent" in cmd[1]):
                    out.append({"pid": int(d.name), "name": first})
        elif sys.platform == "darwin":
            import subprocess
            r = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5)
            for ln in r.stdout.splitlines():
                pid, _, comm = ln.strip().partition(" ")
                if "/Cursor.app/" in comm or comm.endswith("/cursor-agent"):
                    out.append({"pid": int(pid), "name": comm.rsplit("/", 1)[-1]})
    except Exception:
        pass
    return out


def running() -> bool:
    return bool(procs())


# ---------- 只读打开 SQLite ----------

def _uri(db: Path) -> str:
    p = str(db.resolve()).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p                                  # C:/… → /C:/…
    return "file://" + urllib.parse.quote(p, safe="/:")


def connect(db: Path, live: bool | None = None) -> sqlite3.Connection:
    """只读连接。Cursor 没开、WAL 空 → immutable（不碰任何文件，包括 -shm）；否则 mode=ro。"""
    if not db.exists():
        raise CursorError(f"没有 {db}")
    wal = db.with_name(db.name + "-wal")
    if live is None:
        live = running()
    quiet = not live and (not wal.exists() or wal.stat().st_size == 0)
    c = sqlite3.connect(_uri(db) + ("?immutable=1" if quiet else "?mode=ro"), uri=True, timeout=5, check_same_thread=False)
    c.execute("pragma query_only=1")
    return c


def _range(c: sqlite3.Connection, prefix: str):
    """key 以 prefix 开头的行（走主键索引，不全表扫）。"""
    hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return c.execute("select key, value from cursorDiskKV where key >= ? and key < ?", (prefix, hi))


def _json(v):
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    try:
        return json.loads(v)
    except Exception:
        return None


# ---------- 路径 / 时间 ----------

def uri_path(u) -> str | None:
    """file:///c%3A/x/y → C:\\x\\y（Windows 样子）或 /x/y。别的协议（远程）原样返回。"""
    if isinstance(u, dict):
        if u.get("fsPath"):
            return u["fsPath"]
        u = (u.get("scheme") or "file") + "://" + (u.get("authority") or "") + (u.get("path") or "")
    if not isinstance(u, str) or not u:
        return None
    if not u.startswith("file:"):
        return u
    m = re.match(r"^file:/{0,2}([A-Za-z]:[\\/].*)$", u)      # 不规范的 file://C:\x 写法
    if m:
        return m.group(1)[0].upper() + urllib.parse.unquote(m.group(1)[1:]).replace("/", "\\")
    p = urllib.parse.unquote(urllib.parse.urlparse(u).path)
    m = re.match(r"^/([A-Za-z]):(/.*)?$", p)
    if m:
        return m.group(1).upper() + ":" + (m.group(2) or "/").replace("/", "\\")
    return p


def slug(path: str) -> str:
    """~/.cursor/projects 下的目录名：路径里不是字母数字的都换成 -。"""
    return re.sub(r"[^A-Za-z0-9]+", "-", path or "").strip("-")


def _ms_iso(s) -> int | None:
    if isinstance(s, (int, float)):
        return int(s)
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _iso_ms(ms) -> str:
    return datetime.fromtimestamp((ms or time.time() * 1000) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{int(ms or 0) % 1000:03d}Z"


def workspaces() -> dict[str, str]:
    """workspaceStorage 的 <hash> → 文件夹（或 .code-workspace）路径。"""
    out = {}
    d = user_dir() / "workspaceStorage"
    if not d.is_dir():
        return out
    for w in d.iterdir():
        try:
            j = json.loads((w / "workspace.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        p = uri_path(j.get("folder") or j.get("workspace"))
        if p:
            out[w.name] = p
    return out


# ---------- 文字 ----------

_TAGS = re.compile(r"^\s*<(user_info|system_reminder|attached_files|open_and_recently_viewed_files|additional_data|"
                   r"cursor_commands|rules|agent_requestable_workspace_rules|always_applied_workspace_rules|git_status|"
                   r"timestamp|terminal_files_information|project_layout|mcp_file_system|task_notification)\b")


def user_text(t: str) -> str | None:
    """agent 记录 / 命令行里你说的话包在 <user_query> 里；没有的是 Cursor 自己塞的上下文（环境、规则…）→ None。"""
    if not isinstance(t, str):
        return None
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", t, re.S)
    if m:
        return m.group(1)
    if _TAGS.match(t):
        return None
    return t


def _rich_text(rt) -> str:
    """richText（Lexical 编辑器的 JSON）→ 纯文字。"""
    j = _json(rt) if isinstance(rt, str) else rt
    out = []

    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("text"), str):
                out.append(n["text"])
            for ch in n.get("children") or []:
                walk(ch)
            if n.get("type") in ("paragraph",):
                out.append("\n")
        elif isinstance(n, list):
            for x in n:
                walk(x)
    walk((j or {}).get("root") if isinstance(j, dict) else j)
    return "".join(out).strip()


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n…（还有 {len(s) - n} 字，截掉了）"


def _result_text(r) -> str:
    """工具结果（JSON 字符串，各工具字段不一样）→ 文字。"""
    j = _json(r) if isinstance(r, str) else r
    if j is None:
        return r if isinstance(r, str) else ""
    if isinstance(j, str):
        return j
    if isinstance(j, dict):
        for k in ("output", "contents", "content", "stdout", "result", "text", "message", "summary"):
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                extra = j.get("stderr")
                return v + (("\n[stderr]\n" + extra) if isinstance(extra, str) and extra.strip() else "")
        if isinstance(j.get("files"), list):
            return "\n".join(str(x.get("path") or x.get("uri") or x) if isinstance(x, dict) else str(x) for x in j["files"][:400])
    return json.dumps(j, ensure_ascii=False)[:OUT_CLIP]


def _args(a):
    if isinstance(a, str):
        j = _json(a)
        return j if j is not None else a
    return a


# ---------- 编辑器（state.vscdb）----------

def _composer_meta(d: dict, ws: dict, hdr: dict) -> dict:
    cid = d.get("composerId")
    w = d.get("workspaceIdentifier") or {}
    cwd = uri_path(w.get("uri")) if w.get("uri") else None
    if not cwd and w.get("id") in ws:
        cwd = ws[w["id"]]
    h = hdr.get(cid) or {}
    if not cwd and h.get("workspaceId") in ws:
        cwd = ws[h["workspaceId"]]
    sub = d.get("subagentInfo") or {}
    heads = [x.get("bubbleId") for x in d.get("fullConversationHeadersOnly") or [] if isinstance(x, dict) and x.get("bubbleId")]
    return {"id": cid, "source": "ide", "title": (d.get("name") or "").strip(), "created": d.get("createdAt"),
            "updated": d.get("lastUpdatedAt") or d.get("conversationCheckpointLastUpdatedAt") or d.get("createdAt"),
            "mode": d.get("unifiedMode") or d.get("forceMode"), "model": (d.get("modelConfig") or {}).get("modelName"),
            "parent": sub.get("parentComposerId"), "subagent": bool(sub) or bool(h.get("isSubagent")),
            "subagent_type": sub.get("subagentTypeName"), "archived": bool(h.get("isArchived")), "draft": bool(d.get("isDraft")),
            "cwd": cwd, "heads": heads, "v": d.get("_v")}


def ide_composers(c: sqlite3.Connection) -> list[dict]:
    ws = workspaces()
    hdr = {}
    try:
        for cid, wid, arch, sub in c.execute("select composerId, workspaceId, isArchived, isSubagent from composerHeaders"):
            hdr[cid] = {"workspaceId": wid, "isArchived": arch, "isSubagent": sub}
    except sqlite3.Error:
        pass
    out = []
    for k, v in _range(c, "composerData:"):
        d = _json(v)
        if not isinstance(d, dict) or not d.get("composerId"):
            continue
        out.append(_composer_meta(d, ws, hdr))
    return out


def bubble_items(b: dict) -> list[dict]:
    """一个气泡 → 镜像里的几条（user 文字 / assistant 文字 / 思考 / 工具）。"""
    ts = _ms_iso(b.get("createdAt"))
    model = (b.get("modelInfo") or {}).get("modelName")
    out = []
    if b.get("type") == 1:
        t = b.get("text") or _rich_text(b.get("richText"))
        imgs = len(b.get("images") or [])
        if t or imgs:
            it = {"role": "user", "kind": "text", "text": t or "", "ts": ts}
            if imgs:
                it["images"] = imgs
                paths = [x.get("path") for x in b.get("images") if isinstance(x, dict) and isinstance(x.get("path"), str) and x.get("path")]
                if paths:                       # 0.4.1：记下原图在本机的位置，转换时（文件还在的话）把原图带过去
                    it["image_paths"] = paths[:20]
            out.append(it)
        return out
    th = b.get("thinking")
    th = th.get("text") if isinstance(th, dict) else th
    if isinstance(th, str) and th.strip():
        out.append({"role": "assistant", "kind": "thinking", "text": th, "ts": ts})
    tf = b.get("toolFormerData")
    if isinstance(tf, dict) and tf.get("name"):
        it = {"role": "assistant", "kind": "tool", "tool": tf.get("name"), "input": _args(tf.get("params") or tf.get("rawArgs") or ""),
              "output": _clip(_result_text(tf.get("result")) if tf.get("result") is not None else "", OUT_CLIP),
              "status": tf.get("status"), "ts": ts}
        if tf.get("error"):
            it["error"] = _clip(str(tf["error"]), 4000)
        out.append(it)
    t = b.get("text")
    if isinstance(t, str) and t.strip():
        it = {"role": "assistant", "kind": "text", "text": t, "ts": ts}
        if model:
            it["model"] = model
        tc = b.get("tokenCount") or {}
        if tc.get("inputTokens") or tc.get("outputTokens"):
            it["tokens"] = {"in": tc.get("inputTokens") or 0, "out": tc.get("outputTokens") or 0}
        out.append(it)
    elif not out:
        for cb in b.get("codeBlocks") or []:          # 只有代码块没有文字的回复
            if isinstance(cb, dict) and (cb.get("content") or cb.get("code")):
                lang = cb.get("languageId") or cb.get("language") or ""
                where = uri_path(cb.get("uri")) or ""
                out.append({"role": "assistant", "kind": "text", "ts": ts,
                            "text": (f"`{where}`\n" if where else "") + f"```{lang}\n{cb.get('content') or cb.get('code')}\n```"})
    return out


def ide_items(c: sqlite3.Connection, meta: dict) -> list[dict]:
    items = []
    for bid in meta["heads"]:
        r = c.execute("select value from cursorDiskKV where key = ?", (f"bubbleId:{meta['id']}:{bid}",)).fetchone()
        b = _json(r[0]) if r else None
        if isinstance(b, dict):
            items += bubble_items(b)
    return items


# ---------- agent 记录（~/.cursor/projects/*/agent-transcripts）----------

def transcript_files() -> list[dict]:
    out = []
    pd = cursor_home() / "projects"
    if not pd.is_dir():
        return out
    for proj in pd.iterdir():
        td = proj / "agent-transcripts"
        if not td.is_dir():
            continue
        for d in td.iterdir():
            f = d / f"{d.name}.jsonl"
            if d.is_dir() and f.exists():
                out.append({"id": d.name, "file": f, "project": proj.name, "parent": None})
                sd = d / "subagents"
                if sd.is_dir():
                    for s in sd.glob("*.jsonl"):
                        out.append({"id": s.stem, "file": s, "project": proj.name, "parent": d.name})
            elif d.suffix == ".jsonl" and d.is_file():
                out.append({"id": d.stem, "file": d, "project": proj.name, "parent": None})
    return out


def transcript_items(f: Path) -> list[dict]:
    items = []
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        o = _json(ln)
        if not isinstance(o, dict) or o.get("role") not in ("user", "assistant"):
            continue
        c = (o.get("message") or {}).get("content")
        parts = [{"type": "text", "text": c}] if isinstance(c, str) else c or []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                if o["role"] == "user":
                    t = user_text(p["text"])
                    if t:
                        items.append({"role": "user", "kind": "text", "text": t})
                else:
                    items.append({"role": "assistant", "kind": "text", "text": p["text"]})
            elif p.get("type") == "tool_use":
                items.append({"role": "assistant", "kind": "tool", "tool": p.get("name"), "input": p.get("input"), "output": ""})
    return items


# ---------- 命令行（~/.cursor/chats/*/*/store.db）----------

def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise ValueError("varint")
        x = b[i]
        i += 1
        n |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return n, i


def pb_fields(b: bytes) -> list[tuple[int, int, object]]:
    """最小的 protobuf 解析：[(字段号, 线型, 值)]，长度型给 bytes。坏了就抛 ValueError。"""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            n, i = _varint(b, i)
            if i + n > len(b):
                raise ValueError("length")
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.append((f, wt, v))
    return out


def cli_files() -> list[dict]:
    d = cursor_home() / "chats"
    if not d.is_dir():
        return []
    return [{"id": f.parent.name, "file": f, "wskey": f.parent.parent.name} for f in d.glob("*/*/store.db")]


def cli_load(f: Path) -> tuple[dict, list[dict]]:
    c = connect(f, live=False if not (f.with_name(f.name + "-wal")).exists() else None)
    try:
        r = c.execute("select value from meta where key = '0'").fetchone()
        if not r:
            raise CursorError("store.db 里没有 meta")
        v = r[0]
        v = v.decode() if isinstance(v, (bytes, bytearray)) else v
        try:
            meta = json.loads(bytes.fromhex(v).decode("utf-8"))
        except ValueError:
            meta = json.loads(v)
        root = c.execute("select data from blobs where id = ?", (meta.get("latestRootBlobId"),)).fetchone()
        items, cwd = [], None
        if root:
            fs = pb_fields(bytes(root[0]))
            for fnum, wt, val in fs:
                if fnum == 9 and wt == 2 and cwd is None:
                    try:
                        cwd = uri_path(val.decode("utf-8")) or None
                    except UnicodeDecodeError:
                        pass
            ids = [val.hex() for fnum, wt, val in fs if fnum == 1 and wt == 2 and len(val) == 32]
            for bid in ids:
                rr = c.execute("select data from blobs where id = ?", (bid,)).fetchone()
                m = _json(rr[0]) if rr else None
                if isinstance(m, dict):
                    items += _cli_msg(m)
        meta["_cwd"] = cwd
        return meta, items
    finally:
        c.close()


def _cli_msg(m: dict) -> list[dict]:
    role, c = m.get("role"), m.get("content")
    parts = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []
    out = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if role == "user" and t == "text":
            s = user_text(p.get("text"))
            if s:
                out.append({"role": "user", "kind": "text", "text": s})
        elif role == "user" and t == "image":
            it = {"role": "user", "kind": "text", "text": "", "images": 1}
            d = p.get("image") if isinstance(p.get("image"), str) else p.get("data") if isinstance(p.get("data"), str) else None
            if d:                               # 0.4.1：命令行版把图片内嵌成 base64 / data URL，原样留着给转换用
                mt = p.get("mimeType") or p.get("mediaType") or ""
                if d.startswith("data:") and "," in d:
                    mt, d = d[5:].split(",", 1)[0].split(";")[0], d.split(",", 1)[1]
                if len(d) <= 7_000_000:
                    it["image_inline"] = [{"mt": mt, "b64": d}]
            out.append(it)
        elif role == "assistant" and t == "text" and p.get("text"):
            out.append({"role": "assistant", "kind": "text", "text": p["text"]})
        elif role == "assistant" and t in ("reasoning", "redacted-reasoning") and p.get("text"):
            out.append({"role": "assistant", "kind": "thinking", "text": p["text"]})
        elif role == "assistant" and t == "tool-call":
            out.append({"role": "assistant", "kind": "tool", "tool": p.get("toolName"), "input": p.get("args"), "output": "",
                        "id": p.get("toolCallId")})
        elif role == "tool" and t == "tool-result":
            r = p.get("result")
            out.append({"role": "tool", "kind": "result", "id": p.get("toolCallId"), "tool": p.get("toolName"),
                        "output": _clip(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False), OUT_CLIP)})
    return out


def _merge_results(items: list[dict]) -> list[dict]:
    """命令行的工具结果是单独的消息：并回对应的工具调用里。"""
    by = {it.get("id"): it for it in items if it.get("kind") == "tool" and it.get("id")}
    out = []
    for it in items:
        if it.get("kind") == "result":
            t = by.get(it.get("id"))
            if t is not None:
                t["output"] = it.get("output") or ""
                continue
            it = {"role": "assistant", "kind": "tool", "tool": it.get("tool"), "input": None, "output": it.get("output") or ""}
        out.append(it)
    for it in out:
        it.pop("id", None)
    return out


# ---------- 镜像 ----------

def norm_bytes(header: dict, items: list[dict]) -> bytes:
    h = dict(header, type="cursor", format=NORM)
    lines = [json.dumps(h, ensure_ascii=False, sort_keys=True)]
    lines += [json.dumps({k: v for k, v in it.items() if v is not None}, ensure_ascii=False, sort_keys=True) for it in items]
    return ("\n".join(lines) + "\n").encode("utf-8")


def load_norm(data: bytes) -> tuple[dict, list[dict]]:
    header, items = {}, []
    for ln in data.decode("utf-8", "replace").splitlines():
        o = _json(ln)
        if not isinstance(o, dict):
            continue
        if o.get("type") == "cursor" and not header:
            header = o
        elif o.get("role"):
            items.append(o)
    return header, items


def _write_if_changed(f: Path, data: bytes) -> None:
    try:
        if f.read_bytes() == data:
            return
    except OSError:
        pass
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, f)


_STATE: dict = {"sig": None, "rows": [], "at": 0, "error": None}


def _sig() -> tuple:
    parts = []
    for f in (global_db(), global_db().with_name("state.vscdb-wal")):
        try:
            st = f.stat()
            parts.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append((str(f), 0, 0))
    for x in transcript_files() + cli_files():
        try:
            st = x["file"].stat()
            parts.append((str(x["file"]), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    return tuple(parts)


def _title_of(items: list[dict]) -> str:
    t = next((it.get("text") for it in items if it.get("role") == "user" and it.get("text")), "")
    return " ".join(t.split())[:60]


def _cache() -> dict:
    try:
        return json.loads((MIRROR / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh(force: bool = False) -> list[dict]:
    """读 Cursor 的三处数据 → 更新镜像，返回每个对话一行（不带内容）。没变就用上次的结果。"""
    sig = _sig()
    if not force and sig == _STATE["sig"] and _STATE["rows"] is not None:
        return _STATE["rows"]
    old = _cache()
    rows, seen, err = [], set(), None
    ide_meta: dict[str, dict] = {}
    db = global_db()
    if db.exists():
        try:
            c = connect(db)
            try:
                for m in ide_composers(c):
                    ide_meta[m["id"]] = m
                    if not m["heads"]:
                        continue
                    key = [m["updated"], len(m["heads"]), m["heads"][-1], m.get("title"), m.get("archived"), m.get("cwd")]
                    f = MIRROR / "ide" / f"{m['id']}.jsonl"
                    o = old.get(f"ide:{m['id']}")
                    if not force and o and o.get("key") == key and f.exists():
                        rows.append(o["row"])
                        seen.add(m["id"])
                        continue
                    items = ide_items(c, m)
                    if not items:
                        continue
                    rows.append(_mirror(f, "ide", m, items, key))
                    seen.add(m["id"])
            finally:
                c.close()
        except (sqlite3.Error, CursorError, OSError) as e:
            err = f"读 Cursor 的 state.vscdb 出错：{type(e).__name__}: {e}"
    for t in transcript_files():                       # 气泡里没有的（新版 agent / 只有记录的）
        if t["id"] in seen:
            continue
        try:
            st = t["file"].stat()
            key = [st.st_mtime_ns, st.st_size]
            f = MIRROR / "transcript" / f"{t['id']}.jsonl"
            o = old.get(f"transcript:{t['id']}")
            if not force and o and o.get("key") == key and f.exists():
                rows.append(o["row"])
                seen.add(t["id"])
                continue
            items = transcript_items(t["file"])
            if not items:
                continue
            m = dict(ide_meta.get(t["id"]) or {})
            if not m:
                cwd = next((x["cwd"] for x in ide_meta.values() if x.get("cwd") and slug(x["cwd"]).lower() == t["project"].lower()), None)
                m = {"id": t["id"], "title": "", "created": int(st.st_mtime * 1000), "updated": int(st.st_mtime * 1000), "cwd": cwd,
                     "parent": t["parent"], "subagent": bool(t["parent"]), "mode": "agent", "project": t["project"]}
            else:
                m["updated"] = max(m.get("updated") or 0, int(st.st_mtime * 1000))
            m.update(source="transcript", project=t["project"])
            rows.append(_mirror(f, "transcript", m, items, key))
            seen.add(t["id"])
        except Exception as e:
            err = err or f"读 agent 记录出错：{type(e).__name__}: {e}"
    for x in cli_files():
        if x["id"] in seen:
            continue
        try:
            st = x["file"].stat()
            key = [st.st_mtime_ns, st.st_size]
            f = MIRROR / "cli" / f"{x['id']}.jsonl"
            o = old.get(f"cli:{x['id']}")
            if not force and o and o.get("key") == key and f.exists():
                rows.append(o["row"])
                continue
            meta, items = cli_load(x["file"])
            items = _merge_results(items)
            if not items:
                continue
            m = {"id": x["id"], "source": "cli", "title": (meta.get("name") or "").strip() if meta.get("name") != "New Agent" else "",
                 "created": meta.get("createdAt"), "updated": max(meta.get("createdAt") or 0, int(st.st_mtime * 1000)),
                 "model": meta.get("lastUsedModel"), "mode": meta.get("mode"), "cwd": meta.get("_cwd"), "wskey": x["wskey"]}
            rows.append(_mirror(f, "cli", m, items, key))
        except Exception as e:
            err = err or f"读 cursor-agent 的 store.db 出错：{type(e).__name__}: {e}"
    idx = {f"{r['source']}:{r['id']}": {"key": r.pop("_key"), "row": r} for r in rows if "_key" in r}
    for k, v in old.items():                           # 没变的那些行原样留着
        if k not in idx and any(r["id"] == v["row"]["id"] and r["source"] == v["row"]["source"] for r in rows):
            idx[k] = v
    try:
        _write_if_changed(MIRROR / "index.json", json.dumps(idx, ensure_ascii=False, sort_keys=True).encode())
    except OSError:
        pass
    _STATE.update(sig=sig, rows=rows, at=time.time(), error=err)
    return rows


def _mirror(f: Path, source: str, m: dict, items: list[dict], key) -> dict:
    header = {k: m.get(k) for k in ("id", "title", "created", "cwd", "parent", "subagent", "mode", "model", "project", "subagent_type")}
    header["source"] = source
    data = norm_bytes(header, items)
    _write_if_changed(f, data)
    users = sum(1 for it in items if it["role"] == "user")
    asst = sum(1 for it in items if it["role"] == "assistant" and it.get("kind") == "text")
    models = [it.get("model") for it in items if it.get("model")]
    return {"id": m["id"], "source": source, "title": m.get("title") or "", "first": _title_of(items), "cwd": m.get("cwd"),
            "created": m.get("created"), "updated": m.get("updated"), "mode": m.get("mode"), "model": models[-1] if models else m.get("model"),
            "parent": m.get("parent"), "subagent": bool(m.get("subagent")), "archived": bool(m.get("archived")),
            "project": m.get("project"), "file": str(f), "size": len(data), "msgs": users + asst,
            "tools": sum(1 for it in items if it.get("kind") == "tool"), "_key": key}


# ---------- 给「会话管理」 ----------

SOURCE_NAME = {"ide": "Cursor 编辑器", "transcript": "Cursor agent 记录", "cli": "cursor-agent 命令行"}


def sessions(names: dict | None = None) -> list[dict]:
    names = names or {}
    out = []
    for r in refresh():
        al = names.get(f"cursor:{r['id']}")
        tt = r.get("title") or None
        out.append({"agent": "cursor", "id": r["id"], "title": al or tt or r.get("first") or "（无标题）", "alias": al, "tool_title": tt,
                    "first": r.get("first") or "", "cwd": r.get("cwd"), "last": (r.get("updated") or 0) / 1000, "size": r.get("size"),
                    "archived": r.get("archived", False), "registered": True, "visible": True, "file": r["file"],
                    "model": r.get("model"), "subagent": r.get("subagent", False), "msgs": r.get("msgs"), "source": r["source"],
                    "source_name": SOURCE_NAME.get(r["source"]), "project": r.get("project"), "mode": r.get("mode"),
                    "imported": False, "accounts": [],
                    "resume": f"cursor-agent --resume {r['id']}" if r["source"] == "cli" else None})
    return out


def find_file(sid: str) -> Path | None:
    for r in refresh():
        if r["id"] == sid:
            return Path(r["file"])
    for d in ("ide", "transcript", "cli"):
        f = MIRROR / d / f"{sid}.jsonl"
        if f.exists():
            return f
    return None


def _brief(a) -> str:
    if isinstance(a, dict):
        for k in ("command", "targetFile", "relativeWorkspacePath", "path", "pattern", "globPattern", "searchTerm", "url", "query", "description"):
            if isinstance(a.get(k), str) and a[k]:
                return " ".join(a[k].split())[:160]
        return " ".join(json.dumps(a, ensure_ascii=False).split())[:160]
    return " ".join(str(a or "").split())[:160]


def entries_from_bytes(data: bytes) -> list[dict]:
    """只读看：人话 / 回复 / 工具一行（思考不显示）。"""
    _, items = load_norm(data)
    out = []
    for it in items:
        ts = (it.get("ts") or 0) / 1000
        if it["role"] == "user":
            s = it.get("text") or ""
            if it.get("images"):
                s = (s + "\n" if s else "") + f"[图片 ×{it['images']}]"
            if s:
                out.append({"role": "user", "text": s, "ts": ts})
        elif it.get("kind") == "text":
            out.append({"role": "assistant", "text": it.get("text") or "", "ts": ts})
        elif it.get("kind") == "tool":
            out.append({"role": "tool", "text": f"{it.get('tool')}  {_brief(it.get('input'))}".strip(), "ts": ts})
    return out


def count_msgs(data: bytes) -> int:
    n = 0
    for ln in data.decode("utf-8", "replace").splitlines():
        o = _json(ln)
        if isinstance(o, dict) and (o.get("role") == "user" or (o.get("role") == "assistant" and o.get("kind") == "text")):
            n += 1
    return n


def read(sid: str, limit: int = 400) -> dict:
    f = find_file(sid)
    if not f:
        return {"ok": False, "error": "找不到这个 Cursor 对话"}
    es = entries_from_bytes(f.read_bytes())
    return {"ok": True, "file": str(f), "total": len(es), "entries": [dict(e, text=e["text"][:6000]) for e in es[-limit:]],
            "cut": max(0, len(es) - limit)}


def detect() -> dict:
    db = global_db()
    info = install_info()
    ps = procs()
    rows = refresh()
    by = {}
    for r in rows:
        by[r["source"]] = by.get(r["source"], 0) + 1
    st = None
    try:
        st = db.stat()
    except OSError:
        pass
    return {"user_dir": str(user_dir()), "db": str(db), "db_exists": st is not None, "db_bytes": st.st_size if st else 0,
            "cursor_home": str(cursor_home()), "cursor_home_exists": cursor_home().is_dir(),
            "transcripts": len(transcript_files()), "cli_chats": len(cli_files()), "install": info, "running": bool(ps),
            "procs": len(ps), "conversations": len(rows), "by_source": by, "error": _STATE.get("error"),
            "found": st is not None or bool(info) or cursor_home().is_dir(), "mirror": str(MIRROR)}


# ---------- Cursor → Claude Code ----------

def _fence(s: str, lang: str = "") -> str:
    from . import codex_import as CI
    return CI._fence(s) if not lang else f"```{lang}\n{s}\n```"


def build_claude(data: bytes, new_sid: str, version: str, title: str | None = None, reasoning: bool = False,
                 out_lines: int = 20, out_chars: int = 2000) -> dict:
    """镜像 → Claude Code 会话记录（只算，不写）。会丢 / 会变：工具调用和结果变成文字（结果截短），思考缺省丢（reasoning=True 当文字带上），
    图片换成占位，Cursor 塞进去的上下文（规则、打开的文件、环境）不带。"""
    from . import codex_import as CI
    h, items = load_norm(data)
    if h.get("format") != NORM:
        raise CursorError("不是本程序的 Cursor 镜像")
    sid = h.get("id") or ""
    cwd = CI.norm_cwd(h.get("cwd")) or str(Path.home())
    title = title or h.get("title") or _title_of(items) or "Cursor 对话"
    stats = {"user": 0, "assistant": 0, "tools": 0, "reasoning_dropped": 0, "reasoning_kept": 0, "images": 0}
    turns: list[list] = []

    def push(role, text, ts):
        if turns and turns[-1][0] == role:
            turns[-1][1].append(text)
        else:
            turns.append([role, [text], ts])

    src = {"ide": "Cursor 编辑器", "transcript": "Cursor（agent 记录）", "cli": "cursor-agent 命令行"}.get(h.get("source"), "Cursor")
    push("user", f"（以下是从 {src} 导入的对话「{title}」，Cursor 对话 {sid}，工作目录 {cwd}。"
                 "Cursor 里跑过的工具只留了文字摘要，输出是截短的；接着做之前先看一下文件 / git 的当前状态。）", h.get("created"))
    for it in items:
        ts = it.get("ts")
        if it["role"] == "user":
            stats["user"] += 1
            t = it.get("text") or ""
            if it.get("images"):
                stats["images"] += it["images"]
                t = (t + "\n" if t else "") + f"[图片 ×{it['images']}，没有带过来]"
            if t:
                push("user", t, ts)
        elif it.get("kind") == "text":
            stats["assistant"] += 1
            push("assistant", it.get("text") or "", ts)
        elif it.get("kind") == "thinking":
            if reasoning:
                stats["reasoning_kept"] += 1
                push("assistant", "〔Cursor 里的思考〕\n" + (it.get("text") or ""), ts)
            else:
                stats["reasoning_dropped"] += 1
        elif it.get("kind") == "tool":
            stats["tools"] += 1
            a = it.get("input")
            a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False, indent=1)
            s = f"〔Cursor 调用了 {it.get('tool')}〕" + ("（出错）" if it.get("status") == "error" or it.get("error") else "") + \
                "\n" + _fence(CI._clip(a or "", 30, 3000))
            out = it.get("output") or it.get("error") or ""
            if out:
                s += "\n输出：\n" + _fence(CI._clip(out, out_lines, out_chars))
            push("assistant", s, ts)
    if turns[-1][0] == "user":
        push("assistant", "（Cursor 那边到这里还没有回复。导入到此为止。）", turns[-1][2])
    recs, parent, last_ts = [], None, None
    for role, parts, ts in turns:
        u = str(uuid.uuid4())
        ts = ts or last_ts or h.get("created")
        last_ts = ts
        r = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": new_sid,
             "version": version, "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": _iso_ms(ts)}
        text = "\n\n".join(p for p in parts if p)
        if role == "user":
            r["message"] = {"role": "user", "content": text}
        else:
            r["message"] = {"id": "msg_cursorimport_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": "cursor-import", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        parent = u
    recs.append({"type": "custom-title", "customTitle": TO_CC_PREFIX + title, "sessionId": new_sid})
    return {"records": recs, "cwd": cwd, "title": title, "turns": len(turns), "stats": stats, "cursor_id": sid,
            "chars": sum(len(json.dumps(r, ensure_ascii=False)) for r in recs)}


def build_claude_bytes(data: bytes, new_sid: str, title: str | None = None) -> dict:
    from . import codex_import as CI
    return build_claude(data, new_sid, CI.claude_version(), title)


def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def plan_to_claude(only: list[str] | None = None, subagents: bool = False) -> dict:
    from . import codex_import as CI
    mp = _load(MAP, {})
    rows = []
    keys = [k.strip().lower() for k in (only or []) if k.strip()]
    for r in refresh():
        sid = r["id"]
        if keys and not any(sid.lower() == k or sid.lower().endswith(k) for k in keys):
            continue
        if r.get("subagent") and not keys and not subagents:
            continue                                   # 子代理缺省不导（父对话里有它的结果）
        f = Path(r["file"])
        rec = mp.get(f"cursor2cc:{sid}")
        state = "new"
        if rec:
            jf = Path(rec.get("file", ""))
            if not jf.exists():
                state = "new"
            elif rec.get("sha") and _sha(jf) != rec["sha"]:
                state = "diverged"
            elif rec.get("src") == _sha(f):
                state = "same"
            else:
                state = "update"
        rows.append({"id": sid, "title": r.get("title") or r.get("first") or "", "cwd": r.get("cwd"), "path": str(f),
                     "last": (r.get("updated") or 0) / 1000, "msgs": r.get("msgs"), "tools": r.get("tools"), "state": state,
                     "source": r["source"], "subagent": r.get("subagent"), "model": r.get("model"),
                     "session": rec.get("session") if rec and state in ("update", "same", "diverged") else None})
    try:
        dt = CI._desktop_target(None)
    except Exception:
        dt = None
    return {"ok": True, "rows": rows, "counts": {k: sum(1 for r in rows if r["state"] == k) for k in ("new", "update", "same", "diverged")},
            "desktop": dt, "claude_home": str(paths.CLAUDE_HOME), "error": _STATE.get("error")}


def _new_ledger(what: str) -> tuple[Path, dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = LEDGERS / stamp
    while d.exists():
        d = d.with_name(d.name + "x")
    (d / "before").mkdir(parents=True)
    return d, {"what": what, "time": datetime.now().isoformat(timespec="seconds"), "created": [], "updated": [],
               "items": [], "map_before": {}, "errors": [], "claude_home": str(paths.CLAUDE_HOME)}


def run_to_claude(only: list[str] | None = None, dry: bool = True, reasoning: bool = False, desktop: bool = True,
                  target: str | None = None, subagents: bool = False) -> dict:
    """Cursor → Claude Code：写 ~/.claude/projects/<目录>/<新会话号>.jsonl（可选登记进桌面端列表）。Cursor 的数据一个字节都不改。
    再导一次：Cursor 那边有新内容 → 原地更新那份（你没在 Claude 里接着聊过的话）；没变 → 跳过；接着聊过 → 不动。"""
    from . import codex_import as CI
    p = plan_to_claude(only, subagents)
    if target:
        try:
            p["desktop"] = CI._desktop_target(target)
        except Exception:
            p["desktop"] = None
    todo = [r for r in p["rows"] if r["state"] in ("new", "update")]
    ver = CI.claude_version()
    if dry or not todo:
        for r in todo:
            try:
                b = build_claude(Path(r["path"]).read_bytes(), r.get("session") or "00000000-0000-0000-0000-000000000000", ver,
                                 reasoning=reasoning)
                r.update(turns=b["turns"], chars=b["chars"], stats=b["stats"], title=b["title"],
                         target=str(paths.CLAUDE_HOME / "projects" / CI.project_slug(b["cwd"])))
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
        return dict(p, dry=True, done=0)
    mp = _load(MAP, {})
    tdir = Path(p["desktop"]["path"]) if desktop and p.get("desktop") else None
    led_dir, led = _new_ledger("cursor-to-claude")
    led["desktop"] = p["desktop"]["key"] if tdir else None

    def save_led():
        _save(led_dir / "ledger.json", led)

    def write(f: Path, text: str):
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists():
            n = f"{len(led['updated'])}-{f.name}"
            shutil.copy2(f, led_dir / "before" / n)
            led["updated"].append({"file": str(f), "before": n})
        else:
            led["created"].append(str(f))
        save_led()
        tmp = f.with_name(f.name + ".cc-sessionsync.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, f)

    done = 0
    for r in todo:
        k = f"cursor2cc:{r['id']}"
        rec = dict(mp.get(k) or {})
        led["map_before"][k] = mp.get(k)
        try:
            src = Path(r["path"])
            new = rec.get("session") if r["state"] == "update" and rec.get("session") else str(uuid.uuid4())
            b = build_claude(src.read_bytes(), new, ver, reasoning=reasoning)
            jf = paths.CLAUDE_HOME / "projects" / CI.project_slug(b["cwd"]) / f"{new}.jsonl"
            write(jf, CI._dump(b["records"]))
            ent = None
            if tdir is not None:
                cur = None
                old = rec.get("local") if r["state"] == "update" and rec.get("folder") == p["desktop"]["key"] else None
                if old and (tdir / f"{old}.json").exists():
                    cur = _load(tdir / f"{old}.json", {})
                d = CI._desktop_entry(tdir, new, b["cwd"], b["title"], (r.get("last") or time.time()), cur, prefix=TO_CC_PREFIX)
                ent = d["sessionId"]
                write(tdir / f"{ent}.json", json.dumps(d, ensure_ascii=False, indent=1))
            mp[k] = {"session": new, "file": str(jf), "sha": _sha(jf), "src": _sha(src), "local": ent or rec.get("local"),
                     "folder": p["desktop"]["key"] if ent else rec.get("folder"), "title": b["title"], "cwd": b["cwd"],
                     "imported": time.time(), "ledger": led_dir.name}
            led["items"].append({"cursor": r["id"], "session": new, "title": b["title"], "state": r["state"], "file": str(jf),
                                 "turns": b["turns"], "stats": b["stats"]})
            r.update(session=new, file=str(jf), turns=b["turns"], stats=b["stats"], resume=CI.resume_cmd(b["cwd"], new))
            done += 1
        except Exception as e:
            led["errors"].append(f"{r['id']}：{type(e).__name__}: {e}")
            r["error"] = f"{type(e).__name__}: {e}"
        save_led()
    _save(MAP, mp)
    save_led()
    return dict(p, dry=False, done=done, errors=led["errors"], ledger=led_dir.name, ledger_path=str(led_dir))


# ---------- 任意会话 → 给 Cursor 用的上下文文件 ----------

def context_md(agent: str, sid: str, out_lines: int = 12, out_chars: int = 1200) -> dict:
    """把一个会话（Claude Code / Codex / DeepSeek Harness / Cursor）写成一份给 Cursor 看的 Markdown：
    只有对话文字 + 工具一行摘要（不带原始记录，免得把上下文撑爆）。"""
    from . import sessions as S
    f = S.agent_file(agent, sid)
    if not f:
        raise CursorError("找不到会话记录文件")
    es = S.parse_bytes(agent, f.read_bytes())
    row = S.find(agent, sid) or {}
    title = row.get("title") or "对话"
    app = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "cursor": "Cursor"}.get(agent, agent)
    cwd = row.get("cwd") or ""
    L = [f"# {title}", "",
         f"> 这是在 {app} 里的一段对话（会话 `{sid}`{('，工作目录 `' + cwd + '`') if cwd else ''}），由 CC Session Sync 导出给 Cursor 当上下文。",
         "> 工具调用只留了一行摘要；接着做之前先看一下文件 / git 的当前状态。", ""]
    n = 0
    for e in es:
        t = (e.get("text") or "").rstrip()
        if not t:
            continue
        if e["role"] == "user":
            L += ["## 用户", "", t, ""]
            n += 1
        elif e["role"] == "assistant":
            L += [f"## {app}", "", t, ""]
            n += 1
        else:
            L += [f"- 〔工具〕`{' '.join(t.split())[:200]}`", ""]
    L += ["---", "", "（导出到此为止。请先读完上面的对话，再接着做最后一个没做完的事。）", ""]
    return {"text": "\n".join(L), "title": title, "cwd": cwd, "msgs": n, "app": app}


def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip()[:60].strip(". ")
    return s or "对话"


def plan_context(agent: str, sid: str, into_project: bool = False) -> dict:
    c = context_md(agent, sid)
    name = f"{_safe_name(c['title'])}-{sid[-8:]}.md"
    if into_project:
        if not c["cwd"] or not Path(c["cwd"]).is_dir():
            raise CursorError("这个会话的工作目录不在这台电脑上，没法写进项目；改成导出到本程序的导出目录")
        dst = Path(c["cwd"]) / ".cursor" / "sessionsync" / name
        mention = f"@.cursor/sessionsync/{name}"
    else:
        dst = EXPORTS / name
        mention = f"@{dst}"
    return dict(c, path=str(dst), exists=dst.exists(), mention=mention,
                prompt=f"{mention} 这是之前的对话记录，请先读完，再接着做最后一个没做完的事。")


def run_context(agent: str, sid: str, into_project: bool = False, dry: bool = True) -> dict:
    p = plan_context(agent, sid, into_project)
    p["text_chars"] = len(p["text"])
    text = p.pop("text")
    if dry:
        return dict(p, dry=True)
    dst = Path(p["path"])
    led_dir, led = _new_ledger("context-for-cursor")
    if dst.exists():
        n = f"0-{dst.name}"
        shutil.copy2(dst, led_dir / "before" / n)
        led["updated"].append({"file": str(dst), "before": n})
    else:
        led["created"].append(str(dst))
    _save(led_dir / "ledger.json", led)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, dst)
    led["items"].append({"agent": agent, "session": sid, "file": str(dst), "sha": _sha(dst)})
    led["final"] = {str(dst): _sha(dst)}
    _save(led_dir / "ledger.json", led)
    return dict(p, dry=False, ledger=led_dir.name)


# ---------- 账本 / 撤回 ----------

def ledgers() -> list[dict]:
    out = []
    if not LEDGERS.is_dir():
        return out
    for d in sorted(LEDGERS.iterdir(), reverse=True):
        led = _load(d / "ledger.json", None)
        if isinstance(led, dict):
            out.append({"id": d.name, "time": led.get("time"), "what": led.get("what"), "items": len(led.get("items", [])),
                        "created": len(led.get("created", [])), "updated": len(led.get("updated", [])), "undone": led.get("undone")})
    return out


def undo(ledger_id: str) -> dict:
    """撤回：新建的文件挪进回收站（不直接删），覆盖过的换回原来的。导过去之后又改过的（sha 变了）不动，列出来。"""
    if not ledger_id or any(c in ledger_id for c in "/\\") or ".." in ledger_id:
        return {"ok": False, "error": f"账本号不对：{ledger_id}"}
    d = LEDGERS / ledger_id
    led = _load(d / "ledger.json", None)
    if not isinstance(led, dict):
        return {"ok": False, "error": f"没有这个账本：{ledger_id}"}
    if led.get("undone"):
        return {"ok": False, "error": f"已经撤回过了（{led['undone']}）"}
    mp = _load(MAP, {})
    final = {v.get("file"): v.get("sha") for v in mp.values() if v.get("ledger") == ledger_id}
    final.update(led.get("final") or {})
    trash = paths.HOME / "trash" / f"cursor-import-undo-{datetime.now():%Y%m%d-%H%M%S}"
    moved, restored, kept = 0, 0, []
    for f in led.get("created", []):
        fp = Path(f)
        if not fp.exists():
            continue
        if fp.suffix in (".jsonl", ".md") and final.get(f) and _sha(fp) != final[f]:
            kept.append(f)
            continue
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(fp), str(trash / f"{moved}-{fp.name}"))
        moved += 1
    for u in led.get("updated", []):
        fp, src = Path(u["file"]), d / "before" / u["before"]
        if fp.exists() and final.get(u["file"]) and _sha(fp) != final[u["file"]]:
            kept.append(u["file"])
            continue
        if src.exists():
            if fp.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, trash / f"replaced-{restored}-{fp.name}")
            shutil.copy2(src, fp)
            restored += 1
    for k, before in (led.get("map_before") or {}).items():
        if (mp.get(k) or {}).get("ledger") != ledger_id:
            continue
        if before:
            mp[k] = before
        else:
            mp.pop(k, None)
    _save(MAP, mp)
    led["undone"] = datetime.now().isoformat(timespec="seconds")
    led["undo"] = {"moved": moved, "restored": restored, "kept": kept, "trash": str(trash) if trash.exists() else None}
    _save(d / "ledger.json", led)
    return {"ok": True, "moved": moved, "restored": restored, "kept": kept, "trash": str(trash) if trash.exists() else None}

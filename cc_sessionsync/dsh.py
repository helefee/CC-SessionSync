# -*- coding: utf-8 -*-
"""DeepSeek Harness（dsh，https://github.com/deepseek-ai/deepseek-harness ，MIT）的会话：找、列、只读看、备份，
和 Claude Code 之间互相导入（dsh → Claude Code、Claude Code → dsh，后者只新建会话）。

dsh 的会话格式（第 4 版，按它仓库里的 docs/persistence-catalog.md、packages/session/session-persistence-jsonl 读的；没有拷代码）：
    <会话根>/--<projectKey(cwd)>--/<encodeSegment(会话号)>/session.v4.jsonl.zstd    （压缩关掉时是 .jsonl；一个根里只能是同一种）
    会话根缺省 $DSH_HOME/sessions（DSH_HOME 缺省 ~/.dsh），profiles/<名字>/cordis.patch.yml 里 id: session-persistence-jsonl 的
    config.root / config.compression 可以改。
    zstd 版：一串各自独立、带校验和的 zstd 帧；第一帧只放头一行，后面每帧是一批事件行。
    第一行（头）：{type:'session', version:4, id:'session-<uuid>', createdAt 毫秒, cwd 绝对路径?, parentSession?, isSeeded,
                   origin?:'subagent', delegationDepth, agentPreset?}
    事件行：{type, seq（从 0 连续）, time 毫秒, data, ignorable?, surfaceOp?, sourceEventSeqs?}
      进模型的（surface）只有 system/message、developer/message、user/message、assistant/message、tool/result，
      它们带 surfaceOp：'append' 接在后面，{op:'replace', startSeq, endSeq} 把那一段换成这一条（压缩上下文用）。
      认不出的事件类型没标 ignorable 的，dsh 拒绝打开整个会话。
    session.lock：dsh 开着这个会话时独占它（Windows 是不共享的句柄，别的系统是 flock）；文件本身一直留着，不代表正在用。

这里只读用户的 dsh 数据；写只有一种：导入时新建一个会话目录（从不碰已有的会话），带账本、能撤回。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths

NAME = "DeepSeek Harness"
FORMAT = 4
TO_CC_PREFIX = "（DeepSeek）"          # 从 dsh 导进 Claude Code 的会话标题前缀
TO_DSH_PREFIX = "（Claude Code）"      # 从 Claude Code 导进 dsh 的
LEDGERS = paths.HOME / "dsh-import"
MAP = paths.HOME / "dsh-import-map.json"
IMPORT_PROVIDER = "claude-code-import"
SURFACE = {"system/message", "developer/message", "user/message", "assistant/message", "tool/result"}
# dsh 0.2.x 仓库里登记过的事件类型（docs/persistence-catalog.md + experimental-persistence-catalog.md）
KNOWN = set("""agent-preset/selected agent/inbox/spliced approval/asked approval/decided approval/policy assistant/attempt
assistant/message command/done command/run compaction/end compaction/prune compaction/start compaction/summary
deliverables/presented developer/message feedback/message-delete feedback/message-put feedback/record goal/change
hook/invoked hook/result image/offload job/registered job/settled llm/retry llm/retry-started model/selection
permission/preset plan/mode request/context request/header sandbox/mode schedule/change session-log-deepseek/delivery-accepted
session/end-seed session/title session/title-llm-request step/end step/start subagent/catalog subagent/descriptor
subagent/model-selection-policy system/message team/member team/message/delivered team/message/queued team/task todo/write
tool-workflow/agent-end tool-workflow/agent-start tool-workflow/run-end tool-workflow/run-start tool/call tool/ptc-dispatch
tool/ptc-dispatch-start tool/result turn/end turn/start user/message web/deepseek-search-llm-request
working-directory/change workspace/changes""".split())
HEADER_REQ = ("type", "version", "id", "createdAt", "isSeeded", "delegationDepth")
HEADER_KEYS = set(HEADER_REQ) | {"cwd", "parentSession", "origin", "agentPreset"}
BLOCKS = {"text", "reasoning", "image", "file", "tool-call", "tool-addition", "tool-removal"}


class DshError(Exception):
    pass


# ---------- 在哪 ----------

def home() -> Path:
    from . import settings
    v = os.environ.get("DSH_HOME") or settings.get("dsh_home")
    return Path(v).expanduser() if v else Path.home() / ".dsh"


def _patch_entry(text: str, pid: str = "session-persistence-jsonl") -> dict | None:
    """在 cordis.patch.yml（顶层是一个列表）里找 id: <pid> 那一项，只认 root / compression / disabled 三个字段。"""
    lines = text.splitlines()
    out, on = None, False
    for ln in lines:
        if re.match(r"^- ", ln):
            on = bool(re.match(rf"^-\s+id:\s*['\"]?{re.escape(pid)}['\"]?\s*$", ln))
            if on:
                out = {}
            continue
        if not on:
            continue
        m = re.match(r"^\s+(root|compression|disabled):\s*(.+?)\s*$", ln)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def _root_value(v: str, h: Path) -> tuple[Path | None, str | None]:
    m = re.match(r"^!!js\s+dshHomePath\(\s*['\"]([^'\"]+)['\"]\s*\)$", v)
    if m:
        return h / m.group(1), None
    if v.startswith("!!js"):
        return None, "会话根是一段 !!js 表达式，算不出来"
    p = Path(os.path.expandvars(v)).expanduser()
    return (p if p.is_absolute() else h / p), None


def roots() -> list[dict]:
    """[{root, compression, why, note}]：缺省根 + 各个 profile 里改过的根（去重）。"""
    h = home()
    out = [{"root": h / "sessions", "compression": "zstd", "why": "缺省（$DSH_HOME/sessions）", "note": None, "profile": None}]
    pd = h / "profiles"
    try:
        profs = sorted(p for p in pd.iterdir() if p.is_dir()) if pd.is_dir() else []
    except OSError:
        profs = []
    for p in profs:
        f = p / "cordis.patch.yml"
        try:
            e = _patch_entry(f.read_text(encoding="utf-8", errors="replace")) if f.is_file() else None
        except OSError:
            e = None
        if not e:
            continue
        r, note = _root_value(e["root"], h) if e.get("root") else (h / "sessions", None)
        comp = e.get("compression") or "zstd"
        if comp not in ("zstd", "none"):
            note = f"不认识的压缩方式 {comp}"
        if r is None:
            out.append({"root": None, "compression": comp, "why": f"profile {p.name}", "note": note, "profile": p.name})
            continue
        hit = next((x for x in out if x["root"] == r), None)
        if hit:
            if comp != hit["compression"]:
                hit["note"] = f"profile {p.name} 把压缩改成了 {comp}"
                hit["compression"] = comp
            hit["profile"] = hit["profile"] or p.name
        else:
            out.append({"root": r, "compression": comp, "why": f"profile {p.name} 改的", "note": note, "profile": p.name})
    return out


def install_info() -> dict | None:
    """桌面版（Electron 壳 + 自带的 dsh）装在哪、版本号。"""
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "Programs" / NAME)
    for k in ("ProgramFiles", "ProgramW6432"):
        if os.environ.get(k):
            cands.append(Path(os.environ[k]) / NAME)
    if sys.platform == "darwin":
        cands.append(Path("/Applications") / f"{NAME}.app")
    for d in cands:
        exe = d / f"{NAME}.exe"
        if exe.is_file() or (sys.platform == "darwin" and d.is_dir()):
            ver = None
            for vf in (d / "version", d / "resources" / "version", d / "resources" / "app" / "version"):
                try:
                    if vf.is_file() and vf.stat().st_size < 64:
                        ver = vf.read_text(encoding="utf-8", errors="replace").strip() or None
                        break
                except OSError:
                    pass
            return {"dir": str(d), "exe": str(exe), "version": ver}
    return None


def procs() -> list[dict]:
    """在跑的 dsh（桌面版 / 命令行）。只看进程名 / 路径。"""
    fake = os.environ.get("CC_SESSIONSYNC_FAKE_DSH_PROCS")
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
                n = (p.get("name") or "").lower()
                if n in ("deepseek harness.exe", "dsh.exe") or "deepseek harness" in (p.get("exe") or "").lower():
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
                second = cmd[1].decode("utf-8", "replace").lower() if len(cmd) > 1 else ""
                if first in ("dsh", "deepseek-harness") or (first == "node" and re.search(r"(^|/)dsh(\.js)?$|@deepseek-ai/dsh/", second)):
                    out.append({"pid": int(d.name), "name": first})
    except Exception:
        pass
    return out


def detect() -> dict:
    h = home()
    rs = roots()
    info = install_info()
    ps = procs()
    rows = []
    for r in rs:
        root = r["root"]
        enc = None
        if root is not None and root.is_dir():
            try:
                enc = root_encoding(root)
            except DshError as e:
                enc = f"错误：{e}"
        rows.append({"root": str(root) if root else None, "exists": bool(root and root.is_dir()), "compression": r["compression"],
                     "found_encoding": enc, "why": r["why"], "note": r["note"]})
    return {"home": str(h), "home_exists": h.is_dir(), "home_env": bool(os.environ.get("DSH_HOME")),
            "roots": rows, "install": info, "running": len(ps) > 0, "procs": len(ps), "zstd": _zstd_ok(),
            "found": h.is_dir() or bool(info)}


# ---------- zstd ----------

def _zstd():
    try:
        import zstandard
        return zstandard
    except Exception:
        return None


def _zstd_ok() -> bool:
    return _zstd() is not None


def _frames(raw: bytes) -> list[tuple[int, int, bool]]:
    """把一串 zstd 帧切开（只看帧头 / 块头，不解压）：[(起, 止, 带不带校验和)]。最后一帧不完整就报错（会话被写坏了 / 正在写）。"""
    out, pos, n = [], 0, len(raw)
    while pos < n:
        start = pos
        if n - pos < 4:
            raise DshError(f"第 {pos} 字节：帧不完整")
        magic = int.from_bytes(raw[pos:pos + 4], "little")
        if 0x184D2A50 <= magic <= 0x184D2A5F:            # skippable 帧
            size = int.from_bytes(raw[pos + 4:pos + 8], "little")
            pos += 8 + size
            out.append((start, pos, False))
            continue
        if magic != 0xFD2FB528:
            raise DshError(f"第 {pos} 字节：不是 zstd 帧")
        pos += 4
        fhd = raw[pos]
        pos += 1
        fcs_flag, single, has_ck, did_flag = fhd >> 6, (fhd >> 5) & 1, (fhd >> 2) & 1, fhd & 3
        if not single:
            pos += 1                                    # window descriptor
        pos += (0, 1, 2, 4)[did_flag]
        pos += (1 if single else 0, 2, 4, 8)[fcs_flag]
        while True:
            if pos + 3 > n:
                raise DshError(f"第 {start} 字节开始的帧不完整")
            bh = int.from_bytes(raw[pos:pos + 3], "little")
            pos += 3
            last, btype, bsize = bh & 1, (bh >> 1) & 3, bh >> 3
            pos += 1 if btype == 1 else bsize
            if last:
                break
        if has_ck:
            pos += 4
        if pos > n:
            raise DshError(f"第 {start} 字节开始的帧不完整")
        out.append((start, pos, bool(has_ck)))
    return out


def decode(raw: bytes, compression: str) -> tuple[str, dict]:
    """→ (文本, {frames, checksums})。zstd：每帧单独解，带校验和的由 zstandard 校验。"""
    if compression == "none":
        return raw.decode("utf-8"), {"frames": 0, "checksums": 0}
    z = _zstd()
    if z is None:
        raise DshError("缺 zstandard 模块，读不了压缩的 dsh 会话")
    fr = _frames(raw)
    parts = []
    for a, b, _ in fr:
        if int.from_bytes(raw[a:a + 4], "little") != 0xFD2FB528:
            continue
        try:
            d = z.ZstdDecompressor().decompressobj()
            parts.append(d.decompress(raw[a:b]))
        except Exception as e:
            raise DshError(f"第 {a} 字节的帧解不开 / 校验和不对：{type(e).__name__}") from None
    return b"".join(parts).decode("utf-8"), {"frames": len(fr), "checksums": sum(1 for f in fr if f[2])}


def encode_frames(chunks: list[str]) -> bytes:
    """每段压成一个独立、带校验和（XXH64）的 zstd 帧，按顺序接起来（和 dsh 自己写的一样：头一帧，事件一帧）。"""
    z = _zstd()
    if z is None:
        raise DshError("缺 zstandard 模块，写不了压缩的 dsh 会话")
    c = z.ZstdCompressor(level=3, write_checksum=True, write_content_size=True)
    return b"".join(c.compress(s.encode("utf-8")) for s in chunks)


# ---------- 会话文件 ----------

LOG_RE = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl(\.zstd)?$")


def log_files(d: Path) -> list[tuple[Path, int, str]]:
    """会话目录里的各代记录文件：[(路径, 格式版本, 压缩)]，新的在前。session.jsonl[.zstd] 是第 0 版（升级前留下的）。"""
    out = []
    try:
        for f in d.iterdir():
            m = LOG_RE.match(f.name)
            if m and f.is_file():
                out.append((f, int(m.group(1) or 0), "zstd" if m.group(2) else "none"))
    except OSError:
        pass
    out.sort(key=lambda x: -x[1])
    return out


def root_encoding(root: Path) -> str | None:
    """这个根里已有的会话用的哪种压缩（'zstd' / 'none' / 还没有会话 None）。两种都有 → 报错（dsh 自己也拒绝）。"""
    seen = set()
    for proj in _iterdirs(root):
        for d in _iterdirs(proj):
            for _, _, comp in log_files(d):
                seen.add(comp)
    if len(seen) > 1:
        raise DshError("这个会话根里压缩的和不压缩的都有")
    return next(iter(seen), None)


def _iterdirs(p: Path):
    try:
        return sorted(x for x in p.iterdir() if x.is_dir() and not x.name.startswith("."))
    except OSError:
        return []


def load_bytes(raw: bytes, compression: str) -> dict:
    text, info = decode(raw, compression)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise DshError("空文件")
    try:
        header = json.loads(lines[0])
    except Exception:
        raise DshError("头一行不是 JSON") from None
    evs = []
    for i, ln in enumerate(lines[1:], 1):
        try:
            evs.append(json.loads(ln))
        except Exception:
            raise DshError(f"第 {i + 1} 行不是 JSON") from None
    return {"header": header, "events": evs, "frames": info["frames"], "checksums": info["checksums"], "compression": compression}


def load(f: Path) -> dict:
    comp = "zstd" if f.name.endswith(".zstd") else "none"
    return load_bytes(f.read_bytes(), comp)


def sniff(raw: bytes) -> str:
    return "zstd" if raw[:4] == b"\x28\xb5\x2f\xfd" else "none"


def surface(events: list[dict]) -> list[dict]:
    """按 surfaceOp 拼出模型看到的那一串（压缩过的上下文用替换后的那一条）。"""
    out: list[dict] = []
    for e in events:
        if e.get("type") not in SURFACE:
            continue
        op = e.get("surfaceOp", "append")
        if isinstance(op, dict) and op.get("op") == "replace":
            idx = [i for i, x in enumerate(out) if op.get("startSeq", -1) <= x.get("seq", -2) <= op.get("endSeq", -1)]
            if idx:
                out[idx[0]:idx[-1] + 1] = [e]
            else:
                out.append(e)
        else:
            out.append(e)
    return out


def _msg(e: dict) -> dict:
    d = e.get("data") or {}
    return d if e.get("type") == "user/message" else (d.get("message") or {})


def _txt(content, reasoning: bool = False) -> str:
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            parts.append(b["text"])
        elif t == "reasoning" and reasoning and b.get("text"):
            parts.append("〔思考〕\n" + b["text"])
    return "\n\n".join(parts)


def _brief_args(a) -> str:
    try:
        j = json.loads(a) if isinstance(a, str) else a
    except Exception:
        return str(a)[:160]
    if isinstance(j, dict):
        v = j.get("command") or j.get("cmd") or j.get("path") or j.get("file_path") or j.get("pattern") or j.get("description") or j.get("url") or j.get("query")
        if v:
            return str(v)[:160]
        return json.dumps(j, ensure_ascii=False)[:160]
    return str(j)[:160]


def entries(events: list[dict]) -> list[dict]:
    """只读看：人话 / 回复 / 工具一行（和 Claude / Codex 的一样）。系统提示、运行时上下文这些不显示。"""
    out = []
    for e in surface(events):
        t, ts = e.get("type"), (e.get("time") or 0) / 1000
        m = _msg(e)
        if t == "user/message":
            if ((m.get("source") or {}).get("kind")) != "user":
                continue
            s = _txt(m.get("content"))
            imgs = sum(1 for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "image")
            if imgs:
                s = (s + "\n" if s else "") + f"[图片 ×{imgs}]"
            if s:
                out.append({"role": "user", "text": s, "ts": ts})
        elif t == "assistant/message":
            s = _txt(m.get("content"))
            if s:
                out.append({"role": "assistant", "text": s, "ts": ts})
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool-call":
                    out.append({"role": "tool", "text": f"{b.get('name')}  {_brief_args(b.get('arguments'))}".strip(), "ts": ts})
    return out


def entries_from_bytes(data: bytes) -> list[dict]:
    try:
        return entries(load_bytes(data, sniff(data))["events"])
    except Exception:
        return []


def count_msgs(data: bytes) -> int:
    """备份 / 时间线的「n 条消息」。增量备份时 data 可能只是追加的那几帧（没有头行），也能数。"""
    try:
        text, _ = decode(data, sniff(data))
    except Exception:
        return 0
    n = 0
    for ln in text.split("\n"):
        if '"user/message"' not in ln and '"assistant/message"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        m = _msg(e)
        if e.get("type") == "user/message" and (m.get("source") or {}).get("kind") == "user":
            n += 1
        elif e.get("type") == "assistant/message" and _txt(m.get("content")):
            n += 1
    return n


# ---------- 列表 ----------

_CACHE: dict[str, tuple] = {}


def session_files() -> list[dict]:
    """[{dir, file, version, compression, root, project, legacy}]：每个会话目录取最新一代记录文件。"""
    out, seen = [], set()
    for r in roots():
        root = r["root"]
        if root is None or not root.is_dir():
            continue
        for proj in _iterdirs(root):
            for d in _iterdirs(proj):
                fs = log_files(d)
                if not fs:
                    continue
                f, ver, comp = fs[0]
                if str(f) in seen:
                    continue
                seen.add(str(f))
                out.append({"dir": d, "file": f, "version": ver, "compression": comp, "root": root, "project": proj.name,
                            "legacy": [str(x[0]) for x in fs[1:]]})
    return out


def summary(sf: dict) -> dict:
    f = sf["file"]
    try:
        st = f.stat()
    except OSError:
        return {}
    k = str(f)
    c = _CACHE.get(k)
    if c and c[0] == (st.st_mtime, st.st_size):
        return c[1]
    s = {"id": sf["dir"].name, "format": sf["version"], "size": st.st_size, "mtime": st.st_mtime}
    if sf["version"] != FORMAT:
        s["error"] = f"第 {sf['version']} 版格式（只认第 {FORMAT} 版；用 dsh 打开一次会自动升级）"
    else:
        try:
            L = load(f)
            h, evs = L["header"], L["events"]
            s.update(id=h.get("id") or s["id"], cwd=h.get("cwd"), created=(h.get("createdAt") or 0) / 1000,
                     subagent=h.get("origin") == "subagent", parent=h.get("parentSession"))
            if h.get("version") != FORMAT:
                s["error"] = f"头里写的是第 {h.get('version')} 版"
            title, model, last = None, None, 0.0
            usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0, "calls": 0}
            for e in evs:
                t = e.get("type")
                last = max(last, (e.get("time") or 0) / 1000)
                if t == "session/title":
                    title = (e.get("data") or {}).get("title") or title
                elif t == "assistant/message":
                    d = e.get("data") or {}
                    model = ((d.get("message") or {}).get("source") or {}).get("model") or model
                    u = d.get("usage")
                    if isinstance(u, dict):
                        usage["calls"] += 1
                        usage["input"] += int(u.get("inputTokens") or 0)
                        usage["output"] += int(u.get("outputTokens") or 0)
                        usage["cache_read"] += int(u.get("cacheReadTokens") or 0)
                        usage["cache_write"] += int(u.get("cacheWriteTokens") or 0)
                        usage["reasoning"] += int(u.get("reasoningTokens") or 0)
            ents = entries(evs)
            first = next((" ".join(x["text"].split())[:200] for x in ents if x["role"] == "user"), "")
            s.update(title=title, first=first, model=model, last=max(last, s["created"]), usage=usage, events=len(evs),
                     msgs=sum(1 for x in ents if x["role"] in ("user", "assistant")), frames=L["frames"])
        except Exception as e:
            s["error"] = f"读不了：{e}"
    _CACHE[k] = ((st.st_mtime, st.st_size), s)
    return s


def resume_cmd(cwd: str | None, sid: str) -> str:
    q = (f'"{cwd}"' if any(c in cwd for c in ' &()') else cwd) if cwd else None
    return (f"cd {q} && " if q else "") + f"dsh --profile headless --session-id {sid} \"继续\""


def sessions(names: dict | None = None) -> list[dict]:
    """给「会话管理」的行（和 Claude / Codex 的字段一样，agent='dsh'）。"""
    names = names or {}
    out = []
    for sf in session_files():
        s = summary(sf)
        if not s:
            continue
        sid = s["id"]
        al = names.get(f"dsh:{sid}")
        tool_title = s.get("title")
        out.append({"agent": "dsh", "id": sid, "title": al or tool_title or s.get("first") or "（无标题）", "alias": al,
                    "tool_title": tool_title, "first": s.get("first") or "", "cwd": s.get("cwd"),
                    "last": max(s.get("last") or 0, s["mtime"]), "size": s["size"], "archived": False, "registered": True,
                    "visible": not s.get("error"), "why": s.get("error"), "file": str(sf["file"]), "model": s.get("model"),
                    "format": s.get("format"), "subagent": s.get("subagent", False), "msgs": s.get("msgs"),
                    "imported": (tool_title or "").startswith(TO_DSH_PREFIX), "accounts": [],
                    "resume": None if s.get("error") else resume_cmd(s.get("cwd"), sid)})
    return out


def find_file(sid: str) -> Path | None:
    for sf in session_files():
        if sf["dir"].name == sid or encode_segment(sid) == sf["dir"].name:
            return sf["file"]
    return None


def read(sid: str, limit: int = 400) -> dict:
    f = find_file(sid)
    if not f:
        return {"ok": False, "error": "找不到这个 DeepSeek Harness 会话"}
    try:
        L = load(f)
    except Exception as e:
        return {"ok": False, "error": f"读不了：{e}"}
    if L["header"].get("version") != FORMAT:
        return {"ok": False, "error": f"第 {L['header'].get('version')} 版格式，只认第 {FORMAT} 版"}
    es = entries(L["events"])
    return {"ok": True, "file": str(f), "total": len(es), "entries": [dict(e, text=e["text"][:6000]) for e in es[-limit:]],
            "cut": max(0, len(es) - limit)}


def usage_events() -> list[dict]:
    """用量统计用：每次模型调用一条 {sid, ts, provider, model, input, output, cache_read, cache_write, reasoning, msg}。"""
    out = []
    for sf in session_files():
        if sf["version"] != FORMAT:
            continue
        try:
            L = load(sf["file"])
        except Exception:
            continue
        sid = L["header"].get("id") or sf["dir"].name
        for e in L["events"]:
            if e.get("type") != "assistant/message":
                continue
            d = e.get("data") or {}
            u = d.get("usage")
            if not isinstance(u, dict):
                continue
            src = (d.get("message") or {}).get("source") or {}
            if src.get("provider") == IMPORT_PROVIDER:
                continue
            out.append({"sid": sid, "ts": (e.get("time") or 0) / 1000, "provider": src.get("provider"), "model": src.get("model"),
                        "input": int(u.get("inputTokens") or 0), "output": int(u.get("outputTokens") or 0),
                        "cache_read": int(u.get("cacheReadTokens") or 0), "cache_write": int(u.get("cacheWriteTokens") or 0),
                        "reasoning": int(u.get("reasoningTokens") or 0),
                        "msg": (d.get("message") or {}).get("id") or f"{sid}:{e.get('seq')}", "cwd": L["header"].get("cwd")})
    return out


# ---------- 校验（按 dsh 的规则，离线） ----------

def validate_bytes(raw: bytes, compression: str | None = None, filename: str | None = None) -> dict:
    errs, warns = [], []
    comp = compression or sniff(raw)
    if filename:
        m = LOG_RE.match(filename)
        if not m:
            errs.append(f"文件名 {filename} 不是 dsh 认的名字")
        elif int(m.group(1) or 0) != FORMAT:
            errs.append(f"文件名是第 {m.group(1) or 0} 版")
        elif ("zstd" if m.group(2) else "none") != comp:
            errs.append("文件名的压缩后缀和内容对不上")
    if comp == "zstd":
        try:
            fr = _frames(raw)
            if any(not ck for _, _, ck in fr):
                warns.append("有的帧没有校验和")
            z = _zstd()
            first = z.ZstdDecompressor().decompressobj().decompress(raw[fr[0][0]:fr[0][1]]) if fr else b""
            if first.count(b"\n") != 1 or not first.endswith(b"\n"):
                errs.append("第一帧应该只有头一行")
        except Exception as e:
            errs.append(f"zstd：{e}")
            return {"ok": False, "errors": errs, "warnings": warns}
    try:
        L = load_bytes(raw, comp)
    except Exception as e:
        return {"ok": False, "errors": errs + [str(e)], "warnings": warns}
    h, evs = L["header"], L["events"]
    if not isinstance(h, dict) or h.get("type") != "session":
        errs.append("头一行不是 type: session")
        h = h if isinstance(h, dict) else {}
    else:
        miss = [k for k in HEADER_REQ if k not in h]
        if miss:
            errs.append(f"头缺字段 {miss}")
        extra = [k for k in h if k not in HEADER_KEYS]
        if extra:
            errs.append(f"头里有 dsh 不认的字段 {extra}")
        if h.get("version") != FORMAT:
            errs.append(f"头的格式版本是 {h.get('version')}，不是 {FORMAT}")
        if not (isinstance(h.get("id"), str) and h["id"]):
            errs.append("会话号不对")
        for k in ("createdAt", "delegationDepth"):
            if not (isinstance(h.get(k), int) and not isinstance(h.get(k), bool) and h[k] >= 0):
                errs.append(f"头的 {k} 应该是非负整数")
        if not isinstance(h.get("isSeeded"), bool):
            errs.append("头的 isSeeded 应该是 true/false")
        if "cwd" in h and not (isinstance(h["cwd"], str) and (h["cwd"].startswith("/") or re.match(r"^[A-Za-z]:[\\/]", h["cwd"])
                                                                or h["cwd"].startswith("\\\\"))):
            errs.append("头的 cwd 应该是绝对路径")
        if h.get("origin") not in (None, "subagent"):
            errs.append("头的 origin 只能是 subagent")
    seqs: set[int] = set()
    calls, results = set(), set()
    last_t = 0
    for i, e in enumerate(evs):
        t = e.get("type")
        if e.get("seq") != i:
            errs.append(f"第 {i} 个事件的 seq 是 {e.get('seq')}（应该从 0 连续）")
            break
        if not isinstance(e.get("time"), int) or isinstance(e.get("time"), bool) or e["time"] < 0:
            errs.append(f"seq {i}：time 应该是毫秒整数")
        else:
            if e["time"] < last_t:
                warns.append(f"seq {i}：时间往回走了")
            last_t = max(last_t, e["time"])
        if "data" not in e:
            errs.append(f"seq {i}：缺 data")
        if t not in KNOWN and e.get("ignorable") is not True:
            errs.append(f"seq {i}：事件类型 {t} dsh 不认识，又没标 ignorable（dsh 会拒绝打开）")
        if t in SURFACE:
            op = e.get("surfaceOp")
            if op != "append" and not (isinstance(op, dict) and op.get("op") == "replace" and op.get("startSeq") in seqs
                                       and op.get("endSeq") in seqs and op["startSeq"] <= op["endSeq"]):
                errs.append(f"seq {i}：surfaceOp 不对")
            if isinstance(op, dict):
                src = set(e.get("sourceEventSeqs") or [])
                need = {s for s in seqs if op.get("startSeq", 0) <= s <= op.get("endSeq", -1)}
                if not need <= src:
                    warns.append(f"seq {i}：替换的那段没全列进 sourceEventSeqs")
            seqs.add(i)
            m = _msg(e)
            role = {"user/message": "user", "assistant/message": "assistant", "tool/result": "tool",
                    "system/message": "system", "developer/message": "developer"}[t]
            if m.get("role") != role:
                errs.append(f"seq {i}：消息的 role 应该是 {role}")
            if not (isinstance(m.get("id"), str) and m["id"]):
                errs.append(f"seq {i}：消息缺 id")
            if not isinstance(m.get("content"), list):
                errs.append(f"seq {i}：content 应该是列表")
            src = m.get("source")
            if not (isinstance(src, dict) and isinstance(src.get("kind"), str)):
                errs.append(f"seq {i}：消息缺 source.kind")
            elif t == "assistant/message" and (src.get("kind") != "model" or not src.get("provider") or not src.get("model")):
                errs.append(f"seq {i}：助手消息的 source 应该是 {{kind: model, provider, model}}")
            elif t == "system/message" and src.get("kind") != "system-prompt":
                errs.append(f"seq {i}：系统消息的 source.kind 应该是 system-prompt")
            for b in m.get("content") or []:
                bt = b.get("type") if isinstance(b, dict) else None
                if bt not in BLOCKS:
                    warns.append(f"seq {i}：内容块类型 {bt} 不在 dsh 核心里")
                if bt == "tool-call":
                    if not isinstance(b.get("arguments"), str):
                        errs.append(f"seq {i}：tool-call 的 arguments 应该是 JSON 字符串")
                    calls.add(b.get("id"))
                if bt == "reasoning" and set(b) - {"type", "text"}:
                    errs.append(f"seq {i}：reasoning 块只能有 text")
                if bt == "text" and not isinstance(b.get("text"), str):
                    errs.append(f"seq {i}：text 块缺文字")
            if t == "assistant/message":
                d = e.get("data") or {}
                if not isinstance(d.get("stream"), list):
                    errs.append(f"seq {i}：assistant/message 缺 stream 列表")
                if not (isinstance(d.get("turn"), int) and isinstance(d.get("step"), int)):
                    errs.append(f"seq {i}：assistant/message 缺 turn / step")
            if t == "tool/result":
                results.add(m.get("toolCallId"))
        elif "surfaceOp" in e or "sourceEventSeqs" in e:
            errs.append(f"seq {i}：只有进模型的消息能带 surfaceOp / sourceEventSeqs")
    if results - calls:
        errs.append("有工具结果找不到对应的调用")
    return {"ok": not errs, "errors": errs[:50], "warnings": warns[:50], "events": len(evs), "frames": L["frames"],
            "checksums": L["checksums"], "header": {k: h.get(k) for k in ("version", "isSeeded", "delegationDepth", "origin")}}


def validate(f: Path) -> dict:
    return validate_bytes(f.read_bytes(), "zstd" if f.name.endswith(".zstd") else "none", f.name)


# ---------- 路径编码（照 dsh 的 format.ts：encodeSegment / projectKey） ----------

def _utf16_units(s: str):
    b = s.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(b), 2):
        yield b[i] | b[i + 1] << 8


_SAFE = re.compile(r"^[A-Za-z0-9._-]$")


def encode_segment(raw: str) -> str:
    if not raw:
        raise ValueError("空的路径段")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for u in _utf16_units(raw):
        ch = chr(u) if u < 0x80 else None
        out.append(ch if ch and ch != "~" and _SAFE.match(ch) else "~%04X" % u)
    return "".join(out)


def project_key(cwd: str) -> str:
    if not cwd:
        raise ValueError("空的工作目录")
    out, run = [], False
    for u in _utf16_units(cwd):
        ch = chr(u) if u < 0x80 else None
        if ch in ("/", "\\", ":"):
            if not run:
                out.append("-")
            run = True
        else:
            out.append(ch if ch and ch != "~" and _SAFE.match(ch) else "~%04X" % u)
            run = False
    slug = re.sub(r"^-+", "", "".join(out)) or "root"
    return f"--{slug[:251]}--"


# ---------- 小工具 ----------

def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _iso_ms(ms) -> str:
    try:
        return datetime.fromtimestamp((ms or 0) / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _attachment(ref: dict) -> tuple[bytes | None, str]:
    """dsh 的图片：$DSH_HOME/attachments/v1/objects/<前两位>/<sha256>，sha 对得上才用。"""
    m = re.match(r"^sha256:([a-f0-9]{64})$", str((ref or {}).get("attachmentId") or ""))
    if not m:
        return None, "图片引用不对"
    f = home() / "attachments" / "v1" / "objects" / m.group(1)[:2] / m.group(1)
    try:
        data = f.read_bytes()
    except OSError:
        return None, "图片文件不在了"
    if hashlib.sha256(data).hexdigest() != m.group(1):
        return None, "图片文件校验不对"
    return data, ref.get("mediaType") or "image/png"


# ---------- dsh → Claude Code ----------

def build_claude(L: dict, new_sid: str, version: str, title: str | None = None, reasoning: bool = False,
                 images: str = "placeholder", out_lines: int = 20, out_chars: int = 2000) -> dict:
    """把一个 dsh 会话拼成 Claude Code 会话记录（只算，不写）。先按 surfaceOp 拼出模型看到的那一串；会丢：系统提示 / 运行时上下文 /
    技能目录（不带）、思考（缺省丢，reasoning=True 当文字带上）、工具调用和结果（变成文字摘要，结果截短）、
    图片（缺省占位，images='embed' 内嵌 base64）。"""
    from . import codex_import as CI
    h, evs = L["header"], L["events"]
    if h.get("version") != FORMAT:
        raise DshError(f"第 {h.get('version')} 版格式，只认第 {FORMAT} 版")
    sid = h.get("id") or ""
    cwd = CI.norm_cwd(h.get("cwd")) or str(Path.home())
    if title is None:
        title = next((e["data"].get("title") for e in reversed(evs)
                      if e.get("type") == "session/title" and (e.get("data") or {}).get("title")), None)
    first = next((x["text"] for x in entries(evs) if x["role"] == "user"), "")
    title = title or " ".join(first.split())[:40] or "DeepSeek Harness 对话"
    stats = {"user": 0, "assistant": 0, "tools": 0, "tool_results": 0, "reasoning_dropped": 0, "reasoning_kept": 0,
             "images": 0, "images_embedded": 0, "context_skipped": 0, "system_skipped": 0,
             "replaced": sum(1 for e in evs if isinstance(e.get("surfaceOp"), dict))}
    turns: list[list] = []

    def push(role, part, ts):
        if turns and turns[-1][0] == role:
            turns[-1][1].append(part)
        else:
            turns.append([role, [part], ts])

    first_ts = h.get("createdAt")
    push("user", {"type": "text", "text": f"（以下是从 DeepSeek Harness 导入的对话「{title}」，dsh 会话 {sid}，工作目录 {cwd}。"
                  "dsh 里跑过的工具只留了文字摘要，输出是截短的；接着做之前先看一下文件 / git 的当前状态。）"}, first_ts)
    for e in surface(evs):
        t, ts = e.get("type"), e.get("time")
        m = _msg(e)
        if t in ("system/message", "developer/message"):
            stats["system_skipped"] += 1
            continue
        if t == "user/message":
            if (m.get("source") or {}).get("kind") != "user":
                stats["context_skipped"] += 1
                continue
            stats["user"] += 1
            for b in m.get("content") or []:
                bt = b.get("type") if isinstance(b, dict) else None
                if bt == "text" and b.get("text"):
                    push("user", {"type": "text", "text": b["text"]}, ts)
                elif bt == "image":
                    stats["images"] += 1
                    ref = b.get("attachment") or {}
                    data, mt = _attachment(ref) if images == "embed" else (None, "")
                    if data is not None and mt in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                        stats["images_embedded"] += 1
                        push("user", {"type": "image", "source": {"type": "base64", "media_type": mt,
                                                                  "data": base64.b64encode(data).decode()}}, ts)
                    else:
                        nm = ref.get("name") or "图片"
                        push("user", {"type": "text", "text": f"[图片：{nm}，{ref.get('width', '?')}×{ref.get('height', '?')}，没有带过来]"}, ts)
                elif bt == "file":
                    ref = b.get("attachment") or {}
                    push("user", {"type": "text", "text": f"[附件：{ref.get('name') or '文件'}，{ref.get('bytes', '?')} 字节，没有带过来]"}, ts)
        elif t == "assistant/message":
            stats["assistant"] += 1
            for b in m.get("content") or []:
                bt = b.get("type") if isinstance(b, dict) else None
                if bt == "text" and b.get("text"):
                    push("assistant", {"type": "text", "text": b["text"]}, ts)
                elif bt == "reasoning" and b.get("text"):
                    if reasoning:
                        stats["reasoning_kept"] += 1
                        push("assistant", {"type": "text", "text": "〔DeepSeek Harness 里的思考〕\n" + b["text"]}, ts)
                    else:
                        stats["reasoning_dropped"] += 1
                elif bt == "tool-call":
                    stats["tools"] += 1
                    a = b.get("arguments") or ""
                    try:
                        a = json.dumps(json.loads(a), ensure_ascii=False, indent=1)
                    except Exception:
                        pass
                    push("assistant", {"type": "text", "text": f"〔DeepSeek Harness 调用了 {b.get('name')}〕\n" + CI._fence(CI._clip(a, 30, 3000))}, ts)
        elif t == "tool/result":
            stats["tool_results"] += 1
            s = _txt(m.get("content"))
            if any(isinstance(b, dict) and b.get("type") == "image" for b in m.get("content") or []):
                s += "\n[工具返回了图片，没有带过来]"
            push("assistant", {"type": "text", "text": ("输出（出错）：\n" if m.get("isError") else "输出：\n")
                               + CI._fence(CI._clip(s, out_lines, out_chars))}, ts)
    if turns[-1][0] == "user":
        push("assistant", {"type": "text", "text": "（DeepSeek Harness 那边到这里还没有回复。导入到此为止。）"}, turns[-1][2])
    recs, parent, last_ts = [], None, None
    for role, parts, ts in turns:
        u = str(uuid.uuid4())
        ts = ts or last_ts or first_ts
        last_ts = ts
        r = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": new_sid,
             "version": version, "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": _iso_ms(ts)}
        merged: list[dict] = []
        for p in parts:                       # 相邻的文字拼成一块
            if p["type"] == "text" and merged and merged[-1]["type"] == "text":
                merged[-1] = {"type": "text", "text": merged[-1]["text"] + "\n\n" + p["text"]}
            else:
                merged.append(p)
        if role == "user":
            r["message"] = {"role": "user", "content": merged[0]["text"] if len(merged) == 1 and merged[0]["type"] == "text" else merged}
        else:
            r["message"] = {"id": "msg_dshimport_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": "deepseek-harness-import", "content": merged, "stop_reason": "end_turn", "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        parent = u
    recs.append({"type": "custom-title", "customTitle": TO_CC_PREFIX + title, "sessionId": new_sid})
    return {"records": recs, "cwd": cwd, "title": title, "turns": len(turns), "stats": stats, "dsh_id": sid,
            "chars": sum(len(json.dumps(r, ensure_ascii=False)) for r in recs)}


def build_claude_bytes(data: bytes, new_sid: str, title: str | None = None) -> dict:
    """时间线「另存为新的 Claude Code 会话」用：备份库里的 dsh 那一版 → Claude Code 记录。"""
    from . import codex_import as CI
    return build_claude(load_bytes(data, sniff(data)), new_sid, CI.claude_version(), title)


def _src_key(f: Path) -> list:
    st = f.stat()
    return [st.st_mtime, st.st_size]


def plan_to_claude(only: list[str] | None = None, include_imported: bool = False) -> dict:
    from . import codex_import as CI
    mp = _load(MAP, {})
    rows = []
    keys = [k.strip().lower() for k in (only or []) if k.strip()]
    for sf in session_files():
        s = summary(sf)
        sid = s.get("id") or sf["dir"].name
        if keys and not any(sid.lower() == k or sid.lower().endswith(k) for k in keys):
            continue
        if s.get("error"):
            rows.append({"id": sid, "state": "error", "error": s["error"], "path": str(sf["file"])})
            continue
        if s.get("subagent") and not keys:
            continue                                   # 子代理的会话缺省不导（父会话里有它的结果）
        if not s.get("msgs"):
            rows.append({"id": sid, "state": "empty", "path": str(sf["file"]), "title": s.get("title") or "", "cwd": s.get("cwd")})
            continue                                   # 开了没说话的空会话
        if (s.get("title") or "").startswith(TO_DSH_PREFIX) and not include_imported and not keys:
            continue                                   # 本来就是从 Claude Code 导过去的
        rec = mp.get(f"dsh2cc:{sid}")
        state = "new"
        if rec:
            jf = Path(rec.get("file", ""))
            if not jf.exists():
                state = "new"
            elif rec.get("sha") and _sha(jf) != rec["sha"]:
                state = "diverged"                     # 你已经在 Claude 里接着聊过：不动
            elif rec.get("src") == _src_key(sf["file"]):
                state = "same"
            else:
                state = "update"
        rows.append({"id": sid, "title": s.get("title") or s.get("first") or "", "cwd": s.get("cwd"), "path": str(sf["file"]),
                     "last": s.get("last"), "msgs": s.get("msgs"), "state": state,
                     "session": rec.get("session") if rec and state in ("update", "same", "diverged") else None, "model": s.get("model")})
    try:
        dt = CI._desktop_target(None)
    except Exception:
        dt = None
    return {"ok": True, "rows": rows, "counts": {k: sum(1 for r in rows if r["state"] == k) for k in ("new", "update", "same", "diverged", "error", "empty")},
            "desktop": dt, "dsh_home": str(home()), "claude_home": str(paths.CLAUDE_HOME)}


def _new_ledger(what: str) -> tuple[Path, dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = LEDGERS / stamp
    while d.exists():
        d = d.with_name(d.name + "x")
    (d / "before").mkdir(parents=True)
    return d, {"what": what, "time": datetime.now().isoformat(timespec="seconds"), "created": [], "created_dirs": [], "updated": [],
               "items": [], "map_before": {}, "errors": [], "dsh_home": str(home()), "claude_home": str(paths.CLAUDE_HOME)}


def run_to_claude(only: list[str] | None = None, dry: bool = True, reasoning: bool = False, images: str = "placeholder",
                  desktop: bool = True, target: str | None = None) -> dict:
    """dsh → Claude Code：写 ~/.claude/projects/<目录>/<新会话号>.jsonl，再在桌面端当前账号的列表里登记一条。
    再导一次：dsh 那边有新内容 → 原地更新那份（你没在 Claude 里接着聊过的话）；没变 → 跳过；接着聊过 → 不动。"""
    from . import codex_import as CI
    p = plan_to_claude(only)
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
                b = build_claude(load(Path(r["path"])), r.get("session") or "00000000-0000-0000-0000-000000000000", ver,
                                 reasoning=reasoning, images=images)
                r.update(turns=b["turns"], chars=b["chars"], stats=b["stats"], title=b["title"],
                         target=str(paths.CLAUDE_HOME / "projects" / CI.project_slug(b["cwd"])))
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
        return dict(p, dry=True, done=0)
    mp = _load(MAP, {})
    tdir = Path(p["desktop"]["path"]) if desktop and p.get("desktop") else None
    led_dir, led = _new_ledger("dsh-to-claude")
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
        save_led()                                    # 先记账再写：中途断了也撤得回
        tmp = f.with_name(f.name + ".cc-sessionsync.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, f)

    done = 0
    for r in todo:
        k = f"dsh2cc:{r['id']}"
        rec = dict(mp.get(k) or {})
        led["map_before"][k] = mp.get(k)
        try:
            src = Path(r["path"])
            new = rec.get("session") if r["state"] == "update" and rec.get("session") else str(uuid.uuid4())
            b = build_claude(load(src), new, ver, reasoning=reasoning, images=images)
            jf = paths.CLAUDE_HOME / "projects" / CI.project_slug(b["cwd"]) / f"{new}.jsonl"
            write(jf, CI._dump(b["records"]))
            ent = None
            if tdir is not None:
                cur = None
                old = rec.get("local") if r["state"] == "update" and rec.get("folder") == p["desktop"]["key"] else None
                if old and (tdir / f"{old}.json").exists():
                    cur = _load(tdir / f"{old}.json", {})
                d = CI._desktop_entry(tdir, new, b["cwd"], b["title"], src.stat().st_mtime, cur, prefix=TO_CC_PREFIX)
                ent = d["sessionId"]
                write(tdir / f"{ent}.json", json.dumps(d, ensure_ascii=False, indent=1))
            mp[k] = {"session": new, "file": str(jf), "sha": _sha(jf), "src": _src_key(src), "local": ent or rec.get("local"),
                     "folder": p["desktop"]["key"] if ent else rec.get("folder"), "title": b["title"], "cwd": b["cwd"],
                     "imported": time.time(), "ledger": led_dir.name}
            led["items"].append({"dsh": r["id"], "session": new, "title": b["title"], "state": r["state"], "file": str(jf),
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


# ---------- Claude Code → dsh（只新建） ----------

SKIP_USER = ("<command-name>", "<local-command-stdout>", "<local-command-caveat>", "<system-reminder>", "Caveat: The messages below")


def _cc_parts(c) -> tuple[list[str], int, list[tuple[str, str]], list[str]]:
    """Claude 的 content → (文字段, 图片数, [(工具名, 参数)], [工具结果文字])"""
    if isinstance(c, str):
        return [c], 0, [], []
    texts, imgs, tools, results = [], 0, [], []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            texts.append(b["text"])
        elif t == "image":
            imgs += 1
        elif t == "tool_use":
            tools.append((b.get("name") or "tool", json.dumps(b.get("input") or {}, ensure_ascii=False, indent=1)))
        elif t == "tool_result":
            rc = b.get("content")
            if isinstance(rc, list):
                rc = "\n".join(x.get("text", "") for x in rc if isinstance(x, dict) and x.get("type") == "text")
            results.append(("（出错）" if b.get("is_error") else "") + str(rc or ""))
    return texts, imgs, tools, results


def _ms(ts) -> int | None:
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def build_dsh(cc_file: Path, title: str | None = None, out_lines: int = 20, out_chars: int = 2000) -> dict:
    """把一个 Claude Code 会话拼成 dsh 第 4 版会话（只算，不写）：每一轮 = turn/start、step/start、user/message、
    assistant/message、step/end、turn/end，全是 'append'。只带文字：工具调用和结果变成助手消息里的文字摘要（结果截短），
    思考丢掉，图片占位，系统提示 / 子代理 / 命令行内部消息不带。"""
    from . import codex_import as CI
    recs = []
    with open(cc_file, "rb") as fh:
        for ln in fh:
            try:
                recs.append(json.loads(ln))
            except Exception:
                continue
    cwd, ctitle, csid = None, None, None
    turns: list[list] = []                     # [role, [文字], ts 毫秒, model]
    stats = {"user": 0, "assistant": 0, "tools": 0, "tool_results": 0, "thinking_dropped": 0, "images": 0, "skipped": 0}

    def push(role, text, ts, model=None):
        if turns and turns[-1][0] == role:
            turns[-1][1].append(text)
            turns[-1][3] = turns[-1][3] or model
        else:
            turns.append([role, [text], ts, model])

    for d in recs:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "custom-title":
            ctitle = d.get("customTitle") or ctitle
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        cwd = cwd or d.get("cwd")
        csid = csid or d.get("sessionId")
        ts = _ms(d.get("timestamp"))
        m = d.get("message") or {}
        texts, imgs, tools, results = _cc_parts(m.get("content"))
        if isinstance(m.get("content"), list):
            stats["thinking_dropped"] += sum(1 for b in m["content"] if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
        if d["type"] == "user":
            for r in results:
                stats["tool_results"] += 1
                push("assistant", "输出：\n" + CI._fence(CI._clip(r, out_lines, out_chars)), ts)
            texts = [t for t in texts if not any(t.lstrip().startswith(x) for x in SKIP_USER)]
            if not texts and not imgs:
                if not results:
                    stats["skipped"] += 1
                continue
            stats["user"] += 1
            stats["images"] += imgs
            push("user", "\n\n".join(texts + ([f"[图片 ×{imgs}，没有带过来]"] if imgs else [])), ts)
        else:
            for t in texts:
                push("assistant", t, ts, m.get("model"))
            for name, args in tools:
                stats["tools"] += 1
                push("assistant", f"〔Claude Code 调用了 {name}〕\n" + CI._fence(CI._clip(args, 30, 3000)), ts, m.get("model"))
            if texts or tools:
                stats["assistant"] += 1
    if not any(t[0] == "user" for t in turns):
        raise DshError("这个 Claude Code 会话里没有你说的话")
    while turns and turns[0][0] == "assistant":          # 开头的助手段并到第一轮里
        a = turns.pop(0)
        turns[0][1] = ["（之前）\n" + "\n\n".join(a[1])] + turns[0][1]
    cwd = CI.norm_cwd(cwd) or None
    if cwd and not (cwd.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", cwd) or cwd.startswith("\\\\")):
        cwd = None
    base_title = (ctitle or "").removeprefix("（Codex）").removeprefix(TO_CC_PREFIX).strip()
    title = (title or base_title or " ".join(turns[0][1][0].split())[:40] or "Claude Code 对话").strip()
    now = int(time.time() * 1000)
    created = turns[0][2] or now
    sid = f"session-{uuid.uuid4()}"
    header = {"type": "session", "version": FORMAT, "id": sid, "createdAt": int(created)}
    if cwd:
        header["cwd"] = cwd
    header.update(isSeeded=False, delegationDepth=0)
    evs: list[dict] = []
    clock = [int(created)]

    def add(t, data, surf=False, ts=None):
        clock[0] = max(clock[0], int(ts or clock[0]))
        e = {"type": t, "seq": len(evs), "time": clock[0], "data": data}
        if surf:
            e["surfaceOp"] = "append"
        evs.append(e)
        return e["seq"]

    note = (f"（以下是从 Claude Code 导入的对话「{title}」，Claude Code 会话 {csid or cc_file.stem}"
            + (f"，工作目录 {cwd}" if cwd else "")
            + "。Claude Code 里跑过的工具只留了文字摘要，输出是截短的；接着做之前先看一下文件 / git 的当前状态。）\n\n")
    turn, i, titled = 0, 0, False
    while i < len(turns):
        role, parts, ts, _ = turns[i]
        if role != "user":
            i += 1
            continue
        turn += 1
        u_text = ("\n\n".join(parts))
        if turn == 1:
            u_text = note + u_text
        a = turns[i + 1] if i + 1 < len(turns) and turns[i + 1][0] == "assistant" else None
        add("turn/start", {"turn": turn}, ts=ts)
        add("step/start", {"turn": turn, "step": 1}, ts=ts)
        if turn == 1:                                    # 第 0 个节点放一条空的系统提示：dsh 接着聊时把它换成自己的（不然系统提示会排到历史后面）
            add("system/message", {"turn": 1, "step": 1, "message": {"role": "system", "id": str(uuid.uuid4()), "content": [],
                                                                     "source": {"kind": "system-prompt"}}}, True, ts)
        us = add("user/message", {"role": "user", "id": str(uuid.uuid4()), "content": [{"type": "text", "text": u_text}],
                                  "source": {"kind": "user"}}, True, ts)
        if not titled:
            titled = True
            add("session/title", {"title": TO_DSH_PREFIX + title, "messageSeqs": [us], "source": {"kind": "fallback"}}, ts=ts)
        a_text = "\n\n".join(a[1]) if a else "（Claude Code 那边到这里还没有回复。导入到此为止。）"
        add("assistant/message", {"turn": turn, "step": 1,
                                  "message": {"role": "assistant", "id": str(uuid.uuid4()), "content": [{"type": "text", "text": a_text}],
                                              "source": {"kind": "model", "provider": IMPORT_PROVIDER, "model": (a[3] if a else None) or "claude"}},
                                  "stream": []}, True, a[2] if a else ts)
        add("step/end", {"turn": turn, "step": 1})
        add("turn/end", {"turn": turn, "reason": {"kind": "completed"}})
        i += 2 if a else 1
    return {"header": header, "events": evs, "id": sid, "cwd": cwd, "title": title, "turns": turn, "stats": stats,
            "claude": csid or cc_file.stem}


def encode_session(header: dict, events: list[dict], compression: str) -> bytes:
    """和 dsh 新建会话时一样：头一行单独一帧，第一批事件一帧（不压缩时就是纯文本）。"""
    h = json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n"
    body = ("\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in events) + "\n") if events else ""
    if compression == "none":
        return (h + body).encode("utf-8")
    return encode_frames([h] + ([body] if body else []))


def write_session(root: Path, compression: str, header: dict, events: list[dict]) -> Path:
    """新建一个 dsh 会话目录：先占住目录名（已经有就失败，绝不覆盖、绝不往已有的会话里写），在里面写临时文件、落盘，再改成正式名字。"""
    if header.get("version") != FORMAT:
        raise DshError(f"只写第 {FORMAT} 版")
    if compression not in ("zstd", "none"):
        raise DshError(f"不认识的压缩方式 {compression}")
    have = root_encoding(root) if root.is_dir() else None
    if have and have != compression:
        raise DshError(f"会话根里现有的会话是 {have}，和设置的 {compression} 不一样，不写")
    name = f"session.v{FORMAT}.jsonl" + (".zstd" if compression == "zstd" else "")
    data = encode_session(header, events, compression)
    v = validate_bytes(data, compression, name)
    if not v["ok"]:
        raise DshError("拼出来的会话没通过校验：" + "；".join(v["errors"][:3]))
    seg = encode_segment(header["id"])
    if any(sf["dir"].name == seg for sf in session_files()):
        raise DshError(f"会话号 {header['id']} 已经有了")
    proj = root / (project_key(header["cwd"]) if header.get("cwd") else "_no-cwd")
    proj.mkdir(parents=True, exist_ok=True)
    d = proj / seg
    d.mkdir()                                    # 已经有就抛 FileExistsError
    final = d / name
    tmp = d / (name + ".ccss-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if final.exists():
            raise DshError("目标文件已经有了")
        os.replace(tmp, final)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
            d.rmdir()
        except OSError:
            pass
        raise
    return final


def write_root() -> dict:
    """写到哪个根：缺省根（profile 改过的话用改过的；算不出来的不用）。"""
    rs = roots()
    for r in rs[1:] + rs[:1]:
        if r["root"] is not None and not (r.get("note") or "").startswith("不认识"):
            return r
    raise DshError("算不出 dsh 的会话根")


def plan_to_dsh(only: list[str] | None = None, again: bool = False) -> dict:
    from . import sessions as S
    mp = _load(MAP, {})
    mine = {v.get("session") for k, v in mp.items() if k.startswith("dsh2cc:")}
    keys = [k.strip().lower() for k in (only or []) if k.strip()]
    rows = []
    for x in S.claude_sessions():
        if x.get("orphan") or not x.get("file"):
            continue
        if keys and not any(x["id"].lower() == k or x["id"].lower().endswith(k) for k in keys):
            continue
        if not keys and (x["id"] in mine or (x.get("tool_title") or "").startswith(TO_CC_PREFIX)):
            continue                                    # 本来就是从 dsh 导过来的
        f = Path(x["file"])
        rec = mp.get(f"cc2dsh:{x['id']}")
        state = "new"
        if rec and Path(rec.get("file", "")).exists():
            if rec.get("src_sha") == _sha(f):
                state = "same"
            else:
                state = "new" if again else "changed"  # Claude 那边又聊了：已经导过去的那份不改；要的话再导一份新的（again）
        rows.append({"id": x["id"], "title": x.get("title"), "cwd": x.get("cwd"), "path": str(f), "last": x.get("last"),
                     "state": state, "dsh": rec.get("dsh") if rec else None})
    err = None
    try:
        wr = write_root()
        root, comp = wr["root"], wr["compression"]
        have = root_encoding(root) if root.is_dir() else None
        if have and have != comp:
            err = f"会话根里现有的会话是 {have}，设置里是 {comp}"
    except DshError as e:
        root, comp, err = None, None, str(e)
    if comp == "zstd" and not _zstd_ok():
        err = "缺 zstandard 模块，写不了压缩的 dsh 会话"
    return {"ok": not err, "error": err, "rows": rows, "root": str(root) if root else None, "compression": comp,
            "counts": {k: sum(1 for r in rows if r["state"] == k) for k in ("new", "same", "changed")}, "running": bool(procs())}


def run_to_dsh(only: list[str] | None = None, dry: bool = True, again: bool = False) -> dict:
    """Claude Code → dsh：每个会话新建一个 dsh 会话目录（新会话号），已有的 dsh 会话一个字节都不碰。"""
    p = plan_to_dsh(only, again)
    todo = [r for r in p["rows"] if r["state"] == "new"]
    if dry or not todo or not p["ok"]:
        for r in todo:
            try:
                b = build_dsh(Path(r["path"]))
                v = validate_bytes(encode_session(b["header"], b["events"], p["compression"] or "none"), p["compression"] or "none")
                r.update(turns=b["turns"], stats=b["stats"], events=len(b["events"]), valid=v["ok"], problems=v["errors"][:3],
                         target=str(Path(p["root"] or "") / (project_key(b["cwd"]) if b["cwd"] else "_no-cwd")))
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"
        return dict(p, dry=bool(dry or not todo), done=0)
    mp = _load(MAP, {})
    led_dir, led = _new_ledger("claude-to-dsh")
    led["root"] = p["root"]
    done = 0
    for r in todo:
        k = f"cc2dsh:{r['id']}"
        led["map_before"][k] = mp.get(k)
        try:
            src = Path(r["path"])
            src_sha = _sha(src)
            b = build_dsh(src)
            f = write_session(Path(p["root"]), p["compression"], b["header"], b["events"])
            sha = _sha(f)
            led["created_dirs"].append({"dir": str(f.parent), "file": str(f), "sha": sha, "id": b["id"]})
            mp[k] = {"dsh": b["id"], "file": str(f), "sha": sha, "src_sha": src_sha, "title": b["title"], "cwd": b["cwd"],
                     "imported": time.time(), "ledger": led_dir.name}
            led["items"].append({"claude": r["id"], "dsh": b["id"], "title": b["title"], "file": str(f), "turns": b["turns"], "stats": b["stats"]})
            r.update(dsh=b["id"], file=str(f), turns=b["turns"], stats=b["stats"], resume=resume_cmd(b["cwd"], b["id"]))
            done += 1
        except Exception as e:
            led["errors"].append(f"{r['id']}：{type(e).__name__}: {e}")
            r["error"] = f"{type(e).__name__}: {e}"
        _save(led_dir / "ledger.json", led)
    _save(MAP, mp)
    _save(led_dir / "ledger.json", led)
    return dict(p, dry=False, done=done, errors=led["errors"], ledger=led_dir.name, ledger_path=str(led_dir))


# ---------- 账本 / 撤回 ----------

def ledgers() -> list[dict]:
    out = []
    if not LEDGERS.is_dir():
        return out
    for d in sorted(LEDGERS.iterdir(), reverse=True):
        led = _load(d / "ledger.json", None)
        if isinstance(led, dict):
            out.append({"id": d.name, "time": led.get("time"), "what": led.get("what"), "items": len(led.get("items", [])),
                        "created": len(led.get("created", [])) + len(led.get("created_dirs", [])), "updated": len(led.get("updated", [])),
                        "undone": led.get("undone")})
    return out


def locked(d: Path) -> bool:
    """dsh 正开着这个会话没有：试着独占它的 session.lock（不在 = 没开）。只试一下马上放开，不改文件。"""
    f = d / "session.lock"
    if not f.exists():
        return False
    try:
        if os.name == "nt":
            import msvcrt
            fd = os.open(str(f), os.O_RDONLY)      # dsh 用不共享的句柄占着时，这里就打不开
            try:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass                           # 空文件锁不了字节；能打开就说明没被独占
            finally:
                os.close(fd)
            return False
        import fcntl
        fd = os.open(str(f), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False
    except OSError:
        return True


def undo(ledger_id: str) -> dict:
    """撤回一次导入：新建的文件 / dsh 会话目录挪进回收站（不直接删），更新过的换回原来的。
    导过去之后又接着聊过的（sha 变了）、dsh 正开着的、目录里多了别的东西的，不动，列出来。"""
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
    trash = paths.HOME / "trash" / f"dsh-import-undo-{datetime.now():%Y%m%d-%H%M%S}"
    moved, restored, kept = 0, 0, []
    for f in led.get("created", []):
        fp = Path(f)
        if not fp.exists():
            continue
        if fp.suffix == ".jsonl" and final.get(f) and _sha(fp) != final[f]:
            kept.append(f)
            continue
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(fp), str(trash / f"{moved}-{fp.name}"))
        moved += 1
    for u in led.get("updated", []):
        fp, src = Path(u["file"]), d / "before" / u["before"]
        if fp.suffix == ".jsonl" and fp.exists() and final.get(u["file"]) and _sha(fp) != final[u["file"]]:
            kept.append(u["file"])
            continue
        if src.exists():
            if fp.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, trash / f"replaced-{restored}-{fp.name}")
            shutil.copy2(src, fp)
            restored += 1
    for c in led.get("created_dirs", []):
        dp, fp = Path(c["dir"]), Path(c["file"])
        if not dp.exists():
            continue
        if not fp.exists() or _sha(fp) != c.get("sha") or locked(dp):
            kept.append(str(dp))                   # 在 dsh 里接着聊过 / 正开着：不动
            continue
        if [x.name for x in dp.iterdir() if x.name not in (fp.name, "session.lock")]:
            kept.append(str(dp))
            continue
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dp), str(trash / f"dsh-{moved}-{dp.name}"))
        moved += 1
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

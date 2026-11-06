"""Google Antigravity（agentic IDE / CLI / 独立应用）会话：只读列表、对话、用量；转换目标只导出上下文文件。

存储与读法主要对照 CC Switch（MIT，https://github.com/farion1231/cc-switch @ 4e13413）：
- 会话发现：`~/.gemini/{antigravity-cli,antigravity,antigravity-ide}/brain/<id>/.system_generated/logs/transcript_full.jsonl`
  （见 `session_manager/providers/agy.rs` 的 `session_sources` / `scan_root`；同布局也用于 `--gemini_dir` 私有目录）。
  索引可选读同根 `conversation_summaries.db`（title / workspace_uris / preview）。
- 对话内容：只读上述 transcript；同 step_index 多行时**最后一行胜出**（agy 会重写未完成步骤）。
  USER_INPUT 的正文在 `<USER_REQUEST>…</USER_REQUEST>` 里；PLANNER_RESPONSE = 助手。
- Token 用量：transcript 里 PLANNER_RESPONSE（status=DONE）自带的
  `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens`（及可选 reasoning_tokens）
  —— 与 CC Switch `session_usage_agy.rs` 一致；**input 不含 cache，output 已含 thinking，不要再加减**。
  原生 `conversations/<id>.db` 的 `gen_metadata` 只用来补模型名（field 21 档位 ID，否则 field 19）和计时，不作为用量主来源。
- 旧 `.pb` / 无 transcript 的会话：只在 summaries 里列出，不编造对话或 token（agy.rs：「binary-only … no readable transcript」）。
- **从不写** Antigravity 的库或 transcript（agy.rs 模块头：never open its binary conversation store for writing）。

社区补充：IDE 新路径 `antigravity-ide`；VS Code 壳 `%APPDATA%/Antigravity IDE/User/globalStorage/state.vscdb` 只有侧栏索引，不含 token。
逻辑移植自 CC Switch，未复制其 Rust 源码；字段约定与测试夹具形状与之对齐。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from . import paths

NAME = "Antigravity"
NORM = "ccss-antigravity/1"
TO_CC_PREFIX = "（Antigravity）"
MIRROR = paths.HOME / "antigravity-mirror"
EXPORTS = paths.HOME / "exports" / "antigravity"
PRODUCTS = ("antigravity-cli", "antigravity", "antigravity-ide")  # 与 CC Switch session_sources 一致；不去 backup
# 社区实测的占位模型 → 价格表里有的名字（估；占位可能被以后的安装改指）
MODEL_ALIAS = {
    "gemini-pro-default": "gemini-2.5-pro",
    "gemini-3-flash-a": "gemini-3-flash-preview",
    "gemini-3-flash-b": "gemini-3-flash-preview",
    "gemini-3-flash-c": "gemini-3-flash-preview",
    "gemini-3.1-pro-high": "gemini-3.1-pro-preview",
    "gemini-3.1-pro-low": "gemini-3.1-pro-preview",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gemini-3.5-flash-high": "gemini-3.5-flash",
    "gemini-3.6-flash": "gemini-3.6-flash",
}


class AntigravityError(Exception):
    pass


# ---------- protobuf 最小读取（无额外依赖；字段号见模块头）----------

def _varint(buf: bytes, i: int) -> tuple[int, int] | None:
    v = 0
    for n in range(10):
        if i >= len(buf):
            return None
        b = buf[i]; i += 1
        v |= (b & 0x7F) << (7 * n)
        if not (b & 0x80):
            return v, i
    return None


def _fields(buf: bytes):
    """产出 (field_no, wire, value_int_or_bytes)。坏数据停住，不抛。"""
    i = 0
    while i < len(buf):
        t = _varint(buf, i)
        if not t:
            return
        tag, i = t
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            return
        if wt == 0:
            r = _varint(buf, i)
            if not r:
                return
            v, i = r
            yield fn, 0, v
        elif wt == 2:
            r = _varint(buf, i)
            if not r:
                return
            n, i = r
            if i + n > len(buf):
                return
            yield fn, 2, buf[i:i + n]; i += n
        elif wt == 1:
            if i + 8 > len(buf):
                return
            i += 8
        elif wt == 5:
            if i + 4 > len(buf):
                return
            i += 4
        else:
            return


def _text(b: bytes) -> str | None:
    try:
        s = b.decode("utf-8").strip()
    except Exception:
        return None
    return s or None


def _ts_pb(b: bytes) -> float | None:
    sec = nano = 0
    for fn, wt, v in _fields(b):
        if fn == 1 and wt == 0:
            sec = int(v)
        elif fn == 2 and wt == 0 and int(v) < 1_000_000_000:
            nano = int(v)
    if sec <= 0:
        return None
    return sec + nano / 1e9


def _usage_stats(b: bytes) -> dict:
    u = {"inp": 0, "out": 0, "cw": 0, "cr": 0, "think": 0, "resp": 0, "rid": None, "mid": None}
    for fn, wt, v in _fields(b):
        if wt == 0:
            if fn == 2: u["inp"] = int(v)
            elif fn == 3: u["out"] = int(v)
            elif fn == 4: u["cw"] = int(v)
            elif fn == 5: u["cr"] = int(v)
            elif fn == 9: u["think"] = int(v)
            elif fn == 10: u["resp"] = int(v)
        elif wt == 2:
            if fn == 11: u["rid"] = _text(v)
            elif fn == 7: u["mid"] = _text(v)
            elif fn == 12 and not u["rid"]: u["rid"] = _text(v)
    if not u["out"] and (u["think"] or u["resp"]):
        u["out"] = u["think"] + u["resp"]
    return u


def _chat_model_meta(b: bytes) -> dict | None:
    usages, model, ts = [], None, None
    for fn, wt, v in _fields(b):
        if wt != 2:
            continue
        if fn == 4:
            usages.append(_usage_stats(v))
        elif fn == 17:  # RetryInfo → usage at #2
            for f2, w2, v2 in _fields(v):
                if f2 == 2 and w2 == 2:
                    usages.append(_usage_stats(v2))
        elif fn == 19:
            model = _text(v)
        elif fn == 9:
            for f2, w2, v2 in _fields(v):
                if f2 == 4 and w2 == 2:
                    ts = _ts_pb(v2)
    if not model and not any(u.get("rid") or u.get("inp") or u.get("out") or u.get("cr") for u in usages):
        return None
    return {"model": model, "ts": ts, "usages": usages}


def parse_gen_metadata(blob: bytes) -> list[dict]:
    """gen_metadata.data → [{model, ts, usages:[…]}]。外壳字段号不写死：每个 length-delimited 子消息都试。"""
    out = []
    for _, wt, v in _fields(blob):
        if wt != 2:
            continue
        g = _chat_model_meta(v)
        if g and (g["model"] or g["usages"]):
            out.append(g)
    return out


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F; n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def enc_field_varint(fn: int, v: int) -> bytes:
    return encode_varint(fn << 3) + encode_varint(v)


def enc_field_bytes(fn: int, b: bytes) -> bytes:
    return encode_varint((fn << 3) | 2) + encode_varint(len(b)) + b


def enc_usage(inp=0, out=0, cr=0, cw=0, think=0, resp=0, rid="r1", model_id=1) -> bytes:
    parts = [enc_field_varint(1, model_id), enc_field_varint(2, inp), enc_field_varint(3, out),
             enc_field_varint(4, cw), enc_field_varint(5, cr)]
    if think: parts.append(enc_field_varint(9, think))
    if resp: parts.append(enc_field_varint(10, resp))
    if rid: parts.append(enc_field_bytes(11, rid.encode()))
    return b"".join(parts)


def enc_gen_meta(model: str, usage: bytes, ts: float | None = None) -> bytes:
    chat = enc_field_bytes(4, usage) + enc_field_bytes(19, model.encode())
    if ts:
        stamp = enc_field_varint(1, int(ts)) + enc_field_varint(2, int((ts % 1) * 1e9))
        chat += enc_field_bytes(9, enc_field_bytes(4, stamp))
    return enc_field_bytes(1, chat)  # wrapper field 1 = ChatModelMetadata


# ---------- 目录 ----------

def _setting(k: str) -> str | None:
    try:
        from . import settings
        v = settings.get(k)
        return v if isinstance(v, str) and v.strip() else None
    except Exception:
        return None


def gemini_home() -> Path:
    e = os.environ.get("ANTIGRAVITY_HOME") or os.environ.get("GEMINI_HOME") or _setting("antigravity_dir")
    if e:
        p = Path(e).expanduser()
        # 若用户指到具体产品根（…/antigravity-ide），上一级当 gemini home
        if p.name in PRODUCTS or p.name == "antigravity-backup":
            return p.parent
        return p
    return Path.home() / ".gemini"


def product_roots() -> list[tuple[str, Path]]:
    """[(product_key, root_dir)]，只返回存在的。"""
    h = gemini_home()
    out = []
    for name in PRODUCTS:
        d = h / name
        if d.is_dir():
            out.append((name, d))
    # 设置直接指到某个产品根时也认
    e = os.environ.get("ANTIGRAVITY_HOME") or _setting("antigravity_dir")
    if e:
        p = Path(e).expanduser()
        if p.is_dir() and p.name in PRODUCTS and not any(r == p for _, r in out):
            out.insert(0, (p.name, p))
    return out


def ide_user_dirs() -> list[Path]:
    """VS Code 壳的 User 目录（globalStorage/state.vscdb），只做探测，不读全文。"""
    names = ["Antigravity IDE", "Antigravity", "antigravity"]
    cands = []
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        cands = [base / n / "User" for n in names]
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        cands = [base / n / "User" for n in names]
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        cands = [base / n / "User" for n in names]
    return [p for p in cands if p.is_dir()]



def _title_from_brain(root: Path, sid: str) -> str | None:
    for name in ("task.md", "walkthrough.md", "implementation_plan.md"):
        f = root / "brain" / sid / name
        if f.is_file():
            try:
                t = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            except Exception:
                continue
            for ln in t:
                ln = ln.strip().lstrip("#").strip()
                if ln:
                    return ln[:120]
    return None


def _open_ro(db: Path) -> sqlite3.Connection | None:
    """只读打开；Antigravity 开着时用 mode=ro（含 WAL），关着用 immutable。"""
    try:
        uri = db.resolve().as_uri()
        try:
            return sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=2)
        except sqlite3.OperationalError:
            return sqlite3.connect(f"{uri}?immutable=1", uri=True, timeout=2)
    except Exception:
        return None


# ---------- 发现会话 ----------

def _transcript_candidates(logs: Path) -> Path | None:
    """Prefer transcript_full.jsonl (CC Switch), else the larger of the two."""
    a, b = logs / "transcript_full.jsonl", logs / "transcript.jsonl"
    cands = [x for x in (a, b) if x.is_file()]
    if not cands:
        return None
    # Prefer full when both exist and full is non-empty; else larger
    if a in cands and a.stat().st_size > 0:
        return a
    return max(cands, key=lambda x: x.stat().st_size)


def _valid_sid(sid: str) -> bool:
    return bool(sid) and all(c.isalnum() or c == "-" for c in sid)


def _read_summaries(root: Path) -> dict[str, dict]:
    """Optional conversation_summaries.db (CC Switch agy.rs read_summaries)."""
    db = root / "conversation_summaries.db"
    if not db.is_file():
        return {}
    c = _open_ro(db)
    if not c:
        return {}
    out = {}
    try:
        rows = c.execute(
            "SELECT conversation_id, title, workspace_uris, preview, last_modified_time FROM conversation_summaries"
        ).fetchall()
    except Exception:
        c.close(); return {}
    for cid, title, uris, preview, modified in rows:
        if not _valid_sid(cid):
            continue
        cwd = None
        try:
            import json as _j, urllib.parse
            arr = _j.loads(uris or "[]")
            if arr:
                u = urllib.parse.urlparse(arr[0])
                if u.scheme == "file":
                    cwd = urllib.parse.unquote(u.path)
                    if os.name == "nt" and cwd.startswith("/") and len(cwd) > 2 and cwd[2] == ":":
                        cwd = cwd[1:]
        except Exception:
            pass
        out[cid] = {"title": title or "", "cwd": cwd, "preview": preview or "", "modified": modified or ""}
    c.close()
    return out


def _scan_brain(prod: str, root: Path) -> list[dict]:
    """Scan brain/<id>/…/transcript_*.jsonl — primary discovery (CC Switch scan_root)."""
    brain = root / "brain"
    summaries = _read_summaries(root)
    out = []
    if brain.is_dir():
        try:
            entries = list(brain.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            sid = entry.name
            if not _valid_sid(sid):
                continue
            logs = entry / ".system_generated" / "logs"
            tr = _transcript_candidates(logs)
            if not tr:
                continue
            # Refuse symlink escape (CC Switch unix test)
            try:
                if not tr.resolve().is_relative_to(brain.resolve()):
                    continue
            except Exception:
                try:
                    if str(brain.resolve()) not in str(tr.resolve()):
                        continue
                except Exception:
                    continue
            st = tr.stat()
            sm = summaries.pop(sid, {})
            title = sm.get("title") or _title_from_brain(root, sid) or sid[:8] + "…"
            db = root / "conversations" / f"{sid}.db"
            out.append({
                "id": sid, "product": prod, "root": str(root), "file": str(db) if db.is_file() else None,
                "format": "db" if db.is_file() else ("pb" if (root / "conversations" / f"{sid}.pb").is_file() else "transcript"),
                "transcript": str(tr), "title": title, "cwd": sm.get("cwd"),
                "mtime": st.st_mtime, "size": st.st_size, "readable": True, "opaque": False,
            })
    # Index-only (binary / no transcript): list without claiming readable content
    for sid, sm in summaries.items():
        if any(x["id"] == sid for x in out):
            continue
        db = root / "conversations" / f"{sid}.db"
        pb = root / "conversations" / f"{sid}.pb"
        native = db if db.is_file() else pb if pb.is_file() else None
        out.append({
            "id": sid, "product": prod, "root": str(root), "file": str(native) if native else None,
            "format": "db" if native and native.suffix == ".db" else ("pb" if native else "index"),
            "transcript": None, "title": sm.get("title") or sid[:8] + "…", "cwd": sm.get("cwd"),
            "mtime": 0.0, "size": 0, "readable": False, "opaque": True,
            "note": "只有索引、没有可读 transcript（CC Switch：binary-only，不编造对话）",
        })
    return out


def discover() -> list[dict]:
    """本机所有产品根下的会话（按 id 去重；先出现的根优先，但「仅索引」让位给有 transcript 的根）。"""
    seen: dict[str, int] = {}
    out: list[dict] = []
    for prod, root in product_roots():
        for info in _scan_brain(prod, root):
            sid = info["id"]
            if sid not in seen:
                seen[sid] = len(out)
                out.append(info)
            else:
                i = seen[sid]
                # transcript beats index-only
                if out[i].get("opaque") and not info.get("opaque"):
                    out[i] = info
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return out



# ---------- 读对话 → 中间条目 ----------

_USER_REQ = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.S)
_SKIP_TYPES = {"CONVERSATION_HISTORY", "CHECKPOINT", "SYSTEM_MESSAGE", "GENERIC", "EPHEMERAL_MESSAGE"}


def _unwrap_user(content: str) -> str:
    m = _USER_REQ.search(content or "")
    return (m.group(1).strip() if m else (content or "")).strip()


def _ts_ms(s) -> int | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s if s > 1e11 else s * 1000)
    try:
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _ts_s(s) -> float | None:
    """UI / list / chat 用秒（与 Cursor / OpenCode 一致）；用量扫描另用 _ts_ms。"""
    ms = _ts_ms(s)
    return (ms / 1000.0) if ms is not None else None



def records(path: Path) -> list[dict]:
    """Load transcript; same step_index → last line wins (CC Switch agy::records)."""
    steps = {}
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            idx = o.get("step_index")
            if idx is None:
                continue
            try:
                steps[int(idx)] = o
            except (TypeError, ValueError):
                continue
    except Exception:
        return []
    return [steps[k] for k in sorted(steps)]


def model_from_settings_change(content: str) -> str | None:
    """Parse `changed setting Model Selection from X to Y` (CC Switch session_usage_agy)."""
    start = content.find("Model Selection")
    if start < 0:
        return None
    rest = content[start:]
    to = rest.find(" to ")
    if to < 0:
        return None
    tail = rest[to + 4:]
    end = len(tail)
    for sep in (". ", "\n", "`"):
        i = tail.find(sep)
        if i >= 0:
            end = min(end, i)
    model = tail[:end].strip().strip(".").strip()
    if not model or model.lower() == "none":
        return None
    return model


def model_id_of(display: str) -> str:
    """Display name → id: `Gemini 3.8 Flash (High)` → `gemini-3.8-flash-high` (CC Switch)."""
    if " " not in display:
        return display
    s = display.lower().replace(" (", "-").replace("(", "").replace(")", "")
    return "-".join(s.split())


def _items_from_transcript(path: Path) -> tuple[list[dict], dict]:
    """Transcript → IR items. Tokens stay on PLANNER_RESPONSE rows for usage scan."""
    items, created, updated, model = [], None, None, None
    pending_tools: dict[str, dict] = {}
    for o in records(path):
        typ = o.get("type") or ""
        if typ in _SKIP_TYPES:
            continue
        ts = _ts_s(o.get("created_at"))
        if ts:
            created = created or ts
            updated = ts
        src = o.get("source") or ""
        content = o.get("content") or ""
        if typ == "USER_INPUT" or src == "USER_EXPLICIT":
            m = model_from_settings_change(content)
            if m:
                model = m
            # Settings-only lines are not a user prompt (CC Switch keeps them for model tracking only)
            if m and "<USER_REQUEST>" not in (content or "") and "USER_REQUEST" not in (content or ""):
                continue
            text = _unwrap_user(content)
            if text and text != content.strip():  # unwrapped from tags
                items.append({"role": "user", "kind": "text", "text": text, "ts": ts})
            elif text and not m:
                items.append({"role": "user", "kind": "text", "text": text, "ts": ts})
            elif text and "<USER_REQUEST>" in content:
                items.append({"role": "user", "kind": "text", "text": text, "ts": ts})
            continue
        if typ == "PLANNER_RESPONSE" or (src == "MODEL" and typ not in (
            "RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "SEARCH_WEB", "CODE_ACTION", "CALL_MCP_TOOL")):
            if o.get("model"):
                model = str(o["model"])
            if o.get("thinking"):
                items.append({"role": "assistant", "kind": "thinking", "text": str(o["thinking"]), "ts": ts, "model": model})
            if content:
                items.append({"role": "assistant", "kind": "text", "text": content, "ts": ts, "model": model})
            for tc in o.get("tool_calls") or []:
                name = tc.get("name") or tc.get("tool_name") or "tool"
                args = tc.get("args") if "args" in tc else tc.get("arguments") or tc.get("input") or {}
                tid = str(tc.get("id") or tc.get("call_id") or uuid.uuid4())
                it = {"role": "assistant", "kind": "tool", "tool": name,
                      "input": args if isinstance(args, dict) else {"raw": args},
                      "output": "", "status": "pending", "ts": ts, "model": model, "_tid": tid}
                items.append(it)
                pending_tools[tid] = it
                pending_tools[name] = it
            continue
        if typ in ("RUN_COMMAND", "VIEW_FILE", "LIST_DIRECTORY", "GREP_SEARCH", "SEARCH_WEB", "CODE_ACTION", "CALL_MCP_TOOL"):
            name = {"RUN_COMMAND": "run_command", "VIEW_FILE": "view_file", "LIST_DIRECTORY": "list_directory",
                    "GREP_SEARCH": "grep_search", "SEARCH_WEB": "search_web", "CODE_ACTION": "code_action",
                    "CALL_MCP_TOOL": "call_mcp_tool"}.get(typ, (typ or "tool").lower())
            body = content
            if body.startswith("Created At:"):
                body = re.sub(r"^Created At:.*\nCompleted At:.*\n", "", body, count=1)
            it = pending_tools.get(name)
            if it and not it.get("output"):
                it["output"] = body
                it["status"] = "error" if o.get("status") in ("ERROR", "FAILED", 3) else "ok"
            else:
                items.append({"role": "assistant", "kind": "tool", "tool": name, "input": {}, "output": body,
                              "status": "ok", "ts": ts, "model": model})
    return items, {"created": created, "updated": updated, "model": model}


def _strings_from_pb(blob: bytes, min_len: int = 8) -> list[str]:
    """从 steps.step_payload 里捞可读 UTF-8 串（尽力；无 schema）。"""
    out, i = [], 0
    while i < len(blob):
        # 找可打印串
        if 32 <= blob[i] < 127 or blob[i] >= 0xC0:
            j = i
            while j < len(blob) and blob[j] not in (0,):
                if blob[j] < 32 and blob[j] not in (9, 10, 13):
                    break
                j += 1
            chunk = blob[i:j]
            try:
                s = chunk.decode("utf-8")
            except Exception:
                i += 1; continue
            s = s.strip()
            if len(s) >= min_len and not s.startswith("file://") and "\x00" not in s:
                out.append(s)
            i = max(j, i + 1)
        else:
            i += 1
    return out


def _items_from_db_steps(db: Path) -> tuple[list[dict], dict]:
    """无 transcript 时：从 steps 表按 step_type 尽力还原（orangebox 映射）。"""
    c = _open_ro(db)
    if not c:
        return [], {}
    items, created, updated = [], None, None
    try:
        rows = c.execute("SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx").fetchall()
    except Exception:
        c.close(); return [], {}
    for idx, stype, status, meta, payload in rows:
        ts = None
        if meta:
            for fn, wt, v in _fields(meta):
                if fn == 1 and wt == 2:
                    ts = _ts_pb(v)
                    break
        if ts:
            created = created or ts
            updated = ts
        texts = _strings_from_pb(payload or b"") if payload else []
        text = max(texts, key=len) if texts else ""
        if not text:
            continue
        if stype == 14:  # user
            items.append({"role": "user", "kind": "text", "text": _unwrap_user(text), "ts": float(ts) if ts else None})
        elif stype in (15, 5, 23, 85):  # assistant prose / proposals / summary
            items.append({"role": "assistant", "kind": "text", "text": text, "ts": float(ts) if ts else None})
        elif stype in (21, 7, 8, 9):
            items.append({"role": "assistant", "kind": "tool", "tool": f"step_{stype}", "input": {}, "output": text,
                          "status": "ok", "ts": float(ts) if ts else None})
        # 90/98/99 skip
    c.close()
    return items, {"created": created, "updated": updated, "model": None}


def read_session(info: dict) -> tuple[dict, list[dict]]:
    """→ (header, items)。opaque 且无 transcript 时 items 空、header.note 说明。"""
    root, sid = Path(info["root"]), info["id"]
    h = {"format": NORM, "agent": "antigravity", "id": sid, "title": info.get("title"), "cwd": None,
         "created": None, "updated": float(info.get("mtime") or 0) or None, "model": None,
         "source": info.get("product"), "source_name": {"antigravity-ide": "Antigravity IDE", "antigravity": "Antigravity",
                                                        "antigravity-cli": "Antigravity CLI"}.get(info.get("product"), NAME),
         "file": info.get("file"), "opaque": bool(info.get("opaque"))}
    items, meta = [], {}
    if info.get("transcript"):
        items, meta = _items_from_transcript(Path(info["transcript"]))
    elif info.get("format") == "db":
        items, meta = _items_from_db_steps(Path(info["file"]))
        if not items:
            h["note"] = "会话库是 SQLite，但抽不出可读正文（可能正在写或结构有变）"
    else:
        h["note"] = "旧版 .pb 会话：本机没有配套 transcript，且 protobuf 需 Antigravity 进程才能完整解析，这里只列出"
    if meta.get("created"):
        h["created"] = meta["created"]
    if meta.get("updated"):
        h["updated"] = meta["updated"]
    if meta.get("model"):
        h["model"] = meta["model"]
    # 从第一条用户话补标题
    if (not h.get("title") or h["title"].endswith("…")) and items:
        for it in items:
            if it.get("role") == "user" and it.get("text"):
                h["title"] = it["text"].splitlines()[0][:80]
                break
    return h, items


# ---------- 镜像（和 Cursor 一样：会话管理读镜像）----------

def _write_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == data:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def refresh(force: bool = False) -> list[dict]:
    rows = []
    for info in discover():
        h, items = read_session(info)
        body = [{"format": NORM, **{k: h.get(k) for k in ("agent", "id", "title", "cwd", "created", "updated", "model", "source", "source_name", "note", "opaque")}}]
        for it in items:
            body.append({k: it[k] for k in it if not k.startswith("_")})
        data = ("\n".join(json.dumps(x, ensure_ascii=False) for x in body) + "\n").encode()
        dest = MIRROR / info["product"] / f"{info['id']}.jsonl"
        _write_if_changed(dest, data)
        rows.append({
            "id": info["id"], "agent": "antigravity", "title": h.get("title") or info["id"][:8],
            "cwd": h.get("cwd"), "file": str(dest), "mtime": info["mtime"], "msgs": sum(1 for x in items if x.get("kind") == "text"),
            "source": info["product"], "source_name": h.get("source_name"), "opaque": h.get("opaque"),
            "native": info["file"], "format": info["format"],
        })
    _write_if_changed(MIRROR / "index.json", json.dumps(rows, ensure_ascii=False).encode())
    return rows


def sessions(names: dict | None = None) -> list[dict]:
    try:
        idx = json.loads((MIRROR / "index.json").read_text(encoding="utf-8"))
        if not idx:
            idx = refresh()
    except Exception:
        idx = refresh()
    out = []
    for r in idx:
        f = Path(r["file"])
        if not f.is_file():
            continue
        out.append({
            "agent": "antigravity", "id": r["id"], "title": (names or {}).get(f"antigravity:{r['id']}") or r.get("title") or r["id"][:8],
            "cwd": r.get("cwd"), "file": str(f), "mtime": r.get("mtime") or f.stat().st_mtime,
            "last": r.get("mtime"), "source": r.get("source"), "source_name": r.get("source_name") or NAME,
            "opaque": r.get("opaque"), "readonly": True,
        })
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return out


def read(sid: str, limit: int = 400) -> dict:
    for r in sessions():
        if r["id"] == sid:
            lines = Path(r["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
            head = json.loads(lines[0]) if lines else {}
            entries = [json.loads(x) for x in lines[1:limit + 1] if x.strip()]
            return {"ok": True, "head": head, "entries": entries, "more": max(0, len(lines) - 1 - limit)}
    return {"ok": False, "error": f"找不到 Antigravity 会话 {sid}"}


def find_file(sid: str) -> Path | None:
    for r in sessions():
        if r["id"] == sid:
            return Path(r["file"])
    for prod in PRODUCTS:
        f = MIRROR / prod / f"{sid}.jsonl"
        if f.is_file():
            return f
    return None


def status() -> dict:
    roots = product_roots()
    n = len(discover())
    return {"found": bool(roots) or bool(ide_user_dirs()), "roots": [{"product": k, "path": str(p)} for k, p in roots],
            "ide_user": [str(p) for p in ide_user_dirs()], "sessions": n, "mirror": str(MIRROR)}


# ---------- 用量（transcript PLANNER_RESPONSE 真实 token；对照 CC Switch）----------

def model_for_price(name: str | None) -> str:
    if not name:
        return ""
    n = model_id_of(name.strip()) if " " in name.strip() else name.strip()
    if n in MODEL_ALIAS:
        return MODEL_ALIAS[n]
    base = re.sub(r"-[abc]$", "", n)
    if base in MODEL_ALIAS:
        return MODEL_ALIAS[base]
    # strip effort suffix for price table (high/low/medium)
    y = re.sub(r"-(high|low|medium|thinking)$", "", n)
    if y in MODEL_ALIAS:
        return MODEL_ALIAS[y]
    return n


def iter_usage_from_transcript(path: Path, sid: str):
    """Primary usage source: PLANNER_RESPONSE token fields (CC Switch session_usage_agy).
    input excludes cache; output already includes thinking — do not re-add.
    """
    current_model = None
    for o in records(path):
        typ = o.get("type") or ""
        if typ == "USER_INPUT":
            m = model_from_settings_change(o.get("content") or "")
            if m:
                current_model = m
            continue
        if typ != "PLANNER_RESPONSE":
            continue
        status = o.get("status")
        if status and status != "DONE":
            continue
        def tok(k):
            try:
                return max(0, int(o.get(k) or 0))
            except (TypeError, ValueError):
                return 0
        inp, out, cr, cw = tok("input_tokens"), tok("output_tokens"), tok("cache_read_tokens"), tok("cache_write_tokens")
        think = tok("reasoning_tokens")
        if not (inp or out or cr or cw):
            continue
        step = o.get("step_index")
        display = (o.get("model") or current_model or "").strip() or "unknown"
        model = model_for_price(model_id_of(display))
        ts = _ts_ms(o.get("created_at"))
        ts_s = (ts / 1000) if ts else path.stat().st_mtime
        yield {
            "id": f"ag:{sid}:{step}",
            "ts": ts_s,
            "model": model,
            "display": display,
            "inp": inp, "cr": cr, "cw": cw, "out": out, "think": think,
            "step": step,
        }


def iter_usage(info: dict):
    """Yield usage for one discovered session. Transcript first; optional gen_metadata model fill is unused here."""
    tr = info.get("transcript")
    if not tr:
        return
    yield from iter_usage_from_transcript(Path(tr), info["id"])


def usage_sessions() -> list[dict]:
    """Sessions that have a readable transcript (for usage scan)."""
    return [x for x in discover() if x.get("transcript")]


def conversation_dbs() -> list[tuple[str, Path, str]]:
    """Backward-compatible: [(sid, db_path, product)] for sessions that still have a .db."""
    out = []
    for info in discover():
        f = info.get("file")
        if f and str(f).endswith(".db") and Path(f).is_file():
            out.append((info["id"], Path(f), info["product"]))
    return out



def make_fixture_db(path: Path, gens: list[dict], steps: list[tuple[int, int, str]] | None = None) -> None:
    """测试用：建最小 .db。gens=[{model, inp, out, cr, cw, think, rid, ts}]；steps=[(idx, type, text)]。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    c = sqlite3.connect(str(path))
    c.executescript("""
    CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INT, status INT, metadata BLOB, step_payload BLOB);
    CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY, data BLOB);
    """)
    for i, g in enumerate(gens):
        usage = enc_usage(g.get("inp", 0), g.get("out", 0), g.get("cr", 0), g.get("cw", 0),
                          g.get("think", 0), g.get("resp", 0), g.get("rid", f"r{i}"))
        c.execute("INSERT INTO gen_metadata VALUES(?,?)", (i, enc_gen_meta(g.get("model", "gemini-3-flash-a"), usage, g.get("ts"))))
    for idx, stype, text in (steps or []):
        # 极简 payload：length-delimited UTF-8 当 field 1
        payload = enc_field_bytes(1, text.encode())
        meta = enc_field_bytes(1, enc_field_varint(1, int(time.time())) + enc_field_varint(2, 0)) if False else b""
        c.execute("INSERT INTO steps VALUES(?,?,?,?,?)", (idx, stype, 1, meta or None, payload))
    c.commit(); c.close()


def make_fixture_transcript(path: Path, turns: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, t in enumerate(turns):
        lines.append(json.dumps({"step_index": i, **t}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

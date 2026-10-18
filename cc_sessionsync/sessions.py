# -*- coding: utf-8 -*-
"""会话管理：Claude Code（含桌面端登记）和 Codex 的会话列在一张表里；看记录、改名、在终端里继续、归档、删除、还原、导出；
另外把会话记录备份进自己的 SQLite（~/.cc-sessionsync/sessions.sqlite）。

列表 / 预览 / 导出只读。改东西的操作（改名、归档、删除、还原、从备份恢复）：
- 都是用户在界面上点了才做；每次一本账（~/.cc-sessionsync/session-ops/<时间>/ledger.json），能撤回；
- 删除：先备份进 SQLite，再把记录文件（和桌面端登记）挪进回收站 ~/.cc-sessionsync/trash/<批次>/，从不直接删；
  清空回收站要再确认一次（purge 带 confirm="DELETE"）；
- 改桌面端数据（Claude 的 local_*.json 登记、Codex 的 state_*.sqlite）走 apps.run_or_defer：桌面端开着就排队等它退出；
- 还原 / 恢复：原位置空着才放回原处，否则另存一份新的（Claude 换一个新会话号，不覆盖任何东西）。

列表和归档 / 回收站从 CC Msg 的 session_mgr.py 移植；备份库、改名、账本 / 撤回、导出 jsonl 是新写的。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import paths

TRASH = paths.HOME / "trash"
OPS = paths.HOME / "session-ops"
EXPORTS = paths.HOME / "exports"
DB_PATH = paths.HOME / "sessions.sqlite"
HEAD = 256 * 1024
SKIP_USER = ("# AGENTS.md instructions", "<environment_context", "<app-context", "<user_instructions", "<INSTRUCTIONS",
             "<permissions", "<user_shell_command", "<turn_aborted", "<command-name>", "<local-command", "<system-reminder",
             "Caveat: The messages below", "This session is being continued from a previous conversation")
_title_cache: dict[str, tuple] = {}
_db_lock = threading.Lock()


def claude_home() -> Path:
    return paths.CLAUDE_HOME


def codex_home() -> Path:
    return paths.CODEX_HOME


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"' if re.search(r"[\s&()]", s or "") else s


# ---------- 解析（只为了显示：人话 + 回复 + 工具一行摘要） ----------

def _blk_text(c) -> list[tuple[str, str]]:
    """Claude 的 message.content → [(kind, text)]，kind: text / tool / result"""
    if isinstance(c, str):
        return [("text", c)]
    out = []
    for b in c or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            out.append(("text", b["text"]))
        elif t == "tool_use":
            inp = b.get("input") or {}
            brief = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("description") or inp.get("url") or ""
            out.append(("tool", f"{b.get('name')}  {str(brief)[:160]}".strip()))
    return out


def parse_claude(d: dict) -> list[dict]:
    t = d.get("type")
    if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
        return []
    ts = _ts(d.get("timestamp"))
    m = d.get("message") or {}
    out = []
    for kind, text in _blk_text(m.get("content")):
        if kind == "tool":
            out.append({"role": "tool", "text": text, "ts": ts})
        elif t == "user":
            if not any(text.lstrip().startswith(x) for x in SKIP_USER):
                out.append({"role": "user", "text": text, "ts": ts})
        else:
            out.append({"role": "assistant", "text": text, "ts": ts})
    return out


def parse_codex(d: dict) -> list[dict]:
    if d.get("type") != "response_item":
        return []
    p = d.get("payload") or {}
    ts = _ts(d.get("timestamp"))
    if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
        text = "\n".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict) and c.get("text"))
        if not text or (p["role"] == "user" and any(text.lstrip().startswith(x) for x in SKIP_USER)):
            return []
        return [{"role": p["role"], "text": text, "ts": ts}]
    if p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
        a = p.get("arguments") or p.get("input") or ""
        if isinstance(a, str):
            try:
                j = json.loads(a)
                a = (j.get("cmd") or j.get("command") or json.dumps(j, ensure_ascii=False)) if isinstance(j, dict) else a
            except Exception:
                pass
        if isinstance(a, list):
            a = " ".join(map(str, a))
        return [{"role": "tool", "text": f"{p.get('name') or 'shell'}  {str(a)[:160]}", "ts": ts}]
    return []


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _first_user(f: Path, agent: str) -> tuple[str, str | None, str | None]:
    """（第一句人话, 工作目录, 自定义标题）。按 (路径, 修改时间) 缓存。"""
    try:
        mt = f.stat().st_mtime
    except OSError:
        return "", None, None
    c = _title_cache.get(str(f))
    if c and c[0] == mt:
        return c[1], c[2], c[3]
    text, cwd, title = "", None, None
    if agent == "dsh":
        try:
            from . import dsh
            L = dsh.load(f)
            cwd = L["header"].get("cwd")
            title = next((e["data"].get("title") for e in reversed(L["events"]) if e.get("type") == "session/title"
                          and (e.get("data") or {}).get("title")), None)
            text = next((" ".join(x["text"].split())[:200] for x in dsh.entries(L["events"]) if x["role"] == "user"), "")
        except Exception:
            pass
        _title_cache[str(f)] = (mt, text, cwd, title)
        return text, cwd, title
    try:
        with open(f, "rb") as fh:
            data = fh.read(HEAD)
        parse = parse_claude if agent == "claude" else parse_codex
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if agent == "claude":
                cwd = cwd or d.get("cwd")
                if d.get("type") == "custom-title":
                    title = d.get("customTitle") or title
            elif d.get("type") == "session_meta":
                cwd = (d.get("payload") or {}).get("cwd")
            if not text:
                u = next((e["text"] for e in parse(d) if e["role"] == "user"), None)
                if u:
                    text = " ".join(u.split())[:200]
        if agent == "claude" and f.stat().st_size > HEAD:       # 改名记录可能在尾巴上
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 64 * 1024))
                for line in fh.read().splitlines():
                    if b'"custom-title"' in line:
                        try:
                            title = json.loads(line).get("customTitle") or title
                        except Exception:
                            pass
    except Exception:
        pass
    _title_cache[str(f)] = (mt, text, cwd, title)
    return text, cwd, title


# ---------- Claude ----------

def _claude_registry() -> dict[str, list[dict]]:
    """cliSessionId → [{key 账号/组织, path, data}]（每个账号文件夹各一份登记）。"""
    from . import claude_sync
    reg: dict[str, list[dict]] = {}
    # 订阅登录的文件夹 + API 模式（3P）的文件夹（key 带 3p: 前缀）
    dirs = [(Path(f["path"]), f["key"]) for f in claude_sync.all_folders()]
    for org, key in dirs:
        try:
            idx = set(json.loads((org / "archived-sessions.idx").read_text(encoding="utf-8")).get("archived") or [])
        except Exception:
            idx = set()
        for f in org.glob("local_*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            d["_archived"] = bool(d.get("isArchived")) or f.stem in idx
            reg.setdefault(d.get("cliSessionId") or f.stem, []).append({"key": key, "path": str(f), "data": d})
    return reg


def _acct_tag(key: str) -> str:
    """登记的 key → 分组用的账号短号；API 模式的统一是 '3p'。"""
    return "3p" if key.startswith("3p:") else key.split("/")[0][:8]


ORPHAN_WHY = "无法打开（对话文件不存在）"


def claude_file(sid: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9-]{6,80}", sid or ""):
        return None
    return next(iter((claude_home() / "projects").glob(f"*/{sid}.jsonl")), None)


def agent_file(agent: str, sid: str) -> Path | None:
    """会话记录文件：Claude Code（~/.claude/projects）、Codex（rollout）、DeepSeek Harness（session.v4.jsonl[.zstd]）。"""
    if agent == "claude":
        return claude_file(sid)
    if agent == "dsh":
        from . import dsh
        return dsh.find_file(sid)
    return codex_file(sid)


def parse_bytes(agent: str, data: bytes) -> list[dict]:
    """整份记录 → [{role, text, ts}]（只为显示）。"""
    if agent == "dsh":
        from . import dsh
        return dsh.entries_from_bytes(data)
    parse = parse_claude if agent == "claude" else parse_codex
    out = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            out += parse(json.loads(line))
        except Exception:
            continue
    return out


def dsh_sessions(names: dict | None = None) -> list[dict]:
    try:
        from . import dsh
        return dsh.sessions(names if names is not None else _aliases())
    except Exception:
        return []


def claude_sessions(names: dict | None = None) -> list[dict]:
    from . import claude_sync
    names = names if names is not None else _aliases()
    try:
        cur = claude_sync.visible_key()
    except Exception:
        cur = None
    reg = _claude_registry()
    out, seen = [], set()
    for f in (claude_home() / "projects").glob("*/*.jsonl"):
        sid = f.stem
        seen.add(sid)
        regs = reg.get(sid, [])
        mine = next((r for r in regs if r["key"] == cur), None)
        best = mine or max(regs, key=lambda r: r["data"].get("lastActivityAt", 0), default=None)
        d = best["data"] if best else {}
        first, cwd, ctitle = _first_user(f, "claude")
        st = f.stat()
        if mine:
            why = "已归档" if d.get("_archived") else None
        elif regs:
            why = "在 API 模式里" if all(r["key"].startswith("3p:") for r in regs) else "在别的账号里"
        else:
            why = "只在命令行（桌面端没登记）"
        cwd = d.get("cwd") or cwd
        al = names.get(f"claude:{sid}")
        out.append({"agent": "claude", "id": sid, "title": al or d.get("title") or ctitle or first or "（无标题）", "alias": al,
                    "tool_title": d.get("title") or ctitle, "first": first, "cwd": cwd,
                    "last": max(d.get("lastActivityAt", 0) / 1000, st.st_mtime), "size": st.st_size,
                    "archived": bool(d.get("_archived")), "registered": bool(regs),
                    "accounts": sorted({_acct_tag(r["key"]) for r in regs}), "visible": bool(mine) and not d.get("_archived"),
                    "why": why, "file": str(f), "imported": (ctitle or "").startswith("（Codex）"),
                    "resume": (f"cd {_q(cwd)} && " if cwd else "") + f"claude --resume {sid}"})
    for sid, regs in reg.items():          # 登记了但记录文件已经不在
        if sid in seen:
            continue
        d = max(regs, key=lambda r: r["data"].get("lastActivityAt", 0))["data"]
        out.append({"agent": "claude", "id": sid, "title": names.get(f"claude:{sid}") or d.get("title") or "（无标题）", "first": "",
                    "cwd": d.get("cwd"), "last": d.get("lastActivityAt", 0) / 1000, "size": 0, "archived": bool(d.get("_archived")),
                    "registered": True, "accounts": sorted({_acct_tag(r["key"]) for r in regs}), "visible": False,
                    "why": ORPHAN_WHY, "file": None, "resume": None, "orphan": True, "model": d.get("model")})
    return out


# ---------- Codex ----------

def state_db() -> Path | None:
    c = []
    for f in codex_home().glob("state_*.sqlite"):
        try:
            c.append((int(f.stem.split("_")[1]), f))
        except Exception:
            pass
    return max(c)[1] if c else None


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)


def _cols(conn, table="threads") -> set[str]:
    return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def rollouts():
    for base in (codex_home() / "sessions", codex_home() / "archived_sessions"):
        if base.exists():
            yield from base.rglob("rollout-*.jsonl")


def _codex_meta(f: Path) -> dict:
    try:
        with open(f, "rb") as fh:
            m = json.loads(fh.readline())
        return (m.get("payload") or {}) if m.get("type") == "session_meta" else {}
    except Exception:
        return {}


def codex_titles() -> dict:
    out = {}
    try:
        for line in (codex_home() / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("id"):
                    out[d["id"]] = d.get("thread_name") or ""
            except Exception:
                pass
    except OSError:
        pass
    return out


def codex_file(tid: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9-]{6,80}", tid or ""):
        return None
    db = state_db()
    if db:
        try:
            c = _ro(db)
            r = c.execute("select rollout_path from threads where id=?", (tid,)).fetchone()
            c.close()
            if r and r[0] and Path(r[0]).exists():
                return Path(r[0])
        except Exception:
            pass
    return next((f for f in rollouts() if f.stem.endswith(tid)), None)


def codex_sessions(names: dict | None = None) -> list[dict]:
    names = names if names is not None else _aliases()
    idx = codex_titles()
    rows: dict[str, dict] = {}
    db = state_db()
    if db:
        try:
            conn = _ro(db)
            cols = _cols(conn)
            want = [c for c in ("id", "title", "cwd", "updated_at", "archived", "model_provider", "rollout_path",
                                "first_user_message", "thread_source", "name") if c in cols]
            for r in conn.execute(f"select {', '.join(want)} from threads"):
                r = dict(zip(want, r))
                rows[r["id"]] = r
            conn.close()
        except Exception:
            rows = {}
    for f in rollouts():                        # 库里没有的（老版本 / 库坏了）按文件补上
        tid = f.stem[-36:]
        if tid in rows:
            continue
        m = _codex_meta(f)
        tid = m.get("id") or tid
        if tid in rows:
            continue
        rows[tid] = {"id": tid, "cwd": m.get("cwd"), "updated_at": f.stat().st_mtime, "archived": "archived_sessions" in f.parts,
                     "model_provider": m.get("model_provider"), "rollout_path": str(f), "thread_source": m.get("originator")}
    out = []
    for tid, r in rows.items():
        f = Path(r["rollout_path"]) if r.get("rollout_path") else None
        exists = bool(f and f.exists())
        first = " ".join((r.get("first_user_message") or "").split())[:200]
        if exists and not first:
            first = _first_user(f, "codex")[0]
        al = names.get(f"codex:{tid}")
        tool_title = idx.get(tid) or r.get("name") or r.get("title")
        out.append({"agent": "codex", "id": tid, "title": al or tool_title or first or "（无标题）", "alias": al, "tool_title": tool_title,
                    "first": first, "cwd": r.get("cwd"), "last": float(r.get("updated_at") or 0),
                    "size": f.stat().st_size if exists else 0, "archived": bool(r.get("archived")), "registered": bool(db),
                    "provider": r.get("model_provider"), "visible": exists and not r.get("archived"),
                    "why": None if exists else ORPHAN_WHY, "file": str(f) if exists else None, "orphan": not exists,
                    "resume": (f"cd {_q(r['cwd'])} && " if r.get("cwd") else "") + f"codex resume {tid}"})
    return out


def list_all(agent: str | None = None) -> dict:
    t0 = time.time()
    names = _aliases()
    items = (claude_sessions(names) if agent in (None, "", "claude") else []) + \
            (codex_sessions(names) if agent in (None, "", "codex") else []) + \
            (dsh_sessions(names) if agent in (None, "", "dsh") else [])
    items.sort(key=lambda x: -(x["last"] or 0))
    bk = backup_index()
    for x in items:
        x["backups"] = bk.get(f"{x['agent']}:{x['id']}", 0)
    projects = sorted({x["cwd"] for x in items if x.get("cwd")})
    accounts = sorted({a for x in items for a in x.get("accounts") or []})
    groups = account_groups(items)
    return {"ok": True, "items": items, "projects": projects, "accounts": accounts, "account_labels": _account_labels(accounts),
            "groups": groups, "ms": int((time.time() - t0) * 1000)}


def account_groups(items: list[dict]) -> list[dict]:
    """这台电脑上找得到的所有账号（左侧「按账号」分组用），并给每个会话标上 gacct（分到哪个账号下）：
    - Claude：每个桌面端会话列表目录（%APPDATA% 和商店版虚拟化目录都看）下的「账号 / 组织」文件夹、命令行登的号；
      会话归到登记了它的账号（优先当前账号），没登记的（只在命令行）归到「未知账号」。
    - Codex：按供应商分（Codex 的记录里不写账号）；当前登录的号标在它用的供应商上。
    只给打码的邮箱和 rid，完整的要点眼睛走 /api/accounts/reveal。"""
    from . import claude_sync, watch
    from .watch import mask_email
    names = {}
    try:
        names = {u: m.get("email") for u, m in claude_sync._load_names().items()}
    except Exception:
        pass
    try:
        acc = claude_sync.accounts()
    except Exception:
        acc = {}
    cur_d = (acc.get("desktop") or {}).get("accountUuid")
    cur_c = (acc.get("cli") or {}).get("accountUuid")
    for a in acc.values():
        if a.get("accountUuid") and a.get("email"):
            names[a["accountUuid"]] = a["email"]
    g: dict[str, dict] = {}

    def claude_g(uuid_: str) -> dict:
        k = uuid_[:8]
        if k not in g:
            full = next((u for u in list(names) + [cur_d or "", cur_c or ""] if u and u.startswith(k)), uuid_)
            tags = (["桌面端当前"] if cur_d and cur_d.startswith(k) else []) + (["命令行当前"] if cur_c and cur_c.startswith(k) else [])
            g[k] = {"key": k, "kind": "claude", "rid": full if len(full) > 8 else "", "masked": mask_email(names.get(full)) or f"账号 {k}",
                    "tags": tags, "current": bool(tags), "folders": [], "n": 0}
        return g[k]
    alt: dict[str, str] = {}
    try:
        main = claude_sync.sessions_root()
        roots = [(main, "")] + [(Path(c["path"]) / "claude-code-sessions", c["label"]) for c in claude_sync.app_dir_candidates()]
        done = set()
        for root, lab in roots:
            try:
                rp = root.resolve()
            except OSError:
                continue
            if rp in done or not root.is_dir():
                continue
            done.add(rp)
            dev = claude_sync.device_id(root.parent)
            for a in root.iterdir():
                if not a.is_dir() or (dev and a.name.lower() == dev):
                    continue
                for o in a.iterdir():
                    if o.is_dir():
                        fs = list(o.glob("local_*.json"))
                        claude_g(a.name)["folders"].append({"key": f"{a.name}/{o.name}", "where": lab or "当前使用的目录", "count": len(fs)})
                        if root != main:
                            for f in fs:
                                try:
                                    alt.setdefault(json.loads(f.read_text(encoding="utf-8")).get("cliSessionId") or "", a.name[:8])
                                except Exception:
                                    pass
    except Exception:
        pass
    for u in (cur_d, cur_c):
        if u:
            claude_g(u)
    try:                                         # API 模式（3P）：一个分组，所有供应商共用
        ai, mode = claude_sync.api_info(), claude_sync.desktop_mode()["mode"]
        if ai["buckets"]:
            prov = ai.get("provider")
            g["3p"] = {"key": "3p", "kind": "api", "rid": "", "provider": prov,
                       "masked": "API / 第三方供应商（3P）" + (f" · {prov}" if prov else ""),
                       "tags": ["桌面端当前"] if mode == "3p" else [], "current": mode == "3p",
                       "folders": [{"key": b["key"], "where": "API 模式（所有供应商共用）", "count": b["count"]} for b in ai["buckets"]],
                       "n": 0, "hint": "Claude 桌面端 API 模式（API key / 第三方供应商）的会话列表：换供应商不分开，所有供应商共用这一个"}
    except Exception:
        pass
    try:
        cx = watch._read_codex() or {}
    except Exception:
        cx = {}
    for x in items:
        if x["agent"] == "claude":
            accs = x.get("accounts") or []
            k = next((a for a in accs if cur_d and cur_d.startswith(a)), None) or next((a for a in accs if a != "3p"), None) \
                or ("3p" if "3p" in accs and "3p" in g else None) or alt.get(x["id"])
            x["gacct"] = k or "unknown"
            if k == "3p":
                g["3p"]["n"] += 1
            elif k:
                claude_g(k)["n"] += 1
        elif x["agent"] == "dsh":                # DeepSeek Harness：按供应商分（记录里只有 provider id，没有账号）
            prov = x.get("provider") or ""
            k = "dsh:" + (prov or "-")
            if k not in g:
                g[k] = {"key": k, "kind": "dsh", "rid": "", "provider": prov or None,
                        "masked": "DeepSeek Harness" + (f" · {prov}" if prov else ""), "tags": [], "current": False,
                        "folders": [], "n": 0, "hint": "DeepSeek Harness 的会话（~/.dsh/sessions），按记录里的供应商分"}
            g[k]["n"] += 1
            x["gacct"] = k
        else:
            prov = x.get("provider") or "openai"
            k = "codex:" + prov
            if k not in g:
                lab = cx.get("label") or ""
                mine = lab.endswith(" · " + prov)
                g[k] = {"key": k, "kind": "codex", "rid": cx.get("rid", "") if mine else "", "provider": prov,
                        "masked": (lab.rsplit(" · ", 1)[0] if mine else "") or f"供应商 {prov}", "tags": ["Codex 当前"] if mine else [],
                        "current": mine, "folders": [], "n": 0}
            g[k]["n"] += 1
            x["gacct"] = k
    unk = sum(1 for x in items if x.get("gacct") == "unknown")
    out = sorted(g.values(), key=lambda v: ({"claude": 0, "api": 1, "codex": 2}.get(v["kind"], 3), not v["current"], -v["n"]))
    if unk:
        out.append({"key": "unknown", "kind": "unknown", "rid": "", "masked": "未知账号", "tags": [], "current": False,
                    "folders": [], "n": unk, "hint": "只在命令行用过、没在任何桌面端账号里登记的会话"})
    return out


def _account_labels(prefixes: list[str]) -> dict:
    """账号号前 8 位 → {masked: 打码邮箱, rid: 完整账号号}（筛选下拉框里用；完整邮箱要点眼睛去 /api/accounts/reveal 取）。"""
    from . import claude_sync
    from .watch import mask_email
    known = {}
    try:
        for uuid, m in claude_sync._load_names().items():
            known[uuid] = m.get("email")
        for a in claude_sync.accounts().values():
            if a.get("accountUuid"):
                known[a["accountUuid"]] = a.get("email") or known.get(a["accountUuid"])
    except Exception:
        pass
    out = {}
    for p in prefixes:
        hit = next((u for u in known if u.startswith(p)), None)
        out[p] = {"masked": mask_email(known.get(hit)) if hit else "", "rid": hit or ""}
    return out


def find(agent: str, sid: str) -> dict | None:
    rows = claude_sessions() if agent == "claude" else dsh_sessions() if agent == "dsh" else codex_sessions()
    return next((x for x in rows if x["id"] == sid), None)


def read(agent: str, sid: str, limit: int = 400) -> dict:
    """只读看记录：最后 limit 条（人话 / 回复 / 工具一行）。"""
    if agent == "dsh":
        from . import dsh
        return dsh.read(sid, limit)
    f = claude_file(sid) if agent == "claude" else codex_file(sid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    parse = parse_claude if agent == "claude" else parse_codex
    es = []
    with open(f, "rb") as fh:
        for line in fh:
            try:
                es += parse(json.loads(line))
            except Exception:
                continue
    return {"ok": True, "file": str(f), "total": len(es), "entries": [dict(e, text=e["text"][:6000]) for e in es[-limit:]],
            "cut": max(0, len(es) - limit)}


def fulltext(q: str, limit_s: float = 15) -> dict:
    if not (q or "").strip():
        return {"hits": [], "partial": False}
    needle = q.strip().lower().encode("utf-8")
    t0, hits, partial = time.time(), [], False
    files = [("claude", f) for f in (claude_home() / "projects").glob("*/*.jsonl")] + [("codex", f) for f in rollouts()]
    try:
        from . import dsh
        files += [("dsh", sf["file"]) for sf in dsh.session_files() if sf["version"] == dsh.FORMAT]
    except Exception:
        pass
    files.sort(key=lambda x: -x[1].stat().st_mtime)
    for agent, f in files:
        if time.time() - t0 > limit_s:
            partial = True
            break
        if agent == "dsh":                      # 压缩的：解开再找（只在人话 / 回复里找）
            try:
                if any(needle.decode() in e["text"].lower() for e in parse_bytes("dsh", f.read_bytes())):
                    hits.append({"agent": "dsh", "id": dsh.load(f)["header"].get("id") or f.parent.name})
            except Exception:
                pass
            continue
        try:
            with open(f, "rb") as fh:
                while chunk := fh.read(8 * 1024 * 1024):
                    if needle in chunk.lower():
                        hits.append({"agent": agent, "id": f.stem if agent == "claude" else (_codex_meta(f).get("id") or f.stem[-36:])})
                        break
        except OSError:
            continue
    return {"hits": hits, "partial": partial, "ms": int((time.time() - t0) * 1000)}


def export(agent: str, sid: str, fmt: str = "md") -> dict:
    """导出到 ~/.cc-sessionsync/exports/：md / html（排好版、带原始记录，能导回来；见 exports.py）或 jsonl（原样复制）。"""
    f = agent_file(agent, sid)
    if not f:
        return {"ok": False, "error": "找不到会话记录文件"}
    EXPORTS.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    if fmt == "jsonl":
        out = EXPORTS / (f"{agent}-{sid[:16]}-{stamp}.jsonl" + (".zstd" if f.name.endswith(".zstd") else ""))
        shutil.copy2(f, out)
        return {"ok": True, "path": str(out), "name": out.name, "size": out.stat().st_size}
    from . import exports
    r = exports.export_many([{"agent": agent, "id": sid}], "html" if fmt == "html" else "md")
    if not r["ok"]:
        return {"ok": False, "error": "; ".join(r["errors"])}
    out = Path(r["files"][0])
    return {"ok": True, "path": str(out), "name": out.name, "size": out.stat().st_size}


# ---------- 备份库（SQLite：原始 jsonl 压缩存 + 元数据；按 修改时间/大小 增量，按内容 sha256 去重） ----------

def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY, size INT, data BLOB);
    CREATE TABLE IF NOT EXISTS snaps(id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, sid TEXT, path TEXT, sha TEXT,
        mtime REAL, size INT, title TEXT, cwd TEXT, first TEXT, at REAL, why TEXT, UNIQUE(agent, sid, sha));
    CREATE TABLE IF NOT EXISTS seen(path TEXT PRIMARY KEY, mtime REAL, size INT, sha TEXT);
    CREATE TABLE IF NOT EXISTS aliases(k TEXT PRIMARY KEY, name TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    for t, col in (("blobs", "base TEXT"), ("blobs", "depth INT DEFAULT 0"), ("snaps", "msgs INT"), ("snaps", "run REAL")):
        try:                                   # 0.2.0 的库：补上 0.2.1 的列（老数据照常能读）
            c.execute(f"alter table {t} add column {col}")
        except sqlite3.OperationalError:
            pass
    c.execute("create index if not exists snaps_by_sid on snaps(agent, sid, at)")
    c.execute("create index if not exists snaps_by_at on snaps(at)")
    return c


def _aliases() -> dict:
    try:
        with _db_lock:
            c = _db()
            r = dict(c.execute("select k, name from aliases"))
            c.close()
        return r
    except Exception:
        return {}


def backup_index() -> dict:
    try:
        with _db_lock:
            c = _db()
            r = {f"{a}:{s}": n for a, s, n in c.execute("select agent, sid, count(*) from snaps group by 1, 2")}
            c.close()
        return r
    except Exception:
        return {}


MAX_DEPTH = 30          # 增量链最长多少节，超过就存一份完整的（读的时候不用拼太多段）


def count_msgs(agent: str, data: bytes) -> int:
    """数一下有几条人话 / 回复（时间线上显示「n 条消息」）。"""
    if agent == "dsh":
        from . import dsh
        return dsh.count_msgs(data)
    parse = parse_claude if agent == "claude" else parse_codex
    n = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            n += sum(1 for e in parse(json.loads(line)) if e["role"] in ("user", "assistant"))
        except Exception:
            continue
    return n


def _put_blob(c, data: bytes, prev: tuple | None) -> tuple[str, bool, int | None]:
    """存内容（按 sha256 去重）。会话记录是只往后追加的：新内容开头 == 上一版时只存追加的那一段（base + 增量）。
    → (sha, 是不是新存的, 这一版的消息数（增量时按上一版 + 追加部分算，算不出来是 None）)"""
    sha = hashlib.sha256(data).hexdigest()
    if c.execute("select 1 from blobs where sha=?", (sha,)).fetchone():
        return sha, False, None
    base, depth, payload, msgs = None, 0, data, None
    if prev:
        psha, psize, pmsgs, agent = prev
        if psha and 0 < (psize or 0) < len(data) and hashlib.sha256(data[:psize]).hexdigest() == psha:
            row = c.execute("select coalesce(depth, 0) from blobs where sha=?", (psha,)).fetchone()
            if row is not None and row[0] < MAX_DEPTH:
                base, depth, payload = psha, row[0] + 1, data[psize:]
                if pmsgs is not None:
                    msgs = pmsgs + count_msgs(agent, payload)
    c.execute("insert into blobs(sha, size, data, base, depth) values(?,?,?,?,?)",
              (sha, len(data), zlib.compress(payload, 6), base, depth))
    return sha, True, msgs


def _store(c, agent: str, sid: str, f: Path, why: str, data: bytes | None = None, run: float | None = None) -> tuple[str, bool]:
    data = f.read_bytes() if data is None else data
    st = f.stat()
    seen = c.execute("select sha, size from seen where path=?", (str(f),)).fetchone()
    prev = None
    if seen:
        pm = c.execute("select msgs from snaps where sha=? and msgs is not null limit 1", (seen[0],)).fetchone()
        prev = (seen[0], seen[1], pm[0] if pm else None, agent)
    sha, new, msgs = _put_blob(c, data, prev)
    if msgs is None:
        old = c.execute("select msgs from snaps where sha=? and msgs is not null limit 1", (sha,)).fetchone()
        msgs = old[0] if old else count_msgs(agent, data)
    first, cwd, title = _first_user(f, agent)
    c.execute("insert or ignore into snaps(agent, sid, path, sha, mtime, size, title, cwd, first, at, why, msgs, run) "
              "values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (agent, sid, str(f), sha, st.st_mtime, len(data), title, cwd, first, time.time(), why, msgs, run or time.time()))
    c.execute("insert or replace into seen values(?,?,?,?)", (str(f), st.st_mtime, st.st_size, sha))
    return sha, new


BK = {"running": False, "last": None, "error": None}


def backup(full: bool = False, only: list[tuple[str, str]] | None = None, why: str = "scan", low: bool = False) -> dict:
    """把会话记录增量备份进 sessions.sqlite。only=[(agent, sid)] 只备份这几条（删除前用，强制读一遍）。"""
    t0 = time.time()
    if not only and BK.get("running"):
        return {"ok": True, "busy": True, "files": 0, "skipped": 0, "new_blobs": 0, "snapshots": 0, "ms": 0, "db": str(DB_PATH)}
    files: list[tuple[str, str, Path]] = []
    if only:
        for a, s in only:
            f = agent_file(a, s)
            if f:
                files.append((a, s, f))
    else:
        files = [("claude", f.stem, f) for f in (claude_home() / "projects").glob("*/*.jsonl")]
        files += [("codex", _codex_meta(f).get("id") or f.stem[-36:], f) for f in rollouts()]
        try:
            from . import dsh
            files += [("dsh", sf["dir"].name, sf["file"]) for sf in dsh.session_files() if sf["version"] == dsh.FORMAT]
        except Exception:
            pass
    n_new = n_snap = n_skip = 0
    BK["running"] = True
    try:
        with _db_lock:
            c = _db()
            seen = {p: (m, s) for p, m, s, _ in c.execute("select * from seen")}
            for a, s, f in files:
                try:
                    st = f.stat()
                except OSError:
                    continue
                if not full and not only and seen.get(str(f)) == (st.st_mtime, st.st_size):
                    n_skip += 1
                    continue
                before = c.total_changes
                _, new = _store(c, a, s, f, why, run=t0)
                if low:
                    time.sleep(0.002)                  # 后台备份：每个文件之间让一下，不抢前台
                n_new += new
                n_snap += c.total_changes - before > 1
            c.execute("insert or replace into meta values('last_backup', ?)", (str(time.time()),))
            c.commit()
            c.close()
        BK.update(last=time.time(), error=None)
    except Exception as e:
        BK["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        BK["running"] = False
    return {"ok": True, "files": len(files), "skipped": n_skip, "new_blobs": n_new, "snapshots": n_snap,
            "ms": int((time.time() - t0) * 1000), "db": str(DB_PATH)}


def backup_status() -> dict:
    try:
        with _db_lock:
            c = _db()
            n = c.execute("select count(*), count(distinct agent || sid) from snaps").fetchone()
            b = c.execute("select count(*), coalesce(sum(size), 0), coalesce(sum(length(data)), 0) from blobs").fetchone()
            last = c.execute("select v from meta where k='last_backup'").fetchone()
            c.close()
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {"db": str(DB_PATH), "snapshots": n[0], "sessions": n[1], "blobs": b[0], "raw_bytes": b[1], "stored_bytes": b[2], "db_bytes": size,
                "last": float(last[0]) if last else None, **{k: BK[k] for k in ("running", "error")}}
    except Exception as e:
        return {"db": str(DB_PATH), "error": str(e)}


def snapshots(agent: str | None = None, sid: str | None = None, limit: int = 200) -> list[dict]:
    with _db_lock:
        c = _db()
        w, a = [], []
        if agent:
            w.append("agent=?"), a.append(agent)
        if sid:
            w.append("sid=?"), a.append(sid)
        q = "select id, agent, sid, path, sha, mtime, size, title, cwd, first, at, why from snaps" + \
            (" where " + " and ".join(w) if w else "") + " order by at desc limit ?"
        rows = [dict(zip(("id", "agent", "sid", "path", "sha", "mtime", "size", "title", "cwd", "first", "at", "why"), r))
                for r in c.execute(q, a + [int(limit)])]
        c.close()
    for r in rows:
        r["exists"] = Path(r["path"]).exists()
    return rows


def _blob(sha: str) -> bytes:
    """取一份完整内容：增量存的就顺着 base 往回找，拼起来，最后校验 sha256。"""
    parts, cur, guard = [], sha, 0
    with _db_lock:
        c = _db()
        try:
            while cur and guard <= MAX_DEPTH + 5:
                r = c.execute("select data, base from blobs where sha=?", (cur,)).fetchone()
                if not r:
                    raise FileNotFoundError("备份库里没有这份内容")
                parts.append(zlib.decompress(r[0]))
                cur, guard = r[1], guard + 1
        finally:
            c.close()
    data = b"".join(reversed(parts))
    if hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("备份内容校验不对")
    return data


# ---------- 账本 ----------

def _new_op(kind: str, **kw) -> tuple[Path, dict]:
    d = OPS / f"{datetime.now():%Y%m%d-%H%M%S}-{kind}"
    while d.exists():
        d = d.with_name(d.name + "x")
    (d / "before").mkdir(parents=True)
    led = {"kind": kind, "time": datetime.now().isoformat(timespec="seconds"), "steps": [], "undone": None, **kw}
    return d, led


def _save_led(d: Path, led: dict) -> None:
    tmp = d / "ledger.tmp"
    tmp.write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, d / "ledger.json")


def _keep(d: Path, led: dict, f: Path) -> str:
    n = f"{len(led['steps']):04d}-{f.name}"
    shutil.copy2(f, d / "before" / n)
    return n


def ops_list(limit: int = 60) -> list[dict]:
    out = []
    if OPS.exists():
        for d in sorted(OPS.iterdir(), reverse=True)[:limit]:
            try:
                led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({"id": d.name, "kind": led["kind"], "time": led["time"], "undone": led.get("undone"), "write_tool": led.get("write_tool"),
                        "items": led.get("items") or [], "summary": led.get("summary") or "", "errors": led.get("errors") or []})
    return out


def _atomic(f: Path, data: bytes) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".ccss-tmp-{os.getpid()}-{f.name}")
    tmp.write_bytes(data)
    os.replace(tmp, f)


# ---------- 改名 ----------

def rename(agent: str, sid: str, name: str, write_tool: bool = False) -> dict:
    """改名：缺省只改本程序里的显示名（存在 sessions.sqlite，不碰 Claude / Codex 的文件）。
    write_tool=True 时也写进工具自己的标题（格式本身就支持、可撤回）：
      Claude：当前账号的桌面端登记 local_*.json 的 title（先备份），没登记的在记录末尾追加一条 custom-title（和 /rename 一样）；
      Codex：session_index.jsonl 追加一条 {id, thread_name, updated_at}（Codex 自己改名也是追加，最后一条算数）+ 库里的 name/title。"""
    name = (name or "").strip()[:200]
    k = f"{agent}:{sid}"
    d, led = _new_op("rename", items=[{"agent": agent, "id": sid}], summary=f"改名为「{name or '（清除）'}」", write_tool=write_tool)
    with _db_lock:
        c = _db()
        old = c.execute("select name from aliases where k=?", (k,)).fetchone()
        led["steps"].append({"t": "alias", "k": k, "old": old[0] if old else None})
        if name:
            c.execute("insert or replace into aliases values(?,?,?)", (k, name, time.time()))
        else:
            c.execute("delete from aliases where k=?", (k,))
        c.commit()
        c.close()
    errors = []
    if write_tool and name:
        try:
            if agent == "claude":
                _rename_claude(sid, name, d, led)
            elif agent == "dsh":
                errors.append("DeepSeek Harness 的会话不改它自己的文件，只改了本程序里的显示名")
            else:
                _rename_codex(sid, name, d, led)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
    led["errors"] = errors
    _save_led(d, led)
    return {"ok": not errors, "op": d.name, "errors": errors}


def _rename_claude(sid: str, name: str, d: Path, led: dict) -> None:
    from . import claude_sync
    cur = claude_sync.visible_key()
    regs = [r for r in _claude_registry().get(sid, []) if r["key"] == cur]
    if regs:
        for r in regs:
            p = Path(r["path"])
            n = _keep(d, led, p)
            j = json.loads(p.read_text(encoding="utf-8"))
            j["title"] = name
            _atomic(p, json.dumps(j, ensure_ascii=False).encode("utf-8"))
            led["steps"].append({"t": "file", "path": str(p), "before": n})
        return
    f = claude_file(sid)
    if not f:
        raise FileNotFoundError("找不到会话记录文件")
    size = f.stat().st_size
    with open(f, "ab") as fh:
        fh.write((json.dumps({"type": "custom-title", "customTitle": name, "sessionId": sid}, ensure_ascii=False) + "\n").encode("utf-8"))
    led["steps"].append({"t": "append", "path": str(f), "size": size})


def _rename_codex(sid: str, name: str, d: Path, led: dict) -> None:
    idx = codex_home() / "session_index.jsonl"
    size = idx.stat().st_size if idx.exists() else None
    line = json.dumps({"id": sid, "thread_name": name, "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, ensure_ascii=False)
    with open(idx, "ab") as fh:
        fh.write((line + "\n").encode("utf-8"))
    led["steps"].append({"t": "append", "path": str(idx), "size": size})
    db = state_db()
    if db:
        conn = sqlite3.connect(db, timeout=10)
        try:
            cols = _cols(conn)
            r = conn.execute("select " + ", ".join(c for c in ("name", "title") if c in cols) + " from threads where id=?", (sid,)).fetchone() \
                if {"name", "title"} & cols else None
            if r is not None:
                old = dict(zip([c for c in ("name", "title") if c in cols], r))
                with conn:
                    if "name" in cols:
                        conn.execute("update threads set name=? where id=?", (name, sid))
                led["steps"].append({"t": "codex-row", "db": str(db), "id": sid, "old": old})
        finally:
            conn.close()


# ---------- 归档 / 删除（改桌面端数据：由 apps.run_or_defer 调） ----------

def _claude_set_archived(sid: str, on: bool, d: Path, led: dict) -> int:
    n = 0
    for r in _claude_registry().get(sid, []):      # 每个账号里的登记都改（换号同步时状态跟着走）
        p = Path(r["path"])
        led["steps"].append({"t": "file", "path": str(p), "before": _keep(d, led, p)})
        j = json.loads(p.read_text(encoding="utf-8"))
        j["isArchived"] = on
        _atomic(p, json.dumps(j, ensure_ascii=False).encode("utf-8"))
        idx = p.parent / "archived-sessions.idx"
        if idx.exists():
            led["steps"].append({"t": "file", "path": str(idx), "before": _keep(d, led, idx)})
        else:
            led["steps"].append({"t": "created", "path": str(idx)})
        try:
            jj = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            jj = {"v": 1, "archived": []}
        a = [x for x in jj.get("archived", []) if x != p.stem] + ([p.stem] if on else [])
        _atomic(idx, json.dumps({**jj, "archived": a}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        n += 1
    return n


def _codex_dated_dir(f: Path) -> Path:
    m = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", f.name)
    base = codex_home() / "sessions"
    return base / m.group(1) / m.group(2) / m.group(3) if m else base


def _codex_set_archived(conn, tid: str, on: bool, led: dict) -> int:
    """和 Codex 自己归档一样：文件挪到 archived_sessions/（取消归档挪回 sessions/年/月/日/），库里改 archived 与路径。"""
    f = codex_file(tid)
    r = conn.execute("select * from threads where id=?", (tid,)).fetchone() if conn is not None else None
    new = None
    if f and f.exists():
        dst = (codex_home() / "archived_sessions") if on else _codex_dated_dir(f)
        dst.mkdir(parents=True, exist_ok=True)
        if f.parent.resolve() != dst.resolve():
            new = dst / f.name
            shutil.move(str(f), new)
            led["steps"].append({"t": "move", "from": str(f), "to": str(new)})
    if conn is not None and r is not None:
        cols = _cols(conn)
        names = [c[1] for c in conn.execute("pragma table_info(threads)")]
        old = dict(zip(names, r))
        sets, vals = ["archived=?"], [1 if on else 0]
        if "archived_at" in cols:
            sets.append("archived_at=?"), vals.append(int(time.time()) if on else None)
        if new:
            sets.append("rollout_path=?"), vals.append(str(new))
        with conn:
            conn.execute(f"update threads set {', '.join(sets)} where id=?", (*vals, tid))
        led["steps"].append({"t": "codex-row", "id": tid, "old": {k: old.get(k) for k in ("archived", "archived_at", "rollout_path") if k in old}})
    return 1 if (new or r is not None) else 0


def _move_to(src: Path, trash_dir: Path, moved: list) -> None:
    rel = f"{len(moved):04d}-{src.name}"
    shutil.move(str(src), trash_dir / rel)
    moved.append({"from": str(src), "to": rel})


def _claude_delete(sid: str, trash_dir: Path) -> dict:
    moved: list = []
    for f in (claude_home() / "projects").glob(f"*/{sid}.jsonl"):
        _move_to(f, trash_dir, moved)
        side = f.with_suffix("")                 # 同名目录：子代理记录、工具结果
        if side.is_dir():
            _move_to(side, trash_dir, moved)
    for r in _claude_registry().get(sid, []):    # 所有账号里的登记都挪走，免得换号同步又补回来
        _move_to(Path(r["path"]), trash_dir, moved)
    return {"agent": "claude", "id": sid, "moved": moved}


def _codex_delete(conn, tid: str, trash_dir: Path) -> dict:
    moved: list = []
    row = None
    if conn is not None:
        cur = conn.execute("select * from threads where id=?", (tid,))
        names = [c[0] for c in cur.description]
        r = cur.fetchone()
        row = dict(zip(names, r)) if r else None
    f = Path(row["rollout_path"]) if row and row.get("rollout_path") else codex_file(tid)
    if f and f.exists():
        _move_to(f, trash_dir, moved)
    if row and conn is not None:
        with conn:
            conn.execute("delete from threads where id=?", (tid,))
    return {"agent": "codex", "id": tid, "moved": moved, "row": row}


def batch(action: str, items: list[dict]) -> dict:
    """action：archive / unarchive / delete；items：[{agent, id}]（一种 agent 一批，调用方按桌面端分开排队）。"""
    items = [i for i in items if i.get("agent") in ("claude", "codex") and re.fullmatch(r"[A-Za-z0-9-]{6,80}", str(i.get("id")))]
    if action not in ("archive", "unarchive", "delete"):
        raise ValueError("action 只能是 archive / unarchive / delete")
    d, led = _new_op(action, items=[{"agent": i["agent"], "id": i["id"]} for i in items])
    done, errors, manifest = 0, [], []
    trash_dir = None
    if action == "delete":
        bk = backup(only=[(i["agent"], i["id"]) for i in items], why="before-delete")     # 先进备份库
        led["backup"] = bk
        trash_dir = TRASH / d.name
        trash_dir.mkdir(parents=True, exist_ok=True)
        led["trash"] = trash_dir.name
    conn = None
    db = state_db()
    if any(i["agent"] == "codex" for i in items) and db:
        conn = sqlite3.connect(db, timeout=10)
    _save_led(d, led)
    try:
        for it in items:
            try:
                if it["agent"] == "claude":
                    if action == "delete":
                        manifest.append(_claude_delete(it["id"], trash_dir))
                        done += 1
                    else:
                        done += bool(_claude_set_archived(it["id"], action == "archive", d, led))
                else:
                    if action == "delete":
                        manifest.append(_codex_delete(conn, it["id"], trash_dir))
                        done += 1
                    else:
                        done += _codex_set_archived(conn, it["id"], action == "archive", led)
            except Exception as e:
                errors.append(f"{it['agent']}:{it['id'][:8]} {type(e).__name__}: {e}")
            _save_led(d, led)
    finally:
        if conn is not None:
            conn.close()
    if trash_dir:
        (trash_dir / "manifest.json").write_text(json.dumps({"time": led["time"], "op": d.name, "items": manifest},
                                                            ensure_ascii=False, indent=1), encoding="utf-8")
        led["manifest"] = manifest
    led.update(errors=errors, summary=f"{ {'archive': '归档', 'unarchive': '取消归档', 'delete': '删除（挪进回收站）'}[action] } {done} 条")
    _save_led(d, led)
    return {"ok": not errors, "done": done, "errors": errors, "op": d.name, "trash": trash_dir.name if trash_dir else None}


def trash_list() -> list[dict]:
    out = []
    if not TRASH.exists():
        return out
    for d in sorted(TRASH.iterdir(), reverse=True):
        try:
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"id": d.name, "time": m.get("time"), "restored": m.get("restored"),
                    "items": [{"agent": i["agent"], "id": i["id"], "files": len(i.get("moved") or [])} for i in m.get("items", [])]})
    return out


def _new_claude_copy(data: bytes, src_path: Path) -> tuple[Path, str]:
    """另存一份：换一个新会话号（每行的 sessionId 也换掉），放在同一个项目目录。"""
    new = str(uuid.uuid4())
    lines = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            j = json.loads(line)
            if isinstance(j, dict) and "sessionId" in j:
                j["sessionId"] = new
            lines.append(json.dumps(j, ensure_ascii=False))
        except Exception:
            lines.append(line)
    f = src_path.parent / f"{new}.jsonl"
    _atomic(f, ("\n".join(lines) + "\n").encode("utf-8"))
    return f, new


def trash_restore(batch_id: str, only: list[str] | None = None) -> dict:
    """从回收站还原。原位置空着才放回去；已经有东西了：Claude 记录另存为新会话，其它（登记 / Codex）不覆盖，记成错误。"""
    d = TRASH / batch_id
    if ".." in batch_id or "/" in batch_id or "\\" in batch_id or not (d / "manifest.json").exists():
        return {"ok": False, "error": f"回收站里没有这一批：{batch_id}"}
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    od, led = _new_op("restore", items=[{"agent": i["agent"], "id": i["id"]} for i in m["items"] if not only or i["id"] in only],
                      trash=batch_id)
    restored, errors, copies = 0, [], []
    db = state_db()
    conn = sqlite3.connect(db, timeout=10) if db and any(i["agent"] == "codex" for i in m["items"]) else None
    try:
        for it in m["items"]:
            if only and it["id"] not in only:
                continue
            for mv in it["moved"]:
                src, dst = d / mv["to"], Path(mv["from"])
                if not src.exists():
                    continue
                if dst.exists():
                    if it["agent"] == "claude" and dst.suffix == ".jsonl" and src.is_file():
                        f, new = _new_claude_copy(src.read_bytes(), dst)
                        led["steps"].append({"t": "created", "path": str(f)})
                        copies.append({"id": it["id"], "new_id": new, "path": str(f)})
                    else:
                        errors.append(f"{dst} 已经有了，没覆盖")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), dst)
                led["steps"].append({"t": "move", "from": str(src), "to": str(dst)})
            if it["agent"] == "codex" and it.get("row") and conn is not None:
                row = it["row"]
                with conn:
                    have = _cols(conn)
                    ks = [k for k in row if k in have]
                    cur = conn.execute(f"insert or ignore into threads ({', '.join(ks)}) values ({', '.join('?' * len(ks))})", [row[k] for k in ks])
                    if cur.rowcount:
                        led["steps"].append({"t": "codex-insert", "id": it["id"]})
            restored += 1
    finally:
        if conn is not None:
            conn.close()
    m["restored"] = datetime.now().isoformat(timespec="seconds")
    (d / "manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    led.update(errors=errors, copies=copies, summary=f"从回收站还原 {restored} 条" + (f"（{len(copies)} 条另存为新会话）" if copies else ""))
    _save_led(od, led)
    return {"ok": not errors, "restored": restored, "copies": copies, "errors": errors, "op": od.name}


def trash_purge(batch_id: str, confirm: str = "") -> dict:
    """真正删掉回收站里的一批（不可撤回）：必须 confirm == "DELETE"。备份库里的那份不删。"""
    if confirm != "DELETE":
        return {"ok": False, "error": "要再确认一次（confirm=DELETE）才会真删"}
    d = TRASH / batch_id
    if ".." in batch_id or "/" in batch_id or "\\" in batch_id or not (d / "manifest.json").exists():
        return {"ok": False, "error": f"回收站里没有这一批：{batch_id}"}
    shutil.rmtree(d)
    return {"ok": True, "purged": batch_id}


def restore_snapshot(snap_id: int) -> dict:
    """从备份库恢复一份：原位置没有文件就写回原处，有了就另存（Claude：新会话号；Codex：放进 exports，不动 Codex 的目录）。"""
    rows = [r for r in snapshots(limit=100000) if r["id"] == int(snap_id)]
    if not rows:
        return {"ok": False, "error": "备份库里没有这一份"}
    r = rows[0]
    data = _blob(r["sha"])
    dst = Path(r["path"])
    od, led = _new_op("restore-snapshot", items=[{"agent": r["agent"], "id": r["sid"]}], snap=r["id"])
    if not dst.exists():
        _atomic(dst, data)
        led["steps"].append({"t": "created", "path": str(dst)})
        res = {"ok": True, "path": str(dst), "how": "original"}
    elif r["agent"] == "claude":
        f, new = _new_claude_copy(data, dst)
        led["steps"].append({"t": "created", "path": str(f)})
        res = {"ok": True, "path": str(f), "how": "copy", "new_id": new}
    else:
        EXPORTS.mkdir(parents=True, exist_ok=True)
        f = EXPORTS / f"restored-{dst.name}"
        _atomic(f, data)
        led["steps"].append({"t": "created", "path": str(f)})
        res = {"ok": True, "path": str(f), "how": "export",
               "note": "原位置已有这个线程的记录，另存到了 exports 目录，没有动 Codex 的文件"}
    led["summary"] = f"从备份库恢复（{ {'original': '写回原处', 'copy': '另存为新会话', 'export': '另存到导出目录'}[res['how']] }）"
    _save_led(od, led)
    return dict(res, op=od.name)


# ---------- 撤回 ----------

def undo(op_id: str) -> dict:
    """按账本倒着撤：恢复改过的文件、挪回挪走的、删掉新建的（新建的先挪进回收站，不直接删）、库里的行改回去。"""
    d = OPS / op_id
    if ".." in op_id or "/" in op_id or "\\" in op_id or not (d / "ledger.json").exists():
        return {"ok": False, "error": f"没有这一笔：{op_id}"}
    led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
    if led.get("undone"):
        return {"ok": False, "error": "这一笔已经撤回过了"}
    if led["kind"] == "delete" and led.get("trash"):
        r = trash_restore(led["trash"])
        led["undone"] = datetime.now().isoformat(timespec="seconds")
        _save_led(d, led)
        return dict(r, undo_of=op_id)
    errors = []
    bin_dir = TRASH / f"undo-{op_id}"
    for st in reversed(led["steps"]):
        try:
            t = st["t"]
            if t == "alias":
                with _db_lock:
                    c = _db()
                    if st["old"]:
                        c.execute("insert or replace into aliases values(?,?,?)", (st["k"], st["old"], time.time()))
                    else:
                        c.execute("delete from aliases where k=?", (st["k"],))
                    c.commit()
                    c.close()
            elif t == "file":
                _atomic(Path(st["path"]), (d / "before" / st["before"]).read_bytes())
            elif t == "append":
                p = Path(st["path"])
                if st["size"] is None:
                    if p.exists():
                        bin_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(p), bin_dir / p.name)
                elif p.exists() and p.stat().st_size >= st["size"]:
                    with open(p, "r+b") as fh:
                        fh.truncate(st["size"])
            elif t == "created":
                p = Path(st["path"])
                if p.exists():
                    bin_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), bin_dir / f"{len(list(bin_dir.iterdir())):04d}-{p.name}")
            elif t == "move":
                src, dst = Path(st["to"]), Path(st["from"])
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), dst)
                elif src.exists():
                    errors.append(f"{dst} 已经有了，没挪回")
            elif t == "codex-row":
                db = Path(st["db"]) if st.get("db") else state_db()
                if db and db.exists():
                    conn = sqlite3.connect(db, timeout=10)
                    with conn:
                        for k, v in st["old"].items():
                            conn.execute(f"update threads set {k}=? where id=?", (v, st["id"]))
                    conn.close()
            elif t == "codex-insert":
                db = state_db()
                if db:
                    conn = sqlite3.connect(db, timeout=10)
                    with conn:
                        conn.execute("delete from threads where id=?", (st["id"],))
                    conn.close()
        except Exception as e:
            errors.append(f"{st.get('t')}: {type(e).__name__}: {e}")
    led["undone"] = datetime.now().isoformat(timespec="seconds")
    led["undo_errors"] = errors
    _save_led(d, led)
    return {"ok": not errors, "errors": errors, "undo_of": op_id}


def needs_app(action: str, items: list[dict], write_tool: bool = False) -> set[str]:
    """哪些桌面端开着时不能做（要排队）。"""
    kinds = set()
    for i in items:
        if action in ("archive", "unarchive", "delete", "restore") or (action == "rename" and write_tool):
            kinds.add(i["agent"])
    return kinds


# ---------- 定时自动备份 ----------

UNITS = {"min": 60, "hour": 3600, "day": 86400}
MIN_INTERVAL = 300
SCHED = {"next": None, "last_run": None, "last_result": None, "last_error": None, "last_prune": 0.0}
_run_lock = threading.Lock()
_started = False


def interval_s(s: dict) -> float:
    """设置 → 间隔秒数。0.2.0 留下的 session_backup_hours（没有新键时）照旧认。最少 5 分钟。"""
    every, unit = s.get("session_backup_every"), s.get("session_backup_unit") or "min"
    if s.get("_legacy_hours") is not None:
        every, unit = s["_legacy_hours"], "hour"
    try:
        v = float(every) * UNITS.get(unit, 60)
    except (TypeError, ValueError):
        v = 600
    return max(MIN_INTERVAL, v)


def _low_priority() -> None:
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetThreadPriority(k.GetCurrentThread(), -2)        # THREAD_PRIORITY_LOWEST
        except Exception:
            pass


def auto_tick(s: dict, now: float | None = None) -> dict | None:
    """后台每 30 秒调一次：到点（或启动 / 睡醒后已经过点）就增量备份一次；正在跑就跳过。失败才发通知。"""
    now = now or time.time()
    if not s.get("session_backup_auto"):
        SCHED["next"] = None
        return None
    last = SCHED["last_run"] if SCHED["last_run"] is not None else (backup_status().get("last") or 0)
    SCHED["next"] = last + interval_s(s)
    if now < SCHED["next"]:
        return None
    if not _run_lock.acquire(blocking=False):
        return None                                           # 上一次还没跑完
    try:
        SCHED["last_run"] = now
        try:
            r = backup(why="auto", low=True)
            SCHED.update(last_result={k: r[k] for k in ("files", "skipped", "snapshots", "ms")}, last_error=None)
        except Exception as e:
            SCHED["last_error"] = f"{type(e).__name__}: {e}"
            from . import events
            events.add("backup", "会话自动备份失败", SCHED["last_error"][:200], {"page": "backups"})
            return {"ok": False, "error": SCHED["last_error"]}
        if now - SCHED["last_prune"] > 6 * 3600:
            SCHED["last_prune"] = now
            try:
                prune(s)
            except Exception:
                pass
        SCHED["next"] = now + interval_s(s)
        return r
    finally:
        _run_lock.release()


def backup_now(why: str = "manual") -> dict:
    """「立即备份」：和定时的共用一把锁（正在跑就等它跑完再跑）。"""
    with _run_lock:
        r = backup(why=why)
        SCHED.update(last_run=time.time(), last_result={k: r[k] for k in ("files", "skipped", "snapshots", "ms")}, last_error=None)
        return r


def sched_status(s: dict) -> dict:
    return {"auto": bool(s.get("session_backup_auto")), "interval_s": interval_s(s), "every": s.get("session_backup_every"),
            "unit": s.get("session_backup_unit"), "next": SCHED["next"], "last_run": SCHED["last_run"],
            "last_result": SCHED["last_result"], "last_error": SCHED["last_error"], "running": bool(BK.get("running"))}


def start_auto(get_settings) -> None:
    """后台线程（低优先级）：按设置定时增量备份。get_settings() → 设置字典。"""
    global _started
    if _started or os.environ.get("CC_SESSIONSYNC_NO_BACKUP"):
        return
    _started = True

    def loop():
        _low_priority()
        time.sleep(20)
        while True:
            try:
                auto_tick(get_settings())
            except Exception:
                pass
            time.sleep(30)
    threading.Thread(target=loop, daemon=True, name="session-backup").start()


# ---------- 保留规则 / 清理 ----------

def prune(s: dict, now: float | None = None, vacuum: bool = False) -> dict:
    """最近 retention_all_days 天（缺省 7）的版本全留；再往前到 retention_daily_days 天（缺省 90）每天留最后一版；
    更早的每周留最后一版。每个会话最新的一版永远留着。然后删掉没人用的内容（增量链上被引用的底版留着）。"""
    now = now or time.time()
    all_d = float(s.get("retention_all_days") or 7)
    daily_d = max(all_d, float(s.get("retention_daily_days") or 90))
    before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    with _db_lock:
        c = _db()
        rows = c.execute("select id, agent, sid, at from snaps order by agent, sid, at desc").fetchall()
        drop, keep_bucket, last_key = [], set(), None
        for sid_, agent, sid, at in rows:
            k = (agent, sid)
            if k != last_key:
                last_key, keep_bucket = k, set()
                continue                                       # 最新一版
            age = (now - at) / 86400
            if age <= all_d:
                continue
            day = int(at // 86400)
            b = ("d", day) if age <= daily_d else ("w", day // 7)
            if b in keep_bucket:
                drop.append(sid_)
            else:
                keep_bucket.add(b)
        for i in range(0, len(drop), 500):
            ch = drop[i:i + 500]
            c.execute(f"delete from snaps where id in ({','.join('?' * len(ch))})", ch)
        live = {r[0] for r in c.execute("select distinct sha from snaps")}
        bases = dict(c.execute("select sha, base from blobs"))
        stack = list(live)
        while stack:
            b = bases.get(stack.pop())
            if b and b not in live:
                live.add(b)
                stack.append(b)
        dead = [sh for sh in bases if sh not in live]
        for i in range(0, len(dead), 500):
            ch = dead[i:i + 500]
            c.execute(f"delete from blobs where sha in ({','.join('?' * len(ch))})", ch)
        c.commit()
        if vacuum:
            c.execute("vacuum")
        c.close()
    after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {"ok": True, "snapshots_dropped": len(drop), "blobs_dropped": len(dead), "db_before": before, "db_after": after}

# -*- coding: utf-8 -*-
"""跨应用搜索（0.3.7）：六个应用（Claude Code / Codex / DeepSeek Harness / Cursor / OpenCode / ZCode）的会话一起按
关键词、项目、时间筛，给「会话转换 → 全部应用」用。

- 会话清单（标题 / 目录 / 时间 / 条数）放内存缓存，旧了在后台刷新，翻页 / 改条件不再挨个读各应用；
- 「也搜对话内容」用一个本地索引 ~/.cc-sessionsync/search-index.sqlite：每个会话抽出问 / 答文字（小写、截到 40 万字）存一份，
  按（文件, 修改时间, 大小）增量更新，后台线程建，建的过程中先给已经建好的结果；
- 只读各应用的数据（读法和会话转换完全一样），索引可以随时删掉重建。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from . import paths

AGENTS = ("claude", "codex", "dsh", "cursor", "opencode", "zcode")
DB = paths.HOME / "search-index.sqlite"
TTL = 20.0                     # 清单缓存多久算旧（秒）
MAX_TEXT = 400_000
_lock = threading.RLock()
_meta: dict = {"at": 0.0, "rows": None, "refreshing": False, "ms": 0}
_ft: dict = {"running": False, "done": 0, "total": 0, "at": 0.0, "error": None}


# ---------- 会话清单（内存缓存） ----------

def _collect() -> list[dict]:
    from . import convert, sessions as S
    names = S._aliases()
    org = convert.origins()
    out = []
    for a in AGENTS:
        try:
            rows = convert.sources(a, names)
        except Exception:
            continue
        for x in rows:
            out.append({"agent": a, "id": str(x["id"]), "title": x.get("tool_title") or x.get("title") or "", "cwd": x.get("cwd") or "",
                        "last": x.get("last") or 0, "msgs": x.get("msgs"), "size": x.get("size"), "file": str(x.get("file") or ""),
                        "subagent": bool(x.get("subagent")), "archived": bool(x.get("archived")), "source_name": x.get("source_name"),
                        "origin": convert.origin_of(a, str(x["id"]), x.get("tool_title") or x.get("title"), org)})
    out.sort(key=lambda r: -(r["last"] or 0))
    return out


def _refresh() -> None:
    t0 = time.time()
    try:
        rows = _collect()
        with _lock:
            _meta.update(rows=rows, at=time.time(), ms=int((time.time() - t0) * 1000))
    finally:
        with _lock:
            _meta["refreshing"] = False


def meta(force: bool = False) -> list[dict]:
    """全部会话的清单。第一次 / force 同步读；旧了先给旧的，后台刷新。"""
    with _lock:
        rows, age = _meta["rows"], time.time() - _meta["at"]
        if rows is not None and not force:
            if age > TTL and not _meta["refreshing"]:
                _meta["refreshing"] = True
                threading.Thread(target=_refresh, daemon=True, name="ccss-xsearch-meta").start()
            return rows
        _meta["refreshing"] = True
    _refresh()
    return _meta["rows"] or []


def forget() -> None:
    """转换 / 改名 / 删除之后：下次重新读。"""
    with _lock:
        _meta["at"] = 0.0


# ---------- 全文索引 ----------

def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=10)
    c.execute("create table if not exists docs(key text primary key, agent text, sid text, file text, mtime real, size int, text text)")
    return c


def _text_of(row: dict) -> str:
    from . import convert
    _, items = convert.load_ir(row["agent"], row["id"], {"file": row["file"], "title": row["title"], "cwd": row["cwd"]})
    parts, n = [], 0
    for it in items:
        if it.get("kind") == "text" and it.get("text"):
            parts.append(it["text"])
            n += len(it["text"])
            if n > MAX_TEXT:
                break
    return "\n".join(parts)[:MAX_TEXT].lower()


def _stat(f: str) -> tuple[float, int] | None:
    try:
        st = os.stat(f)
        return st.st_mtime, st.st_size
    except OSError:
        return None


def _build(rows: list[dict]) -> None:
    try:
        c = _db()
        have = {k: (m, s) for k, m, s in c.execute("select key, mtime, size from docs")}
        keys = set()
        todo = []
        for r in rows:
            k = f"{r['agent']}:{r['id']}"
            keys.add(k)
            st = _stat(r["file"]) if r["file"] else None
            if st and have.get(k) != st:
                todo.append((r, st))
        with _lock:
            _ft.update(total=len(rows), done=len(rows) - len(todo))
        gone = [k for k in have if k not in keys]
        if gone:
            c.executemany("delete from docs where key = ?", [(k,) for k in gone])
            c.commit()
        for i, (r, st) in enumerate(todo):
            try:
                t = _text_of(r)
            except Exception:
                t = ""
            c.execute("insert or replace into docs values(?,?,?,?,?,?,?)", (f"{r['agent']}:{r['id']}", r["agent"], r["id"], r["file"], st[0], st[1], t))
            if i % 20 == 19:
                c.commit()
            with _lock:
                _ft["done"] += 1
        c.commit()
        c.close()
        with _lock:
            _ft.update(at=time.time(), error=None)
    except Exception as e:                      # 索引坏了不影响按标题搜
        with _lock:
            _ft["error"] = str(e)
    finally:
        with _lock:
            _ft["running"] = False


def ensure_index(rows: list[dict], wait: float = 0.0) -> dict:
    """后台把全文索引补齐（刚建完 3 秒内不重复建，免得每敲一个字都扫一遍）。
    wait > 0：等正在建的建完，再完整建一遍并等它（测试 / 命令行用）。"""
    if wait:
        t0 = time.time()
        while _ft["running"] and time.time() - t0 < wait:
            time.sleep(0.05)
        with _lock:
            _ft.update(running=True, done=0, total=len(rows))
        th = threading.Thread(target=_build, args=(rows,), daemon=True, name="ccss-xsearch-index")
        th.start()
        th.join(max(0.1, wait - (time.time() - t0)))
        return index_state()
    with _lock:
        if _ft["running"] or time.time() - _ft["at"] < 3:
            return index_state()
        _ft.update(running=True, done=0, total=len(rows))
    threading.Thread(target=_build, args=(rows,), daemon=True, name="ccss-xsearch-index").start()
    return index_state()


def index_state() -> dict:
    with _lock:
        return {"running": _ft["running"], "done": _ft["done"], "total": _ft["total"], "at": _ft["at"], "error": _ft["error"]}


def _content_hits(words: list[str], keys: set[str]) -> dict[str, str]:
    """→ {key: 片段}：对话内容里每个词都出现的会话。"""
    if not words or not DB.exists():
        return {}
    c = _db()
    try:
        q = "select key, text from docs where " + " and ".join(["instr(text, ?) > 0"] * len(words))
        out = {}
        for k, t in c.execute(q, words):
            if k in keys:
                i = t.find(words[0])
                a = max(0, i - 30)
                out[k] = ("…" if a else "") + t[a:i + len(words[0]) + 50].replace("\n", " ") + "…"
        return out
    finally:
        c.close()


# ---------- 搜 ----------

def _norm(p: str) -> str:
    p = (p or "").replace("\\", "/").rstrip("/")
    return p.lower() if os.name == "nt" or (len(p) > 1 and p[1] == ":") else p


def search(q: str = "", agents: list[str] | None = None, project: str = "", since: float | None = None, until: float | None = None,
           content: bool = False, sub: bool = False, limit: int = 500, force: bool = False) -> dict:
    """关键词（空格分开的每个词都要有）+ 应用 + 项目 + 时间（秒）。content=True 时也在对话内容里找（后台建索引，没建完先给建好的）。"""
    t0 = time.time()
    rows = meta(force)
    ag = set(a for a in (agents or AGENTS) if a in AGENTS)
    pj = _norm(project)
    words = [w for w in (q or "").lower().split() if w]
    base = []
    for r in rows:
        if r["agent"] not in ag or (not sub and (r["subagent"] or r["archived"])):
            continue
        if pj and not (_norm(r["cwd"]) == pj or _norm(r["cwd"]).startswith(pj + "/")):
            continue
        last = r["last"] or 0
        if since and last < since or until and last > until:
            continue
        base.append(r)
    hits, snips = [], {}
    if words and content:
        st = ensure_index(rows)
        snips = _content_hits(words, {f"{r['agent']}:{r['id']}" for r in base})
    else:
        st = index_state()
    for r in base:
        hay = f"{r['title']}\n{r['cwd']}\n{r['id']}".lower()
        k = f"{r['agent']}:{r['id']}"
        if not words or all(w in hay for w in words):
            hits.append(dict(r, hit="title" if words else None))
        elif k in snips:
            hits.append(dict(r, hit="content", snippet=snips[k]))
    per = {a: 0 for a in AGENTS}
    for r in hits:
        per[r["agent"]] += 1
    projects: dict[str, int] = {}
    for r in rows:
        if r["cwd"] and r["agent"] in ag:
            projects[r["cwd"]] = projects.get(r["cwd"], 0) + 1
    out = [{k: v for k, v in r.items() if k != "file"} for r in hits[:limit]]
    return {"rows": out, "total": len(hits), "all": len(rows), "per_app": per, "limit": limit,
            "projects": sorted(projects.items(), key=lambda x: -x[1])[:200], "index": st if content and words else index_state(),
            "ms": int((time.time() - t0) * 1000), "meta_ms": _meta["ms"], "stale": time.time() - _meta["at"] > TTL}

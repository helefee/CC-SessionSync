# -*- coding: utf-8 -*-
"""会话转换（0.2.7）：任意应用的会话 → 任意应用。

  来源：Claude Code、Codex、DeepSeek Harness（dsh）、Cursor、OpenCode、ZCode —— 都先读成公共的中间格式（见 ir.py）。
  目标：
    原生写入（新建会话，在目标应用里能直接列出来、接着聊）：
      Claude Code  ~/.claude/projects/<目录>/<新会话号>.jsonl（可选登记进桌面端列表；桌面端开着就排队等它退出）
      Codex        <CODEX_HOME>/sessions/YYYY/MM/DD/rollout-…-<新线程号>.jsonl + session_index.jsonl 里一行标题（Codex 桌面端开着就排队）
      dsh          新的 dsh 会话目录（第 4 版格式，见 dsh.write_session）
      OpenCode     交给 OpenCode 自己的命令行 `opencode session import`（OpenCode 开着就排队；没有命令行就只能导出）
    导出上下文文件（目标应用的库没法安全地写）：Cursor、ZCode —— 一份 Markdown + 一句粘贴提示，标得清清楚楚。
  每次都是只建新的，已有的会话一个字节都不改。先演练（dry），真做的时候记账本，能撤回；记着谁是从哪来的，
  不会把从 X 导过来的会话再导回 X（loop）。来源的数据只读。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import ir, ocstore, paths

LEDGERS = paths.HOME / "convert"
MAP = paths.HOME / "convert-map.json"          # "src:id>dst" → {id, file, sha, src_sig, title, cwd, time, ledger}
EXPORTS = paths.HOME / "exports" / "convert"
AGENTS = ir.AGENTS
NATIVE = ("claude", "codex", "dsh", "opencode")
EXPORT_ONLY = {"cursor": "Cursor 的对话在它自己的 SQLite 里（state.vscdb / store.db），格式没公开、还带校验，写进去不安全",
               "zcode": "ZCode 是闭源的，会话库还连着迁移账本和桌面端的任务索引，写进去没法验证它认不认"}
PASTE = {
    "cursor": "在 Cursor 的聊天框里输入 @ 选中这个文件（或把文件拖进去），然后发：「请先读完这份之前的对话记录，再接着做最后一个没做完的事。」",
    "zcode": "在 ZCode 里新建一个任务，把这个文件拖进对话框（或用 @ 引用它），然后发：「请先读完这份之前的对话记录，再接着做最后一个没做完的事。」",
}
PASTE_ANY = "在目标应用里新开一个对话，把这个文件拖进去（或 @ 引用），然后发：「请先读完这份之前的对话记录，再接着做最后一个没做完的事。」"


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


def _sha(f: Path) -> str | None:
    try:
        return hashlib.sha256(Path(f).read_bytes()).hexdigest()
    except OSError:
        return None


def _shab(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------- 方向表 ----------

def mode(dst: str) -> tuple[str, str | None]:
    """→ ("native" | "export", 为什么)。"""
    if dst in EXPORT_ONLY:
        return "export", EXPORT_ONLY[dst]
    if dst == "opencode" and not ocstore.find_cli():
        return "export", "没找到 OpenCode 的命令行（opencode / 桌面版带的 opencode-cli），只能导出上下文文件"
    return "native", None


def matrix() -> dict:
    cols = {}
    for d in AGENTS:
        m, why = mode(d)
        cols[d] = {"mode": m, "why": why}
    cells = {s: {d: ("same" if s == d else cols[d]["mode"]) for d in AGENTS} for s in AGENTS}
    return {"agents": list(AGENTS), "labels": ir.LABEL, "targets": cols, "cells": cells,
            "how": {"claude": "写新的 Claude Code 会话（jsonl），`claude --resume` 能接着聊",
                    "codex": "写新的 Codex rollout + 标题，`codex resume` 能接着聊",
                    "dsh": "写新的 dsh 会话目录，`dsh --session-id` 能接着聊",
                    "opencode": "用 OpenCode 自己的命令行导入新会话，`opencode --session` 能接着聊",
                    "cursor": "导出上下文文件 + 粘贴提示（不写 Cursor 的库）",
                    "zcode": "导出上下文文件 + 粘贴提示（不写 ZCode 的库）"}}


# ---------- 来源：列表 / 读成中间格式 ----------

def sources(agent: str, names: dict | None = None) -> list[dict]:
    from . import sessions as S
    if agent == "claude":
        rows = S.claude_sessions(names)
    elif agent == "codex":
        rows = S.codex_sessions(names)
    elif agent == "dsh":
        rows = S.dsh_sessions(names)
    elif agent == "cursor":
        rows = S.cursor_sessions(names)
    elif agent in ("opencode", "zcode"):
        try:
            rows = ocstore.sessions(agent, names if names is not None else S._aliases())
        except Exception:
            rows = []
    else:
        raise ValueError(f"不认识的应用 {agent}")
    return [r for r in rows if r.get("file") and not r.get("orphan")]


def _claude_ir(f: Path) -> tuple[dict, list[dict]]:
    items, calls = [], {}
    h = {"agent": "claude", "id": f.stem, "cwd": None, "title": None, "created": None}
    SKIP = ("<command-name>", "<command-message>", "<local-command-", "Caveat: The messages below", "<system-reminder>")
    for ln in f.read_bytes().splitlines():
        d = ir._json(ln)
        if not isinstance(d, dict):
            continue
        if d.get("type") == "custom-title" and d.get("customTitle"):
            h["title"] = d["customTitle"]
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        h["cwd"] = h["cwd"] or d.get("cwd")
        ts = ir.to_ms(d.get("timestamp"))
        h["created"] = h["created"] or ts
        m = d.get("message") or {}
        c = m.get("content")
        if d["type"] == "user":
            if isinstance(c, str):
                if c.strip() and not c.lstrip().startswith(SKIP):
                    items.append({"role": "user", "kind": "text", "text": c, "ts": ts})
                continue
            texts, imgs = [], 0
            for b in c or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text") and not b["text"].lstrip().startswith(SKIP):
                    texts.append(b["text"])
                elif b.get("type") == "image":
                    imgs += 1
                elif b.get("type") == "tool_result":
                    it = calls.get(b.get("tool_use_id"))
                    if it is not None:
                        rc = b.get("content")
                        it["output"] = rc if isinstance(rc, str) else "\n".join(
                            x.get("text", "") if x.get("type") == "text" else "[图片]" for x in rc or [] if isinstance(x, dict))
                        if b.get("is_error"):
                            it["status"] = "error"
            if texts or imgs:
                items.append({"role": "user", "kind": "text", "text": "\n\n".join(texts), "ts": ts, "images": imgs or None})
        else:
            mdl = m.get("model") if m.get("model") not in (None, "<synthetic>") else None
            for b in c if isinstance(c, list) else [{"type": "text", "text": c}] if isinstance(c, str) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    items.append({"role": "assistant", "kind": "text", "text": b["text"], "ts": ts, "model": mdl})
                elif b.get("type") == "thinking" and b.get("thinking"):
                    items.append({"role": "assistant", "kind": "thinking", "text": b["thinking"], "ts": ts, "model": mdl})
                elif b.get("type") == "tool_use":
                    it = {"role": "assistant", "kind": "tool", "tool": b.get("name"), "input": b.get("input"), "ts": ts, "model": mdl,
                          "status": "completed"}
                    items.append(it)
                    calls[b.get("id")] = it
    return h, items


def _codex_ir(f: Path) -> tuple[dict, list[dict]]:
    from . import codex_import as CI
    p = CI.parse(f, reasoning=True)
    meta = p["meta"] or {}
    h = {"agent": "codex", "id": meta.get("id") or f.stem[-36:], "cwd": CI.norm_cwd(meta.get("cwd")) or None,
         "created": ir.to_ms(meta.get("timestamp"))}
    items = []
    for x in p["items"]:
        ts = ir.to_ms(x.get("ts"))
        if x["role"] == "user":
            items.append({"role": "user", "kind": "text", "text": x["text"], "ts": ts})
        elif x["role"] == "assistant":
            items.append({"role": "assistant", "kind": "text", "text": x["text"], "ts": ts})
        elif x["role"] == "reasoning":
            items.append({"role": "assistant", "kind": "thinking", "text": x["text"], "ts": ts})
        elif x["role"] == "tool":
            items.append({"role": "assistant", "kind": "tool", "tool": x.get("name"), "input": x.get("args"), "output": x.get("output"),
                          "ts": ts})
    return h, items


def _dsh_ir(f: Path) -> tuple[dict, list[dict]]:
    from . import dsh
    L = dsh.load(f)
    hd = L["header"]
    h = {"agent": "dsh", "id": hd.get("id"), "cwd": hd.get("cwd"), "created": ir.to_ms(hd.get("createdAt"))}
    h["title"] = next((e["data"].get("title") for e in reversed(L["events"])
                       if e.get("type") == "session/title" and (e.get("data") or {}).get("title")), None)
    items, open_calls = [], {}
    for e in dsh.surface(L["events"]):
        t, ts = e.get("type"), ir.to_ms(e.get("time"))
        m = dsh._msg(e)
        if t == "user/message":
            if (m.get("source") or {}).get("kind") != "user":
                continue
            texts = [b["text"] for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            imgs = sum(1 for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "image")
            if texts or imgs:
                items.append({"role": "user", "kind": "text", "text": "\n\n".join(texts), "ts": ts, "images": imgs or None})
        elif t == "assistant/message":
            mdl = m.get("model") if isinstance(m.get("model"), str) else None
            for b in m.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    items.append({"role": "assistant", "kind": "text", "text": b["text"], "ts": ts, "model": mdl})
                elif b.get("type") == "reasoning" and b.get("text"):
                    items.append({"role": "assistant", "kind": "thinking", "text": b["text"], "ts": ts, "model": mdl})
                elif b.get("type") == "tool-call":
                    a = b.get("arguments")
                    it = {"role": "assistant", "kind": "tool", "tool": b.get("name"), "input": ir._json(a) if isinstance(a, str) and ir._json(a) is not None else a,
                          "ts": ts, "model": mdl, "status": "completed"}
                    items.append(it)
                    open_calls[b.get("id")] = it
        elif t == "tool/result":
            it = open_calls.get(m.get("toolCallId"))
            if it is not None:
                it["output"] = dsh._txt(m.get("content"))
                if m.get("isError"):
                    it["status"] = "error"
    return h, items


def load_ir(agent: str, sid: str, row: dict | None = None) -> tuple[dict, list[dict]]:
    """→ (头, 条目)。来源只读。"""
    if row is None:
        row = next((r for r in sources(agent) if r["id"] == sid), None)
        if row is None:
            raise FileNotFoundError(f"找不到 {ir.LABEL.get(agent, agent)} 会话 {sid}")
    f = Path(row["file"])
    if agent == "claude":
        h, items = _claude_ir(f)
    elif agent == "codex":
        h, items = _codex_ir(f)
    elif agent == "dsh":
        h, items = _dsh_ir(f)
    else:                                   # cursor / opencode / zcode：本程序的镜像就是中间格式
        h, items = ir.load(f.read_bytes())
        h["agent"] = agent
    h["id"] = h.get("id") or sid
    h["title"] = row.get("tool_title") or row.get("title") or h.get("title") or ir.title_of(items)
    h["cwd"] = h.get("cwd") or row.get("cwd")
    if agent == "cursor" and row.get("source_name"):
        h["source_name"] = row["source_name"]
    return h, items


# ---------- 从哪来的（防止转回去） ----------

def origins() -> dict:
    """"dst:id" → 来源应用。本模块的记录 + 以前各导入功能的记录。"""
    out = {}
    for k, v in _load(MAP, {}).items():
        try:
            src, dst = k.split(">", 1)
            out[f"{dst}:{v.get('id')}"] = src.split(":", 1)[0]
        except ValueError:
            continue
    for k, v in _load(paths.HOME / "codex-import-map.json", {}).items():
        if isinstance(v, dict) and v.get("session"):
            out[f"claude:{v['session']}"] = "codex"
    for k, v in _load(paths.HOME / "cursor-import-map.json", {}).items():
        if k.startswith("cursor2cc:") and isinstance(v, dict) and v.get("session"):
            out[f"claude:{v['session']}"] = "cursor"
    for k, v in _load(paths.HOME / "dsh-import-map.json", {}).items():
        if not isinstance(v, dict):
            continue
        if k.startswith("dsh2cc:") and v.get("session"):
            out[f"claude:{v['session']}"] = "dsh"
        elif k.startswith("cc2dsh:") and v.get("dsh"):
            out[f"dsh:{v['dsh']}"] = "claude"
    return out


def origin_of(agent: str, sid: str, title: str | None, org: dict | None = None) -> str | None:
    org = org if org is not None else origins()
    o = org.get(f"{agent}:{sid}")
    if o:
        return o
    t = title or ""
    for a, p in ir.PREFIX.items():
        if t.startswith(p) and a != agent:
            return a
    return None


# ---------- 演练 ----------

def _ir_sig(h: dict, items: list[dict]) -> str:
    return _shab(json.dumps(items, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _exists(dst: str, rec: dict) -> bool:
    if dst == "opencode":
        return any(r["id"] == rec.get("id") for r in ocstore.refresh("opencode"))
    return bool(rec.get("file")) and Path(rec["file"]).exists()


def plan(src: str, ids: list[str], dst: str, again: bool = False) -> dict:
    if src not in AGENTS or dst not in AGENTS:
        raise ValueError("来源 / 目标只能是 " + " / ".join(AGENTS))
    if src == dst:
        raise ValueError("来源和目标是同一个应用")
    m, why = mode(dst)
    rows_all = {r["id"]: r for r in sources(src)}
    mp, org = _load(MAP, {}), origins()
    rows = []
    for sid in ids:
        r = rows_all.get(sid)
        if r is None:
            rows.append({"id": sid, "state": "missing", "error": "找不到这个会话"})
            continue
        row = {"id": sid, "title": r.get("title"), "cwd": r.get("cwd"), "last": r.get("last"), "msgs": r.get("msgs")}
        o = origin_of(src, sid, r.get("tool_title") or r.get("title"), org)
        row["origin"] = o
        try:
            h, items = load_ir(src, sid, r)
        except Exception as e:
            rows.append(dict(row, state="error", error=f"{type(e).__name__}: {e}"))
            continue
        sig = _ir_sig(h, items)
        st = ir.stats_of(items)
        row.update(stats=st, sig=sig)
        rec = mp.get(f"{src}:{sid}>{dst}")
        if o == dst and m == "native":
            row["state"] = "loop"                        # 本来就是从目标应用导过来的
        elif m == "export":
            row["state"] = "new"
        elif rec and _exists(dst, rec):
            row.update(target_id=rec.get("id"), target_file=rec.get("file"))
            row["state"] = "same" if rec.get("src_sig") == sig else ("new" if again else "changed")
        else:
            row["state"] = "new"
        if not (st["user"] or st["assistant"]):
            row.update(state="empty")
        rows.append(row)
    cnt = {}
    for r in rows:
        cnt[r["state"]] = cnt.get(r["state"], 0) + 1
    return {"ok": True, "src": src, "dst": dst, "mode": m, "why": why, "rows": rows, "counts": cnt,
            "paste": PASTE.get(dst, PASTE_ANY) if m == "export" else None, "running": bool(_running(dst))}


def _running(dst: str) -> list:
    from . import apps
    if dst in ("codex", "opencode", "zcode"):
        try:
            return apps.running(dst)
        except Exception:
            return []
    return []


def needs_close(dst: str, desktop: bool = True) -> str | None:
    """真做之前要先等哪个应用退出（None = 不用等）。"""
    from . import apps
    m, _ = mode(dst)
    if m == "export":
        return None
    if dst == "claude" and desktop and apps.running("claude"):
        return "claude"
    if dst in ("codex", "opencode") and apps.running(dst):
        return dst
    return None


# ---------- 写入器 ----------

def _uuid7() -> str:
    ms = int(time.time() * 1000)
    b = ms.to_bytes(6, "big") + bytes([0x70 | random.randrange(16), random.randrange(256)]) + \
        bytes([0x80 | random.randrange(64)]) + os.urandom(7)
    return str(uuid.UUID(bytes=b))


def _write_new(f: Path, text: str) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        raise FileExistsError(f"{f} 已经有了，不覆盖")
    tmp = f.with_name(f.name + ".cc-sessionsync.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, f)


def _to_claude(h, items, opts, led) -> dict:
    from . import codex_import as CI
    sid = str(uuid.uuid4())
    b = ir.build_claude(h, items, sid, CI.claude_version(), reasoning=opts.get("reasoning", False),
                        cwd=CI.norm_cwd(h.get("cwd")) or str(Path.home()))
    jf = paths.CLAUDE_HOME / "projects" / CI.project_slug(b["cwd"]) / f"{sid}.jsonl"
    _write_new(jf, CI._dump(b["records"]))
    out = {"id": sid, "file": str(jf), "sha": _sha(jf), "title": b["title"], "cwd": b["cwd"], "turns": b["turns"], "stats": b["stats"],
           "resume": CI.resume_cmd(b["cwd"], sid)}
    if opts.get("desktop", True):
        try:
            dt = CI._desktop_target(opts.get("to"))
        except Exception:
            dt = None
        if dt:
            tdir = Path(dt["path"])
            d = CI._desktop_entry(tdir, sid, b["cwd"], b["title"], time.time(), None, prefix=ir.PREFIX.get(h.get("agent")))
            ef = tdir / f"{d['sessionId']}.json"
            _write_new(ef, json.dumps(d, ensure_ascii=False, indent=1))
            out.update(desktop_file=str(ef), desktop_sha=_sha(ef), desktop=dt["key"])
    return out


def codex_cli_version() -> str:
    from . import codex_import as CI
    try:
        for f in sorted((CI.codex_home() / "sessions").rglob("rollout-*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            with open(f, encoding="utf-8", errors="replace") as fh:
                d = ir._json(fh.readline()) or {}
            v = (d.get("payload") or {}).get("cli_version")
            if v:
                return v
    except OSError:
        pass
    return "0.130.0"


def _to_codex(h, items, opts, led) -> dict:
    from . import codex_import as CI
    tid = _uuid7()
    cwd = CI.norm_cwd(h.get("cwd")) or str(Path.home())
    b = ir.build_codex(h, items, tid, codex_cli_version(), reasoning=opts.get("reasoning", False), cwd=cwd)
    now = datetime.now()
    f = CI.codex_home() / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{tid}.jsonl"
    _write_new(f, "\n".join(b["lines"]) + "\n")
    idx = CI.codex_home() / "session_index.jsonl"
    line = json.dumps({"id": tid, "thread_name": b["title"], "updated_at": ir.iso_ms(None)}, ensure_ascii=False)
    with open(idx, "a", encoding="utf-8", newline="\n") as fh:     # 只追加一行（Codex 自己改名也是这样追加）
        fh.write(line + "\n")
    q = f'"{cwd}"' if any(c in cwd for c in " &()") else cwd
    return {"id": tid, "file": str(f), "sha": _sha(f), "title": b["title"], "cwd": cwd, "turns": b["turns"], "stats": b["stats"],
            "index": str(idx), "index_line": line, "resume": f"cd {q} && codex resume {tid}"}


def _to_dsh(h, items, opts, led) -> dict:
    from . import codex_import as CI
    from . import dsh
    wr = dsh.write_root()
    tmp = Path(tempfile.mkdtemp(prefix="ccss-conv-"))
    try:
        cwd = CI.norm_cwd(h.get("cwd")) or str(Path.home())
        b = ir.build_claude(h, items, str(uuid.uuid4()), CI.claude_version(), reasoning=opts.get("reasoning", False), cwd=cwd)
        b["records"] = [r for r in b["records"] if r.get("type") != "custom-title"]
        cf = tmp / "x.jsonl"
        cf.write_text(CI._dump(b["records"]), encoding="utf-8")
        pre = ir.PREFIX.get(h.get("agent"), "")
        d = dsh.build_dsh(cf, title=h.get("title") or ir.title_of(items) or "对话", note=False, prefix=pre)
        title = pre + d["title"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    f = dsh.write_session(Path(wr["root"]), wr["compression"], d["header"], d["events"])
    return {"id": d["id"], "file": str(f), "dir": str(f.parent), "sha": _sha(f), "title": title, "cwd": d["cwd"], "turns": d["turns"],
            "stats": d["stats"], "resume": dsh.resume_cmd(d["cwd"], d["id"])}


def _to_opencode(h, items, opts, led) -> dict:
    from . import codex_import as CI
    cli = ocstore.find_cli()
    if not cli:
        raise RuntimeError("没找到 OpenCode 的命令行")
    major = ocstore.cli_major(cli)
    cwd = CI.norm_cwd(h.get("cwd")) or str(Path.home())
    b = ir.build_opencode(h, items, reasoning=opts.get("reasoning", False), cwd=cwd, version=2 if major >= 2 else 1)
    r = ocstore.import_session(b["data"], cwd, cli)
    if not r.get("ok"):
        raise RuntimeError(f"{r.get('error')}：{(r.get('out') or '').strip()[-200:]}")
    if not ocstore.has_session(b["id"]):
        raise RuntimeError("opencode 说导入成功了，但库里没找到这个会话")
    q = f'"{cwd}"' if any(c in cwd for c in " &()") else cwd
    return {"id": b["id"], "file": None, "title": b["title"], "cwd": cwd, "turns": b["turns"], "stats": b["stats"], "cli": cli,
            "cli_major": major, "msgs": len(b["data"]["messages"]), "resume": f"cd {q} && opencode --session {b['id']}"}


WRITERS = {"claude": _to_claude, "codex": _to_codex, "dsh": _to_dsh, "opencode": _to_opencode}


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "-", s or "").strip("-")[:40] or "session"


def _export(h, items, dst, opts) -> dict:
    md = ir.context_md(h, items, dst)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{h.get('agent')}-{_safe(h.get('id') or '')[:12]}-{_safe(md['title'])}.md"
    f = EXPORTS / dst / name
    if opts.get("project") and h.get("cwd") and Path(h["cwd"]).is_dir():
        f = Path(h["cwd"]) / ".cc-sessionsync" / name
    _write_new(f, md["text"])
    return {"id": None, "file": str(f), "sha": _sha(f), "title": md["title"], "cwd": md["cwd"], "msgs": md["msgs"], "export": True,
            "paste": PASTE.get(dst, PASTE_ANY), "chars": len(md["text"])}


# ---------- 真做 ----------

def _new_ledger(what: str) -> tuple[Path, dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = LEDGERS / stamp
    while d.exists():
        d = d.with_name(d.name + "x")
    d.mkdir(parents=True)
    return d, {"what": what, "time": time.time(), "created": [], "map_before": {}, "items": [], "errors": []}


def run(src: str, ids: list[str], dst: str, dry: bool = True, again: bool = False, reasoning: bool = False,
        desktop: bool = True, to: str | None = None, project: bool = False, export: bool = False) -> dict:
    """src 的这几个会话 → dst。dry=True 只演练（算出要写什么，不写）。export=True：能原生写也只导出上下文文件。"""
    p = plan(src, ids, dst, again)
    if export and p["mode"] == "native":
        p.update(mode="export", why="选了只导出上下文文件", paste=PASTE.get(dst, PASTE_ANY))
        for r in p["rows"]:
            if r["state"] in ("same", "changed", "loop"):
                r["state"] = "new"
    opts = {"reasoning": reasoning, "desktop": desktop, "to": to, "project": project}
    todo = [r for r in p["rows"] if r["state"] == "new"]
    if dry or not todo:
        for r in todo:
            r["target"] = (f"上下文文件 → {EXPORTS / dst}" if p["mode"] == "export" else
                           {"claude": "Claude Code 新会话", "codex": "Codex 新线程", "dsh": "dsh 新会话目录",
                            "opencode": "OpenCode 新会话（命令行导入）"}.get(dst))
        return dict(p, dry=True, done=0)
    mp = _load(MAP, {})
    led_dir, led = _new_ledger(f"{src}-to-{dst}")
    led.update(src=src, dst=dst, mode=p["mode"])
    done = 0
    for r in todo:
        k = f"{src}:{r['id']}>{dst}"
        try:
            h, items = load_ir(src, r["id"])
            out = _export(h, items, dst, opts) if p["mode"] == "export" else WRITERS[dst](h, items, opts, led)
            out.update(agent=dst, src_id=r["id"])
            led["created"].append(out)
            if p["mode"] == "native":
                led["map_before"][k] = mp.get(k)
                mp[k] = {"id": out["id"], "file": out.get("file"), "sha": out.get("sha"), "src_sig": r.get("sig"), "title": out["title"],
                         "cwd": out.get("cwd"), "time": time.time(), "ledger": led_dir.name}
                _save(MAP, mp)
            r.update({x: out.get(x) for x in ("id", "file", "title", "resume", "paste", "turns")}, id=r["id"], target_id=out.get("id"),
                     done=True)
            done += 1
        except Exception as e:
            led["errors"].append(f"{r['id']}：{type(e).__name__}: {e}")
            r["error"] = f"{type(e).__name__}: {e}"
        _save(led_dir / "ledger.json", led)
    _save(led_dir / "ledger.json", led)
    try:
        from . import events
        if done:
            events.add("import", f"已把 {done} 个 {ir.LABEL[src]} 会话" + ("导出成给 " if p["mode"] == "export" else "转成 ") +
                       f"{ir.LABEL[dst]} 的" + ("上下文文件" if p["mode"] == "export" else "会话"), "撤回在「会话转换」页的记录里",
                       {"page": "convert"})
    except Exception:
        pass
    return dict(p, dry=False, done=done, errors=led["errors"], ledger=led_dir.name, ledger_path=str(led_dir))


# ---------- 账本 / 撤回 ----------

def ledgers() -> list[dict]:
    out = []
    if not LEDGERS.is_dir():
        return out
    for d in sorted(LEDGERS.iterdir(), reverse=True):
        led = _load(d / "ledger.json", None)
        if not isinstance(led, dict):
            continue
        out.append({"name": d.name, "what": led.get("what"), "src": led.get("src"), "dst": led.get("dst"), "mode": led.get("mode"),
                    "time": led.get("time"), "count": len(led.get("created") or []), "errors": len(led.get("errors") or []),
                    "undone": led.get("undone"), "items": [{k: c.get(k) for k in ("src_id", "id", "title", "file", "resume", "export")}
                                                           for c in led.get("created") or []]})
    return out


def undo_needs_close(name: str) -> str | None:
    from . import apps
    led = _load(LEDGERS / name / "ledger.json", {}) or {}
    dst = led.get("dst")
    if led.get("mode") == "native" and dst in ("claude", "codex", "opencode"):
        if dst == "claude" and not any(c.get("desktop_file") for c in led.get("created") or []):
            return None
        return dst if apps.running(dst) else None
    return None


def undo(name: str) -> dict:
    """删掉这次新建的（只删没动过的：你在目标应用里接着聊过的那份留着），转换记录改回去。"""
    if not re.fullmatch(r"[0-9x-]{8,40}", name or ""):
        return {"ok": False, "error": "账本名不对"}
    d = LEDGERS / name
    led = _load(d / "ledger.json", None)
    if not isinstance(led, dict):
        return {"ok": False, "error": "没有这个账本"}
    if led.get("undone"):
        return {"ok": False, "error": "已经撤回过了"}
    removed, kept, errs = [], [], []
    for c in led.get("created") or []:
        a = c.get("agent")
        try:
            if a == "opencode" and not c.get("export"):
                rows = {r["id"]: r for r in ocstore.refresh("opencode", force=True)}
                cur = rows.get(c["id"])
                if cur is None:
                    removed.append(c["id"])
                elif (cur.get("msgs") or 0) > (c.get("turns") or 0):
                    kept.append({"id": c["id"], "why": "在 OpenCode 里接着聊过，没删"})
                else:
                    r = ocstore.delete_session(c["id"], c.get("cli"))
                    (removed.append(c["id"]) if r.get("ok") else errs.append(f"{c['id']}：{r.get('error')}"))
                continue
            f = Path(c["file"]) if c.get("file") else None
            if f and f.exists():
                if _sha(f) != c.get("sha"):
                    kept.append({"id": c.get("id") or str(f), "why": "之后又改过（接着聊过），没删"})
                    continue
                if a == "dsh" and c.get("dir") and not c.get("export"):
                    shutil.rmtree(c["dir"])
                else:
                    f.unlink()
            removed.append(c.get("id") or str(f))
            ef = Path(c["desktop_file"]) if c.get("desktop_file") else None
            if ef and ef.exists() and _sha(ef) == c.get("desktop_sha"):
                ef.unlink()
        except Exception as e:
            errs.append(f"{c.get('id')}：{type(e).__name__}: {e}")
    mp = _load(MAP, {})
    for k, v in (led.get("map_before") or {}).items():
        if v is None:
            mp.pop(k, None)
        else:
            mp[k] = v
    _save(MAP, mp)
    led.update(undone=time.time(), undo={"removed": removed, "kept": kept, "errors": errs})
    _save(d / "ledger.json", led)
    return {"ok": not errs, "removed": len(removed), "kept": kept, "errors": errs, "ledger": name}

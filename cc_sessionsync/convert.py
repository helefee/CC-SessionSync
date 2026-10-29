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
            texts, imgs, fls, data = [], 0, 0, []
            for b in c or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text") and not b["text"].lstrip().startswith(SKIP):
                    texts.append(b["text"])
                elif b.get("type") == "image":
                    imgs += 1
                    srcb = b.get("source") or {}
                    im = ir.img(srcb.get("media_type"), srcb.get("data")) if srcb.get("type") == "base64" else None
                    if im:
                        data.append(im)
                elif b.get("type") == "document":
                    fls += 1
                elif b.get("type") == "tool_result":
                    it = calls.get(b.get("tool_use_id"))
                    if it is not None:
                        rc = b.get("content")
                        it["output"] = rc if isinstance(rc, str) else "\n".join(
                            x.get("text", "") if x.get("type") == "text" else "[图片]" for x in rc or [] if isinstance(x, dict))
                        if b.get("is_error"):
                            it["status"] = "error"
            if texts or imgs or fls:
                items.append({"role": "user", "kind": "text", "text": "\n\n".join(texts), "ts": ts, "images": imgs or None, "files": fls or None,
                              "image_data": data or None})
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
    p = CI.parse(f, reasoning=True, images=True)
    meta = p["meta"] or {}
    h = {"agent": "codex", "id": meta.get("id") or f.stem[-36:], "cwd": CI.norm_cwd(meta.get("cwd")) or None,
         "created": ir.to_ms(meta.get("timestamp"))}
    items = []
    for x in p["items"]:
        ts = ir.to_ms(x.get("ts"))
        if x["role"] == "user":
            items.append({"role": "user", "kind": "text", "text": x["text"], "ts": ts, "images": len(x.get("images") or []) or None,
                          "image_data": [im for im in (ir.img_url(u) for u in x.get("images") or []) if im] or None})
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
            imgs = [b for b in m.get("content") or [] if isinstance(b, dict) and b.get("type") == "image"]
            data = []
            for b in imgs:
                raw, mt = dsh._attachment(b.get("attachment") or {})
                im = ir.img(mt, raw) if raw is not None else None
                if im:
                    data.append(im)
            if texts or imgs:
                items.append({"role": "user", "kind": "text", "text": "\n\n".join(texts), "ts": ts, "images": len(imgs) or None,
                              "image_data": data or None})
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
        if agent in ("opencode", "zcode") and any(it.get("images") for it in items):
            try:                            # 镜像里只记了张数；原图现从库里（只读快照）拿
                per = ocstore.images(agent, h.get("id") or sid)
                users = [it for it in items if it["role"] == "user" and it.get("images")]
                if len(per) == len(users):
                    for it, ims in zip(users, per):
                        it["image_data"] = [im for im in (ir.img(x.get("mt"), x.get("b64")) for x in ims) if im] or None
            except Exception:
                pass
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
    # 0.3.6 加的 "files": None 不算进指纹，免得升级后老转换全变成「源会话改过」
    its = [{k: v for k, v in i.items() if not (k == "files" and v is None)} for i in items]
    return _shab(json.dumps(its, ensure_ascii=False, sort_keys=True).encode("utf-8"))


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
        row.update(stats=st, sig=sig, facts=_facts(items), cwd=h.get("cwd") or row.get("cwd"))
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
    """0.4.1：直接从中间格式拼 dsh 会话（工具调用 / 图片 / 思考按 dsh 自己的样子写）；图片放进 dsh 的附件库，这次新放进去的记在账本里，撤回时一起删。"""
    from . import codex_import as CI
    from . import dsh
    wr = dsh.write_root()
    h = dict(h, cwd=CI.norm_cwd(h.get("cwd")) or str(Path.home()))
    pre = ir.PREFIX.get(h.get("agent"), "")
    d = dsh.build_from_ir(h, items, reasoning=opts.get("reasoning", False), prefix=pre, title=h.get("title") or ir.title_of(items) or "对话")
    f = dsh.write_session(Path(wr["root"]), wr["compression"], d["header"], d["events"])
    att = []
    for raw in d.get("attachments") or []:
        _ref, af, new = dsh.put_attachment(raw)
        if new:
            att.append({"file": str(af), "sha": _sha(af)})
    return {"id": d["id"], "file": str(f), "dir": str(f.parent), "sha": _sha(f), "title": pre + d["title"], "cwd": d["cwd"], "turns": d["turns"],
            "stats": d["stats"], "resume": dsh.resume_cmd(d["cwd"], d["id"]), "attachments": att}


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
    row = next((x for x in ocstore.refresh("opencode", force=True) if x["id"] == b["id"]), None)
    if row is None:
        raise RuntimeError("opencode 说导入成功了，但库里没找到这个会话")
    q = f'"{cwd}"' if any(c in cwd for c in " &()") else cwd
    return {"id": b["id"], "file": None, "title": b["title"], "cwd": cwd, "turns": b["turns"], "stats": b["stats"], "cli": cli,
            "cli_major": major, "msgs": len(b["data"]["messages"]), "base_msgs": row.get("msgs"), "resume": f"cd {q} && opencode --session {b['id']}"}


WRITERS = {"claude": _to_claude, "codex": _to_codex, "dsh": _to_dsh, "opencode": _to_opencode}


def check_written(dst: str, out: dict) -> dict:
    """转换后自检（0.4.1）：按目标应用读会话的规矩把刚写的再读一遍（nativecheck）。出错不影响已经写好的会话，只报出来。"""
    from . import nativecheck as NC
    want = NC.expect(out.get("stats") or {})
    try:
        if dst == "claude":
            return NC.check_claude(Path(out["file"]), want)
        if dst == "codex":
            return NC.check_codex(Path(out["file"]), want, Path(out["index"]) if out.get("index") else None)
        if dst == "dsh":
            return NC.check_dsh(Path(out["file"]), want)
        if dst == "opencode":
            return NC.check_opencode(out["id"], out.get("cli"), out.get("cli_major") or 2, want, out.get("cwd"))
    except Exception as e:
        return {"ok": False, "issues": [f"自检本身出错：{type(e).__name__}: {e}"], "by": "自检"}
    return {"ok": True, "issues": [], "by": "（这个目标不查）", "skipped": True}


def _kept(st: dict) -> dict:
    """结果里给人看的：工具调用多少按原样写、多少当文字；图片带过去几张；思考按原样几段。"""
    return {"tools": st.get("tools", 0), "tools_native": st.get("tools_native", 0), "images": st.get("images", 0),
            "images_kept": st.get("images_kept", 0), "reasoning_native": st.get("reasoning_native", 0)}


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



# ---------- 转换前预览（0.3.6）：写到哪、多少条、会丢 / 会变什么 ----------

OUT_LINES, OUT_CHARS, IN_LINES, IN_CHARS = 20, 2000, 30, 3000       # 和 ir.turns 的截短一致


def _long(v, lines: int, chars: int) -> bool:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1) if v is not None else ""
    return len(s) > chars or s.count("\n") >= lines


def _facts(items: list[dict]) -> dict:
    """预览用的计数（不含正文）。"""
    from . import toolmap
    f = {"user": 0, "assistant": 0, "tools": 0, "tool_errors": 0, "long_out": 0, "long_in": 0, "thinking": 0, "images": 0, "files": 0,
         "models": [], "first": None, "last": None, "images_data": 0, "tools_native": {d: 0 for d in NATIVE}}
    ms = set()
    for it in items:
        ts = it.get("ts")
        if ts:
            f["first"] = min(f["first"] or ts, ts)
            f["last"] = max(f["last"] or ts, ts)
        if it.get("model"):
            ms.add(str(it["model"]))
        if it["role"] == "user":
            f["user"] += 1
            f["images"] += int(it.get("images") or 0)
            f["files"] += int(it.get("files") or 0)
            f["images_data"] += sum(1 for im in it.get("image_data") or [] if isinstance(im, dict) and ir.img(im.get("mt"), im.get("b64")))
        elif it.get("kind") == "text":
            f["assistant"] += 1
        elif it.get("kind") == "thinking":
            f["thinking"] += 1
        elif it.get("kind") == "tool":
            f["tools"] += 1
            c = toolmap.canon(it.get("tool"), it.get("input"))
            for d in NATIVE:
                if c and toolmap.emit(d, c[0], c[1]):
                    f["tools_native"][d] += 1
            if it.get("status") == "error" or it.get("error"):
                f["tool_errors"] += 1
            if _long(it.get("output") or it.get("error") or "", OUT_LINES, OUT_CHARS):
                f["long_out"] += 1
            if _long(it.get("input"), IN_LINES, IN_CHARS):
                f["long_in"] += 1
    f["models"] = sorted(ms)[:6]
    return f


def losses(f: dict, src: str, dst: str, mode: str, reasoning: bool = False) -> list[dict]:
    """→ [{level: lost | changed | kept, what, detail}]：这个会话转过去以后，哪些没了、哪些变了样、哪些照旧。"""
    L, app, to = [], ir.LABEL.get(src, "来源应用"), ir.LABEL.get(dst, dst)
    exp = mode == "export"
    img_ok = 0 if exp or dst not in NATIVE else min(f.get("images_data") or 0, f.get("images") or 0)
    if img_ok:
        L.append({"level": "kept", "what": f"图片 {img_ok} 张", "detail": f"原图带过去（{to} 里能看到），单张超过 5 MB 的除外"})
    if (f.get("images") or 0) - img_ok > 0:
        n = f["images"] - img_ok
        L.append({"level": "lost", "what": f"图片 {n} 张", "detail": "没有原图数据（只记了路径 / 网址，或格式不认识 / 太大），原位置换成「[图片 ×N，没有带过来]」"})
    if f.get("files"):
        L.append({"level": "lost", "what": f"附件 {f['files']} 个", "detail": "文件附件不带过去，只剩对话里提到它的文字"})
    if f.get("thinking"):
        if reasoning and not exp and dst in ir.NATIVE_REASONING:
            L.append({"level": "kept", "what": f"思考 {f['thinking']} 段", "detail": f"写成 {to} 自己的思考块（只有文字，原来的签名 / 加密内容带不过去）"})
        elif reasoning and not exp:
            L.append({"level": "changed", "what": f"思考 {f['thinking']} 段", "detail": f"当普通文字放进回复里（标「{app} 里的思考」）；{to} 不会把它当思考"})
        else:
            L.append({"level": "lost", "what": f"思考 {f['thinking']} 段", "detail": "不带（各家的思考格式互相不认）" + ("" if exp else "；要带上就打开「带上思考内容」")})
    if f.get("tools"):
        if exp:
            L.append({"level": "changed", "what": f"工具调用 {f['tools']} 次", "detail": "只留一行摘要（工具名 + 主要参数），参数全文和输出都不带"})
        else:
            cut = []
            nat = (f.get("tools_native") or {}).get(dst, 0)
            if f.get("long_out"):
                cut.append(f"{f['long_out']} 个输出超过 {OUT_LINES} 行 / {OUT_CHARS} 字被截短")
            if f.get("long_in"):
                cut.append(f"{f['long_in']} 个参数超过 {IN_LINES} 行被截短")
            if nat:
                L.append({"level": "changed", "what": f"工具调用 {f['tools']} 次",
                          "detail": f"{nat} 次按 {to} 自己的工具（命令行 / 读写文件 / 搜索这类）原样记成工具调用，在 {to} 里能展开看"
                                    + (f"；另外 {f['tools'] - nat} 次 {to} 没有对应的工具，变成文字摘要" if f["tools"] > nat else "")
                                    + "；输出" + ("：" + "，".join(cut) if cut else "照旧（太长的会截短）")})
            else:
                L.append({"level": "changed", "what": f"工具调用 {f['tools']} 次",
                          "detail": f"变成文字摘要（工具名 + 参数 + 输出），{to} 里不能重放 / 展开" + ("；" + "，".join(cut) if cut else "")})
        if f.get("tool_errors"):
            L.append({"level": "changed", "what": f"出错的工具调用 {f['tool_errors']} 次", "detail": "标成「（出错）」，错误信息当文字留着"})
    if exp:
        L.append({"level": "changed", "what": "不进会话列表", "detail": f"只写一个 Markdown 上下文文件；在 {to} 里新开对话，粘贴提示 + 把文件 @ 进去"})
        L.append({"level": "lost", "what": "时间 / 模型 / 用量", "detail": "上下文文件里只有对话文字"})
    else:
        L.append({"level": "changed", "what": "模型名和 token 用量", "detail": f"不带：新会话里记成「{src if src in ir.LABEL else '<来源>'}-import」、0 token（不算进用量）"})
        L.append({"level": "changed", "what": "标题", "detail": f"加来源前缀「{ir.PREFIX.get(src, '（来源应用）')}」，以后不会被转回 {app}"})
        L.append({"level": "kept", "what": "对话文字和时间", "detail": "用户 / 助手的文字原样带过去；相邻的同一方合并成一轮；时间照旧"})
    return L


def _target_of(dst: str, mode: str, row: dict, opts: dict, src: str = "") -> dict:
    """→ {where: 人看的位置, path: 目录或文件（可能是样子，带 <新会话号>）}。"""
    from . import codex_import as CI
    cwd = CI.norm_cwd(row.get("cwd")) or str(Path.home())
    if mode == "export":
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{src}-{_safe(str(row.get('id') or ''))[:12]}-{_safe(row.get('title') or '')}.md"
        if opts.get("project") and row.get("cwd") and Path(row["cwd"]).is_dir():
            d = Path(row["cwd"]) / ".cc-sessionsync"
            return {"where": "项目目录里的上下文文件", "path": str(d / name), "dir": str(d)}
        return {"where": "本程序导出目录里的上下文文件", "path": str(EXPORTS / dst / name), "dir": str(EXPORTS / dst)}
    if dst == "claude":
        d = paths.CLAUDE_HOME / "projects" / CI.project_slug(cwd)
        w = "Claude Code 新会话（命令行 claude --resume）" + ("，同时写进 Claude 桌面端当前账号的会话列表" if opts.get("desktop", True) else "")
        return {"where": w, "path": str(d / "<新会话号>.jsonl"), "dir": str(d)}
    if dst == "codex":
        now = datetime.now()
        d = CI.codex_home() / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        return {"where": "Codex 新线程（codex resume / 桌面端列表），并在 session_index.jsonl 追加一行标题",
                "path": str(d / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-<新线程号>.jsonl"), "dir": str(d)}
    if dst == "dsh":
        try:
            from . import dsh
            root = dsh.write_root()["root"]
        except Exception:
            root = None
        return {"where": "DeepSeek Harness 新会话目录（dsh --session-id）", "path": str(Path(root) / "<新会话号>") if root else "（算不出 dsh 的会话根）",
                "dir": str(root) if root else None}
    if dst == "opencode":
        try:
            db = str(ocstore.db_path("opencode"))
        except Exception:
            db = None
        return {"where": "OpenCode 新会话：交给 OpenCode 自己的命令行导入（opencode import），本程序不直接写它的库",
                "path": db or "OpenCode 数据库", "dir": None}
    return {"where": ir.LABEL.get(dst, dst), "path": None, "dir": None}


def preview(src: str, ids: list[str], dst: str, again: bool = False, reasoning: bool = False, project: bool = False,
            export: bool = False, desktop: bool = True) -> dict:
    """转换 / 导出之前给人看的：每个会话写到哪、多少条消息、会丢 / 会变什么；合起来的总数。不写任何东西。"""
    p = plan(src, ids, dst, again)
    if export and p["mode"] == "native":
        p.update(mode="export", why="选了只导出上下文文件", paste=PASTE.get(dst, PASTE_ANY))
        for r in p["rows"]:
            if r["state"] in ("same", "changed", "loop"):
                r["state"] = "new"
    opts = {"project": project, "desktop": desktop}
    tot = {"sessions": 0, "msgs": 0, "user": 0, "assistant": 0, "tools": 0, "thinking": 0, "images": 0, "files": 0, "long_out": 0,
           "images_data": 0, "tools_native": {d: 0 for d in NATIVE}}
    agg: dict = {}
    for r in p["rows"]:
        f = r.pop("facts", None)
        if r["state"] != "new" or not f:
            continue
        r["msgs"] = f["user"] + f["assistant"]
        r["target"] = _target_of(dst, p["mode"], r, opts, src)
        r["losses"] = losses(f, src, dst, p["mode"], reasoning)
        r["span"] = [f["first"], f["last"]]
        tot["sessions"] += 1
        tot["msgs"] += r["msgs"]
        for k in ("user", "assistant", "tools", "thinking", "images", "files", "long_out", "images_data"):
            tot[k] += f.get(k) or 0
        for d in NATIVE:
            tot["tools_native"][d] += (f.get("tools_native") or {}).get(d, 0)
        for x in r["losses"]:
            a = agg.setdefault((x["level"], x["what"].split(" ")[0]), {"level": x["level"], "what": x["what"].split(" ")[0], "n": 0, "detail": x["detail"]})
            a["n"] += 1
    for r in p["rows"]:
        r.pop("facts", None)
    p["total"] = tot
    p["summary"] = losses(dict(tot, tool_errors=0, long_in=0), src, dst, p["mode"], reasoning) if tot["sessions"] else []
    p["dirs"] = sorted({(r.get("target") or {}).get("dir") for r in p["rows"] if (r.get("target") or {}).get("dir")})
    return dict(p, preview=True, reasoning=reasoning, project=project, export=export)

# ---------- 真做 ----------

def _new_ledger(what: str) -> tuple[Path, dict]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = LEDGERS / stamp
    while d.exists():
        d = d.with_name(d.name + "x")
    d.mkdir(parents=True)
    return d, {"what": what, "time": time.time(), "created": [], "map_before": {}, "items": [], "errors": []}


def run(src: str, ids: list[str], dst: str, dry: bool = True, again: bool = False, reasoning: bool = False,
        desktop: bool = True, to: str | None = None, project: bool = False, export: bool = False, progress=None) -> dict:
    """src 的这几个会话 → dst。dry=True 只演练（算出要写什么，不写）。export=True：能原生写也只导出上下文文件。"""
    p = plan(src, ids, dst, again)
    if export and p["mode"] == "native":
        p.update(mode="export", why="选了只导出上下文文件", paste=PASTE.get(dst, PASTE_ANY))
        for r in p["rows"]:
            if r["state"] in ("same", "changed", "loop"):
                r["state"] = "new"
    opts = {"reasoning": reasoning, "desktop": desktop, "to": to, "project": project}
    todo = [r for r in p["rows"] if r["state"] == "new"]
    for r in p["rows"]:
        r.pop("facts", None)
    if dry or not todo:
        for r in todo:
            t = _target_of(dst, p["mode"], r, opts, src)
            r["target"] = t["where"] + (f" → {t['path']}" if t.get("path") else "")
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
            if p["mode"] == "native":
                out["check"] = check_written(dst, out)
            led["created"].append(out)
            if p["mode"] == "native":
                led["map_before"][k] = mp.get(k)
                mp[k] = {"id": out["id"], "file": out.get("file"), "sha": out.get("sha"), "src_sig": r.get("sig"), "title": out["title"],
                         "cwd": out.get("cwd"), "time": time.time(), "ledger": led_dir.name}
                _save(MAP, mp)
            r.update({x: out.get(x) for x in ("id", "file", "title", "resume", "paste", "turns", "check")}, id=r["id"], target_id=out.get("id"),
                     done=True, kept=_kept(out.get("stats") or {}))
            done += 1
        except Exception as e:
            led["errors"].append(f"{r['id']}：{type(e).__name__}: {e}")
            r["error"] = f"{type(e).__name__}: {e}"
        _save(led_dir / "ledger.json", led)
        if progress:
            progress(src, r)
    _save(led_dir / "ledger.json", led)
    try:
        from . import events
        if done:
            events.add("import", f"已把 {done} 个 {ir.LABEL[src]} 会话" + ("导出成给 " if p["mode"] == "export" else "转成 ") +
                       f"{ir.LABEL[dst]} 的" + ("上下文文件" if p["mode"] == "export" else "会话"), "撤回在「会话转换」页的记录里",
                       {"page": "convert"})
    except Exception:
        pass
    try:
        from . import xsearch
        xsearch.forget()
    except Exception:
        pass
    return dict(p, dry=False, done=done, errors=led["errors"], ledger=led_dir.name, ledger_path=str(led_dir))


# ---------- 批量：好几个应用的会话一起转到一个目标（0.3.7） ----------

def _groups(items: list[dict], dst: str) -> dict[str, list[str]]:
    g: dict[str, list[str]] = {}
    for it in items:
        a, i = str(it.get("agent") or it.get("src") or ""), str(it.get("id") or "")
        if a not in AGENTS:
            raise ValueError(f"不认识的应用 {a}")
        if i and a != dst and i not in g.setdefault(a, []):
            g[a].append(i)
    return {a: v for a, v in g.items() if v}


def batch_preview(items: list[dict], dst: str, again: bool = False, reasoning: bool = False, project: bool = False,
                  export: bool = False, desktop: bool = True) -> dict:
    """items = [{agent, id}]（可以来自不同应用）→ 合起来的预览。和目标是同一个应用的跳过。"""
    if dst not in AGENTS:
        raise ValueError("目标只能是 " + " / ".join(AGENTS))
    g = _groups(items, dst)
    same = sum(1 for it in items if str(it.get("agent") or it.get("src")) == dst)
    rows, tot, dirs, out = [], {}, set(), {"mode": None, "why": None, "paste": None, "running": False}
    for src, ids in g.items():
        p = preview(src, ids, dst, again=again, reasoning=reasoning, project=project, export=export, desktop=desktop)
        for r in p["rows"]:
            r["src"] = src
        rows += p["rows"]
        for k, v in p["total"].items():
            tot[k] = tot.get(k, 0) + v
        dirs |= set(p["dirs"])
        out.update(mode=out["mode"] or p["mode"], why=out["why"] or p["why"], paste=out["paste"] or p["paste"], running=out["running"] or p["running"])
    if not g:
        out.update(mode=mode(dst)[0] if not export else "export")
    tot = tot or {"sessions": 0, "msgs": 0, "user": 0, "assistant": 0, "tools": 0, "thinking": 0, "images": 0, "files": 0, "long_out": 0}
    src1 = next(iter(g)) if len(g) == 1 else "*"
    summary = losses(dict(tot, tool_errors=0, long_in=0), src1, dst, out["mode"] or "native", reasoning) if tot.get("sessions") else []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return dict(out, dst=dst, rows=rows, total=tot, summary=summary, dirs=sorted(dirs), counts=counts, same_app=same,
                apps={a: len(v) for a, v in g.items()}, preview=True, reasoning=reasoning, project=project, export=export)


def batch_run(items: list[dict], dst: str, again: bool = False, reasoning: bool = False, desktop: bool = True, to: str | None = None,
              project: bool = False, export: bool = False, progress=None) -> dict:
    """真做：每个来源应用一本账（撤回时一本一本撤）。progress(done, total, row) 每做完一个调一次。"""
    g = _groups(items, dst)
    total, done_n, res = sum(len(v) for v in g.values()), [0], {"done": 0, "errors": [], "ledgers": [], "rows": [], "mode": None, "paste": None}
    for src, ids in g.items():
        def step(s, r):
            done_n[0] += 1
            if progress:
                progress(done_n[0], total, dict(r, src=s))
        r = run(src, ids, dst, dry=False, again=again, reasoning=reasoning, desktop=desktop, to=to, project=project, export=export, progress=step)
        res["done"] += r.get("done") or 0
        res["errors"] += [f"{ir.LABEL.get(src, src)} {e}" for e in r.get("errors") or []]
        if r.get("ledger"):
            res["ledgers"].append(r["ledger"])
        res["rows"] += [dict(x, src=src) for x in r.get("rows") or []]
        res.update(mode=res["mode"] or r.get("mode"), paste=res["paste"] or r.get("paste"))
        skipped = len(ids) - len([x for x in r.get("rows") or [] if x.get("state") == "new"])
        done_n[0] += max(0, skipped)
        if progress and skipped:
            progress(done_n[0], total, None)
    try:
        from . import xsearch
        xsearch.forget()
    except Exception:
        pass
    return dict(res, ok=True, dst=dst, total=total, apps={a: len(v) for a, v in g.items()})


_JOBS: dict[str, dict] = {}


def batch_start(**kw) -> dict:
    """后台做批量转换，前端轮询 batch_status 看进度。"""
    import threading
    jid = uuid.uuid4().hex[:10]
    st = {"id": jid, "state": "running", "done": 0, "total": sum(len(v) for v in _groups(kw.get("items") or [], kw.get("dst") or "").values()),
          "current": None, "result": None, "error": None, "started": time.time()}
    _JOBS[jid] = st

    def prog(n, total, r):
        st.update(done=n, total=total, current=(r or {}).get("title") if r else st["current"])
        if r and r.get("error"):
            st.setdefault("errs", []).append(r["error"])

    def work():
        try:
            st["result"] = batch_run(progress=prog, **kw)
            st["state"] = "done"
        except Exception as e:
            st.update(state="error", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=work, daemon=True, name="ccss-convert-batch").start()
    for k in [k for k, v in _JOBS.items() if v["state"] != "running" and time.time() - v["started"] > 3600]:
        _JOBS.pop(k, None)
    return {"ok": True, "job": jid, "total": st["total"], "deferred": False}


def batch_status(jid: str) -> dict:
    st = _JOBS.get(jid)
    if not st:
        return {"ok": False, "error": "找不到这个任务（程序重启过？）"}
    return dict(st, ok=True)


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
                elif (cur.get("msgs") or 0) > (c.get("base_msgs") if c.get("base_msgs") is not None else c.get("turns") or 0):
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
            for at in c.get("attachments") or []:          # 这次放进 dsh 附件库的图片（当时新建的才记）
                af = Path(at["file"])
                if af.exists() and _sha(af) == at.get("sha"):
                    af.unlink()
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

# -*- coding: utf-8 -*-
"""时间线：备份库（sessions.sqlite）里每个会话的各个版本 → 选一个时间点，看当时的对话、和现在比，恢复。

恢复（都记进 ~/.cc-sessionsync/session-ops 的账本，能撤回）：
- 「恢复到 Claude」（缺省）：另存成一个新的 Claude Code 会话（新会话号，标题「…（恢复 @时间）」），写进
  ~/.claude/projects/<项目>/，再在当前账号的桌面端会话列表里登记一条（桌面端开着时这一步排队，等它退出再做）。
  Codex 的版本先用 Codex → Claude Code 的转换器转一下再写。
- 「替换当前」：先把现在的记录备份进库 + 账本，再换成那一版（Claude 桌面端 / Codex 开着时整个排队）。
- 「恢复到 Codex」（只对 Codex 的版本）：原文件不在就写回原处；在的话按「替换当前」。
全局时间线：选一个时间点，列出那之后变过（或已经没了）的会话，勾选几个一起恢复成那个时间点的样子。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import apps, paths
from . import sessions as S


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _parse(agent: str, data: bytes) -> list[dict]:
    parse = S.parse_claude if agent == "claude" else S.parse_codex
    out = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            out += parse(json.loads(line))
        except Exception:
            continue
    return out


def _snap(snap_id: int) -> dict:
    with S._db_lock:
        c = S._db()
        r = c.execute("select id, agent, sid, path, sha, mtime, size, title, cwd, first, at, why, msgs, run from snaps where id=?",
                      (int(snap_id),)).fetchone()
        c.close()
    if not r:
        raise FileNotFoundError("备份库里没有这一版")
    return dict(zip(("id", "agent", "sid", "path", "sha", "mtime", "size", "title", "cwd", "first", "at", "why", "msgs", "run"), r))


def _current(agent: str, sid: str) -> Path | None:
    return S.claude_file(sid) if agent == "claude" else S.codex_file(sid)


def _sha_file(f: Path) -> str | None:
    import hashlib
    try:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for ch in iter(lambda: fh.read(1 << 20), b""):
                h.update(ch)
        return h.hexdigest()
    except OSError:
        return None


def _meta(agent: str, sid: str) -> dict:
    try:
        return S.find(agent, sid) or {}
    except Exception:
        return {}


# ---------- 看 ----------

def versions(agent: str, sid: str) -> dict:
    """一个会话的所有版本（旧 → 新），带时间、消息数、大小、和上一版比变了什么；标出哪一版就是现在的样子。"""
    with S._db_lock:
        c = S._db()
        rows = [dict(zip(("id", "sha", "size", "at", "why", "msgs", "title", "first", "cwd"), r)) for r in c.execute(
            "select id, sha, size, at, why, msgs, title, first, cwd from snaps where agent=? and sid=? order by at", (agent, sid))]
        c.close()
    f = _current(agent, sid)
    cur_sha = _sha_file(f) if f else None
    prev = None
    for r in rows:
        if prev is None:
            r["change"] = "第一版"
        else:
            dm = (r["msgs"] or 0) - (prev["msgs"] or 0)
            ds = r["size"] - prev["size"]
            r["change"] = (f"+{dm} 条消息" if dm > 0 else f"{dm} 条消息" if dm < 0 else "消息数没变") + \
                          (f" · +{ds // 1024} KB" if ds >= 1024 else f" · {ds} B" if ds else "")
        r["current"] = r["sha"] == cur_sha
        prev = r
    m = _meta(agent, sid)
    return {"ok": True, "agent": agent, "id": sid, "title": m.get("title") or (rows[-1]["title"] if rows else "") or
            (rows[-1]["first"][:60] if rows and rows[-1]["first"] else ""), "cwd": m.get("cwd") or (rows[-1]["cwd"] if rows else None),
            "exists": bool(f), "current_sha": cur_sha, "items": rows}


def view(snap_id: int, limit: int = 400) -> dict:
    """一版的对话（只读），和现在的比：前面相同的有几条、那一版之后现在多了哪些、那一版里有而现在没了的。"""
    r = _snap(snap_id)
    data = S._blob(r["sha"])
    es = _parse(r["agent"], data)
    f = _current(r["agent"], r["sid"])
    cur = []
    if f:
        try:
            cur = _parse(r["agent"], f.read_bytes())
        except OSError:
            cur = []
    k = lambda e: (e["role"], e["text"])
    same = 0
    for a, b in zip(es, cur):
        if k(a) != k(b):
            break
        same += 1
    clip = lambda xs: [dict(e, text=e["text"][:600]) for e in xs[:20]]
    return {"ok": True, "snap": {x: r[x] for x in ("id", "agent", "sid", "at", "size", "msgs", "why", "title", "cwd")},
            "total": len(es), "entries": [dict(e, text=e["text"][:6000]) for e in es[-limit:]], "cut": max(0, len(es) - limit),
            "diff": {"exists": bool(f), "same": same, "current_total": len(cur), "only_now": len(cur) - same,
                     "only_then": len(es) - same, "now_samples": clip(cur[same:]), "then_samples": clip(es[same:]),
                     "identical": bool(f) and same == len(es) == len(cur)}}


def runs(limit: int = 500) -> list[dict]:
    """每一次备份（时间点）：存了多少个会话的新版本。"""
    with S._db_lock:
        c = S._db()
        rows = c.execute("select coalesce(run, at) as r, count(*), group_concat(distinct why) from snaps group by cast(r as int) "
                         "order by r desc limit ?", (int(limit),)).fetchall()
        c.close()
    return [{"at": r[0], "n": r[1], "why": r[2]} for r in rows]


def point(t: float) -> dict:
    """时间点 t：每个会话那时候的版本（t 之前最后一版），和现在比是一样 / 后来变了 / 现在已经没了。"""
    with S._db_lock:
        c = S._db()
        rows = c.execute("select s.id, s.agent, s.sid, s.at, s.size, s.msgs, s.title, s.first, s.cwd, s.sha from snaps s "
                         "join (select agent, sid, max(at) m from snaps where at <= ? group by agent, sid) x "
                         "on s.agent = x.agent and s.sid = x.sid and s.at = x.m", (float(t),)).fetchall()
        later = {(a, s): n for a, s, n in c.execute("select agent, sid, count(*) from snaps where at > ? group by agent, sid", (float(t),))}
        c.close()
    try:
        meta = {(x["agent"], x["id"]): x for x in S.list_all()["items"]}
    except Exception:
        meta = {}
    out = []
    for sid_, agent, sid, at, size, msgs, title, first, cwd, sha in rows:
        m = meta.get((agent, sid)) or {}
        f = m.get("file")
        if not f:
            state = "gone"
        elif later.get((agent, sid)):
            state = "changed"
        else:
            state = "same"
        out.append({"snap": sid_, "agent": agent, "id": sid, "at": at, "size": size, "msgs": msgs, "state": state,
                    "title": m.get("title") or title or (first or "")[:60] or "（无标题）", "cwd": m.get("cwd") or cwd,
                    "accounts": m.get("accounts") or [], "provider": m.get("provider"), "now_size": m.get("size"),
                    "later_versions": later.get((agent, sid), 0)})
    out.sort(key=lambda x: ({"changed": 0, "gone": 1, "same": 2}[x["state"]], -x["at"]))
    return {"ok": True, "t": t, "items": out, "counts": {s: sum(1 for x in out if x["state"] == s) for s in ("changed", "gone", "same")}}


# ---------- 写成 Claude Code 会话（时间线恢复 / 导入共用） ----------

def _claude_records_from(agent: str, data: bytes, new_sid: str, title: str) -> tuple[list[str], str | None]:
    """→ (jsonl 各行, 工作目录)。Claude 的：换会话号；Codex 的：走 Codex → Claude Code 的转换器。"""
    if agent == "claude":
        lines, cwd = [], None
        for line in data.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except Exception:
                lines.append(line)
                continue
            if isinstance(j, dict):
                cwd = cwd or j.get("cwd")
                if "sessionId" in j:
                    j["sessionId"] = new_sid
                if j.get("type") == "custom-title":
                    continue                                   # 老标题不要了，下面加新的
            lines.append(json.dumps(j, ensure_ascii=False))
        lines.append(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": new_sid}, ensure_ascii=False))
        return lines, cwd
    from . import codex_import as CI
    fd, tmp = tempfile.mkstemp(prefix="ccss-rollout-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        b = CI.build(Path(tmp), new_sid, CI.claude_version(), title)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    lines = [json.dumps(r, ensure_ascii=False) for r in b["records"]]
    lines.append(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": new_sid}, ensure_ascii=False))
    return lines, b["cwd"]


def write_claude_session(agent: str, data: bytes, title: str, cwd: str | None, led: dict, od: Path,
                         desktop: bool = True, project_dir: Path | None = None) -> dict:
    """写一个新的 Claude Code 会话；desktop=True 时在当前账号的桌面端列表登记（开着就排队）。"""
    from . import codex_import as CI
    sid = str(uuid.uuid4())
    lines, cwd2 = _claude_records_from(agent, data, sid, title)
    cwd = CI.norm_cwd(cwd or cwd2) or str(Path.home())
    pdir = project_dir or (S.claude_home() / "projects" / CI.project_slug(cwd))
    f = pdir / f"{sid}.jsonl"
    S._atomic(f, ("\n".join(lines) + "\n").encode("utf-8"))
    led["steps"].append({"t": "created", "path": str(f)})
    try:                                                       # 新会话也进备份库
        with S._db_lock:
            c = S._db()
            S._store(c, "claude", sid, f, "restored")
            c.commit()
            c.close()
    except Exception:
        pass
    res = {"id": sid, "file": str(f), "cwd": cwd, "title": title, "resume": CI.resume_cmd(cwd, sid)}
    if desktop:
        reg = {"sid": sid, "cwd": cwd, "title": title, "mtime": time.time()}
        led.setdefault("regs", []).append(reg)
        res["desktop"] = "pending"
    return res


def register_desktop(op: str, regs: list[dict]) -> dict:
    """在当前账号的桌面端会话列表里登记（桌面端没开时才调；开着由 apps 排队，等退出后在后台进程里调）。"""
    from . import codex_import as CI
    d = S.OPS / op
    led = json.loads((d / "ledger.json").read_text(encoding="utf-8"))
    t = CI._desktop_target(None)
    if not t:
        return {"ok": False, "error": "没找到桌面端的会话列表文件夹（没装 Claude 桌面端？）；命令行 claude --resume 照样能用"}
    tdir = Path(t["path"])
    done = []
    for r in regs:
        e = CI._desktop_entry(tdir, r["sid"], r["cwd"], r["title"], r["mtime"], None)
        e["title"], e["titleSource"] = r["title"], "user"          # 不加「（Codex）」前缀：标题里已经写了「恢复 @…」
        p = tdir / f"{e['sessionId']}.json"
        S._atomic(p, json.dumps(e, ensure_ascii=False, indent=1).encode("utf-8"))
        led["steps"].append({"t": "created", "path": str(p)})
        done.append(e["sessionId"])
    led["desktop"] = {"key": t["key"], "entries": done}
    S._save_led(d, led)
    return {"ok": True, "registered": len(done), "folder": t["key"]}


def _finish_regs(od: Path, led: dict) -> dict | None:
    regs = led.get("regs") or []
    S._save_led(od, led)
    if not regs:
        return None
    return apps.run_or_defer("claude", "desktop-register", {"op": od.name, "regs": regs}, f"在桌面端登记 {len(regs)} 个恢复的会话")


# ---------- 恢复 ----------

def _title_for(r: dict) -> str:
    m = _meta(r["agent"], r["sid"])
    base = m.get("title") or r.get("title") or (r.get("first") or "")[:40] or "会话"
    return f"{base}（恢复 @{_when(r['at'])}）"


def restore(snaps: list[int], mode: str = "copy", target: str = "claude", desktop: bool = True) -> dict:
    """mode=copy：另存成新的 Claude Code 会话（缺省）；mode=replace：替换当前（先备份）。target=codex：写回 Codex（只对 Codex 的版本）。"""
    rs = [_snap(i) for i in snaps]
    if not rs:
        return {"ok": False, "error": "没选版本"}
    if mode == "replace" or target == "codex":
        kinds = {r["agent"] for r in rs}
        if target == "codex" and kinds != {"codex"}:
            return {"ok": False, "error": "只有 Codex 的版本能恢复到 Codex"}
        out = {"ok": True, "results": []}
        for kind in sorted(kinds):
            part = [r["id"] for r in rs if r["agent"] == kind]
            res = apps.run_or_defer(kind, "timeline-replace", {"snaps": part},
                                    f"把 {len(part)} 个{'Claude' if kind == 'claude' else 'Codex'}会话换回备份里的版本")
            out["results"].append(dict(res, agent=kind))
            out["ok"] = out["ok"] and res.get("ok", True) is not False
        return out
    od, led = S._new_op("timeline-restore", items=[{"agent": r["agent"], "id": r["sid"]} for r in rs],
                        summary=f"从时间线恢复 {len(rs)} 个会话（另存为新的 Claude Code 会话）")
    made, errors = [], []
    for r in rs:
        try:
            data = S._blob(r["sha"])
            pdir = Path(r["path"]).parent if r["agent"] == "claude" and Path(r["path"]).parent.parent.name == "projects" else None
            if pdir is not None and pdir.parent != S.claude_home() / "projects":
                pdir = S.claude_home() / "projects" / pdir.name
            res = write_claude_session(r["agent"], data, _title_for(r), r.get("cwd"), led, od, desktop, pdir)
            made.append(dict(res, snap=r["id"], from_agent=r["agent"]))
        except Exception as e:
            errors.append(f"{r['sid'][:8]}: {type(e).__name__}: {e}")
    led.update(made=made, errors=errors)
    job = _finish_regs(od, led) if desktop else (S._save_led(od, led) or None)
    return {"ok": not errors, "made": made, "errors": errors, "op": od.name, "desktop": job}


def replace(snaps: list[int]) -> dict:
    """替换当前：先把现在的记录存进备份库 + 账本里留一份，再原子换成那一版（Codex 原文件没了就写回原处）。"""
    rs = [_snap(i) for i in snaps]
    od, led = S._new_op("timeline-replace", items=[{"agent": r["agent"], "id": r["sid"]} for r in rs],
                        summary=f"把 {len(rs)} 个会话换回备份里的版本")
    done, errors = [], []
    for r in rs:
        try:
            data = S._blob(r["sha"])
            f = _current(r["agent"], r["sid"])
            if f and f.exists():
                S.backup(only=[(r["agent"], r["sid"])], why="before-restore")
                n = S._keep(od, led, f)
                S._atomic(f, data)
                led["steps"].append({"t": "file", "path": str(f), "before": n})
                done.append({"agent": r["agent"], "id": r["sid"], "path": str(f), "how": "replaced"})
            else:
                dst = Path(r["path"])
                if dst.exists():
                    raise FileExistsError(f"{dst} 已经有了")
                S._atomic(dst, data)
                led["steps"].append({"t": "created", "path": str(dst)})
                done.append({"agent": r["agent"], "id": r["sid"], "path": str(dst), "how": "written-back"})
        except Exception as e:
            errors.append(f"{r['sid'][:8]}: {type(e).__name__}: {e}")
    led.update(done=done, errors=errors)
    S._save_led(od, led)
    return {"ok": not errors, "done": done, "errors": errors, "op": od.name}

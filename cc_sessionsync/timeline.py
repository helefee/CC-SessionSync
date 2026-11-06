# -*- coding: utf-8 -*-
"""写入 Claude Code 会话的薄封装（导入用）。

时间线 UI / 快照恢复 / 版本浏览已移除；本模块只保留：
- write_claude_session / register_desktop / _finish_regs / _claude_records_from
供 bigimport / 文件导入把 .md/.html/.jsonl 写成新的 Claude Code 会话。
磁盘上的 sessions.sqlite 不删；删除会话前 sessions.backup() 仍可能写入。
账号同步的备份与「同步记录」撤销走另一套（~/.cc-sessionsync 同步账本），与本模块无关。
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
    if agent in ("opencode", "zcode"):                         # OpenCode / ZCode：镜像是公共中间格式，走通用的 → Claude Code
        from . import ir, codex_import as CI
        h, items = ir.load(data)
        h["agent"] = agent
        b = ir.build_claude(h, items, new_sid, CI.claude_version(), title=title, cwd=CI.norm_cwd(h.get("cwd")) or None)
        b["records"][-1]["customTitle"] = title
        return [json.dumps(r, ensure_ascii=False) for r in b["records"]], b["cwd"]
    if agent == "cursor":                                      # Cursor：走 Cursor → Claude Code 的转换器
        from . import cursor
        b = cursor.build_claude_bytes(data, new_sid, title)
        b["records"][-1]["customTitle"] = title
        return [json.dumps(r, ensure_ascii=False) for r in b["records"]], b["cwd"]
    if agent == "dsh":                                         # DeepSeek Harness：走 dsh → Claude Code 的转换器
        from . import dsh
        b = dsh.build_claude_bytes(data, new_sid, title)
        b["records"][-1]["customTitle"] = title
        return [json.dumps(r, ensure_ascii=False) for r in b["records"]], b["cwd"]
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


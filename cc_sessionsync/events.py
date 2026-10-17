# -*- coding: utf-8 -*-
"""进程内的事件流：托盘轮询 /api/tray?since=<seq> 拿新事件弹通知；界面也拿它刷新「最近发生的事」。

事件：{"seq", "time", "kind", "title", "text", "open"}；kind：
    auto-sync     自动同步补了会话
    job           排队等桌面端退出的活做完了 / 失败了
    import        Codex 对话导入完成
    account       检测到 Claude Code / Codex 账号切换（watch.py）
    update        有新版本
    backup        会话自动备份完成（只在有新内容时）
    selfcheck     自检有失败项
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_events: list[dict] = []
_seq = 0


def add(kind: str, title: str, text: str = "", open_target: dict | None = None, **extra) -> dict:
    global _seq
    with _lock:
        _seq += 1
        ev = {"seq": _seq, "time": time.time(), "kind": kind, "title": title, "text": text, "open": open_target or {}, **extra}
        _events.append(ev)
        del _events[:-100]
    return ev


def since(seq: int) -> tuple[int, list[dict]]:
    with _lock:
        return _seq, [e for e in _events if e["seq"] > seq]


def recent(n: int = 30) -> list[dict]:
    with _lock:
        return list(_events[-n:])


def reset() -> None:          # 测试用
    global _seq
    with _lock:
        _events.clear()
        _seq = 0


# ---------- 排队的活：看 pending-jobs.json 的状态变化 ----------

_job_seen: dict[str, str] = {}


def poll_jobs() -> list[dict]:
    """排队的活从 waiting 变成 done / failed 时各出一条事件。第一次调用只记状态。"""
    from . import apps
    out = []
    first = not _job_seen
    for j in apps.pending():
        old = _job_seen.get(j["id"])
        _job_seen[j["id"]] = j["state"]
        if j.get("runner"):                       # 「关闭并执行」做的：appclose 自己出通知（带重新打开的结果）
            continue
        if first or old == j["state"] or j["state"] not in ("done", "failed") or old not in (None, "waiting"):
            continue
        if old is None and time.time() - float(j.get("finished") or 0) > 120:
            continue                              # 很早以前做完的，不补通知
        ok = j["state"] == "done"
        out.append(add("job", ("已完成：" if ok else "没做成：") + j.get("title", j["op"]),
                       (j.get("result") or "")[:200], {"page": "jobs"}, ok=ok))
    _job_seen["__init__"] = "1"
    return out

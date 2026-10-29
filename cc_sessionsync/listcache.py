# -*- coding: utf-8 -*-
"""启动快、内存有上限（0.4.0）。

1. 文件头缓存（磁盘）：每个会话文件按（修改时间, 大小）记住列表要用的几样东西（第一句话、目录、标题……），
   重开程序时没改过的文件不用再打开读。存在 ~/.cc-sessionsync/cache/heads-v1.json；条数有上限，删掉的文件下次整理时去掉。
2. 会话列表快照（磁盘）：上次的完整列表。刚启动时先把它给界面（马上能看），同时在后台按缓存增量刷新，刷新好了界面再换上新的。
   存在 ~/.cc-sessionsync/cache/sessions-list.json。
两样都可以随时删，删了只是第一次慢一点。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import paths

VERSION = 1
MAX_HEADS = 60000            # 文件头缓存最多这么多条（每条几百字节）
_lock = threading.RLock()
_heads: dict | None = None   # ns -> {path: [sig, value, used]}
_dirty = False
_used_at = 0.0


def cache_dir() -> Path:
    return paths.HOME / "cache"


def heads_file() -> Path:
    return cache_dir() / f"heads-v{VERSION}.json"


def list_file() -> Path:
    return cache_dir() / "sessions-list.json"


def _atomic(f: Path, data: bytes) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    for i in range(5):
        try:
            os.replace(tmp, f)
            return
        except PermissionError:
            time.sleep(0.1 * (i + 1))
    os.replace(tmp, f)


def _load() -> dict:
    global _heads, _used_at
    _used_at = time.time()
    if _heads is None:
        try:
            d = json.loads(heads_file().read_text(encoding="utf-8"))
            _heads = d.get("ns") if isinstance(d, dict) and d.get("v") == VERSION else {}
            if not isinstance(_heads, dict):
                _heads = {}
        except Exception:
            _heads = {}
    return _heads


def head_get(ns: str, key: str, sig):
    """sig 一样就返回缓存的值，不一样 / 没有返回 None。sig 要能 JSON（列表 / 数字 / 字符串）。"""
    with _lock:
        e = _load().get(ns, {}).get(key)
        if e and e[0] == (list(sig) if isinstance(sig, (tuple, list)) else sig):
            e[2] = 1
            return e[1]
    return None


def head_put(ns: str, key: str, sig, value) -> None:
    global _dirty
    with _lock:
        _load().setdefault(ns, {})[key] = [list(sig) if isinstance(sig, (tuple, list)) else sig, value, 1]
        _dirty = True


def head_count() -> int:
    with _lock:
        return sum(len(v) for v in _load().values())


def save(prune: bool = False) -> bool:
    """有改动才写。prune=True：这一轮没用到的条目（文件没了）去掉；超过上限也去掉旧的。"""
    global _dirty
    with _lock:
        h = _load()
        n = sum(len(v) for v in h.values())
        if prune:
            for ns in h:
                gone = [k for k, e in h[ns].items() if not e[2]]
                for k in gone:
                    del h[ns][k]
                _dirty = _dirty or bool(gone)
        if n > MAX_HEADS:
            for ns in h:
                keep = int(MAX_HEADS * len(h[ns]) / max(1, n))
                if len(h[ns]) > keep:
                    for k in list(h[ns])[:len(h[ns]) - keep]:
                        del h[ns][k]
            _dirty = True
        for ns in h:
            for e in h[ns].values():
                e[2] = 0
        if not _dirty:
            return False
        try:
            data = json.dumps({"v": VERSION, "ns": h}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return False
        _dirty = False
    try:
        _atomic(heads_file(), data)
        return True
    except OSError:
        return False


def snapshot_write(data: bytes) -> None:
    """上次回给界面的会话列表（已经打过码的 JSON），下次启动先给它。"""
    try:
        _atomic(list_file(), data)
    except OSError:
        pass


def snapshot_read() -> tuple[bytes, float] | None:
    try:
        f = list_file()
        return f.read_bytes(), f.stat().st_mtime
    except OSError:
        return None


def reset_memory() -> None:
    """测试用：当成刚启动。"""
    global _heads, _dirty
    with _lock:
        _heads, _dirty = None, False


def release_if_idle(idle_s: float) -> bool:
    """一段时间没人要列表：先存盘，再把文件头缓存从内存里放掉（下次要的时候从磁盘读回来，几十毫秒）。"""
    global _heads
    with _lock:
        if _heads is None or time.time() - _used_at < idle_s:
            return False
        save()
        _heads = None
    return True

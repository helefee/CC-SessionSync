# -*- coding: utf-8 -*-
"""清理 ~/.cc-sessionsync/backups（0.3.9）。

这个文件夹放的是改配置文件之前留的备份（0.2–0.3.4 改 Codex / Claude Code 配置、0.3.5 收拾旧改动时），
每份是 `<时间>-<毫秒>-<标签><后缀>`，`index.jsonl` 里一行记着它是哪个文件的备份。和会话备份（sessions.sqlite）、
Claude 同步备份（claude-sync-bak）无关，这里不碰那些。

规则：
- 按时间：比 N 天旧的删；按大小：总共超过 M MB 时，从最旧的开始删，删到不超过为止。两个可以一起用，0 / 空 = 不按这个删。
- **每个原文件最新的一份永远不删**（按 index.jsonl 记的原文件分组；没记录的按「标签 + 后缀」分组）。
- 只删 backups 文件夹第一层里的备份文件；index.jsonl、子文件夹、认不出来的文件都不动。
- 自动清理缺省关（设置 → 高级）；打开后每天最多做一次。手动「清理」先给预览（删哪些、腾出多少），确认了才删。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import paths, settings

NAME_RE = re.compile(r"^(\d{8}-\d{6})(?:-(\d{3}))?-(.+?)(\.[A-Za-z0-9]{1,8})?$")
SKIP = {"index.jsonl"}
DAY = 86400
MB = 1024 * 1024
_lock = threading.Lock()


def root() -> Path:
    return paths.HOME / "backups"


def state_file() -> Path:
    return paths.HOME / "backup-clean.json"


def _index() -> dict[str, dict]:
    out = {}
    try:
        lines = (root() / "index.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for x in lines:
        try:
            r = json.loads(x)
            out[str(r["id"]) + str(r.get("ext") or "")] = r
        except Exception:
            continue
    return out


def _norm(f: str) -> str:
    return os.path.normcase(os.path.normpath(f))


def scan() -> list[dict]:
    """backups 第一层的备份文件，新的在前；每个原文件最新的一份 keep=True。"""
    d = root()
    if not d.is_dir():
        return []
    idx = _index()
    out = []
    for p in d.iterdir():
        try:
            if not p.is_file() or p.is_symlink() or p.name in SKIP or p.name.endswith(".tmp"):
                continue
            st = p.stat()
        except OSError:
            continue
        r = idx.get(p.name)
        m = NAME_RE.match(p.name)
        if not r and not m:
            continue                                   # 认不出来的文件不动
        label = (r or {}).get("label") or (m.group(3) if m else "")
        ext = (r or {}).get("ext") or (m.group(4) if m and m.group(4) else "")
        at = (r or {}).get("at")
        if not isinstance(at, (int, float)):
            try:
                at = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else st.st_mtime
            except Exception:
                at = st.st_mtime
        file = (r or {}).get("file")
        out.append({"name": p.name, "size": st.st_size, "at": float(at), "file": file, "label": label,
                    "group": _norm(file) if file else f"?{label}{ext}", "keep": False})
    out.sort(key=lambda x: (x["at"], x["name"]), reverse=True)
    seen = set()
    for x in out:
        if x["group"] not in seen:
            seen.add(x["group"])
            x["keep"] = True
    return out


def _num(v) -> float:
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        raise ValueError("天数 / 大小要填数字")
    if v < 0:
        raise ValueError("天数 / 大小不能是负数")
    return v


def plan(days=None, mb=None, now: float | None = None, limit: int = 500) -> dict:
    """按规则算出要删哪些（不删）。days / mb 为 0 或空 = 不按这个删。"""
    days, mb = _num(days), _num(mb)
    now = time.time() if now is None else now
    items = scan()
    dele: set[str] = set()
    if days:
        dele |= {x["name"] for x in items if not x["keep"] and x["at"] < now - days * DAY}
    if mb:
        left = sum(x["size"] for x in items if x["name"] not in dele)
        for x in sorted(items, key=lambda x: (x["at"], x["name"])):     # 从最旧的开始
            if left <= mb * MB:
                break
            if not x["keep"] and x["name"] not in dele:
                dele.add(x["name"])
                left -= x["size"]
    gone = [x for x in items if x["name"] in dele]
    total = sum(x["size"] for x in items)
    freed = sum(x["size"] for x in gone)
    return {"dir": str(root()), "days": days, "mb": mb, "count": len(items), "total": total,
            "delete": [dict(x) for x in gone[:limit]], "delete_count": len(gone), "freed": freed, "after": total - freed,
            "protected": sum(1 for x in items if x["keep"]), "groups": len({x["group"] for x in items}),
            "oldest": min((x["at"] for x in items), default=None), "rule": bool(days or mb)}


def _drop_index(names: set[str]) -> None:
    f = root() / "index.jsonl"
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    keep = []
    for x in lines:
        try:
            r = json.loads(x)
            if str(r["id"]) + str(r.get("ext") or "") in names:
                continue
        except Exception:
            pass
        keep.append(x)
    tmp = f.with_name(f"index.{os.getpid()}.tmp")
    tmp.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
    os.replace(tmp, f)


def apply(days=None, mb=None, names=None, auto: bool = False, now: float | None = None) -> dict:
    """按规则删。names 给了的话只删预览里勾的那些（还得是规则现在仍然允许删的）——每个文件最新的一份怎么都不删。"""
    with _lock:
        p = plan(days, mb, now=now, limit=10 ** 9)
        if not p["rule"]:
            raise ValueError("先填天数或大小")
        ok = {x["name"]: x for x in p["delete"]}
        want = set(ok) if names is None else {n for n in names if n in ok}
        done, freed, errors = [], 0, []
        for n in sorted(want):
            if "/" in n or "\\" in n or n.startswith("."):
                continue
            try:
                (root() / n).unlink()
                done.append(n)
                freed += ok[n]["size"]
            except FileNotFoundError:
                done.append(n)
            except OSError as e:
                errors.append(f"{n}：{e}")
        if done:
            try:
                _drop_index(set(done))
            except OSError as e:
                errors.append(f"index.jsonl：{e}")
        res = {"ok": not errors, "deleted": len(done), "freed": freed, "errors": errors, "auto": auto,
               "time": time.time(), "left": p["count"] - len(done)}
        try:
            st = _state()
            st["auto" if auto else "manual"] = {k: res[k] for k in ("time", "deleted", "freed")}
            state_file().write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return res


def _state() -> dict:
    try:
        d = json.loads(state_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def status() -> dict:
    items = scan()
    s = settings.load()
    return {"dir": str(root()), "count": len(items), "total": sum(x["size"] for x in items),
            "auto": bool(s.get("backup_clean_auto")), "days": s.get("backup_clean_days"), "mb": s.get("backup_clean_mb"),
            "last": _state()}


def auto_tick(now: float | None = None) -> dict | None:
    """后台循环调：开了自动清理、离上次自动清理满一天才做。"""
    s = settings.load()
    if not s.get("backup_clean_auto"):
        return None
    days, mb = s.get("backup_clean_days") or 0, s.get("backup_clean_mb") or 0
    if not (days or mb):
        return None
    now = time.time() if now is None else now
    last = (_state().get("auto") or {}).get("time") or 0
    if now - last < DAY:
        return None
    return apply(days, mb, auto=True, now=now)

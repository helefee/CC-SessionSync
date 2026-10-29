# -*- coding: utf-8 -*-
"""0.4.8：会话的「类型和关系」跟着导出 / 导入走（主会话 + 子代理会话）。

Claude Code 把子代理（Task / Agent 工具）的记录放在主会话旁边：
  projects/<项目>/<会话号>.jsonl                       主会话
  projects/<项目>/<会话号>/subagents/agent-<id>.jsonl  子代理（isSidechain=true，agentId，sessionId=主会话号）
  projects/<项目>/<会话号>/subagents/agent-<id>.meta.json（有的版本有）、<会话号>/tool-results/…（大的工具输出）
  老版本：projects/<项目>/agent-<id>.jsonl（sessionId=主会话号）
Codex：子代理是单独的 rollout，session_meta.payload.source 里写着 parent_thread_id。

「导出原始 jsonl」：主文件照旧是一个 .jsonl（别的工具也能直接用）；有子会话的，旁边多一个同名文件夹
  <导出文件名>.ccss/  ←  manifest.json（ccss-bundle/1：类型 main/subagent、主会话号、agentId、标题、工作目录）+ 子会话文件（相对路径不变）
.md / .html 导出：子会话的原始记录也藏在文件里（ccss-sub:v1 块），一样能无损导回来。
导入：子会话写到新会话的 <新会话号>/subagents/ 下，sessionId 统一换成新会话号，agentId 不动（主会话里的 agentId 也不动），
Claude Code 里照样是「主会话 + 子代理」的层级。批量 / 文件夹导入时，子会话文件归到它的主会话下，不会被当成单独的会话导入。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import sessions as S

SIDE = ".ccss"
FORMAT = "ccss-bundle/1"


def sidecar_dir(export_file: Path) -> Path:
    return export_file.with_name(export_file.name + SIDE)


def _first_obj(f: Path, n: int = 40) -> dict:
    """前几行里第一条带 sessionId 的记录。"""
    try:
        with open(f, "rb") as fh:
            for i, ln in enumerate(fh):
                if i >= n:
                    break
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if isinstance(o, dict) and o.get("sessionId"):
                    return o
    except OSError:
        pass
    return {}


def _sub_info(f: Path, rel: str, parent: str) -> dict:
    o = _first_obj(f)
    aid = o.get("agentId") or (f.stem[6:] if f.stem.startswith("agent-") else f.stem)
    meta = {}
    mf = f.with_name(f.stem + ".meta.json")
    if mf.exists():
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return {"path": rel, "type": "subagent", "agentId": aid, "parent": parent, "size": f.stat().st_size,
            "agentType": meta.get("agentType") or meta.get("subagent_type"), "title": meta.get("description") or meta.get("title")}


# ---------- 找子会话 ----------

def claude_children(main: Path, sid: str) -> list[tuple[Path, str]]:
    """主会话文件旁边属于它的文件 → [(文件, 相对路径)]：<会话号>/ 下面的全部（子代理、meta、tool-results…）+ 老版本的 agent-*.jsonl。"""
    out: list[tuple[Path, str]] = []
    d = main.parent / sid
    if d.is_dir():
        out += [(f, f.relative_to(d).as_posix()) for f in sorted(d.rglob("*")) if f.is_file()]
    have = {r for _, r in out}
    for f in sorted(main.parent.glob("agent-*.jsonl")):                 # 老版本：放在项目目录顶层
        if f"subagents/{f.name}" not in have and _first_obj(f).get("sessionId") == sid:
            out.append((f, f"subagents/{f.name}"))
    return out


def codex_head(f: Path) -> dict:
    """rollout 第一行 session_meta 的 payload（整份，含 source 里的父线程）。"""
    try:
        with open(f, "rb") as fh:
            m = json.loads(fh.readline())
        return (m.get("payload") or {}) if isinstance(m, dict) and m.get("type") == "session_meta" else {}
    except Exception:
        return {}


def _parent_of_codex(m: dict) -> str | None:
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("parent_thread_id", "parentThreadId", "parent_id") and isinstance(v, str):
                    return v
                r = walk(v)
                if r:
                    return r
        return None
    return walk(m.get("source")) if isinstance(m.get("source"), (dict, list)) else None


def codex_children(sid: str) -> list[tuple[Path, str]]:
    out = []
    for f in S.rollouts():
        if _parent_of_codex(codex_head(f)) == sid:
            out.append((f, f"subagents/{f.name}"))
    return sorted(out, key=lambda x: x[1])


def children(agent: str, sid: str, main: Path) -> list[tuple[Path, str]]:
    try:
        if agent == "claude":
            return claude_children(main, sid)
        if agent == "codex":
            return codex_children(sid)
    except Exception:
        pass
    return []


def manifest(agent: str, sid: str, kids: list[tuple[Path, str]], title: str | None, cwd: str | None, main_name: str) -> dict:
    subs, extra = [], []
    for f, rel in kids:
        if f.name.endswith(".jsonl") and (rel.startswith("subagents/") or agent == "codex"):
            subs.append(_sub_info(f, rel, sid) if agent == "claude" else
                        {"path": rel, "type": "subagent", "agentId": (codex_head(f).get("id") or f.stem[-36:]), "parent": sid,
                         "size": f.stat().st_size, "title": None})
        else:
            extra.append({"path": rel, "size": f.stat().st_size})
    return {"format": FORMAT, "agent": agent, "main": {"file": main_name, "id": sid, "type": "main", "title": title, "cwd": cwd},
            "children": subs, "files": extra}


# ---------- 导出 ----------

def export_sidecar(agent: str, sid: str, main: Path, out: Path, title: str | None = None, cwd: str | None = None) -> dict | None:
    """有子会话就在导出文件旁边建 <导出文件>.ccss/ 文件夹；没有就什么也不做。"""
    kids = children(agent, sid, main)
    if not kids:
        return None
    d = sidecar_dir(out)
    for f, rel in kids:
        t = d / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, t)
    man = manifest(agent, sid, kids, title, cwd, out.name)
    (d / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(d), "subs": len(man["children"]), "files": len(kids)}


# ---------- 导入：一个主文件带了哪些子会话 ----------

def find(p: Path, agent: str, sid: str | None) -> dict:
    """导入时：这个主文件带的子会话 → {"kids": [(文件, 相对路径)], "children": [...清单...], "where": 说明}"""
    d = sidecar_dir(p)
    if d.is_dir():
        kids = [(f, f.relative_to(d).as_posix()) for f in sorted(d.rglob("*")) if f.is_file() and f.name != "manifest.json"]
        try:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            man = manifest(agent, sid or "", kids, None, None, p.name)
        return {"kids": kids, "children": man.get("children") or [], "where": d.name}
    if agent == "claude" and sid and p.stem == sid:               # 直接从 Claude Code 的项目目录里选的原始文件
        kids = claude_children(p, sid)
        if kids:
            return {"kids": kids, "children": manifest(agent, sid, kids, None, None, p.name)["children"], "where": sid}
    return {"kids": [], "children": [], "where": None}


def rewrite_line(ln: bytes, old: str | None, new: str) -> bytes:
    """子会话的一行：sessionId（= 主会话号）换成新的，别的不动。"""
    s = ln.strip()
    if not s:
        return b""
    if old:
        pat = b'"sessionId":"' + old.encode() + b'"'
        if s.count(pat) == 1:
            return s.replace(pat, b'"sessionId":"' + new.encode() + b'"') + b"\n"
    try:
        j = json.loads(s)
    except Exception:
        return s + b"\n"
    if isinstance(j, dict) and "sessionId" in j and (old is None or j["sessionId"] == old):
        j["sessionId"] = new
    return json.dumps(j, ensure_ascii=False).encode("utf-8") + b"\n"


def write_claude_kids(kids: list, old_sid: str | None, new_sid: str, dst_dir: Path, led: dict, src_agent: str = "claude", stop=lambda: False) -> int:
    """把子会话写到 <项目>/<新会话号>/ 下（Claude 的原样换会话号；Codex 的转成 Claude 子代理记录）。kids：[(Path 或 bytes, 相对路径)]。→ 写了几个子会话"""
    if not kids:
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    led["steps"].append({"t": "created", "path": str(dst_dir)})   # 撤回时整个文件夹挪进回收站
    n = 0
    for src, rel in kids:
        if stop():
            from .bigimport import Cancelled
            raise Cancelled()
        t = dst_dir / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        is_sub = rel.endswith(".jsonl") and rel.startswith("subagents/")
        if src_agent == "codex" and is_sub:
            _codex_kid(src, new_sid, t)
            n += 1
            continue
        if not is_sub:                                           # meta.json、tool-results…：原样
            if isinstance(src, (bytes, bytearray)):
                t.write_bytes(src)
            else:
                shutil.copyfile(src, t)
            continue
        old = old_sid
        lines = src.splitlines(True) if isinstance(src, (bytes, bytearray)) else None
        with open(t, "wb") as fo:
            if lines is not None:
                for ln in lines:
                    fo.write(rewrite_line(ln, old, new_sid))
            else:
                with open(src, "rb") as fi:
                    for ln in fi:
                        fo.write(rewrite_line(ln, old, new_sid))
        n += 1
    return n


def _codex_kid(src, new_sid: str, t: Path) -> None:
    """Codex 子代理 rollout → Claude Code 子代理记录（isSidechain、agentId = Codex 线程号前 16 位）。"""
    import tempfile, os
    from . import codex_import as CI
    tmp = None
    if isinstance(src, (bytes, bytearray)):
        fd, tmp = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "wb") as fh:
            fh.write(src)
        src = Path(tmp)
    try:
        aid = (codex_head(Path(src)).get("id") or Path(src).stem[-36:]).replace("-", "")[:16]
        b = CI.build(Path(src), new_sid, CI.claude_version())
        with open(t.with_name(f"agent-{aid}.jsonl"), "wb") as fo:
            for r in b["records"]:
                if r.get("type") in ("user", "assistant"):
                    r = dict(r, isSidechain=True, agentId=aid, sessionId=new_sid)
                    fo.write(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n")
    finally:
        if tmp:
            os.unlink(tmp)

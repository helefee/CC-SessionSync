# -*- coding: utf-8 -*-
"""0.4.8：大文件 / 批量导入。

0.4.7 的导入是页面把整个文件读成字符串、拼进一个 JSON 发给本程序，本程序再整份读进内存解析，
一批文件的内容还要一起留到写完——十几个、上百 MB 的文件一起选，页面（WebView）先撑爆内存就崩了。
现在：
- 桌面版用系统的「打开文件 / 选文件夹」对话框，页面只拿到路径，文件内容不经过页面；
- 浏览器里退回到上传，但分块（8 MB 一块）、原样字节直接写盘，不转 base64、不拼 JSON；
- 预览只读开头几百行（大小、哪个应用、标题、前几条消息），消息条数另起一遍流式数（不急，慢慢补上）；
- 导入在后台一个一个文件做，Claude Code → Claude Code 是一行一行流式复制（只换会话号、换标题），
  做完一个放掉一个；有进度、能取消；一个文件出错不影响其它的；整批一笔账，一次撤回。
- 不再有 200 MB 上限。
"""
from __future__ import annotations

import gc
import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import sessions as S

HEAD_LINES = 300                     # 预览只看开头这么多行
HEAD_BYTES = 8 * 1024 * 1024
WHOLE_MAX = 16 * 1024 * 1024         # 比这个小的，预览时顺手把条数数准；更大的「数条数」另起请求
STORE_MAX = 16 * 1024 * 1024         # 导入完顺手进备份库的上限；更大的等下一次自动备份
WARN_SIZE = 4 * 1024 * 1024 * 1024   # 超过 4 GB 只提醒（会慢），不拦
CHUNK_LINES = 2000


class Cancelled(Exception):
    pass


def kind(name: str) -> str | None:
    n = name.lower()
    if n.endswith(".jsonl.zstd"):
        return "zstd"
    if n.endswith(".jsonl"):
        return "jsonl"
    if n.endswith((".md", ".markdown")):
        return "md"
    if n.endswith((".html", ".htm")):
        return "html"
    return None


def list_folder(folder: str, recursive: bool = False) -> list[str]:
    """文件夹里能导入的文件（缺省不进子文件夹），按名字排。"""
    d = Path(folder).expanduser()
    if not d.is_dir():
        raise ValueError(f"不是文件夹：{folder}")
    it = d.rglob("*") if recursive else d.iterdir()
    return sorted(str(p) for p in it if p.is_file() and kind(p.name)
                  and not any(x.endswith(".ccss") for x in p.relative_to(d).parts[:-1]))   # 导出时旁边的子会话文件夹：跟着主文件走


def _head(p: Path) -> bytes:
    out, n = [], 0
    with open(p, "rb") as fh:
        for ln in fh:
            out.append(ln)
            n += len(ln)
            if len(out) >= HEAD_LINES or n >= HEAD_BYTES:
                break
    return b"".join(out)


def _first_user(es: list[dict]) -> str | None:
    u = next((e["text"] for e in es if e["role"] == "user" and e.get("text")), "")
    return " ".join(u.split())[:40] or None


def _scan_one(path: str) -> dict:
    """一个文件的预览行：不整份读（.md / .html / .zstd 和小的非 Claude jsonl 除外）。"""
    from . import exports as EX
    p = Path(path)
    row = {"file": str(p), "name": p.name}
    try:
        k = kind(p.name)
        if not k:
            raise ValueError("只认 .md / .html / .jsonl")
        if not p.is_file():
            raise ValueError("文件不在了")
        size = p.stat().st_size
        row["size"] = size
        if size > WARN_SIZE:
            row["warn"] = "文件很大（超过 4 GB），导入要好一会儿"
        if k == "jsonl" and size > WHOLE_MAX:
            head = _head(p)
            agent = EX.sniff_jsonl(head)
            if agent not in ("claude", "codex"):          # 镜像 / dsh：整份格式，只能整份读
                r = EX.parse_file(str(p))
                r.pop("raw", None)
                return dict(row, **{**r, "size": size})
            es = S.parse_bytes(agent, head)
            if not any(e["role"] in ("user", "assistant") for e in es):
                raise ValueError(f"看着像 {EX.APP_NAME[agent]} 的记录，但开头一条对话消息都没有")
            cwd = None
            for o in EX._objs(head, HEAD_LINES):
                if isinstance(o, dict):
                    cwd = o.get("cwd") or ((o.get("payload") or {}).get("cwd") if o.get("type") == "session_meta" else None)
                    if cwd:
                        break
            return dict(row, how="lossless", agent=agent, format="jsonl", title=_first_user(es) or p.name.split(".")[0], cwd=cwd,
                        messages=None, preview=[dict(e, text=e["text"][:600]) for e in es[:12]])
        r = EX.parse_file(str(p))
        r.pop("raw", None)
        return dict(row, **{**r, "size": size})
    except ValueError as e:
        return dict(row, error=str(e))
    except Exception as e:
        return dict(row, error=f"{type(e).__name__}: {e}")


def _relation(p: Path, row: dict) -> dict:
    """类型（main / subagent）、主会话号、带了几个子会话。"""
    from . import bundle
    agent, k = row.get("agent"), kind(p.name)
    kids = row.pop("kids", None)
    if k in ("md", "html"):
        row.update(type="main", subs=row.get("subs") or 0)
        return row
    if k != "jsonl" or agent not in ("claude", "codex"):
        row.update(type="main", subs=0)
        return row
    sid, parent, aid = None, None, None
    if agent == "claude":
        o = bundle._first_obj(p)
        sid = o.get("sessionId")
        if o.get("isSidechain") and o.get("agentId"):
            parent, aid = sid, o["agentId"]
    else:
        m = bundle.codex_head(p)
        sid = m.get("id")
        parent = bundle._parent_of_codex(m)
        aid = sid
    if parent:
        return dict(row, type="subagent", parent=parent, agentId=aid, subs=0)
    f = bundle.find(p, agent, sid)
    return dict(row, type="main", sid=sid, subs=len(f["children"]), children=f["children"][:200], sidecar=f["where"])


def scan_one(path: str) -> dict:
    p = Path(path)
    if kind(p.name) == "jsonl" and p.is_file():                 # 子代理的记录：里面全是 isSidechain，按普通会话读会是空的
        from . import bundle
        o = bundle._first_obj(p)
        if o.get("isSidechain") and o.get("agentId") and o.get("type") in ("user", "assistant"):
            info = bundle._sub_info(p, "subagents/" + p.name, o.get("sessionId"))
            return {"file": str(p), "name": p.name, "size": p.stat().st_size, "agent": "claude", "format": "jsonl", "how": "lossless",
                    "type": "subagent", "parent": o.get("sessionId"), "agentId": info["agentId"], "title": info.get("title") or p.stem,
                    "cwd": o.get("cwd"), "messages": None, "preview": []}
    r = _scan_one(path)
    if "error" in r:
        r.pop("kids", None)
        return r
    try:
        return _relation(Path(path), r)
    except Exception:
        r.pop("kids", None)
        return dict(r, type="main", subs=0)


ORPHAN = "这是子代理（子会话）的记录，不单独导入：和它的主会话一起选（或者选整个文件夹），会归到主会话下面"


def group(rows: list[dict]) -> list[dict]:
    """批量时：子会话文件归到同一批里的主会话下面（不当成单独的会话）；找不到主会话的给说明。"""
    mains = {r["sid"]: r for r in rows if r.get("type") == "main" and r.get("sid") and "error" not in r}
    out = []
    for r in rows:
        if r.get("type") == "subagent" and "error" not in r:
            m = mains.get(r.get("parent"))
            if m is None:
                out.append(dict(r, error=ORPHAN))
                continue
            have = {c.get("agentId") for c in m.get("children") or []}
            if r.get("agentId") not in have:                    # 旁边文件夹里没有的，才单独接上
                m.setdefault("attach", []).append(r["file"])
                m.setdefault("children", []).append({"path": "subagents/" + Path(r["file"]).name, "type": "subagent",
                                                     "agentId": r.get("agentId"), "parent": r.get("parent"), "size": r.get("size")})
                m["subs"] = (m.get("subs") or 0) + 1
            m.setdefault("grouped", []).append(r["name"])
            continue
        out.append(r)
    return out


def scan(paths: list[str] | None = None, folder: str | None = None, recursive: bool = False) -> dict:
    ps = list(paths or [])
    if folder:
        ps += list_folder(folder, recursive)
    seen, uniq = set(), []
    for x in ps:
        k = os.path.normcase(os.path.abspath(x))
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    raw = [scan_one(x) for x in uniq]
    rows = group(raw)
    return {"ok": True, "rows": rows, "folder": folder, "total": sum(r.get("size") or 0 for r in rows),
            "grouped": len(raw) - len(rows)}


def count(path: str, agent: str = "claude") -> dict:
    """流式数一遍：消息条数（不算工具）、标题（Claude：最后一个自定义标题 / 摘要）、工作目录。内存只用一小块。"""
    parse = S.parse_claude if agent == "claude" else S.parse_codex
    n, title, cwd, first = 0, None, None, None
    with open(path, "rb") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if agent == "claude":
                cwd = cwd or d.get("cwd")
                t = d.get("type")
                if t == "custom-title" and d.get("customTitle"):
                    title = d["customTitle"]
                elif t == "summary" and d.get("summary") and not title:
                    title = d["summary"]
            elif d.get("type") == "session_meta":
                cwd = cwd or (d.get("payload") or {}).get("cwd")
            for e in parse(d):
                if e["role"] != "tool":
                    n += 1
                    if first is None and e["role"] == "user":
                        first = " ".join(e["text"].split())[:40]
    return {"ok": True, "file": path, "messages": n, "title": title or first, "cwd": cwd}


# ---------- 写 ----------

def _copy_claude(src: Path, dst: Path, new_sid: str, title_fn, tick, stop) -> dict:
    """Claude Code 会话流式复制：换 sessionId、去掉老标题、末尾加新标题。原样字节能不动就不动。→ {title, cwd}"""
    old, old_pat, new_pat = None, None, None
    seen_title = cwd = first = None
    tmp = dst.with_name(dst.name + ".ccss-tmp")
    dst.parent.mkdir(parents=True, exist_ok=True)
    done, lines = 0, 0
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            for ln in fi:
                done += len(ln)
                lines += 1
                if lines % CHUNK_LINES == 0:
                    tick(done)
                    if stop():
                        raise Cancelled()
                s = ln.strip()
                if not s:
                    continue
                if old_pat and s.count(old_pat) == 1 and b'"custom-title"' not in s and b'"summary"' not in s:
                    fo.write(s.replace(old_pat, new_pat) + b"\n")    # 快路：只换会话号，其余字节原样
                    if first is None and b'"type":"user"' in s:
                        try:
                            first = _first_user(S.parse_claude(json.loads(s)))
                        except Exception:
                            pass
                    continue
                try:
                    j = json.loads(s)
                except Exception:
                    fo.write(s + b"\n")
                    continue
                if isinstance(j, dict):
                    cwd = cwd or j.get("cwd")
                    t = j.get("type")
                    if t == "custom-title":
                        seen_title = j.get("customTitle") or seen_title
                        continue                               # 老标题不要了，最后加新的
                    if t == "summary" and j.get("summary") and not seen_title:
                        seen_title = j["summary"]
                    if isinstance(j.get("sessionId"), str):
                        if old is None:
                            old = j["sessionId"]
                            old_pat = json.dumps({"sessionId": old})[1:-1].encode()       # "sessionId": "x"
                            old_pat = old_pat.replace(b'": "', b'":"')
                            new_pat = old_pat.replace(old.encode(), new_sid.encode())
                        j["sessionId"] = new_sid
                    if first is None and t == "user":
                        first = _first_user(S.parse_claude(j))
                fo.write(json.dumps(j, ensure_ascii=False).encode("utf-8") + b"\n")
            title = title_fn(seen_title or first)
            fo.write(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": new_sid}, ensure_ascii=False).encode("utf-8") + b"\n")
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    tick(done)
    return {"title": title, "cwd": cwd, "old": old}


def _kids_for(p: Path, row: dict, agent: str, old_sid: str | None) -> list:
    from . import bundle
    kids = list(bundle.find(p, agent, old_sid)["kids"]) if old_sid or kind(p.name) == "jsonl" else []
    have = {r for _, r in kids}
    for f in row.get("attach") or []:                          # 同一批里单独选上的子会话文件
        rel = "subagents/" + Path(f).name
        if rel not in have:
            kids.append((Path(f), rel))
            have.add(rel)
            mf = Path(f).with_name(Path(f).stem + ".meta.json")
            if mf.exists() and f"subagents/{mf.name}" not in have:
                kids.append((mf, f"subagents/{mf.name}"))
    return kids


def import_one(path: str, row: dict | None, cwd: str | None, led: dict, od: Path, desktop: bool, tick=lambda n: None, stop=lambda: False) -> dict:
    """导入一个文件成新的 Claude Code 会话（记进 led），连同它的子会话。标题保持原样（0.4.8 起不加「（导入）」）。
    Claude Code 的 jsonl 走流式复制；其它的整份读（一次一个）。"""
    from . import exports as EX, timeline, codex_import as CI, bundle
    p = Path(path)
    k = kind(p.name)
    row = row or {}
    agent = row.get("agent")
    if k == "jsonl" and not agent:
        agent = EX.sniff_jsonl(_head(p))
    if row.get("type") == "subagent":
        raise ValueError(ORPHAN)
    sid = str(uuid.uuid4())
    if k == "jsonl" and agent == "claude":
        c = CI.norm_cwd(cwd or row.get("cwd")) or None
        if not c:                                              # 开头没记工作目录：先流式找一个
            c = CI.norm_cwd(count(str(p))["cwd"]) or None
        c = c or str(Path.home())
        f = S.claude_home() / "projects" / CI.project_slug(c) / f"{sid}.jsonl"
        led["steps"].append({"t": "created", "path": str(f)})   # 先记账：写到一半断电也能撤
        r = _copy_claude(p, f, sid, lambda t: t or row.get("title") or p.name.split(".")[0], tick, stop)
        old = r.get("old") or row.get("sid")
        subs = bundle.write_claude_kids(_kids_for(p, row, "claude", old), old, sid, f.parent / sid, led, "claude", stop)
        if f.stat().st_size <= STORE_MAX:
            try:
                with S._db_lock:
                    db = S._db()
                    S._store(db, "claude", sid, f, "restored")
                    db.commit()
                    db.close()
            except Exception:
                pass
        res = {"id": sid, "file": str(f), "cwd": c, "title": r["title"], "resume": CI.resume_cmd(c, sid)}
        if desktop:
            led.setdefault("regs", []).append({"sid": sid, "cwd": c, "title": r["title"], "mtime": time.time()})
            res["desktop"] = "pending"
        return dict(res, how="lossless", agent_from="claude", subs=subs)
    if stop():
        raise Cancelled()
    r = EX.parse_file(str(p))
    tick(p.stat().st_size // 2)
    res = timeline.write_claude_session(r["agent"], r["raw"], r["title"], cwd or r.get("cwd"), led, od, desktop)
    subs = 0
    if r["agent"] in ("claude", "codex"):
        kids = r.get("kids") or []
        if not kids and k == "jsonl":
            old = row.get("sid") or (bundle.codex_head(p).get("id") if r["agent"] == "codex" else bundle._first_obj(p).get("sessionId"))
            kids = _kids_for(p, row, r["agent"], old)
        else:
            old = r.get("sid")
        nf = Path(res["file"])
        subs = bundle.write_claude_kids(kids, old, res["id"], nf.parent / res["id"], led, r["agent"], stop)
    tick(p.stat().st_size)
    return dict(res, how=r["how"], agent_from=r["agent"], subs=subs)


def _check(f: str) -> dict:
    try:
        from . import nativecheck as NC
        return NC.check_claude(Path(f))
    except Exception as e:
        return {"ok": False, "issues": [f"自检出错：{type(e).__name__}: {e}"]}


# ---------- 后台批量 ----------

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _summary(n: int) -> str:
    return f"导入 {n} 个 .md / .html / .jsonl 成 Claude Code 会话"


def run(files: list[str], cwd: str | None = None, desktop: bool = True, st: dict | None = None, rows: dict | None = None) -> dict:
    """一个一个导入（同一笔账）。st：进度（后台任务用），里面 cancel=True 就停。"""
    from . import timeline
    st = st if st is not None else {}
    rows = rows or {}
    sizes = []
    for f in files:
        try:
            sizes.append(Path(f).stat().st_size)
        except OSError:
            sizes.append(0)
    st.update(n=len(files), i=0, total=sum(sizes), done_bytes=0, made=[], errors=[], state="running", started=time.time())
    od, led = S._new_op("import", items=[], summary=_summary(len(files)))
    st["op"] = od.name
    made, errors = st["made"], st["errors"]
    base = 0
    for i, f in enumerate(files):
        if st.get("cancel"):
            break
        st.update(i=i + 1, cur=Path(f).name, cur_size=sizes[i], cur_done=0)

        def tick(n, _b=base):
            st["cur_done"] = n
            st["done_bytes"] = _b + n

        try:
            res = import_one(f, rows.get(f), cwd, led, od, desktop, tick, lambda: bool(st.get("cancel")))
            led["items"].append({"agent": "claude", "id": res["id"]})
            res["check"] = _check(res["file"])
            made.append(dict(res, file_from=f))
        except Cancelled:
            led["steps"] = [s for s in led["steps"] if Path(s.get("path", "")).exists() or s.get("t") != "created"]
            break
        except Exception as e:                                 # 一个出错不影响后面的
            led["steps"] = [s for s in led["steps"] if Path(s.get("path", "")).exists() or s.get("t") != "created"]
            errors.append(f"{f}: {e}" if isinstance(e, ValueError) else f"{f}: {type(e).__name__}: {e}")
        base += sizes[i]
        st["done_bytes"] = base
        led.update(made=made, errors=errors)
        S._save_led(od, led)                                   # 每做完一个就记账：中途出事也能撤
        gc.collect()
    cancelled = bool(st.get("cancel"))
    led.update(made=made, errors=errors, cancelled=cancelled)
    if cancelled:
        led["summary"] = _summary(len(files)) + f"（取消了，做完 {len(made)} 个）"
    job = timeline._finish_regs(od, led) if desktop else (S._save_led(od, led) or None)
    try:
        S.invalidate()
    except Exception:
        pass
    st.update(state="cancelled" if cancelled else "done", desktop=job, ended=time.time(), cur=None)
    return {"ok": not errors, "dry": False, "made": made, "errors": errors, "op": od.name, "desktop": job, "cancelled": cancelled}


def start(files: list[str], cwd: str | None = None, desktop: bool = True, rows: list[dict] | None = None) -> dict:
    files = [str(f) for f in files if f]
    if not files:
        raise ValueError("没选文件")
    bad = [f for f in files if not kind(f)]
    if bad:
        raise ValueError("只认 .md / .html / .jsonl：" + "、".join(Path(b).name for b in bad))
    with _LOCK:
        if any(j["state"] == "running" for j in JOBS.values()):
            raise ValueError("上一批还在导入，等它做完或先取消")
        jid = uuid.uuid4().hex[:12]
        st = JOBS[jid] = {"id": jid, "state": "starting", "n": len(files), "i": 0}
    rmap = {r["file"]: r for r in (rows or []) if isinstance(r, dict) and r.get("file")}

    def go():
        try:
            st["result"] = run(files, cwd, desktop, st, rmap)
        except Exception as e:
            st.update(state="failed", error=f"{type(e).__name__}: {e}")
    threading.Thread(target=go, name="ccss-import", daemon=True).start()
    return {"ok": True, "job": jid}


def job(jid: str) -> dict:
    st = JOBS.get(jid)
    if not st:
        return {"ok": False, "error": "没有这个导入任务"}
    return dict({k: v for k, v in st.items() if k != "result"}, ok=True, result=st.get("result"))


def cancel(jid: str) -> dict:
    st = JOBS.get(jid)
    if not st:
        return {"ok": False, "error": "没有这个导入任务"}
    st["cancel"] = True
    return {"ok": True}


# ---------- 浏览器里的分块上传（桌面版用不到） ----------

def upload(up: str | None, name: str, off: int, body) -> dict:
    """把一块原样字节接到 imports/<up>/<name> 后面。up 为空 = 新建一批。body：可以 read(n) 的流 + 长度。"""
    from . import paths
    import re
    name = Path(str(name or "")).name
    if not kind(name):
        raise ValueError(f"只认 .md / .html / .jsonl：{name}")
    if up and not re.fullmatch(r"[0-9A-Za-z-]{6,40}", up):
        raise ValueError("上传批次号不对")
    up = up or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = paths.HOME / "imports" / up
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    have = f.stat().st_size if f.exists() else 0
    if off == 0 and have:
        f.unlink()
        have = 0
    if off != have:
        raise ValueError(f"上传断了（{name}：服务端有 {have} 字节，页面从 {off} 开始）")
    stream, n = body
    with open(f, "ab") as fh:
        left = n
        while left > 0:
            b = stream.read(min(left, 1 << 20))
            if not b:
                break
            fh.write(b)
            left -= len(b)
    return {"ok": True, "up": up, "path": str(f), "size": f.stat().st_size}

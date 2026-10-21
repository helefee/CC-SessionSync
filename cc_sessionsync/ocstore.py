# -*- coding: utf-8 -*-
"""OpenCode 和 ZCode 的会话（0.2.7）。

OpenCode（anomalyco/opencode，原 sst/opencode，开源的终端 / 桌面编程助手）：
  数据目录 $XDG_DATA_HOME/opencode，没设就是 ~/.local/share/opencode（Windows 上也是 C:\\Users\\<你>\\.local\\share\\opencode）。
  会话在 opencode.db（SQLite，WAL）里，两代表结构可能同时在：
    1.x：session / message / part（message.data、part.data 是 JSON；part 有 text / reasoning / tool / file / step-start …）
    2.x：session_v2 / session_message（type = user / assistant / idle …，assistant 的 data.content 是 text / reasoning / tool 的数组）
  更老的版本（<1.0）是 storage/ 下的一堆 JSON，只数个数，不读。
  写入：用 OpenCode 自己的命令行 `opencode session import --standalone`（2.x）/ `opencode import`（1.x），只建新会话。
ZCode（智谱 Z.ai 的 GLM 编程助手桌面版，Electron）：
  引擎 zcode-cli 是 OpenCode 1.x 的分支，数据在 $ZCODE_HOME，没设就是 ~/.zcode；会话在 cli/db/db.sqlite
  （session / message / part，多了 sequence、task_type、time_deleted、message.data.semantics.kind 这些）。
  桌面那边的任务列表在 v2/tasks-index.sqlite。ZCode 是闭源的，库里还有迁移账本 / 代际这些，没法验证写进去它认不认，
  所以 ZCode 只读；转给 ZCode 走上下文文件。

读的时候不碰原库：把 db + -wal 复制到临时目录再打开副本（只读），所以应用开着也能读、也不会留下 -shm 的改动。
不读：账号 / 凭据表（account、credential、control_account …）、service.json、credentials.json。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import ir, paths

OUT_CLIP = 20000
NAMES = {"opencode": "OpenCode", "zcode": "ZCode"}
SOURCE_NAME = {"opencode2": "OpenCode 2.x", "opencode1": "OpenCode 1.x", "zcode": "ZCode"}
_KEEP_KINDS = {"user_prompt", "assistant_response"}


def _set(k: str) -> str | None:
    try:
        v = json.loads((paths.HOME / "settings.json").read_text(encoding="utf-8")).get(k) or ""
        return v if v and Path(v).is_dir() else None
    except Exception:
        return None


def data_dir(agent: str) -> Path:
    if agent == "opencode":
        v = os.environ.get("CC_SESSIONSYNC_OPENCODE_DATA") or _set("opencode_dir")
        if v:
            return Path(v)
        x = os.environ.get("XDG_DATA_HOME")
        return Path(x) / "opencode" if x else Path.home() / ".local" / "share" / "opencode"
    v = os.environ.get("CC_SESSIONSYNC_ZCODE_HOME") or _set("zcode_dir") or os.environ.get("ZCODE_HOME")
    return Path(v) if v else Path.home() / ".zcode"


def db_path(agent: str) -> Path:
    d = data_dir(agent)
    return d / "opencode.db" if agent == "opencode" else d / "cli" / "db" / "db.sqlite"


def mirror_dir(agent: str) -> Path:
    return paths.HOME / "mirror" / agent


# ---------- 安装 / 进程 ----------

def install_info(agent: str) -> dict | None:
    fake = os.environ.get(f"CC_SESSIONSYNC_{agent.upper()}_INSTALL")
    cands = [Path(fake)] if fake else []
    if os.name == "nt":
        la = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        cands += [la / "Programs" / "@opencodedesktop", la / "Programs" / "OpenCode", pf / "OpenCode"] if agent == "opencode" \
            else [pf / "ZCode", la / "Programs" / "ZCode"]
    elif sys.platform == "darwin":
        cands += [Path(f"/Applications/{NAMES[agent]}.app/Contents")]
    exe = "OpenCode.exe" if agent == "opencode" else "ZCode.exe"
    for d in cands:
        if (d / exe).exists() or (d / "resources").is_dir():
            info = {"path": str(d), "exe": str(d / exe), "name": NAMES[agent], "version": None}
            for vf in (d / "resources" / "app-update.yml", d / "resources" / "app" / "package.json"):
                try:
                    t = vf.read_text(encoding="utf-8", errors="replace")
                    if vf.suffix == ".json":
                        info["version"] = json.loads(t).get("version")
                        break
                except Exception:
                    pass
            return info
    return None


def find_cli() -> str | None:
    """OpenCode 的命令行：设置 / 环境变量 > PATH 上的 opencode > 桌面版自带的 opencode-cli(.exe)。"""
    v = os.environ.get("CC_SESSIONSYNC_OPENCODE_CLI")
    if not v:
        try:
            v = json.loads((paths.HOME / "settings.json").read_text(encoding="utf-8")).get("opencode_cli") or ""
        except Exception:
            v = ""
    if v and Path(v).exists():
        return v
    for n in ("opencode", "opencode-cli"):
        w = shutil.which(n)
        if w:
            return w
    info = install_info("opencode")
    cands = []
    if info:
        cands.append(Path(info["path"]) / "resources" / ("opencode-cli.exe" if os.name == "nt" else "opencode-cli"))
    if os.name == "nt":
        ad = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "ai.opencode.desktop" / "cli"
        try:
            cands += sorted(ad.glob("*/opencode-cli.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
    for c in cands:
        if c.exists():
            return str(c)
    return None


def cli_version(cli: str) -> str | None:
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                           env=_cli_env(tempfile.gettempdir()), cwd=tempfile.gettempdir())
        v = (r.stdout or "").strip().splitlines()
        return v[-1].strip() if v else None
    except Exception:
        return None


def procs(agent: str) -> list[dict]:
    fake = os.environ.get(f"CC_SESSIONSYNC_FAKE_{agent.upper()}_PROCS")
    if fake:
        try:
            return json.loads(Path(fake).read_text())
        except Exception:
            return []
    names = {"opencode": ("opencode.exe", "opencode-cli.exe", "opencode", "opencode-cli"),
             "zcode": ("zcode.exe", "zcode")}[agent]
    out = []
    try:
        if os.name == "nt":
            from . import detect
            for p in detect._win_procs({"total": 0, "denied": 0, "cmd_denied": 0, "errors": []}):
                if (p.get("name") or "").lower() in names:
                    out.append({"pid": p["pid"], "name": p["name"]})
        elif sys.platform.startswith("linux"):
            for d in Path("/proc").iterdir():
                if not d.name.isdigit() or int(d.name) == os.getpid():
                    continue
                try:
                    cmd = (d / "cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                first = cmd[0].decode("utf-8", "replace").rsplit("/", 1)[-1].lower() if cmd else ""
                if first in names:
                    out.append({"pid": int(d.name), "name": first})
    except Exception:
        pass
    return out


# ---------- 读库（副本） ----------

def _sig(agent: str) -> tuple:
    f = db_path(agent)
    out = []
    for x in (f, f.with_name(f.name + "-wal")):
        try:
            st = x.stat()
            out.append((str(x), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(x), 0, 0))
    return tuple(out)


class _Snap:
    """把库和 -wal 复制一份再只读打开；用完删。"""

    def __init__(self, f: Path):
        self.f, self.tmp, self.con = f, None, None

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccss-oc-"))
        dst = self.tmp / "s.db"
        shutil.copyfile(self.f, dst)
        w = self.f.with_name(self.f.name + "-wal")
        if w.exists():
            shutil.copyfile(w, self.tmp / "s.db-wal")
        self.con = sqlite3.connect(str(dst))          # 副本：WAL 要能回放，所以不用 mode=ro
        self.con.row_factory = sqlite3.Row
        return self.con

    def __exit__(self, *a):
        try:
            self.con.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)


def _tables(con) -> dict[str, set]:
    out = {}
    for (n,) in con.execute("select name from sqlite_master where type='table'"):
        out[n] = {r[1] for r in con.execute(f"pragma table_info(`{n}`)")}
    return out


def _j(v):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else v
    except Exception:
        return None


def _clip(s, n=OUT_CLIP):
    s = "" if s is None else (s if isinstance(s, str) else json.dumps(s, ensure_ascii=False))
    return s if len(s) <= n else s[:n] + f"\n…（还有 {len(s) - n} 字）"


def _tool_out(st: dict) -> str:
    o = st.get("output")
    if o is None and isinstance(st.get("content"), list):
        o = "\n".join(x.get("text", "") for x in st["content"] if isinstance(x, dict) and x.get("type") == "text")
    return _clip(o or "")


def _model(m) -> str | None:
    if isinstance(m, dict):
        a, b = m.get("providerID"), m.get("id") or m.get("modelID")
        return f"{a}/{b}" if a and b else (b or None)
    return m if isinstance(m, str) else None


def _items_v2(rows) -> list[dict]:
    items = []
    for r in rows:
        d = _j(r["data"]) or {}
        t = r["type"]
        ts = r["time_created"]
        if t == "user":
            files = d.get("files") or []
            imgs = sum(1 for f in files if isinstance(f, dict) and str(f.get("mime") or f.get("mediaType") or "").startswith("image/"))
            if d.get("text") or imgs:
                items.append({"role": "user", "kind": "text", "text": d.get("text") or "", "ts": ts, "images": imgs or None})
        elif t == "assistant":
            mdl = _model(d.get("model"))
            for c in d.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    items.append({"role": "assistant", "kind": "text", "text": c["text"], "ts": ts, "model": mdl})
                elif c.get("type") == "reasoning" and c.get("text"):
                    items.append({"role": "assistant", "kind": "thinking", "text": c["text"], "ts": ts, "model": mdl})
                elif c.get("type") == "tool":
                    st = c.get("state") or {}
                    items.append({"role": "assistant", "kind": "tool", "tool": c.get("name"), "input": st.get("input"),
                                  "output": _tool_out(st), "status": "error" if st.get("status") == "error" else st.get("status"),
                                  "error": _clip(st.get("error")) if st.get("error") else None, "ts": ts, "model": mdl})
    return items


def _items_v1(msgs, parts_by: dict) -> list[dict]:
    items = []
    for m in msgs:
        d = _j(m["data"]) or {}
        role = d.get("role")
        sem = d.get("semantics") if isinstance(d.get("semantics"), dict) else None
        if sem and sem.get("kind") and sem["kind"] not in _KEEP_KINDS:
            continue                              # ZCode 的提醒 / 通知 / 时间线这些不是对话
        mdl = _model({"providerID": d.get("providerID") or d.get("providerId"), "id": d.get("modelID") or d.get("modelId")}) \
            if role == "assistant" else None
        ts = (d.get("time") or {}).get("created") or m["time_created"]
        imgs, texts = 0, []
        for p in parts_by.get(m["id"], []):
            pd = _j(p["data"]) or {}
            pt = pd.get("type")
            if pt == "text" and pd.get("text") and not pd.get("synthetic") and not pd.get("ignored"):
                if role == "user":
                    texts.append(pd["text"])
                else:
                    items.append({"role": "assistant", "kind": "text", "text": pd["text"], "ts": ts, "model": mdl})
            elif pt == "reasoning" and pd.get("text") and role == "assistant":
                items.append({"role": "assistant", "kind": "thinking", "text": pd["text"], "ts": ts, "model": mdl})
            elif pt == "tool" and role == "assistant":
                st = pd.get("state") or {}
                items.append({"role": "assistant", "kind": "tool", "tool": pd.get("tool"), "input": st.get("input"),
                              "output": _tool_out(st), "status": st.get("status"),
                              "error": _clip(st.get("error")) if st.get("error") else None, "ts": ts, "model": mdl})
            elif pt == "file" and str(pd.get("mime") or pd.get("mediaType") or "").startswith("image/"):
                imgs += 1
        if role == "user" and (texts or imgs):
            items.append({"role": "user", "kind": "text", "text": "\n\n".join(texts), "ts": ts, "images": imgs or None})
    return items


def _read_db(agent: str) -> tuple[list[dict], dict]:
    """→ ([{meta…, items}], 统计)。"""
    f = db_path(agent)
    if not f.exists():
        return [], {"db": False}
    out, stat = [], {"db": True, "v1": 0, "v2": 0}
    with _Snap(f) as con:
        T = _tables(con)
        proj = {}
        if "project" in T:
            for r in con.execute("select * from project"):
                proj[r["id"]] = {"worktree": r["worktree"], "name": r["name"] if "name" in T["project"] else None}
        if "session_v2" in T and "session_message" in T:
            for s in con.execute("select * from session_v2 order by time_updated desc"):
                rows = con.execute("select type, time_created, data from session_message where session_id=? order by seq",
                                   (s["id"],)).fetchall()
                out.append({"id": s["id"], "source": "opencode2", "title": s["title"] or "", "cwd": s["directory"],
                            "created": s["time_created"], "updated": s["time_updated"], "parent": s["parent_id"],
                            "archived": bool(s["time_archived"]), "model": _model(_j(s["model"])), "project": s["project_id"],
                            "project_dir": (proj.get(s["project_id"]) or {}).get("worktree"), "items": _items_v2(rows)})
                stat["v2"] += 1
        if "session" in T and "message" in T and "part" in T:
            sc = T["session"]
            seq_m = "sequence" if "sequence" in T["message"] else "time_created"
            seq_p = "sequence" if "sequence" in T["part"] else "id"
            where = " where time_deleted is null" if "time_deleted" in sc else ""
            for s in con.execute(f"select * from session{where} order by time_updated desc"):
                msgs = con.execute(f"select id, time_created, data from message where session_id=? order by {seq_m}, id", (s["id"],)).fetchall()
                pb: dict = {}
                for p in con.execute(f"select message_id, data from part where session_id=? order by {seq_p}", (s["id"],)):
                    pb.setdefault(p["message_id"], []).append(p)
                sub = bool(s["parent_id"]) or ("task_type" in sc and (s["task_type"] or "") not in ("", "interactive"))
                out.append({"id": s["id"], "source": "zcode" if agent == "zcode" else "opencode1", "title": s["title"] or "",
                            "cwd": s["directory"], "created": s["time_created"], "updated": s["time_updated"],
                            "parent": s["parent_id"], "subagent": sub, "archived": bool(s["time_archived"]),
                            "model": _model(_j(s["model"])) if "model" in sc else None, "project": s["project_id"],
                            "project_dir": (proj.get(s["project_id"]) or {}).get("worktree"), "items": _items_v1(msgs, pb)})
                stat["v1"] += 1
    if agent == "zcode":
        tk = _zcode_tasks()
        stat["tasks"] = len(tk)
        stat["tasks_matched"] = 0
        keep = []
        for m in out:
            t = tk.get(m["id"])
            if t:
                stat["tasks_matched"] += 1
                if t.get("deleted"):
                    continue                       # 在 ZCode 里删了的不列
                if t.get("title") and (t.get("title_overridden") or not m["title"]):
                    m["title"] = t["title"]
                m["archived"] = m["archived"] or bool(t.get("archived"))
                m["cwd"] = m["cwd"] or t.get("workspace_path")
            keep.append(m)
        out = keep
    return out, stat


def _zcode_tasks() -> dict:
    """ZCode 桌面端的任务列表（v2/tasks-index.sqlite）：task_id → {title, archived, deleted}。只拿这几列。"""
    f = data_dir("zcode") / "v2" / "tasks-index.sqlite"
    if not f.exists():
        return {}
    out = {}
    try:
        with _Snap(f) as con:
            cols = _tables(con).get("tasks") or set()
            want = [c for c in ("task_id", "title", "title_overridden", "archived", "deleted", "workspace_path") if c in cols]
            if "task_id" not in want:
                return {}
            for r in con.execute(f"select {', '.join(want)} from tasks"):
                out[r["task_id"]] = {k: r[k] for k in want}
    except Exception:
        return {}
    return out


def legacy_count(agent: str) -> int:
    d = data_dir(agent) / "storage" / "session"
    try:
        return sum(1 for _ in d.rglob("*.json"))
    except OSError:
        return 0


# ---------- 镜像 ----------

_STATE: dict = {}


def refresh(agent: str, force: bool = False) -> list[dict]:
    sig = _sig(agent)
    st = _STATE.get(agent)
    if st and not force and st["sig"] == sig:
        return st["rows"]
    rows, err, stat = [], None, {}
    try:
        got, stat = _read_db(agent)
    except Exception as e:
        got, err = [], f"{type(e).__name__}: {e}"
    md = mirror_dir(agent)
    for m in got:
        items = m.pop("items")
        h = {k: m.get(k) for k in ("id", "title", "cwd", "created", "updated", "parent", "model", "project", "source")}
        h.update(agent=agent, source_name=SOURCE_NAME.get(m["source"]))
        data = ir.dumps(h, items)
        f = md / f"{m['id']}.jsonl"
        _write_if_changed(f, data)
        s = ir.stats_of(items)
        rows.append(dict(m, file=str(f), size=len(data), msgs=s["user"] + s["assistant"], tools=s["tools"], first=ir.title_of(items)))
    _STATE[agent] = {"sig": sig, "rows": rows, "error": err, "stat": stat, "at": time.time()}
    return rows


def _write_if_changed(f: Path, data: bytes) -> None:
    try:
        if f.read_bytes() == data:
            return
    except OSError:
        pass
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, f)


def find_file(agent: str, sid: str) -> Path | None:
    for r in refresh(agent):
        if r["id"] == sid:
            return Path(r["file"])
    f = mirror_dir(agent) / f"{sid}.jsonl"
    return f if f.exists() else None


def load(agent: str, sid: str) -> tuple[dict, list[dict]]:
    f = find_file(agent, sid)
    if not f:
        raise FileNotFoundError(f"找不到这个 {NAMES[agent]} 会话")
    return ir.load(f.read_bytes())


def read(agent: str, sid: str, limit: int = 400) -> dict:
    f = find_file(agent, sid)
    if not f:
        return {"ok": False, "error": f"找不到这个 {NAMES[agent]} 会话"}
    es = ir.entries_from_bytes(f.read_bytes())
    return {"ok": True, "file": str(f), "total": len(es), "entries": [dict(e, text=e["text"][:6000]) for e in es[-limit:]],
            "cut": max(0, len(es) - limit)}


def group_of(r: dict) -> str:
    return r.get("cwd") or r.get("project_dir") or r.get("project") or ""


def sessions(agent: str, names: dict | None = None) -> list[dict]:
    names = names or {}
    out = []
    for r in refresh(agent):
        al = names.get(f"{agent}:{r['id']}")
        tt = r.get("title") or None
        out.append({"agent": agent, "id": r["id"], "title": al or tt or r.get("first") or "（无标题）", "alias": al, "tool_title": tt,
                    "first": r.get("first") or "", "cwd": group_of(r), "last": (r.get("updated") or 0) / 1000, "size": r.get("size"),
                    "archived": r.get("archived", False), "registered": True, "visible": True, "file": r["file"],
                    "model": r.get("model"), "subagent": bool(r.get("subagent") or r.get("parent")), "msgs": r.get("msgs"),
                    "source": r["source"], "source_name": SOURCE_NAME.get(r["source"]), "project": r.get("project"),
                    "imported": False, "accounts": [],
                    "resume": (f"opencode --session {r['id']}" if agent == "opencode" else None)})
    return out


def detect(agent: str) -> dict:
    f = db_path(agent)
    rows = refresh(agent)
    st = _STATE.get(agent) or {}
    try:
        size = f.stat().st_size
    except OSError:
        size = None
    info = install_info(agent)
    by: dict = {}
    for r in rows:
        by[r["source"]] = by.get(r["source"], 0) + 1
    d = {"agent": agent, "name": NAMES[agent], "data_dir": str(data_dir(agent)), "db": str(f), "db_exists": size is not None,
         "db_bytes": size or 0, "install": info, "running": bool(procs(agent)), "sessions": len(rows), "by_source": by,
         "projects": len({group_of(r) for r in rows}), "error": st.get("error"), "mirror": str(mirror_dir(agent)),
         "legacy_json": legacy_count(agent), "stat": st.get("stat"), "found": size is not None or bool(info) or data_dir(agent).is_dir()}
    if agent == "opencode":
        d["cli"] = find_cli()
    return d


# ---------- 写：OpenCode（只用它自己的命令行，只建新会话） ----------

def _cli_env(cwd: str | None = None) -> dict:
    env = dict(os.environ)
    if cwd:
        env["PWD"] = cwd                                # opencode 认 $PWD 先于真正的工作目录：不对上的话 1.x 跑完不退出
    v = os.environ.get("CC_SESSIONSYNC_OPENCODE_DATA") or _set("opencode_dir")
    if v and Path(v).name == "opencode":               # 自己指定了数据目录：让命令行也用它
        env["XDG_DATA_HOME"] = str(Path(v).parent)
    env.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "1")
    return env


def cli_major(cli: str) -> int:
    import re
    m = re.search(r"(\d+)\.\d+", cli_version(cli) or "")
    return int(m.group(1)) if m else 2


def import_session(data: dict, cwd: str | None, cli: str | None = None, timeout: int = 120) -> dict:
    """写一个临时 JSON，交给 `opencode session import --standalone`（2.x）或 `opencode import`（1.x）。返回 {ok, id, out}。"""
    cli = cli or find_cli()
    if not cli:
        return {"ok": False, "error": "没找到 OpenCode 的命令行（opencode / opencode-cli）"}
    major = cli_major(cli)
    tmp = Path(tempfile.mkdtemp(prefix="ccss-ocimp-"))
    try:
        f = tmp / "session.json"
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        args = [cli, "session", "import", "--standalone"] + (["--directory", cwd] if cwd and Path(cwd).is_dir() else []) + [str(f)] \
            if major >= 2 else [cli, "import", str(f)]
        run_cwd = cwd if cwd and Path(cwd).is_dir() else str(tmp)
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=run_cwd, stdin=subprocess.DEVNULL, env=_cli_env(run_cwd),
                           encoding="utf-8", errors="replace")
        out = ((r.stdout or "") + (r.stderr or ""))[-400:]
        if r.returncode != 0:
            return {"ok": False, "error": f"opencode 退出码 {r.returncode}", "out": out, "major": major}
        return {"ok": True, "id": data["info"]["id"], "out": out, "major": major}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "opencode 命令行超时"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def has_session(sid: str) -> bool:
    refresh("opencode", force=True)
    return any(r["id"] == sid for r in _STATE.get("opencode", {}).get("rows", []))


def delete_session(sid: str, cli: str | None = None) -> dict:
    cli = cli or find_cli()
    if not cli:
        return {"ok": False, "error": "没找到 OpenCode 的命令行"}
    major = cli_major(cli)
    args = [cli, "session", "delete", "--standalone", sid] if major >= 2 else [cli, "session", "delete", sid]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, env=_cli_env(tempfile.gettempdir()),
                           cwd=tempfile.gettempdir(), encoding="utf-8", errors="replace")
        ok = r.returncode == 0 and not has_session(sid)
        return {"ok": ok, "error": None if ok else f"opencode 退出码 {r.returncode}", "out": ((r.stdout or "") + (r.stderr or ""))[-300:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "opencode 命令行超时"}

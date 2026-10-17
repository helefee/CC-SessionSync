# -*- coding: utf-8 -*-
"""把 Codex 的对话导进 Claude Code，在 Claude Code（命令行 claude --resume / 桌面端）里接着聊。

    cc-sessionsync codex list   [--days 30] [--cwd 项目目录] [--all]
    cc-sessionsync codex import [--days 30] [--cwd 项目目录] [--only 号,号] [--apply] [--no-desktop] [--to 账号/组织]
    cc-sessionsync codex ledgers
    cc-sessionsync codex undo <账本号>

不加 --apply 只演练（列出会写哪些文件），什么都不写。

读：<CODEX_HOME>/sessions/**/rollout-*.jsonl（和 archived_sessions/），session_index.jsonl 里的标题。只读，Codex 的文件一个字节都不改。
写（只写新文件；--apply 才写）：
- <CLAUDE_HOME>/projects/<项目目录名>/<新会话号>.jsonl：一份 Claude Code 会话记录（对话文字 + 工具过程的文字摘要）；
- 桌面端会话列表条目 <Claude 数据目录>/claude-code-sessions/<账号>/<组织>/local_<新号>.json（当前账号；--no-desktop 不写）；
- 账本 <数据目录>/codex-import/<时间>/ledger.json：这次新建 / 更新了哪些文件，更新前的版本也拷在这里。undo 按账本撤回。
同一个 Codex 对话再导一次：Codex 那边有新内容、而导过去那份你没在 Claude 里接着聊过 → 先备份再更新同一份；
你已经在 Claude 里接着聊过（文件被 Claude 改过）→ 不动它。

可选（缺省关）：--mark-codex-imported / 界面开关 / 设置 codex_mark_imported。打开后，把导过去的 Claude 会话登记进
Codex 自己的 <CODEX_HOME>/external_agent_session_imports.json（Codex「从 Claude Code 导入会话」用的导入记录），
记成「这份 Claude 会话 = 原来那个 Codex 线程」，Codex 桌面端就不会再把它当成新的 Claude 会话导回去成一份重复的。
这是本工具唯一会写的 Codex 文件：写之前先备份进账本，原子替换，只增 / 改我们自己那几条记录；undo 时还原。
文件格式不对（不是 {"records": [...]}）就整个不动、报错。

为什么是「文字摘要」而不是原样的工具调用：Codex 的工具（shell、apply_patch、update_plan…）和 Claude Code 的工具
（Bash、Edit…）参数格式不一样，硬塞成 Claude 的 tool_use 有可能被 API 拒掉，整份会话就续不上了。所以工具过程折成
助手回复里的一段文字（命令 + 截短的输出），思考过程（reasoning，大多加密）默认不带，图片换成「[图片]」占位。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import paths

LEDGERS = paths.HOME / "codex-import"
MAP = paths.HOME / "codex-import-map.json"          # Codex 线程号 → 导过去的 Claude 会话
PREFIX = "（Codex）"
DEFAULT_VERSION = "2.1.302"
KEEP_KEYS = ("model", "effort", "sessionSettings", "permissionMode", "remoteMcpServersConfig",
             "chromePermissionMode", "cliBinaryPin")
# Codex 往用户消息里塞的上下文（不是用户自己说的话）
SKIP_USER = ("# AGENTS.md instructions", "<environment_context", "<app-context", "<user_instructions", "<INSTRUCTIONS",
             "<permissions", "<skills_instructions", "<turn_aborted", "<user_shell_command", "<subagent_notification")
DESKTOP_ORIGINATORS = ("Codex Desktop", "codex_work_desktop")
IMPORTS_NAME = "external_agent_session_imports.json"   # Codex 的「外部会话导入记录」（codex-rs/external-agent-migration/src/sessions/ledger.rs）


def codex_home() -> Path:
    return paths.CODEX_HOME


def claude_home() -> Path:
    return paths.CLAUDE_HOME


# ---------- 小工具 ----------

def _sha(f: Path) -> str:
    return hashlib.sha256(f.read_bytes()).hexdigest()


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _js_hash(s: str) -> int:
    """Claude Code 的字符串哈希（按 UTF-16 码元的 Java 式 31 进制，32 位有符号）。"""
    h = 0
    b = s.encode("utf-16-le")
    for i in range(0, len(b), 2):
        h = (h * 31 + (b[i] | b[i + 1] << 8)) & 0xFFFFFFFF
    return h - (1 << 32) if h & 0x80000000 else h


def _b36(n: int) -> str:
    n = abs(n)
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + s
        if not n:
            return s


def project_slug(cwd: str) -> str:
    """和 Claude Code 一样把工作目录变成 ~/.claude/projects 下的文件夹名：非字母数字换成 -，太长截到 200 再加哈希。"""
    s = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return s if len(s) <= 200 else f"{s[:200]}-{_b36(_js_hash(cwd))}"


def norm_cwd(cwd: str | None) -> str:
    c = str(cwd or "").strip()
    if c.startswith("\\\\?\\"):
        c = c[4:]
    if len(c) > 3 and c.endswith(("\\", "/")):
        c = c[:-1]
    return c


def iso(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ts(ts) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ---------- 找 Codex 对话 ----------

def rollouts():
    for base in (codex_home() / "sessions", codex_home() / "archived_sessions"):
        if base.is_dir():
            yield from base.rglob("rollout-*.jsonl")


def meta_of(p: Path) -> dict:
    try:
        with open(p, "rb") as f:
            for _ in range(5):
                line = f.readline()
                if not line:
                    break
                d = json.loads(line)
                if d.get("type") == "session_meta":
                    pl = d.get("payload") or {}
                    return pl.get("meta", pl) if isinstance(pl.get("meta"), dict) and "id" not in pl else pl
    except Exception:
        pass
    return {}


def titles() -> dict:
    out = {}
    try:
        for line in (codex_home() / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
                if d.get("thread_name"):
                    out[d["id"]] = d["thread_name"]
            except Exception:
                pass
    except OSError:
        pass
    return out


def _norm_src(p: str) -> str:
    p = str(p or "")
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\"):
        p = p[4:]
    return os.path.normcase(os.path.normpath(p)) if p else ""


def imported_from_claude() -> set[str]:
    """Codex 从 Claude Code 导进来的线程：不再导回 Claude，免得来回套娃。
    例外：我们自己登记的「导过去的那份 = 原来的 Codex 线程」（--mark-codex-imported）那条指回原线程，不算。"""
    d = _load(codex_home() / IMPORTS_NAME, {})
    ours = {_norm_src(v.get("file", "")): tid for tid, v in _load(MAP, {}).items()}
    out = set()
    for r in (d.get("records") or []) if isinstance(d, dict) else []:
        if not isinstance(r, dict) or not r.get("imported_thread_id"):
            continue
        if ours.get(_norm_src(r.get("source_path", ""))) == r["imported_thread_id"]:
            continue
        out.add(r["imported_thread_id"])
    return out


def _user_thread(m: dict) -> bool:
    src = m.get("thread_source")
    if src is not None and src != "user":           # 子代理、后台 review 等
        return False
    s = m.get("source")
    if isinstance(s, dict) and ("subagent" in s or "sub_agent" in s):
        return False
    return m.get("originator") != "codex_exec"        # codex exec 一次性跑的


def _first_prompt(p: Path) -> str:
    for it in parse(p)["items"]:
        if it["role"] == "user":
            return it["text"].splitlines()[0][:60]
    return ""


def threads(days: float = 30, cwd: str | None = None, include_all: bool = False) -> list[dict]:
    """可以导的 Codex 对话，新的在前。days<=0 不限时间。"""
    t = titles()
    skip = set() if include_all else imported_from_claude()
    since = time.time() - days * 86400 if days and days > 0 else 0
    want = norm_cwd(cwd).lower().replace("\\", "/") if cwd else None
    best: dict[str, tuple[Path, dict, float]] = {}
    for p in rollouts():
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue
        if mt < since:
            continue
        m = meta_of(p)
        tid = m.get("id") or p.stem[-36:]
        if not include_all and (not _user_thread(m) or tid in skip):
            continue
        mc = norm_cwd(m.get("cwd")).lower().replace("\\", "/")
        if want and not (mc == want or mc.startswith(want.rstrip("/") + "/")):
            continue
        if tid not in best or mt > best[tid][2]:
            best[tid] = (p, m, mt)
    out = []
    for tid, (p, m, mt) in sorted(best.items(), key=lambda kv: -kv[1][2]):
        out.append({"thread": tid, "path": str(p), "title": t.get(tid) or "", "mtime": mt,
                    "cwd": norm_cwd(m.get("cwd")), "originator": m.get("originator"),
                    "archived": "archived_sessions" in p.parts})
    return out


# ---------- 解析 ----------

def _out_text(o) -> str:
    """function_call_output 的 output：字符串、{content}、或内容块数组；shell 的还常是 JSON 字符串 {output, metadata}。"""
    if isinstance(o, dict):
        o = o.get("content") if "content" in o else o.get("body", o)
    if isinstance(o, list):
        parts = []
        for x in o:
            if isinstance(x, dict):
                if x.get("type") in ("input_text", "output_text", "text"):
                    parts.append(x.get("text", ""))
                elif x.get("type") in ("input_image", "image"):
                    parts.append("[图片]")
            else:
                parts.append(str(x))
        return "\n".join(parts)
    s = "" if o is None else str(o)
    if s.startswith("{") and '"output"' in s:
        try:
            d = json.loads(s)
            code = (d.get("metadata") or {}).get("exit_code")
            s = str(d.get("output", "")) + (f"\n（退出码 {code}）" if code not in (None, 0) else "")
        except Exception:
            pass
    return s


def _call_desc(pl: dict) -> tuple[str, str]:
    """→ (工具名, 参数摘要)"""
    pt = pl.get("type")
    name = pl.get("name") or pt
    if pl.get("namespace"):
        name = f"{pl['namespace']}.{name}"
    if pt == "local_shell_call":
        a = (pl.get("action") or {})
        cmd = a.get("command")
        return "shell", " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd or a)
    if pt == "web_search_call":
        a = pl.get("action") or {}
        return "web_search", str(a.get("query") or a.get("url") or a.get("queries") or "")
    raw = pl.get("arguments") if pt == "function_call" else pl.get("input")
    if raw is None:
        raw = pl.get("arguments") or pl.get("input") or ""
    try:
        a = json.loads(raw) if isinstance(raw, str) and raw[:1] in "{[" else raw
    except Exception:
        a = raw
    if isinstance(a, dict):
        if name == "update_plan" and isinstance(a.get("plan"), list):
            return name, "; ".join(f"[{x.get('status', '')}] {x.get('step', '')}" for x in a["plan"] if isinstance(x, dict))
        cmd = a.get("cmd") or a.get("command") or a.get("input") or a.get("patch")
        if cmd is None:
            cmd = json.dumps(a, ensure_ascii=False)
        if isinstance(cmd, list):
            if len(cmd) >= 3 and str(cmd[0]).split("/")[-1] in ("bash", "sh", "zsh", "powershell", "pwsh", "powershell.exe", "pwsh.exe") \
                    and str(cmd[1]) in ("-lc", "-c", "-Command", "-NoProfile"):
                cmd = cmd[-1]
            else:
                cmd = " ".join(map(str, cmd))
        return name, str(cmd)
    return name, str(a)


def parse(p: Path, reasoning: bool = False) -> dict:
    """→ {meta, items:[{role: user|assistant|tool|reasoning, text, ts, ...}], stats}"""
    items, calls, meta = [], {}, {}
    st = {"images": 0, "tools": 0, "reasoning": 0, "reasoning_encrypted": 0, "skipped_context": 0, "bad_lines": 0,
          "compactions": 0}
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                st["bad_lines"] += 1 if line.strip() else 0
                continue
            ts, typ = d.get("timestamp"), d.get("type")
            pl = d.get("payload") or {}
            if typ == "session_meta":
                meta = meta or (pl.get("meta", pl) if isinstance(pl.get("meta"), dict) and "id" not in pl else pl)
                continue
            if typ == "compacted":
                st["compactions"] += 1
                continue
            if typ != "response_item" or not isinstance(pl, dict):
                continue
            pt = pl.get("type")
            if pt == "message" and pl.get("role") in ("user", "assistant"):
                parts = []
                for x in pl.get("content") or []:
                    if not isinstance(x, dict):
                        continue
                    if x.get("type") in ("input_text", "output_text", "text"):
                        parts.append(x.get("text", ""))
                    elif x.get("type") in ("input_image", "image"):
                        parts.append("[图片：Codex 里贴的图，没有导过来]")
                        st["images"] += 1
                    elif x.get("type") == "refusal":
                        parts.append(x.get("refusal", ""))
                text = "\n".join(t for t in parts if t).strip()
                if pl["role"] == "user" and text.startswith(SKIP_USER):
                    st["skipped_context"] += 1
                    continue
                if text:
                    items.append({"role": pl["role"], "text": text, "ts": ts})
            elif pt == "reasoning":
                summ = "\n".join(x.get("text", "") for x in pl.get("summary") or [] if isinstance(x, dict)).strip()
                if summ or pl.get("content"):
                    st["reasoning"] += 1
                    if reasoning and summ:
                        items.append({"role": "reasoning", "text": summ, "ts": ts})
                elif pl.get("encrypted_content"):
                    st["reasoning_encrypted"] += 1
            elif pt in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call", "tool_search_call",
                        "image_generation_call"):
                name, args = _call_desc(pl)
                it = {"role": "tool", "name": name, "args": args, "output": None, "ts": ts}
                if pt == "image_generation_call":
                    it.update(name="image_generation", args=str(pl.get("revised_prompt") or ""), output="[生成的图片没有导过来]")
                    st["images"] += 1
                items.append(it)
                st["tools"] += 1
                if pl.get("call_id"):
                    calls[pl["call_id"]] = it
            elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output", "tool_search_output"):
                it = calls.get(pl.get("call_id"))
                if it is not None:
                    it["output"] = _out_text(pl.get("output") if "output" in pl else pl.get("tools"))
    return {"meta": meta, "items": items, "stats": st}


# ---------- 拼成 Claude Code 会话 ----------

def _clip(s: str, lines: int, chars: int) -> str:
    s = (s or "").rstrip()
    ls = s.splitlines()
    cut = len(ls) > lines or len(s) > chars
    s = "\n".join(ls[:lines])[:chars]
    return s + ("\n…（截断）" if cut else "")


def _fence(s: str) -> str:
    f = "```"
    while f in s:
        f += "`"
    return f"{f}\n{s}\n{f}"


def claude_version() -> str:
    """本机最近一份 Claude Code 会话记录里的版本号（新记录写同一个版本，免得太老被认不出）。"""
    try:
        fs = sorted((claude_home() / "projects").glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
    except OSError:
        fs = []
    for f in fs:
        try:
            with open(f, "rb") as fh:
                for i, line in enumerate(fh):
                    if i > 50:
                        break
                    v = json.loads(line).get("version")
                    if v:
                        return v
        except Exception:
            continue
    return DEFAULT_VERSION


def build(p: Path, sid: str, version: str = DEFAULT_VERSION, title: str | None = None, out_lines: int = 20,
          out_chars: int = 2000, reasoning: bool = False) -> dict:
    """把一个 Codex 线程拼成 Claude Code 会话记录（只算，不写）。→ {records, cwd, title, turns, stats, thread}"""
    conv = parse(p, reasoning)
    m = conv["meta"]
    tid = m.get("id") or p.stem[-36:]
    cwd = norm_cwd(m.get("cwd")) or str(Path.home())
    git = m.get("git") or {}
    branch = git.get("branch") or "HEAD"
    title = title or titles().get(tid) or _first_prompt(p) or "Codex 对话"
    turns: list[list] = []                  # [role, [片段], ts]，严格 user/assistant 交替

    def push(role, text, ts):
        if turns and turns[-1][0] == role:
            turns[-1][1].append(text)
        else:
            turns.append([role, [text], ts])

    first_ts = conv["items"][0]["ts"] if conv["items"] else m.get("timestamp")
    push("user", f"（以下是从 Codex 导入的对话「{title}」，Codex 线程 {tid}，工作目录 {cwd}。"
                 "Codex 里跑过的命令和改动只留了文字摘要，输出是截短的；接着做之前先看一下文件 / git 的当前状态。）", first_ts)
    for it in conv["items"]:
        if it["role"] == "tool":
            s = f"〔Codex 调用了 {it['name']}〕\n" + _fence(_clip(it["args"], 30, 3000))
            if it.get("output"):
                s += "\n输出：\n" + _fence(_clip(it["output"], out_lines, out_chars))
            push("assistant", s, it["ts"])
        elif it["role"] == "reasoning":
            push("assistant", "〔Codex 的思考摘要〕\n" + it["text"], it["ts"])
        else:
            push(it["role"], it["text"], it["ts"])
    if turns[-1][0] == "user":              # 最后一条得是助手，resume 后你的新问题才接得上
        push("assistant", "（Codex 那边到这里还没有回复。导入到此为止。）", turns[-1][2])
    recs, parent, last_ts = [], None, None
    for role, parts, ts in turns:
        u = str(uuid.uuid4())
        t = iso(ts or last_ts)
        last_ts = ts or last_ts
        text = "\n\n".join(parts)
        r = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": sid,
             "version": version, "gitBranch": branch, "type": role, "uuid": u, "timestamp": t}
        if role == "user":
            r["message"] = {"role": "user", "content": text}
        else:
            r["message"] = {"id": "msg_codeximport_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": "codex-import", "content": [{"type": "text", "text": text}],
                            "stop_reason": "end_turn", "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        parent = u
    recs.append({"type": "custom-title", "customTitle": PREFIX + title, "sessionId": sid})
    return {"records": recs, "cwd": cwd, "title": title, "turns": len(turns), "stats": conv["stats"], "thread": tid,
            "chars": sum(len(json.dumps(r, ensure_ascii=False)) for r in recs)}


def _dump(recs: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs)


# ---------- 桌面端列表条目 ----------

def _desktop_target(target: str | None) -> dict | None:
    from . import claude_sync
    fs = claude_sync.folders()
    if not fs:
        return None
    if target:
        return next((f for f in fs if f["key"] == target), None)
    key = claude_sync.current_key("desktop")
    return next((f for f in fs if f["key"] == key), None) or fs[0]


def _desktop_entry(tdir: Path, cli_sid: str, cwd: str, title: str, mtime: float, cur: dict | None) -> dict:
    tmpl = {}
    try:
        fs = sorted(tdir.glob("local_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        tmpl = _load(fs[0], {}) if fs else {}
    except OSError:
        pass
    cur = cur or {}
    d = {k: tmpl[k] for k in KEEP_KEYS if k in tmpl}
    d.update(cur)
    name = cur.get("sessionId") or f"local_{uuid.uuid4()}"
    ms = int(mtime * 1000)
    d.update({"sessionId": name, "cliSessionId": cli_sid, "cwd": cwd, "originCwd": cwd,
              "lastActivityAt": max(ms, cur.get("lastActivityAt", 0)), "lastFocusedAt": max(ms, cur.get("lastFocusedAt", 0))})
    d.setdefault("createdAt", ms)
    d.setdefault("isArchived", False)
    if cur.get("titleSource") != "user" or not cur.get("title"):
        d.update({"title": PREFIX + title, "titleSource": "user"})
    return d


# ---------- 登记进 Codex 的导入记录（可选） ----------
# 格式（codex-rs/external-agent-migration/src/sessions/ledger.rs，serde）：
#   {"records": [{"source_path": 规范化的绝对路径（Windows 是 \\?\C:\...）, "content_sha256": 文件内容 sha256（小写十六进制）,
#                 "imported_thread_id": Codex 线程号, "imported_at": 秒, "source_modified_at": 文件修改时间（纳秒）,
#                 "connector_names": [], "title": 标题}],
#    "detected_connector_records": [...]}
# Codex 扫 ~/.claude/projects 时：路径 + 修改时间对得上 → 跳过；路径对得上、内容 sha 不同 → 试着把新增的部分接到那个线程后面
# （对不上就放弃，不会新建重复的）；没有记录 → 当新会话导入。所以登记了就不会出重复。

def mark_setting() -> bool:
    try:
        return bool(json.loads((paths.HOME / "settings.json").read_text(encoding="utf-8")).get("codex_mark_imported"))
    except Exception:
        return False


def set_mark_setting(on: bool) -> None:
    f = paths.HOME / "settings.json"
    d = _load(f, {})
    d = d if isinstance(d, dict) else {}
    d["codex_mark_imported"] = bool(on)
    _save(f, d)


def codex_src_path(f: Path) -> str:
    """和 Codex（Rust fs::canonicalize）一样的路径写法。"""
    p = os.path.realpath(f)
    if os.name == "nt":
        if p.startswith("\\\\?\\"):
            return p
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def imports_check() -> dict:
    """Codex 导入记录文件现在能不能安全地改。→ {ok, exists, error?}"""
    f = codex_home() / IMPORTS_NAME
    if not f.exists():
        return {"ok": True, "exists": False, "file": str(f)}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "exists": True, "file": str(f), "error": f"Codex 的导入记录 {f} 不是合法的 JSON（{e}），没有动它"}
    if not isinstance(d, dict) or not isinstance(d.get("records"), list) or \
            not all(isinstance(r, dict) for r in d["records"]) or \
            not isinstance(d.get("detected_connector_records", []), list):
        return {"ok": False, "exists": True, "file": str(f), "error": f"Codex 的导入记录 {f} 格式不认识（应该是 {{\"records\": [...]}}），没有动它"}
    return {"ok": True, "exists": True, "file": str(f)}


def _write_imports(d: dict) -> None:
    f = codex_home() / IMPORTS_NAME
    tmp = f.with_name(f".{f.name}.cc-sessionsync-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)


def _mark_imports(entries: list[dict], led: dict, led_dir: Path) -> dict:
    """把这次导入的会话登记进 Codex 的导入记录。entries: [{file, thread, title}]。先备份原文件（进账本），再原子替换。"""
    f = codex_home() / IMPORTS_NAME
    chk = imports_check()
    if not chk["ok"]:
        return {"ok": False, "error": chk["error"]}
    if chk["exists"]:
        d = json.loads(f.read_text(encoding="utf-8"))
        shutil.copy2(f, led_dir / "before" / IMPORTS_NAME)
        before_sha = _sha(f)
    else:
        d = {"records": [], "detected_connector_records": []}
        before_sha = None
    prev = {}
    for e in entries:
        jf = Path(e["file"])
        src = codex_src_path(jf)
        old = [r for r in d["records"] if r.get("source_path") == src]
        prev[src] = old[-1] if old else None
        d["records"] = [r for r in d["records"] if r.get("source_path") != src]
        st = jf.stat()
        d["records"].append({"source_path": src, "content_sha256": _sha(jf), "imported_thread_id": e["thread"],
                             "imported_at": int(time.time()), "source_modified_at": st.st_mtime_ns,
                             "connector_names": [], "title": e.get("title")})
    d.setdefault("detected_connector_records", [])
    led["codex_imports"] = {"file": str(f), "existed": chk["exists"], "before": IMPORTS_NAME if chk["exists"] else None,
                            "before_sha": before_sha, "after_sha": None, "entries": prev}
    _save(led_dir / "ledger.json", led)        # 先记账再写
    _write_imports(d)
    led["codex_imports"]["after_sha"] = _sha(f)
    _save(led_dir / "ledger.json", led)
    return {"ok": True, "marked": len(entries), "file": str(f)}


def _unmark_imports(ci: dict, led_dir: Path, keep_files: list[str], trash: Path) -> dict:
    """撤回登记。文件从那以后没被别人改过、也没有要保留的 → 原样换回备份（原来没有就挪进回收站）；
    否则只把我们那几条记录换回原来的样子（Codex 之后自己加的记录不动）。"""
    f = Path(ci["file"])
    if not f.exists():
        return {"ok": True, "note": "Codex 的导入记录文件已经不在了，没什么要撤的"}
    keep = {codex_src_path(Path(k)) for k in keep_files}
    if ci.get("after_sha") and _sha(f) == ci["after_sha"] and not keep:
        trash.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, trash / f"replaced-{f.name}")
        if ci.get("existed"):
            tmp = f.with_name(f".{f.name}.cc-sessionsync-{os.getpid()}.tmp")
            shutil.copy2(led_dir / "before" / ci["before"], tmp)
            os.replace(tmp, f)
            return {"ok": True, "how": "restored"}
        f.unlink()
        return {"ok": True, "how": "removed"}
    chk = imports_check()
    if not chk["ok"]:
        return {"ok": False, "error": chk["error"]}
    d = json.loads(f.read_text(encoding="utf-8"))
    trash.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f, trash / f"replaced-{f.name}")
    for src, old in (ci.get("entries") or {}).items():
        if src in keep:
            continue                               # 那份会话你在 Claude 里接着聊过、留着了：登记也留着，免得 Codex 把它导成重复的
        d["records"] = [r for r in d["records"] if r.get("source_path") != src]
        if old:
            d["records"].append(old)
    _write_imports(d)
    return {"ok": True, "how": "entries"}


# ---------- 计划 / 执行 / 撤回 ----------

def plan(days: float = 30, cwd: str | None = None, only: list[str] | None = None, include_all: bool = False,
         desktop: bool = True, target: str | None = None, mark: bool | None = None) -> dict:
    mp = _load(MAP, {})
    rows = []
    keys = [k.strip().lower() for k in (only or []) if k.strip()]
    for c in threads(days, cwd, include_all):
        if keys and not any(c["thread"].lower() == k or c["thread"].lower().endswith(k) for k in keys):
            continue
        rec = mp.get(c["thread"])
        state = "new"
        if rec:
            jf = Path(rec.get("file", ""))
            if not jf.exists():
                state = "new"                       # 导过去那份被删了（或撤回了）：重新导一份新的
            elif rec.get("sha") and _sha(jf) != rec["sha"]:
                state = "diverged"                  # 你已经在 Claude 里接着聊过：不动
            elif rec.get("src_mtime", 0) >= c["mtime"]:
                state = "same"
            else:
                state = "update"
        sid = rec.get("session") if rec and state in ("update", "same", "diverged") else None
        rows.append(dict(c, state=state, session=sid,
                         file=str(claude_home() / "projects" / project_slug(c["cwd"] or str(Path.home())) / f"{sid}.jsonl") if sid else None))
    dt = None
    if desktop:
        try:
            dt = _desktop_target(target)
        except Exception:
            dt = None
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("new", "update", "same", "diverged")}
    mark = mark_setting() if mark is None else bool(mark)
    mk = imports_check() if mark else None
    return {"ok": True, "rows": rows, "counts": counts, "desktop": dt, "mark": mark, "mark_check": mk,
            "desktop_note": None if not desktop else (None if dt else "没找到 Claude 桌面端的会话列表目录，只写命令行能用的会话记录"),
            "codex_home": str(codex_home()), "claude_home": str(claude_home())}


def run(days: float = 30, cwd: str | None = None, only: list[str] | None = None, include_all: bool = False,
        desktop: bool = True, target: str | None = None, dry: bool = True, reasoning: bool = False,
        mark: bool | None = None) -> dict:
    """mark：登记进 Codex 的导入记录（None = 按设置，缺省关）。"""
    p = plan(days, cwd, only, include_all, desktop, target, mark)
    todo = [r for r in p["rows"] if r["state"] in ("new", "update")]
    ver = claude_version()
    mp = _load(MAP, {})
    tdir = Path(p["desktop"]["path"]) if p.get("desktop") else None
    if dry or not todo:
        for r in todo:                              # 演练：也真拼一遍，报出轮数 / 大小 / 会丢掉什么
            try:
                b = build(Path(r["path"]), r.get("session") or "00000000-0000-0000-0000-000000000000", ver,
                          r["title"] or None, reasoning=reasoning)
                r.update(turns=b["turns"], chars=b["chars"], stats=b["stats"], title=b["title"],
                         target=str(claude_home() / "projects" / project_slug(b["cwd"])))
            except Exception as e:
                r["error"] = repr(e)
        return dict(p, dry=True, done=0)
    if p["mark"] and not p["mark_check"]["ok"]:     # 要登记、但 Codex 的记录文件格式不对：什么都不写
        return dict(p, ok=False, dry=False, done=0, error=p["mark_check"]["error"] + "。修好它，或者关掉「登记进 Codex 导入记录」再导。")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    led_dir = LEDGERS / stamp
    while led_dir.exists():
        led_dir = led_dir.with_name(led_dir.name + "x")
    (led_dir / "before").mkdir(parents=True)
    led = {"what": "codex-import", "time": datetime.now().isoformat(timespec="seconds"), "created": [], "updated": [],
           "threads": [], "map_before": {}, "errors": [], "codex_home": str(codex_home()), "claude_home": str(claude_home()),
           "desktop": p["desktop"]["key"] if p.get("desktop") else None}

    def save_led():
        _save(led_dir / "ledger.json", led)

    def keep_before(f: Path):
        n = f"{len(led['updated'])}-{f.name}"
        shutil.copy2(f, led_dir / "before" / n)
        led["updated"].append({"file": str(f), "before": n})

    def write_new_or_update(f: Path, text: str):
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists():
            keep_before(f)
        else:
            led["created"].append(str(f))
        save_led()                                  # 先记账再写：中途断了也撤得回
        tmp = f.with_name(f.name + ".cc-sessionsync.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        tmp.replace(f)

    done = 0
    for r in todo:
        rec = dict(mp.get(r["thread"]) or {})
        led["map_before"][r["thread"]] = mp.get(r["thread"])
        try:
            sid = rec.get("session") if r["state"] == "update" else str(uuid.uuid4())
            b = build(Path(r["path"]), sid, ver, r["title"] or None, reasoning=reasoning)
            jf = claude_home() / "projects" / project_slug(b["cwd"]) / f"{sid}.jsonl"
            write_new_or_update(jf, _dump(b["records"]))
            ent = None
            if tdir is not None:
                cur = None
                old = rec.get("local") if r["state"] == "update" and rec.get("folder") == p["desktop"]["key"] else None
                if old and (tdir / f"{old}.json").exists():
                    cur = _load(tdir / f"{old}.json", {})
                d = _desktop_entry(tdir, sid, b["cwd"], b["title"], r["mtime"], cur)
                ent = d["sessionId"]
                write_new_or_update(tdir / f"{ent}.json", json.dumps(d, ensure_ascii=False, indent=1))
            mp[r["thread"]] = {"session": sid, "file": str(jf), "sha": _sha(jf), "src_mtime": r["mtime"],
                               "local": ent or rec.get("local"), "folder": p["desktop"]["key"] if ent else rec.get("folder"),
                               "title": b["title"], "cwd": b["cwd"], "imported": time.time(), "ledger": led_dir.name}
            led["threads"].append({"thread": r["thread"], "session": sid, "title": b["title"], "state": r["state"],
                                   "file": str(jf), "turns": b["turns"], "stats": b["stats"]})
            r.update(session=sid, file=str(jf), turns=b["turns"], stats=b["stats"], resume=resume_cmd(b["cwd"], sid))
            done += 1
        except Exception as e:
            led["errors"].append(f"{r['thread']}：{e!r}")
            r["error"] = repr(e)
        save_led()
    _save(MAP, mp)
    save_led()
    mres = None
    if p["mark"] and led["threads"]:
        try:
            mres = _mark_imports([{"file": t["file"], "thread": t["thread"], "title": PREFIX + t["title"]} for t in led["threads"]], led, led_dir)
        except Exception as e:
            mres = {"ok": False, "error": f"登记进 Codex 导入记录失败：{e!r}"}
        if not mres["ok"]:
            led["errors"].append(mres["error"])
            save_led()
    return dict(p, dry=False, done=done, errors=led["errors"], ledger=led_dir.name, ledger_path=str(led_dir), marked=mres)


def resume_cmd(cwd: str, sid: str) -> str:
    q = f'"{cwd}"' if any(c in cwd for c in ' &()') else cwd
    return f"cd {q} && claude --resume {sid}"


def ledgers() -> list[dict]:
    out = []
    if not LEDGERS.is_dir():
        return out
    for d in sorted(LEDGERS.iterdir(), reverse=True):
        led = _load(d / "ledger.json", None)
        if isinstance(led, dict):
            out.append({"id": d.name, "time": led.get("time"), "what": led.get("what"), "threads": len(led.get("threads", [])),
                        "created": len(led.get("created", [])), "updated": len(led.get("updated", [])),
                        "undone": led.get("undone")})
    return out


def undo(ledger_id: str) -> dict:
    """撤回一次导入：新建的文件挪进回收站（不直接删），更新过的换回原来的版本，映射表恢复。
    导过去之后你在 Claude 里接着聊过的会话（文件变了）不动，列出来让你自己决定。"""
    if not ledger_id or any(c in ledger_id for c in "/\\") or ".." in ledger_id:
        return {"ok": False, "error": f"账本号不对：{ledger_id}"}
    d = LEDGERS / ledger_id
    led = _load(d / "ledger.json", None)
    if not isinstance(led, dict):
        return {"ok": False, "error": f"没有这个账本：{ledger_id}"}
    if led.get("undone"):
        return {"ok": False, "error": f"这次导入已经撤回过了（{led['undone']}）"}
    mp = _load(MAP, {})
    final = {v.get("file"): v.get("sha") for v in mp.values() if v.get("ledger") == ledger_id}
    trash = paths.HOME / "trash" / f"codex-import-undo-{datetime.now():%Y%m%d-%H%M%S}"
    moved, restored, kept = 0, 0, []
    for f in led.get("created", []):
        fp = Path(f)
        if not fp.exists():
            continue
        if fp.suffix == ".jsonl" and final.get(f) and _sha(fp) != final[f]:
            kept.append(f)                          # 导进来之后在 Claude 里接着聊过：不动
            continue
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / f"{moved}-{fp.name}"
        shutil.move(str(fp), dst)
        moved += 1
    for u in led.get("updated", []):
        fp, src = Path(u["file"]), d / "before" / u["before"]
        if fp.suffix == ".jsonl" and fp.exists() and final.get(u["file"]) and _sha(fp) != final[u["file"]]:
            kept.append(u["file"])
            continue
        if src.exists():
            if fp.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, trash / f"replaced-{restored}-{fp.name}")
            shutil.copy2(src, fp)
            restored += 1
    mres = None
    if led.get("codex_imports"):
        try:
            mres = _unmark_imports(led["codex_imports"], d, kept, trash)
        except Exception as e:
            mres = {"ok": False, "error": f"撤回 Codex 导入记录失败：{e!r}"}
    for tid, before in (led.get("map_before") or {}).items():
        if mp.get(tid, {}).get("ledger") != ledger_id:
            continue
        if before:
            mp[tid] = before
        else:
            mp.pop(tid, None)
    _save(MAP, mp)
    led["undone"] = datetime.now().isoformat(timespec="seconds")
    led["undo"] = {"moved": moved, "restored": restored, "kept": kept, "trash": str(trash) if trash.exists() else None,
                   "codex_imports": mres}
    _save(d / "ledger.json", led)
    return {"ok": True, "moved": moved, "restored": restored, "kept": kept, "trash": str(trash) if trash.exists() else None,
            "codex_imports": mres}

# -*- coding: utf-8 -*-
"""工具调用的对照表（0.4.1）：各家叫法不同、参数名不同，但常用的几种是一回事。

先把来源的工具调用认成一种「通用工具」（shell / read / write / edit / grep / glob / fetch / search）和整理过的参数，
再按目标应用自己的工具名和参数表拼回去。认不出来的、参数不全的（比如没有原文 / 新文的「编辑」）一律返回 None，
写入器就照旧把它写成文字摘要——宁可当文字，也不写一个目标应用会读错的工具调用。

各家的工具表（照各自命令行实际发给模型的工具表）：
  Claude Code：Bash{command, description} Read{file_path, offset, limit} Write{file_path, content}
               Edit{file_path, old_string, new_string, replace_all} Grep{pattern, path, glob} Glob{pattern, path}
               WebFetch{url, prompt} WebSearch{query}
  dsh：        bash{command, description, workdir} read{file_path, offset, limit} write{file_path, content}
               edit{file_path, old_string, new_string, replace_all} grep{pattern, path, include} glob{pattern, path}
               web_fetch{url} web_search{queries[]}
  Codex：      exec_command{cmd, workdir}（老版本是 shell{command[]}）——别的工具 Codex 没有对应的
  OpenCode 2.x：shell{command, workdir} read{path, offset, limit} write{path, content} edit{path, oldString, newString, replaceAll}
               grep{pattern, path, include} glob{pattern, path} webfetch{url, format} websearch{query}
  OpenCode 1.x：bash{command, workdir} read{filePath, offset, limit} write{filePath, content}
               edit{filePath, oldString, newString, replaceAll} grep{pattern, path, include} glob{pattern, path} webfetch{url, format}
"""
from __future__ import annotations

import re
import json

KINDS = ("shell", "read", "write", "edit", "grep", "glob", "fetch", "search")

_NAMES = {
    "shell": {"bash", "shell", "shell_command", "exec_command", "local_shell", "local_shell_call", "run_terminal_cmd", "run_terminal_command",
              "run_command", "execute_command", "terminal", "command"},
    "read": {"read", "read_file", "readfile", "view", "view_file", "open_file"},
    "write": {"write", "write_file", "writefile", "create_file"},
    "edit": {"edit", "str_replace", "str_replace_edit", "search_replace", "replace", "replace_in_file", "edit_file"},
    "grep": {"grep", "grep_search", "rg", "ripgrep", "search_file_content", "search_files"},
    "glob": {"glob", "glob_file_search", "find_files"},
    "fetch": {"webfetch", "web_fetch", "fetch", "fetch_url", "read_url"},
    "search": {"websearch", "web_search", "search_web", "web_search_call"},
}
_KIND_OF = {n: k for k, ns in _NAMES.items() for n in ns}


def _obj(a):
    if isinstance(a, str) and a[:1] in "{[":
        try:
            return json.loads(a)
        except Exception:
            return a
    return a


def _first(a: dict, *keys):
    for k in keys:
        v = a.get(k)
        if v not in (None, "", []):
            return v
    return None


def _cmd(v) -> str | None:
    if isinstance(v, list):
        v = [str(x) for x in v]
        if len(v) >= 3 and v[0].split("/")[-1].lower() in ("bash", "sh", "zsh", "powershell", "pwsh", "powershell.exe", "pwsh.exe", "cmd", "cmd.exe") \
                and v[1] in ("-lc", "-c", "-Command", "/c", "-NoProfile"):
            return v[-1]
        return " ".join(v)
    return v if isinstance(v, str) and v.strip() else None


def _int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def canon(name, inp) -> tuple[str, dict] | None:
    """来源的 (工具名, 参数) → (通用工具, 参数)。参数：shell{command, description?, workdir?} read{path, offset?, limit?}
    write{path, content} edit{path, old, new, all?} grep{pattern, path?, include?} glob{pattern, path?} fetch{url} search{query}。"""
    n = str(name or "").strip().lower().split(".")[-1].split("__")[-1]
    kind = _KIND_OF.get(n) or _KIND_OF.get(re.sub(r"_v\d+$", "", n))      # Cursor 新版：run_terminal_command_v2 / read_file_v2
    if not kind:
        return None
    a = _obj(inp)
    if isinstance(a, str):                    # Codex 的读取器只留了命令行文字 / 搜的词
        s = a.strip()
        if not s:
            return None
        if kind == "shell":
            return "shell", {"command": s}
        if kind == "search":
            return "search", {"query": s}
        if kind == "fetch" and s.startswith(("http://", "https://")):
            return "fetch", {"url": s}
        return None
    if not isinstance(a, dict):
        return None
    if kind == "shell":
        c = _cmd(_first(a, "command", "cmd", "commandLine", "script"))
        if not c:
            return None
        out = {"command": c}
        d, w = _first(a, "description", "explanation"), _first(a, "workdir", "cwd", "working_directory", "directory")
        if isinstance(d, str):
            out["description"] = d
        if isinstance(w, str):
            out["workdir"] = w
        return "shell", out
    path = _first(a, "file_path", "filePath", "path", "target_file", "targetFile", "relativeWorkspacePath", "absolute_path", "file")
    if kind == "read":
        if not isinstance(path, str):
            return None
        out = {"path": path}
        o, lim = _int(_first(a, "offset", "start_line", "startLine")), _int(_first(a, "limit", "line_count"))
        if o is not None:
            out["offset"] = o
        if lim is not None:
            out["limit"] = lim
        return "read", out
    if kind == "write":
        c = _first(a, "content", "contents", "file_text", "text")
        if not isinstance(path, str) or not isinstance(c, str):
            return None
        return "write", {"path": path, "content": c}
    if kind == "edit":
        old, new = a.get("old_string", a.get("oldString", a.get("old_str", a.get("old_text")))), \
            a.get("new_string", a.get("newString", a.get("new_str", a.get("new_text"))))
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str) or not old:
            return None
        out = {"path": path, "old": old, "new": new}
        if a.get("replace_all") or a.get("replaceAll"):
            out["all"] = True
        return "edit", out
    if kind == "grep":
        p = _first(a, "pattern", "query", "regex", "search")
        if not isinstance(p, str):
            return None
        out = {"pattern": p}
        if isinstance(path, str):
            out["path"] = path
        inc = _first(a, "include", "glob", "include_pattern", "includePattern")
        if isinstance(inc, str):
            out["include"] = inc
        return "grep", out
    if kind == "glob":
        p = _first(a, "pattern", "glob_pattern", "globPattern", "glob")
        if not isinstance(p, str):
            return None
        out = {"pattern": p}
        d = _first(a, "path", "target_directory", "targetDirectory", "directory", "cwd")
        if isinstance(d, str):
            out["path"] = d
        return "glob", out
    if kind == "fetch":
        u = _first(a, "url", "uri")
        return ("fetch", {"url": u}) if isinstance(u, str) and u else None
    if kind == "search":
        q = _first(a, "query", "q", "search_term", "searchTerm", "queries")
        if isinstance(q, list):
            q = " / ".join(str(x) for x in q if x)
        return ("search", {"query": q}) if isinstance(q, str) and q else None
    return None


def _put(d: dict, **kw) -> dict:
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def emit(target: str, kind: str, a: dict, opencode_major: int = 2) -> tuple[str, dict] | None:
    """(通用工具, 参数) → 目标应用的 (工具名, 参数)；目标没有这种工具 → None（写成文字摘要）。"""
    if target == "claude":
        if kind == "shell":
            return "Bash", _put({"command": a["command"]}, description=a.get("description"))
        if kind == "read":
            return "Read", _put({"file_path": a["path"]}, offset=a.get("offset"), limit=a.get("limit"))
        if kind == "write":
            return "Write", {"file_path": a["path"], "content": a["content"]}
        if kind == "edit":
            return "Edit", _put({"file_path": a["path"], "old_string": a["old"], "new_string": a["new"]}, replace_all=a.get("all"))
        if kind == "grep":
            return "Grep", _put({"pattern": a["pattern"]}, path=a.get("path"), glob=a.get("include"))
        if kind == "glob":
            return "Glob", _put({"pattern": a["pattern"]}, path=a.get("path"))
        if kind == "fetch":
            return "WebFetch", {"url": a["url"], "prompt": "（导入的调用，原来没有提问）"}
        if kind == "search":
            return "WebSearch", {"query": a["query"]}
    elif target == "dsh":
        if kind == "shell":
            return "bash", _put({"command": a["command"], "description": a.get("description") or " ".join(a["command"].split())[:60]},
                                workdir=a.get("workdir"))
        if kind == "read":
            return "read", _put({"file_path": a["path"]}, offset=a.get("offset"), limit=a.get("limit"))
        if kind == "write":
            return "write", {"file_path": a["path"], "content": a["content"]}
        if kind == "edit":
            return "edit", _put({"file_path": a["path"], "old_string": a["old"], "new_string": a["new"]}, replace_all=a.get("all"))
        if kind == "grep":
            return "grep", _put({"pattern": a["pattern"]}, path=a.get("path"), include=a.get("include"))
        if kind == "glob":
            return "glob", _put({"pattern": a["pattern"]}, path=a.get("path"))
        if kind == "fetch":
            return "web_fetch", {"url": a["url"]}
        if kind == "search":
            return "web_search", {"queries": [a["query"]]}
    elif target == "codex":
        if kind == "shell":
            return "exec_command", _put({"cmd": a["command"]}, workdir=a.get("workdir"))
    elif target == "opencode":
        v2 = opencode_major >= 2
        fp = "path" if v2 else "filePath"
        if kind == "shell":
            return ("shell" if v2 else "bash"), _put({"command": a["command"]}, workdir=a.get("workdir"))
        if kind == "read":
            return "read", _put({fp: a["path"]}, offset=a.get("offset"), limit=a.get("limit"))
        if kind == "write":
            return "write", {fp: a["path"], "content": a["content"]}
        if kind == "edit":
            return "edit", _put({fp: a["path"], "oldString": a["old"], "newString": a["new"]}, replaceAll=a.get("all"))
        if kind == "grep":
            return "grep", _put({"pattern": a["pattern"]}, path=a.get("path"), include=a.get("include"))
        if kind == "glob":
            return "glob", _put({"pattern": a["pattern"]}, path=a.get("path"))
        if kind == "fetch":
            return "webfetch", {"url": a["url"], "format": "text"}
        if kind == "search" and v2:
            return "websearch", {"query": a["query"]}
    return None


def native(target: str, name, inp, opencode_major: int = 2) -> tuple[str, dict] | None:
    c = canon(name, inp)
    return emit(target, c[0], c[1], opencode_major) if c else None

# -*- coding: utf-8 -*-
"""导出成 .md / .html（好看、能直接打开），再导回来。

导出的文件里藏一份原始 jsonl（zlib + base64，带 sha256）：
- .md：末尾一个 HTML 注释块 <!-- ccss-raw:v1 … -->（Markdown 渲染时看不见）；开头有 YAML front-matter（标题、来源、会话号、目录）。
- .html：<script type="application/json" id="ccss-raw">…</script>；页面本身是自包含的（内联 CSS，跟系统浅色 / 深色）。
导回来：有这份原始数据 → 原样还原（Codex 的走转换器）成一个新的 Claude Code 会话；没有（别处来的聊天记录）→ 尽量按
「你 / 用户 / User」「Claude / Assistant / ChatGPT …」这样的小标题拆成一问一答的纯文字会话。都先演练，写的时候记账本、能撤回。
"""
from __future__ import annotations

import base64
import hashlib
import html as H
import json
import re
import time
import uuid
import zlib
from datetime import datetime
from pathlib import Path

from . import __version__
from . import sessions as S

RAW_MD = re.compile(r"<!--\s*ccss-raw:v1\s+(\{.*?\})\s*\n([A-Za-z0-9+/=\s]+?)\s*-->", re.S)
RAW_HTML = re.compile(r'<script type="application/json" id="ccss-raw">(.*?)</script>', re.S)


def _pack(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def _unpack(b64: str, sha: str) -> bytes:
    raw = zlib.decompress(base64.b64decode("".join(b64.split())))
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("文件里藏的原始记录校验不对（被改过？）")
    return raw


def _when(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else ""
    except Exception:
        return ""


def _entries(agent: str, raw: bytes) -> list[dict]:
    return S.parse_bytes(agent, raw)


ROLE = {"user": "你", "assistant": None, "tool": "工具"}


def _who(agent: str, role: str) -> str:
    return ROLE.get(role) or {"codex": "Codex", "dsh": "DeepSeek Harness", "cursor": "Cursor", "opencode": "OpenCode", "zcode": "ZCode"}.get(agent, "Claude")


# ---------- 导出 ----------

def to_md(agent: str, raw: bytes, meta: dict) -> str:
    es = _entries(agent, raw)
    sha = hashlib.sha256(raw).hexdigest()
    fm = {"title": meta.get("title") or "", "source": agent, "session": meta.get("id") or "", "cwd": meta.get("cwd") or "",
          "exported": datetime.now().isoformat(timespec="seconds"), "exporter": f"CC Session Sync {__version__}",
          "messages": sum(1 for e in es if e["role"] != "tool")}
    out = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items()] + ["---", "", f"# {meta.get('title') or '会话'}", ""]
    if meta.get("cwd"):
        out += [f"> { {'codex': 'Codex', 'dsh': 'DeepSeek Harness', 'cursor': 'Cursor', 'opencode': 'OpenCode', 'zcode': 'ZCode'}.get(agent, 'Claude Code') } · `{meta['cwd']}`", ""]
    tools: list[str] = []

    def flush():
        if tools:
            out.extend(["<details><summary>工具调用 ×%d</summary>" % len(tools), "", "```"] + tools + ["```", "", "</details>", ""])
            tools.clear()
    for e in es:
        if e["role"] == "tool":
            tools.append(e["text"])
            continue
        flush()
        out += [f"### {_who(agent, e['role'])}" + (f" · {_when(e.get('ts'))}" if e.get("ts") else ""), "", e["text"].rstrip(), ""]
    flush()
    head = json.dumps({"agent": agent, "id": meta.get("id"), "sha256": sha, "size": len(raw)}, ensure_ascii=False)
    b64 = _pack(raw)
    out += ["", f"<!-- ccss-raw:v1 {head}", "\n".join(b64[i:i + 100] for i in range(0, len(b64), 100)), "-->", ""]
    return "\n".join(out)


def _md_inline(s: str) -> str:
    s = H.escape(s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
    return s


def _render(text: str) -> str:
    """够用的 Markdown：``` 代码块、`行内代码`、**粗体**、段落 / 换行。"""
    parts = re.split(r"```[^\n]*\n(.*?)(?:```|\Z)", text, flags=re.S)
    out = []
    for i, p in enumerate(parts):
        if i % 2:
            out.append(f"<pre><code>{H.escape(p.rstrip())}</code></pre>")
        else:
            for para in re.split(r"\n\s*\n", p.strip()):
                if para.strip():
                    out.append("<p>" + _md_inline(para).replace("\n", "<br>") + "</p>")
    return "".join(out)


CSS = """
:root{--bg:#faf9f7;--fg:#22201c;--dim:#7a756d;--card:#fff;--line:#e8e4dd;--acc:#c96442;--user:#f3ece4;--code:#f4f2ee}
@media (prefers-color-scheme:dark){:root{--bg:#1d1c1a;--fg:#ece9e4;--dim:#9b958b;--card:#262522;--line:#37352f;--acc:#e0805c;--user:#3a302a;--code:#2d2b27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 20px 80px}h1{font-size:22px;margin:0 0 4px}.meta{color:var(--dim);font-size:13px;margin-bottom:24px}
.m{margin:14px 0;display:flex;flex-direction:column}.m .who{font-size:12px;color:var(--dim);margin-bottom:4px}
.m .b{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;max-width:100%;overflow-wrap:anywhere}
.m.user{align-items:flex-end}.m.user .b{background:var(--user);border-color:transparent;max-width:85%}
.b p{margin:.4em 0}pre{background:var(--code);border-radius:8px;padding:10px 12px;overflow:auto;font:13px/1.5 ui-monospace,Consolas,monospace}
code{background:var(--code);border-radius:4px;padding:0 4px;font:13px ui-monospace,Consolas,monospace}pre code{background:none;padding:0}
details{margin:8px 0;color:var(--dim);font-size:13px}summary{cursor:pointer}details pre{color:var(--fg)}
footer{color:var(--dim);font-size:12px;margin-top:40px;border-top:1px solid var(--line);padding-top:10px}
"""


def to_html(agent: str, raw: bytes, meta: dict) -> str:
    es = _entries(agent, raw)
    sha = hashlib.sha256(raw).hexdigest()
    title = meta.get("title") or "会话"
    body, tools = [], []

    def flush():
        if tools:
            body.append(f'<details><summary>工具调用 ×{len(tools)}</summary><pre><code>{H.escape(chr(10).join(tools))}</code></pre></details>')
            tools.clear()
    for e in es:
        if e["role"] == "tool":
            tools.append(e["text"])
            continue
        flush()
        body.append(f'<div class="m {e["role"]}"><div class="who">{H.escape(_who(agent, e["role"]))}'
                    f'{" · " + _when(e.get("ts")) if e.get("ts") else ""}</div><div class="b">{_render(e["text"])}</div></div>')
    flush()
    raw_json = json.dumps({"v": 1, "agent": agent, "id": meta.get("id"), "sha256": sha, "size": len(raw), "title": title,
                           "cwd": meta.get("cwd"), "data": _pack(raw)}, ensure_ascii=False).replace("</", "<\\/")
    src = {"codex": "Codex", "dsh": "DeepSeek Harness", "cursor": "Cursor", "opencode": "OpenCode", "zcode": "ZCode"}.get(agent, "Claude Code")
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="generator" content="CC Session Sync {__version__}"><title>{H.escape(title)}</title><style>{CSS}</style></head><body><main>'
            f'<h1>{H.escape(title)}</h1><div class="meta">{src}{" · " + H.escape(meta["cwd"]) if meta.get("cwd") else ""} · '
            f'{sum(1 for e in es if e["role"] != "tool")} 条消息 · 导出于 {datetime.now():%Y-%m-%d %H:%M}</div>'
            + "".join(body) +
            f'<footer>CC Session Sync {__version__} 导出 · 文件里带着原始记录，能用 CC Session Sync 原样导回去</footer></main>'
            f'<script type="application/json" id="ccss-raw">{raw_json}</script></body></html>')


def _safe(name: str) -> str:
    return (re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip() or "session")[:60]


def export_many(items: list[dict], fmt: str = "md", label: str = "") -> dict:
    """items=[{agent, id}] 或 [{snap}]（时间线上的某一版）。一个就写一个文件，多个放进一个文件夹。"""
    if fmt not in ("md", "html"):
        raise ValueError("格式只能是 md / html")
    from . import timeline
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = S.EXPORTS if len(items) == 1 else S.EXPORTS / f"{stamp}-{_safe(label or 'batch')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    files, errors = [], []
    for it in items:
        try:
            if it.get("snap"):
                r = timeline._snap(int(it["snap"]))
                raw, agent, sid = S._blob(r["sha"]), r["agent"], r["sid"]
                m = S.find(agent, sid) or {}
                meta = {"id": sid, "title": f"{m.get('title') or r.get('title') or '会话'}（{timeline._when(r['at'])} 的版本）",
                        "cwd": m.get("cwd") or r.get("cwd")}
            else:
                agent, sid = it["agent"], it["id"]
                f = S.agent_file(agent, sid)
                if not f:
                    raise FileNotFoundError("找不到会话记录文件")
                raw = f.read_bytes()
                m = S.find(agent, sid) or {}
                meta = {"id": sid, "title": m.get("title"), "cwd": m.get("cwd")}
            text = to_md(agent, raw, meta) if fmt == "md" else to_html(agent, raw, meta)
            p = out_dir / f"{stamp}-{_safe(meta.get('title') or sid)}-{sid[:8]}.{fmt}"
            p.write_text(text, encoding="utf-8", newline="\n")
            files.append(str(p))
        except Exception as e:
            errors.append(f"{it}: {type(e).__name__}: {e}")
    return {"ok": not errors, "files": files, "dir": str(out_dir), "errors": errors, "path": files[0] if len(files) == 1 else str(out_dir)}


# ---------- 导入 ----------

USER_H = re.compile(r"^#{1,4}\s*(你|我|用户|User|Human|Me|You|Q)\b.*$", re.I | re.M)
ANY_H = re.compile(r"^#{1,4}\s*(你|我|用户|User|Human|Me|You|Q|Claude|Assistant|ChatGPT|GPT|Codex|AI|助手|Bot|A)\b.*$", re.I | re.M)


def _plain_turns(text: str, is_html: bool) -> list[tuple[str, str]]:
    if is_html:
        t = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", text)
        t = re.sub(r'(?is)<div class="who">(.*?)</div>', lambda m: "\n### " + re.sub("<[^>]+>", "", m.group(1)) + "\n", t)
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</pre>|</h\d>", "\n", t)
        t = re.sub(r"(?i)<h\d[^>]*>", "\n### ", t)
        text = H.unescape(re.sub(r"<[^>]+>", "", t))
    text = re.sub(r"(?s)^---\n.*?\n---\n", "", text)
    text = re.sub(r"(?s)<details>.*?</details>", "", text)
    heads = list(ANY_H.finditer(text))
    turns: list[tuple[str, str]] = []
    for i, h in enumerate(heads):
        body = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        if not body:
            continue
        role = "user" if USER_H.match(h.group(0)) else "assistant"
        if turns and turns[-1][0] == role:
            turns[-1] = (role, turns[-1][1] + "\n\n" + body)
        else:
            turns.append((role, body))
    if not turns and text.strip():
        turns = [("user", text.strip()[:200000])]
    if turns and turns[0][0] == "assistant":
        turns.insert(0, ("user", "（以下是导入的一段对话记录）"))
    if turns[-1][0] == "user":
        turns.append(("assistant", "（导入的记录到这里为止。）"))
    return turns


def _records_from_turns(turns: list[tuple[str, str]], cwd: str, title: str) -> bytes:
    from . import codex_import as CI
    sid, parent, recs = str(uuid.uuid4()), None, []
    ver, now = CI.claude_version(), time.time()
    for i, (role, text) in enumerate(turns):
        u = str(uuid.uuid4())
        r = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": sid, "version": ver,
             "gitBranch": "HEAD", "type": role, "uuid": u, "timestamp": CI.iso(now - (len(turns) - i))}
        if role == "user":
            r["message"] = {"role": "user", "content": text}
        else:
            r["message"] = {"id": "msg_mdimport_" + u.replace("-", "")[:20], "type": "message", "role": "assistant",
                            "model": "md-import", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        recs.append(r)
        parent = u
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8")


def parse_file(path: str) -> dict:
    """读一个 .md / .html：→ {how: lossless|plain, agent, raw(bytes), title, cwd, messages, preview[]}"""
    p = Path(path)
    if p.suffix.lower() not in (".md", ".markdown", ".html", ".htm"):
        raise ValueError("只认 .md / .html")
    if p.stat().st_size > 200 * 1024 * 1024:
        raise ValueError("文件太大（超过 200 MB）")
    text = p.read_text(encoding="utf-8", errors="replace")
    is_html = p.suffix.lower() in (".html", ".htm")
    title, cwd, agent, raw, how = None, None, "claude", None, "plain"
    m = RAW_HTML.search(text) if is_html else RAW_MD.search(text)
    if m:
        if is_html:
            j = json.loads(m.group(1).replace("<\\/", "</"))
            raw, agent, title, cwd = _unpack(j["data"], j["sha256"]), j.get("agent") or "claude", j.get("title"), j.get("cwd")
        else:
            j = json.loads(m.group(1))
            raw, agent = _unpack(m.group(2), j["sha256"]), j.get("agent") or "claude"
            fm = re.match(r"(?s)^---\n(.*?)\n---\n", text)
            if fm:
                for line in fm.group(1).splitlines():
                    k, _, v = line.partition(":")
                    try:
                        v = json.loads(v.strip())
                    except Exception:
                        v = v.strip()
                    if k == "title":
                        title = v
                    elif k == "cwd":
                        cwd = v or None
        how = "lossless"
    if raw is None:
        h = re.search(r"(?m)^#\s+(.+)$", text) if not is_html else re.search(r"(?is)<title>(.*?)</title>", text)
        title = H.unescape(h.group(1)).strip() if h else p.stem
        turns = _plain_turns(text, is_html)
        raw = _records_from_turns(turns, cwd or str(Path.home()), title)
        agent = "claude"
    es = _entries(agent, raw)
    return {"how": how, "agent": agent, "raw": raw, "title": title or p.stem, "cwd": cwd, "file": str(p),
            "messages": sum(1 for e in es if e["role"] != "tool"),
            "preview": [dict(e, text=e["text"][:600]) for e in es[:12]], "size": len(raw)}


def import_files(paths_: list[str], cwd: str | None = None, desktop: bool = True, dry: bool = True) -> dict:
    """导入成新的 Claude Code 会话。cwd：放进哪个项目（工作目录），不填用文件里记的 / 家目录。dry=True 只看不写。"""
    from . import timeline
    rows = []
    for f in paths_:
        try:
            r = parse_file(f)
            rows.append({k: v for k, v in r.items() if k != "raw"} | {"_raw": r["raw"]})
        except Exception as e:
            rows.append({"file": f, "error": f"{type(e).__name__}: {e}"})
    if dry:
        return {"ok": all("error" not in r for r in rows), "dry": True, "rows": [{k: v for k, v in r.items() if k != "_raw"} for r in rows]}
    od, led = S._new_op("import", items=[], summary=f"导入 {len(rows)} 个 .md / .html 成 Claude Code 会话")
    made, errors = [], []
    for r in rows:
        if "error" in r:
            errors.append(f"{r['file']}: {r['error']}")
            continue
        try:
            res = timeline.write_claude_session(r["agent"], r["_raw"], f"{r['title']}（导入）", cwd or r.get("cwd"), led, od, desktop)
            led["items"].append({"agent": "claude", "id": res["id"]})
            made.append(dict(res, file_from=r["file"], how=r["how"]))
        except Exception as e:
            errors.append(f"{r['file']}: {type(e).__name__}: {e}")
    led.update(made=made, errors=errors)
    job = timeline._finish_regs(od, led) if desktop else (S._save_led(od, led) or None)
    return {"ok": not errors, "dry": False, "made": made, "errors": errors, "op": od.name, "desktop": job}

# -*- coding: utf-8 -*-
"""转换后自检（0.4.1）：刚写好的目标会话，按目标应用自己读会话的规矩再读一遍，有问题就写进结果里。

  Claude Code：每行一个 JSON；uuid / parentUuid 连成一条链；sessionId 和文件名一致；内容块的类型、图片（base64、类型、≤5 MB）；
               每个 tool_use 的结果必须就在下一条用户消息里（Claude 的接口就是这么要求的），不能有对不上的 tool_result；文字块不能是空的。
  Codex：      第一行 session_meta，id 和文件名里的一致；每行有时间且不往回走；消息内容块类型对；function_call 的 arguments 是 JSON、
               call_id 不重复，每个调用都有 function_call_output，输出不能指向不存在的调用；图片是能解开的 data URL；session_index 里有它。
  dsh：        本程序照 dsh 的格式写的校验（帧、校验和、seq 连续、事件类型、消息结构，dsh.validate），再加：每个 tool-call 都有
               tool/call 和 tool/result，引用的图片在附件库里且内容对得上。
  OpenCode：   用 OpenCode 自己的命令行导出这个会话（`opencode session export` / `opencode export`），看导出来的消息数、工具部件、图片。

最后都用本程序读这个应用的读取器读回来，和写的时候数的对一下：用户消息几条、原生工具调用几个、图片几张。只读，不改任何东西。"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

from . import ir

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _b64_ok(data, mt) -> str | None:
    if mt not in ir.IMG_TYPES:
        return f"图片类型 {mt} 不认识"
    try:
        raw = base64.b64decode(str(data or ""), validate=True)
    except Exception:
        return "图片的 base64 解不开"
    if not raw:
        return "图片是空的"
    if len(raw) > ir.IMG_MAX:
        return "图片超过 5 MB"
    return None


def _cmp(issues: list, got: dict, want: dict | None) -> None:
    for k, lab in (("user", "用户消息"), ("tools", "原生工具调用"), ("images", "图片")):
        if want and k in want and got.get(k) != want[k]:
            issues.append(f"读回来的{lab}是 {got.get(k)} 个，写的时候是 {want[k]} 个")


# ---------- Claude Code ----------

READBACK_MAX = 16 * 1024 * 1024        # 0.4.8：大于这个的只按行流式检查，不整份读回来（省内存）


def check_claude(f: Path, want: dict | None = None) -> dict:
    issues: list[str] = []
    sid = Path(f).stem
    seen, prev_tools = set(), None
    n, last_type, has_title = 0, None, False
    with open(f, "rb") as fh:                                  # 0.4.8：一行一行读，几百 MB 的会话也不整份进内存
        for i, ln in enumerate(fh):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                issues.append(f"第 {i + 1} 行不是 JSON")
                continue
            if not isinstance(r, dict):
                issues.append(f"第 {i + 1} 行不是对象")
                continue
            if r.get("type") == "custom-title":
                has_title = True
            if r.get("type") not in ("user", "assistant"):
                continue
            where = f"第 {n + 1} 条消息"
            if r.get("sessionId") != sid:
                issues.append(f"{where}：sessionId 和文件名不一致")
            u = r.get("uuid")
            if not isinstance(u, str) or u in seen:
                issues.append(f"{where}：uuid 缺了或重复")
            p = r.get("parentUuid")
            if (n == 0 and p is not None) or (n > 0 and p not in seen):
                issues.append(f"{where}：parentUuid 接不上前一条")
            seen.add(u)
            if ir.to_ms(r.get("timestamp")) is None:
                issues.append(f"{where}：时间解不开")
            m = r.get("message") or {}
            if m.get("role") != r["type"]:
                issues.append(f"{where}：message.role 和 type 不一致")
            c = m.get("content")
            blocks = [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else None
            if blocks is None:
                issues.append(f"{where}：content 既不是文字也不是列表")
                blocks = []
            ok_types = {"text", "image", "tool_result", "document"} if r["type"] == "user" else {"text", "tool_use", "thinking", "redacted_thinking"}
            results, uses = set(), set()
            for b in blocks:
                t = b.get("type") if isinstance(b, dict) else None
                if t not in ok_types:
                    issues.append(f"{where}：内容块类型 {t} 不该出现在 {r['type']} 消息里")
                    continue
                if t == "text" and not (isinstance(b.get("text"), str) and b["text"].strip()):
                    issues.append(f"{where}：有空的文字块（Claude 接口会拒绝）")
                if t == "image":
                    s_ = b.get("source") or {}
                    e = "图片不是 base64" if s_.get("type") != "base64" else _b64_ok(s_.get("data"), s_.get("media_type"))
                    if e:
                        issues.append(f"{where}：{e}")
                if t == "tool_use":
                    if not (isinstance(b.get("id"), str) and b["id"] and isinstance(b.get("name"), str) and isinstance(b.get("input"), dict)):
                        issues.append(f"{where}：tool_use 缺 id / name / input")
                    uses.add(b.get("id"))
                if t == "tool_result":
                    results.add(b.get("tool_use_id"))
                    rc = b.get("content")
                    if not (isinstance(rc, str) and rc.strip() or isinstance(rc, list) and rc):
                        issues.append(f"{where}：tool_result 是空的")
            if r["type"] == "user":
                if prev_tools and not prev_tools <= results:
                    issues.append(f"{where}：前一条的工具调用没有全部在这里给结果")
                if results - (prev_tools or set()):
                    issues.append(f"{where}：有 tool_result 对不上前一条的工具调用")
                prev_tools = None
            else:
                if prev_tools:
                    issues.append(f"{where}：工具调用后面没有紧跟结果")
                prev_tools = uses or None
            n, last_type = n + 1, r["type"]
            if len(issues) > 200:
                break
    if not n:
        issues.append("一条消息都没有")
    if prev_tools:
        issues.append("最后停在工具调用上，没有结果")
    if n and last_type != "assistant":
        issues.append("最后一条不是助手的回复（接着聊时历史会接不上）")
    if not has_title:
        issues.append("没有标题记录")
    if want is None and Path(f).stat().st_size > READBACK_MAX:
        return {"ok": not issues, "issues": issues[:30], "by": "按 Claude Code 的会话规矩", "streamed": True}
    from . import convert
    try:
        _h, items = convert._claude_ir(Path(f))
        got = {"user": sum(1 for x in items if x["role"] == "user"), "tools": sum(1 for x in items if x.get("kind") == "tool"),
               "images": sum(len(x.get("image_data") or []) for x in items)}
        _cmp(issues, got, want)
    except Exception as e:
        issues.append(f"读回来出错：{type(e).__name__}: {e}")
    return {"ok": not issues, "issues": issues[:30], "by": "按 Claude Code 的会话规矩"}


# ---------- Codex ----------

KNOWN_CODEX = {"session_meta", "response_item", "event_msg", "turn_context", "compacted", "world_state", "token_usage_record"}


def check_codex(f: Path, want: dict | None = None, index: Path | None = None) -> dict:
    issues: list[str] = []
    f = Path(f)
    m = _UUID.search(f.name)
    last, calls, outs = 0, {}, set()
    lines = f.read_bytes().splitlines()
    for i, ln in enumerate(lines):
        try:
            d = json.loads(ln)
        except Exception:
            issues.append(f"第 {i + 1} 行不是 JSON")
            continue
        ts = ir.to_ms(d.get("timestamp"))
        if ts is None:
            issues.append(f"第 {i + 1} 行没有时间")
        elif ts < last:
            issues.append(f"第 {i + 1} 行的时间往回走了")
        last = max(last, ts or 0)
        t, pl = d.get("type"), d.get("payload") or {}
        if i == 0:
            if t != "session_meta":
                issues.append("第一行不是 session_meta")
            elif not m or pl.get("id") != m.group(0):
                issues.append("session_meta 的 id 和文件名对不上")
            elif not isinstance(pl.get("cwd"), str):
                issues.append("session_meta 缺 cwd")
            continue
        if t not in KNOWN_CODEX:
            issues.append(f"第 {i + 1} 行：类型 {t} Codex 不认识")
        if t != "response_item":
            continue
        pt = pl.get("type")
        if pt == "message":
            if pl.get("role") not in ("user", "assistant", "developer", "system"):
                issues.append(f"第 {i + 1} 行：消息的 role 不对")
            for c in pl.get("content") or []:
                ct = c.get("type") if isinstance(c, dict) else None
                if ct not in ("input_text", "output_text", "input_image", "refusal"):
                    issues.append(f"第 {i + 1} 行：内容块类型 {ct} 不认识")
                elif ct == "input_image":
                    im = ir.img_url(c.get("image_url"))
                    if not im:
                        issues.append(f"第 {i + 1} 行：图片不是能解开的 data URL")
                elif not isinstance(c.get("text"), str):
                    issues.append(f"第 {i + 1} 行：文字块缺 text")
        elif pt == "function_call":
            cid = pl.get("call_id")
            if not cid or cid in calls:
                issues.append(f"第 {i + 1} 行：call_id 缺了或重复")
            try:
                if not isinstance(json.loads(pl.get("arguments") or ""), dict):
                    raise ValueError
            except Exception:
                issues.append(f"第 {i + 1} 行：function_call 的 arguments 不是 JSON 对象")
            if not isinstance(pl.get("name"), str):
                issues.append(f"第 {i + 1} 行：function_call 缺 name")
            calls[cid] = i
        elif pt == "function_call_output":
            if pl.get("call_id") not in calls:
                issues.append(f"第 {i + 1} 行：function_call_output 找不到前面的调用")
            if not isinstance(pl.get("output"), (str, list, dict)):
                issues.append(f"第 {i + 1} 行：function_call_output 缺 output")
            outs.add(pl.get("call_id"))
    if set(calls) - outs:
        issues.append(f"有 {len(set(calls) - outs)} 个工具调用没有输出")
    if index is not None and m:
        try:
            ids = {(ir._json(x) or {}).get("id") for x in Path(index).read_bytes().splitlines()}
            if m.group(0) not in ids:
                issues.append("session_index.jsonl 里没有这个线程")
        except OSError:
            issues.append("读不到 session_index.jsonl")
    from . import convert
    try:
        _h, items = convert._codex_ir(f)
        got = {"user": sum(1 for x in items if x["role"] == "user"), "tools": sum(1 for x in items if x.get("kind") == "tool"),
               "images": sum(len(x.get("image_data") or []) for x in items)}
        _cmp(issues, got, want)
    except Exception as e:
        issues.append(f"读回来出错：{type(e).__name__}: {e}")
    return {"ok": not issues, "issues": issues[:30], "by": "按 Codex 的 rollout 规矩"}


# ---------- dsh ----------

def check_dsh(f: Path, want: dict | None = None) -> dict:
    from . import dsh
    v = dsh.validate(Path(f))
    issues = list(v.get("errors") or [])
    try:
        L = dsh.load(Path(f))
        evs = L["events"]
        tc = {b.get("id") for e in evs if e.get("type") == "assistant/message" for b in dsh._msg(e).get("content") or []
              if isinstance(b, dict) and b.get("type") == "tool-call"}
        calls = {(e.get("data") or {}).get("callId") for e in evs if e.get("type") == "tool/call"}
        res = {dsh._msg(e).get("toolCallId") for e in evs if e.get("type") == "tool/result"}
        if tc - calls:
            issues.append(f"{len(tc - calls)} 个 tool-call 没有 tool/call 事件")
        if tc - res:
            issues.append(f"{len(tc - res)} 个 tool-call 没有结果")
        for e in evs:
            if e.get("type") != "user/message":
                continue
            for b in dsh._msg(e).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "image":
                    data, why = dsh._attachment(b.get("attachment") or {})
                    if data is None:
                        issues.append(f"seq {e.get('seq')}：图片 {why}")
        from . import convert
        _h, items = convert._dsh_ir(Path(f))
        got = {"user": sum(1 for x in items if x["role"] == "user"), "tools": sum(1 for x in items if x.get("kind") == "tool"),
               "images": sum(len(x.get("image_data") or []) for x in items)}
        _cmp(issues, got, want)
    except Exception as e:
        issues.append(f"读回来出错：{type(e).__name__}: {e}")
    return {"ok": not issues, "issues": issues[:30], "by": "按 dsh 第 4 版会话格式"}


# ---------- OpenCode ----------

def export_cli(sid: str, cli: str, major: int, cwd: str | None = None, timeout: int = 60) -> dict | None:
    """OpenCode 自己的命令行导出这个会话（只读）。"""
    import tempfile
    from . import ocstore
    args = [cli, "session", "export", "--standalone", sid] if major >= 2 else [cli, "export", sid]
    run_cwd = cwd if cwd and Path(cwd).is_dir() else tempfile.gettempdir()
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=run_cwd, env=ocstore._cli_env(run_cwd))
    except Exception:
        return None
    out = r.stdout
    i = out.find("{")
    try:
        return json.loads(out[i:]) if i >= 0 else None
    except Exception:
        return None


def check_opencode(sid: str, cli: str | None, major: int, want: dict | None = None, cwd: str | None = None) -> dict:
    issues: list[str] = []
    if not cli:
        return {"ok": True, "issues": [], "by": "没找到 OpenCode 命令行，没查", "skipped": True}
    d = export_cli(sid, cli, major, cwd)
    if not isinstance(d, dict):
        return {"ok": False, "issues": ["OpenCode 命令行导不出这个会话（它自己读不了？）"], "by": "OpenCode 自己的命令行"}
    info = d.get("info") or {}
    if info.get("id") != sid:
        issues.append("导出来的会话号不对")
    msgs = d.get("messages") or []
    user = tools = images = 0
    for m in msgs:
        if major >= 2:
            t = m.get("type")
            if t == "user":
                user += 1
                for x in m.get("files") or []:
                    if str(x.get("mime") or "").startswith("image/"):
                        e = _b64_ok(x.get("data"), x.get("mime"))
                        if e:
                            issues.append(e)
                        images += 1
            elif t == "assistant":
                for c in m.get("content") or []:
                    if c.get("type") == "tool":
                        tools += 1
                        st = c.get("state") or {}
                        if st.get("status") not in ("completed", "error") or not isinstance(st.get("input"), dict):
                            issues.append("有工具部件的状态 / 输入不对")
        else:
            info_m, parts = m.get("info") or {}, m.get("parts") or []
            if info_m.get("role") == "user":
                user += 1
            for p in parts:
                if p.get("type") == "tool":
                    tools += 1
                    st = p.get("state") or {}
                    if st.get("status") not in ("completed", "error") or not isinstance(st.get("input"), dict):
                        issues.append("有工具部件的状态 / 输入不对")
                elif p.get("type") == "file" and str(p.get("mime") or "").startswith("image/"):
                    images += 1
                    if not ir.img_url(p.get("url")):
                        issues.append("有图片不是能解开的 data URL")
    _cmp(issues, {"user": user, "tools": tools, "images": images}, want)
    return {"ok": not issues, "issues": issues[:30], "by": "OpenCode 自己的命令行（session export）"}


def expect(stats: dict, turns_list=None) -> dict:
    return {"user": stats.get("user_turns"), "tools": stats.get("tools_native", 0), "images": stats.get("images_kept", 0)}

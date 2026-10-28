# -*- coding: utf-8 -*-
"""格式自检（0.3.8）：六个应用的存储格式 / 版本是不是本程序认识的那种；变了就说清楚哪儿变了、有几个会话读不出来，
而不是悄悄少几个会话。

每个应用一项：{agent, name, status: ok | warn | fail | absent, format, read, raw, unread, issues[], fix, sig}
- ok：格式认识，会话都读得出来；
- warn：格式有我们没见过的新东西（新的记录类型 / 字段版本），会话读得出来但可能缺一部分；
- fail：有会话读不出来（缺表 / 缺列 / 头不对 / 新的格式版本），或者整个库都读不了；
- absent：没装 / 没数据，不提示。

全部只读：OpenCode / ZCode 的库先复制一份再打开（和平时读一样），只看表名、列名、会话 / 消息条数，不碰账号 / 凭据表；
Cursor 的 state.vscdb 只读打开，只看键名和 _v；Codex / Claude Code 每个文件只读开头一小段；dsh 只看文件名里的格式版本。
结果缓存一分钟；签名（哪些应用有问题 + 问题种类）变了才发一次通知。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import paths

STATE = paths.HOME / "formats.json"
NAMES = {"claude": "Claude Code", "codex": "Codex", "dsh": "DeepSeek Harness", "cursor": "Cursor", "opencode": "OpenCode", "zcode": "ZCode"}
SAMPLE = 120                  # 每个应用最多看最近的多少个文件
HEAD = 256 * 1024             # 每个文件最多读开头多少字节

# OpenCode / ZCode：本程序读的表和列（ocstore._read_db 用到的）
OC_V2 = {"session_v2": {"id", "title", "directory", "time_created", "time_updated", "parent_id", "time_archived", "model", "project_id"},
         "session_message": {"session_id", "type", "time_created", "data", "seq"}}
OC_V1 = {"session": {"id", "title", "directory", "time_created", "time_updated", "parent_id", "time_archived", "project_id"},
         "message": {"id", "session_id", "time_created", "data"},
         "part": {"message_id", "session_id", "data"}}
OC_SECRET = ("account", "credential", "auth", "token", "key")      # 这些表连名字以外的东西都不看
CODEX_TOP = {"session_meta", "response_item", "event_msg", "turn_context", "compacted"}
CODEX_ITEM = {"message", "reasoning", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
              "local_shell_call", "local_shell_call_output", "web_search_call", "tool_search_call", "tool_search_output",
              "image_generation_call", "ghost_snapshot", "compaction", "compaction_summary"}
CURSOR_COMPOSER_V = 18        # composerData 的 _v，本程序按这一版写的
CURSOR_BUBBLE_V = 3
CLAUDE_CONV = {"user", "assistant"}

_cache: dict = {"at": 0.0, "res": None}


def _r(agent: str, status: str, fmt: str = "", read: int | None = None, raw: int | None = None, issues: list | None = None, fix: str = "") -> dict:
    unread = max(0, (raw or 0) - (read or 0)) if raw is not None and read is not None else None
    return {"agent": agent, "name": NAMES[agent], "status": status, "format": fmt, "read": read, "raw": raw, "unread": unread,
            "issues": issues or [], "fix": fix}


def _newest(files, n: int = SAMPLE) -> list[Path]:
    fs = []
    for f in files:
        try:
            fs.append((f.stat().st_mtime, f))
        except OSError:
            pass
    fs.sort(key=lambda x: -x[0])
    return [f for _, f in fs[:n]]


def _lines(f: Path, limit: int = HEAD) -> list[bytes]:
    with open(f, "rb") as fh:
        b = fh.read(limit)
    ls = b.splitlines()
    if len(b) == limit and ls:
        ls = ls[:-1]                       # 最后一行可能被截断
    return [x for x in ls if x.strip()]


# ---------- 各应用 ----------

def probe_claude() -> dict:
    root = paths.CLAUDE_HOME / "projects"
    if not root.is_dir():
        return _r("claude", "absent")
    files = _newest(root.glob("*/*.jsonl"))
    if not files:
        return _r("claude", "absent")
    bad, noconv, shape, types = 0, 0, 0, {}
    for f in files:
        try:
            ls = _lines(f)
        except OSError:
            bad += 1
            continue
        if not ls:
            continue
        ok_json, conv = 0, 0
        for ln in ls:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ok_json += 1
            t = d.get("type")
            types[t] = types.get(t, 0) + 1
            if t in CLAUDE_CONV:
                m = d.get("message")
                if not isinstance(m, dict) or not isinstance(m.get("content"), (str, list)):
                    shape += 1
                else:
                    conv += 1
        if ok_json < len(ls) / 2:
            bad += 1
        elif not conv and any(t in CLAUDE_CONV for t in types):
            noconv += 1
    issues, st = [], "ok"
    if bad:
        issues.append(f"{bad} 个记录文件大半行不是 JSON（格式变了或文件写坏了）")
        st = "fail"
    if shape:
        issues.append(f"{shape} 条问 / 答记录没有 message.content（字段改名了？）")
        st = "fail"
    unread = bad + (noconv if shape else 0)
    return _r("claude", st, f"jsonl（看了最近 {len(files)} 个文件）", len(files) - unread, len(files), issues,
              "" if st == "ok" else "可能是 Claude Code 新版本改了记录格式：先升级 CC Session Sync；还不行把「诊断 → 复制诊断报告」发给作者")


def probe_codex() -> dict:
    from . import sessions as S
    try:
        all_f = list(S.rollouts())
    except Exception:
        all_f = []
    base = S.codex_home() / "sessions"
    old = [f for f in base.rglob("rollout-*.json")] if base.is_dir() else []
    if not all_f and not old:
        return _r("codex", "absent")
    files = _newest(all_f)
    nometa, unk_top, unk_item, vers = 0, {}, {}, {}
    for f in files:
        try:
            ls = _lines(f)
        except OSError:
            nometa += 1
            continue
        if not ls:
            continue
        try:
            first = json.loads(ls[0])
        except Exception:
            first = None
        if not (isinstance(first, dict) and first.get("type") == "session_meta" and (first.get("payload") or {}).get("id")):
            nometa += 1
            continue
        v = (first.get("payload") or {}).get("cli_version")
        if v:
            vers[v] = vers.get(v, 0) + 1
        for ln in ls[1:]:
            try:
                d = json.loads(ln)
            except Exception:
                continue
            t = d.get("type") if isinstance(d, dict) else None
            if t not in CODEX_TOP:
                unk_top[str(t)] = unk_top.get(str(t), 0) + 1
            elif t == "response_item":
                pt = (d.get("payload") or {}).get("type")
                if pt not in CODEX_ITEM:
                    unk_item[str(pt)] = unk_item.get(str(pt), 0) + 1
    issues, st = [], "ok"
    if nometa:
        issues.append(f"{nometa} 个会话文件第一行不是 session_meta（读不出会话号 / 目录）")
        st = "fail"
    if old:
        issues.append(f"{len(old)} 个是很早以前的 .json 格式（不是 .jsonl），不支持")
        st = "fail" if st == "fail" else "warn"
    if unk_top or unk_item:
        top = sorted({**unk_top, **unk_item}.items(), key=lambda x: -x[1])[:4]
        issues.append("有没见过的记录类型：" + "、".join(f"{k}×{n}" for k, n in top) + "（这部分内容不会带过来）")
        st = st if st == "fail" else "warn"
    newest = max(vers, key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")]) if vers else None
    fmt = "rollout jsonl" + (f"，最新写入的 Codex {newest}" if newest else "")
    return _r("codex", st, fmt, len(files) - nometa, len(files) + len(old) if len(files) < SAMPLE else None, issues,
              "" if st == "ok" else "Codex 新版本可能改了 rollout 格式：先升级 CC Session Sync；会话本身没事，只是这里读不全")


def probe_dsh() -> dict:
    from . import dsh
    try:
        det = dsh.detect()
    except Exception:
        det = {}
    try:
        fs = dsh.session_files()
    except Exception as e:
        return _r("dsh", "fail", "", None, None, [f"读会话目录出错：{type(e).__name__}: {e}"], "检查 DSH_HOME / 目录权限")
    if not fs:
        return _r("dsh", "absent") if not det.get("found") else _r("dsh", "ok", f"session.v{dsh.FORMAT}.jsonl", 0, 0)
    vers: dict = {}
    for x in fs:
        vers[x["version"]] = vers.get(x["version"], 0) + 1
    other = {v: n for v, n in vers.items() if v != dsh.FORMAT}
    issues, st = [], "ok"
    newer = {v: n for v, n in other.items() if v > dsh.FORMAT}
    older = {v: n for v, n in other.items() if v < dsh.FORMAT}
    if newer:
        issues.append("、".join(f"{n} 个是 v{v} 格式" for v, n in sorted(newer.items())) + f"（本程序认 v{dsh.FORMAT}），读不出来")
        st = "fail"
    if older:
        issues.append("、".join(f"{n} 个是老的 v{v} 格式" for v, n in sorted(older.items())) + "（dsh 打开时会自己升级），先不读")
        st = st if st == "fail" else "warn"
    if any(x["compression"] == "zstd" for x in fs) and not det.get("zstd", True):
        issues.append("会话是 zstd 压缩的，但缺 zstandard 模块")
        st = "fail"
    read = vers.get(dsh.FORMAT, 0)
    return _r("dsh", st, f"session.v{dsh.FORMAT}.jsonl" + ("(.zstd)" if any(x["compression"] == "zstd" for x in fs) else ""), read, len(fs), issues,
              "" if st == "ok" else ("dsh 升级了会话格式：升级 CC Session Sync 后才读得到" if newer else "用 dsh 打开一次这些会话（它会升级格式），或者不用管"))


def probe_cursor() -> dict:
    from . import cursor
    db = cursor.global_db()
    try:
        tr = list(cursor.transcript_files())
    except Exception:
        tr = []
    try:
        cl = list(cursor.cli_files())
    except Exception:
        cl = []
    if not db.exists() and not tr and not cl:
        return _r("cursor", "absent")
    try:
        rows = cursor.refresh()
    except Exception as e:
        return _r("cursor", "fail", "", None, None, [f"读不了：{type(e).__name__}: {e}"], "把「诊断 → 复制诊断报告」发给作者")
    got = {r["id"] for r in rows}
    issues, st, raw, unread_ids = [], "ok", 0, set()
    cv, bv = {}, {}
    if db.exists():
        try:
            c = cursor.connect(db)
            try:
                tabs = {n for (n,) in c.execute("select name from sqlite_master where type='table'")}
                if "cursorDiskKV" not in tabs:
                    issues.append("state.vscdb 里没有 cursorDiskKV 表（Cursor 换了存储方式），编辑器里的对话读不出来")
                    st = "fail"
                else:
                    for k, v in cursor._range(c, "composerData:"):
                        d = cursor._json(v)
                        if not isinstance(d, dict):
                            continue
                        cv[d.get("_v")] = cv.get(d.get("_v"), 0) + 1
                        heads = [x for x in d.get("fullConversationHeadersOnly") or [] if isinstance(x, dict) and x.get("bubbleId")]
                        if heads:
                            raw += 1
                            if d.get("composerId") not in got:
                                unread_ids.add(d.get("composerId"))
                    n = 0
                    for k, v in cursor._range(c, "bubbleId:"):
                        d = cursor._json(v)
                        if isinstance(d, dict):
                            bv[d.get("_v")] = bv.get(d.get("_v"), 0) + 1
                        n += 1
                        if n >= 400:
                            break
            finally:
                c.close()
        except Exception as e:
            issues.append(f"打不开 state.vscdb：{type(e).__name__}: {e}")
            st = "fail"
    err = (cursor._STATE or {}).get("error")
    if err:
        issues.append(err)
        st = "fail"
    newer_c = sorted(v for v in cv if isinstance(v, int) and v > CURSOR_COMPOSER_V)
    newer_b = sorted(v for v in bv if isinstance(v, int) and v > CURSOR_BUBBLE_V)
    if newer_c or newer_b:
        issues.append("Cursor 的对话数据版本比本程序见过的新：" + "、".join(
            ([f"对话 _v={','.join(map(str, newer_c))}（见过 {CURSOR_COMPOSER_V}）"] if newer_c else []) +
            ([f"消息 _v={','.join(map(str, newer_b))}（见过 {CURSOR_BUBBLE_V}）"] if newer_b else [])))
        st = st if st == "fail" else "warn"
    if unread_ids:
        issues.append(f"{len(unread_ids)} 个有消息的对话一条也读不出来" + ("" if newer_c or newer_b else "（可能是 Cursor 新格式，也可能是 Cursor 自己清掉了旧内容）"))
        st = "fail" if newer_c or newer_b or st == "fail" else "warn"
    fmt = "state.vscdb" + (f"（对话 _v {','.join(str(v) for v in sorted(x for x in cv if isinstance(x, int)))}）" if cv else "") + \
          (f" + agent 记录 {len(tr)}" if tr else "") + (f" + cursor-agent {len(cl)}" if cl else "")
    return _r("cursor", st, fmt, len(rows), len(rows) + len(unread_ids), issues,
              "" if st == "ok" else "Cursor 升级后改了对话的存法：先升级 CC Session Sync；还不行把诊断报告发给作者")


def _oc(agent: str) -> dict:
    from . import ocstore
    f = ocstore.db_path(agent)
    legacy = ocstore.legacy_count(agent)
    if not f.exists():
        if legacy:
            return _r(agent, "warn", "旧版 JSON 存储（storage/session）", 0, legacy,
                      [f"{legacy} 个会话是很早的 JSON 存储（{NAMES[agent]} 0.x），本程序只读 SQLite 库"], f"升级 {NAMES[agent]}（它会迁移到新库）")
        return _r(agent, "absent")
    rows = ocstore.refresh(agent)
    st0 = ocstore._STATE.get(agent) or {}
    stat = st0.get("stat") or {}
    issues, st, raw, fmt, has_user = [], "ok", 0, [], set()
    try:
        with ocstore._Snap(f) as con:
            T = {}
            for (n,) in con.execute("select name from sqlite_master where type='table'"):
                if any(x in n.lower() for x in OC_SECRET):
                    continue                                    # 账号 / 凭据表：只当没看见
                T[n] = {r[1] for r in con.execute(f"pragma table_info(`{n}`)")}
            v2 = all(t in T for t in OC_V2)
            v1 = all(t in T for t in OC_V1)
            if v2:
                fmt.append("2.x（session_v2）")
                miss = {t: sorted(cols - T[t]) for t, cols in OC_V2.items() if cols - T[t]}
                if miss:
                    issues.append("2.x 的表缺列：" + "；".join(f"{t} 缺 {', '.join(c)}" for t, c in miss.items()))
                    st = "fail"
                raw += con.execute("select count(*) from session_v2").fetchone()[0]
                if not miss:
                    has_user |= {r[0] for r in con.execute("select distinct session_id from session_message where type = 'user'")}
            if v1:
                fmt.append("1.x（session / message / part）")
                miss = {t: sorted(cols - T[t]) for t, cols in OC_V1.items() if cols - T[t]}
                if miss:
                    issues.append("1.x 的表缺列：" + "；".join(f"{t} 缺 {', '.join(c)}" for t, c in miss.items()))
                    st = "fail"
                where = " where time_deleted is null" if "time_deleted" in T["session"] else ""
                raw += con.execute(f"select count(*) from session{where}").fetchone()[0]
            newer = sorted(n for n in T if n.startswith("session_v") and n not in OC_V2 and n[9:].isdigit())
            if newer:
                issues.append(f"发现新版本的表 {', '.join(newer)}（还不认识的格式）：里面的会话读不出来")
                st = "fail"
                for n in newer:
                    try:
                        raw += con.execute(f"select count(*) from `{n}`").fetchone()[0]
                    except sqlite3.Error:
                        pass
            if not v1 and not v2 and not newer:
                issues.append("库里没有认识的会话表（session / session_v2）：格式变了")
                st = "fail"
    except Exception as e:
        issues.append(f"打不开库：{type(e).__name__}: {e}")
        st = "fail"
    if st0.get("error"):
        issues.append("读的时候出错：" + st0["error"])
        st = "fail"
    got = (stat.get("v1") or 0) + (stat.get("v2") or 0)           # 读进来的（ZCode 删了的任务后面才去掉，这里还算）
    unread = max(0, raw - got)
    if unread and not any("新版本的表" in i or "缺列" in i for i in issues):
        issues.append(f"{unread} 个会话读不出来")
        st = "fail"
    blank = [r for r in rows if r["id"] in has_user and not r.get("msgs")]
    if blank:
        issues.append(f"{len(blank)} 个有消息的会话一条也读不出来（消息格式可能变了）")
        st = st if st == "fail" else "warn"
    if agent == "zcode":
        tf = ocstore.data_dir("zcode") / "v2" / "tasks-index.sqlite"
        if tf.exists():
            try:
                with ocstore._Snap(tf) as con:
                    cols = {r[1] for r in con.execute("pragma table_info(tasks)")}
                if "task_id" not in cols:
                    issues.append("桌面端任务索引（v2/tasks-index.sqlite）格式变了：改过的标题 / 删除标记用不上")
                    st = st if st == "fail" else "warn"
            except Exception:
                pass
    if legacy:
        issues.append(f"另有 {legacy} 个旧版 JSON 存储的会话不读")
        st = st if st != "ok" else "warn"
    r = _r(agent, st, " + ".join(fmt) or "—", len(rows), None, issues,
           "" if st == "ok" else f"{NAMES[agent]} 升级后改了库的结构：先升级 CC Session Sync；会话本身没事，只是这里读不全")
    r.update(raw=raw, unread=unread + len(blank))
    return r


def probe_opencode() -> dict:
    return _oc("opencode")


def probe_zcode() -> dict:
    return _oc("zcode")


PROBES = {"claude": probe_claude, "codex": probe_codex, "dsh": probe_dsh, "cursor": probe_cursor, "opencode": probe_opencode, "zcode": probe_zcode}


def check(agent: str) -> dict:
    try:
        return PROBES[agent]()
    except Exception as e:                       # 自检自己出错不能拖垮别的
        return _r(agent, "warn", "", None, None, [f"自检出错：{type(e).__name__}: {e}"], "")


def check_all(max_age: float = 60, force: bool = False) -> dict:
    if not force and _cache["res"] and time.time() - _cache["at"] < max_age:
        return _cache["res"]
    t0 = time.time()
    items = [check(a) for a in PROBES]
    bad = [x for x in items if x["status"] in ("warn", "fail")]
    sig = "|".join(sorted(f"{x['agent']}:{x['status']}:{len(x['issues'])}" for x in bad))
    res = {"items": items, "problems": len(bad), "fail": sum(1 for x in bad if x["status"] == "fail"), "sig": sig,
           "time": time.time(), "ms": int((time.time() - t0) * 1000)}
    _cache.update(at=time.time(), res=res)
    _notify(res)
    return res


def _notify(res: dict) -> None:
    """有新问题（签名变了）时记一条事件 / 通知；同样的问题不重复提醒。"""
    try:
        prev = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        prev = {}
    if res["sig"] == prev.get("sig"):
        return
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"sig": res["sig"], "time": res["time"], "seen": prev.get("seen") if res["sig"] == prev.get("seen_sig") else None},
                                    ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    if not res["sig"]:
        return
    try:
        from . import events
        bad = [x for x in res["items"] if x["status"] in ("warn", "fail")]
        events.add("format", "有应用的会话格式变了：" + "、".join(x["name"] for x in bad),
                   "；".join(f"{x['name']}：{x['issues'][0]}" for x in bad if x["issues"])[:300], {"page": "diag"})
    except Exception:
        pass


def seen() -> dict:
    """用户点了「知道了」：同样的问题不再在顶上提示（诊断里照样看得到）。"""
    res = check_all()
    try:
        STATE.write_text(json.dumps({"sig": res["sig"], "time": res["time"], "seen": True, "seen_sig": res["sig"]}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


def banner() -> dict | None:
    """顶上提示条用：有问题且没点过「知道了」时给一个简短说明。"""
    res = check_all()
    if not res["problems"]:
        return None
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("seen") and st.get("seen_sig") == res["sig"]:
        return None
    bad = [x for x in res["items"] if x["status"] in ("warn", "fail")]
    return {"level": "fail" if res["fail"] else "warn", "apps": [x["name"] for x in bad],
            "unread": sum(x["unread"] or 0 for x in bad), "sig": res["sig"],
            "text": "；".join(f"{x['name']}：{x['issues'][0]}" for x in bad if x["issues"])}


def selfcheck_items() -> list[dict]:
    """给「诊断自检」用的条目（没装的应用不出现）。"""
    out = []
    for x in check_all(force=True)["items"]:
        if x["status"] == "absent":
            continue
        n = f"{x['read']} 个会话读得出来" if x["read"] is not None else ""
        if x["unread"]:
            n += f"，{x['unread']} 个读不出来"
        det = "；".join(filter(None, [x["format"], n] + x["issues"]))
        out.append({"id": f"format-{x['agent']}", "name": f"{x['name']} 存储格式", "status": {"ok": "pass"}.get(x["status"], x["status"]),
                    "detail": det, "fix": x["fix"]})
    return out

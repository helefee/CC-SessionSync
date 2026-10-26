# -*- coding: utf-8 -*-
"""用量统计：自己扫 Claude Code / Claude Desktop / Codex 的会话日志，算 token、缓存和按 API 价折算的费用。

数据来源（都只读）：
- Claude：<CLAUDE_CONFIG_DIR>/projects/**/*.jsonl 里 assistant 行的 message.usage，外加桌面端 Cowork 自己的
  <数据目录>/local-agent-mode-sessions/**/.claude/projects（订阅登录和 API 模式 Claude-3p 两套数据目录都看）。
  同一次调用会被拆成好几行（流式快照：每个内容块一行，usage 逐步变大，最后一行才有 stop_reason），会话分叉 / 续接
  还会把旧行复制进新文件。按 message.id + requestId 认作同一次调用（没有 requestId 的按 message.id），留最完整的那份
  （usage 加起来最大；一样大时留有 stop_reason 的），日期用最早那次。
  在桌面端 API 模式会话列表里登记过的会话，供应商算「API 模式 · <类型>」、不算订阅。
- Codex：<CODEX_HOME>/sessions/**/rollout-*.jsonl 和 archived_sessions/**。新版每次响应写一条 token_usage_record
  （带 response_id，按它去重）；老版只有 event_msg/token_count（累计 total_token_usage + 本次 last_token_usage）：
  每条的用量 = 这次累计 − 上次累计（累计不变 = 0，同一份快照又写了一次）；累计变小（上下文压缩、运行时重启）或者
  这个文件第一条（续接的会话累计里含着以前的）就退回用 last_token_usage，并以这次累计为新的基线（高水位）。
  键用「时间戳 + 累计值」：续接 / 分叉的会话把旧的 token_count 重放进新文件，键一样，不会算两遍；归档前后也一样。
- 读到哪儿记在「文件身份」上（Windows 上是卷号 + 文件号，挪进归档目录也认得出），只读新加的部分。
token 口径统一成：新输入（不含缓存读 / 写）、缓存读、缓存写（5 分钟 / 1 小时）、输出（含思考 / reasoning，不另加）。
费用按每一条请求算（存进库里，界面上能看到拆分）：token × 价格（manual > models.dev > 内置表）× 供应商倍率，
再按这条请求的完整提示长度套长上下文档（OpenAI >272K：输入 / 缓存 ×2、输出 ×1.5；Claude Haiku 5.5 >100K 全部 ×5；
老的 Sonnet 4 / 4.5 >200K：输入 ×2、输出 ×1.5），日志里有 fast / priority / flex / US 推理地区就按对应倍率（价格表里没有
这个倍率的照标准价算并标「估」）。查不到价格就是 None（界面显示 —），不算 0；总数里有没价格的部分就标「部分」。
按天的统计用本机时区的日历日。
去重规则参考 magpie（MIT），内置价格表的种子数据来自 cc-switch（MIT），见 THIRD_PARTY_NOTICES.md；核对过官方价格页的条目带核对日期。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from . import paths

DB_PATH = paths.HOME / "usage.sqlite"
TOK = ("inp", "cr", "cw5", "cw1h", "out")
CPARTS = ("c_in", "c_cr", "c_cw5", "c_cw1h", "c_out")
SCHEMA = 2
_lock = threading.RLock()
STATE = {"scanning": False, "last": 0.0, "last_ms": 0, "files": 0, "added": 0, "error": "", "first_done": False, "migrated": None}
_NEWCOLS = (("stop", "INT"), ("prompt", "INT"), ("tier", "TEXT"), ("geo", "TEXT"), ("proj", "TEXT"), ("ver", "INT"),
            ("c_in", "REAL"), ("c_cr", "REAL"), ("c_cw5", "REAL"), ("c_cw1h", "REAL"), ("c_out", "REAL"), ("priced", "INT"), ("est", "INT"), ("pfix", "INT"))


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, app TEXT, src TEXT, ts REAL, sid TEXT, provider TEXT, sub INT,
        model TEXT, inp INT, cr INT, cw5 INT, cw1h INT, out INT, reasoning INT, tot INT, agent INT, fid TEXT);
    CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
    CREATE TABLE IF NOT EXISTS files(fid TEXT PRIMARY KEY, path TEXT, kind TEXT, size INT, mtime REAL, off INT, state TEXT);
    CREATE TABLE IF NOT EXISTS prices(model TEXT PRIMARY KEY, inp REAL, out REAL, cr REAL, cw5 REAL, cw1h REAL, src TEXT, at REAL);
    CREATE TABLE IF NOT EXISTS mult(provider TEXT PRIMARY KEY, m REAL);
    CREATE TABLE IF NOT EXISTS hourly(h INT, app TEXT, provider TEXT, model TEXT, sub INT, n INT, inp INT, cr INT, cw5 INT, cw1h INT, out INT,
        PRIMARY KEY(h, app, provider, model, sub));
    CREATE TABLE IF NOT EXISTS daily(d TEXT, app TEXT, provider TEXT, model TEXT, sub INT, n INT, inp INT, cr INT, cw5 INT, cw1h INT, out INT,
        PRIMARY KEY(d, app, provider, model, sub));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    have = {r[1] for r in c.execute("PRAGMA table_info(calls)")}
    for col, typ in _NEWCOLS:
        if col not in have:
            c.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
    return c


def _meta(c, k: str, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


# ---------------- 环境：供应商名、是不是订阅 ----------------

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _claude_ctx() -> dict:
    s = _read_json(paths.CLAUDE_HOME / "settings.json")
    env = s.get("env") if isinstance(s.get("env"), dict) else {}
    base = env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or ""
    keyed = any(env.get(k) or os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")) or bool(s.get("apiKeyHelper"))
    cj = _read_json(paths.CLAUDE_HOME.parent / ".claude.json") if not os.environ.get("CLAUDE_CONFIG_DIR") else _read_json(paths.CLAUDE_HOME / ".claude.json")
    oauth = bool((cj.get("oauthAccount") or {}).get("billingType"))
    out = {"api_sids": set(), "api_provider": None}
    try:                                         # 桌面端 API 模式（3P）的会话：不算订阅
        from . import claude_sync
        out["api_sids"] = claude_sync.api_session_ids()
        out["api_provider"] = "API 模式 · " + (claude_sync.api_info().get("provider") or "第三方")
    except Exception:
        pass
    if base:
        host = re.sub(r"^https?://", "", base).split("/")[0]
        return dict(out, provider=host, sub=0)
    if keyed:
        return dict(out, provider="Anthropic API", sub=0)
    return dict(out, provider="Claude 订阅" if oauth else "Anthropic", sub=1 if oauth else 0)


def _codex_ctx() -> dict:
    names = {}
    try:
        from . import _toml as tomllib
        cfg = tomllib.loads((paths.CODEX_HOME / "config.toml").read_text(encoding="utf-8"))
        for k, v in (cfg.get("model_providers") or {}).items():
            if isinstance(v, dict):
                names[k] = v.get("name") or k
    except Exception:
        pass
    mode = _read_json(paths.CODEX_HOME / "auth.json").get("auth_mode")
    return {"names": names, "chatgpt": mode == "chatgpt"}


def _codex_provider(pid: str | None, ctx: dict) -> tuple[str, int]:
    pid = pid or "openai"
    if pid == "openai":
        return ("ChatGPT 订阅", 1) if ctx["chatgpt"] else ("OpenAI API", 0)
    return (ctx["names"].get(pid) or pid, 0)


# ---------------- 文件身份 + 增量读 ----------------

def _fid(p: Path, st: os.stat_result) -> str:
    return f"{st.st_dev}-{st.st_ino}" if st.st_ino else "p:" + str(p).lower()


def claude_roots() -> list[tuple[Path, str]]:
    """Claude 的会话日志根目录：(目录, 'main' | '1p' | '3p')。Cowork（local-agent-mode-sessions）里自带一份 .claude/projects。"""
    out = [(paths.CLAUDE_HOME / "projects", "main")]
    try:
        from . import claude_sync
        bases = [(Path(c["path"]), "1p") for c in claude_sync.app_dir_candidates()] + [(Path(c["path"]), "3p") for c in claude_sync.api_dir_candidates()]
    except Exception:
        bases = []
    seen = set()
    for b, prof in bases:
        la = b / "local-agent-mode-sessions"
        if not la.is_dir():
            continue
        for pat in ("*/*/*/.claude/projects", "*/*/*/*/.claude/projects"):
            for d in la.glob(pat):
                k = str(d).lower()
                if k not in seen and d.is_dir():
                    seen.add(k)
                    out.append((d, prof))
    return out


def _files() -> list[tuple[str, Path, str]]:
    out = []
    for root, prof in claude_roots():
        if root.is_dir():
            out += [("claude", p, prof) for p in root.rglob("*.jsonl") if p.name != "audit.jsonl"]
    for sub in ("sessions", "archived_sessions"):
        d = paths.CODEX_HOME / sub
        if d.is_dir():
            # 按文件名（rollout-<开始时间>-…）排：原来的会话先读，续接 / 分叉出来的新文件重放的旧记录就记在原会话名下（和目录遍历顺序无关）
            out += [("codex", p, "") for p in sorted(d.rglob("rollout-*.jsonl"), key=lambda p: (p.name, str(p)))]
    return out


def _n(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _ts(s) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


COLS = ("id", "app", "src", "ts", "sid", "provider", "sub", "model", "inp", "cr", "cw5", "cw1h", "out", "reasoning", "tot", "agent", "fid",
        "stop", "prompt", "tier", "geo", "proj", "ver", *CPARTS, "priced", "est")
_UPD = ("inp", "cr", "cw5", "cw1h", "out", "reasoning", "tot", "stop", "prompt", "tier", "geo", "ver", *CPARTS, "priced", "est")
UPSERT = (f"INSERT INTO calls({', '.join(COLS)}) VALUES({','.join('?' * len(COLS))}) ON CONFLICT(id) DO UPDATE SET "
          + ", ".join(f"{k}=excluded.{k}" for k in _UPD)
          + ", model=CASE WHEN excluded.model<>'' THEN excluded.model ELSE calls.model END"
          + ", proj=CASE WHEN coalesce(excluded.proj,'')<>'' THEN excluded.proj ELSE calls.proj END"
          + ", ts=CASE WHEN excluded.ts>0 AND excluded.ts<calls.ts THEN excluded.ts ELSE calls.ts END"
          + " WHERE excluded.tot > calls.tot OR (excluded.tot = calls.tot AND (coalesce(calls.ver,0) < excluded.ver"
          + " OR coalesce(excluded.stop,0) > coalesce(calls.stop,0)))")


def mkrow(id_, app, src, ts, sid, prov, sub, model, t, reasoning, agent, fid, stop=0, prompt=None, tier="", geo="", proj="",
          pt=None, mults=None) -> tuple:
    """一条调用 → 库里的一行（费用按这条请求自己的提示长度 / 档位当场算好）。"""
    tk = dict(zip(TOK, t))
    prompt = prompt if prompt is not None else (t[0] + t[1] + t[2] + t[3])
    cp = cost_parts(tk, find_price(model, pt)[1] if pt is not None else None, (mults or {}).get(prov, 1.0), prompt, tier, geo)
    c5 = cp[:5] if cp else (None,) * 5
    return (id_, app, src, ts, sid, prov, sub, model, *t, reasoning, sum(t), agent, fid,
            stop, prompt, tier or "", geo or "", proj or "", SCHEMA, *c5, 1 if cp else 0, cp[5] if cp else 0)


def _claude_lines(lines, st: dict, fid: str, ctx: dict, agent: int, hours: set, rows: list, prof: str = "main", pt=None, mults=None):
    rids = st.setdefault("rid", {})
    for ln in lines:
        if '"assistant"' not in ln or '"usage"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u = m.get("usage") or {}
        mid, model = m.get("id"), m.get("model") or ""
        if not mid or not u or model in ("<synthetic>", "codex-import") or mid.startswith("msg_ccss"):   # msg_ccss：走模型路由的，代理那边记过了     # codex-import：本程序从 Codex 导进来的对话（0 token）
            continue
        rid = d.get("requestId") or rids.get(mid) or ""
        if rid:
            rids[mid] = rid
            if len(rids) > 400:
                for k in list(rids)[:200]:
                    rids.pop(k, None)
        agg = _n(u.get("cache_creation_input_tokens"))
        sp = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
        if sp:                                           # 拆分：5 分钟 / 1 小时；合计封顶到 cache_creation_input_tokens
            e5, e1 = _n(sp.get("ephemeral_5m_input_tokens")), _n(sp.get("ephemeral_1h_input_tokens"))
            tot_cw = agg if agg else e5 + e1
            c1h = min(e1, tot_cw)
            c5 = tot_cw - c1h
        else:                                            # 没拆分：全按 5 分钟
            c5, c1h = agg, 0
        t = (_n(u.get("input_tokens")), _n(u.get("cache_read_input_tokens")), c5, c1h, _n(u.get("output_tokens")))
        if not sum(t):
            continue
        ts = _ts(d.get("timestamp"))
        ep = d.get("entrypoint")
        app = "claude-desktop" if ep in ("claude-desktop", "local-agent") or prof != "main" else "claude-code"
        prov, sub = ctx["provider"], ctx["sub"]
        if prof == "3p" or (d.get("sessionId") and d.get("sessionId") in ctx.get("api_sids", ())):
            prov, sub = ctx.get("api_provider") or "API 模式", 0
        if model.startswith("ocx-"):
            prov, sub = "opencodex", 0
        th = ((u.get("output_tokens_details") or {}).get("thinking_tokens"))
        tier = "fast" if u.get("speed") == "fast" else ("priority" if u.get("service_tier") == "priority" else
                                                         ("batch" if u.get("service_tier") == "batch" else ""))
        geo = "us" if u.get("inference_geo") == "us" else ""
        rows.append(mkrow(f"cl:{mid}:{rid}", app, "log", ts, d.get("sessionId") or "", prov, sub, model, t, _n(th),
                          1 if (agent or d.get("isSidechain")) else 0, fid, stop=1 if m.get("stop_reason") else 0,
                          tier=tier, geo=geo, proj=d.get("cwd") or "", pt=pt, mults=mults))
        hours.add(int(ts // 3600))


def _cx_tokens(u: dict) -> tuple:
    """Codex：新输入 = input − 缓存读 − 缓存写；reasoning 本来就在 output 里，不另加。"""
    inp, ca, cw = _n(u.get("input_tokens")), _n(u.get("cached_input_tokens")), _n(u.get("cache_write_input_tokens"))
    ca = min(ca, inp)
    cw = min(cw, inp - ca)
    return (inp - ca - cw, ca, cw, 0, _n(u.get("output_tokens")))


_CX_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def _cx_tier(v) -> str:
    v = str(v or "").lower()
    return {"priority": "fast", "fast": "fast", "flex": "flex", "batch": "batch"}.get(v, "")


def _codex_lines(lines, st: dict, fid: str, ctx: dict, hours: set, rows: list, pt=None, mults=None):
    for ln in lines:
        if not any(k in ln for k in ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')):
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        typ, p = d.get("type"), d.get("payload") or {}
        if typ == "session_meta":
            st.setdefault("thread", p.get("id") or p.get("session_id"))
            st["prov"] = p.get("model_provider") or st.get("prov")
            st["cwd"] = p.get("cwd") or st.get("cwd")
            src = p.get("source")
            st["agent"] = 1 if (isinstance(src, dict) or (p.get("thread_source") not in (None, "user"))) else 0
            continue
        if typ == "turn_context":
            st["model"] = p.get("model") or st.get("model")
            st["cwd"] = p.get("cwd") or st.get("cwd")
            if "service_tier" in p:
                st["tier"] = _cx_tier(p.get("service_tier"))
            continue
        ts = _ts(d.get("timestamp"))
        if st.get("prov") == "cc-route":
            continue                                      # 走 CC Session Sync 模型路由的会话：用量由代理按真实上游模型记
        prov, sub = _codex_provider(st.get("prov"), ctx)
        thread = st.get("thread") or fid
        tier = _cx_tier(p.get("service_tier")) if p.get("service_tier") else st.get("tier", "")
        if typ == "token_usage_record":
            u = p.get("usage") or p.get("turn_token_usage") or {}
            t = _cx_tokens(u)
            st["rec_tot"] = _n((p.get("thread_token_usage") or {}).get("total_tokens"))
            rid = p.get("response_id")
            if not sum(t):
                continue
            key = f"cx:{rid}" if rid else f"cx:{p.get('thread_id') or thread}:r{st['rec_tot']}"
            rows.append(mkrow(key, "codex", "log", ts, p.get("thread_id") or thread, prov, sub, st.get("model") or "", t,
                              _n(u.get("reasoning_output_tokens")), st.get("agent", 0), fid, stop=1, prompt=_n(u.get("input_tokens")),
                              tier=tier, proj=st.get("cwd") or "", pt=pt, mults=mults))
            hours.add(int(ts // 3600))
            continue
        if typ == "event_msg" and p.get("type") == "token_count":
            info = p.get("info") or {}
            tot, last = info.get("total_token_usage") or {}, info.get("last_token_usage")
            T = _n(tot.get("total_tokens"))
            if not T:
                continue
            prev = st.get("tot")
            if prev is not None and T == prev:
                continue                                  # 累计没变：同一份快照又写了一次，算 0
            cur = {k: _n(tot.get(k)) for k in _CX_FIELDS}
            if prev is None or T < prev or not st.get("prev_u"):
                # 这个文件第一条（续接的会话：累计里含着以前的）或者累计变小（压缩 / 重启）：用本次 last，累计当新基线
                delta = {k: _n((last or {}).get(k)) for k in _CX_FIELDS} if last else (cur if prev is None else {})
            else:
                pu = st["prev_u"]
                delta = {k: max(0, cur[k] - _n(pu.get(k))) for k in _CX_FIELDS}
            st["tot"], st["prev_u"] = T, cur
            if T == st.get("rec_tot"):
                continue                                  # 这次 token_usage_record 已经算过
            t = _cx_tokens(delta)
            if not sum(t):
                continue
            prompt = _n((last or {}).get("input_tokens")) or _n(delta.get("input_tokens"))
            rows.append(mkrow(f"cx:tc:{d.get('timestamp') or ''}:{T}", "codex", "log", ts, thread, prov, sub, st.get("model") or "", t,
                              _n(delta.get("reasoning_output_tokens")), st.get("agent", 0), fid, stop=1, prompt=prompt,
                              tier=tier, proj=st.get("cwd") or "", pt=pt, mults=mults))
            hours.add(int(ts // 3600))


DSH_PROV = {"deepseek-account": "DeepSeek（账号登录）", "deepseek": "DeepSeek API"}


def _dsh_rows(L: dict, fid: str, hours: set, rows: list, pt=None, mults=None) -> None:
    """DeepSeek Harness 的一个会话 → 每次模型调用一行。dsh 的 inputTokens 不含缓存命中（缓存读 / 写另记），和 Claude 的口径一样。"""
    from . import dsh
    for ev in dsh.usage_of(L):
        t = (ev["input"], ev["cache_read"], ev["cache_write"], 0, ev["output"])
        if not sum(t):
            continue
        prov = ev["provider"] or "dsh"
        prov = DSH_PROV.get(prov, "dsh · " + prov)
        rows.append(mkrow(f"ds:{ev['sid']}:{ev['msg']}", "dsh", "log", ev["ts"], ev["sid"], prov, 0, ev["model"] or "", t,
                          ev["reasoning"], 0, fid, stop=1, proj=ev.get("cwd") or "", pt=pt, mults=mults))
        hours.add(int(ev["ts"] // 3600))


def _scan_dsh(c, known: dict, full: bool, pt, mults, hours: set) -> int:
    """dsh 的会话文件是 zstd 压缩的，不能从中间接着读：变了就整个解一遍（键会去重）。只读，从不写 dsh 的文件。"""
    from . import dsh
    n = 0
    for sf in dsh.session_files():
        p = sf["file"]
        if sf["version"] != dsh.FORMAT:
            continue
        try:
            stt = p.stat()
        except OSError:
            continue
        fid = _fid(p, stt)
        row = known.get(fid)
        if row and not full and row[3] == stt.st_size and row[4] == stt.st_mtime and row[1] == str(p):
            continue
        try:
            L = dsh.load(p)
        except Exception:
            continue                                   # 正在写 / 坏了：下次再看
        rows: list = []
        _dsh_rows(L, fid, hours, rows, pt, mults)
        if rows:
            c.executemany(UPSERT, rows)
        c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)", (fid, str(p), "dsh", stt.st_size, stt.st_mtime, stt.st_size, "{}"))
        n += 1
    return n


def _migrate(c: sqlite3.Connection) -> dict | None:
    """库是老版本（0.2.3 及以前）：把还在的文件从头重读一遍（新口径 + 每条的费用拆分）；已经删掉的文件留下的老记录不丢，
    按存着的 token 重算费用。Codex 的键改了，还在的文件对应的老记录先删掉（重读时按新键写回）。"""
    v = int(_meta(c, "schema", "1") or 1)
    if v >= SCHEMA:
        return None
    before = c.execute("SELECT count(*), coalesce(sum(tot),0) FROM calls").fetchone()
    gone = 0
    live = {}
    for fid, path in c.execute("SELECT fid, path FROM files").fetchall():
        live[fid] = Path(path).exists()
    for fid, ok in live.items():
        if ok:
            c.execute("DELETE FROM calls WHERE fid=? AND src='log' AND app='codex'", (fid,))
        else:
            gone += 1
    c.execute("DELETE FROM files")
    c.commit()
    return {"from": v, "before_n": before[0], "before_tok": before[1], "gone_files": gone}


def _dedupe_ridless(c: sqlite3.Connection) -> int:
    """同一个 message.id，有的行没 requestId（键 cl:<id>:）有的有：留有 requestId 那条，免得算两遍。"""
    n = 0
    for (i,) in c.execute("SELECT id FROM calls WHERE id LIKE 'cl:%:' ").fetchall():
        if c.execute("SELECT 1 FROM calls WHERE id > ? AND id < ? LIMIT 1", (i, i + "\uffff")).fetchone():
            c.execute("DELETE FROM calls WHERE id=?", (i,))
            n += 1
    return n


def scan(full: bool = False) -> dict:
    """扫一遍（增量）。返回这次读了多少文件、加了多少条。"""
    with _lock:
        STATE["scanning"] = True
        t0 = time.time()
        try:
            c = _conn()
            mig = _migrate(c)
            if mig:
                full = True
            known = {r[0]: r for r in c.execute("SELECT fid, path, kind, size, mtime, off, state FROM files")}
            cctx, xctx = _claude_ctx(), _codex_ctx()
            pt, mults = price_table(c), _mults(c)
            nfiles = added = 0
            hours: set = set()
            before = c.execute("SELECT count(*) FROM calls").fetchone()[0]
            for kind, p, prof in _files():
                try:
                    stt = p.stat()
                except OSError:
                    continue
                fid = _fid(p, stt)
                row = known.get(fid)
                off, state = 0, {}
                if row and not full:
                    if row[3] == stt.st_size and row[4] == stt.st_mtime and row[1] == str(p):
                        continue
                    off = row[5] if stt.st_size >= row[5] else 0      # 文件变短 = 被重写：从头读，键会去重
                    state = json.loads(row[6] or "{}") if off else {}
                with open(p, "rb") as f:
                    f.seek(off)
                    data = f.read()
                end = data.rfind(b"\n") + 1                            # 只读到最后一个完整行
                lines = data[:end].decode("utf-8", "replace").splitlines()
                rows: list = []
                if kind == "claude":
                    _claude_lines(lines, state, fid, cctx, 1 if "subagents" in p.parts else 0, hours, rows, prof, pt, mults)
                else:
                    _codex_lines(lines, state, fid, xctx, hours, rows, pt, mults)
                if rows:
                    c.executemany(UPSERT, rows)
                    if kind == "claude":
                        c.executemany("DELETE FROM calls WHERE id=?", [("cl:" + r[0].split(":")[1] + ":ccs",) for r in rows])
                c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                          (fid, str(p), kind, stt.st_size, stt.st_mtime, off + end, json.dumps(state, separators=(",", ":"))))
                nfiles += 1
            try:
                nfiles += _scan_dsh(c, known, full, pt, mults, hours)
                STATE.pop("dsh_error", None)
            except Exception as e:                     # dsh 读不了不影响 Claude / Codex 的统计
                STATE["dsh_error"] = f"{type(e).__name__}: {e}"
            _dedupe_ridless(c)
            c.commit()
            if mig or c.execute("SELECT 1 FROM calls WHERE coalesce(ver,0) < ? LIMIT 1", (SCHEMA,)).fetchone():
                _recost(c, only_old=True)
            if mig:
                after = c.execute("SELECT count(*), coalesce(sum(tot),0) FROM calls").fetchone()
                mig.update(after_n=after[0], after_tok=after[1], at=time.time())
                c.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA),))
                c.execute("INSERT OR REPLACE INTO meta VALUES('migrated', ?)", (json.dumps(mig),))
                STATE["migrated"] = mig
            c.commit()
            added = c.execute("SELECT count(*) FROM calls").fetchone()[0] - before
            if hours or full:
                _rollup(c, None if full else hours)
            c.close()
            STATE.update(last=time.time(), last_ms=int((time.time() - t0) * 1000), files=nfiles, added=added, error="", first_done=True)
            return {"files": nfiles, "added": added, "ms": STATE["last_ms"], **({"migrated": mig} if mig else {})}
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            STATE["scanning"] = False


def _recost(c: sqlite3.Connection, only_old: bool = False) -> int:
    """按现在的价格表把（全部 / 老口径的）记录的费用拆分重算一遍（改价格、改倍率、从 models.dev 更新后调）。"""
    pt, mults = price_table(c), _mults(c)
    q = "SELECT id, model, provider, inp, cr, cw5, cw1h, out, prompt, tier, geo FROM calls"
    q += " WHERE coalesce(pfix,0)=0"                       # 模型路由里给模型单独定的价：不跟价格表走
    if only_old:
        q += f" AND coalesce(ver,0) < {SCHEMA}"
    up = []
    for i, model, prov, *t, prompt, tier, geo in c.execute(q).fetchall():
        t = [v or 0 for v in t]
        prompt = prompt if prompt is not None else t[0] + t[1] + t[2] + t[3]
        cp = cost_parts(dict(zip(TOK, t)), find_price(model, pt)[1], mults.get(prov, 1.0), prompt, tier or "", geo or "")
        up.append((*(cp[:5] if cp else (None,) * 5), 1 if cp else 0, cp[5] if cp else 0, prompt, SCHEMA, i))
    c.executemany("UPDATE calls SET c_in=?, c_cr=?, c_cw5=?, c_cw1h=?, c_out=?, priced=?, est=?, prompt=?, ver=? WHERE id=?", up)
    c.commit()
    return len(up)


def record_proxy(app: str, provider: str, model: str, u: dict, price=None, ts: float | None = None, sid: str = "") -> str:
    """模型路由代理转发的一次请求 → calls 表一行（src=route）。
    price：None = 按价格表；"none" = 不计费（显示 —）；{inp, out, cr?, cw?}（美元 / 百万 token）= 这个供应商这个模型自己的价。"""
    import secrets as _s
    ts = ts or time.time()
    t = (_n(u.get("input")), _n(u.get("cache_read")), _n(u.get("cache_write")), 0, _n(u.get("output")))
    if not sum(t):
        return ""
    rid = f"rt:{int(ts * 1000)}:{_s.token_hex(4)}"
    with _lock:
        c = _conn()
        try:
            pt = price_table(c)
            row = list(mkrow(rid, app, "route", ts, sid, provider, 0, model, t, _n(u.get("reasoning")), 0, "route", stop=1, pt=pt, mults=_mults(c)))
            fix = 0
            if price == "none" or isinstance(price, dict):
                fix = 1
                pp = None
                if isinstance(price, dict):
                    pp = {"inp": price["inp"], "out": price["out"], "cr": price.get("cr", price["inp"]),
                          "cw5": price.get("cw", price["inp"]), "cw1h": price.get("cw", price["inp"])}
                cp = cost_parts(dict(zip(TOK, t)), pp, _mults(c).get(provider, 1.0), t[0] + t[1] + t[2] + t[3])
                i = COLS.index("c_in")
                row[i:i + 5] = list(cp[:5]) if cp else [None] * 5
                row[COLS.index("priced")] = 1 if cp else 0
                row[COLS.index("est")] = 0
            c.execute(UPSERT, row)
            if fix:
                c.execute("UPDATE calls SET pfix=1 WHERE id=?", (rid,))
            c.commit()
            _rollup(c, {int(ts // 3600)})
        finally:
            c.close()
    return rid


def recost() -> int:
    with _lock:
        c = _conn()
        try:
            return _recost(c)
        finally:
            c.close()


def _rollup(c: sqlite3.Connection, hours: set | None):
    """重算受影响小时的 hourly，再重算受影响日期的 daily（按本机时区，直接按每条的时间算日历日）。"""
    cols = "count(*), sum(inp), sum(cr), sum(cw5), sum(cw1h), sum(out)"
    if hours is None:
        c.execute("DELETE FROM hourly")
        c.execute(f"INSERT INTO hourly SELECT CAST(ts/3600 AS INT), app, provider, model, sub, {cols} FROM calls GROUP BY 1,2,3,4,5")
        c.execute("DELETE FROM daily")
        c.execute(f"INSERT INTO daily SELECT date(ts, 'unixepoch', 'localtime'), app, provider, model, sub, {cols} FROM calls GROUP BY 1,2,3,4,5")
        c.commit()
        return
    hs = sorted(hours)
    days = set()
    for i in range(0, len(hs), 400):
        part = hs[i:i + 400]
        q = ",".join("?" * len(part))
        c.execute(f"DELETE FROM hourly WHERE h IN ({q})", part)
        c.execute(f"INSERT INTO hourly SELECT CAST(ts/3600 AS INT) h, app, provider, model, sub, {cols} FROM calls "
                  f"WHERE CAST(ts/3600 AS INT) IN ({q}) GROUP BY 1,2,3,4,5", part)
        days |= {r[0] for r in c.execute(f"SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM calls WHERE CAST(ts/3600 AS INT) IN ({q})", part)}
    for d in days:
        c.execute("DELETE FROM daily WHERE d=?", (d,))
        c.execute(f"INSERT INTO daily SELECT date(ts, 'unixepoch', 'localtime'), app, provider, model, sub, {cols} FROM calls "
                  f"WHERE date(ts, 'unixepoch', 'localtime')=? GROUP BY 1,2,3,4,5", (d,))
    c.commit()


# ---------------- 价格 ----------------
_BUILTIN: dict | None = None
EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|xhigh|max|ultra|thinking|fast)$")


def builtin() -> dict:
    global _BUILTIN
    if _BUILTIN is None:
        try:
            _BUILTIN = json.loads((paths.PKG / "usage_prices.json").read_text(encoding="utf-8"))["models"]
        except Exception:
            _BUILTIN = {}
    return _BUILTIN


_MODS = ("lc", "fast", "flex", "batch", "geo_us", "verified", "ref")


def _row(v, model: str) -> dict:
    """内置表一条 → {inp,out,cr,cw5,cw1h, 以及 lc / fast / flex / geo_us / verified 这些修饰}。
    老格式是 [输入, 输出, 缓存读, 缓存写5m]；核对过官方价格页的是 {"p": [输入, 输出, 缓存读, 缓存写5m, 缓存写1h], ...}。"""
    mods = {}
    if isinstance(v, dict):
        mods = {k: v[k] for k in _MODS if k in v}
        v = v.get("p") or []
    i, o, cr, cw, c1 = (list(v) + [0, 0, 0, 0, None])[:5]
    claude = model.startswith("claude")
    return {"inp": i, "out": o, "cr": cr if cr else (i * 0.1 if claude else 0), "cw5": cw if cw else (i * 1.25 if claude else 0),
            "cw1h": c1 if c1 is not None else (i * 2 if claude else (cw or 0)), **mods}


def price_table(c: sqlite3.Connection | None = None) -> dict:
    """model → {inp,out,cr,cw5,cw1h,src,…修饰}；manual > models.dev > 内置。models.dev / 手填的条目沿用内置表里这个模型的
    长上下文 / fast 这些修饰（价格本身用它们的）。"""
    own = c is None
    c = c or _conn()
    t = {m: dict(_row(v, m), src="builtin") for m, v in builtin().items()}
    rows = c.execute("SELECT model, inp, out, cr, cw5, cw1h, src FROM prices ORDER BY CASE src WHEN 'modelsdev' THEN 0 ELSE 1 END").fetchall()
    for m, i, o, cr, cw5, cw1h, src in rows:
        base = t.get(m) or {}
        mods = {k: base[k] for k in ("lc", "fast", "flex", "batch", "geo_us") if k in base}
        t[m] = {"inp": i or 0, "out": o or 0, "cr": cr or 0, "cw5": cw5 or 0, "cw1h": cw1h if cw1h is not None else (cw5 or 0), "src": src, **mods}
    if own:
        c.close()
    return t


def norm(model: str) -> str:
    m = (model or "").lower().strip()
    m = m.replace("[1m]", "").split("/")[-1]
    m = re.sub(r"^(?:[a-z]{2,4}\.)?anthropic\.", "", m)          # Bedrock：us.anthropic.claude-…-v1:0
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    m = re.sub(r"@(\d{8})$", r"-\1", m)                           # Vertex：claude-…@20250929
    return m


def find_price(model: str, table: dict | None) -> tuple[str, dict] | tuple[None, None]:
    if table is None:
        return None, None
    memo = table.setdefault("\0memo", {})
    if model not in memo:
        memo[model] = _find_price(model, table)
    return memo[model]


def _find_price(model: str, table: dict) -> tuple[str, dict] | tuple[None, None]:
    m = norm(model)
    cands = [m]
    x = re.sub(r"-\d{8}$", "", m)
    cands.append(x)
    y = EFFORT_SUFFIX.sub("", x)
    cands.append(y)
    cands.append(re.sub(r"(\d)\.(\d)", r"\1-\2", y) if y.startswith("claude") else y)
    for k in cands:
        if k in table:
            return k, table[k]
    for k in cands:                                   # 表里是带日期的全名
        for t in table:
            if t != "\0memo" and t.startswith(k + "-") and re.fullmatch(r"\d{8}", t[len(k) + 1:]):
                return t, table[t]
    return None, None


def cost_parts(tokens: dict, p: dict | None, mult: float = 1.0, prompt: int | None = None, tier: str = "", geo: str = ""):
    """一条请求的费用拆分 (输入, 缓存读, 缓存写5m, 缓存写1h, 输出, 是否估算)；没价格 → None。
    prompt：这条请求的完整提示长度（新输入 + 缓存读 + 缓存写），长上下文档按它判断，整条请求都按高档算。"""
    if not p:
        return None
    mi = mo = float(mult)
    est = 0
    lc = p.get("lc")
    if lc and prompt is not None and prompt > lc[0]:
        mi *= lc[1]
        mo *= lc[2]
    if tier:
        x = p.get(tier if tier != "priority" else "fast")
        if x:
            mi *= x
            mo *= x
        else:
            est = 1                                   # 日志里是 fast / flex 之类，但价格表里没有这个倍率：按标准价算，标「估」
    if geo == "us" and p.get("geo_us"):
        mi *= p["geo_us"]
        mo *= p["geo_us"]
    r = (tokens["inp"] * p["inp"] * mi / 1e6, tokens["cr"] * p["cr"] * mi / 1e6, tokens["cw5"] * p["cw5"] * mi / 1e6,
         tokens["cw1h"] * p["cw1h"] * mi / 1e6, tokens["out"] * p["out"] * mo / 1e6)
    return (*r, est)


def cost(tokens: dict, p: dict | None, mult: float = 1.0, prompt: int | None = None, tier: str = "", geo: str = "") -> float | None:
    cp = cost_parts(tokens, p, mult, prompt, tier, geo)
    return None if cp is None else sum(cp[:5])


def set_price(model: str, v: dict | None):
    c = _conn()
    if v is None:
        c.execute("DELETE FROM prices WHERE model=? AND src='manual'", (model,))
    else:
        f = {k: float(v.get(k) or 0) for k in ("inp", "out", "cr", "cw5")}
        cw1h = float(v["cw1h"]) if v.get("cw1h") not in (None, "") else (f["inp"] * 2 if model.startswith("claude") else f["cw5"])
        c.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", (norm(model), f["inp"], f["out"], f["cr"], f["cw5"], cw1h, "manual", time.time()))
    c.commit()
    _recost(c)
    c.close()


def set_mult(provider: str, m: float | None):
    c = _conn()
    if m is None or m == 1:
        c.execute("DELETE FROM mult WHERE provider=?", (provider,))
    else:
        c.execute("INSERT OR REPLACE INTO mult VALUES(?,?)", (provider, float(m)))
    c.commit()
    _recost(c)
    c.close()


MODELS_DEV = "https://models.dev/api.json"


def refresh_models_dev(timeout: float = 20) -> dict:
    req = urllib.request.Request(MODELS_DEV, headers={"User-Agent": "CCSessionSync"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    pref = ["anthropic", "openai", "google", "xai", "deepseek", "moonshotai", "zhipuai", "alibaba", "mistral"]
    found: dict = {}
    for prov in pref + [k for k in data if k not in pref]:
        for mid, m in ((data.get(prov) or {}).get("models") or {}).items():
            cst = (m or {}).get("cost") or {}
            if not cst.get("input") and not cst.get("output"):
                continue
            k = norm(mid)
            if k in found:
                continue
            i = float(cst.get("input") or 0)
            cw = float(cst.get("cache_write") or 0)
            found[k] = (k, i, float(cst.get("output") or 0), float(cst.get("cache_read") or 0), cw,
                        i * 2 if k.startswith("claude") else cw, "modelsdev", time.time())
    c = _conn()
    manual = {r[0] for r in c.execute("SELECT model FROM prices WHERE src='manual'")}
    c.execute("DELETE FROM prices WHERE src='modelsdev'")
    c.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", [v for k, v in found.items() if k not in manual])
    c.execute("INSERT OR REPLACE INTO meta VALUES('modelsdev_at', ?)", (str(time.time()),))
    c.commit()
    _recost(c)
    c.close()
    return {"models": len(found)}


# ---------------- 查询 ----------------

def _where(f: dict, tcol: str = "ts") -> tuple[str, list]:
    w, a = [], []
    if f.get("app"):
        apps = {"claude": ("claude-code", "claude-desktop")}.get(f["app"], (f["app"],))
        w.append(f"app IN ({','.join('?' * len(apps))})")
        a += list(apps)
    for k in ("provider", "model", "proj"):
        if f.get(k):
            w.append(f"{k}=?")
            a.append(f[k])
    if f.get("from"):
        w.append(f"{tcol}>=?")
        a.append(float(f["from"]))
    if f.get("to"):
        w.append(f"{tcol}<?")
        a.append(float(f["to"]))
    return (" WHERE " + " AND ".join(w)) if w else "", a


def _mults(c) -> dict:
    return dict(c.execute("SELECT provider, m FROM mult").fetchall())


def _local_hour_ts(s: str) -> float:
    """'YYYY-MM-DD HH'（本机时区）→ 那个整点的时间戳。"""
    return time.mktime(time.strptime(s, "%Y-%m-%d %H"))


def summary(f: dict) -> dict:
    """按本机时区：小时 / 日历日的序列（含每种 token、费用拆分、按应用 / 模型分），按模型 / 供应商 / 项目的合计。
    费用是每条请求存好的拆分加起来（长上下文 / fast 档已经按每条算过），有没价格的部分就标 partial。"""
    c = _conn()
    w, a = _where(f)
    q = ("SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') hk, app, provider, model, sub, coalesce(proj,''), "
         "count(*), sum(inp), sum(cr), sum(cw5), sum(cw1h), sum(out), "
         "sum(coalesce(c_in,0)), sum(coalesce(c_cr,0)), sum(coalesce(c_cw5,0)), sum(coalesce(c_cw1h,0)), sum(coalesce(c_out,0)), "
         f"sum(CASE WHEN coalesce(priced,0)=0 THEN 1 ELSE 0 END), sum(coalesce(est,0)) FROM calls{w} GROUP BY 1,2,3,4,5,6")
    rows = c.execute(q, a).fetchall()
    c.close()
    Z = lambda: {"n": 0, **{x: 0 for x in TOK}, **{x: 0.0 for x in CPARTS}, "cost": 0.0, "unpriced": 0, "sub": 0, "est": 0, "app": set()}
    tot = Z()
    by_h: dict = {}
    by_d: dict = {}
    by_model: dict = {}
    by_prov: dict = {}
    by_proj: dict = {}
    missing: dict = {}

    def add(e, n, tk, cp, unp, sub, est, app):
        e["n"] += n
        for x in TOK:
            e[x] += tk[x]
        for x in CPARTS:
            e[x] += cp[x]
        e["cost"] += sum(cp.values())
        e["unpriced"] += unp
        e["sub"] += n if sub else 0
        e["est"] += est
        e["app"].add(app)

    for hk, app, prov, model, sub, proj, n, *rest in rows:
        tk = dict(zip(TOK, [v or 0 for v in rest[:5]]))
        cp = dict(zip(CPARTS, [v or 0.0 for v in rest[5:10]]))
        unp, est = rest[10] or 0, rest[11] or 0
        for d, key in ((by_model, model), (by_prov, prov), (by_proj, proj)):
            add(d.setdefault(key, Z()), n, tk, cp, unp, sub, est, app)
        add(tot, n, tk, cp, unp, sub, est, app)
        for d, key in ((by_h, hk), (by_d, hk[:10])):
            e = d.setdefault(key, {"n": 0, "tok": 0, "cost": 0.0, **{x: 0 for x in TOK}, **{x: 0.0 for x in CPARTS},
                                   "unpriced": 0, "apps": {}, "models": {}})
            e["n"] += n
            e["tok"] += sum(tk.values())
            e["cost"] += sum(cp.values())
            e["unpriced"] += unp
            for x in TOK:
                e[x] += tk[x]
            for x in CPARTS:
                e[x] += cp[x]
            if d is by_d:
                s = e.setdefault("projs", {}).setdefault(proj, [0, 0, 0.0])
                s[0] += n
                s[1] += sum(tk.values())
                s[2] += sum(cp.values())
            for sub_d, k2 in ((e["apps"], app if app in ("codex", "dsh") else "claude"), (e["models"], model)):
                s = sub_d.setdefault(k2, [0, 0, 0.0])
                s[0] += n
                s[1] += sum(tk.values())
                s[2] += sum(cp.values())
        if unp:
            missing[model] = missing.get(model, 0) + unp
    inp_all = tot["inp"] + tot["cr"] + tot["cw5"] + tot["cw1h"]
    r6 = lambda v: round(v, 6)
    fin = lambda d: [{"key": k, **{x: v[x] for x in ("n", *TOK, "unpriced", "sub", "est")}, **{x: r6(v[x]) for x in CPARTS},
                      "cost": r6(v["cost"]), "apps": sorted(v["app"])} for k, v in sorted(d.items(), key=lambda kv: -kv[1]["n"])]
    top_proj = [k for k, _ in sorted(by_proj.items(), key=lambda kv: -kv[1]["n"])[:8]]
    for v in by_d.values():                  # 按天的项目分布：只留前 8 个项目，其余并进「其他」（项目可能上千个）
        pj, other = v.pop("projs", {}), [0, 0, 0.0]
        v["projs_"] = {k: pj[k] for k in top_proj if k in pj}
        for k, s in pj.items():
            if k not in v["projs_"]:
                other = [other[0] + s[0], other[1] + s[1], other[2] + s[2]]
        if other[0]:
            v["projs_"]["\x00other"] = other
    ser = lambda d, kf: [{"h": kf(k), "d": k[:10], **{x: (r6(v[x]) if isinstance(v[x], float) else v[x]) for x in v if x not in ("apps", "models", "projs_")},
                          **({"projs": {p2: [s[0], s[1], r6(s[2])] for p2, s in v["projs_"].items()}} if "projs_" in v else {}),
                          "apps": {a2: [s[0], s[1], r6(s[2])] for a2, s in v["apps"].items()},
                          "models": {m2: [s[0], s[1], r6(s[2])] for m2, s in v["models"].items()}} for k, v in sorted(d.items())]
    return {"totals": {**{x: tot[x] for x in ("n", *TOK)}, **{x: r6(tot[x]) for x in CPARTS}, "real": inp_all + tot["out"],
                       "hit": (tot["cr"] / inp_all) if inp_all else None, "cost": r6(tot["cost"]),
                       "unpriced": tot["unpriced"], "sub": tot["sub"], "est": tot["est"],
                       "partial": bool(tot["unpriced"]) and tot["unpriced"] < tot["n"]},
            "series": ser(by_h, _local_hour_ts), "days": ser(by_d, lambda k: time.mktime(time.strptime(k, "%Y-%m-%d"))),
            "models": fin(by_model), "providers": fin(by_prov), "projects": fin(by_proj),
            "missing": [{"model": m, "n": n} for m, n in sorted(missing.items(), key=lambda kv: -kv[1])],
            "options": options(), "tz": time.strftime("%z")}


def options() -> dict:
    c = _conn()
    r = {k: [x[0] for x in c.execute(f"SELECT DISTINCT {k} FROM calls WHERE coalesce({k},'')<>'' ORDER BY 1")] for k in ("app", "provider", "model")}
    span = c.execute("SELECT min(ts), max(ts) FROM calls").fetchone()
    c.close()
    r["span"] = list(span)
    return r


def logs(f: dict, offset: int = 0, limit: int = 100) -> dict:
    c = _conn()
    w, a = _where(f)
    total = c.execute(f"SELECT count(*) FROM calls{w}", a).fetchone()[0]
    out = []
    for r in c.execute(f"SELECT id, app, src, ts, sid, provider, sub, model, inp, cr, cw5, cw1h, out, reasoning, agent, "
                       f"c_in, c_cr, c_cw5, c_cw1h, c_out, priced, est, prompt, tier, geo, proj FROM calls{w} "
                       f"ORDER BY ts DESC LIMIT ? OFFSET ?", a + [int(limit), int(offset)]):
        tk = dict(zip(TOK, r[8:13]))
        cp = dict(zip(CPARTS, r[15:20]))
        out.append({"id": r[0], "app": r[1], "src": r[2], "ts": r[3], "sid": r[4], "provider": r[5], "sub": r[6], "model": r[7], **tk,
                    "reasoning": r[13], "agent": r[14], **cp, "cost": sum(v or 0 for v in cp.values()) if r[20] else None,
                    "est": r[21], "prompt": r[22], "tier": r[23], "geo": r[24], "proj": r[25]})
    c.close()
    return {"total": total, "rows": out}


def prices_view() -> dict:
    c = _conn()
    pt = price_table(c)
    used = [r[0] for r in c.execute("SELECT model FROM calls GROUP BY model ORDER BY count(*) DESC")]
    mults = _mults(c)
    provs = [r[0] for r in c.execute("SELECT DISTINCT provider FROM calls")]
    at = c.execute("SELECT v FROM meta WHERE k='modelsdev_at'").fetchone()
    c.close()
    rows = []
    for m in used:
        k, p = find_price(m, pt)
        rows.append({"model": m, "match": k, **(p or {}), "priced": bool(p)})
    return {"used": rows, "count": sum(1 for k in pt if k != "\0memo"), "mult": [{"provider": p, "m": mults.get(p, 1.0)} for p in provs],
            "modelsdev_at": float(at[0]) if at else None}


# ---------------- 外部来源 ----------------

def _ping(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.8) as r:
            return r.status < 500
    except Exception as e:
        return getattr(e, "code", 0) in (401, 403, 404)


def _port_running(port):
    from .modelcfg import port_running        # 共用后台测端口 + 30 秒缓存（Windows 上同步测一个关着的端口要 ~1 秒）
    return port_running(port)


def proxies() -> list[dict]:
    """检测 opencodex / magpie 本地代理（只看端口通不通；不读它们的令牌文件）。"""
    ocx_port = (_read_json(Path.home() / ".opencodex" / "config.json").get("port")) or 10100
    out = []
    for name, port, cfg in (("opencodex", int(ocx_port), Path.home() / ".opencodex"), ("magpie", 3425, None)):
        out.append({"name": name, "port": port, "configured": bool(cfg and cfg.exists()), "running": _port_running(port)})
    return out


def ccswitch_db() -> Path:
    return Path.home() / ".cc-switch" / "cc-switch.db"


def import_ccswitch(retries: int = 6) -> dict:
    """一次性导入 CC Switch 的历史（只读打开；被它锁着就等一会儿再试）。
    CC Switch 的 Claude 行按 message.id 记（session:msg_…），和我们扫到的同一条就跳过；Codex 按线程跳过我们已经扫到的线程；
    它的代理（proxy）行和会话日志是同一批调用的两份记录，不导入。它记的费用不用，按我们的价格表重算。
    这样能补回 Claude 已经清理掉的旧会话文件里的用量。"""
    src = ccswitch_db()
    if not src.exists():
        raise FileNotFoundError("没找到 CC Switch 的数据库")
    c = _conn()
    mids = {r[0].split(":")[1] for r in c.execute("SELECT id FROM calls WHERE src='log' AND id LIKE 'cl:%'")}
    threads = {r[0] for r in c.execute("SELECT DISTINCT sid FROM calls WHERE src='log' AND app='codex'")}
    last_err = None
    for i in range(retries):
        try:
            s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5)
            cols = [r[1] for r in s.execute("PRAGMA table_info(proxy_request_logs)")]
            sem = "input_token_semantics" if "input_token_semantics" in cols else "0"
            ds = "data_source" if "data_source" in cols else "'proxy'"
            prov_names = {}
            try:
                prov_names = {(r[0], r[1]): r[2] for r in s.execute("SELECT id, app_type, name FROM providers")}
            except sqlite3.Error:
                pass
            q = (f"SELECT request_id, app_type, provider_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                 f"{sem}, created_at, session_id, {ds} FROM proxy_request_logs WHERE status_code BETWEEN 200 AND 299")
            rows, hours, skipped = [], set(), 0
            cctx = _claude_ctx()
            pt, mults = price_table(c), _mults(c)
            for rid, app, pid, model, inp, out, cr, cc, semv, ts, sid, dsrc in s.execute(q):
                rid = str(rid or "")
                if dsrc == "proxy":
                    skipped += 1
                    continue
                if app == "claude":
                    mid = rid.split(":", 1)[1] if rid.startswith("session:") else rid
                    if mid in mids:
                        skipped += 1
                        continue
                    key = f"cl:{mid}:ccs"
                elif app == "codex":
                    parts = rid.split(":")
                    th = parts[2] if len(parts) >= 4 else (sid or "")
                    if th in threads:
                        skipped += 1
                        continue
                    key, sid = f"ccs:{rid}", th or sid
                else:
                    key = f"ccs:{rid}"
                ts = float(ts or 0)
                if ts > 1e12:
                    ts /= 1000
                inp, cr, cc, out = _n(inp), _n(cr), _n(cc), _n(out)
                if app in ("codex", "gemini", "grokbuild"):          # 口径同 cc-switch sql_helpers::fresh_input_sql
                    if semv == 1 and inp >= cr + cc:
                        inp -= cr + cc
                    elif semv == 0 and inp >= cr:
                        inp -= cr
                t = (inp, cr, cc, 0, out)
                if not sum(t):
                    continue
                if app == "claude":
                    pname, sub = cctx["provider"], cctx["sub"]
                    if pid not in ("default", "claude-official", "", None):
                        pname, sub = prov_names.get((pid, app)) or pid, 0
                else:
                    pname, sub = prov_names.get((pid, app)) or pid or "", 0
                rows.append(mkrow(key, "claude-code" if app == "claude" else app, "ccswitch", ts, sid or "",
                                  pname, sub, model or "", t, 0, 0, "ccswitch", stop=1, pt=pt, mults=mults))
                hours.add(int(ts // 3600))
            s.close()
            c.executemany(UPSERT, rows)
            c.execute("INSERT OR REPLACE INTO meta VALUES('ccswitch_import', ?)",
                      (json.dumps({"at": time.time(), "rows": len(rows), "skipped": skipped}),))
            c.commit()
            _rollup(c, hours)
            c.close()
            return {"imported": len(rows), "skipped": skipped}
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    c.close()
    raise RuntimeError(f"CC Switch 数据库一直被占用：{last_err}")


def status() -> dict:
    c = _conn()
    n = c.execute("SELECT src, app, count(*), min(ts), max(ts) FROM calls GROUP BY 1,2").fetchall()
    nf = c.execute("SELECT kind, count(*) FROM files GROUP BY 1").fetchall()
    imp = c.execute("SELECT v FROM meta WHERE k='ccswitch_import'").fetchone()
    c.close()
    return {**STATE, "db": str(DB_PATH), "sources": [{"src": s, "app": a, "n": k, "from": f, "to": t} for s, a, k, f, t in n],
            "files_known": dict(nf), "claude_dir": str(paths.CLAUDE_HOME / "projects"), "codex_dir": str(paths.CODEX_HOME),
            "proxies": proxies(), "ccswitch": {"exists": ccswitch_db().exists(), "imported": json.loads(imp[0]) if imp else None}}


_started = False


def start_background(interval: float = 60):
    """第一次全量扫描放后台，之后每分钟增量。"""
    global _started
    if _started or os.environ.get("CC_SESSIONSYNC_NO_USAGE"):
        return
    _started = True

    def loop():
        time.sleep(3)
        while True:
            try:
                scan()
            except Exception:
                pass
            time.sleep(interval)
    threading.Thread(target=loop, name="usage-scan", daemon=True).start()

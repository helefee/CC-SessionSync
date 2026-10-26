# -*- coding: utf-8 -*-
"""模型路由（0.3.0）：本机协议转换代理，让 Codex 用 Claude（Anthropic API）、Claude Code 用 OpenAI 的模型，或接任意兼容中转。

- 只用 API key（官方 / 中转 / 自建），**不支持**也不会去复用 Claude / ChatGPT 的订阅登录（OAuth）。
- 代理只听 127.0.0.1，端口第一次随机选好后固定（可改），每个请求都要带本机口令（ccss-local-…，存在凭据管理器里）。
- 供应商（照 CLIProxyAPI 的思路，自己实现）：名字、协议（Anthropic Messages / OpenAI Responses / OpenAI Chat Completions）、
  Base URL、多个 key（轮询 / 按顺序用满再换、权重、失败自动换下一个并冷却）、自定义请求头（"$X" = 照抄客户端的 X 头）、
  HTTP 代理（空 = 系统代理，direct = 直连）、模型列表 + 别名（同一个别名可以对应多个上游模型 / 供应商，轮流用、失败换下一个）、
  排除的模型（通配符）、前缀（prefix/model 只走这一家）、每个模型的价格（自定义供应商可填，或标「—」）。
- 客户端：Claude Code 的 opus / sonnet / haiku 槽位 → 别名；Codex 的 profile（CODEX_HOME/cc-route.config.toml）。
- 配置 Codex / Claude Code：先备份，只加 / 删自己的那块；撤销时文件没被别人改过就按字节还原。
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import keystore, paths, xproto

STORE = paths.HOME / "route.json"
TOKEN_KEY = "route-token"
PROTOCOLS = ("anthropic", "responses", "chat")
PROTO_NAME = {"anthropic": "Anthropic Messages", "responses": "OpenAI Responses", "chat": "OpenAI Chat Completions"}
PRESETS = {
    "anthropic": {"name": "Anthropic 官方", "protocol": "anthropic", "base_url": "https://api.anthropic.com"},
    "openai": {"name": "OpenAI 官方", "protocol": "responses", "base_url": "https://api.openai.com/v1"},
    "gemini": {"name": "Gemini（OpenAI 兼容接口）", "protocol": "chat", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "custom": {"name": "自定义供应商", "protocol": "chat", "base_url": ""},
}
SLOTS = ("opus", "sonnet", "haiku", "default")
CODEX_PROFILE = "cc-route"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RETRY_STATUS = {401, 403, 408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529}
_lock = threading.RLock()


# ================================================================ 配置

def _default() -> dict:
    return {"version": 1, "port": 0, "autostart": True, "strategy": "round-robin", "request_retry": 1, "max_retry_wait": 20,
            "proxy_url": "", "providers": [], "clients": {"claude": {"slots": {s: "" for s in SLOTS}},
                                                          "codex": {"model": "", "effort": "medium", "wire": "responses", "context_window": 0}},
            "applied": {}}


def load() -> dict:
    d = _default()
    try:
        x = json.loads(STORE.read_text(encoding="utf-8"))
        if isinstance(x, dict):
            d.update(x)
    except (OSError, ValueError):
        pass
    d["clients"].setdefault("claude", {"slots": {}})
    d["clients"]["claude"].setdefault("slots", {})
    d["clients"].setdefault("codex", {})
    return d


def save(d: dict):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STORE)
    _invalidate()


_CACHE = {"cfg": None, "at": 0.0}


def _invalidate():
    _CACHE["cfg"] = None


def cfg() -> dict:
    c = _CACHE["cfg"]
    if c is None or time.time() - _CACHE["at"] > 5:
        c = _CACHE["cfg"] = load()
        _CACHE["at"] = time.time()
    return c


def _slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:24] or "p"
    if not ID_RE.match(base):
        base = "p-" + base
    s, i = base, 2
    while s in taken:
        s = f"{base}-{i}"
        i += 1
    return s


def _wild(pat: str, s: str) -> bool:
    return fnmatch.fnmatchcase((s or "").lower(), (pat or "").lower())


def _clean_url(u: str) -> str:
    u = (u or "").strip().rstrip("/")
    if u and not re.match(r"^https?://[^\s\"']+$", u):
        raise ValueError("Base URL 要以 http:// 或 https:// 开头")
    return u


def _clean_proxy(u: str) -> str:
    u = (u or "").strip()
    if u in ("", "direct", "none"):
        return u
    if not re.match(r"^https?://[^\s\"']+$", u):
        raise ValueError("代理只支持 http:// / https://（或填 direct = 直连、留空 = 用系统代理）")
    return u


def _clean_price(p):
    if p in (None, "", "auto"):
        return None
    if p == "none":
        return "none"
    if isinstance(p, dict):
        out = {}
        for k in ("inp", "out", "cr", "cw"):
            v = p.get(k)
            if v not in (None, ""):
                v = float(v)
                if v < 0 or v > 10000:
                    raise ValueError("价格不对（每百万 token 的美元数）")
                out[k] = v
        if "inp" not in out or "out" not in out:
            raise ValueError("自定义价格至少要填输入和输出")
        return out
    raise ValueError("价格格式不对")


def clean_provider(p: dict, taken: set) -> dict:
    name = str(p.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("供应商要有名字")
    proto = p.get("protocol")
    if proto not in PROTOCOLS:
        raise ValueError("协议只能是 Anthropic Messages / OpenAI Responses / OpenAI Chat Completions")
    url = _clean_url(p.get("base_url") or PRESETS.get(p.get("preset") or "", {}).get("base_url") or "")
    if not url:
        raise ValueError("要填 Base URL")
    pid = p.get("id") or _slug(name, taken)
    if not ID_RE.match(pid):
        raise ValueError("id 不对")
    headers = {}
    for k, v in (p.get("headers") or {}).items():
        k = str(k).strip()
        if not re.match(r"^[A-Za-z0-9-]{1,64}$", k):
            raise ValueError(f"请求头名字不对：{k}")
        if k.lower() in ("authorization", "x-api-key", "host", "content-length"):
            raise ValueError(f"{k} 由程序自己设，不能在这里填")
        if any(c in str(v) for c in "\r\n"):
            raise ValueError("请求头的值不能换行")
        headers[k] = str(v)[:500]
    models, seen = [], set()
    for m in p.get("models") or []:
        n = str(m.get("name") or "").strip()
        if not n:
            continue
        a = str(m.get("alias") or "").strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$", n) or (a and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$", a)):
            raise ValueError(f"模型名 / 别名不对：{n} {a}")
        if (n, a) in seen:
            continue
        seen.add((n, a))
        models.append({"name": n, "alias": a, "price": _clean_price(m.get("price"))})
    keys = []
    for i, k in enumerate(p.get("keys") or []):
        if not k.get("name"):
            continue
        keys.append({"id": k.get("id") or f"k{i + 1}", "name": keystore.check_name(k["name"]), "label": str(k.get("label") or "")[:40],
                     "weight": max(1, min(100, int(k.get("weight") or 1))), "proxy_url": _clean_proxy(k.get("proxy_url") or ""),
                     "disabled": bool(k.get("disabled"))})
    return {"id": pid, "name": name, "protocol": proto, "preset": p.get("preset") or "custom", "base_url": url, "headers": headers,
            "proxy_url": _clean_proxy(p.get("proxy_url") or ""), "prefix": re.sub(r"[^A-Za-z0-9._-]", "", str(p.get("prefix") or ""))[:24],
            "keys": keys, "models": models, "excluded": [str(x).strip() for x in p.get("excluded") or [] if str(x).strip()][:50],
            "disabled": bool(p.get("disabled")), "max_tokens": max(1024, min(1_000_000, int(p.get("max_tokens") or 32000))),
            "thinking": p.get("thinking") if p.get("thinking") in ("adaptive", "budget", "off") else "adaptive",
            "reasoning": p.get("reasoning") if p.get("reasoning") in ("auto", "on", "off") else "auto",
            "effort": p.get("effort") if p.get("effort") in ("", "minimal", "low", "medium", "high", "xhigh") else "",
            "cache": p.get("cache", True) is not False, "system_role": p.get("system_role") if p.get("system_role") in ("system", "developer") else ""}


def _key_name(pid: str, n: int) -> str:
    return keystore.check_name(f"rt-{pid}-{n}")


def upsert_provider(p: dict, new_keys: list | None = None) -> dict:
    """保存一个供应商。new_keys：[{secret, label?, weight?, proxy_url?}] 新加的 key（明文只在这里过一下就进凭据管理器）。
    已有的 key 用 p["keys"] 里的条目（只有名字，没有明文）；从 p["keys"] 里去掉的 key 会从凭据管理器删掉。"""
    with _lock:
        d = load()
        old = next((x for x in d["providers"] if x["id"] == p.get("id")), None) if p.get("id") else None
        taken = {x["id"] for x in d["providers"] if x is not old}
        keys = [k for k in (p.get("keys") or []) if old and any(k.get("name") == o["name"] for o in old["keys"])]
        q = clean_provider(dict(p, keys=keys), taken)
        used = {k["name"] for k in q["keys"]}
        n = 1
        for nk in new_keys or []:
            sec = (nk.get("secret") or "").strip()
            if not sec:
                continue
            while _key_name(q["id"], n) in used or (keystore.get(_key_name(q["id"], n)) and not old):
                n += 1
            kn = _key_name(q["id"], n)
            keystore.put(kn, sec, f"CC Session Sync 模型路由 · {q['name']}")
            used.add(kn)
            q["keys"].append({"id": f"k{n}", "name": kn, "label": str(nk.get("label") or "")[:40], "weight": max(1, min(100, int(nk.get("weight") or 1))),
                              "proxy_url": _clean_proxy(nk.get("proxy_url") or ""), "disabled": False})
        if old:
            for k in old["keys"]:
                if k["name"] not in used:
                    keystore.delete(k["name"])
            d["providers"] = [q if x is old else x for x in d["providers"]]
        else:
            d["providers"].append(q)
        save(d)
        return view_provider(q)


def delete_provider(pid: str) -> dict:
    with _lock:
        d = load()
        p = next((x for x in d["providers"] if x["id"] == pid), None)
        if not p:
            raise ValueError("没有这个供应商")
        for k in p["keys"]:
            keystore.delete(k["name"])
        d["providers"] = [x for x in d["providers"] if x["id"] != pid]
        save(d)
        return {"ok": True}


def set_clients(c: dict) -> dict:
    with _lock:
        d = load()
        sl = (c.get("claude") or {}).get("slots") or {}
        d["clients"]["claude"]["slots"] = {s: str(sl.get(s) or "").strip()[:128] for s in SLOTS}
        cx = c.get("codex") or {}
        d["clients"]["codex"] = {"model": str(cx.get("model") or "").strip()[:128],
                                 "effort": cx.get("effort") if cx.get("effort") in ("minimal", "low", "medium", "high", "xhigh") else "medium",
                                 "wire": "chat" if cx.get("wire") == "chat" else "responses",
                                 "context_window": max(0, min(10_000_000, int(cx.get("context_window") or 0)))}
        for k in ("strategy",):
            if c.get(k) in ("round-robin", "fill-first"):
                d[k] = c[k]
        if "request_retry" in c:
            d["request_retry"] = max(0, min(5, int(c["request_retry"])))
        if "proxy_url" in c:
            d["proxy_url"] = _clean_proxy(c["proxy_url"])
        if "autostart" in c:
            d["autostart"] = bool(c["autostart"])
        if c.get("port") not in (None, ""):
            pt = int(c["port"])
            if pt and not (1024 <= pt <= 65535):
                raise ValueError("端口要在 1024–65535 之间")
            d["port"] = pt
        save(d)
        return {"ok": True}


def view_provider(p: dict) -> dict:
    v = json.loads(json.dumps(p))
    for k in v["keys"]:
        k["masked"] = keystore.mask(keystore.get(k["name"]))
        k["cooling"] = max(0, int(_COOL.get(k["name"], 0) - time.time()))
        k.update(_KSTAT.get(k["name"], {}))
    return v


# ================================================================ 模型解析 / key 轮换

_RR: dict = {}
_COOL: dict = {}          # key 名 → 冷却到什么时候
_KSTAT: dict = {}         # key 名 → {ok, fail, last_status, last_at}


def resolve(model: str, d: dict | None = None) -> list:
    """客户端请求的模型名 → [(供应商, 上游模型名, 模型条目)]（同一个别名多个上游：按轮询转一下起点）。"""
    d = d or cfg()
    m = (model or "").strip()
    provs = [p for p in d["providers"] if not p.get("disabled") and any(not k.get("disabled") for k in p["keys"])]
    if "/" in m:
        pre, rest = m.split("/", 1)
        pp = [p for p in provs if p.get("prefix") and p["prefix"].lower() == pre.lower()]
        if pp:
            provs, m = pp, rest
    exact, catch = [], []
    for p in provs:
        if p["models"]:
            for e in p["models"]:
                if (e["alias"] and e["alias"].lower() == m.lower()) or e["name"].lower() == m.lower():
                    if not any(_wild(x, e["name"]) for x in p.get("excluded") or []):
                        exact.append((p, e["name"], e))
        elif not any(_wild(x, m) for x in p.get("excluded") or []):
            catch.append((p, m, None))
    out = exact or catch
    if len(out) > 1:
        i = _RR.get(("alias", m.lower()), 0)
        _RR[("alias", m.lower())] = i + 1
        i %= len(out)
        out = out[i:] + out[:i]
    return out


def client_model(client: str, model: str, d: dict | None = None) -> str:
    """Claude Code 的模型名（claude-opus-…）按槽位换成别名；能直接解析的（用户在 /model 里输了别名）照原样。"""
    d = d or cfg()
    if client == "claude":
        if resolve(model, d) and any(e for _, _, e in resolve(model, d)):
            return model
        sl = d["clients"]["claude"].get("slots") or {}
        low = (model or "").lower()
        for s in ("opus", "sonnet", "haiku"):
            if s in low and sl.get(s):
                return sl[s]
        if sl.get("default"):
            return sl["default"]
        return model
    cx = d["clients"].get("codex") or {}
    if not resolve(model, d) and cx.get("model"):
        return cx["model"]
    return model


def pick_keys(p: dict, d: dict) -> list:
    """按策略排好这次要试的 key（冷却中的放最后，全在冷却就按最快解冻的顺序）。"""
    ks = [k for k in p["keys"] if not k.get("disabled")]
    if not ks:
        return []
    now = time.time()
    if d.get("strategy") == "fill-first":
        order = list(ks)
    else:
        wl = [k for k in ks for _ in range(k.get("weight") or 1)]
        i = _RR.get(("key", p["id"]), 0)
        _RR[("key", p["id"])] = i + 1
        first = wl[i % len(wl)]
        j = ks.index(first)
        order = ks[j:] + ks[:j]
    ready = [k for k in order if _COOL.get(k["name"], 0) <= now]
    cool = sorted([k for k in order if _COOL.get(k["name"], 0) > now], key=lambda k: _COOL[k["name"]])
    return ready + cool


def cool(kname: str, status, retry_after=None):
    if status in (401, 403):
        sec = 300
    elif status == 429:
        sec = min(600, float(retry_after)) if retry_after else 30
    else:
        sec = 10
    _COOL[kname] = time.time() + sec


def _kstat(kname: str, ok: bool, status):
    s = _KSTAT.setdefault(kname, {"ok": 0, "fail": 0})
    s["ok" if ok else "fail"] += 1
    s["last_status"], s["last_at"] = status, time.time()


# ================================================================ 本机口令

_TOKEN = {"v": None}


def token(create: bool = True) -> str | None:
    if _TOKEN["v"]:
        return _TOKEN["v"]
    v = keystore.get(TOKEN_KEY)
    if not v and create:
        v = "ccss-local-" + secrets.token_urlsafe(24)
        keystore.put(TOKEN_KEY, v, "CC Session Sync 模型路由：本机代理口令（只在 127.0.0.1 上有用）")
    _TOKEN["v"] = v
    return v


def rotate_token() -> dict:
    keystore.put(TOKEN_KEY, "ccss-local-" + secrets.token_urlsafe(24), "CC Session Sync 模型路由：本机代理口令")
    _TOKEN["v"] = None
    token()
    return {"ok": True, "note": "口令换了：Codex 下次请求会自动取新口令；Claude Code 要重新「应用」一次（settings.json 里的口令要换）"}


# ================================================================ 上游请求

def _opener(proxy: str):
    if proxy in ("direct", "none"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()          # 环境变量 / Windows 系统代理


def endpoint(p: dict, kind: str = "gen") -> str:
    b = p["base_url"].rstrip("/")
    if p["protocol"] == "anthropic":
        root = b[:-3] if b.endswith("/v1") else b
        return root + {"gen": "/v1/messages", "count": "/v1/messages/count_tokens", "models": "/v1/models"}[kind]
    if kind == "models":
        return b + "/models"
    return b + ("/responses" if p["protocol"] == "responses" else "/chat/completions")


def _ua() -> str:
    from . import __version__
    return f"CCSessionSync/{__version__} (model-route)"


def up_headers(p: dict, secret: str, client_headers=None, beta: str | None = None) -> dict:
    h = {"content-type": "application/json", "accept-encoding": "identity", "user-agent": _ua()}
    if p["protocol"] == "anthropic":
        h["x-api-key"] = secret
        h["anthropic-version"] = (client_headers or {}).get("anthropic-version") or "2023-06-01"
        if beta:
            h["anthropic-beta"] = beta
    else:
        h["authorization"] = "Bearer " + secret
    for k, v in (p.get("headers") or {}).items():
        if v.startswith("$"):
            cv = (client_headers or {}).get(v[1:].lower())
            if cv:
                h[k] = cv
        else:
            h[k] = v
    return h


def http(p: dict, k: dict, method: str, url: str, headers: dict, body: bytes | None, timeout: float = 300):
    """返回 urllib 的响应（调用方负责关）；HTTP 错误抛 urllib.error.HTTPError。"""
    proxy = k.get("proxy_url") or p.get("proxy_url") or cfg().get("proxy_url") or ""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    return _opener(proxy).open(req, timeout=timeout)


def opt_of(p: dict) -> dict:
    return {"max_tokens": p.get("max_tokens") or 32000, "thinking": p.get("thinking") or "adaptive", "reasoning": p.get("reasoning") or "auto",
            "effort": p.get("effort") or "", "cache": p.get("cache", True), "system_role": p.get("system_role") or ""}


# ================================================================ 状态 / 记录

STATE = {"running": False, "port": None, "started": None, "error": "", "requests": 0, "errors": 0, "last": None}
LOG: list = []           # 最近 80 条请求（不含内容）


def _scrub(s: str) -> str:
    s = str(s or "")
    s = re.sub(r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]{10,}", r"\1…", s)
    s = re.sub(r"(ccss-local-)[A-Za-z0-9_-]+", r"\1…", s)
    return s[:300]


def _log(rec: dict):
    rec["at"] = time.time()
    if rec.get("message"):
        rec["message"] = _scrub(rec["message"])
    with _lock:
        LOG.append(rec)
        del LOG[:-80]
        STATE["requests"] += 1
        if not rec.get("ok"):
            STATE["errors"] += 1
        STATE["last"] = rec


def _record_usage(client: str, p: dict, model: str, entry, u: dict):
    if not u or not sum(int(u.get(k) or 0) for k in ("input", "cache_read", "cache_write", "output")):
        return
    try:
        from . import usage
        usage.record_proxy("claude-code" if client == "anthropic" else "codex", f"模型路由 · {p['name']}", model, u,
                           (entry or {}).get("price"))
    except Exception as e:                     # 用量记不上不影响请求
        STATE["usage_error"] = f"{type(e).__name__}: {e}"


class UsageSniffer:
    """同协议直通时，从上游的 SSE 里顺手捞用量（不改内容）。"""

    def __init__(self, proto: str):
        self.proto, self.u = proto, {}

    def feed(self, data: str):
        try:
            d = json.loads(data)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        if self.proto == "anthropic":
            if d.get("type") == "message_start":
                self.u = xproto._usage_anth((d.get("message") or {}).get("usage") or {})
            elif d.get("type") == "message_delta":
                for k, v in xproto._usage_anth(d.get("usage") or {}).items():
                    if v:
                        self.u[k] = v
            elif d.get("usage") and d.get("type") == "message":
                self.u = xproto._usage_anth(d["usage"])
        elif self.proto == "responses":
            r = d.get("response") if d.get("type") in ("response.completed", "response.incomplete") else (d if d.get("object") == "response" else None)
            if r and r.get("usage"):
                self.u = xproto._usage_oai(r["usage"])
        elif d.get("usage"):
            self.u = xproto._usage_oai(d["usage"])


# ================================================================ 转发

class ClientGone(Exception):
    pass


def _models_list(anth: bool) -> dict:
    d = cfg()
    ids = []
    for p in d["providers"]:
        if p.get("disabled"):
            continue
        for e in p["models"]:
            for v in (e["alias"], e["name"]):
                if v and v not in ids:
                    ids.append(v)
    if anth:
        return {"data": [{"type": "model", "id": i, "display_name": i, "created_at": "2026-01-01T00:00:00Z"} for i in ids],
                "has_more": False, "first_id": ids[0] if ids else None, "last_id": ids[-1] if ids else None}
    return {"object": "list", "data": [{"id": i, "object": "model", "created": 0, "owned_by": "cc-sessionsync-route"} for i in ids]}


def forward(h, client: str, body: dict, count: bool = False):
    """h：请求处理器（send_json / stream_start / stream_write / stream_end）。client：anthropic / responses / chat。"""
    d = cfg()
    want = str(body.get("model") or "")
    alias = client_model("claude" if client == "anthropic" else "codex", want, d)
    cands = resolve(alias, d)
    t0 = time.time()
    if not cands:
        msg = f"模型路由里没有能处理「{alias or want}」的供应商（检查供应商的模型 / 别名，以及 Claude Code 的槽位设置）"
        _log({"ok": False, "client": client, "model": want, "status": 404, "message": msg})
        st, b = xproto.client_error(client, 404, {"message": msg, "code": "model_not_found"})
        return h.send_json(st, b)
    stream = bool(body.get("stream")) and not count
    rounds = 1 + int(d.get("request_retry") or 0)
    last = None
    for rnd in range(rounds):
        any_ready = any(_COOL.get(k["name"], 0) <= time.time() for p, _, _ in cands for k in p["keys"] if not k.get("disabled"))
        if rnd and not any_ready:
            soon = min([_COOL.get(k["name"], 0) for p, _, _ in cands for k in p["keys"] if not k.get("disabled")] or [time.time()])
            time.sleep(max(0.0, min(soon - time.time(), float(d.get("max_retry_wait") or 20))))
        for p, upm, entry in cands:
            for k in pick_keys(p, d):
                if _COOL.get(k["name"], 0) > time.time() and any_ready:
                    continue
                secret = keystore.get(k["name"])
                if not secret:
                    continue
                try:
                    r = _attempt(h, client, body, p, k, secret, upm, entry, want, stream, count, t0)
                except ClientGone:
                    return
                if r is True:
                    return
                last = r
                if r.get("final"):
                    break
            if last and last.get("final"):
                break
        if last and last.get("final"):
            break
    last = last or {"status": 503, "err": {"message": "所有 key 都不可用（没有 key，或者都在冷却）"}, "headers": {}}
    st, b = xproto.client_error(client, last["status"], last["err"])
    extra = {"retry-after": last["headers"]["retry-after"]} if (last.get("headers") or {}).get("retry-after") else {}
    h.send_json(st, b, extra)


def _attempt(h, client, body, p, k, secret, upm, entry, want, stream, count, t0):
    """试一个（供应商, key）。成功并已回给客户端 → True；失败 → {status, err, headers, final}（final = 不用再换 key）。"""
    if count:
        if p["protocol"] != "anthropic":
            n = xproto.estimate_tokens(body)
            h.send_json(200, {"input_tokens": n})
            return True
        url, ub, ctx = endpoint(p, "count"), dict(body, model=upm), None
        ub.pop("stream", None)
    elif client == p["protocol"]:
        url, ub, ctx = endpoint(p), dict(body, model=upm), None
    else:
        url = endpoint(p)
        ub, ctx = xproto.convert_request(client, p["protocol"], body, upm, opt_of(p))
    beta = None
    if p["protocol"] == "anthropic":
        if client == "anthropic":
            beta = h.headers.get("anthropic-beta")
        elif ub.get("thinking"):
            beta = "interleaved-thinking-2025-05-14" + (",effort-2025-11-24" if ub.get("output_config") else "")
    hdrs = up_headers(p, secret, {kk.lower(): vv for kk, vv in h.headers.items()}, beta)
    data = json.dumps(ub, ensure_ascii=False).encode("utf-8")
    try:
        resp = http(p, k, "POST", url, hdrs, data)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(20000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        txt = txt.replace(secret, "***")
        err = xproto.parse_error(e.code, txt)
        ra = e.headers.get("retry-after") if e.headers else None
        _kstat(k["name"], False, e.code)
        retry = e.code in RETRY_STATUS
        if retry:
            cool(k["name"], e.code, ra)
        _log({"ok": False, "client": client, "provider": p["name"], "key": k.get("label") or k["id"], "model": upm, "status": e.code,
              "ms": int((time.time() - t0) * 1000), "message": err["message"], "retry": retry})
        return {"status": e.code, "err": err, "headers": {"retry-after": ra} if ra else {}, "final": not retry}
    except Exception as e:
        _kstat(k["name"], False, None)
        cool(k["name"], None)
        reason = getattr(e, "reason", None) or e
        msg = f"连不上 {p['name']}：{type(reason).__name__}: {reason}"
        _log({"ok": False, "client": client, "provider": p["name"], "key": k.get("label") or k["id"], "model": upm, "status": 502,
              "ms": int((time.time() - t0) * 1000), "message": msg, "retry": True})
        return {"status": 502, "err": {"message": msg, "code": "upstream_unreachable"}, "headers": {}, "final": False}
    _kstat(k["name"], True, resp.status)
    try:
        u = _relay(h, client, p, resp, ctx, want, stream, count)
    finally:
        try:
            resp.close()
        except Exception:
            pass
    _log({"ok": True, "client": client, "provider": p["name"], "key": k.get("label") or k["id"], "model": upm, "status": 200,
          "ms": int((time.time() - t0) * 1000), "tokens": sum(int((u or {}).get(x) or 0) for x in ("input", "cache_read", "cache_write", "output")),
          "stream": stream})
    if not count:
        _record_usage(client, p, upm, entry, u or {})
    return True


def _rewrite_msg_id(line: bytes) -> bytes:
    """Claude 直通：把 message id 换成 msg_ccss…（会话日志扫描时据此跳过，用量只按代理这边记的算，不会算两遍）。"""
    if b'"message_start"' not in line or not line.startswith(b"data:"):
        return line
    try:
        d = json.loads(line[5:].strip())
        d["message"]["id"] = xproto.uid("msg")
        return b"data: " + json.dumps(d, ensure_ascii=False).encode() + b"\n"
    except Exception:
        return line


def _relay(h, client, p, resp, ctx, want, stream, count):
    passthrough = ctx is None
    if not stream:
        raw = resp.read()
        if passthrough:
            try:
                j = json.loads(raw)
            except ValueError:
                j = None
            if isinstance(j, dict) and client == "anthropic" and j.get("type") == "message":
                j["id"] = xproto.uid("msg")
                raw = json.dumps(j, ensure_ascii=False).encode()
            h.send_raw(resp.status, raw, resp.headers.get("content-type") or "application/json")
            sn = UsageSniffer(p["protocol"])
            sn.feed(raw.decode("utf-8", "replace"))
            return sn.u
        j = json.loads(raw or b"{}")
        em = xproto.EMITTERS[client](want, False, ctx)
        for ev in xproto.ir_from(p["protocol"], False, j):
            em.feed(ev)
        if em.error and not em.started:
            st, b = xproto.client_error(client, em.error.get("status") or 500, em.error)
            h.send_json(st, b)
            return {}
        h.send_json(200, em.result())
        return em.usage
    h.stream_start()
    lines = iter(resp.readline, b"")
    if passthrough:
        sn = UsageSniffer(p["protocol"])
        buf = []
        for line in lines:
            if client == "anthropic":
                line = _rewrite_msg_id(line)
            h.stream_write(line)
            s = line.decode("utf-8", "replace").rstrip("\r\n")
            if s.startswith("data:"):
                buf.append(s[5:].lstrip())
            elif not s and buf:
                sn.feed("\n".join(buf))
                buf = []
        if buf:
            sn.feed("\n".join(buf))
        h.stream_end()
        return sn.u
    em = xproto.EMITTERS[client](want, True, ctx)
    for ev in xproto.ir_from(p["protocol"], True, xproto.sse_events(lines)):
        out = em.feed(ev)
        if out:
            h.stream_write(out)
    h.stream_end()
    return em.usage


# ================================================================ HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CCSessionSync-Route"

    def log_message(self, *a):
        pass

    def _auth(self) -> bool:
        tok = token(create=False) or ""
        got = self.headers.get("x-api-key") or ""
        a = self.headers.get("authorization") or ""
        if not got and a.lower().startswith("bearer "):
            got = a[7:].strip()
        return bool(tok) and hmac.compare_digest(got.encode(), tok.encode())

    def send_raw(self, status: int, data: bytes, ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def send_json(self, status: int, obj, extra=None):
        self.send_raw(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json", extra)

    def stream_start(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-accel-buffering", "no")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()

    def stream_write(self, data: bytes):
        if not data:
            return
        try:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise ClientGone()

    def stream_end(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _body(self) -> bytes:
        if (self.headers.get("transfer-encoding") or "").lower() == "chunked":
            out = b""
            while True:
                n = int(self.rfile.readline().strip().split(b";")[0] or b"0", 16)
                if not n:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(n)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/health"):
            return self.send_json(200, {"ok": True, "service": "cc-sessionsync-route"})
        if not self._auth():
            return self.send_json(401, {"error": {"message": "CC Session Sync 模型路由：本机口令不对", "type": "authentication_error"}})
        if path.endswith("/models"):
            return self.send_json(200, _models_list(bool(self.headers.get("anthropic-version"))))
        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        raw = self._body()
        anth = "/messages" in path
        if not self._auth():
            st, b = xproto.client_error("anthropic" if anth else "responses", 401,
                                        {"message": "CC Session Sync 模型路由：本机口令不对（在「供应商 → 模型路由」里重新应用一次配置）"})
            return self.send_json(st, b)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send_json(400, {"error": {"message": "请求体不是 JSON"}})
        try:
            if path.endswith("/messages/count_tokens"):
                return forward(self, "anthropic", body, count=True)
            if path.endswith("/messages"):
                return forward(self, "anthropic", body)
            if path.endswith("/responses"):
                return forward(self, "responses", body)
            if path.endswith("/chat/completions"):
                return forward(self, "chat", body)
        except ClientGone:
            return
        except Exception as e:
            _log({"ok": False, "client": "?", "status": 500, "message": f"{type(e).__name__}: {e}"})
            try:
                st, b = xproto.client_error("anthropic" if anth else "responses", 500, {"message": f"模型路由内部错误：{type(e).__name__}"})
                return self.send_json(st, b)
            except Exception:
                return
        self.send_json(404, {"error": {"message": f"模型路由不认这个路径：{path}"}})


class _Srv(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


_SRV = {"srv": None}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ensure_port() -> int:
    with _lock:
        d = load()
        if not d.get("port"):
            d["port"] = free_port()
            save(d)
        return d["port"]


def start(port: int | None = None) -> dict:
    with _lock:
        if _SRV["srv"]:
            return status()
        token()
        port = port or ensure_port()
        try:
            srv = _Srv(("127.0.0.1", port), Handler)
        except OSError as e:
            STATE.update(running=False, error=f"端口 {port} 起不来（被别的程序占用？）：{e}")
            return status()
        _SRV["srv"] = srv
        STATE.update(running=True, port=port, started=time.time(), error="")
        threading.Thread(target=srv.serve_forever, daemon=True, name="ccss-route").start()
        return status()


def stop() -> dict:
    with _lock:
        srv = _SRV["srv"]
        _SRV["srv"] = None
        if srv:
            srv.shutdown()
            srv.server_close()
        STATE.update(running=False)
        return status()


def status() -> dict:
    d = cfg()
    return {**{k: v for k, v in STATE.items() if k != "last"}, "last": STATE.get("last"), "configured_port": d.get("port"),
            "autostart": d.get("autostart", True), "providers": len(d["providers"]),
            "keys": sum(len(p["keys"]) for p in d["providers"]), "cooling": sum(1 for v in _COOL.values() if v > time.time()),
            "log": list(reversed(LOG[-30:])), "errors_recent": [x for x in reversed(LOG) if not x.get("ok")][:10]}


def autostart():
    """程序启动时：配过供应商 / 应用过配置、并且开着「随程序启动」就起代理。"""
    d = load()
    if d.get("autostart", True) and (d["providers"] or d.get("applied")):
        return start()
    return None


# ================================================================ 应用 / 撤销：Codex、Claude Code

def _sha(b: bytes | None) -> str | None:
    return hashlib.sha256(b).hexdigest() if b is not None else None


def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


def aliases(d: dict | None = None) -> list:
    d = d or cfg()
    out = []
    for p in d["providers"]:
        if p.get("disabled"):
            continue
        for e in p["models"]:
            v = e["alias"] or e["name"]
            if v not in out:
                out.append(v)
    return out


def base_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or ensure_port()}"


def _provider_table(tk, d: dict, port: int):
    t = tk.table()
    t["name"] = "CC Session Sync 模型路由"
    t["base_url"] = base_url(port) + "/v1"
    t["wire_api"] = d["clients"]["codex"].get("wire") or "responses"
    cmd = keystore.helper_cmd(TOKEN_KEY)
    a = tk.table()
    a["command"] = cmd[0]
    a["args"] = cmd[1:]
    a["timeout_ms"] = 15000
    a["refresh_interval_ms"] = 0
    t["auth"] = a
    t["request_max_retries"] = 1           # 换 key / 重试由路由自己做
    return t


def codex_profile_text(d: dict, port: int) -> str:
    from .modelcfg import _toml
    tk = _toml()
    cx = d["clients"]["codex"]
    doc = tk.document()
    doc.add(tk.comment("CC Session Sync 模型路由（自动生成）。用法：codex --profile cc-route"))
    doc.add(tk.comment("在 CC Session Sync →「供应商 → 模型路由」里改或撤销；手改的话撤销时会保留这个文件。"))
    doc["model_provider"] = CODEX_PROFILE
    doc["model"] = cx.get("model") or (aliases(d) or ["opus"])[0]
    doc["model_reasoning_effort"] = cx.get("effort") or "medium"
    if cx.get("context_window"):
        doc["model_context_window"] = int(cx["context_window"])
    mp = tk.table(is_super_table=True)
    mp[CODEX_PROFILE] = _provider_table(tk, d, port)
    doc["model_providers"] = mp
    return tk.dumps(doc)


def _ready():
    """应用前：要有能用的供应商，代理在跑（没跑就起来），取口令的命令能跑通。"""
    d = load()
    if not any(not p.get("disabled") and p["keys"] for p in d["providers"]):
        raise ValueError("先加一个带 key 的供应商")
    token()
    if not STATE["running"]:
        start()
    return d


def apply_codex(default: bool = False) -> dict:
    """写 CODEX_HOME/cc-route.config.toml（profile，不动 config.toml）；default=True 时再把 config.toml 的缺省供应商指过来（先备份）。"""
    from . import modelcfg
    from . import _toml as tomllib
    with _lock:
        d = _ready()
        keystore.helper_selftest(TOKEN_KEY)
        port = ensure_port()
        rec = {"at": time.time(), "default": default, "files": []}
        f = paths.CODEX_HOME / f"{CODEX_PROFILE}.config.toml"
        before = _read(f)
        prev = (d.get("applied") or {}).get("codex")
        if prev:                                 # 再应用一次：沿用第一次的原始备份
            rec = prev
            rec["default"] = default or prev.get("default")
        txt = codex_profile_text(d, port).encode("utf-8")
        tomllib.loads(txt.decode("utf-8"))
        prof = next((x for x in rec["files"] if x["kind"] == "profile"), None)
        if not prof:
            prof = {"kind": "profile", "file": str(f), "existed": before is not None, "backup": modelcfg.backup(f, "route-codex-profile") if before is not None else None}
            rec["files"].append(prof)
        modelcfg.atomic_write(f, txt)
        prof["sha"] = _sha(txt)
        if default:
            g = paths.CODEX_HOME / "config.toml"
            gb = _read(g)
            cfgf = next((x for x in rec["files"] if x["kind"] == "config"), None)
            tk = modelcfg._toml()
            doc = tk.parse(gb.decode("utf-8") if gb else "")
            if not cfgf:
                cfgf = {"kind": "config", "file": str(g), "existed": gb is not None, "backup": modelcfg.backup(g, "route-codex-default") if gb is not None else None,
                        "orig": {"model_provider": doc.get("model_provider"), "model": doc.get("model")}}
                cfgf["orig"] = {k: (str(v) if v is not None else None) for k, v in cfgf["orig"].items()}
                rec["files"].append(cfgf)
            if "model_providers" not in doc:
                doc["model_providers"] = tk.table(is_super_table=True)
            doc["model_providers"][CODEX_PROFILE] = _provider_table(tk, d, port)
            doc["model_provider"] = CODEX_PROFILE
            doc["model"] = d["clients"]["codex"].get("model") or (aliases(d) or ["opus"])[0]
            out = tk.dumps(doc).encode("utf-8")
            tomllib.loads(out.decode("utf-8"))
            modelcfg.atomic_write(g, out)
            cfgf["sha"] = _sha(out)
            cfgf["ours"] = {"model_provider": CODEX_PROFILE, "model": str(doc["model"])}
        d = load()
        d.setdefault("applied", {})["codex"] = rec
        save(d)
        return {"ok": True, "files": [x["file"] for x in rec["files"]], "cmd": f"codex --profile {CODEX_PROFILE}", "default": bool(rec.get("default"))}


def _restore_file(x: dict) -> str:
    """撤销一个文件：没被改过 → 按字节还原（原来没有就删掉）；被改过 → 返回 "changed" 交给调用方做局部撤销。"""
    from . import modelcfg
    p = Path(x["file"])
    cur = _read(p)
    if _sha(cur) != x.get("sha"):
        return "changed" if cur is not None else "gone"
    if x.get("existed"):
        src = next((b for b in modelcfg.backups(100000) if b["id"] == x.get("backup")), None)
        if not src:
            return "nobackup"
        modelcfg.atomic_write(p, (modelcfg.BACKUPS / (src["id"] + src["ext"])).read_bytes())
    else:
        p.unlink()
    return "restored"


def revert_codex() -> dict:
    from . import modelcfg
    with _lock:
        d = load()
        rec = (d.get("applied") or {}).get("codex")
        if not rec:
            return {"ok": True, "note": "没有应用过"}
        res = []
        for x in rec["files"]:
            r = _restore_file(x)
            if r == "changed" and x["kind"] == "config":
                p = Path(x["file"])
                modelcfg.backup(p, "route-before-revert")
                tk = modelcfg._toml()
                doc = tk.parse(p.read_text(encoding="utf-8"))
                mp = doc.get("model_providers")
                if mp is not None and CODEX_PROFILE in mp:
                    del mp[CODEX_PROFILE]
                    if not len(mp):
                        del doc["model_providers"]
                for k, ours in (x.get("ours") or {}).items():
                    if str(doc.get(k)) == ours:
                        if x["orig"].get(k) is None:
                            del doc[k]
                        else:
                            doc[k] = x["orig"][k]
                modelcfg.atomic_write(p, tk.dumps(doc).encode("utf-8"))
                r = "partial"
            res.append({"file": x["file"], "result": r})
        d["applied"].pop("codex", None)
        save(d)
        return {"ok": True, "files": res}


def claude_env(port: int | None = None) -> dict:
    return {"ANTHROPIC_BASE_URL": base_url(port), "ANTHROPIC_AUTH_TOKEN": token()}


def session_settings_file() -> Path:
    return paths.HOME / "route" / "claude-settings.json"


def apply_claude(mode: str = "global") -> dict:
    """mode=global：~/.claude/settings.json 的 env 里设 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN（先备份）；
    mode=session：只写一个给 `claude --settings <文件>` 用的文件，全局配置不动。"""
    from . import modelcfg
    with _lock:
        _ready()
        port = ensure_port()
        env = claude_env(port)
        sf = session_settings_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        modelcfg.atomic_write(sf, (json.dumps({"env": env}, indent=2) + "\n").encode("utf-8"))
        try:
            os.chmod(sf, 0o600)
        except OSError:
            pass
        out = {"ok": True, "session_file": str(sf), "cmd": f'claude --settings "{sf}"'}
        if mode != "global":
            return out
        d = load()
        f = modelcfg.claude_file("user", None)
        b = _read(f)
        rec = (d.get("applied") or {}).get("claude")
        s = json.loads(b.decode("utf-8-sig")) if b and b.strip() else {}
        if not rec:
            e0 = s.get("env") if isinstance(s.get("env"), dict) else {}
            rec = {"at": time.time(), "file": str(f), "existed": b is not None, "backup": modelcfg.backup(f, "route-claude") if b is not None else None,
                   "had_env": "env" in s, "orig": {k: e0.get(k) for k in env}}
        if not isinstance(s.get("env"), dict):
            s["env"] = {}
        s["env"].update(env)
        data = (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        modelcfg.atomic_write(f, data)
        rec["sha"] = _sha(data)
        rec["ours"] = env
        d.setdefault("applied", {})["claude"] = rec
        save(d)
        return dict(out, file=str(f))


def revert_claude() -> dict:
    from . import modelcfg
    with _lock:
        d = load()
        try:
            session_settings_file().unlink()
        except OSError:
            pass
        rec = (d.get("applied") or {}).get("claude")
        if not rec:
            return {"ok": True, "note": "没有应用过（命令行专用的 settings 文件已删）"}
        r = _restore_file(rec)
        if r == "changed":
            p = Path(rec["file"])
            modelcfg.backup(p, "route-before-revert")
            s = modelcfg._read_json(p)
            env = s.get("env") if isinstance(s.get("env"), dict) else {}
            for k, v in (rec.get("ours") or {}).items():
                if env.get(k) == v:
                    if rec["orig"].get(k) is None:
                        env.pop(k, None)
                    else:
                        env[k] = rec["orig"][k]
            if not env and not rec.get("had_env"):
                s.pop("env", None)
            modelcfg.atomic_write(p, (json.dumps(s, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            r = "partial"
        d["applied"].pop("claude", None)
        save(d)
        return {"ok": True, "file": rec["file"], "result": r}


def applied_view() -> dict:
    d = load()
    out = {}
    for k, rec in (d.get("applied") or {}).items():
        files = rec.get("files") or [rec]
        out[k] = {"at": rec.get("at"), "default": rec.get("default"),
                  "files": [{"file": x["file"], "changed": _sha(_read(Path(x["file"]))) != x.get("sha")} for x in files]}
    return out


# ================================================================ 测试 / 模型列表

def _tiny(p: dict, model: str) -> dict:
    if p["protocol"] == "anthropic":
        return {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    if p["protocol"] == "responses":
        return {"model": model, "input": "ping", "max_output_tokens": 16, "store": False}
    k = "max_completion_tokens" if xproto.is_reasoning(model, opt_of(p)) else "max_tokens"
    return {"model": model, "messages": [{"role": "user", "content": "ping"}], k: 16 if k == "max_completion_tokens" else 1}


def test_provider(pid: str, model: str | None = None) -> dict:
    """每个 key 发一个最小请求（选了模型 → 生成 1 个 token；没选 → 只拉模型列表，不花钱）。只回状态码 / 耗时 / 简短原因。"""
    d = load()
    p = next((x for x in d["providers"] if x["id"] == pid), None)
    if not p:
        raise ValueError("没有这个供应商")
    model = model or next((e["name"] for e in p["models"]), None)
    rows = []
    for k in p["keys"]:
        sec = keystore.get(k["name"])
        row = {"key": k.get("label") or k["id"], "masked": keystore.mask(sec), "disabled": k.get("disabled")}
        if not sec:
            rows.append(dict(row, ok=False, reason="凭据管理器里没有这个 key（重新填一次）"))
            continue
        t0 = time.time()
        try:
            if model:
                url = endpoint(p)
                r = http(p, k, "POST", url, up_headers(p, sec), json.dumps(_tiny(p, model)).encode(), timeout=60)
            else:
                url = endpoint(p, "models")
                r = http(p, k, "GET", url, {kk: vv for kk, vv in up_headers(p, sec).items() if kk != "content-type"}, None, timeout=30)
            r.read(200000)
            r.close()
            rows.append(dict(row, ok=True, status=r.status, ms=int((time.time() - t0) * 1000), what="生成 1 个 token" if model else "模型列表"))
            _kstat(k["name"], True, r.status)
        except urllib.error.HTTPError as e:
            txt = e.read(4000).decode("utf-8", "replace").replace(sec, "***")
            rows.append(dict(row, ok=False, status=e.code, ms=int((time.time() - t0) * 1000), reason=_scrub(xproto.parse_error(e.code, txt)["message"])))
            _kstat(k["name"], False, e.code)
        except Exception as e:
            reason = getattr(e, "reason", None) or e
            rows.append(dict(row, ok=False, status=None, ms=int((time.time() - t0) * 1000), reason=_scrub(f"连不上：{type(reason).__name__}: {reason}")))
    return {"provider": p["name"], "model": model, "url": endpoint(p) if model else endpoint(p, "models"), "keys": rows,
            "ok": bool(rows) and all(r["ok"] for r in rows if not r.get("disabled"))}


def list_models(pid: str | None = None, draft: dict | None = None) -> dict:
    """GET <base>/models（Anthropic 是 /v1/models）。draft：还没保存的 {protocol, base_url, secret, proxy_url}。"""
    if pid:
        p = next((x for x in load()["providers"] if x["id"] == pid), None)
        if not p:
            raise ValueError("没有这个供应商")
        k = next((k for k in p["keys"] if not k.get("disabled") and keystore.get(k["name"])), None)
        sec = keystore.get(k["name"]) if k else None
    else:
        draft = draft or {}
        p = {"protocol": draft.get("protocol") or "chat", "base_url": _clean_url(draft.get("base_url") or ""), "headers": {}, "proxy_url": _clean_proxy(draft.get("proxy_url") or ""),
             "excluded": []}
        k, sec = {}, (draft.get("secret") or "").strip()
    if not sec:
        return {"ok": False, "reason": "没有 key"}
    t0 = time.time()
    try:
        hd = {kk: vv for kk, vv in up_headers(p, sec).items() if kk != "content-type"}
        r = http(p, k or {}, "GET", endpoint(p, "models") + ("?limit=1000" if p["protocol"] == "anthropic" else ""), hd, None, timeout=30)
        j = json.loads(r.read(4_000_000) or b"{}")
        r.close()
    except urllib.error.HTTPError as e:
        txt = e.read(4000).decode("utf-8", "replace").replace(sec, "***")
        return {"ok": False, "status": e.code, "reason": _scrub(xproto.parse_error(e.code, txt)["message"]) + "（这家可能没有模型列表接口：手动填模型名就行）"}
    except Exception as e:
        reason = getattr(e, "reason", None) or e
        return {"ok": False, "status": None, "reason": _scrub(f"连不上：{type(reason).__name__}: {reason}")}
    rows = j.get("data") if isinstance(j, dict) else j
    ids = []
    for m in rows or []:
        v = m.get("id") or m.get("name") if isinstance(m, dict) else m
        if isinstance(v, str) and v and v not in ids and not any(_wild(x, v) for x in p.get("excluded") or []):
            ids.append(v.split("models/")[-1])
    return {"ok": True, "ms": int((time.time() - t0) * 1000), "models": sorted(ids)}


# ================================================================ 从 CLIProxyAPI 的 config.yaml 导入（只导 API key 供应商）

def _yaml(text: str):
    try:
        import yaml
    except ImportError:
        raise RuntimeError("缺少 pyyaml（pip install pyyaml）")
    v = yaml.safe_load(text or "")
    if not isinstance(v, dict):
        raise ValueError("不是 CLIProxyAPI 的 config.yaml（顶层不是映射）")
    return v


CPA_KIND = {"claude": ("anthropic", "https://api.anthropic.com"), "codex": ("responses", "https://api.openai.com/v1"),
            "gemini": ("chat", "https://generativelanguage.googleapis.com/v1beta/openai"), "xai": ("responses", "https://api.x.ai/v1"),
            "openai-compatibility": ("chat", "")}


def parse_cpa(text: str) -> dict:
    """返回 {providers: [草稿（含明文 key，只在内存里）], skipped: [说明]}。OAuth / 订阅登录那部分一律跳过。"""
    y = _yaml(text)
    groups, skipped = [], []
    ak = y.get("api-keys")
    if isinstance(ak, dict):                                    # v8：api-keys.<kind>: [组]
        for kind, lst in ak.items():
            if kind not in CPA_KIND:
                skipped.append(f"api-keys.{kind}（不支持的类型）")
                continue
            for g in lst or []:
                groups.append((kind, g))
    for legacy, kind in (("claude-api-key", "claude"), ("codex-api-key", "codex"), ("gemini-api-key", "gemini"), ("openai-compatibility", "openai-compatibility")):
        for g in y.get(legacy) or []:
            groups.append((kind, g))
    for k in ("oauth", "auth-dir"):
        if y.get(k):
            skipped.append(f"{k}：订阅登录（OAuth）不支持，跳过")
    out = []
    for kind, g in groups:
        if not isinstance(g, dict) or g.get("disabled"):
            continue
        proto, dflt = CPA_KIND[kind]
        keys = []
        for kk in g.get("keys") or g.get("api-key-entries") or []:
            if isinstance(kk, dict) and kk.get("api-key"):
                keys.append({"secret": str(kk["api-key"]), "weight": kk.get("weight") or 1, "proxy_url": kk.get("proxy-url") or ""})
            elif isinstance(kk, str):
                keys.append({"secret": kk})
        for kk in g.get("api-keys") or []:
            if isinstance(kk, str):
                keys.append({"secret": kk})
        if g.get("api-key"):
            keys.append({"secret": str(g["api-key"]), "proxy_url": g.get("proxy-url") or ""})
        base = str(g.get("base-url") or dflt or "").rstrip("/")
        if not base:
            skipped.append(f"{kind} {g.get('name') or ''}：没有 base-url")
            continue
        if kind == "codex" and not re.search(r"/v\d", base):
            base += "/v1"
        if kind == "gemini" and "generativelanguage.googleapis.com" in base and "/openai" not in base:
            base = dflt
        models = [{"name": str(m.get("name")), "alias": str(m.get("alias") or "")} for m in g.get("models") or [] if isinstance(m, dict) and m.get("name")]
        name = str(g.get("name") or {"claude": "Claude API", "codex": "OpenAI / Codex API", "gemini": "Gemini API", "xai": "xAI"}.get(kind, kind))
        hd = {str(k): str(v) for k, v in (g.get("headers") or {}).items()} if isinstance(g.get("headers"), dict) else {}
        out.append({"name": f"{name}（CPA）", "protocol": proto, "base_url": base, "headers": hd,
                    "proxy_url": "" if (g.get("proxy-url") or "").startswith("socks") else (g.get("proxy-url") or ""),
                    "prefix": g.get("prefix") or "", "models": models, "excluded": list(g.get("excluded-models") or []), "keys_new": keys,
                    "note": "socks 代理不支持，已改成系统代理" if str(g.get("proxy-url") or "").startswith("socks") else ""})
    return {"providers": out, "skipped": skipped}


def cpa_preview(text: str) -> dict:
    r = parse_cpa(text)
    return {"providers": [{**{k: v for k, v in p.items() if k != "keys_new"}, "keys": [{"masked": keystore.mask(k["secret"]), "weight": k.get("weight") or 1}
                                                                                  for k in p["keys_new"]]} for p in r["providers"]],
            "skipped": r["skipped"]}


def cpa_import(text: str, picks: list | None = None) -> dict:
    r = parse_cpa(text)
    done = []
    for i, p in enumerate(r["providers"]):
        if picks is not None and i not in picks:
            continue
        nk = p.pop("keys_new")
        p.pop("note", None)
        try:
            for k in nk:
                if k.get("proxy_url", "").startswith("socks"):
                    k["proxy_url"] = ""
            done.append(upsert_provider(dict(p, keys=[]), nk)["name"])
        except ValueError as e:
            r["skipped"].append(f"{p['name']}：{e}")
    return {"ok": True, "imported": done, "skipped": r["skipped"]}


# ================================================================ 给页面

def overview() -> dict:
    d = load()
    try:
        from . import apps
        run = apps.status()
    except Exception:
        run = {}
    tok = token(create=False)
    return {"providers": [view_provider(p) for p in d["providers"]], "clients": d["clients"], "strategy": d.get("strategy"),
            "request_retry": d.get("request_retry"), "proxy_url": d.get("proxy_url"), "autostart": d.get("autostart", True),
            "port": d.get("port"), "status": status(), "applied": applied_view(), "token_masked": keystore.mask(tok) if tok else "",
            "presets": PRESETS, "protocols": PROTO_NAME, "aliases": aliases(d), "running": run, "keystore": keystore.backend(),
            "codex_cmd": f"codex --profile {CODEX_PROFILE}", "session_file": str(session_settings_file()),
            "base_url": f"http://127.0.0.1:{d.get('port')}" if d.get("port") else ""}


def main(argv: list[str]) -> int:
    """cc-sessionsync route serve [--port N] | status"""
    import sys
    if argv[:1] == ["serve"]:
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else None
        st = start(port)
        if not st["running"]:
            print(st["error"], file=sys.stderr)
            return 1
        print(f"CC Session Sync 模型路由：http://127.0.0.1:{st['port']}（Ctrl+C 退出）", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    if argv[:1] == ["status"]:
        s = status()
        print(json.dumps({k: s[k] for k in ("running", "port", "configured_port", "providers", "keys", "requests", "errors")}, ensure_ascii=False))
        return 0
    if argv[:1] == ["apply"] and len(argv) > 1:
        r = apply_codex("--default" in argv) if argv[1] == "codex" else apply_claude("session" if "--session" in argv else "global")
        print(json.dumps(r, ensure_ascii=False))
        return 0
    if argv[:1] == ["revert"] and len(argv) > 1:
        print(json.dumps(revert_codex() if argv[1] == "codex" else revert_claude(), ensure_ascii=False))
        return 0
    print("用法：route serve [--port N] | status | apply codex [--default] | apply claude [--session] | revert codex|claude", file=sys.stderr)
    return 2

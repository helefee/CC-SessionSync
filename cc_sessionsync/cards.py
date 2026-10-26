"""「供应商」页的两张卡片（0.3.1）：Codex 使用… / Claude Code 使用…

用户只选「用什么」、贴 key、点「启用」。要不要协议转换、后台代理在哪个端口，都由这里决定：
- 不用转换（Codex + OpenAI key / Responses 接口；Claude Code + Claude key）：直接把客户端指到上游，key 走取 key 命令；
- 要转换（Codex + Claude key / Anthropic、Chat 接口；Claude Code + OpenAI key / Responses、Chat 接口）：
  客户端指到本机后台代理（独立进程，登录自启 / 取口令时按需拉起），代理只把这个客户端的请求转给卡片的上游。

改的文件都记下原样：停用时没被动过就按字节还原，被动过就只拿掉我们写的那几项。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import keystore, paths, presets, router

CLIENTS = ("codex", "claude")
_ALL = ("default",) + tuple(x["id"] for x in presets.PRESETS) + ("custom",)
KINDS = {"codex": _ALL, "claude": _ALL}       # 两张卡能选的一样（0.3.2）
KIND_NAME = dict({"default": "默认（客户端自己的登录）", "custom": "自定义"}, **{x["id"]: x["name"] for x in presets.PRESETS})
LEGACY = {"claude": "anthropic"}             # 0.3.1 的卡片 kind
CLIENT_NAME = {"codex": "Codex", "claude": "Claude Code"}
OFFICIAL = {"claude": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
DEF_MODEL = {"anthropic": ("claude-opus-5-5", "claude-haiku-5-5"), "openai": ("gpt-5.5", "gpt-5.4-mini")}
EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
_ABSENT = {"__absent__": True}


# ================================================================ 配置

def _blank(client: str) -> dict:
    return {"kind": "default", "base_url": "", "protocol": "anthropic" if client == "codex" else "responses", "model": "", "small_model": "", "sonnet_model": "",
            "effort": "", "region": "", "ctx": {}, "fix_refs": [], "profile_only": False, "enabled": False, "models": []}


def get(client: str, d: dict | None = None) -> dict:
    d = d or router.cfg()
    c = dict(_blank(client), **((d.get("cards") or {}).get(client) or {}))
    c["kind"] = LEGACY.get(c["kind"], c["kind"])
    if c["kind"] not in KINDS[client]:
        c["kind"] = "default"
    return c


def proto(c: dict, client: str | None = None) -> str | None:
    """上游用哪种协议。预设：客户端自己的协议优先（原样转发），没有再挑能转换的。"""
    k = c["kind"]
    if k == "custom":
        return c.get("protocol") or None
    if presets.get(k):
        return presets.pick(k, client or c.get("_client") or "codex", c.get("region"))[0]
    return None


def native(client: str) -> str:
    return "responses" if client == "codex" else "anthropic"


def direct(client: str, c: dict) -> bool:
    """不经后台服务、客户端直接连上游：Codex + OpenAI key；Claude Code + Claude key / 自定义 Anthropic 接口。
    其它都经后台服务：要转换协议，或者要给 Codex 一份模型目录（/model 列表、推理等级）、给 Claude Code 换模型名。"""
    if client == "codex":
        return c["kind"] == "openai"
    return c["kind"] == "anthropic" or (c["kind"] == "custom" and proto(c, client) == "anthropic")


def needs(client: str, c: dict) -> bool:
    """这张卡要不要后台服务。"""
    return c["kind"] != "default" and not direct(client, c)


def needs_proxy(d: dict | None = None) -> bool:
    d = d or router.load()
    return any(get(x, d)["enabled"] and needs(x, get(x, d)) for x in CLIENTS)


def _key_names(kind: str) -> list:
    """一个服务商的 key 可能存在的名字：0.3.4 起两边共用 prov-<id>；之前每张卡各存一份 card-<client>-<id>。"""
    k = {"anthropic": "claude"}.get(kind, kind)              # 0.3.1 存的名字不变（升级后 key 还在）
    return [keystore.check_name(f"prov-{k}")] + [keystore.check_name(f"card-{cl}-{k}") for cl in CLIENTS]


def key_name(client: str, kind: str) -> str:
    """这张卡用哪个 key：共用的那份优先，然后是自己卡片以前存的，再是另一张卡存的（贴一次两边都能用）。"""
    names = _key_names(kind)
    own = keystore.check_name(f"card-{client}-{({'anthropic': 'claude'}.get(kind, kind))}")
    for n in [names[0], own] + names[1:]:
        if keystore.get(n):
            return n
    return names[0]


def put_key(kind: str, sec: str):
    """存 key：写共用的 prov-<id>；以前存过的旧名字也一起更新（已经写进配置的 apiKeyHelper 还指着它们）。"""
    names = _key_names(kind)
    label = f"CC Session Sync · 服务商 {KIND_NAME.get(kind, kind)}"
    keystore.put(names[0], sec, label)
    for n in names[1:]:
        if keystore.get(n):
            keystore.put(n, sec, label)


def upstream_url(c: dict, client: str | None = None) -> str:
    if c["kind"] == "custom":
        return c.get("base_url") or ""
    if c.get("base_url"):                                   # 「更多 → 中转地址」
        return c["base_url"]
    if presets.get(c["kind"]):
        return presets.pick(c["kind"], client or c.get("_client") or "codex", c.get("region"))[1]
    return ""


def _mask(name: str) -> str | None:
    v = keystore.get(name)
    if not v:
        return None
    return (v[:3] + "…" + v[-4:]) if len(v) > 12 else "已保存"


def _ver(m: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", m) if len(x) < 8]
    return (tuple(nums), "-20" not in m and not re.search(r"\d{8}", m))


def latest(models: list, family: str) -> str | None:
    """模型列表里某个系列（opus / haiku / mini …）最新的那个。"""
    xs = [m for m in models if family in m.lower()]
    return max(xs, key=_ver) if xs else None


SKIP = re.compile(r"embed|tts|whisper|dall-e|image|audio|moderation|rerank|transcribe|realtime|speech|ocr|vision-preview|search-preview", re.I)


def defaults_for(kind: str, models: list | None = None) -> tuple:
    models = [m for m in (models or []) if not SKIP.search(m)]
    kind = LEGACY.get(kind, kind)
    if kind == "anthropic":
        return (latest(models, "opus") or DEF_MODEL["anthropic"][0], latest(models, "haiku") or DEF_MODEL["anthropic"][1])
    if kind == "openai":
        big = [m for m in models if m.startswith("gpt-") and "mini" not in m and "nano" not in m and not re.search(r"audio|realtime|image|search|tts|transcribe", m)]
        return (max(big, key=_ver) if big else DEF_MODEL["openai"][0], latest([m for m in models if m.startswith("gpt-")], "mini") or DEF_MODEL["openai"][1])
    pr = presets.get(kind)
    if pr and models:
        big, small = pr["default"]
        cand = [m for m in models if big.lower() in m.lower() and not (small and small.lower() in m.lower())] or models
        b = max(cand, key=_ver) if big else models[0]
        sm = latest(models, small.lower()) if small else None
        return b, sm or ""
    return ((models or [""])[0], "")


def save_card(client: str, b: dict) -> dict:
    """保存卡片设置（不启用）。b.secret 有值就存进凭据管理器（明文不落盘、不回传）。"""
    if client not in CLIENTS:
        raise ValueError("只有 codex / claude 两张卡")
    with router._lock:
        d = router.load()
        c = get(client, d)
        kind = LEGACY.get(b.get("kind", c["kind"]), b.get("kind", c["kind"]))
        if kind not in KINDS[client]:
            raise ValueError("不支持这个选项")
        if kind != c["kind"]:
            c.update(model="", small_model="", sonnet_model="", models=[], base_url="", region="", ctx={})
        c["kind"] = kind
        if "region" in b and presets.get(kind):
            r = str(b.get("region") or "")
            c["region"] = r if r in presets.get(kind)["regions"] else ""
        if "base_url" in b:
            u = str(b.get("base_url") or "").strip()
            if u and kind == "custom" and b.get("protocol") in (None, "", "auto"):
                c["protocol"], u = presets.detect(u)        # 认地址：…/anthropic → Anthropic；Gemini 原生地址 → 它的 OpenAI 兼容接口；其它 → Chat
            c["base_url"] = router._clean_url(u) if u else ""
        if b.get("protocol") in router.PROTOCOLS:
            c["protocol"] = b["protocol"]
        for f in ("model", "small_model", "sonnet_model"):
            if f in b:
                v = str(b.get(f) or "").strip()
                if v and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$", v):
                    raise ValueError(f"模型名不对：{v}")
                c[f] = v
        if b.get("effort", c["effort"]) in EFFORTS:
            c["effort"] = b.get("effort", c["effort"])
        if "profile_only" in b:
            c["profile_only"] = bool(b["profile_only"])
        if isinstance(b.get("models"), list):
            c["models"] = [str(x) for x in b["models"] if isinstance(x, str)][:300]
        if isinstance(b.get("ctx"), dict):
            c["ctx"] = {str(k): int(v) for k, v in b["ctx"].items() if isinstance(v, (int, float)) and 1000 <= v <= 100_000_000}
        if kind == "custom" and not c["base_url"] and c["enabled"]:
            raise ValueError("自定义要填 Base URL")
        sec = (b.get("secret") or "").strip()
        if sec and kind != "default":
            if any(ch in sec for ch in "\r\n \t"):
                raise ValueError("key 里不能有空格或换行")
            put_key(kind, sec)
        if kind != "default" and not c["model"]:
            c["model"], small = defaults_for(kind if kind != "custom" else "", c["models"])
            if not c["model"] and kind in DEF_MODEL:
                c["model"], small = DEF_MODEL[kind]
            c["small_model"] = c["small_model"] or small
        d.setdefault("cards", {})[client] = c
        if kind != "default":                               # 服务商设置两张卡共用（「服务商」页里也能直接改）
            _prov_merge(d, kind, {f: c.get(f) for f in PROV_FIELDS if f in b or f in ("models", "ctx")} if b else {})
        router.save(d)
    return view(client)


def provider(client: str, c: dict, model: str | None = None) -> dict:
    """卡片 → 路由内部用的供应商（不存进供应商列表，只在代理里用）。"""
    kind = c["kind"]
    pr = proto(c, client) or "anthropic"
    p = router.clean_provider({"id": f"card-{client}", "name": f"{CLIENT_NAME[client]} · {KIND_NAME[kind]}", "protocol": pr,
                               "base_url": upstream_url(c, client) or "http://invalid.local", "keys": [{"id": "k1", "name": key_name(client, kind)}],
                               "effort": "", "thinking": thinking_mode(c, model, client)}, set())
    p["card"] = client
    p["style"] = presets.get(kind)["style"] if presets.get(kind) else ("anthropic" if pr == "anthropic" else "generic")
    return p


def thinking_mode(c: dict, model: str | None = None, client: str | None = None) -> str:
    """Claude 的思考参数：新模型（Opus / Sonnet 4.6 起、Haiku 5 起）用 adaptive + effort；老模型用 budget_tokens。"""
    m = (model or c.get("model") or "").lower()
    if proto(c, client) != "anthropic" or not m.startswith("claude-"):
        return "adaptive"
    v = _ver(m)[0]
    v = (v + (0, 0))[:2]
    return "adaptive" if v >= ((5, 0) if "haiku" in m else (4, 6)) else "budget"


def proxy_target(client: str, want: str, d: dict | None = None):
    """代理里用：这个客户端有启用中的、要转换的卡片 → [(供应商, 上游模型, None)]（没配好 → []）；没有卡片 → None（走高级路由）。"""
    d = d or router.cfg()
    c = get(client, d)
    if not (c["enabled"] and needs(client, c)):
        return None
    if not upstream_url(c, client) or not keystore.get(key_name(client, c["kind"])):
        return []
    w = (want or "").strip()
    if client == "claude":
        low = w.lower()
        if w and not low.startswith("claude-") and low not in ("opus", "sonnet", "haiku", "default", "fable"):
            m = w                                    # /model gpt-5.4 或 claude --model gpt-5.4：照原样
        elif "haiku" in low:
            m = c.get("small_model") or c["model"]
        elif "sonnet" in low:
            m = c.get("sonnet_model") or c["model"]
        else:
            m = c["model"]
    else:
        ok = w and (w == c["model"] or w in (c.get("models") or []) or w not in CODEX_BUILTIN)
        m = w if ok else c["model"]                 # Codex 桌面端可能带着自己内置的 gpt-… 来：换成卡片的默认模型
    return [(provider(client, c, m or w), m or w, None)] if (m or w) else []


def not_ready_msg(client: str, d: dict | None = None) -> str:
    c = get(client, d)
    if not upstream_url(c, client):
        return f"CC Session Sync：「{CLIENT_NAME[client]} 使用…」卡片没填地址。打开 CC Session Sync → 供应商 补上，或者点「停用」。"
    return f"CC Session Sync：「{CLIENT_NAME[client]} 使用 {KIND_NAME[c['kind']]}」没有可用的 key（凭据管理器里找不到）。打开 CC Session Sync → 供应商 重新贴一次 key，或者点「停用」。"


# ================================================================ 文件改动记录：按字节还原 / 只拿掉我们写的

def _plain(v):
    if hasattr(v, "unwrap"):
        v = v.unwrap()
    return json.loads(json.dumps(v, default=str))


def _walk(doc, path, create=False, tk=None):
    cur = doc
    for k in path[:-1]:
        if k not in cur:
            if not create:
                return None
            cur[k] = tk.table(is_super_table=True) if tk else {}
        cur = cur[k]
        if not hasattr(cur, "keys"):
            return None
    return cur


def _get(doc, path):
    p = _walk(doc, path)
    if p is None or path[-1] not in p:
        return _ABSENT
    return _plain(p[path[-1]])


def _set(doc, path, v, tk=None):
    if v == _ABSENT:
        p = _walk(doc, path)
        if p is not None and path[-1] in p:
            del p[path[-1]]
        _prune(doc, path)
        return
    p = _walk(doc, path, True, tk)
    if tk and isinstance(v, dict):
        t = tk.table()
        for k, x in v.items():
            if isinstance(x, dict):
                sub = tk.table()
                for k2, x2 in x.items():
                    sub[k2] = x2
                t[k] = sub
            else:
                t[k] = x
        v = t
    p[path[-1]] = v


def _prune(doc, path):
    for i in range(len(path) - 1, 0, -1):
        p = _walk(doc, path[:i])
        if p is not None and not len(p):
            q = _walk(doc, path[:i - 1]) if i > 1 else doc
            if q is not None:
                del q[path[i - 1]]


class Doc:
    def __init__(self, f: Path, fmt: str):
        from . import modelcfg
        self.f, self.fmt = f, fmt
        self.raw = router._read(f)
        if fmt == "toml":
            self.tk = modelcfg._toml()
            self.doc = self.tk.parse(self.raw.decode("utf-8-sig") if self.raw else "")
        else:
            self.tk = None
            self.doc = json.loads(self.raw.decode("utf-8-sig")) if self.raw and self.raw.strip() else {}
            if not isinstance(self.doc, dict):
                raise ValueError(f"{f} 不是 JSON 对象，没法改")

    def dump(self) -> bytes:
        if self.fmt == "toml":
            from . import _toml as tomllib
            out = self.tk.dumps(self.doc)
            tomllib.loads(out)
            return out.encode("utf-8")
        return (json.dumps(self.doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def apply_edits(f: Path, fmt: str, edits: list, rec: dict | None, tag: str) -> dict:
    """edits：[(路径, 值或 _ABSENT)]。rec：之前的记录（再次启用沿用第一次的原样）。返回新记录。"""
    from . import modelcfg
    x = Doc(f, fmt)
    if not rec:
        rec = {"file": str(f), "fmt": fmt, "existed": x.raw is not None,
               "backup": modelcfg.backup(f, tag) if x.raw is not None else None, "edits": []}
    old = {json.dumps(e["path"]): e for e in rec["edits"]}
    new = []
    for path, v in edits:
        e = old.pop(json.dumps(path), None)
        orig = e["orig"] if e else _get(x.doc, path)
        _set(x.doc, path, v, x.tk)
        new.append({"path": path, "orig": orig, "ours": _plain(v) if v != _ABSENT else _ABSENT})
    for e in old.values():                            # 这次不写了的项：放回原样
        if _get(x.doc, e["path"]) == e["ours"]:
            _set(x.doc, e["path"], e["orig"], x.tk)
    rec["edits"] = new
    out = x.dump()
    f.parent.mkdir(parents=True, exist_ok=True)
    modelcfg.atomic_write(f, out)
    rec["sha"] = router._sha(out)
    return rec


def revert_edits(rec: dict) -> str:
    """没被动过 → 按字节还原（原来没有就删）；动过 → 只把还是我们写的值的那几项放回原样。"""
    from . import modelcfg
    r = router._restore_file(rec)
    if r != "changed":
        return r
    f = Path(rec["file"])
    modelcfg.backup(f, "card-before-revert")
    x = Doc(f, rec["fmt"])
    for e in rec["edits"]:
        if _get(x.doc, e["path"]) == e["ours"]:
            _set(x.doc, e["path"], e["orig"], x.tk)
    modelcfg.atomic_write(f, x.dump())
    return "partial"


# ================================================================ 启用 / 停用

def _codex_block(c: dict) -> tuple:
    """→ (provider 表, 模型, 是否走代理)。"""
    via = needs("codex", c)
    keyn = router.TOKEN_KEY if via else key_name("codex", c["kind"])
    cmd = keystore.helper_cmd(keyn)
    t = {"name": "CC Session Sync · " + KIND_NAME[c["kind"]],
         "base_url": (router.base_url() + "/v1") if via else upstream_url(c, "codex"),
         "wire_api": "responses",
         "auth": {"command": cmd[0], "args": cmd[1:], "timeout_ms": 15000, "refresh_interval_ms": 0},
         "request_max_retries": 1 if via else 4}
    return t, c["model"], via


def _codex_edits(c: dict) -> list:
    t, model, via = _codex_block(c)
    ed = [(["model_provider"], router.CODEX_PROFILE), (["model"], model)]
    # 模型目录：Codex 设了 model_catalog_json 就只用那个文件（不再问服务商要列表），桌面端的模型选择器也读它。
    # 走后台服务 → 指向我们写的目录；直连 OpenAI → 先拿掉（用 Codex 自带的 GPT 列表）。停用时都放回原样。
    ed.append((["model_catalog_json"], CATALOG_NAME if via else _ABSENT))
    ed += _fix_edits("codex", c)
    if c["effort"]:                               # 只有用户选了缺省推理等级才写；不选就留着 Codex 自己的（/model 里随时能改）
        ed.append((["model_reasoning_effort"], c["effort"]))
    ed.append((["model_providers", router.CODEX_PROFILE], t))
    return ed


def _claude_edits(c: dict) -> list:
    eff = [(["effortLevel"], c["effort"])] if c.get("effort") in ("low", "medium", "high", "xhigh", "max") else []   # 只是缺省值：/effort 随时能改
    if needs("claude", c):
        return [(["env", "ANTHROPIC_BASE_URL"], router.base_url()), (["env", "ANTHROPIC_AUTH_TOKEN"], router.token()),
                (["env", "ANTHROPIC_API_KEY"], _ABSENT)] + eff + _fix_edits("claude", c)
    ed = [(["apiKeyHelper"], keystore.helper_line(key_name("claude", c["kind"]))),
          (["env", "ANTHROPIC_AUTH_TOKEN"], _ABSENT), (["env", "ANTHROPIC_API_KEY"], _ABSENT)]
    url = c.get("base_url") if c["kind"] == "anthropic" else upstream_url(c, "claude")
    ed.append((["env", "ANTHROPIC_BASE_URL"], url) if url else (["env", "ANTHROPIC_BASE_URL"], _ABSENT))
    return ed + eff + _fix_edits("claude", c)


def check(client: str, c: dict, d: dict):
    if c["kind"] == "default":
        raise ValueError("「默认」不用启用：点「停用」回到客户端自己的登录")
    if not upstream_url(c, client):
        raise ValueError("自定义要填地址")
    if c["kind"] == "custom" and proto(c, client) not in router.PROTOCOLS:
        raise ValueError("自定义要选接口类型")
    if not keystore.get(key_name(client, c["kind"])):
        raise ValueError("先贴 key")
    if not c["model"]:
        raise ValueError("先选模型")
    if (d.get("applied") or {}).get(client):
        raise ValueError(f"「高级 → 模型路由」已经把 {CLIENT_NAME[client]} 指过去了：先在高级里撤销，再用卡片")


def enable(client: str) -> dict:
    """真正写配置（Codex 开着时由 apps.run_or_defer 排队调）。"""
    with router._lock:
        d = router.load()
        c = get(client, d)
        check(client, c, d)
        via = needs(client, c)
        if via:
            router.token()
            keystore.helper_selftest(router.TOKEN_KEY)
        else:
            keystore.helper_selftest(key_name(client, c["kind"]))
        recs = dict((d.get("cards_applied") or {}).get(client) or {})
        files = []
        _self_write(client)
        if client == "codex":
            if via:
                files.append(write_catalog(c))
            else:
                remove_catalog()
            prof = paths.CODEX_HOME / f"{router.CODEX_PROFILE}.config.toml"
            if c["profile_only"]:
                if recs.get("config"):
                    revert_edits(recs.pop("config"))
                ed = [(["model_provider"], router.CODEX_PROFILE)] + [e for e in _codex_edits(c) if e[0] != ["model_provider"]]
                recs["profile"] = apply_edits(prof, "toml", ed, recs.get("profile"), "card-codex-profile")
                files.append(str(prof))
            else:
                if recs.get("profile"):
                    revert_edits(recs.pop("profile"))
                g = paths.CODEX_HOME / "config.toml"
                recs["config"] = apply_edits(g, "toml", _codex_edits(c), recs.get("config"), "card-codex-config")
                files.append(str(g))
            try:
                (paths.CODEX_HOME / "models_cache.json").unlink()   # Codex 的模型目录缓存：删掉让它马上重新拉（/model 里马上看到新列表）
            except OSError:
                pass
        else:
            from . import modelcfg
            f = modelcfg.claude_file("user", None)
            recs["settings"] = apply_edits(f, "json", _claude_edits(c), recs.get("settings"), "card-claude")
            files.append(str(f))
        d = router.load()
        d.setdefault("cards", {}).setdefault(client, c)["enabled"] = True
        d["cards"][client].update({k: c[k] for k in c if k != "enabled"})
        d.setdefault("cards_applied", {})[client] = recs
        if via and d.get("autostart", True):
            try:
                router.login_set(True)
            except OSError:
                pass
        router.save(d)
    ok = router.ensure_running() if via else True
    return {"ok": True, "files": files, "proxy": via, "proxy_ok": ok, "client": client,
            "hint": None if ok else "后台转换没起来：看「诊断自检」"}


def disable(client: str) -> dict:
    with router._lock:
        d = router.load()
        recs = (d.get("cards_applied") or {}).get(client) or {}
        _self_write(client)
        res = [{"file": r["file"], "result": revert_edits(r)} for r in recs.values()]
        if client == "codex":
            remove_catalog()
        d.setdefault("cards_applied", {}).pop(client, None)
        if client in (d.get("cards") or {}):
            d["cards"][client]["enabled"] = False
        router.save(d)
        still = router.needed(d)
    if not still:
        try:
            router.login_set(False)
        except OSError:
            pass
        router.shutdown_background()
    return {"ok": True, "files": res, "client": client}


def run(client: str, act: str) -> dict:
    """页面的「启用 / 停用」：Codex 桌面端开着就排队（退出后做、做完重开）。Claude Code 每次新会话读 settings.json，直接改。"""
    from . import apps
    d = router.load()
    if act == "enable":
        check(client, get(client, d), d)
    if client == "codex":
        return apps.run_or_defer("codex", "route-card-" + act, {"client": client},
                                 f"{'启用' if act == 'enable' else '停用'}「Codex 使用 {KIND_NAME[get(client, d)['kind']]}」")
    r = enable(client) if act == "enable" else disable(client)
    return dict(r, deferred=False)


# ================================================================ 给 Codex 的模型目录（/model 里能选 Claude 模型、能调推理等级）

LEVELS = {"none": "不思考，最快", "low": "想得少，快", "medium": "日常够用", "high": "复杂问题想得更深", "xhigh": "更深", "max": "最深，最慢"}
LEVELS_CODEX = ("none", "low", "medium", "high", "xhigh", "max")
# Codex 0.16x 内置目录里在 /model 列出来的模型（卡片走 Claude 时它们用不了）
CODEX_BUILTIN = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
                 "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2", "gpt-5.2-codex", "gpt-5.1-codex-max", "gpt-5.1-codex-mini")


def catalog_models(c: dict) -> list:
    """卡片的默认模型在最前；然后是拉到的模型列表（Claude：每个系列按新到旧），没拉过就用常见的几个。"""
    ms = [m for m in (c.get("models") or []) if isinstance(m, str) and not SKIP.search(m)]
    if c["kind"] == "anthropic" or (c["kind"] == "custom" and proto(c, "codex") == "anthropic"):
        ms = [m for m in ms if m.startswith("claude-")] or ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        ms.sort(key=lambda m: (0 if "opus" in m else 1 if "sonnet" in m else 2 if "haiku" in m else 3, tuple(-x for x in _ver(m)[0]), not _ver(m)[1]))
    out = []
    for m in [c.get("model")] + ms:
        if m and m not in out:
            out.append(m)
    return out[:80]


CATALOG_NAME = "ccss-models-cc-route.json"       # 相对名：Codex 按 config.toml 所在目录（CODEX_HOME）解析
DEFAULT_CTX = 128000
KNOWN_CTX = (("claude-", 200000), ("gpt-6", 272000), ("gpt-5.6", 272000), ("gpt-5.5", 272000), ("gpt-5.4", 272000))


def catalog_path() -> Path:
    return paths.CODEX_HOME / CATALOG_NAME


def levels_for(c: dict, slug: str) -> list:
    """这个模型在 Codex 里能选的推理等级（按 effort.py 的对应表：服务商不支持的级别不列出来）。"""
    m = slug.lower()
    k = c.get("kind")
    style = presets.get(k)["style"] if presets.get(k) else ("anthropic" if proto(c, "codex") == "anthropic" else "generic")
    if style == "anthropic":
        return ["none", "low", "medium", "high", "xhigh"] + (["max"] if thinking_mode(c, slug, "codex") == "adaptive" else [])
    if style == "xai":
        return ["low", "medium", "high", "xhigh"]
    if style == "gemini":
        return (["none"] if "2.5" in m and "pro" not in m else []) + ["low", "medium", "high"]
    if style == "deepseek":
        return ["none", "low", "high", "max"]
    if style == "glm":
        from .effort import _ver as ev
        v = ev(m)
        return ["low", "high", "max"] if v >= (5, 3) else ["none", "low", "medium", "high", "xhigh", "max"] if v >= (5, 2) else ["none", "high"]
    if style == "kimi":
        return ["low", "high", "max"] if m.startswith("kimi-k3") else ["none", "high"] if m.startswith("kimi-k2.6") else ["high"]
    if style == "qwen":
        return ["none", "low", "medium", "xhigh"] if m.startswith("qwen3.8") else ["none", "low", "medium", "high", "xhigh"]
    return ["none", "low", "medium", "high", "xhigh"]


def ctx_for(c: dict, slug: str) -> int:
    v = (c.get("ctx") or {}).get(slug)
    if v:
        return int(v)
    for pre, n in KNOWN_CTX:
        if slug.lower().startswith(pre):
            return n
    return DEFAULT_CTX


_UP = {"gpt": "GPT", "glm": "GLM", "ai": "AI", "vl": "VL", "oss": "OSS", "r1": "R1", "v3": "V3", "v4": "V4", "k2": "K2", "k3": "K3"}


def pretty(m: str) -> str:
    """模型 ID → 选择器里好认的名字：claude-opus-5-5 → Claude Opus 5.5，deepseek-v4-pro → DeepSeek V4 Pro，
    kimi-k3.1-pro → Kimi K3.1 Pro，glm-5.3 → GLM-5.3，qwen3.8-max → Qwen3.8 Max，anthropic/claude-opus-5.5 → Claude Opus 5.5（anthropic）。"""
    vendor = ""
    if "/" in m:
        vendor, m2 = m.rsplit("/", 1)
        return pretty(m2) + f"（{vendor}）"
    if m.startswith("claude-"):
        parts = m.split("-")[1:]
        fam = [x for x in parts if not x.isdigit()]
        ver = [x for x in parts if x.isdigit() and len(x) < 8]
        return "Claude " + " ".join(x.capitalize() for x in fam) + (" " + ".".join(ver) if ver else "")
    if m.startswith("gpt-"):
        return "GPT-" + m[4:]
    if m.startswith("glm-"):
        return "GLM-" + m[4:].replace("-", " ").title().replace(" ", "-") if m[4:] else "GLM"
    brand = {"deepseek": "DeepSeek", "kimi": "Kimi", "grok": "Grok", "gemini": "Gemini", "qwen": "Qwen", "moonshot": "Moonshot"}
    out = []
    for i, w in enumerate(m.split("-")):
        lw = w.lower()
        if i == 0:
            b = next((v for kk, v in brand.items() if lw.startswith(kk)), None)
            out.append(b + w[len(b):] if b else w[:1].upper() + w[1:])
        elif lw in _UP or (lw[:1] in "vkr" and lw[1:2].isdigit()):
            out.append(_UP.get(lw, w.upper()))
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def _base_prompt() -> str:
    try:
        return (Path(__file__).with_name("codex_prompt.md")).read_text(encoding="utf-8")
    except OSError:
        return "You are a coding agent running in the Codex CLI. Be precise, safe, and helpful."


def model_info(slug: str, prio: int, default_effort: str, levels: list | None = None, ctx: int | None = None) -> dict:
    """Codex 的 ModelInfo（openai/codex rust-v0.164.0 codex-rs/protocol/src/openai_models.rs），也是 model_catalog_json 文件里每一项的格式。
    和 Codex 给不认识的模型用的兜底元数据一样（工具、提示词都不变），多了：在选择器里列出来、能选推理等级、好认的名字、上下文长度。
    老版本 Codex（0.162 起）要的几个字段也带上（additional_speed_tiers / service_tiers / supports_reasoning_summaries），多出来的字段新版本会忽略。"""
    levels = levels or list(LEVELS_CODEX)
    de = default_effort if default_effort in levels else ("medium" if "medium" in levels else "high" if "high" in levels else levels[0])
    ctx = ctx or DEFAULT_CTX
    return {"slug": slug, "display_name": pretty(slug), "description": "用你的 API key（CC Session Sync）",
            "default_reasoning_level": de,
            "supported_reasoning_levels": [{"effort": e, "description": LEVELS.get(e, e)} for e in levels],
            "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True, "priority": prio,
            "additional_speed_tiers": [], "service_tiers": [],
            "availability_nux": None, "upgrade": None, "base_instructions": _base_prompt(),
            "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False, "include_apps_usage_instructions": False,
            "supports_reasoning_summary_parameter": True, "supports_reasoning_summaries": True, "default_reasoning_summary": "auto",
            "support_verbosity": False, "default_verbosity": None, "apply_patch_tool_type": None, "web_search_tool_type": "text",
            "truncation_policy": {"mode": "bytes", "limit": 10000}, "supports_image_detail_original": False, "supports_parallel_tool_calls": False,
            "context_window": ctx, "max_context_window": ctx, "effective_context_window_percent": 95,
            "experimental_supported_tools": [], "input_modalities": ["text", "image"], "supports_search_tool": False}


def catalog_doc(c: dict) -> dict:
    """写进 ~/.codex/ccss-models-cc-route.json 的内容：{"models": [ModelInfo…]}，卡片的默认模型排第一。"""
    return {"models": [model_info(m, i + 1, c.get("effort") or "", levels_for(c, m), ctx_for(c, m)) for i, m in enumerate(catalog_models(c))]}


def write_catalog(c: dict) -> str:
    from . import modelcfg
    f = catalog_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    modelcfg.atomic_write(f, (json.dumps(catalog_doc(c), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return str(f)


def remove_catalog():
    try:
        catalog_path().unlink()
    except OSError:
        pass


def codex_catalog(d: dict | None = None) -> list:
    """本机后台服务的 /v1/models（没设 model_catalog_json 的 Codex 用）。和目录文件同一份，另外把 Codex 自带的 GPT 模型藏起来。"""
    d = d or router.cfg()
    c = get("codex", d)
    if not (c["enabled"] and needs("codex", c)):
        return []
    out = catalog_doc(c)["models"]
    ms = {m["slug"] for m in out}
    for s_ in CODEX_BUILTIN:
        if s_ not in ms:
            out.append(dict(model_info(s_, 900, ""), visibility="hide"))
    return out


# ---------------------------------------------------------------- 别的设置还指着这家没有的模型（review_model 等）：提醒 + 一键改成卡片的模型

CODEX_REFS = ((["review_model"], "审查（/review）用的模型 review_model"), (["agents", "default_subagent_model"], "子代理默认模型 agents.default_subagent_model"))
CLAUDE_REFS = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
               "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")


def _cfg_doc(client: str, c: dict):
    try:
        if client == "codex":
            f = paths.CODEX_HOME / (f"{router.CODEX_PROFILE}.config.toml" if c.get("profile_only") else "config.toml")
            return Doc(f, "toml").doc, f
        from . import modelcfg
        f = modelcfg.claude_file("user", None)
        return Doc(f, "json").doc, f
    except Exception:
        return None, None


def ref_warnings(client: str, c: dict | None = None) -> list:
    """别的设置引用了这家列表里没有的模型 → [{path, value, label, fix}]。不挡启用，只提醒。"""
    c = c or get(client)
    if c["kind"] == "default" or not c.get("enabled"):
        return []
    have = set(catalog_models(c)) | set(c.get("models") or [])
    if not c.get("models"):
        return []
    doc, _f = _cfg_doc(client, c)
    if doc is None:
        return []
    out = []
    fixed = {json.dumps(x.get("path")) for x in c.get("fix_refs") or []}

    def bad(v):
        if not isinstance(v, str) or not v or v in have:
            return False
        if client == "claude" and (v.lower() in ("opus", "sonnet", "haiku", "default", "fable") or v.lower().startswith("claude-")):
            return False                               # Claude 名字会被换成卡片的模型
        return True
    if client == "codex":
        for path, label in CODEX_REFS:
            v = _get(doc, path)
            if v is not _ABSENT and bad(v) and json.dumps(path) not in fixed:
                out.append({"path": path, "value": v, "label": label, "fix": "model"})
        env = (_get(doc, ["shell_environment_policy", "set"]))
        if isinstance(env, dict):
            for k, v in env.items():
                if "MODEL" in k.upper() and bad(v) and json.dumps(["shell_environment_policy", "set", k]) not in fixed:
                    out.append({"path": ["shell_environment_policy", "set", k], "value": v, "label": f"shell 环境变量 {k}", "fix": "model"})
        mcw = _get(doc, ["model_context_window"])
        if isinstance(mcw, int) and mcw > ctx_for(c, c["model"]) and json.dumps(["model_context_window"]) not in fixed:
            out.append({"path": ["model_context_window"], "value": mcw, "label": f"上下文长度 model_context_window（{c['model']} 大约 {ctx_for(c, c['model']):,}）", "fix": "absent"})
    else:
        env = _get(doc, ["env"])
        for k in CLAUDE_REFS:
            v = env.get(k) if isinstance(env, dict) else None
            if bad(v) and json.dumps(["env", k]) not in fixed:
                out.append({"path": ["env", k], "value": v, "label": f"环境变量 {k}", "fix": "model"})
    return out


def _fix_edits(client: str, c: dict) -> list:
    return [(x["path"], _ABSENT if x.get("fix") == "absent" else c["model"]) for x in c.get("fix_refs") or []
            if isinstance(x, dict) and isinstance(x.get("path"), list)]


def fix_refs(client: str, direct: bool = False) -> dict:
    """「一键改成卡片的模型」：记进卡片（下次启用 / 停用一起管，停用时放回原样），再重新启用一次。"""
    with router._lock:
        d = router.load()
        c = get(client, d)
        ws = ref_warnings(client, c)
        if not ws:
            return {"ok": True, "fixed": 0}
        c["fix_refs"] = (c.get("fix_refs") or []) + [{"path": w["path"], "fix": w["fix"]} for w in ws]
        d.setdefault("cards", {})[client] = c
        router.save(d)
    return dict(enable(client) if direct else run(client, "enable"), fixed=len(ws))


def _self_write(client: str):
    """告诉「账号切换」监视：接下来一会儿 Codex 配置的变化是我们自己写的（启用 / 停用卡片），别当成切换账号。"""
    try:
        from . import watch
        watch.note_self(client)
    except Exception:
        pass


# ================================================================ 页面 / 自检

def view(client: str, d: dict | None = None, live: dict | None = None) -> dict:
    d = d or router.load()
    c = get(client, d)
    rec = (d.get("cards_applied") or {}).get(client) or {}
    out = dict(c, client=client, kind_name=KIND_NAME[c["kind"]], needs_proxy=needs(client, c) if c["kind"] != "default" else False,
               upstream=upstream_url(c, client), protocol_used=proto(c, client), keys={k: _mask(key_name(client, k)) for k in KINDS[client] if k != "default"},
               files=[r["file"] for r in rec.values()], drift=False, drift_keys=[],
               catalog=str(catalog_path()) if client == "codex" and c["enabled"] and needs(client, c) else None)
    # 「配置被改过」只看我们写的那几项还在不在：文件别处被改（Codex 记项目信任、桌面端偏好等）不算；
    # model / model_reasoning_effort 被 Codex 的 /model 改掉也不算（那是用户在客户端里切换）。
    for r in rec.values():
        if router._sha(router._read(Path(r["file"]))) == r.get("sha"):
            continue
        try:
            doc = Doc(Path(r["file"]), r["fmt"]).doc
        except Exception:
            continue
        for e in r.get("edits") or []:
            if e["path"] in (["model"], ["model_reasoning_effort"], ["effortLevel"]):
                continue
            if _get(doc, e["path"]) != e["ours"]:
                out["drift_keys"].append(".".join(e["path"]))
    out["drift"] = bool(out["drift_keys"])
    if client == "codex" and out["catalog"] and not catalog_path().exists():
        out["drift"] = True
        out["drift_keys"].append(CATALOG_NAME)
    out["warnings"] = ref_warnings(client, c)
    return out


def overview() -> dict:
    d = router.load()
    need = needs_proxy(d)
    st = router.live_status() if need or d.get("applied") else {"running": False}
    return {"codex": view("codex", d), "claude": view("claude", d), "presets": presets.view(),
            "proxy": {"needed": need, "running": bool(st.get("running")), "port": st.get("port") or d.get("port"), "where": st.get("where"),
                      "login": router.login_get(), "autostart": d.get("autostart", True), "requests": st.get("requests", 0),
                      "errors": st.get("errors", 0), "errors_recent": [x for x in st.get("errors_recent") or [] if str(x.get("provider") or "").startswith("card-") or not x.get("provider")][:5]}}


def fetch_models(client: str, b: dict) -> dict:
    """「拉取模型」：用刚贴的 key（没贴就用已存的）。"""
    kind = LEGACY.get(b.get("kind") or get(client)["kind"], b.get("kind") or get(client)["kind"])
    c = dict(get(client), **{k: v for k, v in b.items() if k in ("kind", "base_url", "protocol", "region")})
    c["kind"] = kind
    if kind == "custom" and c.get("base_url") and b.get("protocol") in (None, "", "auto"):
        c["protocol"], c["base_url"] = presets.detect(c["base_url"])
    sec = (b.get("secret") or "").strip() or keystore.get(key_name(client, kind)) or ""
    if presets.get(kind) and not b.get("base_url"):
        pr, url = presets.models_endpoint(kind, c.get("region"))    # 列表从它家的 OpenAI 兼容 /models 拉（Claude Code 走 Anthropic 接口时也一样）
    else:
        pr, url = proto(c, client) or "chat", upstream_url(c, client)
    r = router.list_models(draft={"protocol": pr, "base_url": url, "secret": sec})
    if r.get("ok"):
        r["models"] = [m for m in r["models"] if not SKIP.search(m)]
        r["ctx"] = {k: v for k, v in (r.get("ctx") or {}).items() if k in r["models"]}
        r["default"], r["small"] = defaults_for(kind, r["models"])
        if kind == "custom":
            r["protocol"], r["base_url"] = c.get("protocol"), c.get("base_url")
        if kind == "anthropic":
            r["models"] = sorted(r["models"], key=lambda m: (0 if "opus" in m else 1 if "sonnet" in m else 2 if "haiku" in m else 3, tuple(-x for x in _ver(m)[0])))
        # 「刷新」时卡片正在用同一家：马上更新卡片的模型列表；Codex 的目录文件也重写（新开的 Codex 窗口 / 会话就能看到）
        cur = get(client)
        if cur["enabled"] and cur["kind"] == kind and not b.get("secret") and (b.get("region") or "") == (cur.get("region") or "") \
                and (b.get("base_url") or "") == (cur.get("base_url") or ""):
            with router._lock:
                d = router.load()
                cc = get(client, d)
                cc["models"], cc["ctx"] = r["models"][:300], r["ctx"]
                d.setdefault("cards", {})[client] = cc
                router.save(d)
            if client == "codex" and needs(client, cc):
                write_catalog(cc)
                r["catalog_updated"] = True
    return r


def test(client: str) -> dict:
    """「测试」：要转换的走一遍本机代理（真的经过后台进程），不转换的直接打上游。"""
    d = router.load()
    c = get(client, d)
    if c["kind"] == "default":
        return {"ok": True, "note": "用的是客户端自己的登录"}
    p = provider(client, c)
    if not needs(client, c):
        return router.test_provider_obj(p, c["model"])
    if not router.ensure_running():
        return {"ok": False, "reason": "后台转换没在跑，也拉不起来（看「诊断自检」）"}
    return router.test_via_proxy(client, c["model"])


# ================================================================ 服务商（0.3.4：配一次，两个 Agent 都能用）

PROV_FIELDS = ("region", "base_url", "protocol", "models", "ctx")


def _prov_merge(d: dict, kind: str, vals: dict):
    pv = d.setdefault("provs", {}).setdefault(kind, {})
    for f, v in vals.items():
        if f in ("models", "ctx") and not v:
            continue                                         # 空列表不覆盖已经拉到的
        if f == "protocol" and kind != "custom":
            continue
        pv[f] = v


def prov_get(kind: str, d: dict | None = None) -> dict:
    """服务商的设置：自己存的优先；老版本只存在卡片里的，从卡片里拿。"""
    d = d or router.load()
    pv = dict((d.get("provs") or {}).get(kind) or {})
    for cl in CLIENTS:
        c = get(cl, d)
        if c["kind"] == kind:
            for f in PROV_FIELDS:
                if not pv.get(f) and c.get(f) and not (f == "protocol" and kind != "custom"):
                    pv[f] = c[f]
    return {"region": pv.get("region") or "", "base_url": pv.get("base_url") or "", "protocol": pv.get("protocol") or "",
            "models": list(pv.get("models") or []), "ctx": dict(pv.get("ctx") or {})}


def _host(u: str) -> str:
    m = re.match(r"^[a-z]+://([^/]+)", u or "")
    return m.group(1) if m else ""


def prov_view(kind: str, d: dict | None = None) -> dict:
    d = d or router.load()
    pv = prov_get(kind, d)
    pre = presets.get(kind)
    fake = {"kind": kind, "region": pv["region"], "base_url": pv["base_url"], "protocol": pv["protocol"] or "chat", "ctx": pv["ctx"]}
    n = key_name("codex", kind)
    url = pv["base_url"] or (presets.region_of(pre, pv["region"]).get(pre["models"]) or next(iter(v for k, v in presets.region_of(pre, pv["region"]).items() if not k.startswith("_"))) if pre else "")
    used = [cl for cl in CLIENTS if get(cl, d)["enabled"] and get(cl, d)["kind"] == kind]
    return dict(pv, id=kind, name=KIND_NAME[kind], vendor=(pre or {}).get("vendor", ""), host=_host(url), relay=bool(pre and pv["base_url"]),
                key=_mask(n), key_hint=(pre or {}).get("key", "API Key"), src=(pre or {}).get("src", ""),
                regions=[{"id": k, "label": v.get("_label") or ""} for k, v in pre["regions"].items()] if pre else [],
                labels={m: pretty(m) for m in pv["models"]}, levels={m: levels_for(fake, m) for m in pv["models"]},
                default=defaults_for(kind if kind != "custom" else "", pv["models"])[0] if pv["models"] else "", used_by=used)


def prov_list() -> dict:
    d = router.load()
    kinds = [p["id"] for p in presets.PRESETS] + ["custom"]
    return {"providers": [prov_view(k, d) for k in kinds], "cards": {cl: view(cl, d) for cl in CLIENTS}}


def prov_save(b: dict) -> dict:
    kind = LEGACY.get(str(b.get("kind") or ""), str(b.get("kind") or ""))
    if kind not in _ALL or kind == "default":
        raise ValueError("不支持这个服务商")
    sec = (b.get("secret") or "").strip()
    if sec and any(ch in sec for ch in "\r\n \t"):
        raise ValueError("key 里不能有空格或换行")
    vals = {}
    if "region" in b and presets.get(kind):
        r = str(b.get("region") or "")
        vals["region"] = r if r in presets.get(kind)["regions"] else ""
    proto_v = b.get("protocol") if b.get("protocol") in router.PROTOCOLS else None
    if "base_url" in b:
        u = str(b.get("base_url") or "").strip()
        if u and kind == "custom" and not proto_v:
            proto_v, u = presets.detect(u)
        vals["base_url"] = router._clean_url(u) if u else ""
    if kind == "custom":
        if not (vals.get("base_url") or prov_get(kind)["base_url"]):
            raise ValueError("自定义要填地址")
        if proto_v:
            vals["protocol"] = proto_v
    if isinstance(b.get("models"), list):
        vals["models"] = [str(x) for x in b["models"] if isinstance(x, str) and not SKIP.search(x)][:300]
    if isinstance(b.get("ctx"), dict):
        vals["ctx"] = {str(k): int(v) for k, v in b["ctx"].items() if isinstance(v, (int, float)) and 1000 <= v <= 100_000_000}
    if not sec and not keystore.get(key_name("codex", kind)):
        raise ValueError("先贴 key")
    if sec:
        put_key(kind, sec)
    with router._lock:
        d = router.load()
        _prov_merge(d, kind, vals)
        router.save(d)
    return prov_view(kind)


def prov_models(b: dict) -> dict:
    """服务商页「检查 key / 刷新模型」：不改任何卡片。"""
    kind = LEGACY.get(str(b.get("kind") or ""), str(b.get("kind") or ""))
    if kind not in _ALL or kind == "default":
        raise ValueError("不支持这个服务商")
    pv = prov_get(kind)
    c = {"kind": kind, "region": b.get("region", pv["region"]) or "", "base_url": b.get("base_url", pv["base_url"]) or "",
         "protocol": b.get("protocol") or pv["protocol"] or ""}
    if kind == "custom" and c["base_url"] and c["protocol"] in ("", "auto"):
        c["protocol"], c["base_url"] = presets.detect(c["base_url"])
    sec = (b.get("secret") or "").strip() or keystore.get(key_name("codex", kind)) or ""
    if not sec:
        return {"ok": False, "reason": "先贴 key"}
    if presets.get(kind) and not c["base_url"]:
        pr, url = presets.models_endpoint(kind, c["region"])
    else:
        pr, url = c["protocol"] or "chat", c["base_url"]
    if not url:
        return {"ok": False, "reason": "先填地址"}
    r = router.list_models(draft={"protocol": pr, "base_url": url, "secret": sec})
    if r.get("ok"):
        r["models"] = [m for m in r["models"] if not SKIP.search(m)]
        if kind == "anthropic":
            r["models"] = sorted(r["models"], key=lambda m: (0 if "opus" in m else 1 if "sonnet" in m else 2 if "haiku" in m else 3, tuple(-x for x in _ver(m)[0])))
        r["ctx"] = {k: v for k, v in (r.get("ctx") or {}).items() if k in r["models"]}
        r["default"], r["small"] = defaults_for(kind if kind != "custom" else "", r["models"])
        r["labels"] = {m: pretty(m) for m in r["models"]}
        if kind == "custom":
            r["protocol"], r["base_url"] = c["protocol"], c["base_url"]
    return r


def prov_delete(b: dict) -> dict:
    kind = LEGACY.get(str(b.get("kind") or ""), str(b.get("kind") or ""))
    if kind not in _ALL or kind == "default":
        raise ValueError("不支持这个服务商")
    d = router.load()
    on = [CLIENT_NAME[cl] for cl in CLIENTS if get(cl, d)["enabled"] and get(cl, d)["kind"] == kind]
    if on:
        raise ValueError(f"{'、'.join(on)} 正在用它：先停用再删")
    for n in _key_names(kind):
        if keystore.get(n):
            keystore.delete(n)
    with router._lock:
        d = router.load()
        (d.get("provs") or {}).pop(kind, None)
        router.save(d)
    return {"ok": True}

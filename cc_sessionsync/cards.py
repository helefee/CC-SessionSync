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

from . import keystore, paths, router

CLIENTS = ("codex", "claude")
KINDS = {"codex": ("default", "claude", "openai", "custom"), "claude": ("default", "openai", "claude", "custom")}
KIND_NAME = {"default": "默认", "claude": "Claude API Key", "openai": "OpenAI API Key", "custom": "自定义"}
CLIENT_NAME = {"codex": "Codex", "claude": "Claude Code"}
OFFICIAL = {"claude": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}
DEF_MODEL = {"claude": ("claude-opus-5-5", "claude-haiku-5-5"), "openai": ("gpt-5.5", "gpt-5.4-mini")}
EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max")
_ABSENT = {"__absent__": True}


# ================================================================ 配置

def _blank(client: str) -> dict:
    return {"kind": "default", "base_url": "", "protocol": "anthropic" if client == "codex" else "responses", "model": "", "small_model": "", "sonnet_model": "",
            "effort": "", "profile_only": False, "enabled": False, "models": []}


def get(client: str, d: dict | None = None) -> dict:
    d = d or router.cfg()
    c = dict(_blank(client), **((d.get("cards") or {}).get(client) or {}))
    return c


def proto(c: dict) -> str | None:
    if c["kind"] == "claude":
        return "anthropic"
    if c["kind"] == "openai":
        return "responses"
    if c["kind"] == "custom":
        return c.get("protocol") or None
    return None


def native(client: str) -> str:
    return "responses" if client == "codex" else "anthropic"


def needs(client: str, c: dict) -> bool:
    """这张卡要不要后台代理做协议转换。"""
    return c["kind"] != "default" and proto(c) != native(client)


def needs_proxy(d: dict | None = None) -> bool:
    d = d or router.load()
    return any(get(x, d)["enabled"] and needs(x, get(x, d)) for x in CLIENTS)


def key_name(client: str, kind: str) -> str:
    return keystore.check_name(f"card-{client}-{kind}")


def upstream_url(c: dict) -> str:
    if c["kind"] == "custom":
        return c.get("base_url") or ""
    return c.get("base_url") or OFFICIAL.get(c["kind"], "")


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


def defaults_for(kind: str, models: list | None = None) -> tuple:
    models = models or []
    if kind == "claude":
        return (latest(models, "opus") or DEF_MODEL["claude"][0], latest(models, "haiku") or DEF_MODEL["claude"][1])
    if kind == "openai":
        big = [m for m in models if m.startswith("gpt-") and "mini" not in m and "nano" not in m and not re.search(r"audio|realtime|image|search|tts|transcribe", m)]
        return (max(big, key=_ver) if big else DEF_MODEL["openai"][0], latest([m for m in models if m.startswith("gpt-")], "mini") or DEF_MODEL["openai"][1])
    return ((models or [""])[0], "")


def save_card(client: str, b: dict) -> dict:
    """保存卡片设置（不启用）。b.secret 有值就存进凭据管理器（明文不落盘、不回传）。"""
    if client not in CLIENTS:
        raise ValueError("只有 codex / claude 两张卡")
    with router._lock:
        d = router.load()
        c = get(client, d)
        kind = b.get("kind", c["kind"])
        if kind not in KINDS[client]:
            raise ValueError("不支持这个选项")
        c["kind"] = kind
        if "base_url" in b:
            u = str(b.get("base_url") or "").strip()
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
        if kind == "custom" and not c["base_url"] and c["enabled"]:
            raise ValueError("自定义要填 Base URL")
        sec = (b.get("secret") or "").strip()
        if sec and kind != "default":
            if any(ch in sec for ch in "\r\n \t"):
                raise ValueError("key 里不能有空格或换行")
            keystore.put(key_name(client, kind), sec, f"CC Session Sync · {CLIENT_NAME[client]} 使用 {KIND_NAME[kind]}")
        if kind != "default" and not c["model"]:
            c["model"], small = defaults_for(kind if kind != "custom" else "", c["models"])
            c["small_model"] = c["small_model"] or small
        d.setdefault("cards", {})[client] = c
        router.save(d)
    return view(client)


def provider(client: str, c: dict, model: str | None = None) -> dict:
    """卡片 → 路由内部用的供应商（不存进供应商列表，只在代理里用）。"""
    kind = c["kind"]
    p = router.clean_provider({"id": f"card-{client}", "name": f"{CLIENT_NAME[client]} · {KIND_NAME[kind]}", "protocol": proto(c) or "anthropic",
                               "base_url": upstream_url(c) or "http://invalid.local", "keys": [{"id": "k1", "name": key_name(client, kind)}],
                               "effort": "", "thinking": thinking_mode(c, model)}, set())
    p["card"] = client
    return p


def thinking_mode(c: dict, model: str | None = None) -> str:
    """Claude 的思考参数：新模型（Opus / Sonnet 4.6 起、Haiku 5 起）用 adaptive + effort；老模型用 budget_tokens。"""
    m = (model or c.get("model") or "").lower()
    if proto(c) != "anthropic" or not m.startswith("claude-"):
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
    if not upstream_url(c) or not keystore.get(key_name(client, c["kind"])):
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
        ok = w and (w == c["model"] or w in (c.get("models") or []) or
                    (proto(c) == "anthropic" and w.startswith("claude-")) or (proto(c) != "anthropic" and not w.startswith("claude-")))
        m = w if ok else c["model"]                 # Codex 桌面端可能带着自己的 gpt-… 来：换成卡片的模型
    return [(provider(client, c, m or w), m or w, None)] if (m or w) else []


def not_ready_msg(client: str, d: dict | None = None) -> str:
    c = get(client, d)
    if not upstream_url(c):
        return f"CC Session Sync：「{CLIENT_NAME[client]} 使用…」卡片没填 Base URL。打开 CC Session Sync → 供应商 补上，或者点「停用」。"
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
         "base_url": (router.base_url() + "/v1") if via else upstream_url(c),
         "wire_api": "responses",
         "auth": {"command": cmd[0], "args": cmd[1:], "timeout_ms": 15000, "refresh_interval_ms": 0},
         "request_max_retries": 1 if via else 4}
    return t, c["model"], via


def _codex_edits(c: dict) -> list:
    t, model, via = _codex_block(c)
    ed = [(["model_provider"], router.CODEX_PROFILE), (["model"], model)]
    if c["effort"]:                               # 只有用户选了缺省推理等级才写；不选就留着 Codex 自己的（/model 里随时能改）
        ed.append((["model_reasoning_effort"], c["effort"]))
    ed.append((["model_providers", router.CODEX_PROFILE], t))
    return ed


def _claude_edits(c: dict) -> list:
    eff = [(["effortLevel"], c["effort"])] if c.get("effort") in ("low", "medium", "high", "xhigh", "max") else []   # 只是缺省值：/effort 随时能改
    if needs("claude", c):
        return [(["env", "ANTHROPIC_BASE_URL"], router.base_url()), (["env", "ANTHROPIC_AUTH_TOKEN"], router.token()),
                (["env", "ANTHROPIC_API_KEY"], _ABSENT)] + eff
    ed = [(["apiKeyHelper"], keystore.helper_line(key_name("claude", c["kind"]))),
          (["env", "ANTHROPIC_AUTH_TOKEN"], _ABSENT), (["env", "ANTHROPIC_API_KEY"], _ABSENT)]
    url = c.get("base_url") if c["kind"] == "claude" else upstream_url(c)
    ed.append((["env", "ANTHROPIC_BASE_URL"], url) if url else (["env", "ANTHROPIC_BASE_URL"], _ABSENT))
    return ed + eff


def check(client: str, c: dict, d: dict):
    if c["kind"] == "default":
        raise ValueError("「默认」不用启用：点「停用」回到客户端自己的登录")
    if not upstream_url(c):
        raise ValueError("自定义要填 Base URL")
    if c["kind"] == "custom" and proto(c) not in router.PROTOCOLS:
        raise ValueError("自定义要选接口协议")
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
        if client == "codex":
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
        res = [{"file": r["file"], "result": revert_edits(r)} for r in recs.values()]
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


def pretty(m: str) -> str:
    """claude-opus-5-5 → Claude Opus 5.5；gpt-5.4-mini → GPT-5.4-mini。"""
    if m.startswith("claude-"):
        parts = m.split("-")[1:]
        fam = [x for x in parts if not x.isdigit()]
        ver = [x for x in parts if x.isdigit() and len(x) < 8]
        return "Claude " + " ".join(x.capitalize() for x in fam) + (" " + ".".join(ver) if ver else "")
    if m.startswith("gpt-"):
        return "GPT-" + m[4:]
    return m


def catalog_models(c: dict) -> list:
    """卡片的默认模型在最前；然后是拉到的模型列表（Claude：每个系列按新到旧），没拉过就用常见的几个。"""
    ms = [m for m in (c.get("models") or []) if isinstance(m, str)]
    if proto(c) == "anthropic":
        ms = [m for m in ms if m.startswith("claude-")] or ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
        ms.sort(key=lambda m: (0 if "opus" in m else 1 if "sonnet" in m else 2 if "haiku" in m else 3, tuple(-x for x in _ver(m)[0]), not _ver(m)[1]))
    out = []
    for m in [c.get("model")] + ms:
        if m and m not in out:
            out.append(m)
    return out[:40]


def _base_prompt() -> str:
    try:
        return (Path(__file__).with_name("codex_prompt.md")).read_text(encoding="utf-8")
    except OSError:
        return "You are a coding agent running in the Codex CLI. Be precise, safe, and helpful."


def model_info(slug: str, prio: int, default_effort: str) -> dict:
    """Codex 0.16x 的 ModelInfo。和 Codex 给不认识的模型用的兜底元数据一样（工具、提示词都不变），
    只多了两样：在 /model 里列出来（visibility=list），以及能选推理等级（supported_reasoning_levels）。"""
    return {"slug": slug, "display_name": pretty(slug), "description": "用你的 API key（CC Session Sync）",
            "default_reasoning_level": default_effort or "medium",
            "supported_reasoning_levels": [{"effort": e, "description": LEVELS[e]} for e in LEVELS_CODEX],
            "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True, "priority": prio,
            "availability_nux": None, "upgrade": None, "base_instructions": _base_prompt(),
            "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False, "include_apps_usage_instructions": False,
            "supports_reasoning_summary_parameter": True, "default_reasoning_summary": "auto",
            "support_verbosity": False, "default_verbosity": None, "apply_patch_tool_type": None, "web_search_tool_type": "text",
            "truncation_policy": {"mode": "bytes", "limit": 10000}, "supports_image_detail_original": False,
            "context_window": 200000, "max_context_window": 200000, "effective_context_window_percent": 95,
            "experimental_supported_tools": [], "input_modalities": ["text", "image"], "supports_search_tool": False}


def codex_catalog(d: dict | None = None) -> list:
    d = d or router.cfg()
    c = get("codex", d)
    if not (c["enabled"] and needs("codex", c)):
        return []
    ms = catalog_models(c)
    out = [model_info(m, i + 1, c.get("effort") or "") for i, m in enumerate(ms)]
    for s in CODEX_BUILTIN:                          # Codex 自带的 GPT 模型在这里用不了：同名条目盖掉、藏起来，/model 里只剩能用的
        if s not in ms:
            out.append(dict(model_info(s, 900, ""), visibility="hide"))
    return out


# ================================================================ 页面 / 自检

def view(client: str, d: dict | None = None, live: dict | None = None) -> dict:
    d = d or router.load()
    c = get(client, d)
    rec = (d.get("cards_applied") or {}).get(client) or {}
    out = dict(c, client=client, kind_name=KIND_NAME[c["kind"]], needs_proxy=needs(client, c) if c["kind"] != "default" else False,
               upstream=upstream_url(c), keys={k: _mask(key_name(client, k)) for k in KINDS[client] if k != "default"},
               files=[r["file"] for r in rec.values()], drift=False)
    for r in rec.values():
        if router._sha(router._read(Path(r["file"]))) != r.get("sha"):
            out["drift"] = True
    return out


def overview() -> dict:
    d = router.load()
    need = needs_proxy(d)
    st = router.live_status() if need or d.get("applied") else {"running": False}
    return {"codex": view("codex", d), "claude": view("claude", d),
            "proxy": {"needed": need, "running": bool(st.get("running")), "port": st.get("port") or d.get("port"), "where": st.get("where"),
                      "login": router.login_get(), "autostart": d.get("autostart", True), "requests": st.get("requests", 0),
                      "errors": st.get("errors", 0), "errors_recent": [x for x in st.get("errors_recent") or [] if str(x.get("provider") or "").startswith("card-") or not x.get("provider")][:5]}}


def fetch_models(client: str, b: dict) -> dict:
    """「拉取模型」：用刚贴的 key（没贴就用已存的）。"""
    kind = b.get("kind") or get(client)["kind"]
    c = dict(get(client), **{k: v for k, v in b.items() if k in ("kind", "base_url", "protocol")})
    c["kind"] = kind
    sec = (b.get("secret") or "").strip() or keystore.get(key_name(client, kind)) or ""
    r = router.list_models(draft={"protocol": proto(c) or "chat", "base_url": upstream_url(c), "secret": sec})
    if r.get("ok"):
        r["default"], r["small"] = defaults_for(kind, r["models"])
        if kind == "claude":
            r["models"] = sorted(r["models"], key=lambda m: (0 if "opus" in m else 1 if "sonnet" in m else 2 if "haiku" in m else 3, tuple(-x for x in _ver(m)[0])))
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

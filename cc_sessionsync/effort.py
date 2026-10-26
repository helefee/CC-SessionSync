"""推理强度：从客户端请求里读出「这次要多大力气」，再按上游供应商的写法放进转发出去的请求（0.3.2）。

客户端那边（每次请求都带，所以在 Codex / Claude Code 里随时切换都生效）：
- Codex（Responses）：reasoning.effort ∈ none / minimal / low / medium / high / xhigh / max
- Claude Code（Anthropic）：thinking + output_config.effort ∈ low / medium / high / xhigh / max；thinking.disabled → none
- Chat 客户端：reasoning_effort

上游那边每家写法不同（来源见 presets.py）。不支持的级别就近折算；完全不支持就不传（用模型自己的默认）。
Anthropic 上游（含 DeepSeek / GLM / Kimi / Qwen 的 Anthropic 兼容接口）由 xproto 写 thinking / output_config，这里不动。
"""
from __future__ import annotations

import re

LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_REASONING = re.compile(r"^(o\d|gpt-5|gpt-6|codex)", re.I)
QWEN_BUDGET = {"minimal": 1024, "low": 4096, "medium": 16384, "high": 32768, "xhigh": 65536, "max": 65536}


def requested(client: str, body: dict):
    """这次请求要的强度；客户端没说 → None。"""
    if client == "responses":
        e = (body.get("reasoning") or {}).get("effort")
        return e if e in LEVELS else None
    if client == "chat":
        e = body.get("reasoning_effort")
        return e if e in LEVELS else None
    th = body.get("thinking") or {}
    eff = (body.get("output_config") or {}).get("effort")
    if th.get("type") == "disabled":
        return "none"
    if eff in LEVELS:
        return eff if th.get("type") != "disabled" else "none"
    if th.get("type") == "enabled":
        bt = int(th.get("budget_tokens") or 0)
        return "low" if bt <= 4096 else ("medium" if bt <= 16000 else "high")
    if th.get("type") == "adaptive":
        return "medium"
    return None


def _fold(e: str, allowed: tuple) -> str:
    """折到允许的级别里：先往上找，找不到再往下。"""
    if e in allowed:
        return e
    i = LEVELS.index(e)
    for x in LEVELS[i + 1:]:
        if x in allowed:
            return x
    for x in reversed(LEVELS[:i]):
        if x in allowed:
            return x
    return allowed[-1]


def _clear(ub: dict):
    ub.pop("reasoning_effort", None)
    r = ub.get("reasoning")
    if isinstance(r, dict):
        r.pop("effort", None)


def _resp_effort(ub: dict, e):
    if e is None:
        return
    ub.setdefault("reasoning", {})
    if isinstance(ub["reasoning"], dict):
        ub["reasoning"]["effort"] = e


def adapt(style: str, upstream: str, ub: dict, e, model: str) -> dict:
    """ub：已经转成上游协议的请求。e：requested() 的结果。返回改好的 ub（同一个对象）。"""
    if upstream == "anthropic" or not style or style == "anthropic":
        return ub
    m = (model or "").lower()
    if isinstance(ub.get("reasoning"), dict):
        ub["reasoning"] = dict(ub["reasoning"])      # 别改到客户端原请求（换 key 重试时还要再读一遍）
    if style == "openai" and not (_REASONING.search(m) or isinstance(ub.get("reasoning"), dict) or "reasoning_effort" in ub):
        return ub                                    # gpt-4.1 这类不带推理的模型：不加
    _clear(ub)
    for k in ("thinking", "enable_thinking", "thinking_budget"):
        ub.pop(k, None)
    if e is None:
        if isinstance(ub.get("reasoning"), dict) and not ub["reasoning"].get("effort") and set(ub["reasoning"]) <= {"summary"} and style != "openai":
            ub.pop("reasoning", None)
        return ub
    chat = upstream == "chat"

    def put(v):
        if v is None:
            return
        if chat:
            ub["reasoning_effort"] = v
        else:
            _resp_effort(ub, v)

    if style in ("openai", "generic"):
        if style == "generic" and e == "none":
            return ub
        put("xhigh" if e == "max" else e)
    elif style == "openrouter":                      # reasoning.effort：xhigh / high / medium / low / minimal / none
        if chat:
            ub["reasoning"] = {"effort": "xhigh" if e == "max" else e}
        else:
            put("xhigh" if e == "max" else e)
    elif style == "xai":                             # low / medium / high / xhigh；不能关
        put(_fold(e, ("low", "medium", "high", "xhigh")) if e not in ("none", "minimal") else "low")
    elif style == "gemini":                          # reasoning_effort：none（只有 2.5 Flash 系列能关）/ minimal / low / medium / high
        if e == "none":
            put("none" if ("2.5" in m and "pro" not in m) else "low")
        else:
            put(_fold(e, ("minimal", "low", "medium", "high")) if e != "minimal" else "low")
    elif style == "deepseek":                        # thinking.type 开关 + reasoning_effort low / high / max
        if e == "none":
            ub["thinking"] = {"type": "disabled"}
        else:
            ub["thinking"] = {"type": "enabled"}
            put({"minimal": "low", "low": "low", "medium": "high", "high": "high", "xhigh": "high", "max": "max"}[e])
    elif style == "glm":                             # thinking.type 开关；GLM-5.2 起 reasoning_effort（5.3 只认 low / high / max）
        v = _ver(m)
        if e == "none" and not m.startswith("glm-5.3"):
            ub["thinking"] = {"type": "disabled"}
        else:
            ub["thinking"] = {"type": "enabled"}
            if v >= (5, 3):
                put(_fold(e if e != "none" else "low", ("low", "high", "max")))
            elif v >= (5, 2):
                put(e if e != "none" else "low")
    elif style == "kimi":                            # kimi-k3*：reasoning_effort low / high / max（一直思考）；kimi-k2.6：thinking 开关
        if m.startswith("kimi-k3"):
            put(_fold(e if e not in ("none",) else "low", ("low", "high", "max")))
        elif m.startswith("kimi-k2.6"):
            ub["thinking"] = {"type": "disabled" if e == "none" else "enabled"}
    elif style == "qwen":                            # enable_thinking 开关；qwen3.8 用 reasoning_effort（low / medium / xhigh），其它用 thinking_budget
        if e == "none":
            ub["enable_thinking"] = False
        else:
            ub["enable_thinking"] = True
            if m.startswith("qwen3.8"):
                put(_fold(e, ("low", "medium", "xhigh")) if e not in ("high", "max") else "xhigh")
            else:
                ub["thinking_budget"] = QWEN_BUDGET.get(e, 16384)
    if not chat and isinstance(ub.get("reasoning"), dict) and not ub["reasoning"]:
        ub.pop("reasoning")
    return ub


def _ver(m: str) -> tuple:
    x = re.search(r"(\d+)(?:\.(\d+))?", m)
    return (int(x.group(1)), int(x.group(2) or 0)) if x else (0, 0)


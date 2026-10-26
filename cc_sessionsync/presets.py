"""「Codex 使用… / Claude Code 使用…」卡片里能选的供应商（0.3.2）。地址 / 协议都照官方文档（来源写在 src）。

每家给出它支持的协议地址（anthropic = Anthropic Messages，responses = OpenAI Responses，chat = OpenAI Chat Completions）。
卡片为每个客户端挑一个：客户端自己的协议优先（不用转换，原样转发，只换模型名），没有再挑能转换的。
effort：推理强度怎么传（见 effort.py）。models：拉模型列表用哪个协议地址。
"""
from __future__ import annotations

PRESETS = [
    {"id": "anthropic", "name": "Claude API Key", "vendor": "Anthropic", "style": "anthropic", "models": "anthropic",
     "regions": {"": {"anthropic": "https://api.anthropic.com"}},
     "src": "https://docs.anthropic.com/en/api/messages", "key": "sk-ant-…", "default": ("opus", "haiku")},
    {"id": "openai", "name": "OpenAI API Key", "vendor": "OpenAI", "style": "openai", "models": "responses",
     "regions": {"": {"responses": "https://api.openai.com/v1", "chat": "https://api.openai.com/v1"}},
     "src": "https://platform.openai.com/docs/api-reference/responses", "key": "sk-…", "default": ("gpt-", "mini")},
    {"id": "gemini", "name": "Gemini", "vendor": "Google", "style": "gemini", "models": "chat",
     "regions": {"": {"chat": "https://generativelanguage.googleapis.com/v1beta/openai"}},
     "src": "https://ai.google.dev/gemini-api/docs/openai", "key": "AIza…", "default": ("pro", "flash")},
    {"id": "deepseek", "name": "DeepSeek", "vendor": "DeepSeek", "style": "deepseek", "models": "chat",
     "regions": {"": {"chat": "https://api.deepseek.com", "anthropic": "https://api.deepseek.com/anthropic"}},
     "src": "https://api-docs.deepseek.com/ · https://api-docs.deepseek.com/guides/anthropic_api", "key": "sk-…", "default": ("pro", "flash")},
    {"id": "glm", "name": "智谱 GLM", "vendor": "Zhipu / Z.ai", "style": "glm", "models": "chat",
     "regions": {"cn": {"chat": "https://open.bigmodel.cn/api/paas/v4", "anthropic": "https://open.bigmodel.cn/api/anthropic", "_label": "中国（bigmodel.cn）"},
                 "intl": {"chat": "https://api.z.ai/api/paas/v4", "anthropic": "https://api.z.ai/api/anthropic", "_label": "国际（z.ai）"},
                 "coding": {"chat": "https://open.bigmodel.cn/api/coding/paas/v4", "anthropic": "https://open.bigmodel.cn/api/anthropic", "_label": "GLM Coding Plan"}},
     "src": "https://docs.bigmodel.cn/cn/guide/develop/claude · https://docs.z.ai/api-reference/llm/chat-completion · https://docs.bigmodel.cn/cn/guide/develop/others",
     "key": "…", "default": ("glm-", "flash")},
    {"id": "kimi", "name": "Kimi（月之暗面）", "vendor": "Moonshot AI", "style": "kimi", "models": "chat",
     "regions": {"cn": {"chat": "https://api.moonshot.cn/v1", "responses": "https://api.moonshot.cn/v1", "anthropic": "https://api.moonshot.cn/anthropic", "_label": "中国（moonshot.cn）"},
                 "intl": {"chat": "https://api.moonshot.ai/v1", "responses": "https://api.moonshot.ai/v1", "anthropic": "https://api.moonshot.ai/anthropic", "_label": "国际（moonshot.ai）"}},
     "src": "https://platform.kimi.com/docs/api/overview · https://platform.kimi.ai/docs/guide/use-thinking-models", "key": "sk-…", "default": ("kimi-k", "")},
    {"id": "qwen", "name": "通义千问 Qwen（百炼）", "vendor": "Alibaba Cloud Model Studio", "style": "qwen", "models": "chat",
     "regions": {"cn": {"chat": "https://dashscope.aliyuncs.com/compatible-mode/v1", "anthropic": "https://dashscope.aliyuncs.com/apps/anthropic", "_label": "中国（北京）"},
                 "intl": {"chat": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "anthropic": "https://dashscope-intl.aliyuncs.com/apps/anthropic", "_label": "国际（新加坡）"}},
     "src": "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions · https://www.alibabacloud.com/help/en/model-studio/claude-code",
     "key": "sk-…", "default": ("max", "flash")},
    {"id": "xai", "name": "Grok（xAI）", "vendor": "xAI", "style": "xai", "models": "responses",
     "regions": {"": {"responses": "https://api.x.ai/v1", "chat": "https://api.x.ai/v1"}},
     "src": "https://docs.x.ai/developers/model-capabilities/text/reasoning", "key": "xai-…", "default": ("grok-", "fast")},
    {"id": "openrouter", "name": "OpenRouter", "vendor": "OpenRouter", "style": "openrouter", "models": "chat",
     "regions": {"": {"chat": "https://openrouter.ai/api/v1"}},
     "src": "https://openrouter.ai/docs/api_reference/parameters", "key": "sk-or-…", "default": ("", "")},
]
BY_ID = {p["id"]: p for p in PRESETS}
ORDER = {"codex": ("responses", "chat", "anthropic"), "claude": ("anthropic", "responses", "chat")}
NATIVE = {"codex": "responses", "claude": "anthropic"}


def get(pid: str) -> dict | None:
    return BY_ID.get(pid)


def region_of(p: dict, region: str | None) -> dict:
    r = p["regions"]
    return r.get(region or "") or r[next(iter(r))]


def pick(pid: str, client: str, region: str | None = None) -> tuple:
    """→ (协议, 地址)。客户端自己的协议优先。"""
    p = BY_ID[pid]
    urls = region_of(p, region)
    for proto in ORDER[client]:
        if urls.get(proto):
            return proto, urls[proto]
    raise ValueError(pid)


def models_endpoint(pid: str, region: str | None = None) -> tuple:
    p = BY_ID[pid]
    urls = region_of(p, region)
    proto = p["models"] if urls.get(p["models"]) else next(k for k in ("chat", "responses", "anthropic") if urls.get(k))
    return proto, urls[proto]


def view() -> list:
    return [{"id": p["id"], "name": p["name"], "vendor": p["vendor"], "key": p["key"], "src": p["src"],
             "regions": [{"id": k, "label": v.get("_label") or ""} for k, v in p["regions"].items()],
             "codex": pick(p["id"], "codex")[0], "claude": pick(p["id"], "claude")[0]} for p in PRESETS]


def detect(url: str) -> tuple:
    """自定义地址 → (协议, 规范后的地址)。只看地址的样子，不发生成请求（不花钱）。认不出来就当 OpenAI Chat Completions。"""
    u = (url or "").strip().rstrip("/")
    low = u.lower()
    if "generativelanguage.googleapis.com" in low and "/openai" not in low:   # Gemini 原生地址 → 它的 OpenAI 兼容接口
        return "chat", "https://generativelanguage.googleapis.com/v1beta/openai"
    if low.endswith("/anthropic") or "/anthropic/" in low or "api.anthropic.com" in low or low.endswith("/apps/anthropic"):
        return "anthropic", u
    if low.endswith("/responses"):
        return "responses", u[: -len("/responses")]
    if low.endswith("/chat/completions"):
        return "chat", u[: -len("/chat/completions")]
    if low.endswith("/v1/messages") or low.endswith("/messages"):
        return "anthropic", u.rsplit("/messages", 1)[0].removesuffix("/v1")
    return "chat", u

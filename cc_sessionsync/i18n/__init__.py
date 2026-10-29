# -*- coding: utf-8 -*-
"""界面语言（0.4.3）：中文 / English。设置里「界面语言」：跟随系统（缺省）/ 中文 / English。
界面里的文字都以中文写在代码里；en.json 是「中文片段 → 英文」的对照表（tools/i18n_extract.py 从 index.html 和后端提示里抽出来）。
英文界面时：网页由页面里的翻译器按对照表换（只换整段都认识的文字，用户自己的会话内容不会被改）；托盘菜单 / 通知用 t()。"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

LANGS = ("zh", "en")
_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_PUNC = {"（": " (", "）": ")", "：": ": ", "；": "; ", "、": ", ", "，": ", ", "。": ". ", "！": "! ", "？": "? ",
         "「": "\"", "」": "\"", "《": "\"", "》": "\"", "【": "[", "】": "]", "　": " "}
_PRX = re.compile("|".join(map(re.escape, _PUNC)))


def _tidy(s: str) -> str:
    """换完以后的中文标点 → 英文标点，再收拾空格。"""
    s = _PRX.sub(lambda m: _PUNC[m.group(0)], s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"[ \t]+([).,;:!?])", r"\1", s)
    s = re.sub(r"(^|\n) \(", r"\1(", s)
    return re.sub(r"[ \t]{2,}", " ", s)


def system_lang() -> str:
    """系统界面语言：中文（简 / 繁）→ zh，其它 → en。CC_SESSIONSYNC_LANG 可以直接指定（测试用）。"""
    v = (os.environ.get("CC_SESSIONSYNC_LANG") or "").lower()
    if v in LANGS:
        return v
    if sys.platform == "win32":
        try:
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "zh" if (lid & 0x3FF) == 0x04 else "en"
        except Exception:
            pass
    for k in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        v = os.environ.get(k)
        if v:
            return "zh" if v.lower().startswith("zh") else "en"
    return "zh"


def setting() -> str:
    from .. import settings as _s
    v = _s.get("lang", "auto")
    return v if v in ("auto",) + LANGS else "auto"


def lang() -> str:
    v = setting()
    return v if v in LANGS else system_lang()


@lru_cache(maxsize=1)
def table() -> dict:
    f = Path(__file__).with_name("en.json")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, str)}


@lru_cache(maxsize=1)
def _rx():
    keys = sorted(table(), key=len, reverse=True)
    return re.compile("|".join(re.escape(k) for k in keys)) if keys else None


def translate(s: str) -> str:
    """中文 → 英文：整句认识就整句换；否则按片段换，换完还剩中文就原样返回（不认识的不乱换）。"""
    if not s or not _CJK.search(s):
        return s
    d = table()
    k = s.strip()
    if k in d and d[k]:
        return s.replace(k, d[k])
    rx = _rx()
    if not rx:
        return s
    out = rx.sub(lambda m: "\x01" + d.get(m.group(0), m.group(0)) + "\x01", s)
    if _HAN.search(out):
        return s
    out = re.sub(r"(?<=[^\s(\[\"'/\x01])\x01+(?=[^\s)\]\"',.;:!?/\x01（）：；、，。])", " ", out)   # 两段英文挨着 → 加空格
    return _tidy(out.replace("\x01", ""))


def t(s: str) -> str:
    """托盘 / 通知用：英文界面时翻译，中文界面原样。"""
    try:
        return translate(s) if lang() == "en" else s
    except Exception:
        return s


def page_payload() -> str:
    """给网页的：英文界面时是 {"lang": "en", "dict": {...}}；中文是 null。"""
    if lang() != "en":
        return "null"
    return json.dumps({"lang": "en", "dict": table()}, ensure_ascii=False).replace("</", "<\\/")

# -*- coding: utf-8 -*-
"""0.4.3：把界面里的中文片段抽出来（index.html 的文字 / 模板字符串里不变的部分，加上后端会显示在界面上的提示），
和 cc_sessionsync/i18n/en.json 对一下：新的片段加进去（值为 null = 还没翻；"" = 英文里不要这个词，比如量词「个」），已经没用的列出来。
python tools/i18n_extract.py [--check]   --check：有没翻的就退出码 1（CI 用）"""
import json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "cc_sessionsync"
EN = PKG / "i18n" / "en.json"
CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
# 一个片段：从第一个中文字开始，到引号 / 标签 / 插值 / 换行为止；去掉两头的空白和不成对的标点
FRAG = re.compile(r"[^`'\"<>{}$\n\\]*[\u4e00-\u9fff][^`'\"<>{}$\n\\]*")
PY = ["convert.py", "update.py", "sessions.py", "selfcheck.py", "ui.py", "bakclean.py", "formats.py", "timeline.py", "events.py",
      "exports.py", "settings.py", "apps.py", "claude_sync.py", "codex_import.py", "dsh.py", "cursor.py", "ocstore.py", "xsearch.py",
      "usage.py", "watch.py", "appclose.py", "tray.py", "desktop.py", "keystore.py", "detect.py", "legacy.py", "launch.py", "health.py", "nativecheck.py"]


def clean(f: str) -> str:
    f = f.strip()
    f = re.sub(r"^[\s,.;:|+*=()\[\]·/-]+", "", f)
    f = re.sub(r"[\s,;:|+*=(\[/-]+$", "", f)
    return f.strip()


def frags_html(s: str) -> set[str]:
    s = re.sub(r"//[^\n]*", "", s)              # 注释里的中文不是界面文字
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    return {c for c in (clean(m) for m in FRAG.findall(s)) if CJK.search(c)}


def frags_py(s: str) -> set[str]:
    out = set()
    s = re.sub(r'"""(.*?)"""', "", s, flags=re.S)   # 文档字符串
    s = re.sub(r"#[^\n]*", "", s)
    for m in re.finditer(r"""(?:f|rf|fr|r)?("([^"\\\n]|\\.)*"|'([^'\\\n]|\\.)*')""", s):
        lit = m.group(1)[1:-1]
        lit = re.sub(r"\{[^{}]*\}", "\x00", lit)
        for part in lit.split("\x00"):
            for f in re.findall(r"[^\n\\]*[\u4e00-\u9fff][^\n\\]*", part):
                c = clean(f)
                if CJK.search(c):
                    out.add(c)
    return out


def collect() -> set[str]:
    fr = frags_html((PKG / "index.html").read_text(encoding="utf-8"))
    for f in PY:
        p = PKG / f
        if p.exists():
            fr |= frags_py(p.read_text(encoding="utf-8"))
    return {f for f in fr if len(f) <= 400}


def main() -> int:
    fr = collect()
    cur = json.loads(EN.read_text(encoding="utf-8")) if EN.exists() else {}
    new = sorted(fr - set(cur))
    gone = sorted(set(cur) - fr)
    todo = [k for k, v in cur.items() if v is None and k in fr]
    if "--check" in sys.argv:
        print(f"{len(fr)} fragments, {len(new)} not in en.json, {len(todo)} empty, {len(gone)} unused")
        return 1 if new or todo else 0
    for k in new:
        cur[k] = None
    EN.parent.mkdir(parents=True, exist_ok=True)
    EN.write_text(json.dumps(dict(sorted(cur.items())), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{len(fr)} fragments; added {len(new)}; empty {len(todo) + len(new)}; unused {len(gone)}")
    for g in gone[:20]:
        print("  unused:", g)
    return 0


if __name__ == "__main__":
    sys.exit(main())

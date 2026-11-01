# -*- coding: utf-8 -*-
"""0.5.3：对话气泡按来源应用换样式（Claude / Codex / Cursor / DeepSeek Harness / OpenCode / ZCode，浅色 / 深色各一套），可改成「统一样式」；
对话文本宽度可调（窄 / 标准 / 宽 / 全宽 + 滑块，记在本机和 settings.json）。只看页面源码 + 调后端设置接口，不开浏览器。"""
import json, os, pathlib, re, sys, tempfile
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-cth-")); H = T / "home"; H.mkdir()
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  XDG_CONFIG_HOME=str(T / "cfg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"),
                  CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1", CC_SESSIONSYNC_NO_WATCHER="1")
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
def grab(name):
    m = re.search(r"^((?:async )?function %s\(|const %s = )" % (re.escape(name), re.escape(name)), page, re.M)
    if not m: return ""
    e = re.compile(r"^(function |const |let |var |async function |//|/\*|\S+\s*=|[A-Za-z_$][\w$.]*\()", re.M).search(page, m.end())
    return page[m.start():e.start() if e else len(page)]

# ---- 主题表
m = re.search(r"const CHTH = (\{.*?\n\});", page, re.S)
ok(bool(m), "CHTH theme table exists")
TH = json.loads(m.group(1)) if m else {}
APPS = {"claude", "codex", "cursor", "dsh", "opencode", "zcode"}
ok(set(TH) == APPS, f"one theme per source app ({sorted(TH)})")
SK = {"r", "af", "tic"}; CK = {"ub", "uf", "link", "ok", "bad", "code"}  # 其余形状键可省（用缺省）; CK = {"ub", "uf", "link", "ok", "bad", "code"}
for a, t in TH.items():
    ok(set(t) >= {"s", "l", "d"} and SK <= set(t["s"]) and CK <= set(t["l"]) and CK <= set(t["d"]), f"{a}: shape + light + dark palettes complete")
    ok(t["l"]["ub"] != t["d"]["ub"], f"{a}: dark bubble differs from light")
ok("绿" in json.dumps(TH["codex"], ensure_ascii=False) or re.match(r"#[0-9a-f]{6}", TH["codex"]["l"]["ub"]), "codex has its own bubble colour")
ok(len({t["l"]["ub"] for t in TH.values()}) >= 5, "apps look different (distinct light bubbles)")
ok(TH["opencode"]["s"]["blw"] not in ("0", "0px") and TH["opencode"]["s"]["r"] in ("0", "0px"), "opencode: left bar, square corners")

css = grab("chatThCss")
ok(':root[data-theme="dark"]' in css and 'prefers-color-scheme:dark' in css and ':root:not([data-theme="light"])' in css, "dark palette follows forced dark and system dark")
ok('id="chatThCss"' in page or "chatThCss" in page and "<style" in page, "generated theme CSS is injected")
th = grab("chatTh")
ok('"uni"' in th and "chatStyle()" in th and '"claude"' in th and "CHTH[" in th, "chatTh: unified → uni, unknown app → claude")
ok("th-${chatTh(agent)}" in grab("chatHtml") and "data-ag=" in grab("chatHtml"), "chatHtml carries the theme class + agent")
ok(".chat>*{content-visibility:auto" in page, "content-visibility kept")
ok("bad" in grab("toolRowHtml") and "t.err" in grab("toolRowHtml"), "failed tool rows get the bad class")

# ---- 宽度
w = grab("applyChatW")
ok('store.set("ccss.chatw"' in w and "--chatw" in w and "chat_width" in w, "applyChatW: CSS var + localStorage + settings")
ok("ccss.chatw" in page.split("</head>")[0] and "--chatw" in page.split("</head>")[0], "head script sets --chatw before first paint")
ok("max-width:var(--chatw" in page, ".chat max-width follows --chatw")
ok("CHW_MIN" in page and "CHW_MAX" in page and 'data-rd="wr"' in page, "width slider with bounds")
s = grab("applyChatStyle")
ok('store.set("ccss.chatstyle"' in s and "chat_style" in s, "applyChatStyle: localStorage + settings")
ok('id="svRd"' in page and "stopPropagation" in page and 'rdBind($("#svRd"))' in page, "reading popover in session header")
ok("rdBind(pg)" in page and "对话气泡样式" in page and "对话文本宽度" in page, "settings page rows")

# ---- 后端
from cc_sessionsync import ui, settings
ok(settings.load()["chat_style"] == "app" and settings.load()["chat_width"] == 860, "defaults: per-app style, 860px")
for v, want in ((100, 520), (5000, 1600), (0, 0), (-3, 0), ("1000", 1000)):
    r = ui.H._post(None, "/api/settings", {"chat_width": v})
    ok(r["chat_width"] == want and settings.load()["chat_width"] == want, f"chat_width {v!r} → {want}")
ok(ui.H._post(None, "/api/settings", {"chat_style": "unified"})["chat_style"] == "unified", "chat_style unified saved")
for bad in ({"chat_style": "pink"}, {"chat_width": "wide"}):
    try: ui.H._post(None, "/api/settings", bad); ok(False, f"reject {bad}")
    except ValueError: ok(True, f"reject {bad}")
ok(settings.load()["chat_style"] == "unified" and settings.load()["chat_width"] == 1000, "rejected values not saved")
sys.exit(1 if FAILS else 0)

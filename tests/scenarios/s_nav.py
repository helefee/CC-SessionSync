# -*- coding: utf-8 -*-
"""0.2.8：顶栏不再有一对一的转换页（Codex→Claude / DeepSeek⇄Claude / Cursor⇄Claude），都并进「会话转换」；
旧页面只留在会话转换页底的链接；深链接 / 托盘 / 通知 / 记住的上次页面指向旧页面时打开会话转换并预选方向；
Codex → Claude Code 在会话转换里用 Codex 导入器（防 Codex 再导回、含子代理、按天数、预览）。"""
import os, re, sys, pathlib, tempfile, json
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="ccss-nav-")); H = T / "home"; H.mkdir()
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                  XDG_CONFIG_HOME=str(T / "cfg"), CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_NO_WATCHER="1")
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
top = page[page.index('<header class="top">'):page.index('</header>')]
navp = re.findall(r'data-p="([\w-]+)"', top)
ok("convert" in navp, "nav has 会话转换")
ok(not any(p in navp for p in ("codex", "dsh", "cursor")), f"no per-pair pages left in the nav ({navp})")
ok("Codex→Claude" not in top and "DeepSeek⇄Claude" not in top and "Cursor⇄Claude" not in top, "no per-pair labels in the top bar")
ok(set(navp) <= {"timeline", "sync", "convert", "backups", "usage", "models", "diag", "settings"}, "nav = timeline / sync / convert / backups / usage / models / diag / settings")
for p in ("codex", "dsh", "cursor"):
    ok(f'id="pg-{p}"' in page and f'data-go="{p}"' in page, f"old {p} page kept, reachable only via the link at the bottom of 会话转换")
ok(re.search(r'const PAGE_ALIAS = \{codex: \["codex", "claude"\]', page) is not None, "PAGE_ALIAS: codex → 会话转换 with Codex → Claude Code preselected")
ok("if (p) goTo(p, t);" in page, "tray / notification ccOpen goes through the alias")
ok('if (R[want]) goTo(want' in page, "deep link ?page= / #hash / remembered last page go through the alias")
ok("return goTo(page, a && a.open)" in page, "account-switch banner 'open' goes through the alias")
ok('const cxMode = CV.src === "codex" && CV.dst === "claude"' in page and 'R.codex(false, $("#cvCodex"))' in page, "会话转换 embeds the Codex importer for Codex → Claude Code")
cx = page[page.index("R.codex = async"):page.index("function cxVisible")]
ok('sw("cxMark"' in cx and 'id="cxAll"' in cx and "含子代理" in cx and 'id="cxDays"' in cx and 'id="cxPv"' in cx, "embedded importer keeps anti-reimport switch, 含子代理, days, preview")
ok("R.codex(false, CX.host)" in cx, "importer re-renders stay embedded")
# 托盘：「会话转换（Codex → Claude Code）…」打开会话转换并预选
from cc_sessionsync import tray
opened = []
class App:
    def visible(self): return True
    def open(self, t): opened.append(t)
tr = object.__new__(tray.Tray); tr.app = App(); tr.state = {"settings": {}, "update": {}}
labels = [x[1] for x in tr.menu_model() if len(x) > 1 and isinstance(x[0], int)]
ok("会话转换（Codex → Claude Code）…" in labels and "Codex → Claude Code…" not in labels, "tray menu item renamed")
tr._do(7)
ok(opened == [{"page": "convert", "src": "codex", "dst": "claude"}], f"tray item opens 会话转换 with Codex preselected ({opened})")
# 导入完成的通知事件
ui_src = (ROOT / "cc_sessionsync" / "ui.py").read_text(encoding="utf-8")
ok('{"page": "codex"}' not in ui_src and '{"page": "convert", "src": "codex", "dst": "claude"}' in ui_src, "Codex import event opens 会话转换")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

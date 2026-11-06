# -*- coding: utf-8 -*-
"""导航：会话转换已移除；顶栏为 时间线与备份 / 账号同步 / 用量 / …；无一对一转换页。"""
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
ok("convert" not in navp and "会话转换" not in top, "nav has no 会话转换")
ok(not any(p in navp for p in ("codex", "dsh", "cursor", "convert")), f"no convert / pair pages in nav ({navp})")
ok("Codex→Claude" not in top and "DeepSeek⇄Claude" not in top and "Cursor⇄Claude" not in top, "no per-pair labels in the top bar")
ok(set(navp) <= {"timeline", "sync", "usage", "models", "diag", "settings", "about"}, f"nav pages ok ({navp})")
for p in ("codex", "dsh", "cursor", "convert"):
    ok(f'id="pg-{p}"' not in page, f"page {p} removed")
ok("PAGE_ALIAS" not in page and "R.convert" not in page and "R.codex" not in page, "convert JS gone")
ok("function goTo(" in page, "goTo kept for timeline tabs / deep links")
ok(navp[:2] == ["timeline", "sync"] and "backups" not in navp, f"top bar starts 时间线与备份 / 账号同步 ({navp})")
# tray: no convert item
from cc_sessionsync import tray
class FakeApp:
    def __init__(self): self.opened = []
    def visible(self): return True
    def hide(self): pass
    def show(self): pass
    def open(self, o): self.opened.append(o)
    def check_update(self): pass
    def quit(self): pass
    port = 0
app = FakeApp()
# tray.Icon may need more; use menu_model via a stub if available
try:
    from cc_sessionsync.tray import Icon
except Exception:
    Icon = None
if Icon:
    # minimal state
    class I:
        pass
    # Read menu_model source expectations from file
    src = (ROOT / "cc_sessionsync" / "tray.py").read_text(encoding="utf-8")
    ok("会话转换" not in src, "tray has no 会话转换 item")
    ok('{"page": "convert"' not in src, "tray does not open convert")
else:
    ok(True, "tray Icon n/a")
# timeline undo: no convert ledgers card
ok("会话转换账本" not in page and "data-vled" not in page and "/api/convert/ledgers" not in page, "no convert ledgers in timeline")
ok("bkNow" in page and "bkJobs" in page and "回收站" in page, "timeline backup / undo panes present")
ok("convert_skip_preview" not in page, "no skip-convert-preview setting")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

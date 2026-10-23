# -*- coding: utf-8 -*-
"""0.2.9：时间线与备份合并为一个页面（子页签 时间线 | 备份 | 撤销记录），旧入口都指向它。
0.2.8：顶栏不再有一对一的转换页（Codex→Claude / DeepSeek⇄Claude / Cursor⇄Claude），都并进「会话转换」；
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
ok(set(navp) <= {"timeline", "sync", "convert", "usage", "models", "diag", "settings"}, "nav = timeline / sync / convert / usage / models / diag / settings")
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
# 0.2.9：「时间线」和「备份」合成一个页面「时间线与备份」，子页签 时间线 | 备份 | 撤销记录
ok(navp[:3] == ["timeline", "sync", "convert"] and "backups" not in navp, f"0.2.9: top bar starts 时间线与备份 / 账号同步 / 会话转换, no separate 备份 ({navp})")
ok("<span>时间线与备份</span>" in top and "<span>备份</span>" not in top, "0.2.9: one top-bar item 时间线与备份")
ok('id="pg-backups"' not in page and 'id="pg-timeline"' in page, "0.2.9: separate 备份 page section removed")
ok('if (p === "backups" || p === "jobs" || p === "undo") { tbSetTab(p === "backups" ? "bk" : "undo"); p = "timeline"; }' in page,
   "0.2.9: old backups / jobs / undo page names (deep links, #backups, remembered page, notifications) open the merged page on the right tab")
ok('const TB_TABS = [["tl", "时间线", "history"], ["bk", "备份", "download"], ["undo", "撤销记录", "undo"]]' in page, "0.2.9: sub-tabs 时间线 | 备份 | 撤销记录")
ok('if (p === "timeline" && o.tab) tbSetTab(o.tab);' in page and 'qs.get("tab")' in page, "0.2.9: goTo / ?tab= select the sub-tab")
bk = page[page.index("async function bkRender"):]
bk = bk[:bk.index("\n}\n") + 3]
for k in ("bkNow", "bkJobs", "bkPrune", "data-vled", "bkImp", "回收站", "会话转换账本", "data-tlo"):
    ok(k in bk, f"0.2.9: merged page keeps {k}")
ok(re.search(r'bkAuto|bkEvery', bk) is not None, "0.2.9: auto-backup interval kept")
ok('goTo("timeline", {tab: "undo"})' in page or 'goTo("timeline",{tab:"undo"})' in page, "0.2.9: 会话转换 links to 撤销记录")
labels2 = [x[1] for x in tr.menu_model() if len(x) > 1 and isinstance(x[0], int)]
ok("时间线与备份…" in labels2, "0.2.9: tray has 时间线与备份…")
opened.clear(); tr._do(14)
ok(opened == [{"page": "timeline", "tab": "tl"}], f"0.2.9: tray item opens the merged page ({opened})")
ss_src = (ROOT / "cc_sessionsync" / "sessions.py").read_text(encoding="utf-8")
ok('{"page": "timeline", "tab": "bk"}' in ss_src, "0.2.9: backup-failure notification opens 时间线与备份 → 备份")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

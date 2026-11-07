# -*- coding: utf-8 -*-
"""0.5.2：会话页不再重复显示会话列表 + 用量页流转图的版式（只看页面源码，不开浏览器）
- 左侧面板就是会话列表：打开一个会话时中间的列表收起、会话占满；「← 会话列表」回去（多选 / 批量在那里，已选保留）；「并排显示列表」恢复左右分栏并记住
- 左侧面板收起（Ctrl+B）时中间列表是唯一的列表，照旧并排；在左侧点分组 = 想看那组的列表，先收起会话
- 用量页：时间轴是流转图下面单独一张画布，悬停提示钉在对角，不挡内容"""
import sys, pathlib, re
ROOT = pathlib.Path(__file__).resolve().parents[2]
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")

def grab(name):
    m = re.search(r"^((?:async )?function %s\(|const %s = )" % (re.escape(name), re.escape(name)), page, re.M)
    if not m: return ""
    e = re.compile(r"^(function |const |let |var |async function |//|/\*|\S+\s*=|[A-Za-z_$][\w$.]*\()", re.M).search(page, m.end())
    return page[m.start():e.start() if e else len(page)]

lay, close = grab("ssLayout"), grab("ssClose")
ok(bool(lay) and bool(close), "ssLayout / ssClose exist")
ok(".split2.focus>.l" in page and ".split2.focus>.grip" in page and re.search(r"\.split2\.focus>\.l[^{]*\{display:none\}", page), "focus mode hides the middle list and the grip")
ok(".split2.nocur>.r" in page, "no session open: list takes the whole width (no empty detail pane)")
ok('ccss.sspin' in lay and 'noside' in lay and '"focus"' in lay, "focus only when the sidebar is visible and the split isn't pinned")
ok('id="ssBack"' in lay and "SS.sel.size" in lay and "ssClose" in lay, "back-to-list button (shows the current multi-selection)")
ok('id="ssPin"' in lay and 'store.set("ccss.sspin"' in lay, "pin toggle restores the old side-by-side split and is remembered")
ok("SS.cur = null" in close and "ssList()" in close and "treeRender()" in close and "SS.sel" not in close, "closing keeps the multi-selection, redraws list + tree")
ok("ssLayout()" in grab("toggleSide"), "Ctrl+B / sidebar toggle re-lays out the session page")
ok("ssLayout()" in grab("ssView"), "opening a session re-lays out")
ok(re.search(r'tn\[data-sel\]"\)\.forEach[\s\S]{0,500}split2\.focus[\s\S]{0,80}ssClose\(\)', page), "clicking a group in the sidebar while a session fills the page shows that group's list")
ok('TR.open.has(id) ? TR.open.delete(id) : TR.open.add(id)' in page, "clicking an account or project row toggles its conversation list open or closed")
ok('id="ssNav"' in page and 'id="ssBatch"' in page, "batch bar still in the list pane")
# 用量页
ok('id="ufTl"' in page and 'id="ufTh"' in page and "function ufTl(" in page, "usage flow: timeline is its own canvas below the flow")
ok("ufStrip" not in page and "g.sy" not in grab("ufDraw") and "seekAt" not in grab("ufDraw"), "usage flow canvas no longer holds the timeline strip")
ok("function fxHover(cv, tipEl, hits, e, extra, pin)" in page and "fxHover(cv, tip, UF.hits, e, null, true)" in page, "usage flow tooltip is pinned to the far corner")
ok("UF_MINB" in grab("ufDraw") and "高度不按比例" in grab("ufDraw") and "比例尺" in grab("ufDraw"), "min band width / card heights not-to-scale / scale bar are drawn and labelled")
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""导出 / 导入：md/html 往返；时间线 API 已移除。临时目录。"""
import json, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-ex-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
info = demo_env.build(T)
H = T / "home"
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
from cc_sessionsync import sessions as S, exports as EX
import cc_sessionsync.timeline as TL

CL = H / ".claude"
f = sorted((CL / "projects").glob("*/*.jsonl"))[0]
sid = f.stem
ex = EX.export_many([{"agent": "claude", "id": sid}], "md")
ok(ex.get("files"), f"export md {ex}")
im = EX.import_files(ex["files"], None, False, False)
ok(im.get("ok") and im.get("made"), f"import md back {im}")
S.undo(im["op"])
exh = EX.export_many([{"agent": "claude", "id": sid}], "html")
imh = EX.import_files(exh["files"], None, False, False)
ok(imh.get("ok") and imh.get("made"), "import html back")
S.undo(imh["op"])

page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")
ok("pg-timeline" not in page and "/api/timeline/" not in page and "R.timeline" not in page, "timeline UI gone")
ok(all(k in page for k in ("/api/import", "/api/export", 'id="ssImp"', "imDlg")), "import/export wired")
ok("时间线与备份" not in page, "no 时间线与备份 label")

ok(not hasattr(TL, "restore") and not hasattr(TL, "versions"), "timeline restore/versions removed")
ok(hasattr(TL, "write_claude_session"), "import write helper kept")

print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

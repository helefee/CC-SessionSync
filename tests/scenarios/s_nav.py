# -*- coding: utf-8 -*-
"""导航：会话转换与时间线与备份已移除；顶栏为 账号同步 / 用量 / …；会话页保留导入导出。"""
import os, re, sys, pathlib, tempfile
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
ok("timeline" not in navp and "时间线与备份" not in top and "backups" not in navp, "nav has no 时间线与备份")
ok(not any(p in navp for p in ("codex", "dsh", "cursor", "convert", "timeline")), f"no convert/timeline in nav ({navp})")
ok(set(navp) <= {"sync", "usage", "models", "diag", "settings", "about"}, f"nav pages ok ({navp})")
ok(navp[:2] == ["sync", "usage"], f"top bar starts 账号同步 / 用量 ({navp})")
for p in ("codex", "dsh", "cursor", "convert", "timeline"):
    ok(f'id="pg-{p}"' not in page, f"page {p} removed")
ok("PAGE_ALIAS" not in page and "R.convert" not in page and "R.timeline" not in page, "convert/timeline JS gone")
ok("/api/timeline/" not in page and "/api/backup/now" not in page, "no timeline APIs in page")
ok("imDlg" in page and "/api/import" in page and "/api/export" in page and 'id="ssImp"' in page, "import/export remain on sessions")
ok("syHist" in page and "/api/sync/history" in page, "sync history remains on 账号同步")
ok("会话自动备份" not in page and "stBkE" not in page, "no session auto-backup setting")
src = (ROOT / "cc_sessionsync" / "tray.py").read_text(encoding="utf-8")
ok("会话转换" not in src and "时间线与备份" not in src, "tray has no convert/timeline items")
ok("convert_skip_preview" not in page, "no skip-convert-preview setting")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

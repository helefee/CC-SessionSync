# -*- coding: utf-8 -*-
"""0.4.3 中文 / English 界面：对照表齐全（tools/i18n_extract.py --check）、翻译规则（整段认识才换，用户内容不动）、
设置里的界面语言（auto / zh / en，缺省跟随系统）、页面里带不带对照表、托盘菜单文字。全在临时目录。"""
import json, os, re, subprocess, sys, tempfile, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import demo_env
T = Path(tempfile.mkdtemp(prefix="ccss-i18n-"))
os.environ.update(demo_env.env_for(T), CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1",
                  CC_SESSIONSYNC_NO_WATCHER="1", CC_SESSIONSYNC_KEYSTORE="file", CC_SESSIONSYNC_UPDATE_URL=(T / "nope.json").as_uri())
for k in ("CC_SESSIONSYNC_LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE"):
    os.environ.pop(k, None)
(T / "ss").mkdir(parents=True, exist_ok=True)
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
CJK = re.compile(r"[\u4e00-\u9fff]")

p = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n_extract.py"), "--check"], capture_output=True, text=True, encoding="utf-8")
ok(p.returncode == 0, "every UI fragment has an English entry: " + p.stdout.strip())

from cc_sessionsync import i18n, settings, ui, tray
d = i18n.table()
ok(len(d) > 1500 and all(not CJK.search(v) for v in d.values()), f"{len(d)} entries, no Chinese left in English values")
tr = i18n.translate
ok(tr("检查更新") == "Check for updates" and tr("隐藏窗口") == "Hide window", "whole strings")
ok(tr("12 个会话") == "12 sessions", "fragments + dropped measure word")
user = "帮我把登录页的白屏问题修一下"
ok(tr(user) == user, "user text that is not fully known stays untouched")
ok(tr("hello") == "hello" and tr("") == "", "non-Chinese passes through")

if sys.platform != "win32":                       # Windows 看系统界面语言（GetUserDefaultUILanguage），不看 LANG
    os.environ["LANG"] = "zh_CN.UTF-8"; ok(i18n.system_lang() == "zh", "LANG zh → zh")
    os.environ["LANG"] = "en_US.UTF-8"; ok(i18n.system_lang() == "en", "LANG en → en")
os.environ["CC_SESSIONSYNC_LANG"] = "zh"; ok(i18n.system_lang() == "zh" and i18n.lang() == "zh", "system zh → zh (default auto)")
os.environ["CC_SESSIONSYNC_LANG"] = "en"; ok(i18n.system_lang() == "en" and i18n.lang() == "en", "system en → en")
ok(settings.get("lang") == "auto", "default setting is auto (follow system)")
settings.update(lang="zh"); ok(i18n.lang() == "zh" and i18n.page_payload() == "null", "forced 中文 → no dictionary in page")
settings.update(lang="en"); pl = json.loads(i18n.page_payload()); ok(pl["lang"] == "en" and pl["dict"]["检查更新"] == "Check for updates", "forced English → dictionary in page")
settings.update(lang="auto")

# 托盘：模型还是中文（测试 / 逻辑用），显示时换
os.environ["CC_SESSIONSYNC_LANG"] = "en"
ok(tray._t("立即同步 Claude 会话") == "Sync Claude sessions now" and tray._t("退出") == "Quit", "tray menu text in English: " + tray._t("立即同步 Claude 会话"))
os.environ["CC_SESSIONSYNC_LANG"] = "zh"
ok(tray._t("退出") == "退出", "tray menu stays Chinese in zh")
os.environ["CC_SESSIONSYNC_LANG"] = "en"            # 下面当作系统是英文

# 网页：设置接口 + 页面里的对照表
import threading
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui.H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
html = urllib.request.urlopen(base + "/").read().decode()
tok = re.search(r'TOKEN = "([^"]+)"', html).group(1)
def api(path, body=None):
    rq = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={"X-Token": tok, "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(rq).read())
    except urllib.error.HTTPError as e:
        return {"http": e.code, **json.loads(e.read() or b"{}")}
ok('const I18N = {"lang": "en"' in html and 'LANG = "en"' in html, "system en + auto → page carries the English table")
s = api("/api/settings"); ok(s.get("lang") == "auto" and s.get("lang_system") == "en" and s.get("lang_now") == "en", "settings view: lang auto, system en")
r = api("/api/settings", {"lang": "zh"}); ok(r.get("ok") and r.get("lang_now") == "zh", "switch to 中文")
html = urllib.request.urlopen(base + "/").read().decode()
ok("const I18N = null" in html and 'LANG = "zh"' in html, "page has no table in 中文")
r = api("/api/settings", {"lang": "fr"}); ok(r.get("http") == 400 or r.get("error"), "unknown language rejected")
api("/api/settings", {"lang": "auto"})
srv.shutdown()

print("FAILS:", FAILS) if FAILS else print("ALL OK")
sys.exit(1 if FAILS else 0)

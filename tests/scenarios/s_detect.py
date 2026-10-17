# -*- coding: utf-8 -*-
import os, sys, pathlib, json, tempfile, time
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="det27-")); H = T / "home"; H.mkdir()
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ab"), APPDATA=str(H / "AppData/Roaming"), LOCALAPPDATA=str(H / "AppData/Local"))
for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CC_SESSIONSYNC_FAKE_APPS", "CC_SESSIONSYNC_FAKE_WSL"): os.environ.pop(k, None)
from cc_sessionsync import detect, apps
FAILS = []
def T_(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
U = "C:\\Users\\Friend"
cases = [
  ("claude.exe", U + "\\.local\\bin\\claude.exe", "claude", [("claude", "cli")], "native installer"),
  ("CLAUDE.EXE", U.upper() + "\\.LOCAL\\BIN\\CLAUDE.EXE", "", [("claude", "cli")], "native, upper case"),
  ("node.exe", "C:\\Program Files\\nodejs\\node.exe", '"C:\\Program Files\\nodejs\\node.exe" ' + U + "\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js --resume", [("claude", "npm")], "npm (node cli.js)"),
  ("Node.exe", "", "node /usr/lib/node_modules/@Anthropic-AI/claude-code/cli.mjs", [("claude", "npm")], "npm, mixed case, posix slashes"),
  ("claude.exe", U + "\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe", "", [("claude", "npm")], "npm package native bin"),
  ("Claude.exe", U + "\\AppData\\Local\\AnthropicClaude\\app-0.14.10\\Claude.exe", "", [("claude", "desktop")], "desktop Squirrel"),
  ("Claude.exe", "C:\\Program Files\\WindowsApps\\Claude_0.14.10.0_x64__pzs8sxrjxfjjc\\app\\Claude.exe", "", [("claude", "desktop")], "desktop MSIX"),
  ("claude.exe", U + "\\AppData\\Roaming\\Claude\\claude-code\\2.1.3\\claude.exe", "", [("claude", "desktop-cli")], "desktop-bundled claude-code"),
  ("claude.exe", U + "\\.vscode\\extensions\\anthropic.claude-code-2.0.9-win32-x64\\resources\\native-binary\\claude.exe", "", [("claude", "vscode")], "VS Code ext native"),
  ("node.exe", "", "node " + U + "\\.cursor\\extensions\\anthropic.claude-code-1.0.0\\cli.js", [("claude", "vscode")], "Cursor ext node"),
  ("wsl.exe", "C:\\Windows\\System32\\wsl.exe", "", [("claude", "wsl"), ("codex", "wsl")], "WSL"),
  ("node.exe", "", "node C:\\npm\\node_modules\\@openai\\codex\\bin\\codex.js", [("codex", "npm")], "codex npm"),
  ("codex.exe", U + "\\AppData\\Roaming\\npm\\node_modules\\@openai\\codex\\vendor\\codex.exe", "", [("codex", "npm")], "codex npm native"),
  ("ChatGPT.exe", "C:\\Program Files\\WindowsApps\\OpenAI.Codex_1.0.0.0_x64__abc\\ChatGPT.exe", "", [("codex", "desktop")], "codex desktop"),
  ("codex.exe", U + "\\.vscode\\extensions\\openai.chatgpt-0.4.1\\bin\\codex.exe", "", [("codex", "vscode")], "codex VS Code ext"),
  ("codex.exe", "D:\\bin\\codex.exe", "codex", [("codex", "cli")], "codex cli"),
  ("node.exe", "", "node server.js", [], "unrelated node"),
  ("Code.exe", "C:\\VSCode\\Code.exe", "", [], "VS Code itself"),
]
for n, e, c, want, m in cases:
    got = detect.classify(n, e, c); T_(got == want, f"classify {m}: {got}")
def fake(ps):
    f = T / "procs.json"; f.write_text(json.dumps(ps)); os.environ["CC_SESSIONSYNC_FAKE_PROCS"] = str(f)
def sett(d):
    (T / "ab").mkdir(exist_ok=True); (T / "ab/settings.json").write_text(json.dumps(d)); detect.forget()
# 1 nothing at all
fake([]); sett({}); r = detect.report()
T_(not r["claude"]["available"] and r["claude"]["state"] == "none" and "~/.claude" in r["claude"]["why"].replace("\\", "/"), "nothing → hidden, reason lists checked paths")
T_(not apps.run_status()["claude"] and "detail" in apps.run_status(), "apps.run_status uses detect")
# 2 friend case: npm claude running only (no claude.exe)
fake([{"name": "node.exe", "exe": "C:\\nodejs\\node.exe", "cmd": "node C:\\npm\\node_modules\\@anthropic-ai\\claude-code\\cli.js"}])
r = detect.report(); T_(r["claude"]["available"] and r["claude"]["state"] == "running" and r["claude"]["kinds"] == ["npm"], "npm-only claude → running")
# 3 data only (no process): recent session file
fake([]); p = H / ".claude/projects/D--x"; p.mkdir(parents=True); (p / "a.jsonl").write_text("{}")
r = detect.report(); T_(r["claude"]["available"] and r["claude"]["state"] == "recent", "no process but recent sessions → shown (recent)")
os.utime(p / "a.jsonl", (time.time() - 5 * 86400,) * 2); r = detect.report()
T_(r["claude"]["available"] and r["claude"]["state"] == "data", "old data → still shown (data)")
import shutil; shutil.rmtree(H / ".claude")
# 4 CLAUDE_CONFIG_DIR
cd = T / "cfgdir"; cd.mkdir(); os.environ["CLAUDE_CONFIG_DIR"] = str(cd); r = detect.report()
T_(r["claude"]["available"] and r["claude"]["dirs"][0]["label"].startswith("环境变量"), "CLAUDE_CONFIG_DIR checked + counts"); del os.environ["CLAUDE_CONFIG_DIR"]
# 5 MSIX virtualized dir
m = H / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude-code-sessions/a/o"; m.mkdir(parents=True); (m / "local_1.json").write_text("{}")
r = detect.report(); T_(r["claude"]["available"] and any("MSIX" in d["label"] and d["exists"] for d in r["claude"]["dirs"]), "MSIX virtualized dir found")
from cc_sessionsync import claude_sync
_p = sys.platform; sys.platform = "win32"
try: T_("Packages" in str(claude_sync.claude_app_dir()), "claude_app_dir falls back to MSIX path when %APPDATA%\\Claude missing")
finally: sys.platform = _p
shutil.rmtree(H / "AppData/Local/Packages")
# 6 always show + manual dir
sett({"claude_show": "always"}); r = detect.report(); T_(r["claude"]["available"] and r["claude"]["state"] == "forced", "always-show override")
md = T / "mydir"; (md / "projects/p").mkdir(parents=True); (md / "projects/p/s.jsonl").write_text("{}")
sett({"claude_dir": str(md)}); r = detect.report(); T_(r["claude"]["available"] and r["claude"]["dirs"][0]["label"] == "设置里指定的" and r["claude"]["state"] == "recent", "manual data dir")
sett({})
# 7 WSL running, claude inside WSL
fake([{"name": "wslhost.exe", "exe": "C:\\Windows\\System32\\wslhost.exe"}]); W = T / "wsl"; (W / "Ubuntu/home/u/.claude/projects/p").mkdir(parents=True)
(W / "Ubuntu/home/u/.claude/projects/p/s.jsonl").write_text("{}")
os.environ.update(CC_SESSIONSYNC_FAKE_WSL="Ubuntu", CC_SESSIONSYNC_WSL_ROOT=str(W)); r = detect.report()
T_(r["claude"]["available"] and r["claude"]["state"] == "recent" and "WSL" in r["claude"]["why"] and any(d["label"] == "WSL Ubuntu" and d["exists"] for d in r["claude"]["dirs"]), "WSL: dirs inside the distro found")
del os.environ["CC_SESSIONSYNC_FAKE_WSL"]
# 8 denied processes counted, never crash
fake([{"name": "node.exe", "denied": True}, {"name": "claude.exe", "exe": U + "\\.local\\bin\\claude.exe"}]); r = detect.report()
T_(r["procs"]["denied"] == 1 and r["claude"]["running"], "access-denied counted, others still matched")
# 9 Codex data only
fake([]); (H / ".codex/sessions/2026/10/17").mkdir(parents=True); (H / ".codex/sessions/2026/10/17/rollout-x.jsonl").write_text("{}")
r = detect.report(); T_(r["codex"]["available"] and r["codex"]["state"] == "recent", "codex data only → shown")
os.environ["CODEX_HOME"] = str(T / "cxh"); (T / "cxh").mkdir(); r = detect.report()
T_(r["codex"]["dirs"][0]["label"] == "环境变量 CODEX_HOME" and r["codex"]["dirs"][0]["exists"], "CODEX_HOME checked")
# 10 diag text: masked home, no secrets
t = detect.text(r); T_(str(H) not in t and "~" in t and "[Claude Code]" in t and "[Codex]" in t, "diag text masks home")
# 11 detection exception → shown
os.environ["CC_SESSIONSYNC_FAKE_PROCS"] = str(T / "procs.json"); orig = detect.report; detect.report = lambda: 1 / 0; detect.forget()
b = detect.brief(); T_(b["claude"] and b["detail"]["claude"]["state"] == "error", "detector crash → still shown"); detect.report = orig
print("FAILS", FAILS)
sys.exit(1 if [v for k, v in list(globals().items()) if k in ("fails", "FAILS") and v] else 0)

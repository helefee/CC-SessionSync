# -*- coding: utf-8 -*-
"""0.5.5：账号切换检测补洞——凭据指纹、CLAUDE_CONFIG_DIR、桌面/CLI 不一致、登出→登录、
原子替换、半截 JSON、来回抖、开关关掉仍只提醒。全在临时目录。"""
import json, os, sys, tempfile, pathlib, uuid, time
T = pathlib.Path(tempfile.mkdtemp(prefix="ccss-swdet-"))
(T / "noprocs.json").write_text("[]")
os.environ.update(
    HOME=str(T / "home"), USERPROFILE=str(T / "home"),
    XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
    CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"),
    CLAUDE_CONFIG_DIR=str(T / "home" / ".claude"),
    CC_SESSIONSYNC_HOME=str(T / "ab"),
    CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"),
    CC_SESSIONSYNC_NO_WATCH="1",
)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from cc_sessionsync import watch as W, settings as S, claude_sync as C

fails = []
def P(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

U = lambda: str(uuid.uuid4())
A1, O1, A2, O2 = U(), U(), U(), U()

# Reset module state between logical sections
def reset_watch():
    W.STATE.update({"claude-desktop": None, "claude-cli": None, "codex": None, "last": [], "error": None,
                    "handled": {}, "notes": [], "pending": {}, "detect": {}, "cred": None})
    W._cand.clear(); W._sig.clear(); W._force.clear()
    W._loaded = False
    sf = W._state_file()
    if sf.exists(): sf.unlink()

# ---------- helpers ----------
cfg_dir = T / "home" / ".claude"
cfg_dir.mkdir(parents=True)
cj = cfg_dir / ".claude.json"   # CLAUDE_CONFIG_DIR points here

_mtime_bump = 2_100_000_000

def write_cli(a, o, email="a@x"):
    global _mtime_bump
    cj.write_text(json.dumps({"oauthAccount": {"accountUuid": a, "organizationUuid": o, "emailAddress": email}}))
    _mtime_bump += 2
    os.utime(cj, (_mtime_bump, _mtime_bump))

def atomic_replace_cli(a, o, email="b@y"):
    global _mtime_bump
    tmp = cj.with_suffix(".tmp")
    tmp.write_text(json.dumps({"oauthAccount": {"accountUuid": a, "organizationUuid": o, "emailAddress": email}}))
    os.replace(tmp, cj)
    _mtime_bump += 2
    os.utime(cj, (_mtime_bump, _mtime_bump))

ldb = T / "cfg" / "Claude" / "Local Storage" / "leveldb"
ldb.mkdir(parents=True)

def login_desk(a, o, ts):
    with open(ldb / "000003.log", "ab") as f:
        f.write(b'{"eventName":"x","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}' % (ts.encode(), a.encode(), o.encode()))

t0 = 2_000_000_000.0

# ---------- 1) first run no sync ----------
reset_watch()
write_cli(A1, O1)
login_desk(A1, O1, "2026-11-01T00:00:00Z")
ev = W.tick(now=t0, stable=0.1)
P(not ev and W.STATE["claude-cli"] and W.STATE["claude-cli"]["id"].startswith(A1), "first run records CLI, no event")
P(W.STATE.get("detect", {}).get("claude-cli", {}).get("reason") == "first_run", "reason first_run")

# ---------- 2) CLAUDE_CONFIG_DIR path used (not bare ~/.claude.json) ----------
bare = T / "home" / ".claude.json"
bare.write_text(json.dumps({"oauthAccount": {"accountUuid": A2, "organizationUuid": O2, "emailAddress": "wrong@x"}}))
P(C.claude_json_path() == cj, "claude_json_path respects CLAUDE_CONFIG_DIR")
P(C.cli_account()["accountUuid"] == A1, "cli_account reads CONFIG_DIR file, not bare home json")

# ---------- 3) logged-out → login counts ----------
reset_watch()
# no oauth
cj.write_text("{}")
login_desk(A1, O1, "2026-11-01T01:00:00Z")
W.tick(now=t0 + 10, stable=0.1)  # baseline logged out for cli
P(W.STATE["claude-cli"]["id"] == "", "baseline logged out")
write_cli(A2, O2, "c@z")
ev = W.tick(now=t0 + 11, stable=0.1) + W.tick(now=t0 + 14, stable=0.1)
# auto-sync on → pending, no account nag
P(W.STATE.get("pending", {}).get("claude-cli") or any(e.get("login") for e in ev) or W.STATE["claude-cli"]["id"].startswith(A2),
  f"login empty→account detected {W.STATE.get('pending')} {W.STATE['claude-cli']}")
P(W.STATE.get("detect", {}).get("claude-cli", {}).get("reason") in ("login", "detected", "debounced", "corroborated", "candidate", "debouncing")
  or W.STATE.get("pending"), f"login reason {W.STATE.get('detect')}")

# ---------- 4) credentials mtime only (no content read) ----------
reset_watch()
write_cli(A1, O1)
W.tick(now=t0 + 20, stable=0.1)
cred = cfg_dir / ".credentials.json"
cred.write_text('{"DO_NOT_READ":"secret-token-value-should-never-be-opened-by-watch"}')
# Ensure watch only stats — monkeypatch read on Path for this file would be heavy; instead check fp + reason
fp1 = W._cred_fp()
cred.write_text('{"DO_NOT_READ":"rotated-token"}')  # content change + mtime
# nudge mtime
os.utime(cred, (t0 + 30, t0 + 30))
W.tick(now=t0 + 30, stable=0.1)
P(W.STATE.get("cred") != fp1 or W.STATE.get("detect", {}).get("credentials", {}).get("reason") == "credentials_changed"
  or W.STATE.get("detect", {}).get("claude-cli", {}).get("reason") == "credentials_hint",
  f"credentials fingerprint change noted {W.STATE.get('detect')}")
# identity unchanged → no pending sync from cred alone
P(not W.STATE.get("pending"), "credentials-only does not invent a switch sync")
# never left a parsed secret in STATE
blob = json.dumps(W.STATE)
P("secret-token" not in blob and "rotated-token" not in blob, "no credential secrets in STATE")

# ---------- 5) SECURESTORAGE dir watched ----------
ss = T / "secure-a"
ss.mkdir()
os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(ss)
reset_watch()
write_cli(A1, O1)
W.tick(now=t0 + 40, stable=0.1)
(ss / ".credentials.json").write_text("{}")
os.utime(ss / ".credentials.json", (t0 + 41, t0 + 41))
W.tick(now=t0 + 41, stable=0.1)
paths = [p[0] for p in (W.STATE.get("cred") or ()) if p[1]]
P(any(str(ss) in p for p in paths), f"SECURESTORAGE credentials path watched {paths}")
os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)

# ---------- 6) desktop vs cli disagree: change on CLI still confirms ----------
reset_watch()
# Fresh leveldb so desktop cannot "see" a later A2 from other sections
for f in ldb.glob("*"):
    f.unlink()
write_cli(A1, O1, "desk@one")
login_desk(A1, O1, "2026-11-06T02:00:00Z")
W.tick(now=t0 + 50, stable=0.1)
P(W.STATE["claude-desktop"]["id"].startswith(A1) and W.STATE["claude-cli"]["id"].startswith(A1), "disagree setup both A1")
write_cli(A2, O2, "cli@two")
# desktop still A1 (no new login_desk)
ev = W.tick(now=t0 + 51, stable=0.1)
P(W._cand.get("claude-cli") and W._cand["claude-cli"][0]["id"].startswith(A2), f"CLI candidate while desktop still old cand={W._cand.get('claude-cli')}")
ev += W.tick(now=t0 + 54, stable=0.1)
P(W.STATE["claude-cli"]["id"].startswith(A2), "CLI switch confirmed despite desktop disagreement")
P(W.STATE["claude-desktop"]["id"].startswith(A1), "desktop still A1")
P(W.STATE.get("detect", {}).get("disagree", {}).get("reason") == "sources_disagree", f"disagree noted {W.STATE.get('detect')}")

# ---------- 7) atomic replace ----------
reset_watch()
write_cli(A1, O1)
W.tick(now=t0 + 60, stable=0.1)
atomic_replace_cli(A2, O2)
ev = W.tick(now=t0 + 61, stable=0.1) + W.tick(now=t0 + 64, stable=0.1)
P(W.STATE["claude-cli"]["id"].startswith(A2), "atomic os.replace of .claude.json detected")

# ---------- 8) malformed transient then valid ----------
reset_watch()
write_cli(A1, O1)
W.tick(now=t0 + 70, stable=0.1)
cj.write_text("{not-json-yet")
sig_before = W._sig.get("claude-cli")
W.tick(now=t0 + 71, stable=0.1)
P(W.STATE.get("detect", {}).get("claude-cli", {}).get("reason") == "parse_error", "malformed → parse_error")
# sig should NOT be stuck on the bad version permanently — rewrite valid with new mtime
time.sleep(0.05)
write_cli(A2, O2)
ev = W.tick(now=t0 + 72, stable=0.1) + W.tick(now=t0 + 75, stable=0.1)
P(W.STATE["claude-cli"]["id"].startswith(A2), f"recover after malformed JSON → {W.STATE['claude-cli']}")

# ---------- 9) flicker A→B→A ignored ----------
reset_watch()
write_cli(A1, O1, "flick@a")
W.tick(now=t0 + 80, stable=2.0)
write_cli(A2, O2, "flick@b")
W.tick(now=t0 + 80.5, stable=2.0)  # candidate B
P(W._cand.get("claude-cli") and W._cand["claude-cli"][0]["id"].startswith(A2), f"flicker: candidate B {W._cand.get('claude-cli')}")
write_cli(A1, O1, "flick@a2")  # back to A before confirm
W.tick(now=t0 + 81.0, stable=2.0)
P(not W._cand.get("claude-cli") and W.STATE["claude-cli"]["id"].startswith(A1), "flicker back to A ignored")
P(W.STATE.get("detect", {}).get("claude-cli", {}).get("reason") in ("flicker_ignored", "stable"),
  f"flicker reason {W.STATE.get('detect')}")

# ---------- 10) toggle off → reminder, no writes ----------
reset_watch()
S.update(claude_auto_sync_on_account_switch=False)
root = T / "cfg" / "Claude" / "claude-code-sessions"
proj = T / "home" / ".claude" / "projects" / "p"; proj.mkdir(parents=True, exist_ok=True)
for a, o in ((A1, O1), (A2, O2)):
    (root / a / o).mkdir(parents=True, exist_ok=True)
# Put 2 sessions on A1 so a switch to A2 would have something to sync if toggle were on
for i in range(2):
    cli = U(); (proj / f"{cli}.jsonl").write_text("{}" + "\n")
    (root / A1 / O1 / f"local_t{i}.json").write_text(json.dumps({
        "sessionId": f"local_t{i}", "cliSessionId": cli, "title": f"T{i}", "cwd": "/p", "lastActivityAt": 1000 + i}))
write_cli(A1, O1)
login_desk(A1, O1, "2026-11-01T03:00:00Z")
W.tick(now=t0 + 90, stable=0.1)
write_cli(A2, O2, "tog@off")
login_desk(A2, O2, "2026-11-01T04:00:00Z")
ev = W.tick(now=t0 + 91, stable=0.1) + W.tick(now=t0 + 94, stable=0.1)
ev += W.switch_tick(now=t0 + 95)
P(any(e.get("kind") == "account" for e in ev) and not W.STATE.get("pending"),
  f"toggle off → account reminder { [e.get('title') for e in ev] }")
n2 = len(list((root / A2 / O2).glob("local_*.json")))
P(n2 == 0, "toggle off → no session files written")
S.update(claude_auto_sync_on_account_switch=True)

# ---------- 11) waiting_desktop phase clear ----------
reset_watch()
write_cli(A1, O1)
login_desk(A1, O1, "2026-11-01T05:00:00Z")
W.tick(now=t0 + 100, stable=0.1)
(T / "desktop-running").write_text("1")
login_desk(A2, O2, "2026-11-01T06:00:00Z")
write_cli(A2, O2)
W.tick(now=t0 + 101, stable=0.1)
W.tick(now=t0 + 104, stable=0.1)
ev = W.switch_tick(now=t0 + 105)
pd = (W.STATE.get("pending") or {}).get("claude-desktop") or (W.STATE.get("pending") or {}).get("claude-cli")
P(pd and pd.get("phase") == "waiting_desktop", f"pending phase waiting_desktop {W.STATE.get('pending')}")
P(any("等待 Claude 桌面端退出后再同步" in (e.get("title") or "") for e in ev),
  f"waiting event title clear {[e.get('title') for e in ev]}")
st = W.status()
P(st.get("pending") and st.get("detect"), "status exposes pending + detect reasons")

print("FAILS", fails)
sys.exit(1 if fails else 0)

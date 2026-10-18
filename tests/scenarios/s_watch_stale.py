# -*- coding: utf-8 -*-
"""0.2.5：「检测到切换账号」误报（照着用户电脑上的实际情况复现）。
桌面端 Local Storage 里有个几个月前的旧文件（000005.ldb，别的账号 B 的遥测事件，9 月的时间戳）一直没删；当前账号 A 的事件
在 015708.log 里，发出去以后会被删掉 / 压缩走。A 的事件暂时都没了时，「时间戳最新的那条」落到旧文件的 B 上 → 以前会报「换号」，
等 A 的新事件写进来又「换回来」。另外：基线要跨重启保存、切 API 模式算模式切换、同步完 / 不缺会话就算处理完不再提醒。全在临时目录。"""
import importlib, os, sys, pathlib, json, tempfile, time, uuid, base64
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="ccss-stale-")); H = T / "home"; H.mkdir()
AD = H / "AppData/Roaming"; LA = H / "AppData/Local"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(AD), XDG_CONFIG_HOME=str(AD), LOCALAPPDATA=str(LA),
                  CLAUDE_CONFIG_DIR=str(T / "claude"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"),
                  CC_SESSIONSYNC_NO_WATCH="1", CC_SESSIONSYNC_NO_USAGE="1", CC_SESSIONSYNC_NO_BACKUP="1")
for k in ("CODEX_HOME", "CC_SESSIONSYNC_FAKE_APPS", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
from cc_sessionsync import claude_sync as C, events, ui, watch
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
P1 = AD / "Claude"; L = P1 / "Local Storage/leveldb"; L.mkdir(parents=True)
A, OA, B, OB, X, OX = (str(uuid.uuid4()) for _ in range(6))
proj = T / "claude/projects/p"; proj.mkdir(parents=True)
now = time.time()
def tele(name, acct, org, ts, mt=None):
    f = L / name
    f.write_bytes(b"".join(b'\x00\x01{"eventName":"claudeai.x","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s","device_id":"d"}\x00'
                           % (t.encode(), acct.encode(), org.encode()) for t in ([ts] if isinstance(ts, str) else ts)))
    mt = mt or time.time(); os.utime(f, (mt, mt)); return f
def reg(acct, org, title):
    d = P1 / "claude-code-sessions" / acct / org; d.mkdir(parents=True, exist_ok=True); cli = str(uuid.uuid4())
    (proj / f"{cli}.jsonl").write_text(json.dumps({"type": "user", "sessionId": cli, "cwd": "/w", "message": {"role": "user", "content": "hi"}}) + "\n")
    f = d / f"local_{uuid.uuid4()}.json"
    f.write_text(json.dumps({"sessionId": f.stem, "cliSessionId": cli, "title": title, "cwd": "/w", "model": "claude-sonnet-4-5", "lastActivityAt": int(now * 1000)}))
    return f
def fresh():
    C.forget(); C._API_RES["v"] = None
# 用户电脑上的样子：旧的 000005.ldb 里 163 条 B（9 月 4 日），当前的 log 里 A（今天）
tele("000005.ldb", B, OB, [f"2026-09-04T18:{m:02d}:02.907Z" for m in range(51, 59)], mt=now - 40 * 86400)
cur = tele("015708.log", A, OA, ["2026-10-18T05:00:00.000Z", "2026-10-18T06:17:00.000Z"])
reg(A, OA, "A 的会话")
(P1 / "claude-code-sessions" / B / OB).mkdir(parents=True)        # 旧号 B 也留着文件夹（以前登过）
fresh()
ok(C.desktop_account(profile=False)["key"] == f"{A}/{OA}", "both files present → the newest record (A) wins")
ok(watch.tick(1000) == [] and watch.status()["claude-desktop"]["id"] == f"{A}/{OA}", "first tick records the baseline (A)")
ok((T / "ss/watch-state.json").exists() and (T / "ss/desktop-account.json").exists(), "baseline + last-known desktop account persisted")
st = (T / "ss/watch-state.json").read_text()
ok("sk-" not in st and "token" not in st.lower(), "persisted state holds ids / masked labels only")

# A 的事件发出去被删了：只剩旧文件里的 B
cur.unlink(); fresh()
d = C.desktop_account(profile=False)
ok(d["key"] == f"{A}/{OA}" and d["via"] == "leveldb-last-known" and d["stale"]["key"] == f"{B}/{OB}",
   "only the stale Sep-4 B records visible → desktop_account keeps A (last known, newer timestamp)")
ok(C.accounts()["desktop"]["key"] == f"{A}/{OA}", "sync target (accounts) also stays on A")
r = [watch.tick(t) for t in (1010, 1025, 1040, 1100)]
ok(r == [[]] * 4 and not [e for e in events.recent(50) if e["kind"] == "account"], "no '检测到切换账号' while only stale records are visible (hours-long gap)")
ok(watch.status()["claude-desktop"]["id"] == f"{A}/{OA}", "baseline stays A")
# A 的新事件又写进来：也不报「换回来」
cur = tele("015710.log", A, OA, "2026-10-18T08:52:00.000Z"); fresh()
ok([watch.tick(t) for t in (1110, 1125)] == [[], []], "A telemetry back → no 'switched back' event")

# 即使没有 last-known 文件，watcher 自己的时间戳检查也挡住：识别出的「新号」事件比基线旧
(T / "ss/desktop-account.json").unlink(); cur.unlink(); fresh()
ok(C.desktop_account(profile=False)["key"] == f"{B}/{OB}", "(no last-known file) raw pick falls to stale B")
ok([watch.tick(t) for t in (1200, 1215, 1230)] == [[], [], []], "watcher's own guard: B's records older than baseline → not a switch")
cur = tele("015712.log", A, OA, "2026-10-18T09:00:00.000Z"); fresh(); watch.tick(1240)

# 重启：基线从文件读回来；第一次读到的不是「第一次」
importlib.reload(watch)
ok(watch.status()["claude-desktop"]["id"] == f"{A}/{OA}", "after restart: baseline restored from watch-state.json")
cur.unlink(); fresh()
ok([watch.tick(t) for t in (2000, 2015, 2030)] == [[], [], []], "after restart + stale-only read → still no event")

# 真的换号到 X（新事件、更新的时间戳）：两轮确认 → 一条事件，说清楚哪里、谁 → 谁
(P1 / "claude-code-sessions" / X / OX).mkdir(parents=True)
cur = tele("015714.log", X, OX, "2026-10-18T12:21:00.000Z"); fresh()
ok(watch.tick(2040) == [], "real switch seen once → candidate only")
ev = watch.tick(2055)
ok(len(ev) == 1 and ev[0]["title"] == "检测到 Claude 桌面端 账号已切换" and "→" in ev[0]["text"] and ev[0]["who"] == "claude-desktop"
   and ev[0]["new_id"] == f"{X}/{OX}" and not ev[0]["mode_switch"], "real switch → one event naming the source and old → new")
ok(ui.tray_state()["attention"] is True, "tray shows attention for the unhandled switch")
# 同步到 X：算处理完
fresh(); r = C.run(None, False, None, "desktop")
ok(r["ok"] and r["done"] >= 1 and r["target"]["key"] == f"{X}/{OX}", "sync to the new account copies A's session")
ts = ui.tray_state()
ok((ts["acct_handled"].get("claude-desktop") or 0) >= ev[0]["time"] and ts["attention"] is False, "after sync: switch marked handled → banner dropped, no attention")
ok(json.loads((T / "ss/watch-state.json").read_text())["handled"]["claude-desktop"]["id"] == f"{X}/{OX}", "handled persisted")
ok([watch.tick(t) for t in (2100, 2115)] == [[], []], "no re-notify for the handled switch")

# 换回 A：A 已经有所有会话（X 里只有从 A 补过来的）→ 不提醒，只记一笔
cur = tele("015716.log", A, OA, "2026-10-18T14:00:00.000Z"); fresh()
r = [watch.tick(t) for t in (2200, 2215)]
ok(r == [[], []] and watch.status()["claude-desktop"]["id"] == f"{A}/{OA}" and watch.status()["notes"]
   and "不缺会话" in watch.status()["notes"][-1]["text"], "switch to an account that already has everything → no event, note only")

# 切到 API 模式（3P）：算模式切换，不是换账号
P3 = LA / "Claude-3p"; P3.mkdir(parents=True); DEV = str(uuid.uuid4())
(P3 / "ant-did").write_text(base64.b64encode(DEV.encode()).decode())
(P3 / "claude-code-sessions" / DEV / C.PLACEHOLDER_ORG).mkdir(parents=True)
(P3 / "claude_desktop_config.json").write_text(json.dumps({"deploymentMode": "3p"})); fresh()
watch.tick(2300); ev = watch.tick(2315)
ok(len(ev) == 1 and ev[0]["mode_switch"] and ev[0]["title"] == "Claude 桌面端切换到 API 模式（3P）" and "不是换账号" in ev[0]["text"],
   "3P mode change → mode-switch event, not an account switch")
ok(watch.status()["claude-desktop"]["id"].startswith("3p:") and not watch.status()["claude-desktop"]["id"].startswith("3p:3p:"), "3P identity = device bucket")
fresh(); r = C.run(None, False, None, "api")
ok(r["ok"] and (ui.tray_state()["acct_handled"].get("claude-desktop") or 0) >= ev[0]["time"], "sync into API mode marks the mode switch handled")
(P3 / "claude_desktop_config.json").write_text(json.dumps({"deploymentMode": "1p"})); t = time.time() + 5; os.utime(P3 / "claude_desktop_config.json", (t, t)); fresh()
watch.tick(2400); ev = watch.tick(2415)
ok(len(ev) == 1 and ev[0]["mode_switch"] and ev[0]["title"] == "Claude 桌面端切回订阅登录模式", "back to 1p → mode-switch event")
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED"); sys.exit(1 if FAILS else 0)

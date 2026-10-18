# -*- coding: utf-8 -*-
"""0.2.4：Claude 桌面端 API 模式（3P）的数据目录 Claude-3p：设备号 + 占位组织、分组、同步目标、对话文件不在的登记。全在临时目录。"""
import os, sys, pathlib, json, tempfile, time, uuid, base64
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
T = Path(tempfile.mkdtemp(prefix="ccss-3p-")); H = T / "home"; H.mkdir()
AD = H / "AppData/Roaming"; LA = H / "AppData/Local"
os.environ.update(HOME=str(H), USERPROFILE=str(H), CC_SESSIONSYNC_HOME=str(T / "ss"), APPDATA=str(AD), XDG_CONFIG_HOME=str(AD), LOCALAPPDATA=str(LA),
                  CLAUDE_CONFIG_DIR=str(T / "claude"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "procs.json"), CC_SESSIONSYNC_FAKE_APP=str(T / "running"))
for k in ("CODEX_HOME", "CC_SESSIONSYNC_FAKE_APPS", "CLAUDE_USER_DATA_DIR"): os.environ.pop(k, None)
(T / "procs.json").write_text("[]")
from cc_sessionsync import claude_sync as C, sessions as S, selfcheck, ui
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
P1 = AD / "Claude"; P3 = LA / "Claude-3p"; PH = C.PLACEHOLDER_ORG
A1, O1, DEV = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
proj = T / "claude/projects/p"; proj.mkdir(parents=True)
now = time.time()
def ldb(base, acct, org):
    l = base / "Local Storage/leveldb"; l.mkdir(parents=True, exist_ok=True)
    (l / "000003.log").write_bytes(b'{"eventName":"b","clientTimestamp":"2026-10-17T05:00:00Z","accountUuid":"%s","organizationUuid":"%s"}' % (acct.encode(), org.encode()))
def reg(d, title, model="claude-sonnet-5-5", transcript=True, mt=None):
    d.mkdir(parents=True, exist_ok=True); cli = str(uuid.uuid4())
    if transcript: (proj / f"{cli}.jsonl").write_text(json.dumps({"type": "user", "sessionId": cli, "cwd": "/w", "message": {"role": "user", "content": "hi"}}) + "\n")
    f = d / f"local_{uuid.uuid4()}.json"
    f.write_text(json.dumps({"sessionId": f.stem, "cliSessionId": cli, "title": title, "cwd": "/w", "model": model, "lastActivityAt": int((mt or now) * 1000)}))
    if mt: os.utime(f, (mt, mt))
    return cli, f
# 订阅登录：一个号两个会话
ldb(P1, A1, O1)
s1, _ = reg(P1 / "claude-code-sessions" / A1 / O1, "订阅里的会话 1", mt=now - 3600)
s2, _ = reg(P1 / "claude-code-sessions" / A1 / O1, "订阅里的会话 2", mt=now - 3500)
# API 模式：设备号 + 占位组织；一个能打开的、一个对话文件已经不在的
P3.mkdir(parents=True); (P3 / "ant-did").write_text(base64.b64encode(DEV.encode()).decode())
(AD / "Claude-3p").mkdir(parents=True); (AD / "Claude-3p/config.json").write_text('{"locale":"zh-CN"}')
B3 = P3 / "claude-code-sessions" / DEV / PH
s3, _ = reg(B3, "API 模式里的会话", model="claude-opus-4-6-thinking", mt=now - 600)
s4, f4 = reg(B3, "对话文件丢了的会话", model="claude-opus-4-6-thinking", transcript=False, mt=now - 500)
(P3 / "configLibrary").mkdir(); (P3 / "configLibrary/_meta.json").write_text(json.dumps({"appliedId": "c4b3", "entries": [{"id": "c4b3"}]}))
SECRET = "sk-test-DO-NOT-LEAK-123"
(P3 / "configLibrary/c4b3.json").write_text(json.dumps({"inferenceProvider": "gateway", "inferenceGatewayBaseUrl": "https://gw.secret.example", "inferenceGatewayApiKey": SECRET}))
(P3 / "claude_desktop_config.json").write_text(json.dumps({"deploymentMode": "1p"}))
C.forget()
i = C.api_info()
ok(i["base"] == str(P3) and i["device"] == DEV and i["bucket"] and i["bucket"]["key"] == f"3p:{DEV}/{PH}" and i["bucket"]["count"] == 2, "API 模式：找到 Claude-3p、设备号、占位组织文件夹")
ok(i["provider"] == "gateway" and i["mode"] == "1p", "供应商类型来自 configLibrary 当前那条；deploymentMode=1p")
ok({c["id"] for c in i["cands"]} >= {"local3p", "appdata3p"}, "候选目录含 %LOCALAPPDATA% 和 %APPDATA% 两处")
blob = json.dumps(ui._clean(ui._status())) + json.dumps(ui.diag()) + json.dumps(selfcheck.check_dirs(), ensure_ascii=False)
ok(SECRET not in blob and "gw.secret.example" not in blob, "状态 / 诊断 / 自检里没有 key 和地址")
ok("API 模式" in ui.diag()["text"] and "gateway" in ui.diag()["text"], "诊断里写了 API 模式目录 / 供应商")
sc = [x for x in selfcheck.check_dirs() if x["id"] == "claude-api-mode"]
ok(sc and sc[0]["status"] == "pass" and "订阅登录" in sc[0]["detail"] and str(P3) in sc[0]["detail"], "自检：API 模式那一项")
# 分组
L = S.list_all()
gs = {g["key"]: g for g in L["groups"]}
ok("3p" in gs and gs["3p"]["kind"] == "api" and "API / 第三方供应商（3P）" in gs["3p"]["masked"] and "gateway" in gs["3p"]["masked"], "分组：API / 第三方供应商（3P） · gateway")
it = {x["id"]: x for x in L["items"]}
ok(it[s3]["gacct"] == "3p" and it[s4]["gacct"] == "3p", "只在 API 模式登记的会话归 API 分组，不进未知账号")
ok(not any(g["key"] == "unknown" for g in L["groups"]), "没有未知账号")
ok(it[s4].get("orphan") and it[s4]["why"] == "无法打开（对话文件不存在）" and it[s4]["resume"] is None, "对话文件不在的：显示、标「无法打开（对话文件不存在）」、不给继续命令")
ok(it[s3]["why"] == "在 API 模式里", "订阅模式下 API 模式的会话：在 API 模式里")
ok(gs["3p"]["n"] == 2 and not gs["3p"]["current"], f"API 分组数 2、不是当前（{gs['3p']['n']}）")
# 同步：缺省目标跟 deploymentMode
ok(C.default_source() == "desktop", "deploymentMode=1p → 缺省同步到订阅账号")
p = C.plan(source="desktop")
names = {a["title"]: a for a in p["add"]}
ok(p["ok"] and "API 模式里的会话" in names and "对话文件丢了的会话" not in names and p["skip_missing"] == 1, "订阅账号可以补 API 模式的会话（丢了对话文件的不补）")
ok(names["API 模式里的会话"]["warn"] and "claude-opus-4-6-thinking" in names["API 模式里的会话"]["warn"], "跨模式：提醒模型在另一边可能没有")
(P3 / "claude_desktop_config.json").write_text(json.dumps({"deploymentMode": "3p"})); C.forget()
ok(C.default_source() == "api" and C.desktop_mode()["mode"] == "3p", "deploymentMode=3p → 缺省同步到 API 模式")
p = C.plan()
p = C.plan(source="api")
ok(p["ok"] and p["target"]["key"] == f"3p:{DEV}/{PH}" and len(p["add"]) == 2 and "所有供应商共用" in p["target"]["label"], "API 模式目标：补订阅里的 2 个，标「所有供应商共用」")
ok(all(a["warn"] for a in p["add"]), "补到 API 模式：每个都提醒模型")
L = S.list_all(); it = {x["id"]: x for x in L["items"]}; gs = {g["key"]: g for g in L["groups"]}
ok(it[s3]["visible"] and gs["3p"]["current"], "API 模式下：API 模式的会话算「看得到」，分组是当前")
r = C.run(source="api")
ok(r["done"] == 2 and len(list(B3.glob("local_*.json"))) == 4, "同步进 API 模式文件夹（只写进已有的文件夹）")
led = json.loads((Path(r["backup"]) / "ledger.json").read_text(encoding="utf-8"))
ok(led["key"].startswith("3p:") and led["profile"] == "3p", "账本记的是 API 模式")
rr = C.restore(Path(r["backup"]).name)
ok(rr["ok"] and rr["removed"] == 2 and len(list(B3.glob("local_*.json"))) == 2, "撤回 API 模式的同步")
# 文件夹还没建：不自己建
import shutil
shutil.rmtree(P3 / "claude-code-sessions"); C.forget()
p = C.plan(source="api")
ok(not p["ok"] and "不会自己建" in p["error"] and not (P3 / "claude-code-sessions").exists(), "API 模式还没文件夹：报错、不建")
ok((P3 / "ant-did").exists() and not (AD / "Claude-3p/ant-did").exists(), "不建 ant-did")
# 自动同步跟着模式走（3p 没文件夹 → 不写）
ok(C.auto_tick(force=True) is None, "自动同步：API 模式没文件夹时什么都不写")
# 关闭并同步：apps 里 claude-sync 带 source=api
from cc_sessionsync import apps
ok("api" in C.SOURCES and callable(apps._runners()["claude-sync"]) if hasattr(apps, "_runners") else "api" in C.SOURCES, "claude-sync 任务认 source=api")
print("FAILS", len(FAILS)); sys.exit(1 if FAILS else 0)

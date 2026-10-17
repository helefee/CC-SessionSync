# -*- coding: utf-8 -*-
"""Codex → Claude Code 导入：只在临时目录里跑。"""
import hashlib, json, os, pathlib, subprocess, sys, tempfile, time, uuid
from pathlib import Path
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
T = Path(tempfile.mkdtemp(prefix="cx2cc-")); H = T / "home"; H.mkdir()
(T / "noprocs.json").write_text("[]")
ENV = dict(HOME=str(H), USERPROFILE=str(H), XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
           CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"),
           CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"))
os.environ.update(ENV)
from codex_fixture import rollout, append_turn
from cc_sessionsync import codex_import as X, apps, paths
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
def tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*")) if f.is_file()} if p.exists() else {}
CX, CL = H / ".codex", H / ".claude"
win_cwd = "D:\\work\\shop app"
lin_cwd = str(T / "proj")
long_cwd = "/srv/" + "/".join(["very-long-directory-name-%02d" % i for i in range(9)])
f1, t1 = rollout(CX, win_cwd)
f2, t2 = rollout(CX, lin_cwd, title=None)
f3, t3 = rollout(CX, long_cwd, title="长路径", archived=True)
_, ts = rollout(CX, lin_cwd, title="子代理", thread_source="subagent")
_, te = rollout(CX, lin_cwd, title="exec", originator="codex_exec")
_, tc = rollout(CX, lin_cwd, title="从 Claude 导进来的")
(CX / "external_agent_session_imports.json").write_text(json.dumps({"records": [{"source_path": "\\\\?\\C:\\x.jsonl", "imported_thread_id": tc}]}))
# Claude desktop: one account folder with a template entry, logged in via Local Storage
A, O = str(uuid.uuid4()), str(uuid.uuid4())
sd = T / "cfg/Claude/claude-code-sessions" / A / O; sd.mkdir(parents=True)
(sd / "local_tmpl.json").write_text(json.dumps({"sessionId": "local_tmpl", "cliSessionId": "x", "cwd": "/x", "title": "t", "model": "claude-opus-5-5", "effort": "high", "permissionMode": "default", "lastActivityAt": 1}))
ldb = T / "cfg/Claude/Local Storage/leveldb"; ldb.mkdir(parents=True)
(ldb / "000003.log").write_bytes(b'{"eventName":"e","clientTimestamp":"2026-10-16T00:00:00Z","accountUuid":"%s","organizationUuid":"%s"}' % (A.encode(), O.encode()))
(CL / "projects").mkdir(parents=True)
codex_before = tree(CX)

# --- 列表 / 筛选
th = X.threads(days=0)
ids = {x["thread"] for x in th}
ok(ids == {t1, t2, t3}, f"only user threads (no subagent / exec / imported-from-Claude): {len(ids)}")
ok({x["thread"] for x in X.threads(days=0, include_all=True)} >= {ts, te, tc}, "--all includes them")
ok([x["thread"] for x in X.threads(days=0, cwd=lin_cwd)] == [t2], "--cwd filter")
ok(next(x for x in th if x["thread"] == t3)["archived"], "archived thread listed")
# --- 解析
c = X.parse(f1)
roles = [i["role"] for i in c["items"]]
ok(roles[0] == "user" and "AGENTS.md" not in c["items"][0]["text"] and "SECRET-CODEX-MARKER" in c["items"][0]["text"], "injected context skipped, real prompt kept")
ok(c["stats"]["images"] >= 1 and "[图片" in c["items"][0]["text"], "image → placeholder")
tools = [i for i in c["items"] if i["role"] == "tool"]
ok([t["name"] for t in tools] == ["exec_command", "shell", "apply_patch", "update_plan", "web_search", "github.mcp__github__get_issue"], f"tool calls: {[t['name'] for t in tools]}")
ok(tools[1]["args"] == "npm test -- login" and "退出码 1" in tools[1]["output"], "shell argv unwrapped, exit code kept")
ok("Begin Patch" in tools[2]["args"] and "Success" in tools[2]["output"], "apply_patch input + output")
ok("[completed] 修 onSubmit" in tools[3]["args"], "update_plan summarised")
ok(c["stats"]["reasoning_encrypted"] == 1 and c["stats"]["reasoning"] == 1 and not any(i["role"] == "reasoning" for i in c["items"]), "reasoning dropped by default")
ok(any(i["role"] == "reasoning" for i in X.parse(f1, reasoning=True)["items"]), "--reasoning keeps plaintext summaries")
ok(c["stats"]["bad_lines"] == 1 and c["stats"]["compactions"] == 1, "bad line + compaction counted, not fatal")
ok(c["items"][-1]["text"] == "加好了 login.test.tsx。", "turns after compaction kept")
# --- 拼装
b = X.build(f1, "11111111-1111-1111-1111-111111111111")
recs = b["records"]; msgs = [r for r in recs if r.get("type") in ("user", "assistant")]
ok(all(msgs[i]["type"] != msgs[i + 1]["type"] for i in range(len(msgs) - 1)) and msgs[0]["type"] == "user" and msgs[-1]["type"] == "assistant", "strict user/assistant alternation, ends with assistant")
ok(all(m["parentUuid"] == (msgs[i - 1]["uuid"] if i else None) for i, m in enumerate(msgs)), "parentUuid chain")
ok(recs[-1] == {"type": "custom-title", "customTitle": "（Codex）修登录页的 bug", "sessionId": "11111111-1111-1111-1111-111111111111"}, "custom title")
ok(msgs[0]["cwd"] == win_cwd and msgs[0]["gitBranch"] == "fix/login", "cwd + git branch from Codex meta")
txt = "\n".join(json.dumps(m["message"], ensure_ascii=False) for m in msgs)
ok("line 59" not in txt and "截断" in txt, "long tool output clipped")
ok("````" in txt, "code fence grows when output contains ```")
ok(X.project_slug(win_cwd) == "D--work-shop-app", "slug (Windows path)")
sl = X.project_slug(long_cwd); ok(len(sl) > 200 and sl[:200] == X.re.sub(r"[^A-Za-z0-9]", "-", long_cwd)[:200] and "-" in sl[200:], f"slug >200 gets hash suffix: …{sl[195:]}")
ok(X._js_hash("hello") == 99162322 and X._js_hash("日本") == 835047 and X._js_hash("😀x") == 54959989, "JS string hash matches")
ok(X.build(f2, "x")["title"].startswith("登录页点了没反应"), "no Codex title → first prompt")
ok(tree(CX) == codex_before, "parse/build wrote nothing in ~/.codex")
# --- 演练
before_cl, before_cfg = tree(CL), tree(T / "cfg")
r = X.run(days=0, dry=True)
ok(r["counts"]["new"] == 3 and all(x.get("turns") for x in r["rows"]) and r["desktop"]["key"] == f"{A}/{O}", "dry run: 3 new, built, desktop target = current account")
ok(tree(CL) == before_cl and tree(T / "cfg") == before_cfg and not X.MAP.exists() and not X.LEDGERS.exists(), "dry run wrote nothing")
cli = subprocess.run([sys.executable, "-m", "cc_sessionsync", "codex", "import", "--days", "0"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=dict(os.environ))
ok(cli.returncode == 0 and "演练" in cli.stdout and tree(CL) == before_cl, "CLI import without --apply = dry run")
# --- 桌面端开着 → 排队
(T / "desktop-running").write_text("1")
cli = subprocess.run([sys.executable, "-m", "cc_sessionsync", "codex", "import", "--days", "0", "--apply"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, CC_SESSIONSYNC_NO_WATCHER="1"))
q = [j for j in apps.pending() if j["state"] == "waiting"]
ok("已排队" in cli.stdout and q and q[-1]["op"] == "codex-import" and tree(CL) == before_cl, "desktop running → queued, nothing written")
apps.cancel(q[-1]["id"]); (T / "desktop-running").unlink()
# --- 真导入
r = X.run(days=0, dry=False)
ok(r["done"] == 3 and not r["errors"] and r["ledger"], f"imported 3 ({r.get('errors')})")
mp = json.loads(X.MAP.read_text(encoding="utf-8"))
j1 = Path(mp[t1]["file"])
ok(j1.parent.name == "D--work-shop-app" and j1.exists(), "jsonl written under projects/<slug>")
lines = [json.loads(l) for l in j1.read_text(encoding="utf-8").splitlines()]
ok(all(l["sessionId"] == j1.stem for l in lines), "sessionId = file name")
ent = json.loads((sd / f"{mp[t1]['local']}.json").read_text(encoding="utf-8"))
ok(ent["cliSessionId"] == j1.stem and ent["cwd"] == win_cwd and ent["title"] == "（Codex）修登录页的 bug" and ent["model"] == "claude-opus-5-5" and ent["sessionId"] == mp[t1]["local"], "desktop entry (template keys, cliSessionId)")
ok(tree(CX) == codex_before, "Codex files untouched after import")
led = json.loads((X.LEDGERS / r["ledger"] / "ledger.json").read_text(encoding="utf-8"))
ok(len(led["created"]) == 6 and not led["updated"], "ledger: 3 jsonl + 3 desktop entries created")
ok(not list(CL.rglob("*.tmp")) and not list(sd.glob("*.tmp")), "no temp files left")
# 二次运行
r2 = X.run(days=0, dry=False)
ok(r2["counts"]["same"] == 3 and r2["done"] == 0, "re-run: nothing to do")
# Codex 那边又聊了 → 更新同一份，先备份
time.sleep(0.05); append_turn(f2, "还有一个问题", "好的，已处理。"); os.utime(f2, (time.time() + 5, time.time() + 5))
j2 = Path(mp[t2]["file"]); old2 = j2.read_bytes()
r3 = X.run(days=0, dry=False)
ok(r3["counts"]["update"] == 1 and r3["done"] == 1 and "好的，已处理" in j2.read_text(encoding="utf-8"), "codex thread grew → same Claude session updated")
led3 = json.loads((X.LEDGERS / r3["ledger"] / "ledger.json").read_text(encoding="utf-8"))
ok(len(led3["updated"]) == 2 and (X.LEDGERS / r3["ledger"] / "before" / led3["updated"][0]["before"]).read_bytes() == old2, "previous version backed up in ledger")
# 在 Claude 里接着聊过 → 不覆盖
with open(j1, "a", encoding="utf-8") as fh: fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "在 Claude 里接着聊"}, "sessionId": j1.stem}, ensure_ascii=False) + "\n")
append_turn(f1, "codex 又一句", "codex 回复"); os.utime(f1, (time.time() + 9, time.time() + 9))
r4 = X.run(days=0, dry=False)
ok(next(x for x in r4["rows"] if x["thread"] == t1)["state"] == "diverged" and "在 Claude 里接着聊" in j1.read_text(encoding="utf-8"), "continued in Claude → not overwritten")
# --- 撤回
u3 = X.undo(r3["ledger"])
ok(u3["ok"] and u3["restored"] == 2 and j2.read_bytes() == old2, "undo update → previous version back")
u1 = X.undo(r["ledger"])
ok(u1["ok"] and u1["moved"] == 5 and u1["kept"] == [str(j1)] and j1.exists() and not j2.exists(), f"undo import: moved 5, kept the one continued in Claude {u1}")
ok(not (sd / f"{mp[t2]['local']}.json").exists() and (sd / "local_tmpl.json").exists(), "desktop entries removed, others untouched")
ok(len(list((paths.HOME / "trash").rglob("*"))) >= 5, "undone files went to trash (not deleted)")
ok(not X.undo(r["ledger"])["ok"], "undo twice refused")
ok(not X.undo("../x")["ok"], "bad ledger id refused")
mp2 = json.loads(X.MAP.read_text(encoding="utf-8"))
ok(t2 not in mp2 and t3 not in mp2, "map restored")
ok(tree(CX) == codex_before or set(tree(CX)) == set(codex_before), "Codex dir: only our own fixture appends changed it")
print("FAILS", FAILS)
sys.exit(1 if FAILS else 0)

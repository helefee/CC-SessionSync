# -*- coding: utf-8 -*-
"""造一套假的 Claude / Codex 数据（两个 Claude 账号、几段 Claude Code 会话、几个 Codex 对话、用量），给界面截图和手工测试用。
用法：python tests/demo_env.py <目录> [--serve 8799]   —— 只写 <目录>，不碰真实数据。
--serve：设好环境变量后在那个端口起界面（不开浏览器）。"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from codex_fixture import append_turn, rollout  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
U = lambda: str(uuid.uuid4())


def env_for(T: Path) -> dict:
    H = T / "home"
    return dict(HOME=str(H), USERPROFILE=str(H), XDG_CONFIG_HOME=str(T / "cfg"), APPDATA=str(T / "cfg"), LOCALAPPDATA=str(T / "la"),
                CLAUDE_CONFIG_DIR=str(H / ".claude"), CODEX_HOME=str(H / ".codex"), CC_SESSIONSYNC_HOME=str(T / "ss"),
                CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"),
                CURSOR_USER_DIR=str(T / "cfg" / "Cursor" / "User"), CURSOR_CONFIG_DIR=str(H / ".cursor"), CC_SESSIONSYNC_FAKE_CURSOR_PROCS=str(T / "noprocs.json"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"),
                CC_SESSIONSYNC_FAKE_OPENCODE_PROCS=str(T / "noprocs.json"), CC_SESSIONSYNC_FAKE_ZCODE_PROCS=str(T / "noprocs.json"),
                CC_SESSIONSYNC_NO_WATCHER="1", CC_SESSIONSYNC_KEYSTORE="file", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                CC_SESSIONSYNC_UPDATE_URL=(T / "release.json").as_uri())


def iso(d: datetime) -> str:
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def claude_transcript(f: Path, sid: str, cwd: str, turns: list[tuple[str, str]], when: datetime, model="claude-sonnet-4-5"):
    f.parent.mkdir(parents=True, exist_ok=True)
    L, parent = [], None
    for i, (q, a) in enumerate(turns):
        t = when + timedelta(minutes=3 * i)
        u = U()
        L.append({"type": "user", "uuid": u, "parentUuid": parent, "sessionId": sid, "cwd": cwd, "timestamp": iso(t), "version": "2.1.0",
                  "message": {"role": "user", "content": q}})
        m = U()
        L.append({"type": "assistant", "uuid": m, "parentUuid": u, "sessionId": sid, "cwd": cwd, "timestamp": iso(t + timedelta(seconds=40)),
                  "requestId": "req_" + U()[:12],
                  "message": {"id": "msg_" + U()[:12], "role": "assistant", "model": model, "type": "message",
                              "content": [{"type": "text", "text": a}],
                              "usage": {"input_tokens": 1200 + 37 * i, "output_tokens": 380 + 11 * i,
                                        "cache_creation_input_tokens": 5200 if i == 0 else 600, "cache_read_input_tokens": 18000 + 900 * i}}})
        parent = m
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in L) + "\n", encoding="utf-8")
    t = (when + timedelta(minutes=3 * len(turns))).timestamp()
    os.utime(f, (t, t))


def jwt(claims: dict) -> str:
    b = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b({'alg': 'none'})}.{b(claims)}.sig"


def build(T: Path, extra: bool = False) -> dict:
    """extra=True（截图用）：再加一个商店版目录里的团队号、一个只在命令行用过的会话、账号名缓存。"""
    H = T / "home"
    H.mkdir(parents=True, exist_ok=True)
    (T / "noprocs.json").write_text("[]")
    A1, O1, A2, O2 = U(), U(), U(), U()
    root = T / "cfg/Claude/claude-code-sessions"
    talks = [
        ("重构支付模块的回调", "/work/shop", [("支付回调总是超时，帮我看看 webhook 处理", "先看 handlers/pay.py：同步调用了下游接口，改成丢队列。"),
                                       ("顺便加个重试", "加了指数退避重试，最多 5 次。")]),
        ("给 README 写英文版", "/work/cc-tools", [("把 README 翻成英文", "写好了 README.en.md。")]),
        ("排查 CI 偶发失败", "/work/shop", [("CI 里 test_orders 偶尔挂", "是时区相关：用了本地时间，改成 UTC 后稳定了。"),
                                       ("再跑十遍确认", "跑了 10 遍全过。"), ("提个 PR", "开好了 #42。")]),
        ("数据库迁移脚本", "/work/infra", [("写个把 orders 表拆成两张的迁移", "写好了 0042_split_orders.py，带回滚。")]),
        ("登录页暗色模式", "/work/web", [("登录页加暗色模式", "加了 prefers-color-scheme，按钮和输入框都调了。")]),
        ("周报脚本", "/work/infra", [("每周一自动汇总 PR", "写了 weekly.py，用 gh api 拉数据。")]),
    ]
    sessions = []
    for i, (title, cwd, turns) in enumerate(talks):
        sid = U()
        slug = cwd.replace("/", "-")
        when = NOW - timedelta(days=i * 2 + 1, hours=i)
        claude_transcript(H / ".claude/projects" / slug / f"{sid}.jsonl", sid, cwd, turns, when,
                          model="claude-opus-4-1" if i == 2 else "claude-sonnet-4-5")
        sessions.append((sid, title, cwd, when))
    # 旧号 A1 有 4 个，新号 A2（桌面端当前）有 2 个
    for k, (sid, title, cwd, when) in enumerate(sessions):
        a, o = (A1, O1) if k < 4 else (A2, O2)
        d = root / a / o
        d.mkdir(parents=True, exist_ok=True)
        ms = int(when.timestamp() * 1000)
        (d / f"local_{U()}.json").write_text(json.dumps({"sessionId": f"local_{k}", "cliSessionId": sid, "title": title, "cwd": cwd,
                                                          "createdAt": ms, "lastActivityAt": ms + 600000, "isArchived": False}, ensure_ascii=False), encoding="utf-8")
    # 第三个账号：商店版（MSIX）虚拟化目录里的一个团队号，有 1 个会话；再来一个只在命令行用过的会话（→ 未知账号）
    A3, O3 = U(), U()
    sid3 = U()
    if extra:
        w3 = NOW - timedelta(days=4, hours=5)
        claude_transcript(H / ".claude/projects/-work-mobile" / f"{sid3}.jsonl", sid3, "/work/mobile",
                          [("App 启动白屏", "是 splash 里同步读了配置，改成异步。"), ("加个启动耗时埋点", "加好了，上报到 /metrics。")], w3)
        d3 = T / "la/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude-code-sessions" / A3 / O3
        d3.mkdir(parents=True, exist_ok=True)
        ms3 = int(w3.timestamp() * 1000)
        (d3 / f"local_{U()}.json").write_text(json.dumps({"sessionId": "local_m", "cliSessionId": sid3, "title": "App 启动白屏", "cwd": "/work/mobile",
                                                           "createdAt": ms3, "lastActivityAt": ms3 + 600000, "isArchived": False}, ensure_ascii=False), encoding="utf-8")
        sid4 = U()
        claude_transcript(H / ".claude/projects/-work-scratch" / f"{sid4}.jsonl", sid4, "/work/scratch",
                          [("随手写个正则：匹配手机号", "^1[3-9]\\d{9}$")], NOW - timedelta(hours=7))
        ssd = T / "ss"
        ssd.mkdir(parents=True, exist_ok=True)
        (ssd / "claude-accounts.json").write_text(json.dumps({A1: {"email": "old.account@example.com", "name": "Old Account", "from": "demo"},
                                                              A3: {"email": "team@corp.example", "name": "Team Corp", "from": "demo"}}), encoding="utf-8")
        # 用量图表（截图用）：各会话的子代理记录里放两周的请求，多个模型 / 项目；不会出现在会话列表里
        import random
        rnd = random.Random(7)
        models = [("claude-opus-5-5", 5), ("claude-sonnet-5-5", 3), ("claude-haiku-4-5-20251001", 2)]
        for k, (sid, title, cwd, when) in enumerate(sessions):
            sub = H / ".claude/projects" / cwd.replace("/", "-") / sid / "subagents" / "agent-demo.jsonl"
            sub.parent.mkdir(parents=True, exist_ok=True)
            L = []
            for n in range(60 + 25 * (k % 3)):
                t = NOW - timedelta(days=rnd.uniform(0, 14) ** 1.15 % 14, minutes=rnd.uniform(0, 600))
                m = rnd.choices([x for x, _ in models], [w for _, w in models])[0]
                cw = rnd.randint(0, 9000)
                L.append({"type": "assistant", "uuid": U(), "sessionId": sid, "cwd": cwd, "isSidechain": True, "timestamp": iso(t), "requestId": "req_" + U()[:12],
                          "message": {"id": "msg_" + U()[:12], "model": m, "stop_reason": "end_turn", "usage": {
                              "input_tokens": rnd.randint(5, 900), "output_tokens": rnd.randint(200, 6000), "cache_read_input_tokens": rnd.randint(20000, 220000),
                              "cache_creation_input_tokens": cw, "cache_creation": {"ephemeral_5m_input_tokens": cw // 3, "ephemeral_1h_input_tokens": cw - cw // 3}}}})
            sub.write_text("\n".join(json.dumps(x) for x in L) + "\n", encoding="utf-8")
        # API 模式（3P）：桌面端的 Claude-3p 目录，设备号 + 占位组织；一个能打开的会话、一个对话文件已经没了的
        P3 = T / "la/Claude-3p"
        dev = "d838cbef-0000-4c1c-987b-000000000001"
        (P3 / "configLibrary").mkdir(parents=True, exist_ok=True)
        (P3 / "ant-did").write_text(base64.b64encode(dev.encode()).decode())
        (P3 / "configLibrary/_meta.json").write_text('{"appliedId":"demo"}')
        (P3 / "configLibrary/demo.json").write_text(json.dumps({"inferenceProvider": "gateway", "inferenceGatewayApiKey": "sk-demo-NOT-SHOWN", "inferenceGatewayBaseUrl": "https://gw.example.invalid"}))
        (P3 / "claude_desktop_config.json").write_text(json.dumps({"deploymentMode": "1p"}))
        b3 = P3 / "claude-code-sessions" / dev / "00000000-0000-4000-8000-000000000001"
        b3.mkdir(parents=True, exist_ok=True)
        sid5, w5 = U(), NOW - timedelta(days=2, hours=3)
        claude_transcript(H / ".claude/projects/-work-gateway" / f"{sid5}.jsonl", sid5, "/work/gateway",
                          [("用公司网关跑一下这个脚本", "跑完了：12 个用例全过。")], w5, model="claude-sonnet-4-6")
        for nm, sid, title, w in (("a", sid5, "网关上试跑脚本", w5), ("b", U(), "以前在网关上聊的（文件没了）", NOW - timedelta(days=9))):
            ms5 = int(w.timestamp() * 1000)
            (b3 / f"local_{U()}.json").write_text(json.dumps({"sessionId": "local_3p" + nm, "cliSessionId": sid, "title": title, "cwd": "/work/gateway",
                                                              "model": "claude-sonnet-4-6", "createdAt": ms5, "lastActivityAt": ms5 + 600000, "isArchived": False}, ensure_ascii=False), encoding="utf-8")
    else:
        A3 = None
    ldb = T / "cfg/Claude/Local Storage/leveldb"
    ldb.mkdir(parents=True, exist_ok=True)
    (ldb / "000003.log").write_bytes(b'{"eventName":"e","clientTimestamp":"%s","accountUuid":"%s","organizationUuid":"%s"}'
                                     % (iso(NOW).encode(), A2.encode(), O2.encode()))
    (H / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": A2, "organizationUuid": O2, "emailAddress": "zhang.wei@example.com",
                                                                  "displayName": "Zhang Wei"}}), encoding="utf-8")
    (H / ".claude/settings.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    cx = H / ".codex"
    for j, (t, cwd, days) in enumerate((("修登录页的 bug", "/work/web", 1), ("给订单接口加缓存", "/work/shop", 3),
                                        ("升级 Node 到 22", "/work/cc-tools", 6), ("写部署文档", "/work/infra", 12))):
        f, tid = rollout(cx, cwd, title=t, when=NOW - timedelta(days=days, hours=j))
        if j == 1:
            append_turn(f, "缓存过期时间多少合适", "订单详情 60 秒，列表 10 秒。")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": iso(NOW - timedelta(days=days)), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 42000 + j * 900, "cached_input_tokens": 30000, "output_tokens": 2100 + j * 50, "reasoning_output_tokens": 600, "total_tokens": 44100},
                "last_token_usage": {"input_tokens": 42000, "cached_input_tokens": 30000, "output_tokens": 2100, "reasoning_output_tokens": 600, "total_tokens": 44100}}}}) + "\n")
            if extra:                                    # 截图用：多几次累计值增长，Codex 曲线不至于只有几个点
                tot = [42000 + j * 900, 30000, 2100 + j * 50]
                for e in range(18):
                    tot = [tot[0] + 9000 + 700 * e, tot[1] + 6000 + 500 * e, tot[2] + 400 + 30 * e]
                    tu = {"input_tokens": tot[0], "cached_input_tokens": tot[1], "output_tokens": tot[2], "reasoning_output_tokens": 300, "total_tokens": tot[0] + tot[2]}
                    fh.write(json.dumps({"timestamp": iso(NOW - timedelta(days=(days + e * 0.7) % 13, hours=e)), "type": "event_msg", "payload": {"type": "token_count", "info": {
                        "total_token_usage": tu, "last_token_usage": tu}}}) + "\n")
        t = (NOW - timedelta(days=days, hours=j)).timestamp() + 1800
        os.utime(f, (t, t))
    (cx / "config.toml").write_text('model = "gpt-5.5"\nmodel_provider = "openai"\n', encoding="utf-8")
    (cx / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acct-" + U()[:8],
                                                                                   "id_token": jwt({"email": "li.na@example.com"}),
                                                                                   "access_token": "FAKE-NOT-A-TOKEN"}}), encoding="utf-8")
    (T / "release.json").write_text(json.dumps({"tag_name": "v9.9.9", "name": "v9.9.9", "draft": False, "prerelease": False,
                                                 "html_url": "https://github.com/helefee/CC-SessionSync/releases/tag/v9.9.9",
                                                 "body": "- （演示用的假版本）\n- 修：托盘菜单偶尔不刷新",
                                                 "published_at": iso(NOW - timedelta(hours=3)),
                                                 "assets": [{"name": "CCSessionSync-Setup-9.9.9.exe", "size": 31457280,
                                                             "browser_download_url": "https://example.invalid/CCSessionSync-Setup-9.9.9.exe"},
                                                            {"name": "CCSessionSync-Setup-9.9.9.exe.sha256", "size": 90,
                                                             "browser_download_url": "https://example.invalid/CCSessionSync-Setup-9.9.9.exe.sha256"}]}),
                                   encoding="utf-8")
    try:                                             # DeepSeek Harness（只在演示 / 截图时加：CCSS_DEMO_DSH=1）：两个会话（一个压缩过、带工具和图片），按 dsh 第 4 版格式写
        if not os.environ.get("CCSS_DEMO_DSH"):
            raise ImportError
        import dsh_fixture as FX
        DH = H / ".dsh"
        (DH / "profiles/desktop").mkdir(parents=True, exist_ok=True)
        for j, (sid, cwd, title, days) in enumerate((("session-" + U(), "/work/web", "排查首页白屏", 1), ("session-" + U(), "/work/shop", "订单导出 CSV", 4))):
            fx = FX.rich(sid, cwd, DH) if j == 0 else FX.Sess(sid, cwd)
            fx.header["createdAt"] = int((NOW - timedelta(days=days)).timestamp() * 1000)
            if j:
                fx.t = fx.header["createdAt"]
                fx.add("turn/start", {}); fx.user("把订单导出成 CSV，带表头")
                fx.assistant([{"type": "text", "text": "加了 /orders/export.csv，用流式写出，10 万行 3 秒。"}],
                             usage={"inputTokens": 2100, "outputTokens": 900, "cacheReadTokens": 18000, "cacheWriteTokens": 0, "reasoningTokens": 300, "totalTokens": 21300})
                fx.add("session/title", {"title": title}); fx.add("turn/end", {"reason": {"kind": "completed"}})
            else:
                fx.add("session/title", {"title": title})
            base = fx.header["createdAt"] - fx.ev[0]["time"] if j == 0 else 0
            for e in fx.ev:
                e["time"] += base
            f = fx.write(DH / "sessions")
            t = (NOW - timedelta(days=days)).timestamp() + 3600
            os.utime(f, (t, t))
    except ImportError:
        pass
    if os.environ.get("CCSS_DEMO_CURSOR"):          # Cursor（只在演示 / 截图时加）：编辑器两个对话（一个带子代理）+ 一个 agent 记录
        import cursor_fixture as CF
        CUD, CH = T / "cfg" / "Cursor" / "User", H / ".cursor"
        w1, w2 = CF.ws_storage(CUD, "/work/web"), CF.ws_storage(CUD, "/work/api")
        ms = lambda days: int((NOW - timedelta(days=days)).timestamp() * 1000)
        c1 = CF.rich(CUD, "/work/web", w1, marker="演示：")
        c1.t0 = c1.t = ms(2); c1.bubbles.clear()
        c1.user("首页白屏了，帮我看看", images=1); c1.thinking("先看路由和入口组件")
        c1.tool("read_file_v2", {"targetFile": "src/App.tsx"}, {"contents": "export default function App(){ return <Home user={u}/> }"})
        c1.tool("run_terminal_command_v2", {"command": "npm test"}, {"output": "1 failing: TypeError: u is undefined"}, status="error", error="exit 1")
        c1.tool("edit_file_v2", {"relativeWorkspacePath": "src/App.tsx"}, {"diff": "- <Home user={u}/>\n+ {u && <Home user={u}/>}"})
        c1.say("找到了：App 里 user 没判空。加了判断，npm test 全过。"); c1.write(CUD)
        sub = CF.Composer(name="找出所有用到 user 的组件", cwd="/work/web", wid=w1, t0=ms(2) + 5000, mode="agent", parent=c1.cid)
        sub.user("找出所有用到 user 的组件"); sub.tool("ripgrep_raw_search", {"pattern": "user\\."}, {"matches": 7}); sub.say("7 处，都在 src/pages 下。"); sub.write(CUD)
        c2 = CF.Composer(name="给 /orders 加分页", cwd="/work/api", wid=w2, t0=ms(5), mode="chat", model="gpt-5")
        c2.user("给 /orders 加分页，默认 20 条"); c2.say("用 limit/offset，默认 limit=20、上限 100；返回 total。代码如下：\n```ts\nconst limit = Math.min(+q.limit || 20, 100)\n```"); c2.write(CUD)
        CF.transcript(CH, "work-shop", "a" + U()[1:], [("user", "订单导出 CSV 太慢"), ("assistant", ("tool", "Read", {"path": "export.ts"})), ("assistant", "改成流式写出，10 万行 3 秒。")])
    if os.environ.get("CCSS_DEMO_OC"):              # OpenCode / ZCode（只在演示 / 截图时加）：用测试夹具的真实表结构，换成演示用的文字
        import sqlite3
        import oc_fixture as OF
        ms = lambda days: int((NOW - timedelta(days=days)).timestamp() * 1000)
        ocd = H / ".local" / "share" / "opencode"
        OF.opencode(ocd, "/work/shop", t0=ms(1))
        OF.zcode(H / ".zcode", "/work/web", t0=ms(3))
        R = [("OC1-MARK ", ""), ("OC1-THINK ", ""), (" OC1-TOOL-OUT", ""), ("OC1-ANSWER ", ""), ("OC2-MARK ", ""), ("OC2-THINK", "看下 utils 有哪些函数"),
             (" OC2-TOOL-OUT", ""), ("OC2-ANSWER ", ""), ("ZC-MARK ", ""), ("ZC-THINK", "先找按钮的事件绑定"), (" ZC-TOOL-OUT", ""), ("ZC-ANSWER ", ""),
             ("ZC-REMINDER", "todo"), ("ZC-NOTIFY", "done")]
        for f, tabs in ((ocd / "opencode.db", ("message", "part", "session_message")), (H / ".zcode" / "cli" / "db" / "db.sqlite", ("message", "part"))):
            c = sqlite3.connect(str(f))
            for t in tabs:
                for a, b in R:
                    c.execute(f"update {t} set data = replace(data, ?, ?)", (a, b))
            for a, b in (("OC1 修复构建", "修复 npm run build 报错"), ("OC2 写单测", "给 utils 写单元测试"), ("ZC 修登录", "登录按钮点了没反应"), ("原标题", "整理路由")):
                c.execute("update " + ("session_v2" if "OC2" in a else "session") + " set title = ? where title = ?", (b, a)) if (f.name != "db.sqlite") == a.startswith("OC") else None
            c.commit()
            c.close()
        t = sqlite3.connect(str(H / ".zcode" / "v2" / "tasks-index.sqlite"))
        t.execute("update tasks set title = '登录按钮点了没反应' where title = 'ZC 修登录'")
        t.execute("update tasks set title = '整理路由（我改的名）' where title = '用户改的名字'")
        t.commit()
        t.close()
    return {"A1": A1, "A2": A2, "A3": A3, "sessions": sessions}


if __name__ == "__main__":
    T = Path(sys.argv[1]).resolve()
    info = build(T, extra="--serve" in sys.argv or "--extra" in sys.argv)
    if "--serve" in sys.argv:
        port = sys.argv[sys.argv.index("--serve") + 1]
        env = dict({k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_")) or k in ("CC_SESSIONSYNC_FAKE_INSTALL", "CC_SESSIONSYNC_FAKE_PROCTABLE")}, **env_for(T))
        os.execvpe(sys.executable, [sys.executable, "-m", "cc_sessionsync", "ui", "--port", port, "--no-browser"],
                   dict(env, PYTHONPATH=str(HERE.parent)))
    print(json.dumps({"dir": str(T), "accounts": [info["A1"], info["A2"]]}))

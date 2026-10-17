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
                CC_SESSIONSYNC_FAKE_APP=str(T / "desktop-running"), CC_SESSIONSYNC_FAKE_PROCS=str(T / "noprocs.json"),
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


def build(T: Path) -> dict:
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
        t = (NOW - timedelta(days=days, hours=j)).timestamp() + 1800
        os.utime(f, (t, t))
    (cx / "config.toml").write_text('model = "gpt-5.5"\nmodel_provider = "openai"\n', encoding="utf-8")
    (cx / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acct-" + U()[:8],
                                                                                   "id_token": jwt({"email": "li.na@example.com"}),
                                                                                   "access_token": "FAKE-NOT-A-TOKEN"}}), encoding="utf-8")
    (T / "release.json").write_text(json.dumps({"tag_name": "v0.2.1", "name": "v0.2.1", "draft": False, "prerelease": False,
                                                 "html_url": "https://github.com/helefee/CC-SessionSync/releases/tag/v0.2.1",
                                                 "body": "- 会话管理：批量导出\n- 修：托盘菜单偶尔不刷新",
                                                 "published_at": iso(NOW - timedelta(hours=3)),
                                                 "assets": [{"name": "CCSessionSync-Setup-0.2.1.exe", "size": 31457280,
                                                             "browser_download_url": "https://example.invalid/CCSessionSync-Setup-0.2.1.exe"},
                                                            {"name": "CCSessionSync-Setup-0.2.1.exe.sha256", "size": 90,
                                                             "browser_download_url": "https://example.invalid/CCSessionSync-Setup-0.2.1.exe.sha256"}]}),
                                   encoding="utf-8")
    return {"A1": A1, "A2": A2, "sessions": sessions}


if __name__ == "__main__":
    T = Path(sys.argv[1]).resolve()
    info = build(T)
    if "--serve" in sys.argv:
        port = sys.argv[sys.argv.index("--serve") + 1]
        env = dict({k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_")) or k == "CC_SESSIONSYNC_FAKE_INSTALL"}, **env_for(T))
        os.execvpe(sys.executable, [sys.executable, "-m", "cc_sessionsync", "ui", "--port", port, "--no-browser"],
                   dict(env, PYTHONPATH=str(HERE.parent)))
    print(json.dumps({"dir": str(T), "accounts": [info["A1"], info["A2"]]}))

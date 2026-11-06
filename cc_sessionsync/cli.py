# -*- coding: utf-8 -*-
"""命令行入口：cc-sessionsync <命令>（或 python -m cc_sessionsync <命令>；安装版 CCSessionSync.exe <命令>）。
不带命令 = 打开桌面窗口（desktop；没装 pywebview 时退回浏览器）。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, apps, claude_sync, detect, paths



def _t(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "—"


def cmd_status(a) -> int:
    acc = claude_sync.accounts()
    print(f"CC Session Sync {__version__}   数据目录 {paths.HOME}")
    print(f"Claude Code 数据 {paths.CLAUDE_HOME}    Codex 数据 {paths.CODEX_HOME}")
    ad = claude_sync.app_dirs_brief()
    for x in ad["parts"]:
        print(f"Claude 桌面端 {x['name']}：{x['path']}（{x['why']}）")
    print(claude_sync._acc_line("桌面端当前账号", acc["desktop"]))
    print(claude_sync._acc_line("命令行当前账号", acc["cli"]))
    for f in claude_sync.folders():
        tag = "、".join(n for n, s in (("桌面端", "desktop"), ("命令行", "cli")) if acc[s].get("key") == f["key"])
        print(f"  {f['key']}  会话 {f['count']:>3}  最近 {_t(f['latest'])}{'  ← ' + tag + '当前' if tag else ''}")
    st = apps.status()
    print("Claude 桌面端：" + ("开着" if st.get("claude") else "没开") + "    Codex 桌面端：" + ("开着" if st.get("codex") else "没开"))
    try:
        from . import sessions as S
        ncx = sum(1 for _ in S.codex_sessions())
    except Exception:
        ncx = 0
    print(f"Codex 对话 {ncx} 个（只读列出会话，不再提供导入转换）")
    q = [j for j in apps.pending() if j["state"] == "waiting"]
    if q:
        print(f"排队中 {len(q)} 个（等桌面端退出后做）：" + "；".join(j["title"] for j in q))
    return 0


def cmd_sync(a) -> int:
    argv = []
    if a.dry_run:
        argv.append("--dry-run")
    if a.to:
        argv += ["--to", a.to]
    if a.source:
        argv += ["--source", a.source]
    if a.only:
        argv += ["--only", a.only]
    return claude_sync.main(argv)




def cmd_backups(a) -> int:
    return claude_sync.main(["--backups"])


def cmd_clean_backups(a) -> int:
    """清理 ~/.cc-sessionsync/backups（0.3.9）：不加 --yes 只列出会删哪些。"""
    from . import bakclean
    try:
        p = bakclean.plan(a.days, a.mb)
    except ValueError as e:
        print("✗ " + str(e))
        return 2
    mbs = lambda n: f"{n / 1048576:.1f} MB"
    print(f"{p['dir']}：{p['count']} 份备份，共 {mbs(p['total'])}；每个文件最新的一份不删（{p['protected']} 份）")
    if not p["rule"]:
        print("加 --days N（比 N 天旧的）和 / 或 --mb N（总共超过 N MB 时从最旧的删）")
        return 0
    for x in p["delete"]:
        print(f"  删  {_t(x['at'])}  {mbs(x['size']):>9}  {x['file'] or x['name']}")
    print(f"会删 {p['delete_count']} 份，腾出 {mbs(p['freed'])}")
    if not a.yes or not p["delete_count"]:
        if p["delete_count"]:
            print("（只是预览；确认删除加 --yes）")
        return 0
    r = bakclean.apply(a.days, a.mb)
    print(f"✓ 删了 {r['deleted']} 份，腾出 {mbs(r['freed'])}" + ("；" + "；".join(r["errors"]) if r["errors"] else ""))
    return 0 if r["ok"] else 1


def cmd_restore(a) -> int:
    return claude_sync.main(["--restore", a.backup])


def cmd_verify(a) -> int:
    return claude_sync.main(["--verify", a.backup])


def cmd_auto(a) -> int:
    print(f"自动同步：Claude 桌面端没开时把别的账号的会话补进当前账号（每 {a.interval:g} 秒看一次，Ctrl+C 停）")
    try:
        while True:
            ev = claude_sync.auto_tick()
            if ev:
                print(f"{_t(ev['time'])}  补 {ev['added']} / 刷新 {ev['updated']}  → {ev['account']}  备份 {ev['backup']}", flush=True)
            if a.once:
                st = claude_sync.auto_status()
                print(json.dumps(st, ensure_ascii=False, default=str))
                return 0
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        claude_sync.auto_release()


def cmd_settings(a) -> int:
    print(f"设置文件：{paths.HOME / 'settings.json'}")
    return 0


def cmd_jobs(a) -> int:
    if a.cancel:
        print("已取消" if apps.cancel(a.cancel) else "没有这个排队中的任务")
        return 0
    for j in apps.pending():
        print(f"{j['id']}  {j['state']:<9}  {_t(j.get('queued'))}  {j['title']}  {j.get('result') or ''}")
    return 0


def cmd_diag(a) -> int:
    detect.forget()
    print(detect.text())
    return 0


def cmd_ui(a) -> int:
    from . import ui
    return ui.serve(a.port, open_browser=not a.no_browser, auto_sync=a.auto)


def cmd_selfcheck(a) -> int:
    from . import selfcheck
    r = selfcheck.run(net=not a.offline)
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else selfcheck.text(r))
    return 0 if r["ok"] else 1


def cmd_sessions(a) -> int:
    from . import sessions
    if a.sess_cmd == "list":
        r = sessions.list_all(a.agent)
        for x in r["items"][: a.n]:
            print(f"{_t(x['last'])}  {x['agent']:<6} {x['id'][:8]}  {x['title'][:50]}  ·  {x.get('cwd') or ''}"
                  + (f"  [{x['why']}]" if x.get("why") else ""))
        print(f"\n共 {len(r['items'])} 个")
        return 0
    if a.sess_cmd == "backup":
        r = sessions.backup(full=a.full, why="cli")
        print(f"✓ 看了 {r['files']} 个文件，没变跳过 {r['skipped']}，新快照 {r['snapshots']}，新内容 {r['new_blobs']}（{r['ms']} ms）→ {r['db']}")
        return 0
    if a.sess_cmd == "export":
        r = sessions.export(a.agent, a.id, a.fmt)
        print(("✓ " + r["path"]) if r.get("ok") else ("✗ " + r["error"]))
        return 0 if r.get("ok") else 1
    if a.sess_cmd == "trash":
        for t in sessions.trash_list():
            print(f"{t['id']}  {len(t['items'])} 个" + (f"  （已还原 {t['restored']}）" if t.get("restored") else ""))
        return 0
    if a.sess_cmd == "ops":
        for o in sessions.ops_list():
            print(f"{o['id']}  {o['summary']}" + (f"  （已撤回 {o['undone']}）" if o.get("undone") else ""))
        return 0
    return 2


def cmd_usage(a) -> int:
    from . import usage
    r = usage.scan(full=a.full)
    s = usage.summary({})
    t = s["totals"]
    print(f"扫描：{json.dumps(r, ensure_ascii=False)}")
    print(f"请求 {t['n']}，真实 tokens {t['real']}，缓存命中率 {('%.1f%%' % (t['hit'] * 100)) if t.get('hit') is not None else '—'}，"
          f"按 API 价折算 {('$%.2f' % t['cost']) if t['n'] - t['unpriced'] else '—'}")
    return 0


def cmd_health(a) -> int:
    from . import health
    return health.run(timeout=a.timeout, out=a.out, fail=a.fail)


def cmd_import(a) -> int:
    """0.4.8：导入 .md / .html / .jsonl（文件或文件夹）成新的 Claude Code 会话；缺省只预览，--apply 才写。"""
    import time as _t
    from . import bigimport as BI
    ps = []
    for x in a.paths:
        ps += BI.list_folder(x, a.recursive) if Path(x).is_dir() else [x]
    uniq = BI.scan(ps)["rows"]                                  # 子会话文件归到主会话下面
    out = {"rows": [{k: v for k, v in r.items() if k not in ("preview", "children")} for r in uniq]}
    if a.apply:
        good = [r for r in uniq if "error" not in r]
        t0 = _t.time()
        res = BI.run([r["file"] for r in good], a.cwd, not a.no_desktop, rows={r["file"]: r for r in good})
        out.update(made=[{k: m.get(k) for k in ("id", "file", "title", "subs", "agent_from")} | {"check_ok": (m.get("check") or {}).get("ok")} for m in res["made"]],
                   errors=res["errors"], op=res["op"], secs=round(_t.time() - t0, 2))
    out["peak_mb"] = round(BI.peak_mb(), 1)
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for r in out["rows"]:
            print(f"{'✗' if r.get('error') else '·'} {r['name']}  {r.get('size', 0) / 1048576:.1f} MB  {r.get('agent') or ''}  "
                  f"{r.get('error') or (r.get('title') or '')}{'  （含 %d 个子会话）' % r['subs'] if r.get('subs') else ''}")
        if a.apply:
            print(f"导入了 {len(out['made'])} 个，出错 {len(out['errors'])} 个，用时 {out['secs']} 秒，撤回：cc-sessionsync sessions undo {out['op']}"
                  if out.get("made") is not None else "")
            for e in out["errors"]:
                print("  ✗", e)
        else:
            print("（只是预览；加 --apply 才导入）")
    return 1 if a.apply and out.get("errors") else 0


def cmd_after_quit(a) -> int:
    return apps.watch(a.kind)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cc-sessionsync", description="Claude Code 换号后会话补齐 + Codex 对话导入 Claude Code")
    ap.add_argument("--version", action="version", version=f"cc-sessionsync {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="{desktop,status,sync,backups,clean-backups,restore,verify,auto,codex,sessions,usage,settings,jobs,diag,selfcheck,ui}")
    sub.add_parser("status", help="看当前账号、会话文件夹、Codex 对话数").set_defaults(fn=cmd_status)
    s = sub.add_parser("sync", help="把别的账号下的 Claude 会话补进当前账号的会话列表")
    s.add_argument("--dry-run", action="store_true", help="只演练，不写")
    s.add_argument("--to", help="目标 账号/组织 文件夹（缺省当前账号）")
    s.add_argument("--source", choices=("desktop", "cli", "both"), help="按桌面端还是命令行的当前账号算（缺省桌面端）")
    s.add_argument("--only", help="只补这几个（local_*.json 文件名，逗号分隔）")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("backups", help="同步备份列表").set_defaults(fn=cmd_backups)
    s = sub.add_parser("clean-backups", help="清理 ~/.cc-sessionsync/backups 里旧的配置备份（先预览，加 --yes 才删）")
    s.add_argument("--days", type=float, help="删比 N 天旧的")
    s.add_argument("--mb", type=float, help="总共超过 N MB 时从最旧的删")
    s.add_argument("--yes", action="store_true", help="确认删除（不加只预览）")
    s.set_defaults(fn=cmd_clean_backups)
    r = sub.add_parser("restore", help="撤回某次同步")
    r.add_argument("backup")
    r.set_defaults(fn=cmd_restore)
    r = sub.add_parser("verify", help="核对某次同步（或它的撤回）是不是到位了")
    r.add_argument("backup", help="备份号（backups 里列的）")
    r.set_defaults(fn=cmd_verify)
    au = sub.add_parser("auto", help="前台跑自动同步（桌面端没开时自动补齐）")
    au.add_argument("--interval", type=float, default=30)
    au.add_argument("--once", action="store_true", help="只看一次就退出")
    au.set_defaults(fn=cmd_auto)

    st = sub.add_parser("settings", help="看 / 改设置")
    st.set_defaults(fn=cmd_settings)
    j = sub.add_parser("jobs", help="排队等桌面端退出的任务")
    j.add_argument("--cancel", metavar="ID")
    j.set_defaults(fn=cmd_jobs)
    sub.add_parser("diag", help="打印 Claude / Codex 检测报告").set_defaults(fn=cmd_diag)
    im = sub.add_parser("import", help="导入 .md / .html / .jsonl（文件或文件夹）成新的 Claude Code 会话（缺省只预览；--apply 才写）")
    im.add_argument("paths", nargs="+")
    im.add_argument("--apply", action="store_true")
    im.add_argument("--recursive", action="store_true", help="文件夹里的子文件夹也看")
    im.add_argument("--cwd", default=None, help="放进哪个项目（工作目录）")
    im.add_argument("--no-desktop", action="store_true", help="不登记进 Claude 桌面端的会话列表")
    im.add_argument("--json", action="store_true")
    im.set_defaults(fn=cmd_import)
    ui = sub.add_parser("ui", help="打开本地界面（缺省 http://127.0.0.1:8766）")
    ui.add_argument("--port", type=int, default=8766)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--auto", action="store_true", help="界面开着时顺便自动同步（桌面端没开时补齐）")
    ui.set_defaults(fn=cmd_ui)
    sub.add_parser("desktop", help="打开桌面窗口（缺省；--port / --minimized / --quit，见 desktop --help）")
    sc = sub.add_parser("selfcheck", help="自检：程序文件、数据目录、凭据管理器、Claude / Codex、更新地址")
    sc.add_argument("--offline", action="store_true", help="不查更新地址")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(fn=cmd_selfcheck)
    se = sub.add_parser("sessions", help="会话管理（只读命令；改动请在窗口里做）")
    ss = se.add_subparsers(dest="sess_cmd", required=True)
    x = ss.add_parser("list", help="列出 Claude Code / Codex 会话")
    x.add_argument("--agent", choices=("claude", "codex"))
    x.add_argument("-n", type=int, default=40)
    x = ss.add_parser("backup", help="把会话记录增量备份进 ~/.cc-sessionsync/sessions.sqlite")
    x.add_argument("--full", action="store_true", help="不看修改时间，全部重读")
    x = ss.add_parser("export", help="导出一个会话（md / jsonl）")
    x.add_argument("agent", choices=("claude", "codex"))
    x.add_argument("id")
    x.add_argument("--fmt", choices=("md", "html", "jsonl"), default="md")
    ss.add_parser("trash", help="回收站")
    ss.add_parser("ops", help="会话操作账本")
    se.set_defaults(fn=cmd_sessions)
    us = sub.add_parser("usage", help="扫一遍会话日志，打印用量汇总")
    us.add_argument("--full", action="store_true")
    us.set_defaults(fn=cmd_usage)
    aq = sub.add_parser("after-quit")
    aq.add_argument("kind", choices=("claude", "codex"))
    aq.set_defaults(fn=cmd_after_quit)
    sub.add_parser("key")
    he = sub.add_parser("health")                 # 0.4.2：安装程序更新后跑的启动自检（不开窗口）
    he.add_argument("--timeout", type=float, default=40)
    he.add_argument("--out")
    he.add_argument("--fail", action="store_true")
    he.set_defaults(fn=cmd_health)
    sub._choices_actions = [c for c in sub._choices_actions if c.dest not in ("after-quit", "key", "health")]   # 后台用的，不在帮助里列
    return ap


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("--quit", "--minimized", "--autostart"):     # 安装程序 / 开机自启 / 双击图标
        argv = ["desktop", *argv]
    if argv[0] == "desktop":
        from . import desktop
        return desktop.main(argv[1:])
    if argv[0] == "route":                       # 0.3.5 拿掉了供应商 / 模型路由：旧的开机自启「route serve」到这里只做收尾（还原配置），然后退出
        from . import legacy
        r = legacy.migrate()
        if "--background" not in argv:
            print("供应商（模型路由）功能已在 0.3.5 移除。" + ("已恢复：\n  " + "\n  ".join(r.get("done") or []) if r.get("done") else "没有要恢复的配置。"))
        return 0
    if argv[0] == "key":                         # Claude 的 apiKeyHelper / Codex 的 auth 命令：只把 key 打到 stdout
        from . import keystore
        return keystore.main(argv[1:])
    ap = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a) or 0

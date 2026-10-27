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

from . import __version__, apps, claude_sync, codex_import, detect, paths

LAB = {"new": "新导入", "update": "更新（Codex 那边有新内容）", "same": "导过了、没变", "diverged": "已在 Claude 里接着聊过，不动"}


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
    th = codex_import.threads(days=0)
    print(f"Codex 对话 {len(th)} 个（可导入 Claude Code，见 codex list）")
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


def cmd_codex(a) -> int:
    if a.codex_cmd == "list":
        rows = codex_import.plan(a.days, a.cwd, None, a.all, desktop=False)["rows"]
        for r in rows[: a.n]:
            print(f"{_t(r['mtime'])}  {r['thread'][-6:]}  [{LAB[r['state']]}]  {r['title'] or '（无标题）'}  ·  {r['cwd']}"
                  + ("  （已归档）" if r["archived"] else ""))
        print(f"\n共 {len(rows)} 个" + ("" if len(rows) <= a.n else f"，只列了最近 {a.n} 个（-n 改）"))
        return 0
    if a.codex_cmd == "ledgers":
        for l in codex_import.ledgers():
            print(f"{l['id']}  导入 {l['threads']} 个对话  新建 {l['created']} / 更新 {l['updated']} 个文件"
                  + (f"  （已撤回 {l['undone']}）" if l["undone"] else ""))
        return 0
    if a.codex_cmd == "undo":
        desk = (codex_import._load(codex_import.LEDGERS / a.ledger / "ledger.json", {}) or {}).get("desktop")
        if desk:
            r = apps.run_or_defer("claude", "codex-undo", {"ledger": a.ledger}, f"撤回 Codex 导入 {a.ledger}")
        else:
            r = codex_import.undo(a.ledger)
        if r.get("deferred"):
            print(r["hint"])
            return 0
        if not r.get("ok"):
            print("✗ " + r["error"])
            return 1
        print(f"✓ 撤回了：挪进回收站 {r['moved']} 个新建的文件，换回 {r['restored']} 个原来的版本"
              + (f"；回收站 {r['trash']}" if r.get("trash") else ""))
        for k in r.get("kept") or []:
            print(f"  没动（导过来之后在 Claude 里接着聊过）：{k}")
        ci = r.get("codex_imports")
        if ci:
            print("  Codex 导入记录：" + ({"restored": "换回了导入前的原文件", "removed": "导入前没有这个文件，已挪进回收站",
                                         "entries": "只撤回了我们登记的那几条（文件之后被改过）"}.get(ci.get("how"), ci.get("note") or ci.get("error") or "")))
            if not ci.get("ok"):
                return 1
        return 0
    # import
    only = a.only.split(",") if a.only else None
    mark = a.mark if a.mark is not None else codex_import.mark_setting()
    kw = dict(days=a.days, cwd=a.cwd, only=only, include_all=a.all, desktop=not a.no_desktop, target=a.to,
              reasoning=a.reasoning, mark=mark)
    r = codex_import.run(dry=True, **kw)
    todo = [x for x in r["rows"] if x["state"] in ("new", "update")]
    for x in r["rows"]:
        extra = ""
        if x.get("turns"):
            s = x.get("stats") or {}
            extra = f"  {x['turns']} 轮 / {x['chars'] // 1000} K 字符 / 工具调用 {s.get('tools', 0)}" + \
                    (f" / 图片 {s['images']}" if s.get("images") else "")
        print(f"[{LAB[x['state']]}] {x['title'] or '（无标题）'}  ·  {x['thread'][-6:]}  ·  {x['cwd']}{extra}")
        if x.get("error"):
            print(f"   ✗ {x['error']}")
    c = r["counts"]
    print(f"\n新 {c['new']} / 更新 {c['update']} / 没变 {c['same']} / 不动 {c['diverged']}")
    if r.get("desktop"):
        print(f"桌面端列表：写进 {r['desktop']['key']}（当前账号）")
    elif r.get("desktop_note"):
        print(r["desktop_note"])
    if r.get("mark"):
        mk = r.get("mark_check") or {}
        if mk.get("ok"):
            print(f"登记进 Codex 导入记录：开（{mk['file']}，{'已有，先备份再改' if mk.get('exists') else '还没有，会新建'}）")
        else:
            print(f"✗ 登记进 Codex 导入记录：{mk.get('error')}")
    else:
        print("登记进 Codex 导入记录：关（Codex 桌面端开了「从 Claude Code 导入」的话可能导回去一份重复的；--mark-codex-imported 打开）")
    if not a.apply:
        print("（演练，什么都没写。确认没问题再加 --apply 真正导入）" if todo else "（没有要导入的）")
        return 0
    if r.get("mark") and not (r.get("mark_check") or {}).get("ok"):
        print("没有导入：修好那个文件，或者加 --no-mark-codex-imported 关掉登记。")
        return 1
    if not todo:
        return 0
    if r.get("desktop") and apps.running("claude"):
        j = apps.run_or_defer("claude", "codex-import", kw, f"把 {len(todo)} 个 Codex 对话导进 Claude Code")
        print(j["hint"] + "\n（只想马上在命令行里用：加 --no-desktop 不写桌面端列表，就不用等）")
        return 0
    r = codex_import.run(dry=False, **kw)
    if r.get("ok") is False:
        print("✗ " + r["error"])
        return 1
    if (r.get("marked") or {}).get("ok"):
        print(f"已登记进 Codex 导入记录 {r['marked']['marked']} 条（{r['marked']['file']}，原文件备份在账本里）")
    print(f"\n✓ 导入 {r['done']} 个，账本 {r.get('ledger')}（撤回：cc-sessionsync codex undo {r.get('ledger')}）")
    for x in r["rows"]:
        if x.get("resume"):
            print(f"  {x['title'][:40]}：{x['resume']}")
    for e in r.get("errors") or []:
        print("✗ " + e)
    if r.get("desktop") and r["done"]:
        print("打开（或重启）Claude 桌面端，会话列表里标题前带「（Codex）」的就是。")
    return 0 if not r.get("errors") else 1


DLAB = {"new": "新导入", "update": "更新（那边有新内容）", "same": "导过了、没变", "diverged": "已经接着聊过，不动",
        "changed": "Claude 那边又聊过（加 --again 再导一份新的）", "error": "读不了", "empty": "空会话"}


def cmd_cursor(a) -> int:
    from . import cursor
    if a.cursor_cmd == "detect":
        print(json.dumps(cursor.detect(), ensure_ascii=False, indent=1))
        return 0
    if a.cursor_cmd == "list":
        rows = cursor.sessions()
        rows.sort(key=lambda x: -(x["last"] or 0))
        for x in rows[: a.n]:
            print(f"{_t(x['last'])}  {x['id'][:8]}  {x['msgs'] or 0:>4} 条  [{x['source_name']}]  {x['title'][:50]}  ·  {x.get('cwd') or x.get('project') or ''}")
        d = cursor.detect()
        print(f"\n共 {len(rows)} 个（{d['db']}；{d['cursor_home']}）" + (f"\n注意：{d['error']}" if d.get("error") else ""))
        return 0
    if a.cursor_cmd == "to-claude":
        only = [x for x in (a.only or "").split(",") if x.strip()] or None
        r = cursor.run_to_claude(only, dry=not a.apply, reasoning=a.reasoning, desktop=not a.no_desktop, subagents=a.subagents)
        for x in r["rows"]:
            print(f"  [{DLAB.get(x['state'], x['state'])}]  {x['id']}  {(x.get('title') or '')[:50]}"
                  + (f"  → {x.get('turns')} 轮" if x.get("turns") is not None else "") + (f"  ✗ {x['error']}" if x.get("error") else "")
                  + (f"\n      {x['resume']}" if x.get("resume") else ""))
        print(("演练（加 --apply 才写）" if r.get("dry") else f"✓ 写了 {r.get('done', 0)} 个，账本 {r.get('ledger')}"))
        return 0 if not r.get("errors") else 1
    if a.cursor_cmd == "context":
        r = cursor.run_context(a.agent, a.id, into_project=a.project, dry=not a.apply)
        print(("演练（加 --apply 才写）：" if r["dry"] else "✓ 写好了：") + r["path"] + f"（{r['msgs']} 条消息，{r['text_chars']} 字）")
        print("在 Cursor 里新开一个对话，发：\n  " + r["prompt"])
        return 0
    if a.cursor_cmd == "ledgers":
        for l in cursor.ledgers():
            print(f"{l['id']}  {l['what']}  {l['items']} 个  新建 {l['created']} / 更新 {l['updated']}"
                  + (f"  （已撤回 {l['undone']}）" if l["undone"] else ""))
        return 0
    if a.cursor_cmd == "undo":
        r = cursor.undo(a.ledger)
        if not r.get("ok"):
            print("✗ " + r["error"])
            return 1
        print(f"✓ 挪进回收站 {r['moved']} 个，换回 {r['restored']} 个" + (f"；没动（之后又改过）：{len(r['kept'])} 个" if r["kept"] else ""))
        return 0
    return 2


CLAB = {"new": "要转", "same": "转过了、没变", "changed": "来源又有新内容（加 --again 再转一份新的）", "loop": "本来就是从目标应用来的，不转回去",
        "empty": "空会话", "error": "读不了", "missing": "找不到"}


def cmd_convert(a) -> int:
    from . import convert, ir, ocstore
    if a.conv_cmd == "matrix":
        m = convert.matrix()
        ags = m["agents"]
        print("来源 \\ 目标".ljust(18) + "".join(ir.SHORT[d][:10].ljust(12) for d in ags))
        for s_ in ags:
            print(ir.SHORT[s_][:16].ljust(18) + "".join({"same": "—", "native": "原生", "export": "导出"}[m["cells"][s_][d]].ljust(11) for d in ags))
        for d in ags:
            print(f"  → {ir.LABEL[d]}：{m['how'][d]}" + (f"（{m['targets'][d]['why']}）" if m["targets"][d]["why"] else ""))
        return 0
    if a.conv_cmd == "detect":
        print(json.dumps({k: ocstore.detect(k) for k in ("opencode", "zcode")}, ensure_ascii=False, indent=1))
        return 0
    if a.conv_cmd == "list":
        rows = convert.sources(a.agent)
        rows.sort(key=lambda x: -(x.get("last") or 0))
        for x in rows[: a.n]:
            print(f"{_t(x.get('last'))}  {x['id']}  {x.get('msgs') or 0:>4} 条  {(x.get('title') or '')[:50]}  ·  {x.get('cwd') or ''}")
        print(f"\n共 {len(rows)} 个")
        return 0
    if a.conv_cmd == "run":
        ids = [x.strip() for x in (a.id or "").split(",") if x.strip()]
        if not ids:
            print("✗ 用 --id 指定会话（逗号分隔）")
            return 2
        if a.apply and not a.export:
            k = convert.needs_close(a.to, desktop=not a.no_desktop)
            if k:
                from . import apps
                print(f"✗ {apps.NAMES[k]} 正开着：先完全退出它再做（界面里的「会话转换」可以排队，等它退出后自动做）")
                return 3
        r = convert.run(a.src, ids, a.to, dry=not a.apply, again=a.again, reasoning=a.reasoning, desktop=not a.no_desktop,
                        project=a.project, export=a.export)
        print(f"{ir.LABEL[a.src]} → {ir.LABEL[a.to]}：" + ("原生写入" if r["mode"] == "native" else "导出上下文文件") + (f"（{r['why']}）" if r.get("why") else ""))
        for x in r["rows"]:
            print(f"  [{CLAB.get(x['state'], x['state'])}]  {x['id']}  {(x.get('title') or '')[:50]}"
                  + (f"  ✗ {x['error']}" if x.get("error") else "")
                  + (f"\n      → {x['file']}" if x.get("done") and x.get("file") else "")
                  + (f"\n      {x['resume']}" if x.get("resume") else ""))
        if r.get("paste") and not r.get("dry"):
            print("粘贴提示：" + r["paste"])
        print("演练（加 --apply 才写）" if r.get("dry") else f"✓ 做了 {r.get('done', 0)} 个，账本 {r.get('ledger')}")
        return 0 if not r.get("errors") else 1
    if a.conv_cmd == "ledgers":
        for l in convert.ledgers():
            print(f"{l['name']}  {l['what']}  {l['count']} 个" + ("（导出）" if l["mode"] == "export" else "") + (f"  （已撤回）" if l["undone"] else ""))
        return 0
    if a.conv_cmd == "undo":
        r = convert.undo(a.ledger)
        if r.get("error"):
            print("✗ " + r["error"])
            return 1
        print(f"✓ 删了 {r['removed']} 个新建的" + (f"；没动（之后又改过）：{len(r['kept'])} 个" if r["kept"] else "") + (f"；出错：{r['errors']}" if r["errors"] else ""))
        return 0 if r["ok"] else 1
    return 2


def cmd_dsh(a) -> int:
    from . import dsh
    if a.dsh_cmd == "detect":
        print(json.dumps(dsh.detect(), ensure_ascii=False, indent=1))
        return 0
    if a.dsh_cmd == "list":
        rows = dsh.sessions()
        rows.sort(key=lambda x: -(x["last"] or 0))
        for x in rows[: a.n]:
            print(f"{_t(x['last'])}  {x['id'][8:16]}  {x['msgs'] or 0:>4} 条  {x['title'][:50]}  ·  {x.get('cwd') or ''}"
                  + (f"  [{x['why']}]" if x.get("why") else ""))
        print(f"\n共 {len(rows)} 个（{dsh.home()}）")
        return 0
    if a.dsh_cmd == "validate":
        f = Path(a.path) if a.path and Path(a.path).exists() else dsh.find_file(a.path)
        if not f:
            print("✗ 找不到")
            return 1
        v = dsh.validate(f)
        print(("✓ " if v["ok"] else "✗ ") + f"{f}  事件 {v.get('events')}  帧 {v.get('frames')}（带校验和 {v.get('checksums')}）")
        for e in v["errors"]:
            print("  错：" + e)
        for w in v["warnings"]:
            print("  注意：" + w)
        return 0 if v["ok"] else 1
    if a.dsh_cmd in ("to-claude", "from-claude"):
        only = [x for x in (a.only or "").split(",") if x.strip()] or None
        if a.dsh_cmd == "to-claude":
            r = dsh.run_to_claude(only, dry=not a.apply, reasoning=a.reasoning, images="embed" if a.images else "placeholder",
                                  desktop=not a.no_desktop)
        else:
            r = dsh.run_to_dsh(only, dry=not a.apply, again=a.again)
            if r.get("error"):
                print("✗ " + r["error"])
        for x in r["rows"]:
            print(f"  [{DLAB.get(x['state'], x['state'])}]  {x['id']}  {(x.get('title') or '')[:50]}"
                  + (f"  → {x.get('turns')} 轮" if x.get("turns") is not None else "") + (f"  ✗ {x['error']}" if x.get("error") else "")
                  + (f"\n      {x['resume']}" if x.get("resume") else ""))
        print(("演练（加 --apply 才写）" if r.get("dry") else f"✓ 写了 {r.get('done', 0)} 个，账本 {r.get('ledger')}"))
        return 0 if r.get("ok", True) and not r.get("errors") else 1
    if a.dsh_cmd == "ledgers":
        for l in dsh.ledgers():
            print(f"{l['id']}  {l['what']}  {l['items']} 个  新建 {l['created']} / 更新 {l['updated']}"
                  + (f"  （已撤回 {l['undone']}）" if l["undone"] else ""))
        return 0
    if a.dsh_cmd == "undo":
        r = dsh.undo(a.ledger)
        if not r.get("ok"):
            print("✗ " + r["error"])
            return 1
        print(f"✓ 挪进回收站 {r['moved']} 个，换回 {r['restored']} 个" + (f"；没动（之后又改过 / 正开着）：{len(r['kept'])} 个" if r["kept"] else ""))
        return 0
    return 2


def cmd_backups(a) -> int:
    return claude_sync.main(["--backups"])


def cmd_restore(a) -> int:
    return claude_sync.main(["--restore", a.backup])


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
    if a.codex_mark_imported:
        codex_import.set_mark_setting(a.codex_mark_imported == "on")
    print(f"codex-mark-imported（导入时登记进 Codex 导入记录）：{'开' if codex_import.mark_setting() else '关'}")
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


def cmd_after_quit(a) -> int:
    return apps.watch(a.kind)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cc-sessionsync", description="Claude Code 换号后会话补齐 + Codex 对话导入 Claude Code")
    ap.add_argument("--version", action="version", version=f"cc-sessionsync {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="{desktop,status,sync,backups,restore,auto,codex,sessions,usage,settings,jobs,diag,selfcheck,ui}")
    sub.add_parser("status", help="看当前账号、会话文件夹、Codex 对话数").set_defaults(fn=cmd_status)
    s = sub.add_parser("sync", help="把别的账号下的 Claude 会话补进当前账号的会话列表")
    s.add_argument("--dry-run", action="store_true", help="只演练，不写")
    s.add_argument("--to", help="目标 账号/组织 文件夹（缺省当前账号）")
    s.add_argument("--source", choices=("desktop", "cli", "both"), help="按桌面端还是命令行的当前账号算（缺省桌面端）")
    s.add_argument("--only", help="只补这几个（local_*.json 文件名，逗号分隔）")
    s.set_defaults(fn=cmd_sync)
    sub.add_parser("backups", help="同步备份列表").set_defaults(fn=cmd_backups)
    r = sub.add_parser("restore", help="撤回某次同步")
    r.add_argument("backup")
    r.set_defaults(fn=cmd_restore)
    au = sub.add_parser("auto", help="前台跑自动同步（桌面端没开时自动补齐）")
    au.add_argument("--interval", type=float, default=30)
    au.add_argument("--once", action="store_true", help="只看一次就退出")
    au.set_defaults(fn=cmd_auto)
    c = sub.add_parser("codex", help="把 Codex 对话导进 Claude Code")
    cs = c.add_subparsers(dest="codex_cmd", required=True)
    for name in ("list", "import"):
        x = cs.add_parser(name, help={"list": "列出 Codex 对话", "import": "导入（缺省只演练；加 --apply 才写）"}[name])
        x.add_argument("--days", type=float, default=30, help="只看最近几天动过的（0 = 不限，缺省 30）")
        x.add_argument("--cwd", help="只看这个项目目录下的")
        x.add_argument("--all", action="store_true", help="连子代理 / codex exec / 从 Claude 导进 Codex 的也算上")
        if name == "list":
            x.add_argument("-n", type=int, default=30)
        else:
            x.add_argument("--only", help="只导这几个（Codex 线程号或末 6 位，逗号分隔）")
            x.add_argument("--apply", action="store_true", help="真正写文件（不加就是演练）")
            x.add_argument("--no-desktop", action="store_true", help="不写 Claude 桌面端会话列表，只写命令行能 --resume 的记录")
            x.add_argument("--to", help="桌面端列表写进哪个 账号/组织（缺省桌面端当前账号）")
            x.add_argument("--reasoning", action="store_true", help="带上 Codex 的思考摘要（有明文摘要的才有）")
            g = x.add_mutually_exclusive_group()
            g.add_argument("--mark-codex-imported", dest="mark", action="store_const", const=True, default=None,
                           help="把导过去的会话登记进 Codex 的 external_agent_session_imports.json，免得 Codex 桌面端再导回去一份（缺省按设置，设置缺省关）")
            g.add_argument("--no-mark-codex-imported", dest="mark", action="store_const", const=False)
    cl = cs.add_parser("ledgers", help="导入账本列表")
    u = cs.add_parser("undo", help="按账本撤回一次导入")
    u.add_argument("ledger")
    c.set_defaults(fn=cmd_codex)
    d = sub.add_parser("dsh", help="DeepSeek Harness 的会话：列出、校验、和 Claude Code 互相导入")
    ds = d.add_subparsers(dest="dsh_cmd", required=True)
    ds.add_parser("detect", help="dsh 装在哪、数据目录、会话根和压缩方式、开没开")
    x = ds.add_parser("list", help="列出 dsh 会话")
    x.add_argument("-n", type=int, default=40)
    x = ds.add_parser("validate", help="按 dsh 的规则离线校验一个会话（会话号或文件路径）")
    x.add_argument("path")
    for name in ("to-claude", "from-claude"):
        x = ds.add_parser(name, help={"to-claude": "dsh → Claude Code（缺省演练；--apply 才写）",
                                      "from-claude": "Claude Code → dsh，只新建会话（缺省演练；--apply 才写）"}[name])
        x.add_argument("--only", help="只导这几个（会话号或末尾几位，逗号分隔）")
        x.add_argument("--apply", action="store_true", help="真正写（不加就是演练）")
        if name == "to-claude":
            x.add_argument("--reasoning", action="store_true", help="把 dsh 里的思考当文字带上（缺省丢掉）")
            x.add_argument("--images", action="store_true", help="图片内嵌进 Claude 会话（缺省只放占位文字）")
            x.add_argument("--no-desktop", action="store_true", help="不写 Claude 桌面端会话列表")
        else:
            x.add_argument("--again", action="store_true", help="Claude 那边又聊过的，再导一份新的（旧的那份不动）")
    ds.add_parser("ledgers", help="导入账本")
    x = ds.add_parser("undo", help="按账本撤回一次导入")
    x.add_argument("ledger")
    d.set_defaults(fn=cmd_dsh)
    cv = sub.add_parser("convert", help="会话转换：任意应用的会话 → 任意应用（原生写入或导出上下文文件）")
    cvs = cv.add_subparsers(dest="conv_cmd", required=True)
    cvs.add_parser("matrix", help="哪些方向能原生写入、哪些只能导出")
    cvs.add_parser("detect", help="检测 OpenCode / ZCode（数据目录、会话数，只读）")
    x = cvs.add_parser("list", help="列出某个应用的会话")
    x.add_argument("agent", choices=("claude", "codex", "dsh", "cursor", "opencode", "zcode"))
    x.add_argument("-n", type=int, default=50)
    x = cvs.add_parser("run", help="转换（缺省只演练）")
    x.add_argument("--from", dest="src", required=True, choices=("claude", "codex", "dsh", "cursor", "opencode", "zcode"))
    x.add_argument("--to", required=True, choices=("claude", "codex", "dsh", "cursor", "opencode", "zcode"))
    x.add_argument("--id", required=True, help="会话号，逗号分隔")
    x.add_argument("--apply", action="store_true", help="真的写（缺省只演练）")
    x.add_argument("--again", action="store_true", help="来源又有新内容时，再转一份新的")
    x.add_argument("--reasoning", action="store_true", help="思考也当文字带上")
    x.add_argument("--no-desktop", action="store_true", help="→ Claude Code 时不登记进桌面端列表")
    x.add_argument("--export", action="store_true", help="能原生写也只导出上下文文件")
    x.add_argument("--project", action="store_true", help="上下文文件写进会话的工作目录下 .cc-sessionsync/")
    cvs.add_parser("ledgers", help="转换记录")
    x = cvs.add_parser("undo", help="撤回一次转换（只删没动过的）")
    x.add_argument("ledger")
    cv.set_defaults(fn=cmd_convert)

    cu = sub.add_parser("cursor", help="Cursor 的对话：列出、导进 Claude Code、把任意会话导成给 Cursor 的上下文文件")
    cs = cu.add_subparsers(dest="cursor_cmd", required=True)
    cs.add_parser("detect", help="Cursor 装在哪、数据在哪、开没开、有几个对话")
    x = cs.add_parser("list", help="列出 Cursor 的对话（编辑器 / agent 记录 / cursor-agent 命令行）")
    x.add_argument("-n", type=int, default=40)
    x = cs.add_parser("to-claude", help="Cursor → Claude Code（缺省演练；--apply 才写；Cursor 的数据只读）")
    x.add_argument("--only", help="只导这几个（对话号或末尾几位，逗号分隔）")
    x.add_argument("--apply", action="store_true", help="真正写（不加就是演练）")
    x.add_argument("--reasoning", action="store_true", help="把 Cursor 里的思考当文字带上（缺省丢掉）")
    x.add_argument("--subagents", action="store_true", help="子代理的对话也导（缺省跳过）")
    x.add_argument("--no-desktop", action="store_true", help="不写 Claude 桌面端会话列表")
    x = cs.add_parser("context", help="把一个会话写成给 Cursor 用的 Markdown（在 Cursor 里 @ 它接着聊；不写 Cursor 的数据库）")
    x.add_argument("agent", choices=("claude", "codex", "dsh", "cursor"))
    x.add_argument("id")
    x.add_argument("--project", action="store_true", help="写进会话的工作目录下 .cursor/sessionsync/（缺省写到本程序的导出目录）")
    x.add_argument("--apply", action="store_true")
    cs.add_parser("ledgers", help="导入账本")
    x = cs.add_parser("undo", help="按账本撤回一次导入 / 导出")
    x.add_argument("ledger")
    cu.set_defaults(fn=cmd_cursor)
    st = sub.add_parser("settings", help="看 / 改设置")
    st.add_argument("--codex-mark-imported", choices=("on", "off"), help="codex import 缺省要不要登记进 Codex 导入记录")
    st.set_defaults(fn=cmd_settings)
    j = sub.add_parser("jobs", help="排队等桌面端退出的任务")
    j.add_argument("--cancel", metavar="ID")
    j.set_defaults(fn=cmd_jobs)
    sub.add_parser("diag", help="打印 Claude / Codex 检测报告").set_defaults(fn=cmd_diag)
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
    sub._choices_actions = [c for c in sub._choices_actions if c.dest not in ("after-quit", "key")]   # 后台用的，不在帮助里列
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

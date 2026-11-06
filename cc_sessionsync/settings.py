# -*- coding: utf-8 -*-
"""设置：~/.cc-sessionsync/settings.json（和 0.1.x 同一个文件；原子写）。

键（缺省值见 DEFAULTS）：
    auto_sync            切 Claude 账号后自动同步会话（桌面版开着时做；缺省关）
    claude_auto_sync_on_account_switch  检测到 Claude 账号切换（桌面端 / Claude Code 命令行）就把别的号的会话补进新号（缺省开，0.5.3）
    tray_close           关窗口时藏到托盘（Windows）
    start_min            启动时藏到托盘
    notify               系统通知（自动同步 / 排队的活做完 / 导入完成 / 有新版本）
    update_auto          后台检查更新（6 小时最多一次）
    selfcheck_done       第一次启动的自检做过了
    claude_dir / codex_dir / claude_app_dir   手动指定的数据目录（诊断里选）
    dsh_home                                  DeepSeek Harness 的数据目录（缺省 ~/.dsh；环境变量 DSH_HOME 优先）
    ui_font / ui_zoom                         界面字号倍数（0.8–1.5，缺省 1）和界面缩放（0.8–2，缺省 1）（0.2.6）
    cursor_user_dir / cursor_home             Cursor 的 User 目录（state.vscdb 所在）和 ~/.cursor（诊断里选）（0.2.6）
    opencode_dir / zcode_dir / opencode_cli    OpenCode 数据目录、ZCode 主目录（~/.zcode）、opencode 命令行路径（0.2.7）
    antigravity_dir                         Antigravity / Gemini 根目录（缺省 ~/.gemini）（0.5.4）
    backup_clean_auto / backup_clean_days / backup_clean_mb   自动清理 ~/.cc-sessionsync/backups（缺省关；天数 / MB，0 = 不按这个删）（0.3.9）
    lang                                      界面语言：auto（跟随系统，缺省）/ zh / en（0.4.3）
    chat_style / chat_width                   看会话时的气泡样式 app（跟随来源应用，缺省）/ unified；正文最宽多少 px（520–1600，0 = 全宽，缺省 860）（0.5.2）
开机自启不存在这里：看注册表 HKCU\\...\\Run 本身（tray.autostart_get）。
"""
from __future__ import annotations

import json
import os
import threading

from . import paths

FILE = paths.HOME / "settings.json"
DEFAULTS = {"auto_sync": False, "claude_auto_sync_on_account_switch": True, "tray_close": True, "start_min": False, "notify": True, "update_auto": True,
            "selfcheck_done": False,
            "show_full_accounts": False,
            "backup_clean_auto": False, "backup_clean_days": 90, "backup_clean_mb": 0,   # 高级：自动清理旧的配置备份（0.3.9）
            # 会话备份库（删除前仍可能写入；时间线 UI 已移除，自动备份缺省关）
            "session_backup_auto": False, "session_backup_every": 10, "session_backup_unit": "min",
            "retention_all_days": 7, "retention_daily_days": 90, "ui_font": 1, "ui_zoom": 1,
            "lang": "auto",                         # 界面语言（0.4.3）
            "chat_style": "app", "chat_width": 860}  # 对话气泡样式 / 文本宽度（0.5.2）
BOOL_KEYS = ("auto_sync", "claude_auto_sync_on_account_switch", "tray_close", "start_min", "notify", "update_auto", "selfcheck_done",
             "tray_hint_shown", "show_full_accounts", "session_backup_auto", "backup_clean_auto")
_lock = threading.Lock()


def load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    return dict(DEFAULTS, **d)


def get(k: str, default=None):
    v = load().get(k)
    return default if v is None else v


def update(**kw) -> dict:
    """改几个键（值为 None 删掉），原子写。返回改完的全部设置。"""
    with _lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
            d = d if isinstance(d, dict) else {}
        except Exception:
            d = {}
        for k, v in kw.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = bool(v) if k in BOOL_KEYS else v
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE.with_name(f"settings.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, FILE)
    return dict(DEFAULTS, **d)

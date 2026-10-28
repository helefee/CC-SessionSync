# -*- coding: utf-8 -*-
"""设置：~/.cc-sessionsync/settings.json（和 0.1.x 同一个文件；原子写）。

键（缺省值见 DEFAULTS）：
    auto_sync            切 Claude 账号后自动同步会话（桌面版开着时做；缺省关）
    tray_close           关窗口时藏到托盘（Windows）
    start_min            启动时藏到托盘
    notify               系统通知（自动同步 / 排队的活做完 / 导入完成 / 有新版本）
    update_auto          后台检查更新（6 小时最多一次）
    codex_mark_imported  导入 Codex 对话时登记进 Codex 的导入记录（0.1.1）
    selfcheck_done       第一次启动的自检做过了
    claude_dir / codex_dir / claude_app_dir   手动指定的数据目录（诊断里选）
    dsh_home                                  DeepSeek Harness 的数据目录（缺省 ~/.dsh；环境变量 DSH_HOME 优先）
    ui_font / ui_zoom                         界面字号倍数（0.8–1.5，缺省 1）和界面缩放（0.8–2，缺省 1）（0.2.6）
    cursor_user_dir / cursor_home             Cursor 的 User 目录（state.vscdb 所在）和 ~/.cursor（诊断里选）（0.2.6）
    opencode_dir / zcode_dir / opencode_cli    OpenCode 数据目录、ZCode 主目录（~/.zcode）、opencode 命令行路径（0.2.7）
开机自启不存在这里：看注册表 HKCU\\...\\Run 本身（tray.autostart_get）。
"""
from __future__ import annotations

import json
import os
import threading

from . import paths

FILE = paths.HOME / "settings.json"
DEFAULTS = {"auto_sync": False, "tray_close": True, "start_min": False, "notify": True, "update_auto": True,
            "codex_mark_imported": False, "selfcheck_done": False,
            "show_full_accounts": False,
            "convert_skip_preview": False,          # 高级：转换前不弹预览直接做（0.3.6）
            # 会话自动备份（0.2.1 起缺省开，每 10 分钟；最少 5 分钟）+ 保留规则
            "session_backup_auto": True, "session_backup_every": 10, "session_backup_unit": "min",
            "retention_all_days": 7, "retention_daily_days": 90, "ui_font": 1, "ui_zoom": 1}
BOOL_KEYS = ("auto_sync", "tray_close", "start_min", "notify", "update_auto", "codex_mark_imported", "selfcheck_done",
             "tray_hint_shown", "show_full_accounts", "session_backup_auto", "convert_skip_preview")
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

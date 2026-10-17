# -*- coding: utf-8 -*-
"""CC Session Sync：
1. Claude Code 切换账号后，把别的账号下的会话补进新账号的会话列表（桌面端 + 命令行），带备份和撤回；
2. 把 Codex 的对话导进 Claude Code（新写一份 Claude Code 会话，原文件不动），带账本和撤回。

最早是 CC Msg（https://github.com/helefee/CCMessage）里的功能，这里单独拿出来。
"""
__version__ = "0.2.1"
PRODUCT = "CC Session Sync"
PRODUCT_VERSION = __version__

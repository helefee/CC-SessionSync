# -*- coding: utf-8 -*-
"""Windows exe 的入口（Nuitka 编译这个文件）。双击 = 打开本地界面；带参数 = 命令行。"""
import sys

from cc_sessionsync.cli import main

if __name__ == "__main__":
    sys.exit(main())

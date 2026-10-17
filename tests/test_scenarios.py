# -*- coding: utf-8 -*-
"""每个场景脚本各开一个进程跑（它们在导入包之前就把 HOME / 数据目录都指到临时目录，互不干扰）。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCEN = sorted((HERE / "scenarios").glob("s_*.py"))


@pytest.mark.parametrize("script", SCEN, ids=[s.stem for s in SCEN])
def test_scenario(script):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "CC_SESSIONSYNC_"))}
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=300)
    print(r.stdout)
    print(r.stderr)
    assert r.returncode == 0, "\n".join(l for l in r.stdout.splitlines() if l.startswith(("FAIL", "FAILS"))) + r.stderr[-3000:]

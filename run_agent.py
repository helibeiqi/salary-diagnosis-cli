#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_agent.py — 项目根目录的统一启动器
================================================================================

只做两件事：
  1. 把**项目根目录**放进 `sys.path`，让 `src.tools.*` 在任意工作目录下都可导入；
  2. 把参数转交 `src/main.py`。

为什么需要它（而不是让用户直接 `python src/main.py`）
--------------------------------------------------------------------------------
* `python src/main.py` 时 Python 只会把 `src/` 放进 sys.path，
  于是 `import src.tools.registry` 失败 —— 这是 Python 新手最常撞的一堵墙，
  也是「用户照 README 敲了命令却报 ModuleNotFoundError」的头号成因。
* dsh 插件 spawn Python 时也用它作入口（`run_agent.py --serve`），
  保证**命令行调试路径与插件运行路径完全一致**。

用法
--------------------------------------------------------------------------------
    python run_agent.py --list
    python run_agent.py --status
    python run_agent.py --pipeline --file data/sample_salary.csv
    python run_agent.py --serve                 # 常驻 stdio worker（dsh 插件用）
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Windows 控制台默认 GBK，中文摘要会直接抛 UnicodeEncodeError。
# 这一段必须在任何 print 之前执行。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
            pass

from src.main import main  # noqa: E402 - 必须在 sys.path 调整之后导入

if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
薪酬诊断工具包。

本包**刻意不在 __init__ 里 import 任何子模块** —— 因为 `import pandas`
冷启动 2-3 秒，而 `registry.list_tools()`（列工具清单）根本不需要 pandas。
让 `import src.tools.registry` 保持轻量，是 worker 启动速度与
「计算模块未交付时仍能列全 11 个工具」两件事的共同前提。
"""

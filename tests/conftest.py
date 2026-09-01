# -*- coding: utf-8 -*-
"""
conftest.py —— 测试期全局隔离
================================================================================
把测试运行产生的报告产物重定向到 ``tests/_out/``，避免污染对外发布的
``report/`` 目录（qa_e2e 的 FIX3 会真实列举 ``report/`` 校验「无历史残留」）。

必须在 report.py 首次被导入之前设置 ``COMP_REPORT_DIR``，因为 report.py 在
模块顶层（import 时）就冻结了这个目录（见 src/tools/report.py:135）。
pytest 总是先导入根 conftest，再导入各测试模块，所以这里设置一定早于
任何 ``from ... import report``。
"""
import os

_OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out")
os.makedirs(_OUT_DIR, exist_ok=True)
os.environ["COMP_REPORT_DIR"] = _OUT_DIR

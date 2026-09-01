# -*- coding: utf-8 -*-
"""
tests.fixtures 包 —— 供 charts.py / report.py 自验与 QA 回归复用的假数据。

对外暴露：
    build_mock_meta(include=None, with_figures=False) -> dict
    build_mock_session(session_id, include, with_figures) -> MockSession
    build_band_table / build_current_state / build_market_benchmark / ...
"""

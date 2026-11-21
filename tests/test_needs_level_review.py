# -*- coding: utf-8 -*-
"""
tests/test_needs_level_review.py — R5：职级推断覆盖率人工复核（2026-11-21）

ROADMAP R5 落地回归：职级推断覆盖率 < 70%（level_infer.REVIEW_THRESHOLD）时，
  1. infer_levels report 带需要复核的标记 needs_level_review=True；
  2. confirm_mapping 返回值与 meta['mapping'] 均透出 needs_level_review；
  3. confirm_mapping warnings 追加「人工复核建议」条目（含覆盖率与复核线）；
  4. 覆盖率 ≥ 70% 时不触发（样例数据 80% 覆盖场景不受影响）。

运行：pytest tests/test_needs_level_review.py -q
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from src.tools.loader import load_salary_data, confirm_mapping  # noqa: E402
from src.tools.level_infer import REVIEW_THRESHOLD, infer_levels  # noqa: E402


def _df_with_coverage(n_known: int, n_unknown: int) -> pd.DataFrame:
    """构造指定覆盖率的职位表：known=能被关键词识别，unknown=词表外。"""
    known = ["工程师"] * n_known
    unknown = ["综合支持岗", "生产协调岗", "质量保障岗", "设备维护岗",
               "培训发展岗", "现场对接岗", "运营支撑岗", "流程陪伴岗"][:n_unknown]
    return pd.DataFrame({"job_title": known + unknown})


# ---------------------------------------------------------------------------
# 1) infer_levels 层：flag 判定
# ---------------------------------------------------------------------------

def test_low_coverage_flags_review():
    df = _df_with_coverage(4, 6)   # 4/10 = 40% < 70%
    out, rep = infer_levels(df)
    assert rep["coverage"] == pytest.approx(0.4)
    assert rep["needs_level_review"] is True
    assert rep["review_threshold"] == REVIEW_THRESHOLD
    assert (out["level"] == "UNKNOWN").sum() == 6


def test_high_coverage_no_review_flag():
    df = _df_with_coverage(10, 0)  # 100% ≥ 70%
    _, rep = infer_levels(df)
    assert rep["coverage"] == pytest.approx(1.0)
    assert rep["needs_level_review"] is False


def test_threshold_boundary_is_exclusive():
    # 恰好 70%（7/10）不算"低于复核线" → 不触发
    df = _df_with_coverage(7, 3)
    _, rep = infer_levels(df)
    assert rep["coverage"] == pytest.approx(0.7)
    assert rep["needs_level_review"] is False


def test_no_title_column_flags_review_when_rows_exist():
    df = pd.DataFrame({"foo": [1, 2]})
    _, rep = infer_levels(df, title_col="job_title")
    assert rep["unresolved"] == 2
    assert rep["needs_level_review"] is True


def test_empty_table_never_flags():
    _, rep = infer_levels(pd.DataFrame({"job_title": []}))
    assert rep["total"] == 0
    assert rep["needs_level_review"] is False


# ---------------------------------------------------------------------------
# 2) confirm_mapping 层：端到端透出 + 警告文案
# ---------------------------------------------------------------------------

def _confirm_with_titles(tmp_path, titles):
    """写临时工资表 → load → 用建议映射 confirm_mapping，返回 (res, sid)。"""
    df = pd.DataFrame({
        "工号": [f"E{i:03d}" for i in range(1, len(titles) + 1)],
        "姓名": [f"员工{i}" for i in range(1, len(titles) + 1)],
        "部门": ["制造部"] * len(titles),
        "岗位": titles,
        "月薪": [10000] * len(titles),
    })
    path = os.path.join(str(tmp_path), "review_probe.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    res = load_salary_data(path)
    assert res.get("ok"), res
    sid = res["session_id"]
    mp = {c: v["suggest"] for c, v in res["suggested_mapping"].items() if v.get("suggest")}
    assert "job_title" in mp.values(), f"岗位列未被映射: {mp}"
    return confirm_mapping(sid, mp), sid


def test_confirm_mapping_surfaces_review_flag_and_warning(tmp_path):
    titles = ["工程师"] * 4 + ["综合支持岗", "生产协调岗",
                                "质量保障岗", "设备维护岗"]     # 50% < 70%
    res, sid = _confirm_with_titles(tmp_path, titles)
    assert res.get("ok"), res
    assert res["needs_level_review"] is True
    review_warns = [w for w in res["warnings"] if "人工复核建议" in w]
    assert review_warns, f"warnings 缺少人工复核建议: {res['warnings']}"
    assert "50%" in review_warns[0] and "70%" in review_warns[0]
    meta = res.get("meta") or {}
    map_meta = meta.get("mapping") or {}
    if map_meta:
        assert map_meta.get("needs_level_review") is True


def test_confirm_mapping_no_flag_when_coverage_high(tmp_path):
    titles = ["工程师", "高级工程师", "生产主管", "人力资源经理",
              "财务总监", "实习生", "行政助理", "资深技术专家"]    # 100%
    res, _ = _confirm_with_titles(tmp_path, titles)
    assert res.get("ok"), res
    assert res["needs_level_review"] is False
    assert not [w for w in res["warnings"] if "人工复核建议" in w]

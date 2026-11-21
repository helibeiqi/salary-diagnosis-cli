# -*- coding: utf-8 -*-
"""
tests/test_jobeval_guide_chart.py — 岗位评估升级回归（2026-11-21）

覆盖三块升级：
  1. B：海氏 Guide Chart 点数法（schemas.compute_hay_total）
     —— 校准约束（最大 < 1600 / 最小落 P1）、单一子维度单调性、
        缺失子维度告警与兜底、职级映射可用。
  2. A②：子维度缺失告警 + 美世权重重归一化
     —— 要素整列缺失 → 重归一化告警、总分仍为正；
        要素内子维度部分缺失 → "部分缺失"告警。
  3. A③：BARS 锚定描述 + 美世诚实标注
     —— 两模型全部子维度有 4 档锚定；mercer label/method 明示
        "线性近似"；get_job_eval_guide 指南工具可用。

运行：pytest tests/test_jobeval_guide_chart.py -q
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tools.errors import InvalidParameter  # noqa: E402
from tools.jobeval import (  # noqa: E402
    _evaluate_from_subscores,
    get_job_eval_guide,
)
from tools.schemas import (  # noqa: E402
    JOB_EVAL_ANCHORS,
    JOB_EVAL_MODELS,
    JOB_SCORE_LEVEL_BANDS,
    compute_hay_total,
    get_job_eval_anchors,
    score_to_level,
)

HAY_SUBFACTORS = [
    "专业知识深度", "管理技能要求", "人际沟通技能",
    "思维环境复杂度", "思维难度挑战",
    "行动自由度", "对结果的影响", "影响范围大小",
]


# ---------------------------------------------------------------------------
# 1) 海氏 Guide Chart：校准约束
# ---------------------------------------------------------------------------

def test_hay_all_max_within_scale_and_lands_m3():
    res = compute_hay_total({sf: 10 for sf in HAY_SUBFACTORS})
    assert res["total"] < 1600, f"全档位最大总分超刻度上界: {res['total']}"
    assert res["total"] >= 1350, f"全档位最大总分应落入 M3 段: {res['total']}"
    assert score_to_level(res["total"]) == "M3"
    assert res["warnings"] == []


def test_hay_all_min_lands_p1():
    res = compute_hay_total({sf: 1 for sf in HAY_SUBFACTORS})
    assert res["total"] > 0
    assert score_to_level(res["total"]) == "P1"


def test_hay_grade_rounding_is_valid():
    # 小数等级取整查表：8.6 → 9 档；越界裁剪：0.4 → 1 档 / 99 → 10 档
    res_86 = compute_hay_total({sf: 8.6 for sf in HAY_SUBFACTORS})
    res_9 = compute_hay_total({sf: 9 for sf in HAY_SUBFACTORS})
    assert res_86["total"] == res_9["total"]
    res_lo = compute_hay_total({sf: 0.4 for sf in HAY_SUBFACTORS})
    res_1 = compute_hay_total({sf: 1 for sf in HAY_SUBFACTORS})
    assert res_lo["total"] == res_1["total"]
    res_hi = compute_hay_total({sf: 99 for sf in HAY_SUBFACTORS})
    res_10 = compute_hay_total({sf: 10 for sf in HAY_SUBFACTORS})
    assert res_hi["total"] == res_10["total"]


def test_hay_monotonic_in_each_subfactor():
    """单一子维度升档（不低于基准档），总分单调不减（Guide Chart 基本性质）。"""
    base = {sf: 5 for sf in HAY_SUBFACTORS}
    base_total = compute_hay_total(base)["total"]
    for sf in HAY_SUBFACTORS:
        for g in range(5, 11):
            scores = dict(base)
            scores[sf] = g
            assert compute_hay_total(scores)["total"] >= base_total - 1e-9, (
                f"子维度 {sf}@{g} 档总分低于 5 档基准，违反单调性"
            )


def test_hay_missing_subfactor_warns_and_keeps_total_positive():
    scores = {sf: 7 for sf in HAY_SUBFACTORS}
    full = compute_hay_total(scores)["total"]
    # KH 子维度缺失 → 按 0 点计（总分下降）+ 告警
    partial = dict(scores)
    del partial["专业知识深度"]
    res = compute_hay_total(partial)
    assert res["total"] < full
    assert any("专业知识深度" in w for w in res["warnings"])
    # PS / ACC 关键子维度缺失 → 中位档/乘数 1.0 兜底 + 告警，总分仍为正
    for gone in ["思维环境复杂度", "行动自由度", "影响范围大小"]:
        p = dict(scores)
        del p[gone]
        r = compute_hay_total(p)
        assert r["total"] > 0
        assert any(gone in w for w in r["warnings"])


def test_hay_structure_formula_nonlinear():
    """总分 = KH + KH×PS% + ACC：KH 与 PS 之间存在乘法交叉项（非线性证据）。"""
    lo = compute_hay_total({sf: 3 for sf in HAY_SUBFACTORS})
    hi = compute_hay_total({sf: 9 for sf in HAY_SUBFACTORS})
    kh_gain = hi["kh_points"] - lo["kh_points"]
    acc_gain = hi["acc_points"] - lo["acc_points"]
    total_gain = hi["total"] - lo["total"]
    # 若无 KH×PS 交叉项，total_gain 恰为 kh_gain + acc_gain；
    # 交叉项 = hi_kh×hi_ps% − lo_kh×lo_ps% > 0 → total_gain 严格大于线性拆分和
    assert total_gain > kh_gain + acc_gain + 1e-6
    assert 0.15 <= lo["ps_pct"] < hi["ps_pct"] <= 0.87


# ---------------------------------------------------------------------------
# 2) A②：子维度缺失告警 + 美世权重重归一化
# ---------------------------------------------------------------------------

def _mercer_df(cols_with_values):
    """构造美世子维度测试表：仅含给定列，值域 1-10。"""
    data = {c: [1, 5, 10] for c in cols_with_values}
    return pd.DataFrame(data)


def test_mercer_missing_factor_renormalizes_with_warning():
    # 只给「影响」要素 → 其余三要素缺失，权重应重归一化到「影响」=1.0
    df = _mercer_df(["影响性质", "贡献度", "组织规模"])
    totals, warnings = _evaluate_from_subscores(df, "mercer")
    assert any("重归一化" in w and "沟通" in w for w in warnings)
    assert totals.notna().all() and (totals > 0).all()
    # 三行 1/5/10 分 → W=1/5/10 → ×160 = 160/800/1600
    assert totals.iloc[0] == pytest.approx(160.0)
    assert totals.iloc[1] == pytest.approx(800.0)
    assert totals.iloc[2] == pytest.approx(1600.0)


def test_mercer_two_factors_renormalize_proportionally():
    # 「影响」+「知识」两要素在位 → 权重 0.35:0.25 → 归一化 0.583:0.417
    df = _mercer_df(["影响性质", "知识要求"])
    totals, warnings = _evaluate_from_subscores(df, "mercer")
    assert any("重归一化" in w for w in warnings)
    # 第一行全 1 分 → W = 1.0（权重和恒为 1）→ 160
    assert totals.iloc[0] == pytest.approx(160.0)
    # 第三行全 10 分 → 1600
    assert totals.iloc[2] == pytest.approx(1600.0)


def test_mercer_partial_subfactor_warns():
    # 「影响」只有 2/3 子维度列 → "部分缺失"告警，仍能出分
    df = _mercer_df(["影响性质", "贡献度", "沟通性质", "沟通范围",
                     "创新要求", "复杂性", "知识要求", "团队角色", "知识应用"])
    totals, warnings = _evaluate_from_subscores(df, "mercer")
    assert any("影响" in w and "部分缺失" in w for w in warnings)
    assert totals.notna().all() and (totals > 0).all()


def test_hay_subfactor_path_returns_totals_in_scale():
    """海氏子维度链路端到端：0-1600 刻度、职级可映射、告警聚合。"""
    data = {sf: [2, 6, 10] for sf in HAY_SUBFACTORS}
    df = pd.DataFrame(data)
    totals, warnings = _evaluate_from_subscores(df, "hay")
    assert totals.notna().all()
    assert (totals > 0).all() and (totals < 1600).all()
    assert totals.iloc[0] < totals.iloc[1] < totals.iloc[2]
    levels = [score_to_level(t) for t in totals]
    rank = {lv: i for i, (lo, hi, lv) in enumerate(JOB_SCORE_LEVEL_BANDS)}
    ranks = [rank[lv] for lv in levels]
    assert ranks[0] <= ranks[1] <= ranks[2]
    assert warnings == []  # 全列在位 → 无告警


# ---------------------------------------------------------------------------
# 3) A③：BARS 锚定 + 美世诚实标注 + 指南工具
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["hay", "mercer"])
def test_anchors_cover_every_subfactor(model):
    anchors = get_job_eval_anchors(model)
    for factor, sfs in JOB_EVAL_MODELS[model]["subfactors"].items():
        for sf in sfs:
            assert sf in anchors, f"{model} 子维度「{sf}」缺少锚定描述"
            assert set(anchors[sf].keys()) == {"1-3", "4-6", "7-8", "9-10"}
            for band, text in anchors[sf].items():
                assert text.strip(), f"{sf} 锚定档 {band} 文案为空"


def test_mercer_honestly_labeled_as_linear_scorecard():
    cfg = JOB_EVAL_MODELS["mercer"]
    assert "线性近似" in cfg["label"]
    assert cfg["method"] == "linear_scorecard"
    assert "线性" in cfg["method_note"] and "不是正式" in cfg["method_note"]


def test_hay_labeled_as_guide_chart_with_disclaimer():
    cfg = JOB_EVAL_MODELS["hay"]
    assert "Guide Chart" in cfg["label"]
    assert cfg["method"] == "guide_chart"
    assert "自建" in cfg["method_note"] and "IP" in cfg["method_note"]


def test_get_job_eval_guide_returns_full_payload():
    for model in ["hay", "mercer"]:
        res = get_job_eval_guide(model=model)
        assert res["ok"]
        assert res["model"] == model
        assert res["anchors"] == JOB_EVAL_ANCHORS[model]
        assert res["subfactors"] == JOB_EVAL_MODELS[model]["subfactors"]
    res_hay = get_job_eval_guide(model="hay")
    assert "hay_tables" in res_hay and "total_formula" in res_hay["hay_tables"]


def test_get_job_eval_guide_rejects_unknown_model():
    res = get_job_eval_guide(model="hay2")
    assert not res["ok"]

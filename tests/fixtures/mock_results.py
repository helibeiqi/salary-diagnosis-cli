# -*- coding: utf-8 -*-
"""
mock_results.py — 供 charts.py / report.py 自验使用的**假分析结果**构造脚本
================================================================================

为什么需要这个文件
--------------------------------------------------------------------------------
1. `session.py`（会话状态管理）由 `engineer-core` 并行开发，`report.py` 不可能等它
   写完才能测。这里手工造出**形状严格对齐 ARCHITECTURE §5 契约**的假 dict，
   让报告/图表可以独立跑通（解耦并行开发）。
2. QA 后续做端到端验证时可以复用同一份假数据做**回归基线**：
   真数据跑出来的报告结构，必须与假数据跑出来的结构一致（章节数、图片引用、字段口径）。
3. 造假数据的原则：**数字要自洽，不能随便填**。
   比如带宽下限/中位值/上限必须满足 `中位值 = (下限+上限)/2`，
   CR 分布必须真的产生约 11% 红圈 / 13% 绿圈（与 mock_data.py 的造数口径一致），
   否则报告的“关键数字”就会前后矛盾，测出来的不是 bug 而是假数据本身的错。

数据规模口径（与 data/sample_salary.csv 对齐）
--------------------------------------------------------------------------------
    员工 150 人 ｜ 职级 9 个（P1-P6 / M1-M3）｜ 年度薪资总额约 3,917 万元
    红圈 17 人（11.3%）｜ 绿圈 20 人（13.3%）｜ 公司整体低于市场 P50 约 3%

用法
--------------------------------------------------------------------------------
    from tests.fixtures.mock_results import build_mock_session, build_mock_meta

    sess = build_mock_session("test-001")     # 一个鸭子类型的假 Session 对象
    meta = sess.meta                          # 直接拿 meta 做报告生成

⚠️ 安全声明：全部为随机合成数据，与任何真实自然人无关。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import numpy as np
import pandas as pd

# 让本文件可独立运行：把 src/ 与项目根加进搜索路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))
sys.path.insert(0, _PROJECT_ROOT)

from tools.schemas import (  # noqa: E402
    JOB_FAMILY_PAY_MIX,
    RED_CIRCLE_CR,
    GREEN_CIRCLE_CR,
    LEVEL_TIER_RULES,
)

# 固定随机种子：保证每次造假数据完全一致，测试才可复现
SEED = 20260830

# 九个职级与「现状中位数」（此处直接用现状中位数充当基准中位值，
# 对应 generate_band 的 mode='现状优化' 路径）
LEVELS: List[str] = ["P1", "P2", "P3", "P4", "P5", "P6", "M1", "M2", "M3"]
BASE_MIDS: Dict[str, float] = {
    "P1": 6500.0, "P2": 7800.0, "P3": 9500.0,
    "P4": 11800.0, "P5": 14500.0, "P6": 18000.0,
    "M1": 22000.0, "M2": 28000.0, "M3": 36000.0,
}
# 各职级人数（合计 150；高职级人少，呈金字塔形）
LEVEL_COUNTS: Dict[str, int] = {
    "P1": 26, "P2": 30, "P3": 28, "P4": 22, "P5": 16,
    "P6": 12, "M1": 8, "M2": 5, "M3": 3,
}

# 调薪预算占薪资总额比例
BUDGET_PCT = 0.05
# 年度薪资总额（与 mock_data.py 实跑结果 3,917 万对齐）
TOTAL_ANNUAL_COST = 39_170_000.0
# 市场分位相对公司中位数的整体关系：公司略低于 P50 约 3%
COMPANY_VS_P50 = -0.03


# =============================================================================
# 一、带宽表（模块 2 的产出）
# =============================================================================

def build_band_table() -> List[Dict[str, Any]]:
    """
    构造 9 个职级的带宽表。

    公式（必须与 generate_band 的实现一致，否则自验测不出真问题）：
        下限 = 中位值 / (1 + 带宽幅度 / 2)
        上限 = 下限 × (1 + 带宽幅度)
    可验证恒等式：中位值 = (下限 + 上限) / 2

    重叠度（WorldatWork 标准口径）：
        重叠区间宽度 / 相邻两级带宽的平均宽度
    """
    rows: List[Dict[str, Any]] = []
    prev: Dict[str, Any] | None = None

    for lv in LEVELS:
        tier, spread = LEVEL_TIER_RULES.get(lv, ("专业/技术", 0.35))
        mid = BASE_MIDS[lv]
        bmin = mid / (1 + spread / 2)
        bmax = bmin * (1 + spread)

        row: Dict[str, Any] = {
            "level": lv,
            "tier": tier,
            "band_min": round(bmin, 0),
            "band_mid": round(mid, 0),
            "band_max": round(bmax, 0),
            "spread": spread,                     # 带宽幅度 = (上限-下限)/下限
            "midpoint_diff": None,                # 中位值级差，下面补
            "overlap_with_next": None,            # 与下一职级的重叠度
            "count": LEVEL_COUNTS[lv],
        }

        if prev is not None:
            # 中位值级差 = 本级中位值 / 上一级中位值 - 1
            row["midpoint_diff"] = round(mid / prev["band_mid"] - 1, 4)
            # 重叠区间 = [max(两个下限), min(两个上限)]，负值表示带宽断裂
            ov_lo = max(prev["band_min"], row["band_min"])
            ov_hi = min(prev["band_max"], row["band_max"])
            if ov_hi > ov_lo:
                avg_width = ((prev["band_max"] - prev["band_min"])
                             + (row["band_max"] - row["band_min"])) / 2
                prev["overlap_with_next"] = round((ov_hi - ov_lo) / avg_width, 4)
            else:
                prev["overlap_with_next"] = 0.0

        rows.append(row)
        prev = row

    # 最高职级没有“下一级”，重叠度置 None（报告中显示为 —）
    rows[-1]["overlap_with_next"] = None
    return rows


# =============================================================================
# 二、现状诊断（模块 1 的产出）
# =============================================================================

def build_cr_values() -> List[float]:
    """
    构造 150 个员工的 CR 值。

    造数口径刻意贴近真实企业：
      - 主体呈正态分布，中心 0.97（略低于中位值，对应“公司整体低于市场”）
      - 右侧拖尾产生约 11% 红圈（CR > 1.20）
      - 左侧拖尾产生约 13% 绿圈（CR < 0.80）
    """
    rng = np.random.default_rng(SEED)
    n = sum(LEVEL_COUNTS.values())          # 150

    # 尾部样本数 = 目标红/绿圈人数 - 主体分布自然溢出到尾部的人数。
    # σ=0.09 时约有 0.6 人自然溢出到 CR>1.20、约 3.4 人自然溢出到 CR<0.80，
    # 因此尾部只需显式注入 16 / 17 人，合计即为目标的 17 / 20 人。
    n_red_tail, n_green_tail = 16, 17
    core = rng.normal(0.97, 0.09, n - n_red_tail - n_green_tail)
    # 红圈尾部（司龄长 / 历史高薪 / 快速晋升）
    red_tail = rng.uniform(1.21, 1.52, n_red_tail)
    # 绿圈尾部（新入职 / 快速晋升尚未调薪）
    green_tail = rng.uniform(0.60, 0.79, n_green_tail)

    cr = np.concatenate([core, red_tail, green_tail])
    rng.shuffle(cr)                          # 打散，避免尾部样本全部集中在末段
    return [round(float(v), 4) for v in np.clip(cr[:n], 0.45, 1.80)]


def build_level_stats(cr_values: List[float]) -> List[Dict[str, Any]]:
    """构造各职级薪酬统计表（人数/极值/分位/均值/CR 中位数）。"""
    rng = np.random.default_rng(SEED + 1)
    rows: List[Dict[str, Any]] = []
    cursor = 0
    for lv in LEVELS:
        cnt = LEVEL_COUNTS[lv]
        sub = cr_values[cursor:cursor + cnt]
        cursor += cnt
        mid = BASE_MIDS[lv]
        # 用 CR 反推月薪：月薪 = CR × 中位值
        salaries = [round(float(c) * mid, 0) for c in sub]
        s = pd.Series(salaries, dtype=float)
        rows.append({
            "level": lv,
            "count": cnt,
            "min": float(s.min()),
            "p25": round(float(s.quantile(0.25)), 0),
            "median": round(float(s.median()), 0),
            "p75": round(float(s.quantile(0.75)), 0),
            "max": float(s.max()),
            "mean": round(float(s.mean()), 0),
            "band_min": round(mid / 1.2, 0),
            "band_mid": mid,
            "band_max": round(mid * 1.2, 0),
            "cr_median": round(float(np.median(sub)) if sub else np.nan, 3),
            "cr_mean": round(float(np.mean(sub)) if sub else np.nan, 3),
        })
    return rows


def build_circles(cr_values: List[float]) -> Dict[str, Any]:
    """
    构造红绿圈统计。

    成本口径：
        红圈年成本溢出 = Σ(个人月薪 - 带宽上限) × 12，只累加超出上限的部分
        （红圈员工并非全部薪酬都是浪费，只有超出带宽的部分才是“溢出”）
        绿圈补涨成本 = Σ(带宽下限 - 个人月薪) × 12，只累加低于下限的部分
        这是把绿圈员工补到带宽下限所需的最小成本
    """
    red = [c for c in cr_values if c > RED_CIRCLE_CR]
    green = [c for c in cr_values if c < GREEN_CIRCLE_CR]
    ok = [c for c in cr_values if GREEN_CIRCLE_CR <= c <= RED_CIRCLE_CR]
    n = len(cr_values)

    # 为简化，用各级中位值近似个人所在职级中位值（假数据，用于验证报告数字链路）
    mid_of = []
    cursor = 0
    for lv in LEVELS:
        mid_of.extend([BASE_MIDS[lv]] * LEVEL_COUNTS[lv])
        cursor += LEVEL_COUNTS[lv]

    pairs = list(zip(cr_values, mid_of))
    red_overflow = sum(max(0.0, c * m - m * 1.2) * 12 for c, m in pairs if c > RED_CIRCLE_CR)
    green_gap = sum(max(0.0, m * 0.8 - c * m) * 12 for c, m in pairs if c < GREEN_CIRCLE_CR)

    return {
        "red": {
            "count": len(red),
            "pct": round(len(red) / n, 4),
            "annual_cost": round(red_overflow, 0),
        },
        "green": {
            "count": len(green),
            "pct": round(len(green) / n, 4),
            "annual_cost": round(green_gap, 0),
        },
        "ok": {
            "count": len(ok),
            "pct": round(len(ok) / n, 4),
            "annual_cost": 0.0,
        },
        "note": "红圈成本=超出带宽上限部分的年化；绿圈成本=补到带宽下限所需年化",
    }


def build_current_state() -> Dict[str, Any]:
    """构造完整的 current_state 结果（对应 analyze_current_state 的返回）。"""
    cr_values = build_cr_values()
    level_stats = build_level_stats(cr_values)
    circles = build_circles(cr_values)

    return {
        "ok": True,
        "summary": {
            "headcount": 150,
            "level_count": len(LEVELS),
            "total_monthly_cost": round(TOTAL_ANNUAL_COST / 12, 0),
            "total_annual_cost": TOTAL_ANNUAL_COST,
            "salary_median": 16200.0,
            "salary_mean": 21756.0,
            "cr_mean": round(float(np.mean(cr_values)), 3),
            "cr_median": round(float(np.median(cr_values)), 3),
            "cr_std": round(float(np.std(cr_values, ddof=1)), 3),
        },
        "by_level": level_stats,
        "circles": circles,
        "cr_values": cr_values,          # 供 cr_distribution_chart 直接出图
        # 数据清洗摘要（coerce_report）—— 对应 loader 的强制类型转换环节
        "coerce_report": {
            "raw_rows": 156,
            "clean_rows": 150,
            "dropped_duplicates": 4,
            "fixed_numeric": 7,
            "dropped_missing_required": 2,
            "notes": [
                "「当前月薪」列发现 5 个非数值（'待定'、'—'），已置为缺失并剔除",
                "「绩效等级」列存在大小写不统一（a/b/c），已统一为大写 A/B/C/D",
                "「员工ID」发现 4 条完全重复行，已按首次出现保留",
                "「年度总现金」缺失率 42%，已用 月薪×12 推算填充",
            ],
        },
        # 各标准字段的缺失率（0-1）
        "field_missing_rate": {
            "emp_id": 0.0,
            "name": 0.0,
            "dept": 0.0,
            "level": 0.0,
            "job_title": 0.0,
            "job_family": 0.0,
            "job_score": 0.3467,
            "monthly_salary": 0.0,
            "annual_total_cash": 0.42,
            "tenure_years": 0.0,
            "perf_grade": 0.0133,
            "pay_mix": 0.2867,
            "band_min": 1.0,
            "band_mid": 1.0,
            "band_max": 1.0,
            "mkt_p25": 0.0,
            "mkt_p50": 0.0,
            "mkt_p75": 0.0,
        },
        "warnings": [
            "数据表中缺少带宽三列（下限/中位值/上限），已按职级分层默认幅度生成建议带宽后再诊断",
            "「年度总现金」缺失率 42%，涉及成本口径的估算存在偏差",
        ],
    }


# =============================================================================
# 三、市场对标（模块 3 的产出）
# =============================================================================

def build_market_benchmark() -> Dict[str, Any]:
    """构造市场对标结果：公司中位数 vs 市场 P25/P50/P75 及差距。"""
    rows: List[Dict[str, Any]] = []
    total_gap_cost = 0.0
    total_company = 0.0

    for lv in LEVELS:
        company = BASE_MIDS[lv]
        # 公司整体低于 P50 约 3%：公司 = P50 × (1 - 3%)
        p50 = company / (1 + COMPANY_VS_P50)
        p25 = p50 * 0.82
        p75 = p50 * 1.22
        gap_pct = company / p50 - 1
        cnt = LEVEL_COUNTS[lv]
        # 补到 P50 所需的年化成本（负数表示公司已经高于市场，无需补）
        gap_cost = max(0.0, (p50 - company)) * 12 * cnt
        total_gap_cost += gap_cost
        total_company += company * 12 * cnt

        rows.append({
            "level": lv,
            "count": cnt,
            "company_median": round(company, 0),
            "mkt_p25": round(p25, 0),
            "mkt_p50": round(p50, 0),
            "mkt_p75": round(p75, 0),
            "gap_p50_pct": round(gap_pct, 4),
            "gap_p25_pct": round(company / p25 - 1, 4),
            "gap_p75_pct": round(company / p75 - 1, 4),
            "gap_cost_annual": round(gap_cost, 0),
            "position": "低于P50" if gap_pct < -0.02 else ("高于P50" if gap_pct > 0.02 else "持平P50"),
        })

    return {
        "ok": True,
        "has_market_data": True,
        "strategy": "P50",              # 默认跟随市场
        "by_level": rows,
        "summary": {
            "overall_gap_p50_pct": round(sum(
                r["gap_p50_pct"] * r["count"] for r in rows
            ) / sum(r["count"] for r in rows), 4),
            "adjustment_cost_annual": round(total_gap_cost, 0),
            "adjustment_cost_pct": round(total_gap_cost / total_company, 4),
            "levels_below_p50": sum(1 for r in rows if r["gap_p50_pct"] < -0.02),
            "levels_above_p50": sum(1 for r in rows if r["gap_p50_pct"] > 0.02),
            "target_strategy_by_family": {
                "销售": "P75", "技术": "P75", "管理": "P50", "操作": "P25", "职能": "P50",
            },
        },
        "warnings": [
            "P1/P2 已高于市场 P50，继续普涨会进一步放大基层成本",
            "P5/P6/M1 低于市场 P50 超过 3%，这类岗位市场流动性最强，建议优先补涨",
        ],
    }


# =============================================================================
# 四、调薪模拟（模块 4 的产出）
# =============================================================================

def build_increase_sim(cr_values: List[float]) -> Dict[str, Any]:
    """
    构造四种调薪策略的对比结果 + 调薪前后 CR 分布。

    四种策略（对应 PRD 模块 4）：
      A 平均分配    —— 人人涨同样的比例，最省事但解决不了内部公平性
      B 优先补绿圈  —— 把钱集中给低于带宽下限的人，流失风险控制最优
      C 绩效加权    —— 按绩效矩阵分配（A 1.8 / B 1.2 / C 0.5 / D 0）
      D 红圈冻结    —— 红圈不涨（或给一次性补贴），预算全给绿圈与绩效优秀者
    """
    budget = TOTAL_ANNUAL_COST * BUDGET_PCT     # 预算上限：5% 薪资总额
    # 调薪前红绿圈人数直接从 CR 样本算，保证与 current_state 的口径**完全一致**
    n_red = int(sum(1 for c in cr_values if c > RED_CIRCLE_CR))
    n_green = int(sum(1 for c in cr_values if c < GREEN_CIRCLE_CR))

    strategies = [
        {
            "strategy": "A",
            "label": "A 平均分配",
            "desc": "全员按同一比例（5%）上调，操作最简单，但不区分绩效与内部公平性",
            "total_cost": round(budget, 0),
            "cost_pct": BUDGET_PCT,
            "red_before": n_red, "red_after": n_red,
            "green_before": n_green, "green_after": 14,
            "cr_std_after": round(float(np.std(cr_values, ddof=1)) * 0.99, 3),
            "recommended": False,
            "pros": "沟通成本最低，员工感知公平（人人都涨）",
            "cons": "钱撒胡椒面，绿圈人群补涨不足，离职风险未化解",
        },
        {
            "strategy": "B",
            "label": "B 优先补绿圈",
            "desc": "预算优先投向 CR < 0.80 的员工，将其补至带宽下限（CR=0.80）",
            "total_cost": round(budget * 0.889, 0),
            "cost_pct": round(BUDGET_PCT * 0.889, 4),
            "red_before": n_red, "red_after": n_red,
            "green_before": n_green, "green_after": 4,
            "cr_std_after": round(float(np.std(cr_values, ddof=1)) * 0.82, 3),
            "recommended": True,
            "pros": "直接压制离职高发区，单位成本留人效率最高",
            "cons": "未涨薪员工可能产生被剥夺感，需配套沟通话术",
        },
        {
            "strategy": "C",
            "label": "C 绩效加权",
            "desc": "按绩效调薪矩阵分配：A 1.8 / B 1.2 / C 0.5 / D 0，拉开差距保留关键人才",
            "total_cost": round(budget * 0.979, 0),
            "cost_pct": round(BUDGET_PCT * 0.979, 4),
            "red_before": n_red, "red_after": 15,
            "green_before": n_green, "green_after": 11,
            "cr_std_after": round(float(np.std(cr_values, ddof=1)) * 0.90, 3),
            "recommended": False,
            "pros": "强化绩效导向，高绩效者获得显著高于平均的涨幅",
            "cons": "严重依赖绩效评级公信力；评级失真时激励效果反向",
        },
        {
            "strategy": "D",
            "label": "D 红圈冻结 + 精准补绿",
            "desc": "红圈员工冻结月薪（改发一次性补贴，不进调薪池），释放预算给绿圈与高绩效者",
            "total_cost": round(budget * 0.758, 0),
            "cost_pct": round(BUDGET_PCT * 0.758, 4),
            "red_before": n_red, "red_after": n_red,
            "green_before": n_green, "green_after": 7,
            "cr_std_after": round(float(np.std(cr_values, ddof=1)) * 0.87, 3),
            "recommended": False,
            "pros": "成本最省，且遏制红圈继续扩大；固定成本不上升",
            "cons": "红圈员工连续冻结易引发核心骨干流失，须一对一沟通",
        },
    ]

    # 调薪后 CR：模拟"B 优先补绿圈"的效果 —— 绿圈被抬到 0.80 附近，其余基本不变
    after = []
    for c in cr_values:
        if c < GREEN_CIRCLE_CR:
            after.append(round(min(c + (GREEN_CIRCLE_CR - c) * 0.82, 0.95), 4))
        else:
            after.append(round(c, 4))

    return {
        "ok": True,
        "budget_pct": BUDGET_PCT,
        "budget": round(budget, 0),
        "strategies": strategies,
        "recommended": "B 优先补绿圈",
        "recommend_reason": (
            "绿圈（CR<0.80）是离职高发区，单位投入的留人效率最高；"
            "B 方案以 89% 的预算把绿圈从 20 人压到 4 人，"
            "CR 标准差下降 18%，内部公平性改善幅度最大。"
        ),
        "before": {"cr": cr_values},
        "after": {"cr": after},
        "details_path": "assets/increase_details.csv",
        "warnings": [
            "调薪后仍有 17 名红圈员工，建议单独以一次性补贴处理，不占用年度调薪池",
            "D 方案虽成本最省，但红圈连续冻结 2 年以上会显著抬升骨干流失率，慎用于核心岗位",
        ],
    }


# =============================================================================
# 五、固浮比（模块 5 的产出）
# =============================================================================

def build_pay_mix() -> Dict[str, Any]:
    """
    构造固浮比激励曲线。

    设计原则（报告中要写清楚）：
      各序列在**达成率 100% 处的目标总现金一致**（同为 24 万元/年），
      差异只体现在**曲线斜率**（即浮动占比）上。
      这样才是“激励强度不同”而非“目标收入不同” —— 后者会变成变相降薪。
    """
    target_tc = 240_000.0                       # 年度目标总现金（100% 达成时）
    achievements = list(range(0, 151, 10))      # 0% - 150%，步长 10%

    curves: Dict[str, Dict[str, List[float]]] = {}
    by_family: List[Dict[str, Any]] = []

    for fam, (fix_pct, var_pct) in JOB_FAMILY_PAY_MIX.items():
        fixed = target_tc * fix_pct / 100
        variable = target_tc * var_pct / 100
        incomes = [round(fixed + variable * (a / 100.0), 0) for a in achievements]
        curves[fam] = {
            "achievement": achievements,
            "total_income": incomes,
        }
        by_family.append({
            "job_family": fam,
            "target_mix": f"{fix_pct}:{var_pct}",
            "current_mix": f"{min(90, fix_pct + 12)}:{max(10, var_pct - 12)}",  # 现状偏保守
            "fixed_annual": round(fixed, 0),
            "variable_annual": round(variable, 0),
            "target_tc": target_tc,
            "income_at_0": incomes[0],
            "income_at_100": incomes[achievements.index(100)],
            "income_at_150": incomes[-1],
            "leverage": round(variable * 0.5 / fixed, 3),   # 达成率 ±50% 时的收入波动幅度
            "precondition_ok": fam in ("销售",),            # 只有销售满足高浮动三前提
            "note": (
                "业绩可直接归因到个人、结算周期短（月度/季度），适合高浮动"
                if fam == "销售" else
                "产出周期长、强协作，浮动占比不宜超过 30%，否则破坏协作"
                if fam == "技术" else
                "与经营结果绑定，但需保留固定部分承担管理职责"
                if fam == "管理" else
                "产出标准化、个体可控度低，浮动仅用于质量/安全考核"
                if fam == "操作" else
                "产出难量化，浮动主要用于年度绩效兑现"
            ),
        })

    return {
        "ok": True,
        "target_tc": target_tc,
        "achievements": achievements,
        "curves": curves,
        "by_family": by_family,
        "warnings": [
            "技术序列当前浮动占比仅 18%，激励强度偏弱；但不建议一步提到 40%，"
            "研发协作型产出强行高浮动会破坏知识共享",
            "销售序列浮动 60% 已接近上限，继续提高会导致招人困难与短期行为",
        ],
        "principle": (
            "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；"
            "长周期协作型业务强行高浮动会破坏协作。"
        ),
    }


# =============================================================================
# 六、岗位价值评估（模块 6 的产出）
# =============================================================================

def build_job_eval() -> Dict[str, Any]:
    """构造海氏岗位评估结果与建议职级归属。"""
    table = [
        {"job_title": "Java 高级工程师", "score": 612.0, "current_level": "P5", "suggested_level": "P4", "gap": "高配 1 级"},
        {"job_title": "产品经理", "score": 548.0, "current_level": "P3", "suggested_level": "P4", "gap": "低配 1 级"},
        {"job_title": "区域销售经理", "score": 705.0, "current_level": "P5", "suggested_level": "P5", "gap": "匹配"},
        {"job_title": "财务主管", "score": 663.0, "current_level": "P6", "suggested_level": "P5", "gap": "高配 1 级"},
        {"job_title": "产线班组长", "score": 331.0, "current_level": "P3", "suggested_level": "P2", "gap": "高配 1 级"},
        {"job_title": "HRBP", "score": 502.0, "current_level": "P4", "suggested_level": "P4", "gap": "匹配"},
        {"job_title": "测试工程师", "score": 388.0, "current_level": "P2", "suggested_level": "P3", "gap": "低配 1 级"},
    ]
    mismatch = sum(1 for r in table if r["gap"] != "匹配")
    return {
        "ok": True,
        "model": "hay",
        "model_label": "海氏三要素法",
        "table": table,
        "summary": {
            "total_jobs": len(table),
            "mismatch_count": mismatch,
            "mismatch_pct": round(mismatch / len(table), 4),
            "over_graded": sum(1 for r in table if "高配" in r["gap"]),
            "under_graded": sum(1 for r in table if "低配" in r["gap"]),
        },
        "warnings": [
            "高配岗位（3 个）往往是历史承诺或谈判定薪的产物，"
            "建议通过冻结涨薪 + 自然更替逐步回归带宽，不做一次性下调",
        ],
    }


# =============================================================================
# 七、字段映射（模块 1 前置步骤）
# =============================================================================

def build_mapping() -> Dict[str, Any]:
    """构造字段映射确认结果（演示 AI 从不规范表头识别标准字段的能力）。"""
    return {
        "ok": True,
        "source": "data/messy_salary.csv",
        "mapping": {
            "工号": "emp_id",
            "员工姓名": "name",
            "所属部门": "dept",
            "职务级别": "level",
            "岗位": "job_title",
            "职族": "job_family",
            "岗位评估得分": "job_score",
            "基本工资(元/月)": "monthly_salary",
            "年度总现金(元)": "annual_total_cash",
            "司龄(年)": "tenure_years",
            "上年度绩效": "perf_grade",
            "固定浮动比": "pay_mix",
            "带宽下限": "band_min",
            "带宽中位值": "band_mid",
            "带宽上限": "band_max",
            "市场25分位": "mkt_p25",
            "市场中位值": "mkt_p50",
            "市场75分位": "mkt_p75",
        },
        # 每个字段的匹配置信度（0-100），低于 70 的建议人工复核
        "confidence": {
            "工号": 100, "员工姓名": 100, "所属部门": 100, "职务级别": 100,
            "岗位": 80, "职族": 100, "岗位评估得分": 100, "基本工资(元/月)": 80,
            "年度总现金(元)": 100, "司龄(年)": 100, "上年度绩效": 60,
            "固定浮动比": 100, "带宽下限": 100, "带宽中位值": 100, "带宽上限": 100,
            "市场25分位": 100, "市场中位值": 80, "市场75分位": 100,
        },
        # 未映射到标准字段的原始列（报告中要提示用户这些列不参与计算）
        "unmapped": ["入职日期", "备注", "数据状态"],
        "auto_mapped_count": 18,
        "needs_review": ["上年度绩效（置信度 60，别名匹配为'绩效'子串）"],
    }


# =============================================================================
# 八、组装
# =============================================================================

class MockSession:
    """
    鸭子类型的假 Session 对象。

    只提供 report.py 真正会用到的接口（session_id + meta + get()），
    这样即使 engineer-core 的 Session 实现换了，只要接口一致就能直接替换。
    """

    def __init__(self, session_id: str, meta: Dict[str, Any]):
        self.session_id = session_id
        self.meta = meta

    def get(self, key: str, default: Any = None) -> Any:
        """按 key 取结果；未执行过的步骤返回 default。"""
        return self.meta.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MockSession {self.session_id} keys={list(self.meta.keys())}>"


def build_mock_meta(
    include: Any = None,
    with_figures: bool = False,
) -> Dict[str, Any]:
    """
    构造完整的 session.meta 假数据。

    Args:
        include:      需要包含的步骤键列表；None 表示全部包含。
                      传部分键可模拟“前置步骤缺失”的降级场景（自验必测）。
        with_figures: 是否在 meta 里预置 figure 字段。
                      False（默认）用于测试 report.py 的**现场补画**兜底逻辑；
                      True 用于测试“上游已落图”的正常路径。

    Returns:
        meta 字典，键顺序即报告的逻辑顺序
    """
    cr_values = build_cr_values()
    full = {
        "mapping": build_mapping(),
        "current_state": build_current_state(),
        "market_benchmark": build_market_benchmark(),
        "band": {
            "ok": True,
            "mode": "现状优化",
            "params": {
                "base_mid": "现状中位数",
                "spread": "按职级分层默认幅度",
                "midpoint_diff": 0.22,
                "use_market": False,
            },
            "band_table": build_band_table(),
            "overlap_summary": {
                "avg_overlap": 0.291,
                "max_overlap": 0.361,
                "min_overlap": 0.198,
                "healthy_range": (0.20, 0.40),
            },
            "warnings": [
                "P6→M1 的重叠度仅 19.8%，略低于健康区间下限，"
                "意味着晋升到 M1 会带来较大幅度的薪酬跳变，需关注晋升成本",
            ],
        },
        "increase_sim": build_increase_sim(cr_values),
        "pay_mix": build_pay_mix(),
        "job_eval": build_job_eval(),
    }

    if with_figures:
        # 预置空 figure 占位，模拟“上游工具已调用过 save_figure”的情形
        for key, chart_name in [
            ("band", "band_overlap"),
            ("current_state", "cr_distribution"),
            ("market_benchmark", "market_gap"),
            ("increase_sim", "strategy_cost"),
            ("pay_mix", "paymix_curve"),
        ]:
            full[key]["figure"] = {
                "name": chart_name,
                "html_path": None,     # 会在真实调用中被替换
                "png_path": None,
                "div": None,
                "html_rel": None,
                "png_rel": None,
                "png_error": None,
            }

    if include is None:
        return full
    return {k: v for k, v in full.items() if k in list(include)}


def build_mock_session(
    session_id: str = "mock-20260830-001",
    include: Any = None,
    with_figures: bool = False,
) -> MockSession:
    """构造一个可直接喂给 generate_report 的假 Session。"""
    return MockSession(session_id, build_mock_meta(include=include, with_figures=with_figures))


if __name__ == "__main__":  # pragma: no cover
    # 独立运行：打印假数据的自检摘要，便于人工核对数字是否自洽
    meta = build_mock_meta()
    print("=" * 78)
    print("mock_results 自检")
    print("=" * 78)
    print(f"meta 键：{list(meta.keys())}")

    bt = meta["band"]["band_table"]
    print("\n[带宽表自检：中位值 == (下限+上限)/2 ？]")
    bad = 0
    for r in bt:
        calc = (r["band_min"] + r["band_max"]) / 2
        flag = "OK" if abs(calc - r["band_mid"]) < 1.0 else f"!! 差异 {calc - r['band_mid']:.1f}"
        if not flag.startswith("OK"):
            bad += 1
        print(f"  {r['level']}: 下限 {r['band_min']:>8,.0f}  中位 {r['band_mid']:>8,.0f} "
              f" 上限 {r['band_max']:>8,.0f}  幅度 {r['spread']:.0%} "
              f" 级差 {r['midpoint_diff'] if r['midpoint_diff'] is None else format(r['midpoint_diff'], '.1%')}"
              f"  与下一级重叠 {r['overlap_with_next'] if r['overlap_with_next'] is None else format(r['overlap_with_next'], '.1%')}  {flag}")
    print(f"  -> 不通过的职级数：{bad}")

    cs = meta["current_state"]
    print(f"\n[现状自检] 人数 {cs['summary']['headcount']} ｜ "
          f"红圈 {cs['circles']['red']['count']}（{cs['circles']['red']['pct']:.1%}）｜ "
          f"绿圈 {cs['circles']['green']['count']}（{cs['circles']['green']['pct']:.1%}）｜ "
          f"CR 中位数 {cs['summary']['cr_median']}")
    print(f"            CR 样本数 {len(cs['cr_values'])}")

    inc = meta["increase_sim"]
    print(f"\n[调薪自检] 预算 {inc['budget']:,.0f} 元（占薪资总额 {inc['budget_pct']:.1%}）")
    for s in inc["strategies"]:
        print(f"  {s['label']:<16} 成本 {s['total_cost']:>12,.0f}  "
              f"红圈 {s['red_before']}→{s['red_after']}  绿圈 {s['green_before']}→{s['green_after']}"
              f"{'  ★推荐' if s['recommended'] else ''}")

    pm = meta["pay_mix"]
    print(f"\n[固浮比自检] 目标总现金 {pm['target_tc']:,.0f} 元；"
          f"各序列 100% 达成收入应全部等于该值")
    for r in pm["by_family"]:
        print(f"  {r['job_family']:<4} 目标固浮 {r['target_mix']:<6} "
              f"0%:{r['income_at_0']:>10,.0f}  100%:{r['income_at_100']:>10,.0f}  150%:{r['income_at_150']:>10,.0f}")
    print("\n全部自检完成。")

# -*- coding: utf-8 -*-
"""
report_adapter.py —— 报告数据适配层（writer 词汇 → report 词汇）
================================================================================

## 为什么必须有这一层

`report.py` 是整条工具链的最后一公里，它从 `session.meta` 的六个分区里取数渲染
七章报告。问题在于：**分区键**和**分区内部字段名**都是跨模块的隐式契约，而六个
写入模块（band / diagnose / market / increase / paymix / jobeval）由不同轮次实现，
字段名与 `report.py` 当初设想的不一致。

2026-08-30 实测的错位清单（不是推测，是逐个字段比对出来的）：

| report.py 读的                | 模块实际写的                                  | 后果                     |
|-------------------------------|-----------------------------------------------|--------------------------|
| `current_state`               | `diagnose`                                    | 第 1/3 章整章空          |
| `market_benchmark`            | `market`                                      | 第 4.2 节空              |
| `increase_sim`                | `increase`                                    | 第 5 章空                |
| `pay_mix`                     | `paymix`                                      | 第 6 章空                |
| `job_eval`                    | `jobeval`                                     | 第 4.3 节空              |
| `summary.headcount`           | `counts` / `cr_stats`                         | 执行摘要数字全为「—」     |
| `circles.red/green`           | `counts.红圈` / `counts.绿圈`（中文键）        | 红绿圈人数取不到          |
| `band.band_table`             | `midpoints` + `spreads`（两个分开的 dict）     | 带宽表空                  |
| `market.summary.overall_gap_pct` | `overall_gap`                              | 市场差距显示「—」          |
| `increase.strategies`（4 条） | `scenarios`（2 条，且只有 after 计数）         | 策略对比表空              |

**修复策略的选择**：可以改写入方（6 个模块 + 它们的 JSON Schema + 金标准测试全要动），
也可以加一层适配（只动 1 个新文件 + report.py 1 行）。选后者 —— 写入方的字段已经
被各自的单元测试和 PRD 验收标准钉死，为迁就报告去改它们，等于让「最后一公里」
反向定义上游契约，风险与回归面都大得多。

**这一层只做翻译，不做计算创新**：所有数值要么原样搬运，要么用与上游完全相同的
口径（如 `band_min = mid / (1 + spread/2)`）从已有字段推出，绝不引入新口径。
唯一从明细补算的是「各职级薪酬分布」——上游 meta 里根本没有这张表（diagnose 的
`by_level` 实际是「各职级红/绿/合理人数」，不是薪酬分位），而报告第 3 章需要它，
只能从会话 DataFrame 用 pandas 聚合，口径与 AC-19 的 `Σ(月薪×12)` 保持一致。

## 设计约束

1. **幂等**：同一份 meta 调 N 次结果完全相同（不写状态、不落盘）。
2. **不抛异常**：任何一处取不到就退化为 None / 空表，由 report.py 统一显示「—」。
   适配层是宽容的，但它**不掩盖结构性错位** —— 真正的键名漂移由
   `tests/test_meta_contract.py` 在回归测试里拦。
3. **不改原 meta**：返回新 dict，原 meta 保持只读。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# schemas 是单一真理源：分区键、职级排序、红绿圈阈值都从那里取
try:  # pragma: no cover
    from tools.schemas import (  # type: ignore
        GREEN_CIRCLE_CR, META_SECTION_KEYS, RED_CIRCLE_CR,
        level_sort_key, meta_key,
    )
except Exception:  # noqa: BLE001
    try:
        from .schemas import (  # type: ignore
            GREEN_CIRCLE_CR, META_SECTION_KEYS, RED_CIRCLE_CR,
            level_sort_key, meta_key,
        )
    except Exception:  # noqa: BLE001
        GREEN_CIRCLE_CR, RED_CIRCLE_CR = 0.80, 1.20
        META_SECTION_KEYS = {
            "mapping": "", "band": "", "diagnose": "",
            "market": "", "increase": "", "paymix": "", "jobeval": "",
        }

        def meta_key(s: str) -> str:  # type: ignore[misc]
            return s

        def level_sort_key(level: str):  # type: ignore[misc]
            return (0, 0, str(level))


__all__ = ["build_view", "META_SECTION_KEYS"]


# =============================================================================
# 一、通用小工具
# =============================================================================

def _f(v: Any) -> Optional[float]:
    """安全转 float：None / NaN / 空串 / 占位符一律返回 None（对齐 report._num）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return None
    return None if f != f else f


def _d(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _l(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


# ⚠️ 刻意不提供"比率 → 百分数"的换算。
#
# 适配层只负责搬运，**一切比率都以小数形态原样交给报告层**（0.0323 而不是 3.23）。
# 理由：report._pct 本身对两种口径容错（|v| <= 1.5 视作比率），但章节里存在
# `avg_ov > hi`、`ov > 0.45` 这类**裸数值比较**。适配层一旦把小数放大成百分数，
# 显示层看起来正常，判断逻辑却会全盘反向 —— 这种"显示对、结论错"的 bug 比
# 直接显示错误更难发现。统一用小数，让比较和显示共用同一个真值。


def _round2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


# =============================================================================
# 二、会话 DataFrame 补算：各职级薪酬分布
# =============================================================================

# diagnose 往 df 里回写的列名不固定，这里按优先级探测
_CR_COL_CANDIDATES = ("cr", "compa_ratio", "CR", "CompaRatio")
_CIRCLE_COL_CANDIDATES = ("circle", "red_green", "band_status", "judge")


def _load_df(session_id: Optional[str]):
    """
    取会话 DataFrame。取不到（模块未导入 / 会话过期 / parquet 损坏）返回 None，
    由调用方降级 —— 报告不能因为明细读不出来就整体失败。
    """
    if not session_id:
        return None
    try:  # pragma: no cover
        from .session import get_store  # type: ignore
    except Exception:  # noqa: BLE001
        try:
            from src.tools.session import get_store  # type: ignore
        except Exception:  # noqa: BLE401
            return None
    try:
        return get_store().load(session_id).df
    except Exception:  # noqa: BLE001
        return None


def _pick_col(df, candidates) -> Optional[str]:
    cols = set(map(str, df.columns))
    for c in candidates:
        if c in cols:
            return c
    return None


def _level_distribution(df) -> List[Dict[str, Any]]:
    """
    各职级月薪分布表（报告第 3 章用）。

    上游 meta 里没有这张表：`diagnose.by_level` 的真实含义是
    「各职级的红/绿/合理人数」，与薪酬分位无关。所以只能从明细补算。
    """
    if df is None or "level" not in df.columns or "monthly_salary" not in df.columns:
        return []
    sal_col = "monthly_salary"
    cr_col = _pick_col(df, _CR_COL_CANDIDATES)

    rows: List[Dict[str, Any]] = []
    for level, sub in df.groupby("level", sort=False):
        s = sub[sal_col].astype("float64").dropna()
        if s.empty:
            continue
        row: Dict[str, Any] = {
            "level": str(level),
            "count": int(len(s)),
            "min": _round2(float(s.min())),
            "p25": _round2(float(s.quantile(0.25))),
            "median": _round2(float(s.median())),
            "p75": _round2(float(s.quantile(0.75))),
            "max": _round2(float(s.max())),
            "mean": _round2(float(s.mean())),
        }
        if cr_col:
            cr = sub[cr_col].astype("float64").dropna()
            row["cr_median"] = _round2(float(cr.median())) if not cr.empty else None
        rows.append(row)

    # 职级排序交给 schemas.level_sort_key，避免 "P10" 排在 "P2" 前面
    rows.sort(key=lambda r: level_sort_key(r["level"]))
    return rows


def _annual_payroll(df) -> Optional[float]:
    """
    年度薪酬总额 = Σ(月薪 × 12)。

    口径必须与 AC-19 的预算分母**完全一致** —— 报告里的「占薪酬总额 x%」
    要能和调薪模块的预算率对上，两处各算各的就会出现自相矛盾的数字。
    """
    if df is None or "monthly_salary" not in df.columns:
        return None
    s = df["monthly_salary"].astype("float64").dropna()
    return None if s.empty else float(s.sum()) * 12.0


def _salary_median(df) -> Optional[float]:
    """全样本月薪中位数（元/月）。"""
    if df is None or "monthly_salary" not in df.columns:
        return None
    s = df["monthly_salary"].astype("float64").dropna()
    return None if s.empty else float(s.median())


def _level_counts(df) -> Dict[str, int]:
    if df is None or "level" not in df.columns:
        return {}
    return {str(k): int(v) for k, v in df["level"].value_counts().items()}


# =============================================================================
# 三、各分区适配
# =============================================================================

def _view_diagnose(sec: Dict[str, Any], df, payroll: Optional[float]) -> Dict[str, Any]:
    """diagnose → {summary, circles, by_level, cr_stats, cost, warnings}"""
    counts = _d(sec.get("counts"))          # {'合理': 98, '红圈': 26, '绿圈': 26}
    cr = _d(sec.get("cr_stats"))            # mean/std/min/p25/median/p75/max
    cost = _d(sec.get("cost"))              # red_overflow_annual / green_to_min_annual ...
    lv_counts = _level_counts(df)

    total = sum(int(_f(v) or 0) for v in counts.values())
    red_n = int(_f(counts.get("红圈")) or 0)
    green_n = int(_f(counts.get("绿圈")) or 0)
    ok_n = int(_f(counts.get("合理")) or 0)
    unknown_n = int(_f(counts.get("未识别")) or 0)
    if not total or total <= 0:
        total = red_n + green_n + ok_n + unknown_n

    def _grp(n: int, annual_cost: Any) -> Dict[str, Any]:
        return {
            "count": n,
            "pct": (n / total) if total else None,
            "annual_cost": _f(annual_cost),
        }

    dist = _level_distribution(df)

    # 行级 CR 序列：报告 3.2 节 cr_distribution 图的数据源。diagnose 的 meta
    # 只存聚合值（不存行级明细），所以这里从会话 df 的 cr 列现取——
    # 缺列（诊断未跑 / 老会话）时保持缺省，报告按既有逻辑跳过该图。
    cr_values: List[float] = []
    if df is not None:
        cr_col = _pick_col(df, _CR_COL_CANDIDATES)
        if cr_col:
            cr_values = [
                float(x) for x in df[cr_col].astype("float64").dropna()
            ]

    return {
        **sec,  # 原字段全部保留，便于排查与未来扩展
        "summary": {
            "headcount": total or (int(len(df)) if df is not None else None),
            "total_count": total,
            "unknown_count": unknown_n,
            "level_count": len(lv_counts) or len(dist),
            "total_annual_cost": payroll,
            "annual_cost": payroll,
            # 月薪中位数要从明细补算：cr_stats 里的 median 是「CR 的中位数」，
            # 不是薪酬的中位数 —— 两个 median 放在一起最容易看错。
            "salary_median": _round2(_salary_median(df)),
            "cr_mean": _round2(_f(cr.get("mean"))),
            "cr_median": _round2(_f(cr.get("median"))),
            "cr_std": _round2(_f(cr.get("std"))),
        },
        "circles": {
            "red": _grp(red_n, cost.get("red_overflow_annual")),
            "green": _grp(green_n, cost.get("green_to_min_annual")),
            "ok": _grp(ok_n, None),
            "normal": _grp(ok_n, None),
            "unknown": _grp(unknown_n, None),
            "red_cr": sec.get("red_cr", RED_CIRCLE_CR),
            "green_cr": sec.get("green_cr", GREEN_CIRCLE_CR),
        },
        "cr_stats": cr,
        "cr_values": cr_values,  # 行级 CR（report §3.2 图用）；meta 不存明细，故只能在此处取
        "cost": cost,
        # ⚠️ 覆盖原始 by_level（它是红/绿/合理人数），换成报告要的薪酬分布
        "by_level": dist,
        "level_stats": dist,
        "_by_level_circles": _d(sec.get("by_level")),  # 原数据另存，供核对
        "warnings": _l(sec.get("warnings")),
    }


def _view_band(sec: Dict[str, Any], df) -> Dict[str, Any]:
    """band → {mode, params, band_table, overlap_summary, warnings}

    带宽表由 `midpoints` + `spreads` 两个 dict 还原，口径与 generate_band 完全一致：
        下限 = 中位值 ÷ (1 + 幅度/2)
        上限 = 下限 × (1 + 幅度)
    """
    mids = _d(sec.get("midpoints"))
    spreads = _d(sec.get("spreads"))
    lv_counts = _level_counts(df)

    table: List[Dict[str, Any]] = []
    for lv in sorted(mids.keys(), key=level_sort_key):
        mid = _f(mids.get(lv))
        sp = _f(spreads.get(lv))
        if mid is None or sp is None:
            continue
        lo = mid / (1.0 + sp / 2.0)
        hi = lo * (1.0 + sp)
        # 比率一律以**小数**形态交给报告层（0.25 而不是 25）。
        # report._pct 对两種口径都容错，但章节里还有 `avg_ov > hi` 这类
        # **裸数值比较**（判断重叠度是否落在健康区间），那里只认小数 ——
        # 传百分数会把 15% 当成 15 倍去和 0.45 比，判断必然反。
        table.append({
            "level": str(lv),
            "band_min": _round2(lo),
            "band_mid": _round2(mid),
            "band_max": _round2(hi),
            "spread": sp,
            "count": lv_counts.get(str(lv)),
            "midpoint_diff": _f(sec.get("midpoint_diff")),
        })

    # 相邻职级重叠度：(上一级上限 − 下一级下限) ÷ (上一级上限 − 上一级下限)
    overlaps: List[float] = []
    for a, b in zip(table, table[1:]):
        a_hi, a_lo = a.get("band_max"), a.get("band_min")
        b_lo = b.get("band_min")
        if None in (a_hi, a_lo, b_lo) or a_hi == a_lo:
            continue
        ov = (a_hi - b_lo) / (a_hi - a_lo)
        a["overlap_with_next"] = ov
        overlaps.append(ov)

    ov_summary: Dict[str, Any] = {}
    if overlaps:
        ov_summary = {
            "avg_overlap": sum(overlaps) / len(overlaps),
            "max_overlap": max(overlaps),
            "min_overlap": min(overlaps),
            # 必须是「两个小数的序列」：report 里 `avg_ov > hi` 会拿它做裸比较，
            # 给字符串会让整章抛 TypeError（2026-08-30 真实踩到）。
            # 行业经验值：相邻职级重叠 20%~50% 属健康，过低=晋升即大幅涨薪（成本陡增），
            # 过高=职级带宽失去区分度（晋升级差激励不足）。
            "healthy_range": (0.20, 0.50),
        }

    src = sec.get("midpoint_source")
    base_mid_label = {
        "current_median": "当前在职人员薪酬中位数",
        "market": "市场中位值",
    }.get(str(src), str(src) if src else None)

    return {
        **sec,
        "mode": sec.get("mode"),
        "params": {
            "base_mid": base_mid_label,
            "basis": base_mid_label,
            "spread": "按职级分层（基层 25%~28% / 专业·技术 35%~38% / 中高层 45%~60%）",
            "spread_rule": "按职级分层",
            "midpoint_diff": _f(sec.get("midpoint_diff")),
            "use_market": str(src) == "market",
        },
        "band_table": table,
        "bands": table,
        "overlap_summary": ov_summary,
        "overlap": ov_summary,
        "warnings": _l(sec.get("warnings")),
    }


def _strategy_by_family(strategy: Any, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把「分位值策略」统一成 {岗位序列: 分位} 的 dict。"""
    if isinstance(strategy, dict):
        return dict(strategy)
    if strategy is None:
        return {}
    # 标量 = 全公司统一策略，按实际参与对标的序列展开
    return {str(r.get("job_family")): str(strategy)
            for r in rows if r.get("job_family")}


def _view_market(sec: Dict[str, Any]) -> Dict[str, Any]:
    """market → {has_market_data, by_level, summary, warnings}"""
    families = _l(sec.get("by_family"))
    cost = _d(sec.get("cost"))
    # overall_gap 实际是 {'mean': -0.0323, 'median': -0.0423} 这样的 dict。
    # 报告口径取 **mean**（与 PRD AC-15「整体差距 −3.23%」一致）；
    # 万一上游哪天改成标量，这里也能兼容。
    _gap_raw = sec.get("overall_gap")
    gap = (_f(_d(_gap_raw).get("mean")) if isinstance(_gap_raw, dict)
           else _f(_gap_raw))
    gap_median = _f(_d(_gap_raw).get("median")) if isinstance(_gap_raw, dict) else None

    rows: List[Dict[str, Any]] = []
    below = 0
    for r in families:
        if not isinstance(r, dict):
            continue
        g = _f(r.get("median_gap_pct"))
        if g is not None and g < 0:
            below += 1
        rows.append({
            # 报告表格第一列表头是「职级」，这里实际按岗位序列聚合 —— 保留
            # `job_family` 原值，report 侧若取 level 则退化为序列名，语义不丢。
            "level": r.get("job_family"),
            "job_family": r.get("job_family"),
            "count": r.get("n"),
            "company_median": _round2(_f(r.get("company_median"))),
            "mkt_p50": _round2(_f(r.get("market_target_median"))),
            # 上游 by_family 只给目标中位值，P25/P75 未提供 —— 明确置 None，
            # 让 report 显示「—」而不是拿 P50 去冒充分位区间。
            "mkt_p25": None,
            "mkt_p75": None,
            "gap_p50_pct": g,
            "gap_pct": g,
        })

    return {
        **sec,
        "has_market_data": bool(families),
        "by_level": rows,
        "table": rows,
        "summary": {
            "overall_gap_p50_pct": gap,
            "overall_gap_pct": gap,
            "gap_pct": gap,
            "overall_gap_median_pct": gap_median,
            "levels_below_p50": below,
            "below_market_levels": below,
            "levels_above_p50": max(0, len(rows) - below),
            "adjustment_cost_annual": _f(cost.get("annual")),
            "adjustment_cost_pct": _f(cost.get("pct_of_payroll")),
            # report 侧对这个字段做 `for f, p in strat.items()`，必须是 dict。
            # 上游存的可能是「全局统一策略」标量（如 "P50"），也可能是
            # 「按序列差异化」的 dict —— 标量时展开成 {序列: 策略}，
            # 既不丢信息，也不会让整章抛 AttributeError。
            "target_strategy_by_family": _strategy_by_family(sec.get("strategy"), rows),
            "strategy_by_family": _strategy_by_family(sec.get("strategy"), rows),
            "headcount_with_target": cost.get("headcount"),
        },
        "warnings": _l(sec.get("warnings")),
    }


def _view_increase(sec: Dict[str, Any], diag_view: Dict[str, Any],
                   df=None) -> Dict[str, Any]:
    """increase → {budget, budget_pct, strategies[], recommended, before/after}

    ⚠️ 诚实边界：上游 `simulate_increase` 一次只算**一个**策略（调用方指定），
    产出的是该策略的**两个情景**（rebase_band False/True），而不是四策略横向对比表。
    报告第 5 章原本按四策略对比设计，这里不伪造另外三条 —— 只呈现实际执行的
    那一个，并把两个情景的调薪后红绿圈计数并列出来。
    需要四策略对比时，应让模型连续调用四次 simulate_increase（架构上支持），
    而不是在报告层编造未计算的数字。
    """
    scen = _l(sec.get("scenarios"))
    s0 = _d(scen[0]) if scen else {}
    s1 = _d(scen[1]) if len(scen) > 1 else {}
    after0, after1 = _d(s0.get("after")), _d(s1.get("after"))
    circles = _d(diag_view.get("circles"))

    budget = _f(sec.get("budget_amount"))
    total_cost = _f(sec.get("total_cost"))
    strategy = sec.get("strategy")

    label_map = {
        "A": "平均分配", "B": "优先补绿圈",
        "C": "按绩效加权", "D": "冻结红圈（红圈不调）",
    }
    desc_map = {
        "A": "全员等比例调薪，最简单、最易解释，但不区分绩效与带宽位置。",
        "B": "先把绿圈（低于带宽下限）补齐，再分配剩余预算，优先修复留任风险。",
        "C": "按绩效等级加权（A/B/C/D = 1.8 : 1.2 : 0.5 : 0）分配，激励导向最强，"
             "但强依赖绩效评级公信力。",
        "D": "红圈（超出带宽上限）本轮不调，预算全部投向绿圈与合理区间。",
    }

    strategies = [{
        "label": f"{strategy} · {label_map.get(str(strategy), '')}".strip(" ·"),
        "name": strategy,
        "strategy": strategy,
        "desc": desc_map.get(str(strategy), ""),
        "total_cost": total_cost,
        "cost": total_cost,
        "cost_pct": _f(sec.get("usage_rate")),
        "red_before": _d(circles.get("red")).get("count"),
        "green_before": _d(circles.get("green")).get("count"),
        "red_after": after0.get("red"),
        "green_after": after0.get("green"),
        "cr_mean_after": _round2(_f(after0.get("cr_mean"))),
        "cr_std_after": None,   # 上游未产出调薪后 CR 标准差，不猜
        "over_max_after": after0.get("over_max"),
        "pros": "预算守恒、口径可解释，成本与红绿圈变化均可复核。",
        "cons": "调薪后红圈数可能上升（低于中位者补涨更快），需配套沟通话术。",
    }] if strategy else []

    # 调薪前后对比：报告 5.3 的 cr_before_after 图需要**行级 CR 序列**
    # （调薪前 = df.cr，调薪后 = df.new_cr，均由上游工具 write_back 落盘）。
    # 拿不到行级序列时才退回计数 dict——此时图表会降级，但报告不出错。
    # 注意顺序：increase 的 before 依赖 diagnose 的红绿圈计数
    def _cr_series(col: str):
        if df is None or col not in getattr(df, "columns", []):
            return None
        s = df[col].astype("float64").dropna()
        s = s[s > 0]  # 排除无法计算 CR 的占位值
        return [float(x) for x in s] if len(s) else None

    _cr_b = _cr_series("cr")
    _cr_a = _cr_series("new_cr")
    _before_counts = {
        "red": _d(circles.get("red")).get("count"),
        "green": _d(circles.get("green")).get("count"),
    }
    return {
        **sec,
        "budget": budget,
        "budget_amount": budget,
        "budget_pct": _f(sec.get("budget_rate")),
        "strategies": strategies,
        "compare": strategies,
        "recommended": strategy,
        "recommended_strategy": strategy,
        "recommend_reason": (
            f"本轮实际执行策略 {strategy}（{label_map.get(str(strategy), '')}）；"
            "如需横向对比四种策略，请让模型对同一会话连续调用四次 "
            "simulate_increase 后再生成报告 —— 报告层不会替未执行的方案编造数字。"
        ),
        # 调薪前后对比：after 取主情景（rebase_band=False），before 取诊断口径
        "before": _cr_b if _cr_b else _before_counts,
        "cr_before": _cr_b if _cr_b else _before_counts,
        "after": _cr_a if _cr_a else after0,
        "cr_after": _cr_a if _cr_a else after0,
        "scenario_rebase": {
            "rebase_band": s1.get("rebase_band"),
            "red": after1.get("red"),
            "green": after1.get("green"),
            "normal": after1.get("normal"),
        },
        "per_perf_rate": _d(sec.get("per_perf_rate")),
        "warnings": _l(sec.get("warnings")),
    }


def _view_jobeval(sec: Dict[str, Any]) -> Dict[str, Any]:
    """jobeval → {model_label, table, summary, warnings}"""
    return {
        **sec,
        "model_label": sec.get("model_label") or sec.get("model"),
        "table": _l(sec.get("mismatch_examples")),
        "scores": _l(sec.get("mismatch_examples")),
        "summary": {
            "total_jobs": sec.get("n_jobs"),
            "mismatch_count": sec.get("mismatch_count"),
            "mismatch_pct": _f(sec.get("mismatch_pct")),
            "scale_note": sec.get("scale_note"),
        },
        "warnings": _l(sec.get("warnings")),
    }


def _view_mapping(sec: Dict[str, Any]) -> Dict[str, Any]:
    """mapping → {source, mapping, auto_mapped_count, unmapped, needs_review}"""
    mp = _d(sec.get("map")) or _d(sec.get("mapped_fields"))
    return {
        **sec,
        "source": sec.get("source_file"),
        "file_path": sec.get("source_file"),
        "mapping": mp,
        "auto_mapped_count": len(mp),
        "unmapped": _l(sec.get("unmapped_columns")),
        "needs_review": _l(sec.get("missing_required")),
        "confidence": None,   # 上游写入的是最终映射，不保留逐列置信度
        "confidence_note": (
            "逐列匹配置信度仅在 load_salary_data 的建议阶段产出"
            "（meta.suggested_mapping），映射确认后不再保留。"
        ),
    }


# =============================================================================
# 四、入口
# =============================================================================

def build_view(session_id: Optional[str], meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    把真实 meta 翻译成 report.py 的读取词汇。

    返回**新** dict；原 meta 不被修改。任何分区缺失就原样返回空 dict，
    由 report.py 走既有的「本章未执行」降级文案。
    """
    src = _d(meta)
    df = _load_df(session_id)
    payroll = _annual_payroll(df)

    out: Dict[str, Any] = dict(src)

    # 顶层补充字段：报告第 2 章要用，但上游把这些放在 meta 顶层而非分区里
    out.setdefault("shape", src.get("shape"))
    out.setdefault("file_info", src.get("file_info"))
    out.setdefault("coerce_report", src.get("coerce_report"))

    # 注意顺序：increase 的 before 依赖 diagnose 的红绿圈计数
    if src.get(meta_key("diagnose")):
        out[meta_key("diagnose")] = _view_diagnose(
            _d(src.get("diagnose")), df, payroll)
    if src.get(meta_key("band")):
        out[meta_key("band")] = _view_band(_d(src.get("band")), df)
    if src.get(meta_key("market")):
        out[meta_key("market")] = _view_market(_d(src.get("market")))
    if src.get(meta_key("mapping")):
        out[meta_key("mapping")] = _view_mapping(_d(src.get("mapping")))
    if src.get(meta_key("jobeval")):
        out[meta_key("jobeval")] = _view_jobeval(_d(src.get("jobeval")))
    if src.get(meta_key("increase")):
        out[meta_key("increase")] = _view_increase(
            _d(src.get("increase")), _d(out.get("diagnose")), df)

    # paymix 的键与 report 期望天然一致（by_family / curves / principle），
    # 无需适配；此处只补一个空 warnings，保持各分区形状统一。
    if src.get(meta_key("paymix")):
        pm = dict(_d(src.get("paymix")))
        pm.setdefault("warnings", [])
        out[meta_key("paymix")] = pm

    return out

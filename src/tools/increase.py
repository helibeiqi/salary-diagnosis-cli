# -*- coding: utf-8 -*-
"""
increase.py — 调薪模拟（模块 6：simulate_increase）
================================================================================

回答：「如果预算只有 X%，怎么分最划算？」—— 4 种分配策略，全部**预算守恒**。

四种策略（PRD FR-06 / AC-19~AC-25，口径锁定）
--------------------------------------------------------------------------
A 平均分配    ：每人调薪率 = budget_rate（天然守恒）
B 优先补绿圈  ：① 绿圈补到 max(band_min, 0.8×band_mid)×1.001；② 剩余预算按绩效权重全员二次分配
C 按绩效加权  ：调薪率_i = 预算 × w_i / Σ(w_j × 基数_j)（率只取决于绩效等级，与个体薪资无关）
D 冻结红圈    ：红圈调薪额 = 0；预算在非红圈员工中按绩效权重分配

预算守恒（核心纪律，AC-19）
--------------------------------------------------------------------------
    预算 = round( Σ(月薪 × 12) × budget_rate , 2 )        # 基数分母 K2/K10 = 月薪×12
    每种策略都构造为：Σ(个人调薪额) == 预算（解析上精确相等，无需进位修正）
    → usage_rate 恒为 1.000000

双情景并列（AC-24b，✅ team-lead 裁定强制）
--------------------------------------------------------------------------
    主口径 rebase_band=False：带宽锚定「调薪前」水平（保守，暴露真实成本）
    对照口径 rebase_band=True ：带宽随「全员平均调薪率」同步上移（=年度重定级后的稳态）
    两者 total_cost 完全相同（rebase 只改判定基准，不改调薪额本身）

红绿圈判定复用 band.classify_cr 的口径：
    CR = 月薪 / band_mid；红圈 CR>1.20 或 薪资>上限；绿圈 CR<0.80 或 薪资<下限。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .band import classify_cr, generate_band, _timestamp_compact
from .errors import (
    ColumnMissing,
    CompToolError,
    InvalidParameter,
    UpstreamMissing,
    error_result,
    ok_result,
    tool_guard,
)
from .loader import require_columns, require_session
from .schemas import (
    DEFAULT_MIDPOINT_DIFF,
    GREEN_CIRCLE_CR,
    PERF_WEIGHTS,
    RED_CIRCLE_CR,
)
from .session import get_store
from ._summary import build_increase_summary_md

# 合法策略枚举
_STRATEGIES = ("A", "B", "C", "D")


# -----------------------------------------------------------------------------
# 主工具：simulate_increase
# -----------------------------------------------------------------------------
@tool_guard
def simulate_increase(
    session_id: str,
    strategy: str,
    budget_pct: Optional[float] = None,
    rebase_band: bool = False,
    write_back: bool = True,
    perf_weights: Optional[Dict[str, float]] = None,
    custom_weights: Optional[Dict[str, float]] = None,
    cap_at_max: bool = False,
) -> Dict[str, Any]:
    """
    调薪模拟：在给定预算率下，按指定策略分配调薪额，并给出双情景（rebase）判定。

    参数
    ----
    session_id : str
        load_salary_data → confirm_mapping → generate_band 之后的会话 ID。
        若 DataFrame 缺 band_min/band_mid/band_max，本工具会**自动**以
        current_median + 分层幅度（round_to=0，不取整）补生成带宽。
    strategy : {"A","B","C","D"}
        见模块 docstring 四种策略。
    budget_pct : float, optional
        预算率（占 Σ(月薪×12) 的比例），如 0.05 = 5%。缺省 0.05。
    rebase_band : bool, default False
        仅影响「调薪后红绿圈判定」用的带宽基准：
        False = 用调薪前带宽（主口径）；True = 带宽随全员平均调薪率上移。
        注意：它**不改变调薪额**，两个情景 total_cost 恒等。
    write_back : bool, default True
        是否把调薪列（raise_*/new_*）与 meta['increase'] 回写会话，并导出明细 CSV。
    perf_weights / custom_weights : dict, optional
        绩效权重覆盖（custom_weights 优先级最高，校验 len==4 且 ≥0）。
    cap_at_max : bool, default False
        True 时单人调薪后不超过 band_max（守恒降级为 ≤ 预算，返回 unused_budget）。
        默认 False（真实调薪极少硬封顶，且封顶会破坏守恒可解释性，见 PRD FR-06）。

    返回
    ----
    ok_result 包裹：
        strategy / budget_rate / budget_amount / base_total_annual / total_cost /
        usage_rate / per_perf_rate / scenarios[2] / detail_csv / meta_written
    """
    # ---- 0) 入参校验 ----------------------------------------------------------
    strategy = str(strategy).strip().upper()
    if strategy not in _STRATEGIES:
        raise InvalidParameter(
            f"strategy 只接受 {_STRATEGIES}，收到 {strategy!r}",
            hint="A=平均分配；B=优先补绿圈；C=按绩效加权；D=冻结红圈。",
            details={"strategy": strategy},
        )
    if budget_pct is None:
        budget_pct = 0.05
    budget_pct = float(budget_pct)
    if not (0 < budget_pct <= 0.5):
        raise InvalidParameter(
            f"budget_pct 必须 ∈ (0, 0.5]，收到 {budget_pct}",
            hint="预算率通常取 0.03~0.08（3%~8%）。",
            details={"budget_pct": budget_pct},
        )

    # ---- 1) 取会话 + 确保带宽列存在 -------------------------------------------
    session = require_session(session_id, need_mapping=True)
    df = session.df
    require_columns(df, ["level", "monthly_salary"], tool_name="simulate_increase")

    if not {"band_min", "band_mid", "band_max"}.issubset(df.columns):
        # 自动补生成带宽（不取整，匹配 PRD「建议带宽」口径）
        generate_band(session_id, mode="optimize",
                      midpoint_source="current_median",
                      spread="auto", round_to=0, write_back=True)
        df = get_store().load(session_id).df

    # ---- 2) 计算红绿圈（复用 band.classify_cr，保证口径单一来源）-------------
    enriched = classify_cr(df, band=None, use_bounds=True)
    salary = pd.to_numeric(enriched["monthly_salary"], errors="coerce")
    bmin = pd.to_numeric(enriched["band_min"], errors="coerce")
    bmid = pd.to_numeric(enriched["band_mid"], errors="coerce")
    bmax = pd.to_numeric(enriched["band_max"], errors="coerce")
    is_red = (enriched["flag"] == "红圈").fillna(False)
    is_green = (enriched["flag"] == "绿圈").fillna(False)

    # ---- 3) 绩效权重 ----------------------------------------------------------
    weights = _resolve_perf_weights(enriched, perf_weights, custom_weights)
    # 无绩效列却选 C/D → 回退 A（PRD FR-06 异常处理）
    fallback_a = False
    if weights is None:
        if strategy in ("C", "D"):
            fallback_a = True
        weights = pd.Series(1.0, index=df.index)
    w = weights.astype(float).values

    # ---- 4) 预算基数与预算额（K2/K10：月薪×12）------------------------------
    base = (salary * 12.0).fillna(0.0)                 # 个人年度现金基数
    base_total = float(base.sum())                     # 预算分母
    budget = round(base_total * budget_pct, 2)          # 预算总额（元）
    if base_total <= 0:
        raise InvalidParameter("薪资基数合计为 0，无法分配预算。",
                               details={"base_total": base_total})

    # ---- 5) 按策略分配年度调薪额（解析上 Σ == budget）-----------------------
    # step1_info：仅策略 B 有值（第①步补绿圈成本），供 AC-22 机检
    step1_info: Optional[Dict[str, Any]] = None
    if strategy == "A" or fallback_a:
        raise_annual = _alloc_flat(base.values, budget)
        per_perf_rate = {g: round(budget_pct, 6) for g in ("A", "B", "C", "D")}
    elif strategy == "C":
        raise_annual, per_perf_rate = _alloc_by_perf(base.values, w, budget)
    elif strategy == "D":
        non_red = ~is_red.values
        raise_annual, per_perf_rate = _alloc_redistribute(
            base.values, w, budget, mask=non_red)
    elif strategy == "B":
        raise_annual, per_perf_rate, step1_info = _alloc_fix_green(
            salary.values, bmin.values, bmid.values, base.values, w, budget,
            is_green=is_green.values)
    else:  # pragma: no cover - 已在上面拦截
        raise InvalidParameter(f"未知策略 {strategy}")

    raise_annual = np.asarray(raise_annual, dtype="float64")

    # ---- 6) cap_at_max（可选封顶，守恒降级为 ≤ 预算）-------------------------
    unused_budget = 0.0
    if cap_at_max:
        # 调薪后月薪 > 上限 的部分截掉
        new_sal = salary.values + raise_annual / 12.0
        over = np.maximum(new_sal - bmax.values, 0.0)
        cut = over * 12.0
        raise_annual = raise_annual - cut
        unused_budget = float(cut.sum())

    total_cost = float(raise_annual.sum())
    usage_rate = round(total_cost / budget, 6) if budget > 0 else 0.0

    # ---- 7) 双情景判定（rebase_band）--------------------------------------
    avg_rate = (total_cost / base_total) if base_total > 0 else 0.0
    scenarios = [
        _scenario(enriched, salary.values, raise_annual, bmin.values, bmid.values,
                  bmax.values, rebase=avg_rate if rebase_band else 0.0,
                  rebase_band=rebase_band),
        _scenario(enriched, salary.values, raise_annual, bmin.values, bmid.values,
                  bmax.values, rebase=avg_rate, rebase_band=True),
    ]

    # ---- 8) 写回 + 导出明细 ---------------------------------------------------
    detail_csv = None
    if write_back:
        try:
            out = enriched.copy()
            out["raise_annual"] = np.round(raise_annual, 2)
            out["raise_monthly"] = np.round(raise_annual / 12.0, 2)
            out["raise_rate"] = np.where(base > 0, raise_annual / base, 0.0)
            out["new_monthly_salary"] = np.round(salary + raise_annual / 12.0, 2)
            # 主情景的新 CR / 新判定
            scn0 = scenarios[0]
            out["new_cr"] = scn0["cr"]
            out["new_flag"] = scn0["flag"]
            store = get_store()
            store.save_df(session_id, out)
            store.set_meta(session_id, {
                "increase": {
                    "generated_at": _now(),
                    "strategy": strategy,
                    "budget_rate": budget_pct,
                    "budget_amount": budget,
                    "base_total_annual": round(base_total, 2),
                    "total_cost": round(total_cost, 2),
                    "usage_rate": usage_rate,
                    "per_perf_rate": per_perf_rate,
                    "rebase_band": rebase_band,
                    "step1": step1_info,
                    "scenarios": [{"rebase_band": s["rebase_band"],
                                   "after": s["after"]} for s in scenarios],
                }
            })
            detail_csv = _export_detail(out, strategy)
        except Exception as exc:  # noqa: BLE001
            return error_result(
                exc, message="调薪计算成功，但回写/导出明细失败（不影响本次结果）。")

    # ---- 9) 组装返回 ----------------------------------------------------------
    return ok_result(
        session_id=session_id,
        strategy=strategy,
        budget_rate=budget_pct,
        budget_amount=round(budget, 2),
        base_total_annual=round(base_total, 2),
        total_cost=round(total_cost, 2),
        usage_rate=usage_rate,
        per_perf_rate=per_perf_rate,
        rebase_band=rebase_band,
        scenarios=[{
            "rebase_band": s["rebase_band"],
            "new_band_scaled": bool(s["rebase_band"]),
            "total_cost": round(total_cost, 2),
            "after": s["after"],
        } for s in scenarios],
        detail_csv=detail_csv,
        # 第①步补绿圈成本（仅策略 B；AC-22 机检用，其余策略为 None）
        step1_cost=step1_info["cost"] if step1_info else None,
        step1_headcount=step1_info["headcount"] if step1_info else None,
        step1_scaled=step1_info["scaled"] if step1_info else None,
        unused_budget=round(unused_budget, 2) if cap_at_max else None,
        # P6 修复（2026-09-04 续）：语义化摘要由代码生成，LLM 只做转述。
        summary_md=build_increase_summary_md({
            "strategy": strategy,
            "budget_rate": budget_pct,
            "budget_amount": round(budget, 2),
            "base_total_annual": round(base_total, 2),
            "total_cost": round(total_cost, 2),
            "usage_rate": usage_rate,
            "step1_cost": step1_info["cost"] if step1_info else None,
            "step1_headcount": step1_info["headcount"] if step1_info else None,
            "unused_budget": round(unused_budget, 2) if cap_at_max else None,
        }),
        meta_written="increase" if write_back else None,
        hint="调薪模拟完成。下一步：generate_report 汇总成报告，或对比多种策略。",
    )


# -----------------------------------------------------------------------------
# 分配内核（均解析上 Σ == budget）
# -----------------------------------------------------------------------------
def _alloc_flat(base: np.ndarray, budget: float) -> np.ndarray:
    """策略 A：每人调薪率相同 = budget / Σ基数。"""
    s = float(base.sum())
    if s <= 0:
        return np.zeros_like(base, dtype="float64")
    return base * (budget / s)


def _alloc_by_perf(base: np.ndarray, w: np.ndarray,
                   budget: float) -> tuple[np.ndarray, Dict[str, float]]:
    """策略 C：调薪率只取决于绩效等级（与个体薪资无关）。

    公式：raise_i = budget × (w_i × base_i) / Σ(w_j × base_j)
    → 同绩效等级的人 raise_i / base_i 完全相同 = budget × w_i / Σ(w_j base_j)
    """
    eff = w * base
    s = float(eff.sum())
    if s <= 0:
        # 权重全 0（极端情形）→ 退化为均分
        return _alloc_flat(base, budget), {g: round(budget / (float(base.sum()) or 1), 8)
                                           for g in ("A", "B", "C", "D")}
    raises = budget * eff / s
    # 各绩效等级的「统一调薪率」用闭式解：rate_g = budget × w_g / Σ(w_j·base_j)
    # 说明：数学上等价于取组内任一人的 raise_i/base_i，但闭式解不依赖具体个体，
    #       因而与下游封顶/调整脱钩，也不会因单点浮点尾差失真。
    # 精度：保留 8 位小数——PRD AC-21 金标准（9.060064%/6.040042%/2.516684%）
    #       即为 8 位小数口径，取 6 位会使金标准在 1e-8 量级上不可机器校验。
    per_perf: Dict[str, float] = {}
    for g, wg in (("A", 1.8), ("B", 1.2), ("C", 0.5), ("D", 0.0)):
        mask = (np.isclose(w, wg)) & (base > 0)
        per_perf[g] = round(budget * wg / s, 8) if mask.any() else 0.0
    return raises, per_perf


def _alloc_redistribute(base: np.ndarray, w: np.ndarray, budget: float,
                         mask: np.ndarray) -> tuple[np.ndarray, Dict[str, float]]:
    """策略 D：mask 内（非红圈）员工按绩效权重分掉全部预算；mask 外（红圈）= 0。"""
    raises = np.zeros_like(base, dtype="float64")
    eff = w * base
    eff_m = eff[mask]
    s = float(eff_m.sum())
    if s <= 0:
        # 非红圈群体权重全 0 → 退回均分非红圈
        n = int(mask.sum())
        if n:
            raises[mask] = budget / n
        return raises, {g: 0.0 for g in ("A", "B", "C", "D")}
    raises[mask] = budget * eff_m / s
    per_perf: Dict[str, float] = {}
    for g, wg in (("A", 1.8), ("B", 1.2), ("C", 0.5), ("D", 0.0)):
        m = mask & (np.isclose(w, wg)) & (base > 0)
        # 闭式解：rate_g = budget × w_g / Σ(非红圈 w_j·base_j)，与 _alloc_by_perf 同口径
        per_perf[g] = round(budget * wg / s, 8) if m.any() else 0.0
    return raises, per_perf


def _alloc_fix_green(salary: np.ndarray, bmin: np.ndarray, bmid: np.ndarray,
                     base: np.ndarray, w: np.ndarray, budget: float,
                     is_green: Optional[np.ndarray] = None,
                     ) -> tuple[np.ndarray, Dict[str, float], Dict[str, Any]]:
    """策略 B：先补绿圈到 max(band_min, 0.8×band_mid)×1.001，剩余按绩效权重全员二次分配。

    第①步成本若已超预算，则第①步整体按比例缩放到预算（剩余=0）。

    参数
    ----
    is_green : ndarray of bool, optional
        绿圈掩码。PRD AC-22 原文为「**绿圈员工**补到 max(band_min, 0.8×band_mid)×1.001」，
        故传入时第①步只作用于绿圈员工；不传则退回「所有低于目标者」的宽松口径。
        （金标准数据集上二者结果完全一致：非绿圈者薪资必然 ≥ 目标值，
        故此处传与不传不影响 AC-22，但传入更贴合 PRD 语义、且对其它数据集更稳。）

    返回
    ----
    (raises, per_perf, step1)
        step1 = {"cost": 第①步年度补绿圈成本, "headcount": 涉及人数,
                 "scaled": 是否因超预算被比例缩放}
        供 AC-22 机检使用（PRD 要求「第①步成本 472,889 元 / 26 人」可机检）。
    """
    # 第①步：补到 max(band_min, 0.8×band_mid)×1.001（×1.001 确保补完即脱离绿圈）
    target = np.maximum(bmin, 0.8 * bmid) * 1.001
    raise1 = np.maximum(target - salary, 0.0) * 12.0          # 年度
    if is_green is not None:
        raise1 = np.where(is_green, raise1, 0.0)
    step1_cost = float(raise1.sum())
    step1_head = int((raise1 > 0).sum())
    scaled = False
    if step1_cost > budget and step1_cost > 0:
        raise1 = raise1 * (budget / step1_cost)
        step1_cost = budget
        scaled = True
    remaining = budget - step1_cost

    # 第②步：剩余在**全员**中按绩效权重分配
    eff = w * base
    s = float(eff.sum())
    raise2 = np.zeros_like(base, dtype="float64")
    if remaining > 0 and s > 0:
        raise2 = remaining * eff / s
    raises = raise1 + raise2

    per_perf: Dict[str, float] = {}
    for g, wg in (("A", 1.8), ("B", 1.2), ("C", 0.5), ("D", 0.0)):
        m = (np.isclose(w, wg)) & (base > 0)
        per_perf[g] = round(float(raises[m][0] / base[m][0]), 6) if m.any() else 0.0

    step1 = {"cost": round(step1_cost, 2),
             "headcount": step1_head,
             "scaled": scaled}
    return raises, per_perf, step1


# -----------------------------------------------------------------------------
# 双情景判定
# -----------------------------------------------------------------------------
def _scenario(enriched: pd.DataFrame, salary: np.ndarray, raise_annual: np.ndarray,
              bmin: np.ndarray, bmid: np.ndarray, bmax: np.ndarray,
              rebase: float, rebase_band: bool) -> Dict[str, Any]:
    """计算某个 rebase 情景下的新薪资 / 新 CR / 新红绿圈。

    rebase=0      → 带宽不变（主口径）
    rebase=avg_rate → 带宽三列同步 ×(1+avg_rate)（年度重定级稳态）
    调薪额 raise_annual 在两个情景里完全相同（只改判定基准）。
    """
    new_salary = salary + raise_annual / 12.0
    scale = (1.0 + rebase)
    nbmin = bmin * scale
    nbmid = bmid * scale
    nbmax = bmax * scale
    new_cr = np.where(nbmid > 0, new_salary / nbmid, np.nan)
    above = new_salary > nbmax
    below = new_salary < nbmin
    flag = np.where(new_cr > RED_CIRCLE_CR, "红圈",
                    np.where(above, "红圈",
                             np.where(new_cr < GREEN_CIRCLE_CR, "绿圈",
                                      np.where(below, "绿圈", "合理"))))
    red = int((flag == "红圈").sum())
    green = int((flag == "绿圈").sum())
    normal = int((flag == "合理").sum())
    over_max = int(above.sum())
    cr_valid = new_cr[~np.isnan(new_cr)]
    return {
        "rebase_band": rebase_band,
        "cr": np.round(new_cr, 6),
        "flag": flag,
        "after": {
            "red": red, "green": green, "normal": normal, "over_max": over_max,
            "cr_mean": round(float(np.mean(cr_valid)), 6) if len(cr_valid) else 0.0,
            "cr_median": round(float(np.median(cr_valid)), 6) if len(cr_valid) else 0.0,
        },
    }


# -----------------------------------------------------------------------------
# 绩效权重解析
# -----------------------------------------------------------------------------
def _resolve_perf_weights(enriched: pd.DataFrame,
                          perf_weights: Optional[Dict[str, float]],
                          custom_weights: Optional[Dict[str, float]]
                          ) -> Optional[pd.Series]:
    """返回每人的绩效权重 Series；没有可用绩效列时返回 None。"""
    # 防御：工资表天然不含 perf_grade 列，缺失时直接回退（否则 KeyError 崩）。
    if "perf_grade" not in enriched.columns:
        return None
    src = custom_weights or perf_weights or PERF_WEIGHTS
    if enriched["perf_grade"].isna().all():
        return None
    default = PERF_WEIGHTS.get("C", 0.5)

    def _w(g: Any) -> float:
        if g is None or (isinstance(g, float) and np.isnan(g)):
            return 0.0
        return float(src.get(str(g).strip().upper(), default))

    return enriched["perf_grade"].map(_w)


# -----------------------------------------------------------------------------
# 明细导出
# -----------------------------------------------------------------------------
def _export_detail(out: pd.DataFrame, strategy: str) -> Optional[str]:
    """导出个人调薪明细 CSV（含脱敏 emp_id，绝不含姓名之外 PII 原文）。"""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(root, "data")
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, f"increase_{strategy}_{_timestamp_compact()}.csv")
        cols = ["emp_id", "level", "perf_grade", "monthly_salary",
                "raise_annual", "raise_monthly", "raise_rate",
                "new_monthly_salary", "new_cr", "new_flag"]
        cols = [c for c in cols if c in out.columns]
        out[cols].to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except Exception:  # noqa: BLE001
        return None


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

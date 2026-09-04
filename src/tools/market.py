# -*- coding: utf-8 -*-
"""
market.py — 市场对标（模块 5：market_benchmark）
================================================================================

回答三个业务问题：
    ① 我们比市场低多少？（公司薪资 vs 市场分位值的差距）
    ② 补齐到市场分位要花多少钱？（达标成本测算）
    ③ 按什么分位值花钱最划算？（P25 / P50 / P75 / 按序列选分位）

口径严格遵循 PRD §2.2 / FR-05 / AC-15~AC-18：
    - 差距 = (个人月薪 - 市场目标值) / 市场目标值
        · 负数 = 低于市场（外部竞争力不足、流失风险）
        · 正数 = 高于市场（成本高、内部公平性风险）
    - 市场目标值（mkt_target）的取法：
        · strategy="P25"/"P50"/"P75" → 全公司统一取 mkt_p25/p50/p75 列
        · strategy="by_family"        → 按岗位序列选分位（见 DEFAULT_MARKET_STRATEGY）
            销售/技术→P75（领先市场，留核心人才）、管理/职能→P50（跟随市场）、
            操作→P25（滞后市场，成本优先）
    - 达标成本 = Σ max(0, mkt_target - 月薪)（只算「低于市场、需要补」的部分）
    - 预算分母（K10）= Σ(月薪 × 12)，与调薪模块完全一致

数据源优先级：
    ① 调用方显式传入 market_data（dict：{序列: {p25,p50,p75}}）
    ② 会话 DataFrame 自带 mkt_p25/p50/p75 列（推荐，金标准 AC-15~18 走这条）
    ③ 缺列时回退到本模块内置 DEFAULT_MARKET_TABLE（按序列的合成值，仅兜底/演示用；
       真实市场分位值应由用户导入，见 PRD §8 #4：本工具不做数据源）
    ④ 三者皆无 → 返回 ok=false + error.code = MISSING_REQUIRED_FIELD

⚠️ 安全：写回 DataFrame 的只是「每人一个目标值 + 差距 + 定位」的聚合语义列，
   绝不写回任何 PII；meta['market'] 只存聚合统计。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

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
    DEFAULT_MARKET_STRATEGY,
    MARKET_FIELD_BY_KEY,
    infer_job_family,
)
from .session import get_store
from ._summary import build_market_summary_md

# -----------------------------------------------------------------------------
# 内置默认市场分位表（按岗位序列，单位：元/月）
# -----------------------------------------------------------------------------
# ⚠️ 仅为「缺市场列时的兜底 / 演示」提供一组合成值，量级参考本数据集的市场列。
#    真实对标务必由用户导入 mkt_p25/p50/p75 三列（PRD §8 #4）。
#    键是岗位序列（与 infer_job_family 的返回值对齐）；值取该序列的 p25/p50/p75。
DEFAULT_MARKET_TABLE: Dict[str, Dict[str, float]] = {
    "销售": {"p25": 12000.0, "p50": 16000.0, "p75": 22000.0},
    "技术": {"p25": 11000.0, "p50": 15000.0, "p75": 21000.0},
    "管理": {"p25": 32000.0, "p50": 45000.0, "p75": 65000.0},
    "操作": {"p25": 5000.0,  "p50": 7000.0,  "p75": 9500.0},
    "职能": {"p25": 8000.0,  "p50": 12000.0, "p75": 17000.0},
}

# 合法的策略枚举（与 PRD FR-05 一致）
_MARKET_STRATEGIES = ("P25", "P50", "P75", "by_family")


# -----------------------------------------------------------------------------
# 主工具：market_benchmark
# -----------------------------------------------------------------------------
@tool_guard
def market_benchmark(
    session_id: str,
    strategy: str = "P50",
    group_by: Optional[List[str]] = None,
    only_below: bool = True,
    market_data: Optional[Dict[str, Dict[str, float]]] = None,
    write_back: bool = True,
) -> Dict[str, Any]:
    """
    市场对标：计算个人/公司 vs 市场分位值的差距、达标成本与分位策略结论。

    参数
    ----
    session_id : str
        load_salary_data → confirm_mapping 之后的会话 ID（映射必须已确认）。
    strategy : {"P25","P50","P75","by_family"}, default "P50"
        P25/P50/P75 = 全公司统一取对应市场分位列；
        by_family   = 按岗位序列自动选分位（DEFAULT_MARKET_STRATEGY）。
    group_by : list[str], optional
        聚合维度，默认 ["level"]；可加 "job_family"。
    only_below : bool, default True
        达标成本是否只算「低于市场、需要补」的部分（max(0, target - 月薪)）。
        False 时改成「双向对齐成本」Σ|target - 月薪|。
    market_data : dict, optional
        外部市场分位表 {序列: {"p25":..,"p50":..,"p75":..}}，优先级高于内置兜底。
    write_back : bool, default True
        是否把 mkt_target / mkt_gap / mkt_position 三列与 meta['market'] 回写会话。

    返回
    ----
    ok_result 包裹：
        strategy_used        实际采用的策略
        overall_gap          {mean, median}（个人均值口径 / 中位数口径，见 AC-15 陷阱）
        gap_table            逐职级差距（n/公司中位/市场中位/中位数口径差距/均值口径差距）
        cost                 {monthly, annual, pct_of_payroll, headcount}
        by_family            逐序列达标成本
        meta_written         "market"
    """
    # ---- 0) 入参校验 ----------------------------------------------------------
    if strategy not in _MARKET_STRATEGIES:
        raise InvalidParameter(
            f"strategy 只接受 {_MARKET_STRATEGIES}，收到 {strategy!r}",
            hint="P25=滞后市场；P50=跟随市场；P75=领先市场；by_family=按序列选分位。",
            details={"strategy": strategy},
        )

    # ---- 1) 取会话 ------------------------------------------------------------
    session = require_session(session_id, need_mapping=True)
    df = session.df
    require_columns(df, ["level", "monthly_salary"], tool_name="market_benchmark")

    group_by = group_by or ["level"]

    # ---- 2) 解析每人的市场目标值 mkt_target -----------------------------------
    target, src_desc, warnings = _resolve_market_target(
        df, strategy, market_data=market_data)

    if target is None or target.notna().sum() == 0:
        # 没有任何可用的市场数据：明确失败，引导用户补市场列
        # 注意：PRD AC-34#2 / AC-37 要求本场景的错误码为 MISSING_REQUIRED_FIELD，
        # 而 errors.py 既有的语义等价类是 COLUMN_MISSING；这里复用后者并改写 code
        # 字符串，既复用异常行为又不引入新的裸异常类。
        exc = ColumnMissing(
            "会话中没有任何可用的市场分位值（无 mkt_p25/p50/p75 列，"
            "且无 market_data / 内置兜底也无法覆盖）。",
            hint="请为数据补充 mkt_p25/p50/p75 三列，或在调用时传入 market_data。",
            details={"strategy": strategy},
        )
        exc.code = "MISSING_REQUIRED_FIELD"
        raise exc

    # ---- 3) 逐人计算差距 / 市场比值 / 定位 ------------------------------------
    salary = pd.to_numeric(df["monthly_salary"], errors="coerce")
    tgt = pd.to_numeric(target, errors="coerce")
    valid = salary.notna() & tgt.notna() & (tgt > 0)

    gap = pd.Series(np.nan, index=df.index, dtype="float64")
    gap_pct = pd.Series(np.nan, index=df.index, dtype="float64")
    compa = pd.Series(np.nan, index=df.index, dtype="float64")
    position = pd.Series("", index=df.index, dtype="object")
    gap.loc[valid] = (salary.loc[valid] - tgt.loc[valid])
    gap_pct.loc[valid] = (salary.loc[valid] - tgt.loc[valid]) / tgt.loc[valid]
    compa.loc[valid] = salary.loc[valid] / tgt.loc[valid]
    # 定位：相差 < 0.5% 视为「持平(at)」，否则 below/above
    pos = pd.Series("above", index=df.index, dtype="object")
    pos.loc[valid & (gap_pct < -0.005)] = "below"
    pos.loc[valid & (gap_pct > 0.005)] = "above"
    pos.loc[valid & (gap_pct.abs() <= 0.005)] = "at"
    position.loc[valid] = pos.loc[valid]

    # ---- 4) 聚合：整体 / 逐职级 / 逐序列 --------------------------------------
    # 4.1 整体差距（双口径）
    gp = gap_pct[valid]
    overall_gap = {
        "mean": round(float(gp.mean()), 6) if len(gp) else 0.0,           # 个人均值口径
        "median": round(float(gp.median()), 6) if len(gp) else 0.0,       # 中位数口径
    }

    # 4.2 逐职级差距表
    gap_table: List[Dict[str, Any]] = []
    lv_key = "level"
    for lv, g in df[valid].groupby(lv_key):
        s = salary.loc[g.index]
        t = tgt.loc[g.index]
        gpp = gap_pct.loc[g.index]
        gap_table.append({
            "level": str(lv),
            "n": int(len(g)),
            "company_median": round(float(s.median()), 2),
            "market_target_median": round(float(t.median()), 2),
            "median_gap_pct": round(float(gpp.median()), 6),
            "mean_gap_pct": round(float(gpp.mean()), 6),
        })
    gap_table.sort(key=lambda d: str(d["level"]))

    # 4.3 逐序列（按推断序列）差距
    by_family: List[Dict[str, Any]] = []
    fam_series = _family_series(df)
    for fam, g in df[valid].groupby(fam_series.loc[valid]):
        s = salary.loc[g.index]
        t = tgt.loc[g.index]
        gpp = gap_pct.loc[g.index]
        by_family.append({
            "job_family": str(fam),
            "n": int(len(g)),
            "company_median": round(float(s.median()), 2),
            "market_target_median": round(float(t.median()), 2),
            "median_gap_pct": round(float(gpp.median()), 6),
        })
    by_family.sort(key=lambda d: str(d["job_family"]))

    # ---- 5) 达标成本 ----------------------------------------------------------
    # 只算「低于市场、需要补」的部分：max(0, target - salary)
    if only_below:
        need = (tgt - salary).clip(lower=0)
    else:
        need = (tgt - salary).abs()
    monthly_cost = float(need[valid].sum())
    annual_cost = monthly_cost * 12.0
    # 预算分母 K10：统一用 Σ(月薪 × 12)，覆盖率 100%。
    #   分母必须是**全量薪资基数**（所有月薪有效行），而不是「有市场目标值的行」。
    #   本指标回答的是「补齐到市场要花掉我们工资总额的百分之多少」，基数就是
    #   公司实际发放的工资总额；若改用子集做分母，会系统性**高估**占比。
    #   实测（sample_salary.csv，by_family，mkt_p25 有 10 个 NaN 致 3 行无法对标）：
    #       子集分母 → 15.42%，全量分母 → 15.26%（= PRD AC-18 金标准）。
    #   市场数据缺失只影响**分子**（无对标值者不计入补齐成本），不影响分母。
    salary_valid = salary.notna()
    base_total = float((salary.loc[salary_valid] * 12.0).sum())
    pct_of_payroll = round(annual_cost / base_total, 6) if base_total > 0 else 0.0
    headcount = int((need[valid] > 0).sum())
    if int((salary_valid & ~valid).sum()):
        warnings.append(
            f"有 {int((salary_valid & ~valid).sum())} 行因缺少市场分位值无法对标，"
            "未计入达标成本（分子），但已计入薪资基数（分母，K10 覆盖率 100%）。")

    cost = {
        "monthly": round(monthly_cost, 2),
        "annual": round(annual_cost, 2),
        "pct_of_payroll": pct_of_payroll,
        "headcount": headcount,
        "only_below": only_below,
    }

    # ---- 6) 写回会话（列 + meta）--------------------------------------------
    if write_back:
        try:
            out = df.copy()
            out["mkt_target"] = tgt
            out["mkt_gap"] = gap
            out["mkt_position"] = position
            # 若已存在 mkt_comparatio 等列则更新，避免重复
            store = get_store()
            store.save_df(session_id, out)
            store.set_meta(session_id, {
                "market": {
                    "generated_at": _now(),
                    "strategy": strategy,
                    "source": src_desc,
                    "overall_gap": overall_gap,
                    "cost": cost,
                    "by_family": by_family,
                }
            })
        except Exception as exc:  # noqa: BLE001
            return error_result(
                exc, message="市场对标计算成功，但回写会话失败（不影响本次结果）。")

    # ---- 7) 组装返回 ----------------------------------------------------------
    return ok_result(
        session_id=session_id,
        strategy_used=strategy,
        source=src_desc,
        overall_gap=overall_gap,
        gap_table=gap_table,
        by_family=by_family,
        cost=cost,
        # P5 修复（2026-09-04 续）：语义化摘要由确定性代码生成，LLM 只做转述。
        # 裸字段 JSON 曾诱导模型把"补齐成本"说成"节省"、并擅自跨字段加总。
        summary_md=build_market_summary_md({
            "strategy_used": strategy,
            "source": src_desc,
            "overall_gap": overall_gap,
            "gap_table": gap_table,
            "by_family": by_family,
            "cost": cost,
        }),
        columns_added=["mkt_target", "mkt_gap", "mkt_position"],
        meta_written="market" if write_back else None,
        warnings=warnings,
        hint="市场对标完成。下一步：simulate_increase 在预算内制定调薪方案。",
    )


# -----------------------------------------------------------------------------
# 内部：岗位序列解析（显式列优先）
# -----------------------------------------------------------------------------
def _family_series(df: pd.DataFrame) -> "pd.Series":
    """
    返回每行所属的岗位序列（销售/技术/管理/操作/职能）。

    优先级
    ------
    ① 表内显式的 ``job_family`` 列 —— 数据自带分类，最权威；
    ② 该列缺失或该行为空时，退回 ``infer_job_family(dept, level)`` 推断。

    为什么必须「显式列优先」（AC-18 定标依据）
    ------------------------------------------
    ``infer_job_family`` 的规则是 *level 以 M 开头即判为「管理」*（见
    schemas.py:220-223），会把 M 职级的销售总监、技术专家等**按职能归属**
    的人错误并入「管理」序列。由于不同序列对应的市场分位不同
    （DEFAULT_MARKET_STRATEGY：销售/技术→P75、管理/职能→P50、操作→P25），
    错分序列会直接错配目标分位值，进而算错达标成本与人头数。

    实测（data/sample_salary.csv，150 行，strategy="by_family"）：
        显式 job_family 列 → 498,000 元/月、99 人  ← 与 PRD AC-18 金标准一致
        纯 infer_job_family → 498,100 元/月、102 人 ← 偏离（3 人误并入管理）
    故本函数以显式列为第一优先级，推断仅作兜底。
    """
    inferred = df.apply(
        lambda r: infer_job_family(r.get("dept"), r.get("level")), axis=1)

    if "job_family" in df.columns:
        # 统一为去空白的字符串；空值 / 纯空白 / 字面 "nan" 均视为未归类
        s = df["job_family"].map(
            lambda v: str(v).strip()
            if (v is not None and str(v).strip() != ""
                and str(v).lower() != "nan")
            else None)
        if s.notna().any():
            # 显式列有值即用之，未归类的行才用推断兜底
            return s.where(s.notna(), inferred)

    return inferred


# -----------------------------------------------------------------------------
# 内部：解析每人的市场目标值
# -----------------------------------------------------------------------------
def _resolve_market_target(
    df: pd.DataFrame,
    strategy: str,
    market_data: Optional[Dict[str, Dict[str, float]]] = None,
) -> tuple[Optional[pd.Series], str, List[str]]:
    """
    返回 (目标值 Series 或 None, 数据源描述, 警告列表)。

    解析优先级：
        ① market_data 显式传入
        ② DataFrame 自带 mkt_p25/p50/p75 列
        ③ 内置 DEFAULT_MARKET_TABLE 兜底（按推断序列）
    """
    warnings: List[str] = []
    has_cols = {k: (k in df.columns) for k in MARKET_FIELD_BY_KEY.values()}

    # ---- 路径 A：外部 market_data（按序列查表）------------------------------
    if market_data:
        fam = _family_series(df)
        tgt = fam.map(lambda f: _pick_from_table(market_data, f, strategy))
        if tgt.notna().any():
            return tgt, "外部 market_data（按岗位序列）", warnings
        warnings.append("market_data 未能覆盖任何行的岗位序列，回退到其他数据源。")

    # ---- 路径 B：表内市场列 ---------------------------------------------------
    if any(has_cols.values()):
        if strategy in ("P25", "P50", "P75"):
            col = MARKET_FIELD_BY_KEY[strategy]
            if col in df.columns:
                return df[col].copy(), f"表内市场列 {col}", warnings
        else:  # by_family：逐行按序列选分位列
            out = pd.Series(np.nan, index=df.index, dtype="float64")
            fam_series = _family_series(df)
            for fam, pct in DEFAULT_MARKET_STRATEGY.items():
                col = MARKET_FIELD_BY_KEY[pct]
                if col not in df.columns:
                    continue
                mask = fam_series == fam
                out.loc[mask] = df.loc[mask, col]
            if out.notna().any():
                n_miss = int((~out.notna()).sum())
                if n_miss:
                    warnings.append(
                        f"by_family 策略下有 {n_miss} 行无法匹配到市场列（已忽略）。")
                return out, "表内市场列（按序列选 P25/P50/P75）", warnings

    # ---- 路径 C：内置兜底表（按序列）----------------------------------------
    fam = _family_series(df)
    tgt = fam.map(lambda f: _pick_from_table(DEFAULT_MARKET_TABLE, f, strategy))
    if tgt.notna().any():
        warnings.append("无市场列，已用内置默认市场分位表兜底（合成值，仅供演示）。")
        return tgt, "内置默认市场分位表（兜底）", warnings

    return None, "无可用市场数据", warnings


def _pick_from_table(table: Dict[str, Dict[str, float]],
                     family: str, strategy: str) -> Optional[float]:
    """从 {序列: {p25,p50,p75}} 表里取该序列对应分位的值；by_family 时序列映射后再取。"""
    row = table.get(family)
    if not row:
        return None
    if strategy in ("P25", "P50", "P75"):
        return row.get(strategy.lower())
    # by_family：先按序列决定分位（DEFAULT_MARKET_STRATEGY），再取对应分位
    pct = DEFAULT_MARKET_STRATEGY.get(family, "P50")
    return row.get(pct.lower())


def _now() -> str:
    """返回人类可读时间戳（与 band/loader 同款）。"""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

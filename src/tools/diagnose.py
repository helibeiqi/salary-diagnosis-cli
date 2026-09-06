# -*- coding: utf-8 -*-
"""
diagnose.py — 薪酬现状诊断（模块 3：analyze_current_state）
================================================================================

这是整个诊断链路的第一道「体检」：在已经生成带宽（generate_band）之后，
对每一个员工计算：

    ① CR（Compa-Ratio）= 个人月薪 / 该职级带宽中位值
    ② 带宽渗透率（Range Penetration）= (月薪 - 下限) / (上限 - 下限)
    ③ 红圈 / 绿圈 / 合理 判定
    ④ 各职级、全公司的 CR 分布与成本测算

所有口径严格复用 band.py 的 classify_cr / summarize_cr，本模块只做「编排 + 回写」，
不重复实现任何薪酬数学，避免口径漂移（这是本项目最重要的纪律之一：
单一真理源，禁止同一公式多处改写）。

红绿圈判定规则（PRD 模块 1 口径，PRD §K 与 band.classify_cr 同源）：
    - 红圈：CR > 1.20 或 月薪 > 带宽上限 → 薪酬高于该职级应有水平，成本溢出
    - 绿圈：CR < 0.80 或 月薪 < 带宽下限 → 薪酬低于该职级应有水平，流失风险最高
    - 合理：0.80 ≤ CR ≤ 1.20 且 月薪在带宽内

数据写回约定（与 generate_band 完全一致，见 band.py:1186）：
    - 新增列 cr / penetration / flag / flag_reason 合并回会话 DataFrame
    - meta['diagnose'] 写入汇总结论（**只存聚合数值，绝不存行级薪资明细**）
"""

from __future__ import annotations

from typing import Any, Dict


from .band import classify_cr, summarize_cr
from .errors import UpstreamMissing, error_result, ok_result, tool_guard
from .loader import require_columns, require_session
from .session import get_store
from .timefmt import now_str as _now
from ._summary import build_summary_md


# -----------------------------------------------------------------------------
# 主工具：analyze_current_state
# -----------------------------------------------------------------------------
@tool_guard
def analyze_current_state(
    session_id: str,
    level_field: str = "level",
    salary_field: str = "monthly_salary",
    red_cr: float = 1.20,
    green_cr: float = 0.80,
    annual_months: int = 12,
    write_back: bool = True,
) -> Dict[str, Any]:
    """
    对会话做一次完整的薪酬现状诊断（CR / 渗透率 / 红绿圈 / 成本测算）。

    前置条件
    --------
    必须先调用 generate_band（write_back=True）让会话 DataFrame 带上
    band_min / band_mid / band_max 三列。若缺失则抛 UpstreamMissing，
    明确提示用户「先跑 generate_band」。

    参数
    ----
    session_id : str
        load_salary_data → confirm_mapping → generate_band 之后得到的会话 ID。
    level_field / salary_field : str
        分组键与薪资列（默认 level / monthly_salary）。
    red_cr / green_cr : float
        红/绿圈 CR 阈值（默认 1.20 / 0.80，来自 schemas.RED_CIRCLE_CR / GREEN_CIRCLE_CR）。
    annual_months : int
        年化月数（默认 12），用于把月度成本测算换算成年化。
    write_back : bool
        是否把 CR/渗透率/红绿圈列与 meta['diagnose'] 回写会话。

    返回
    ----
    ok_result 包裹的诊断结论：
        df_columns_added     本次新增的列名
        cr_summary           CR 分布统计（mean/std/min/p25/median/p75/max）
        red_green            红/绿/合理人数与占比
        cost                 红圈溢出 / 绿圈补足（至下限 / 至中位值）年化成本
        by_level             各职级红绿圈人数
        summary_md           ★语义化摘要（代码生成，供 narration 直接转述；方向已标明）
        meta_written         回写到的 meta 键（diagnose）
    """
    # ---- 1) 取会话（映射须已确认）--------------------------------------------
    session = require_session(session_id, need_mapping=True)
    df = session.df
    require_columns(df, [level_field, salary_field], tool_name="analyze_current_state")

    # ---- 2) 校验带宽三列（诊断的命脉）----------------------------------------
    if not {"band_min", "band_mid", "band_max"}.issubset(df.columns):
        raise UpstreamMissing(
            "会话 DataFrame 缺少 band_min/band_mid/band_max 三列，无法计算 CR。",
            hint="请先调用 generate_band（write_back=True）为数据中出现的全部职级生成带宽。",
            details={"missing": sorted(set(["band_min", "band_mid", "band_max"]) - set(df.columns))},
        )

    # ---- 3) 个体判定（完全委托 band.classify_cr，保证口径单一来源）-----------
    # use_bounds=True：红绿圈同时看 CR 阈值与带宽上下限（PRD 口径）
    enriched = classify_cr(
        df,
        band=None,  # 用 df 自带的 band_min/band_mid/band_max
        level_field=level_field,
        salary_field=salary_field,
        red_cr=red_cr,
        green_cr=green_cr,
        use_bounds=True,
    )

    # ---- 4) 汇总（红绿圈人数/占比/成本测算）---------------------------------
    summary = summarize_cr(enriched, monthly_salary_field=salary_field,
                           annual_months=annual_months)

    # ---- 5) 写回会话（列 + meta）--------------------------------------------
    # 回写列：band_min/band_mid/band_max/cr/penetration/flag/flag_reason
    # （列已存在时 save_df 整表覆盖，语义即「重新诊断以最新结果为准」）。
    if write_back:
        try:
            # classify_cr 返回的 enriched 已是「原 df 全部列 + 新增诊断列」，
            # 直接整体回写即可（重新诊断会自然覆盖旧列）。
            store = get_store()
            store.save_df(session_id, enriched)
            store.set_meta(session_id, {
                "diagnose": {
                    "generated_at": _now(),
                    "red_cr": red_cr,
                    "green_cr": green_cr,
                    "annual_months": annual_months,
                    "cr_stats": summary.get("cr_stats", {}),
                    "counts": summary.get("counts", {}),
                    "ratios": summary.get("ratios", {}),
                    "cost": summary.get("cost", {}),
                    "by_level": summary.get("by_level", {}),
                }
            })
        except Exception as exc:  # noqa: BLE001
            return error_result(
                exc, message="诊断计算成功，但回写会话失败（不影响本次结果）。")

    # ---- 6) 组装返回 ----------------------------------------------------------
    return ok_result(
        session_id=session_id,
        df_columns_added=["cr", "penetration", "flag", "flag_reason"],
        cr_summary=summary.get("cr_stats", {}),
        red_green={
            "counts": summary.get("counts", {}),
            "ratios": summary.get("ratios", {}),
        },
        cost=summary.get("cost", {}),
        by_level=summary.get("by_level", {}),
        # P1 修复（2026-09-04 实测）：语义化摘要由确定性代码生成，让 LLM 只做转述。
        # 裸字段 JSON 曾诱导模型把"补足成本"说成"节省"、并擅自跨字段加总（A/B 3/3 复现）。
        summary_md=build_summary_md({
            "cr_summary": summary.get("cr_stats", {}),
            "red_green": {
                "counts": summary.get("counts", {}),
                "ratios": summary.get("ratios", {}),
            },
            "cost": summary.get("cost", {}),
            "by_level": summary.get("by_level", {}),
        }),
        meta_written="diagnose" if write_back else None,
        hint="现状诊断完成。下一步：market_benchmark 做市场对标，或 simulate_increase 做调薪模拟。",
    )


# -----------------------------------------------------------------------------
# 语义化摘要（summary_md）已迁至 _summary.py（单一真理源，见该模块「设计四原则」）。
# 本模块只通过 `from ._summary import build_summary_md` 引用，避免逻辑分叉。
# -----------------------------------------------------------------------------


# 供 registry / 工具注册表引用的元数据（名称、入参、返回），保持与 PRD 11 工具命名空间一致
TOOL_META = {
    "name": "analyze_current_state",
    "description": "薪酬现状诊断：计算 CR、带宽渗透率、红绿圈判定与成本测算。",
    "parameters": {
        "session_id": {"type": "string", "description": "会话 ID", "required": True},
        "red_cr": {"type": "number", "description": "红圈 CR 阈值（默认 1.20）", "required": False},
        "green_cr": {"type": "number", "description": "绿圈 CR 阈值（默认 0.80）", "required": False},
    },
}

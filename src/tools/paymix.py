# -*- coding: utf-8 -*-
"""
paymix.py — 固浮比（Pay Mix）拆分与激励曲线分析
================================================================================

「固浮比」= 固定薪 : 浮动薪（如 销售 40:60 表示固定占 40%、浮动占 60%）。
它是薪酬体系中「保障 vs 激励」的旋钮：浮动比例越高，员工收入越跟业绩挂钩、
公司风险越低，但前提是**业绩能量化、能归因到个人**。

本模块做两件事：
    1. **拆分（结构设计）**：把每个员工的月薪 monthly_salary 按目标固浮比拆成
       base_salary（固定）+ variable_salary（浮动），**总额不变**（base + variable
       严格等于原月薪，不做任何加薪/降薪），回写到会话 DataFrame。
    2. **激励曲线（诊断）**：按目标固浮比画出「达成率 → 年度总收入」曲线，
       并给出每个岗位序列的「方法论前提与风险」说明，供 HR 判断是否该调高/调低浮动。

目标固浮比来源（优先级）
--------------------------------------------------------------------------------
1. 入参 target_mix：{岗位序列: "40:60"} 显式覆盖；
2. 默认值 schemas.JOB_FAMILY_PAY_MIX（行业参考：
   销售 40:60 / 技术 70:30 / 管理 60:40 / 操作 80:20 / 职能 75:25）；
3. 员工岗位序列 job_family：优先用数据里的 job_family 列，
   缺失时按部门用 schemas.infer_job_family 推断（兜底为「职能」）。

⚠️ 必含段落：方法论前提与风险
--------------------------------------------------------------------------------
PRD 硬性要求报告里必须出现「固浮比方法论前提与风险」一段。本模块在 meta 里
写入固定金句（PRD 原文）：

    "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；
     长周期协作型业务强行高浮动会破坏协作。"

并据此对「长周期协作型序列（技术/职能/管理/操作）强行高浮动」给出风险提示。

安全纪律
--------------------------------------------------------------------------------
- 拆分**不改总额**，base+variable == monthly_salary（variable 用差值法保证精确守恒，
  避免 base/variable 各自四舍五入造成总额漂移）。
- @tool_guard 装饰；会话访问走 loader.require_session。
- meta 只写聚合摘要（按家族汇总 + 曲线 + 方法论段），不写逐人明细（受 50 行上限约束）。
- 逐人拆分结果回写 DataFrame 的 base_salary / variable_salary 两列（write_back=True）。
"""

from __future__ import annotations

import re
from .timefmt import now_str as _now
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .errors import (
    CompToolError,
    InvalidParameter,
    UpstreamMissing,
    ok_result,
    tool_guard,
)
from .loader import require_session
from .schemas import (
    DEFAULT_PAY_MIX,
    JOB_FAMILY_PAY_MIX,
    infer_job_family,
)
from .session import get_store
from ._summary import build_paymix_summary_md

# PRD 硬性金句（必须原样出现在报告与 meta 的方法论段落）
PAYMIX_PRINCIPLE = (
    "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；"
    "长周期协作型业务强行高浮动会破坏协作。"
)
# 哪些序列属于「长周期协作型」——强行高浮动需特别提示风险
_COLLABORATIVE_FAMILIES = ("技术", "职能", "管理", "操作")
# 激励曲线的达成率采样点（0=未达标只拿固定，1.0=100%达成，1.5=超额150%）
_ACHIEVEMENT_POINTS = [0.0, 0.5, 1.0, 1.5]


def _parse_mix(text: Any) -> Optional[Tuple[int, int]]:
    """把 "40:60" / "70:30" 这类字符串解析成 (固定%, 浮动%) 整数元组。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    m = re.match(r"^(\d+)\s*[:：/]\s*(\d+)$", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a + b > 0:
            return (a, b)
    # 单数字（如 70 或 0.7）：当作固定占比
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 1.0:
        a = int(round(v * 100))
    else:
        a = int(round(v))
    b = 100 - a
    return (a, b)


def _resolve_target_mix(job_family: str, target_mix: Optional[Dict[str, str]]
                        ) -> Tuple[int, int]:
    """按优先级解析某序列的目标固浮比 (固定%, 浮动%)。"""
    if target_mix and job_family in target_mix:
        parsed = _parse_mix(target_mix[job_family])
        if parsed:
            return parsed
    if job_family in JOB_FAMILY_PAY_MIX:
        return JOB_FAMILY_PAY_MIX[job_family]
    return DEFAULT_PAY_MIX


# =============================================================================
# 主工具
# =============================================================================

@tool_guard
def simulate_pay_mix(session_id: str,
                     target_mix: Optional[Dict[str, str]] = None,
                     write_back: bool = True) -> Dict[str, Any]:
    """
    按目标固浮比拆分月薪为「固定 + 浮动」，并产出激励曲线与「方法论前提与风险」说明。

    参数
    ----------
    session_id : str
        load_salary_data 返回的会话 ID（映射必须已确认）。
    target_mix : dict, optional
        {岗位序列: "固定:浮动"} 覆盖默认固浮比，如 {"销售": "50:50"}。
        缺省用 schemas.JOB_FAMILY_PAY_MIX。
    write_back : bool, default True
        是否把 base_salary / variable_salary 两列回写到会话 DataFrame。

    返回
    -------
    {
      "ok": True,
      "session_id",
      "target_mix_source": "JOB_FAMILY_PAY_MIX" | "override",
      "by_family": [ {job_family, target_mix, current_mix, base_pct, variable_pct,
                      headcount, monthly_total, annual_total, fixed_annual,
                      variable_annual, income_at_0/100/150, precondition_ok, note}, ... ],
      "curves": { 序列: {"achievement":[...], "total_income":[...]} },
      "methodology_premise_risks": "...(含 PRD 金句)",
      "principle": "...(PRD 金句)",
      "hint": "..."
    }
    """
    session = require_session(session_id, need_mapping=True)
    df = session.df
    if "monthly_salary" not in df.columns:
        raise UpstreamMissing(
            "数据中缺少 monthly_salary，无法拆分固浮比",
            hint="请先调用 confirm_mapping 把月薪列映射到标准字段 monthly_salary。",
        )

    salary = pd.to_numeric(df["monthly_salary"], errors="coerce").fillna(0.0)

    # 解析每个员工的岗位序列（优先 job_family 列，否则按部门推断）
    if "job_family" in df.columns:
        fam = df["job_family"].map(
            lambda v: v if (v is not None and str(v).strip()) else None
        )
    else:
        fam = pd.Series(None, index=df.index, dtype="object")

    def _family_of(i: int) -> str:
        fv = fam.iloc[i]
        if fv is not None and str(fv).strip():
            return str(fv).strip()
        dept = df["dept"].iloc[i] if "dept" in df.columns else None
        lvl = df["level"].iloc[i] if "level" in df.columns else None
        return infer_job_family(dept, lvl)

    families = [_family_of(i) for i in range(len(df))]
    fam_series = pd.Series(families, index=df.index)

    # 解析员工现有固浮比（用于 current_mix 对照；缺失则 None）
    current_mix_series = None
    if "pay_mix" in df.columns:
        current_mix_series = df["pay_mix"].map(_parse_mix)

    # ---- 逐人拆分（base 四舍五入，variable = 月薪 - base，保证总额精确守恒）----
    base_list, var_list = [], []
    for i in range(len(df)):
        fam_i = families[i]
        base_pct, var_pct = _resolve_target_mix(fam_i, target_mix)
        sal_i = float(salary.iloc[i])
        base_i = round(sal_i * base_pct / 100.0, 2)
        var_i = round(sal_i - base_i, 2)  # 差值法：base + variable == 原月薪
        base_list.append(base_i)
        var_list.append(var_i)

    base_arr = pd.Series(base_list, index=df.index)
    var_arr = pd.Series(var_list, index=df.index)

    # ---- 回写 DataFrame --------------------------------------------------------
    if write_back:
        merged = df.copy()
        merged["base_salary"] = base_arr.round(2)
        merged["variable_salary"] = var_arr.round(2)
        try:
            get_store().save_df(session_id, merged)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "COMP_ERROR",
                    "message": f"固浮比拆分结果回写会话失败：{exc}",
                    "hint": "请确认会话可写；拆分计算本身已完成。",
                },
            }

    # ---- 按家族聚合 ------------------------------------------------------------
    by_family: List[Dict[str, Any]] = []
    curves: Dict[str, Dict[str, Any]] = {}
    for fam_i in fam_series.dropna().unique():
        fam_i = str(fam_i)
        mask = (fam_series == fam_i)
        n = int(mask.sum())
        if n == 0:
            continue
        base_pct, var_pct = _resolve_target_mix(fam_i, target_mix)
        fam_base = float(base_arr[mask].sum())
        fam_var = float(var_arr[mask].sum())
        fam_sal = float(salary[mask].sum())
        monthly_total = fam_sal
        annual_total = fam_sal * 12
        fixed_annual = fam_base * 12
        variable_annual = fam_var * 12

        # 家族平均固浮比驱动激励曲线
        avg_base = fam_base / n if n else 0.0
        avg_var = fam_var / n if n else 0.0
        income_curve = [
            round((avg_base + avg_var * a) * 12, 2) for a in _ACHIEVEMENT_POINTS
        ]
        curves[fam_i] = {
            "achievement": _ACHIEVEMENT_POINTS,
            "total_income": income_curve,
        }

        # 现有固浮比（取该家族出现最多的一种；仅作对照）
        current_mix_str = None
        if current_mix_series is not None:
            vals = current_mix_series[mask].dropna()
            if len(vals):
                from collections import Counter
                top = Counter(vals).most_common(1)[0][0]
                current_mix_str = f"{top[0]}:{top[1]}"

        # 方法论前提与风险判定
        is_collab = fam_i in _COLLABORATIVE_FAMILIES
        precondition_ok = True
        note = ""
        if is_collab and var_pct >= 40:
            precondition_ok = False
            note = (f"{fam_i}属长周期协作型序列，目标浮动比例已达 {var_pct}%，"
                    "需确认业绩可量化、可归因到个人、结算周期短；否则强行高浮动会破坏协作。")
        elif var_pct >= 50:
            note = (f"{fam_i}浮动比例较高（{var_pct}%），适合业绩可直接归因到个人的场景；"
                    "若业绩难以个人量化，应下调浮动。")
        else:
            note = f"{fam_i}浮动比例 {var_pct}%，属稳健结构，适合产出难量化的序列。"

        by_family.append({
            "job_family": fam_i,
            "target_mix": f"{base_pct}:{var_pct}",
            "current_mix": current_mix_str,
            "base_pct": base_pct,
            "variable_pct": var_pct,
            "headcount": n,
            "monthly_total": round(monthly_total, 2),
            "annual_total": round(annual_total, 2),
            "fixed_annual": round(fixed_annual, 2),
            "variable_annual": round(variable_annual, 2),
            "income_at_0": income_curve[0],
            "income_at_100": income_curve[2],
            "income_at_150": income_curve[3],
            "precondition_ok": precondition_ok,
            "note": note,
        })

    # 按可变比例从高到低排，便于阅读
    by_family.sort(key=lambda x: -x["variable_pct"])

    # ---- 方法论前提与风险（必含段落）------------------------------------------
    n_collab_risk = sum(1 for x in by_family
                        if not x["precondition_ok"])
    methodology = (
        PAYMIX_PRINCIPLE + "\n\n"
        "本项目固浮比设计取行业参考值：销售 40:60（业绩强归因、结算周期短，适合高浮动）；"
        "技术 70:30 / 职能 75:25 / 管理 60:40 / 操作 80:20（产出偏长周期或难量化，浮动宜低）。\n"
        f"本次拆分中，有 {n_collab_risk} 个长周期协作型序列的目标浮动比例偏高，"
        "已在上表逐序列出风险提示，落地前需业务侧确认激励条件是否成立。"
    )

    target_source = "override" if target_mix else "JOB_FAMILY_PAY_MIX"

    if write_back:
        get_store().set_meta(session_id, {
            "paymix": {
                "generated_at": _now(),
                "target_mix_source": target_source,
                "by_family": by_family,
                "curves": curves,
                "methodology_premise_risks": methodology,
                "principle": PAYMIX_PRINCIPLE,
            }
        })

    hint = (
        "固浮比已拆分并回写 base_salary / variable_salary（总额不变）。"
        "请重点核对「方法论前提与风险」段落：长周期协作型序列若浮动比例偏高，"
        "需确认业绩可量化、可归因到个人、结算周期短，否则应下调浮动。"
    )

    return ok_result(
        session_id=session_id,
        target_mix_source=target_source,
        by_family=by_family,
        curves=curves,
        methodology_premise_risks=methodology,
        principle=PAYMIX_PRINCIPLE,
        # P7 修复（2026-09-04 续）：语义化摘要由代码生成，LLM 只做转述。
        summary_md=build_paymix_summary_md({
            "principle": PAYMIX_PRINCIPLE,
            "by_family": by_family,
            "methodology_premise_risks": methodology,
        }),
        hint=hint,
    )

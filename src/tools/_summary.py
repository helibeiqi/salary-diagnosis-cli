# -*- coding: utf-8 -*-
"""
_summary.py — 各工具「语义化摘要」单一真理源
================================================================================

所有工具的 ``summary_md`` 都在此集中生成。LLM（narration / 转述）只读这些文本，
**只负责把人话讲出来，不负责任何数值计算**。这是 P1 修复（2026-09-04 实测坐实）
的核心纪律：裸字段 JSON 让本地模型在 3/3 抽样中把"补足成本"说成"节省"、并擅自
跨字段加总；把聚合与方向判定收回代码层后，错误清零。

设计四原则（任何改动都须保留）：
  1. 聚合在代码里算完（分组合计、占比、方向），模型只引用，不让它自己加；
  2. 每个金额配方向词：支出增加 / 新增投入 / 是成本支出，不是节省；
  3. 显式写"禁止加总/换算/推断"——实测有效（裸 JSON 自造加总 3/3 → 语义化 0/3）；
  4. 表格 + 结论行双通道：表格给原始数据，结论行给可直接引用的完整句子。

各 builder 的输入即对应工具 ok_result 返回的 ``data`` 子集，字段名与 PRD 契约一致。
"""

from typing import Any, Dict, List, Optional

# -----------------------------------------------------------------------------
# 共享 helper
# -----------------------------------------------------------------------------
# 职级分组（与 diagnose 红绿圈分组合计一致）
_LEVEL_GROUPS = [
    ("P1–P3", ("P1", "P2", "P3")),
    ("P4–P6", ("P4", "P5", "P6")),
    ("M1–M3", ("M1", "M2", "M3")),
]
# 成本字段 → (中文标签, 方向词)
_COST_FIELDS_DIAGNOSE = [
    ("red_overflow_annual", "红圈超出带宽上限造成的成本溢出", "**支出增加**，公司多付的钱"),
    ("green_to_min_annual", "绿圈补足到带宽下限", "**是成本支出，不是节省**"),
    ("green_to_mid_annual", "绿圈补足到带宽中位值", "**是成本支出，不是节省**"),
]


def _agg(bucket: Dict[str, Any], keys) -> int:
    return int(sum(bucket.get(k, 0) or 0 for k in keys))


def _fmt_money(x: Any) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_num(x: Any) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_pct(x: Any, nd: int = 2) -> str:
    """x 为小数比例（0.096777 → 9.68%）。None/异常安全。"""
    try:
        return f"{float(x) * 100:.{nd}f}%"
    except (TypeError, ValueError):
        return str(x)


# -----------------------------------------------------------------------------
# diagnose.py：薪酬现状诊断
# -----------------------------------------------------------------------------
def build_summary_md(data: Dict[str, Any]) -> str:
    """由确定性代码生成语义化摘要 —— LLM 只负责把它讲成人话。

    参数即 ``analyze_current_state`` 返回的 data 字典
    （cr_summary / red_green / cost / by_level）。
    详见 ``salary_cli_llm_assessment_part3_ab.md``：裸字段 JSON 让本地模型在 3/3 抽样中
    把成本方向说反、并擅自跨字段加总；本函数把聚合与方向判定收回代码层后，错误清零。
    """
    cr: Dict[str, Any] = data.get("cr_summary") or {}
    rg: Dict[str, Any] = data.get("red_green") or {}
    cost: Dict[str, Any] = data.get("cost") or {}
    by_level: Dict[str, Any] = data.get("by_level") or {}

    counts: Dict[str, Any] = rg.get("counts") or {}
    ratios: Dict[str, Any] = rg.get("ratios") or {}
    total = int(sum(v for v in counts.values() if isinstance(v, (int, float))) or 0)

    L: list = ["### 薪酬现状诊断（由计算引擎生成，以下均为真实计算结果）", ""]
    L.append(f"- 样本：{total} 人。")
    if cr:
        L.append(
            f"- CR（月薪 ÷ 带宽中位值）：均值 {cr.get('mean', 0):.4f}，"
            f"中位 {cr.get('median', 0):.4f}，标准差 {cr.get('std', 0):.4f}；"
            f"最小 {cr.get('min', 0):.4f} / P25 {cr.get('p25', 0):.4f} / "
            f"P75 {cr.get('p75', 0):.4f} / 最大 {cr.get('max', 0):.4f}。"
        )
    if counts:
        seg = "、".join(f"{k} {v} 人（{ratios.get(k, 0) * 100:.1f}%）"
                        for k, v in counts.items())
        L.append(f"- 圈层分布：{seg}。")

    if by_level:
        levels = sorted({k for b in by_level.values() for k in b})
        L += ["", "**红绿圈分职级人数（已由代码汇总，请直接引用，不要自行重新加总）：**",
              "| 圈层 | " + " | ".join(levels) + " |",
              "|---|" + "---|" * len(levels)]
        for bucket in ("红圈", "绿圈", "合理"):
            if bucket in by_level:
                L.append(f"| {bucket} | "
                         + " | ".join(str(by_level[bucket].get(k, 0)) for k in levels) + " |")
        L.append("")
        L.append("**分组合计（代码已算好，直接引用）：**")
        for bucket in ("红圈", "绿圈"):
            if bucket not in by_level:
                continue
            parts = [f"{gname} {_agg(by_level[bucket], keys)} 人"
                     for gname, keys in _LEVEL_GROUPS]
            L.append(f"- {bucket}：" + "，".join(parts) + "。")
        L.append("")
        for bucket in ("红圈", "绿圈"):
            if bucket not in by_level:
                continue
            vals = [(gname, _agg(by_level[bucket], keys)) for gname, keys in _LEVEL_GROUPS]
            ranked = sorted(vals, key=lambda x: -x[1])
            if ranked and ranked[0][1] > 0:
                top, top_n = ranked[0]
                rest = "，".join(f"{g} {n} 人" for g, n in ranked[1:] if n >= 0)
                L.append(
                    f"- {bucket}集中度：**最多的是 {top}（{top_n} 人）**"
                    + (f"，其余为 {rest}" if rest else "")
                    + "。**请直接引用本行结论，不要自行比较大小。**"
                )

    if cost:
        L += ["", "**成本（年化，方向已标明，请严格按方向表述）：**"]
        for field, label, direction in _COST_FIELDS_DIAGNOSE:
            if field in cost:
                L.append(f"- {label}：{_fmt_money(cost[field])} 元/年（{direction}）。")
        if cost.get("annual_months"):
            L.append(f"- （年化口径：{cost['annual_months']} 个月）")

    L += ["",
          "**禁止事项：** 不要对上述金额做任何加总或换算；不要增减或修改任何数字；"
          "表格里没有的分组不要自行合并；本摘要未给出的结论不要推断。"]
    return "\n".join(L)


# -----------------------------------------------------------------------------
# market.py：市场对标
# -----------------------------------------------------------------------------
def build_market_summary_md(data: Dict[str, Any]) -> str:
    """data = market_benchmark ok_result 的 data 子集。

    字段：strategy_used / source / overall_gap{mean,median} / gap_table[...] /
    by_family[...] / cost{monthly,annual,pct_of_payroll,headcount,only_below}。
    """
    strategy: Optional[str] = data.get("strategy_used")
    overall: Dict[str, Any] = data.get("overall_gap") or {}
    by_family: List[Dict[str, Any]] = data.get("by_family") or []
    gap_table: List[Dict[str, Any]] = data.get("gap_table") or []
    cost: Dict[str, Any] = data.get("cost") or {}
    source: str = data.get("source", "")

    median_gap = overall.get("median")
    mean_gap = overall.get("mean")

    L: list = ["### 市场对标（由计算引擎生成，以下均为真实计算结果）", ""]
    if strategy:
        L.append(f"- 采用对标策略：**{strategy}**（数据源：{source}）。")

    if median_gap is not None:
        if median_gap < 0:
            direction = "低于市场（外部竞争力不足、流失风险）"
        elif median_gap > 0:
            direction = "高于市场（成本高、内部公平性风险）"
        else:
            direction = "与市场持平"
        L.append(
            f"- 整体差距：公司中位月薪相对市场目标中位 **{_fmt_pct(median_gap)}**"
            f"（{direction}）；个人均值口径 {_fmt_pct(mean_gap)}。"
        )

    if cost:
        annual = cost.get("annual")
        monthly = cost.get("monthly")
        hc = cost.get("headcount")
        pct = cost.get("pct_of_payroll")
        only_below = cost.get("only_below")
        L.append("")
        L.append("**达标成本（年化，方向已标明）：**")
        L.append(
            f"- 补齐到市场目标值需**新增投入** {_fmt_money(annual)} 元/年"
            f"（{_fmt_money(monthly)} 元/月），涉及 {_fmt_num(hc)} 人，"
            f"约占工资总额 {_fmt_pct(pct)}。"
        )
        scope = ("仅计算低于市场的补齐部分（max(0, 目标-月薪)）"
                 if only_below else "按双向对齐 |目标-月薪| 计算")
        L.append(f"  - 这是**成本支出，不是节省**；{scope}。")

    if by_family:
        L += ["",
              "**逐序列差距（median_gap_pct，负值=低于市场；直接引用，不要自行排序或合并）：**",
              "| 序列 | 人数 | 公司中位(元) | 市场目标中位(元) | 差距 |",
              "|---|---|---|---|---|"]
        for fam in by_family:
            L.append(
                f"| {fam.get('job_family')} | {fam.get('n')} | "
                f"{_fmt_money(fam.get('company_median'))} | "
                f"{_fmt_money(fam.get('market_target_median'))} | "
                f"{_fmt_pct(fam.get('median_gap_pct'))} |"
            )

    if gap_table:
        L += ["",
              "**逐职级差距（直接引用）：**",
              "| 职级 | 人数 | 公司中位(元) | 市场目标中位(元) | 中位差距 |",
              "|---|---|---|---|---|"]
        for row in gap_table:
            L.append(
                f"| {row.get('level')} | {row.get('n')} | "
                f"{_fmt_money(row.get('company_median'))} | "
                f"{_fmt_money(row.get('market_target_median'))} | "
                f"{_fmt_pct(row.get('median_gap_pct'))} |"
            )

    L += ["",
          "**禁止事项：** 不要对上述金额做任何加总或换算；不要自行排序/合并序列或职级；"
          "本摘要未给出的结论不要推断。"]
    return "\n".join(L)


# -----------------------------------------------------------------------------
# increase.py：调薪模拟
# -----------------------------------------------------------------------------
def build_increase_summary_md(data: Dict[str, Any]) -> str:
    """data = simulate_increase ok_result 的 data 子集。

    字段：strategy / budget_rate / budget_amount / base_total_annual / total_cost /
    usage_rate / step1_cost / step1_headcount / unused_budget。
    """
    strategy: Optional[str] = data.get("strategy")
    budget_amount = data.get("budget_amount")
    budget_rate = data.get("budget_rate")
    total_cost = data.get("total_cost")
    usage_rate = data.get("usage_rate")
    base_total = data.get("base_total_annual")
    step1_cost = data.get("step1_cost")
    step1_hc = data.get("step1_headcount")
    unused = data.get("unused_budget")

    L: list = ["### 调薪模拟（由计算引擎生成，以下均为真实计算结果）", ""]
    if strategy:
        L.append(f"- 采用策略：**{strategy}**；预算比例（占年度现金基数）{_fmt_pct(budget_rate)}。")
    if budget_amount is not None and base_total is not None:
        L.append(
            f"- 预算总额：{_fmt_money(budget_amount)} 元/年"
            f"（基于年度现金基数 {_fmt_money(base_total)} 元）。"
        )
    if total_cost is not None:
        L.append(
            f"- 实际调薪总额：{_fmt_money(total_cost)} 元/年"
            f"（**是新增投入，不是节省**）；预算使用率 {_fmt_pct(usage_rate)}。"
        )
    if step1_cost is not None:
        L.append(
            f"- 第①步补绿圈成本：{_fmt_money(step1_cost)} 元/年，覆盖 {_fmt_num(step1_hc)} 人"
            f"（**成本支出**）；该步已计入总预算，不要与总额再加总。"
        )
    if unused is not None:
        L.append(f"- 封顶未使用预算：{_fmt_money(unused)} 元（已收回，不计入调薪总额）。")

    L += ["",
          "**禁止事项：** 不要对预算/调薪额做任何加总或换算；"
          "step1 成本已含在 total_cost 内，请勿重复相加；"
          "本摘要未给出的结论不要推断。"]
    return "\n".join(L)


# -----------------------------------------------------------------------------
# paymix.py：固浮比拆分
# -----------------------------------------------------------------------------
def build_paymix_summary_md(data: Dict[str, Any]) -> str:
    """data = simulate_pay_mix ok_result 的 data 子集。

    字段：principle / by_family[...] / methodology_premise_risks。
    by_family 每项含 job_family/target_mix/headcount/fixed_annual/variable_annual/
    precondition_ok/note。
    """
    by_family: List[Dict[str, Any]] = data.get("by_family") or []
    principle: str = data.get("principle", "")
    methodology: str = data.get("methodology_premise_risks", "")

    L: list = ["### 固浮比拆分（由计算引擎生成，以下均为真实计算结果）", ""]
    if principle:
        L.append(f"- 设计原则：{principle}")
    if by_family:
        L += ["",
              "**逐序列目标固浮比（base+variable 之和恒等于原月薪；直接引用，不要跨序列加总）：**",
              "| 序列 | 目标固浮 | 人数 | 年化固定(元) | 年化浮动(元) |",
              "|---|---|---|---|---|"]
        for fam in by_family:
            L.append(
                f"| {fam.get('job_family')} | {fam.get('target_mix')} | "
                f"{fam.get('headcount')} | {_fmt_money(fam.get('fixed_annual'))} | "
                f"{_fmt_money(fam.get('variable_annual'))} |"
            )
        risk = [f for f in by_family if not f.get("precondition_ok")]
        if risk:
            L.append("")
            L.append(f"**风险提示：{len(risk)} 个序列需业务侧确认激励条件是否成立：**")
            for f in risk:
                L.append(f"- {f.get('job_family')}：{f.get('note')}")
    if methodology:
        L += ["", "**方法论前提与风险：**", methodology]

    L += ["",
          "**禁止事项：** 各序列固定/浮动年化是拆分结果，base+variable 之和恒等于原月薪；"
          "不要跨序列加总得出全公司总额（除非代码另行给出）；"
          "本摘要未给出的结论不要推断。"]
    return "\n".join(L)


# -----------------------------------------------------------------------------
# report.py：报告生成
# -----------------------------------------------------------------------------
def build_report_summary_md(data: Dict[str, Any]) -> str:
    """data = generate_report ok_result 的 data 子集。

    字段：title / sections(list[str]) / figure_count / char_count /
    missing_sections / broken_figures。
    """
    title: Optional[str] = data.get("title")
    figure_count = data.get("figure_count")
    char_count = data.get("char_count")
    sections: List[Any] = data.get("sections") or []
    missing = data.get("missing_sections") or []
    broken = data.get("broken_figures") or []

    L: list = ["### 报告生成（由计算引擎生成）", ""]
    if title:
        L.append(f"- 报告标题：**{title}**。")
    L.append(
        f"- 章节数：{len(sections)}；图表：{_fmt_num(figure_count)} 张；"
        f"正文字数：{_fmt_num(char_count)} 字。"
    )
    if sections:
        names = "、".join(str(s) for s in sections)
        L.append(f"- 已生成章节：{names}。")
    if missing:
        L.append(f"- 缺失章节：{', '.join(missing)}（数据源不具备对应分析）。")
    if broken:
        L.append(f"- ⚠️ 断链图表：{', '.join(broken)}。")

    L += ["",
          "**说明：** 本报告是前述各模块结论的汇总呈现。各模块真实数值已在 "
          "market_benchmark / simulate_increase / simulate_pay_mix 的 summary_md 中给出，"
          "转述时直接引用那些数值，**不要重新计算或加总**。",
          "**禁止事项：** 不要凭空补充报告未含的数字；缺失章节不要臆造结论。"]
    return "\n".join(L)

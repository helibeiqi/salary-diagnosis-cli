# -*- coding: utf-8 -*-
"""
band.py — 薪酬带宽（Salary Band / Salary Range）引擎
================================================================================

什么是薪酬带宽
--------------------------------------------------------------------------------
薪酬带宽是**每一个职级对应的一段薪资区间 [下限, 上限]**，中位值（Midpoint）居中。
它是 3P 薪酬体系里「岗位（Position）」这一 P 的落地载体：
岗位价值决定职级 → 职级决定带宽 → 个人在带宽里的位置由能力（Person）与绩效（Performance）决定。

带宽的三个要素（缺一不可）：
    ┌─────────────────────────────────────────────────────────┐
    │  下限 (Min)      中位值 (Mid)        上限 (Max)          │
    │    ├──────────────────┬──────────────────┤              │
    │    │   带宽幅度 Spread = (Max - Min) / Min              │
    └─────────────────────────────────────────────────────────┘

核心公式（本项目全部口径的基石）
--------------------------------------------------------------------------------
**1. 带宽幅度（Range Spread）**
    Spread = (上限 - 下限) / 下限
    业务含义：一个职级内「最高能拿」比「最低能拿」高出多少。
    幅度越大，说明该职级内容纳的能力/绩效差异越大（高层管理幅度 50%+，
    基层操作 20%-30%，因为基层工作标准化、成长空间有限）。
    出处：WorldatWork《Salary Range Structure Design》、美世 IPE 落地实践。

**2. 由中位值与幅度反解上下限（本项目采用的标准做法）**
    下限 = 中位值 / (1 + 幅度 / 2)
    上限 = 下限 × (1 + 幅度)
    为什么用这个形式而不是 Mid ± X%？
    因为**行业惯例是先定中位值（对标市场分位），再定幅度（定政策宽度）**。
    以中位值为锚反解，能保证中位值 100% 落在设计位置 —— 中位值是薪酬策略的
    "定价锚"，不能因为取整或公式变形而漂移。
    可以验证：(下限 + 上限) / 2
             = [Mid/(1+s/2) + Mid·(1+s)/(1+s/2)] / 2
             = Mid·(2+s) / (2+s) = Mid   ✓ 严格成立

**3. 上限的等价写法（本模块实际用的形式）**
    上限 = 2 × 中位值 - 下限
    它与 `上限 = 下限 × (1 + 幅度)` **在数学上完全等价**（见上面第 2 条的推导），
    但有一个巨大的工程好处：**当上下限被取整到百元时，用这种写法能保证
    (下限+上限)/2 == 中位值 精确成立**，取整误差只影响幅度（≤1%），
    而中位值作为定价锚一点不漂。
    若用 `上限 = 下限×(1+幅度)` 再各自取整，中位值会漂几十元，
    在月薪 8000 的职级上就是 0.5% 的定价误差 —— 不该出现。

**4. 中位值级差（Midpoint Differential）**
    级差_n = 中位值_n / 中位值_(n-1) - 1
    业务含义：晋升一级，中位值该涨多少。
    经验值：基层 10%-15%，专业/中层 15%-25%，高层 25%-40%。
    级差太小 → 晋升"不值钱"，留不住人；级差太大 → 晋升成本过高、职级通胀。

**5. 带宽重叠度（Range Overlap）—— 本模块给两个口径**
    相邻两个职级 P（低）与 Q（高）的带宽重叠：
        重叠金额 = 上限_P - 下限_Q      （为负说明两个带宽之间有"断档"）

    口径 A `overlap_lower`（**业界最常用**）：
        重叠度 = 重叠金额 / (上限_P - 下限_P)
        分母是**低职级自己的带宽跨度**。含义："高职级的起薪，落在低职级
        带宽的哪一个位置" —— 直观回答"我低职级干到顶，会不会还不如高职级新人"。

    口径 B `overlap_span`（**更保守**）：
        重叠度 = 重叠金额 / (上限_Q - 下限_P)
        分母是**两个职级合起来的总跨度**。分母更大 → 数值更小 → 更保守。

    业务含义与合理区间：
        **重叠度低（<20%）** → 晋升才涨薪。晋升的激励性最强，
            但员工横向发展/轮岗困难，且容易出现"高职级新人工资倒挂低职级老人"。
        **重叠度高（>60%）** → 职级间几乎可横向平移。员工不必挤晋升独木桥，
            但晋升的薪资吸引力被稀释，还容易出现职级虚高。
        **经验合理区间 30%~50%**（技术/专业序列常取 40% 左右，
            操作序列可更低，管理序列可更高）。

**6. CR（Compa-Ratio，比较比率）**
    CR = 个人月薪 / 该职级带宽中位值
    1.00 = 正好在中位值；**CR > 1.20 红圈**（薪酬偏高、成本溢出）；
    **CR < 0.80 绿圈**（薪酬偏低、流失风险）。阈值取自 schemas.RED_CIRCLE_CR / GREEN_CIRCLE_CR。

**7. 渗透率（Range Penetration / Compa-Ratio Position）**
    渗透率 = (个人薪资 - 带宽下限) / (带宽上限 - 带宽下限)
    0% = 刚进入带宽，100% = 顶到上限。
    与 CR 的数学关系（本模块在自验里会校验）：
        渗透率 = (CR × (1 + 幅度/2) - 1) / 幅度
    即 **CR = 1.0 时渗透率恒为 50%**（因为中位值就在带宽正中间）。

对外函数
--------------------------------------------------------------------------------
    generate_band(...)              主工具：生成完整带宽表（含重叠度、CSV 导出）
    compute_band_table(...)         核心公式：中位值 + 幅度 → 上下限
    compute_overlap(...)            相邻职级重叠度（双口径）
    classify_cr(...)                红圈/绿圈判定
    summarize_cr(...)               红绿圈汇总（人数/占比/成本影响）
    compute_penetration(...)        带宽渗透率
    suggest_midpoints(...)          中位值阶梯建议（等比 / 拟合）
    compute_midpoint_differential(...) 中位值级差
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from .timefmt import now_str as _now
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from .errors import (
    ColumnMissing,
    CompToolError,
    InvalidParameter,
    UpstreamMissing,
    ok_result,
    tool_guard,
)
from .loader import require_columns, require_session
from .schemas import (
    DEFAULT_MIDPOINT_DIFF,
    GREEN_CIRCLE_CR,
    RED_CIRCLE_CR,
    get_level_tier,
    level_sort_key,
)
from .session import get_store

# =============================================================================
# 一、常量与口径
# =============================================================================

#: 项目根（用于定位 data/ 输出目录）
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
#: 带宽表 CSV 的默认输出目录
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

#: 允许的带宽设计模式
#   new      = 全新设计：完全按「锚点 + 固定级差」的阶梯生成，不迁就现状
#   optimize = 基于现状优化：以现状/市场中位值为准，阶梯仅作为偏离度诊断
BAND_MODES = ("new", "optimize")

#: 允许的中位值来源
#   current_median  现状月薪中位数（内部公平性视角，最常用）
#   market_p50      表内 mkt_p50 列（外部竞争力视角）
#   market_custom   由 midpoint_custom 传入的市场锚（外部购买的分位数据 / 手动调过的策略值）
#   explicit        直接指定最终中位值（midpoint_custom），不做平滑
MIDPOINT_SOURCES = ("market_p50", "market_custom", "current_median", "explicit")

#: 几种中位值来源对应的标准字段
_SOURCE_FIELD = {
    "market_p50": "mkt_p50",
    "current_median": "monthly_salary",
}

#: 重叠度的经验合理区间（业务经验值，用于给出文字评价）
OVERLAP_REASONABLE: Tuple[float, float] = (0.30, 0.50)


def _timestamp_compact() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _round_step(value: float, step: int) -> float:
    """
    按步长四舍五入（step=100 即取整到百元）。

    step<=0 表示不取整，原样返回（用于需要精确验证公式的场景）。
    """
    if not step or step <= 0:
        return float(value)
    return float(round(float(value) / step) * step)


# =============================================================================
# 二、带宽幅度解析
# =============================================================================


def resolve_spreads(levels: Sequence[str],
                    spread: Union[str, float, Dict[str, float]] = "auto"
                    ) -> Dict[str, float]:
    """
    把 `spread` 参数的三种入参形态统一解析成 {职级: 幅度}。

    三种形态（覆盖真实使用中的全部需求）：
        1. "auto"                     → **按职级分层**取 schemas.get_level_tier() 的默认值。
                                        这是默认行为，也是专业做法：
                                        基层 25%、专业/技术 35%~38%、中层 45%~48%、高层 55%~60%。
                                        用全局单一幅度会让基层带宽过宽、高层带宽过窄。
        2. 标量 0.35                  → 全部职级统一幅度（用户强制指定政策宽度时用）。
        3. {"P1": 0.25, "M3": 0.60}   → 逐职级指定；**未指定的职级回退到分层默认值**，
                                        不会因漏写而报错。

    参数校验：
        - 幅度必须落在 (0, 3] —— 超过 3 倍（即上限是下限的 4 倍以上）
          在真实薪酬体系里不存在，视为录入错误。
    """
    if levels is None or len(levels) == 0:
        raise InvalidParameter(
            "职级列表为空，无法确定带宽幅度",
            hint="请先用 confirm_mapping 确认字段映射，确保数据里有职级列。",
        )

    out: Dict[str, float] = {}
    for lv in levels:
        key = str(lv)
        if isinstance(spread, str):
            if spread.lower() != "auto":
                raise InvalidParameter(
                    f"spread 字符串只接受 'auto'，收到 {spread!r}",
                    hint="spread 支持三种写法：'auto'（按职级分层）、标量 0.35（全局统一）、"
                         "字典 {'P1':0.25}（逐职级指定）。",
                    details={"spread": spread},
                )
            out[key] = float(get_level_tier(key)[1])
        elif isinstance(spread, dict):
            if key in spread:
                out[key] = float(spread[key])
            else:
                out[key] = float(get_level_tier(key)[1])
        elif isinstance(spread, (int, float)):
            out[key] = float(spread)
        else:
            raise InvalidParameter(
                f"spread 类型不支持：{type(spread).__name__}",
                hint="spread 支持：'auto' / 数字 / {职级: 幅度} 字典。",
                details={"spread": repr(spread)},
            )

        if not (0 < out[key] <= 3):
            raise InvalidParameter(
                f"职级 {key} 的带宽幅度 {out[key]} 不在合理区间 (0, 3]",
                hint="带宽幅度 = (上限-下限)/下限。常见取值：基层 0.20~0.30、"
                     "专业/技术 0.30~0.40、中层 0.40~0.50、高层 0.50~0.60。",
                details={"level": key, "spread": out[key]},
            )
    return out


# =============================================================================
# 三、中位值阶梯建议
# =============================================================================


def suggest_midpoints(levels: Sequence[str],
                      midpoint_diff: Optional[float] = None,
                      anchor_level: Optional[str] = None,
                      anchor_value: Optional[float] = None,
                      fit_targets: Optional[Dict[str, float]] = None,
                      weights: Optional[Dict[str, float]] = None,
                      round_to: int = 100) -> Dict[str, float]:
    """
    生成「等比中位值阶梯」，即一套**内部一致**的职级定价体系。

    两种驱动方式（二选一，anchor_value 优先）：

    **A. 锚点驱动**（给了 anchor_value）
        中位值_lv = anchor_value × (1 + 级差)^(序号_lv - 序号_anchor)
        适用：HR 手里有一个确定的市场锚（如"P3 市场中位值 14,500"），
        其余职级按政策级差外推。

    **B. 目标拟合**（给了 fit_targets，不给 anchor_value）
        1. 若给了 midpoint_diff：**固定斜率**，只解截距，
           使「人数加权的阶梯中位值合计 == 人数加权的现状中位值合计」
           —— 即**薪酬包总额不变**（成本中性），这是"基于现状优化"的关键约束，
              保证新带宽不会凭空增加或减少公司总薪酬成本。
        2. 若没给 midpoint_diff：对 (序号, log(中位值)) 做**最小二乘拟合**，
           斜率和截距都从数据里来 —— 用来诊断"实际级差是多少"。

    参数
    ----------
    levels : list[str]
        按**从低到高**排好的职级序列（调用方需先排序）。
    midpoint_diff : float, optional
        中位值级差，如 0.15 表示每升一级中位值 +15%。
    anchor_level / anchor_value : 锚点职级与其月薪中位值。
    fit_targets : dict, optional
        {职级: 目标中位值}（现状中位数或市场 P50）。
    weights : dict, optional
        {职级: 权重}（通常传人数），用于成本中性拟合。
    round_to : int, default 100
        取整步长（取整到百元）。

    返回
    -------
    {职级: 中位值}
    """
    if levels is None or len(levels) == 0:
        raise InvalidParameter("职级列表为空，无法生成中位值阶梯")
    levels = [str(lv) for lv in levels]

    diff = DEFAULT_MIDPOINT_DIFF if midpoint_diff is None else float(midpoint_diff)
    if not (0 < diff <= 2):
        raise InvalidParameter(
            f"中位值级差 {diff} 不在合理区间 (0, 2]",
            hint="级差 = 相邻职级中位值增幅。经验值：基层 10%-15%、专业/中层 15%-25%、高层 25%-40%。",
            details={"midpoint_diff": diff},
        )

    idx = {lv: i for i, lv in enumerate(levels)}

    # ---- A. 锚点驱动 ---------------------------------------------------------
    if anchor_value is not None:
        anchor_value = float(anchor_value)
        if anchor_value <= 0:
            raise InvalidParameter(
                f"锚点中位值必须为正，收到 {anchor_value}",
                details={"anchor_value": anchor_value},
            )
        if anchor_level and anchor_level in idx:
            base_i = idx[anchor_level]
        else:
            base_i = idx[levels[0]] if anchor_level is None else 0
        return {lv: _round_step(anchor_value * (1 + diff) ** (i - base_i), round_to)
                for lv, i in idx.items()}

    # ---- B. 目标拟合 ---------------------------------------------------------
    if not fit_targets:
        raise InvalidParameter(
            "suggest_midpoints 需要 anchor_value 或 fit_targets 之一",
            hint="给了锚点就按锚点外推；否则请提供各职级现状中位值用于拟合。",
            details={"levels": levels},
        )

    # usable 保存 (职级, 目标中位值) 对 —— 注意第一个元素是**职级名**不是序号，
    # 后面加权求和要按职级名去 weights 里取人数
    usable = [(lv, float(v)) for lv, v in fit_targets.items()
              if lv in idx and v is not None and np.isfinite(float(v)) and float(v) > 0]
    if not usable:
        raise InvalidParameter(
            "fit_targets 里没有可用于拟合的正数中位值",
            hint="请检查数据中职级的月薪/市场 P50 是否解析成功（见 coerce_report）。",
            details={"levels": levels, "fit_targets": fit_targets},
        )

    w = {lv: float((weights or {}).get(lv, 1.0)) for lv in levels}

    if midpoint_diff is None:
        # B-2：双参数最小二乘（log 空间线性拟合），级差也从数据里来
        xs = np.array([idx[lv] for lv, _ in usable], dtype="float64")
        ys = np.log(np.array([v for _, v in usable], dtype="float64"))
        if len(usable) >= 2:
            slope, intercept = np.polyfit(xs, ys, 1)
        else:
            slope, intercept = math.log(1 + DEFAULT_MIDPOINT_DIFF), float(ys[0] - xs[0])
    else:
        # B-1：斜率固定为 log(1+diff)，只解截距 → 成本中性
        #   约束：Σ w_i · base·(1+d)^i  =  Σ w_i · target_i
        #   解：  base = Σ w_i·target_i / Σ w_i·(1+d)^i
        slope = math.log(1 + diff)
        num = sum(w[lv] * float(fit_targets[lv]) for lv, _ in usable)
        den = sum(w[lv] * ((1 + diff) ** idx[lv]) for lv in levels) or 1.0
        base = num / den
        if base <= 0:
            raise CompToolError(
                "中位值阶梯拟合失败（得到非正基数）",
                hint="请检查各职级中位值是否都为正数，或换用 mode='optimize'。",
                details={"base": base, "fit_targets": fit_targets},
            )
        intercept = math.log(base)

    return {lv: _round_step(math.exp(slope * i + intercept), round_to)
            for lv, i in idx.items()}


def compute_midpoint_differential(midpoints: Dict[str, float],
                                  levels: Optional[Sequence[str]] = None
                                  ) -> Dict[str, Optional[float]]:
    """
    计算相邻职级的**中位值级差**：级差_n = 中位值_n / 中位值_(n-1) - 1。

    最低职级的级差为 None（没有更低一级可比）。
    业务含义：晋升一级能带来多少"定价上的"涨幅，是晋升激励强弱的直接度量。
    """
    keys = [str(l) for l in levels] if levels else sorted(midpoints.keys(), key=level_sort_key)
    out: Dict[str, Optional[float]] = {}
    prev: Optional[float] = None
    for lv in keys:
        cur = midpoints.get(lv)
        if cur is None or prev is None or not np.isfinite(cur) or prev <= 0:
            out[lv] = None
        else:
            out[lv] = round(float(cur) / float(prev) - 1.0, 6)
        if cur is not None and np.isfinite(cur):
            prev = float(cur)
    return out


# =============================================================================
# 四、核心公式：中位值 + 幅度 → 带宽上下限
# =============================================================================


def compute_band_table(midpoints: Dict[str, float],
                       spreads: Dict[str, float],
                       round_to: int = 100,
                       levels: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """
    由「各职级中位值 + 各职级幅度」算出带宽上下限 —— **本模块最核心的公式**。

    公式（逐条见模块顶部 docstring 的推导）：
        下限 = 中位值 / (1 + 幅度 / 2)
        上限 = 2 × 中位值 - 下限          （≡ 下限 × (1 + 幅度)，但取整后保中位值不漂）

    为什么上限用 `2×中位值 - 下限` 而不是 `下限×(1+幅度)`？
        两者在实数域上完全等价；但真实交付的带宽表必须**取整到百元**（没人发布
        14,236.78 这样的带宽），取整会破坏恒等式。用这种写法，取整误差被推给了
        幅度（影响 ≤1%，幅度本来就是政策参数、允许微调），
        而**中位值 —— 薪酬策略的定价锚 —— 保持精确**。

    返回 DataFrame（已按职级从低到高排序），列：
        level, tier, spread_target, spread_realized, spread_error,
        band_min, band_mid, band_max, midpoint_diff_vs_prev
    其中 `spread_realized = (band_max - band_min) / band_min`，
    `spread_error = spread_realized - spread_target`（取整造成，应 ≤1%）。
    """
    if not midpoints:
        raise InvalidParameter(
            "midpoints 为空，无法计算带宽",
            hint="请先确认数据中职级与月薪均已正确解析。",
        )
    keys = [str(l) for l in levels] if levels else sorted(midpoints.keys(), key=level_sort_key)

    rows: List[Dict[str, Any]] = []
    prev_mid: Optional[float] = None
    for lv in keys:
        mid_raw = float(midpoints[lv])
        spread = float(spreads.get(lv, get_level_tier(lv)[1]))
        if not np.isfinite(mid_raw) or mid_raw <= 0:
            raise InvalidParameter(
                f"职级 {lv} 的中位值无效：{mid_raw}",
                hint="中位值必须为正数。请检查该职级的月薪是否已正确解析。",
                details={"level": lv, "midpoint": mid_raw},
            )

        # ---- 核心公式 ---------------------------------------------------------
        # 中位值是定价锚：先取整，后续一切以它为准
        band_mid = _round_step(mid_raw, round_to)
        # 下限 = 中位值 / (1 + 幅度/2)
        band_min = _round_step(mid_raw / (1.0 + spread / 2.0), round_to)
        # 上限 = 2×中位值 - 下限（等价于 下限×(1+幅度)，但保证 (min+max)/2 == mid 精确成立）
        band_max = 2.0 * band_mid - band_min

        # 实占幅度（取整后与政策幅度的偏差，用于自检）
        spread_realized = (band_max - band_min) / band_min if band_min > 0 else float("nan")

        rows.append({
            "level": lv,
            "tier": get_level_tier(lv)[0],
            "spread_target": round(spread, 6),
            "spread_realized": round(float(spread_realized), 6),
            "spread_error": round(float(spread_realized) - spread, 6),
            "band_min": float(band_min),
            "band_mid": float(band_mid),
            "band_max": float(band_max),
            "midpoint_diff_vs_prev": (
                round(band_mid / prev_mid - 1.0, 6)
                if (prev_mid and prev_mid > 0) else None
            ),
        })
        prev_mid = band_mid

    return pd.DataFrame(rows)


# =============================================================================
# 五、重叠度（双口径）
# =============================================================================


def compute_overlap(band_table: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    计算**相邻职级**的带宽重叠度，同时给出两个口径。

    口径 A `overlap_lower`（业界最常用）
        重叠度 = (上限_低 - 下限_高) / (上限_低 - 下限_低)
        分母 = 低职级自身的带宽跨度。
        回答的问题："高职级的起薪点，落在低职级带宽的什么位置？"

    口径 B `overlap_span`（更保守）
        重叠度 = (上限_低 - 下限_高) / (上限_高 - 下限_低)
        分母 = 两个职级合起来的总跨度（更大）→ 数值更小 → 更保守。

    两者都为负 → 说明两个带宽之间**存在断档**：
        低职级干到顶也够不着高职级的起薪，会造成"晋升即大幅涨薪"的成本压力，
        也说明职级设置可能过密。

    返回
    -------
    list[dict]，每项：
        {
          "pair": "P1->P2",
          "lower_level" / "upper_level",
          "overlap_amount": 重叠金额（元/月，负数=断档金额）,
          "overlap_lower": 口径A（0~1）,
          "overlap_span": 口径B（0~1）,
          "gap": 断档金额（>0 表示有断档）,
          "assessment": "重叠偏低/合理/偏高/断档"
        }
    """
    if band_table is None or len(band_table) < 2:
        return []

    need = {"level", "band_min", "band_mid", "band_max"}
    missing = need - set(band_table.columns)
    if missing:
        raise ColumnMissing(
            f"band_table 缺少列：{sorted(missing)}",
            hint="请传入 compute_band_table() 的返回值。",
            details={"missing": sorted(missing)},
        )

    df = band_table.reset_index(drop=True)
    out: List[Dict[str, Any]] = []
    lo, hi = OVERLAP_REASONABLE

    for i in range(len(df) - 1):
        low, high = df.loc[i], df.loc[i + 1]
        min_low, max_low = float(low["band_min"]), float(low["band_max"])
        min_high, max_high = float(high["band_min"]), float(high["band_max"])

        # 重叠金额：低职级上限 高出 高职级下限 的部分
        amount = max_low - min_high
        span_low = max_low - min_low                  # 口径A 分母：低职级跨度
        span_total = max_high - min_low               # 口径B 分母：两职级总跨度
        ratio_a = amount / span_low if span_low > 0 else float("nan")
        ratio_b = amount / span_total if span_total > 0 else float("nan")

        if amount < 0:
            assess = "断档（无重叠，晋升将带来跳涨）"
        elif ratio_a < lo:
            assess = "重叠偏低（晋升激励强，横向发展困难）"
        elif ratio_a > hi:
            assess = "重叠偏高（晋升涨薪空间被稀释）"
        else:
            assess = "合理区间"

        out.append({
            "pair": f"{low['level']}->{high['level']}",
            "lower_level": str(low["level"]),
            "upper_level": str(high["level"]),
            "overlap_amount": round(amount, 2),
            "overlap_lower": round(float(ratio_a), 6),
            "overlap_span": round(float(ratio_b), 6),
            "gap": round(-amount, 2) if amount < 0 else 0.0,
            "assessment": assess,
        })
    return out


# =============================================================================
# 六、CR / 渗透率 / 红绿圈
# =============================================================================


def compute_penetration(salary: pd.Series, band_min: pd.Series,
                        band_max: pd.Series) -> pd.Series:
    """
    带宽渗透率（Range Penetration）= (个人薪资 - 带宽下限) / (带宽上限 - 带宽下限)。

    业务含义：个人薪资在这个带宽里「走了多远」。
        0%   = 刚够到带宽下限（新人/刚晋升）
        50%  = 正好在中位值（**等价于 CR = 1.0**）
        100% = 顶到上限（再涨就得晋升了）
    与 CR 的关系（自验会校验，最大偏差 < 1e-6）：
        渗透率 = (CR × (1 + 幅度/2) - 1) / 幅度
    注意：这里的「幅度」必须用**实占幅度 spread_realized**（取整后的真实
        (上限-下限)/下限），而不是政策幅度 spread_target —— 两者因取整有 ≤1% 差异，
        用政策幅度会让渗透率算出来偏几个百分点。
    """
    span = band_max - band_min
    # 带宽跨度为 0 时避免除零（理论上不会发生，幅度校验已保证 > 0）
    span = span.replace(0, np.nan)
    return (salary - band_min) / span


def classify_cr(df: pd.DataFrame,
                band: Optional[Union[pd.DataFrame, Dict[str, Dict[str, float]]]] = None,
                level_field: str = "level",
                salary_field: str = "monthly_salary",
                red_cr: float = RED_CIRCLE_CR,
                green_cr: float = GREEN_CIRCLE_CR,
                use_bounds: bool = True) -> pd.DataFrame:
    """
    计算 CR、渗透率并判定红圈/绿圈 —— 薪酬诊断最基础的个体判定。

    判定规则（PRD 模块 1 的口径）
    --------------------------------------------------------------------------
        CR = 个人月薪 / 该职级带宽中位值

        红圈（Red Circle）：CR > 1.20 **或** 薪资 > 带宽上限
            业务含义：薪酬高于该职级应有的水平，成本溢出。
            典型成因：历史高薪引进、长期未调薪的老员工被晋升稀释、稀缺人才溢价。
            处理思路：**冻结或给一次性补贴**（不要继续涨），不占用调薪池。

        绿圈（Green Circle）：CR < 0.80 **或** 薪资 < 带宽下限
            业务含义：薪酬低于该职级应有的水平，**流失风险最高的一类人**。
            典型成因：快速晋升未同步调薪、入职时低于市场、历史低起点。
            处理思路：**优先补足**（向带宽下限靠拢），这是调薪预算的第一优先级。

        合理： 0.80 ≤ CR ≤ 1.20 且薪资在带宽内。

    参数
    ----------
    df : pd.DataFrame
        至少含 level_field 与 salary_field 两列。
    band : DataFrame 或 dict, optional
        带宽表。为 None 时要求 df 自带 band_min / band_mid / band_max 三列。
        - DataFrame：compute_band_table() 的返回值，按 level 关联
        - dict：{职级: {"band_min":..,"band_mid":..,"band_max":..}}
    use_bounds : bool, default True
        True  = 红绿圈同时看 CR 阈值**与**带宽上下限（PRD 口径，更贴近实操）
        False = 只看 CR 阈值（用于做纯粹的 CR 分布统计/对账）

    返回
    -------
    df 的副本，新增列：band_min, band_mid, band_max, cr, penetration, flag, flag_reason
    """
    if not isinstance(df, pd.DataFrame):
        raise InvalidParameter(
            f"classify_cr 需要 DataFrame，收到 {type(df).__name__}",
            details={"type": type(df).__name__},
        )
    require_columns(df, [level_field, salary_field], tool_name="classify_cr")
    if not (0 < green_cr <= red_cr):
        raise InvalidParameter(
            f"红绿圈阈值非法：green_cr={green_cr}, red_cr={red_cr}",
            hint="应满足 0 < 绿圈阈值 ≤ 红圈阈值（默认 0.80 / 1.20）。",
            details={"green_cr": green_cr, "red_cr": red_cr},
        )

    out = df.copy()
    out[level_field] = out[level_field].astype(str).str.strip().str.upper()

    # 未识别职级（Plan C·A）：无法计算 Compa-Ratio，标记后排除出 CR/红绿圈诊断，
    # 但保留在人数/成本基数中（由报告透明单列「未识别」）。
    is_unknown = out[level_field].astype(str).str.upper().eq("UNKNOWN")
    n_unknown = int(is_unknown.sum())

    # ---- 关联带宽 ------------------------------------------------------------
    if band is None:
        require_columns(out, ["band_min", "band_mid", "band_max"], tool_name="classify_cr")
    else:
        if isinstance(band, pd.DataFrame):
            bdf = band.set_index("level")[["band_min", "band_mid", "band_max"]]
            bmap = bdf.to_dict(orient="index")
        elif isinstance(band, dict):
            bmap = {str(k): v for k, v in band.items()}
        else:
            raise InvalidParameter(
                f"band 只接受 DataFrame 或 dict，收到 {type(band).__name__}",
                details={"type": type(band).__name__},
            )
        out["band_min"] = out[level_field].map(
            lambda lv: float(bmap[lv]["band_min"]) if lv in bmap else np.nan)
        out["band_mid"] = out[level_field].map(
            lambda lv: float(bmap[lv]["band_mid"]) if lv in bmap else np.nan)
        out["band_max"] = out[level_field].map(
            lambda lv: float(bmap[lv]["band_max"]) if lv in bmap else np.nan)
        n_unmatched = int((out["band_mid"].isna() & ~is_unknown).sum())
        if n_unmatched:
            raise UpstreamMissing(
                f"有 {n_unmatched} 人的职级在带宽表里找不到对应行，无法计算 CR",
                hint="请用 generate_band 为数据中出现的**全部**职级生成带宽。",
                details={"levels_in_data": sorted(out[level_field].unique().tolist()),
                         "levels_in_band": sorted(bmap.keys())},
            )

    # ---- CR：Compa-Ratio = 个人月薪 / 带宽中位值 ------------------------------
    # 保留 10 位小数：既抹掉浮点噪声（0.9999999999999999 之类），
    # 又不至于像 round(6) 那样破坏「渗透率 = (CR(1+s/2)-1)/s」这一恒等式的精度
    mid = pd.to_numeric(out["band_mid"], errors="coerce")
    salary = pd.to_numeric(out[salary_field], errors="coerce")
    out["cr"] = (salary / mid).round(10)

    # ---- 渗透率 = (薪资 - 下限) / (上限 - 下限) --------------------------------
    out["penetration"] = compute_penetration(
        salary,
        pd.to_numeric(out["band_min"], errors="coerce"),
        pd.to_numeric(out["band_max"], errors="coerce"),
    ).round(10)

    # ---- 红绿圈判定 -----------------------------------------------------------
    above_max = salary > pd.to_numeric(out["band_max"], errors="coerce")
    below_min = salary < pd.to_numeric(out["band_min"], errors="coerce")
    is_red = (out["cr"] > red_cr) | (above_max if use_bounds else False)
    is_green = (out["cr"] < green_cr) | (below_min if use_bounds else False)

    out["flag"] = "合理"
    out.loc[is_red.fillna(False), "flag"] = "红圈"
    # 绿圈后写：红绿条件在数据上互斥（不可能同时 >1.2 又 <0.8），
    # 后写只是为了让 use_bounds=False 时的边界语义稳定（>1.2 优先判红）
    out.loc[(~is_red.fillna(False)) & is_green.fillna(False), "flag"] = "绿圈"

    # 判定依据（写进报告，让 HR 看得见"为什么"）
    reason = pd.Series("", index=out.index, dtype="object")
    reason[is_red.fillna(False) & (out["cr"] > red_cr)] = f"CR>{red_cr}"
    reason[is_red.fillna(False) & above_max.fillna(False)] += " 薪资超上限"
    reason[is_green.fillna(False) & (out["cr"] < green_cr)] = f"CR<{green_cr}"
    reason[is_green.fillna(False) & below_min.fillna(False)] += " 薪资低于下限"
    out["flag_reason"] = reason.replace("", "在合理区间内")

    # ---- UNKNOWN 职级：不参与 CR / 红绿圈诊断（Plan C·A）----------------------
    if n_unknown:
        out.loc[is_unknown, ["band_min", "band_mid", "band_max"]] = np.nan
        out.loc[is_unknown, "cr"] = np.nan
        out.loc[is_unknown, "penetration"] = np.nan
        out.loc[is_unknown, "flag"] = "未识别"
        out.loc[is_unknown, "flag_reason"] = "职级未识别(UNKNOWN)，不参与带宽/CR诊断"

    return out


def summarize_cr(df: pd.DataFrame, monthly_salary_field: str = "monthly_salary",
                 annual_months: int = 12) -> Dict[str, Any]:
    """
    红绿圈汇总：人数、占比、涉及薪资成本、以及「补齐/溢出」的成本测算。

    三个关键成本数字（报告里最常被老板追问的部分）：
        - 红圈溢出成本 = Σ max(0, 月薪 - 带宽上限) × 12
          含义：如果严格按带宽封顶，理论上能省下多少 —— 但实际通常**不降薪**，
          所以这笔钱应理解为"历史成本包袱"，处理方式是冻结而非追回。
        - 绿圈补足成本 = Σ max(0, 带宽下限 - 月薪) × 12
          含义：把所有绿圈员工拉到带宽下限**至少**需要多少钱。
          这是调薪预算的**刚性下限**，必须先满足它再谈绩效分配。
        - 绿圈补到中位值成本 = Σ max(0, 带宽中位值 - 月薪) × 12
          含义：把绿圈员工拉到"该职级应有的水平"，成本明显更高，
          通常分 2~3 年逐步到位。
    """
    if "flag" not in df.columns or "cr" not in df.columns:
        raise UpstreamMissing(
            "传入的 DataFrame 缺少 flag / cr 列",
            hint="请先调用 classify_cr() 完成红绿圈判定。",
            details={"columns": [str(c) for c in df.columns]},
        )
    total = int(len(df))
    if total == 0:
        return {"total": 0, "counts": {}, "ratios": {}, "cost": {}}

    salary = pd.to_numeric(df[monthly_salary_field], errors="coerce")
    bmin = pd.to_numeric(df.get("band_min"), errors="coerce")
    bmid = pd.to_numeric(df.get("band_mid"), errors="coerce")
    bmax = pd.to_numeric(df.get("band_max"), errors="coerce")

    counts = df["flag"].value_counts().to_dict()
    ratios = {k: round(v / total, 6) for k, v in counts.items()}

    overflow = float((salary - bmax).clip(lower=0).sum()) if bmax.notna().any() else 0.0
    to_min = float((bmin - salary).clip(lower=0).sum()) if bmin.notna().any() else 0.0
    to_mid = float((bmid - salary).clip(lower=0).sum()) if bmid.notna().any() else 0.0

    by_level: Dict[str, Any] = {}
    if "level" in df.columns:
        ct = pd.crosstab(df["level"], df["flag"])
        for flag in ("红圈", "绿圈", "合理"):
            if flag in ct.columns:
                by_level[flag] = {str(k): int(v) for k, v in ct[flag].items()}

    return {
        "total": total,
        "counts": {k: int(v) for k, v in counts.items()},
        "ratios": ratios,
        "cr_stats": {
            "mean": round(float(df["cr"].mean()), 6),
            "std": round(float(df["cr"].std()), 6),
            "min": round(float(df["cr"].min()), 6),
            "p25": round(float(df["cr"].quantile(0.25)), 6),
            "median": round(float(df["cr"].median()), 6),
            "p75": round(float(df["cr"].quantile(0.75)), 6),
            "max": round(float(df["cr"].max()), 6),
        },
        "cost": {
            "annual_months": annual_months,
            "red_overflow_monthly": round(overflow, 2),
            "red_overflow_annual": round(overflow * annual_months, 2),
            "green_to_min_monthly": round(to_min, 2),
            "green_to_min_annual": round(to_min * annual_months, 2),
            "green_to_mid_monthly": round(to_mid, 2),
            "green_to_mid_annual": round(to_mid * annual_months, 2),
        },
        "by_level": by_level,
    }


# =============================================================================
# 七、主工具：generate_band
# =============================================================================


def _level_stats(df: pd.DataFrame, salary_field: str = "monthly_salary") -> Dict[str, Dict[str, float]]:
    """按职级统计现状月薪分布（count/min/p25/median/mean/p75/max）。"""
    stats: Dict[str, Dict[str, float]] = {}
    if "level" not in df.columns:
        return stats
    for lv, g in df.groupby("level"):
        s = pd.to_numeric(g[salary_field], errors="coerce").dropna()
        if len(s) == 0:
            continue
        stats[str(lv)] = {
            "n": int(len(s)),
            "min": float(s.min()),
            "p25": float(s.quantile(0.25)),
            "median": float(s.median()),
            "mean": float(s.mean()),
            "p75": float(s.quantile(0.75)),
            "max": float(s.max()),
        }
    return stats


def _resolve_base_midpoints(df: pd.DataFrame, levels: Sequence[str],
                            midpoint_source: str,
                            midpoint_custom: Optional[Dict[str, float]],
                            default_diff: float) -> Tuple[Dict[str, float], List[str]]:
    """
    按 midpoint_source 解析「原始中位值」，返回 (中位值 dict, 回退说明 list)。

    处理优先级（保证任何情况下都有值，不因缺列而崩）：
        explicit        → 直接用 midpoint_custom（缺的职级回退现状中位数）
        market_custom   → 用 midpoint_custom 作为**市场锚**，缺的职级回退市场 P50 → 现状中位数
        market_p50      → 用表内 mkt_p50 列；缺的职级回退现状中位数
        current_median  → 用表内 monthly_salary 中位数
    """
    notes: List[str] = []
    current = {lv: v["median"] for lv, v in _level_stats(df).items()}
    market: Dict[str, float] = {}
    if "mkt_p50" in df.columns and "level" in df.columns:
        market = {str(lv): float(g["mkt_p50"].dropna().median())
                  for lv, g in df.groupby("level")
                  if g["mkt_p50"].notna().any()}

    custom = {str(k): float(v) for k, v in (midpoint_custom or {}).items()
              if v is not None and np.isfinite(float(v))}

    if midpoint_source == "explicit":
        if not custom:
            raise InvalidParameter(
                "midpoint_source='explicit' 时必须通过 midpoint_custom 给出各职级中位值",
                hint="形如 {'P1': 8000, 'P2': 10500, ...}；未给出的职级会回退到现状中位数。",
                details={"levels": list(levels)},
            )
        base = {lv: custom.get(lv, current.get(lv, float("nan"))) for lv in levels}
        missing = [lv for lv in levels if lv not in custom]
        if missing:
            notes.append(f"explicit 模式下 {missing} 未指定中位值，已回退为现状中位数。")
        return base, notes

    if midpoint_source == "market_custom":
        if not custom:
            raise InvalidParameter(
                "midpoint_source='market_custom' 时必须通过 midpoint_custom 给出市场锚",
                hint="可以只给一个职级作为锚点（配合 midpoint_diff 外推），或给全部职级。",
                details={"levels": list(levels)},
            )
        base = {}
        for lv in levels:
            if lv in custom:
                base[lv] = custom[lv]
            elif lv in market:
                base[lv] = market[lv]
                notes.append(f"{lv} 未在 midpoint_custom 中给出，已回退为市场 P50。")
            else:
                base[lv] = current.get(lv, float("nan"))
                notes.append(f"{lv} 既无自定义值也无市场 P50，已回退为现状中位数。")
        return base, notes

    if midpoint_source == "market_p50":
        if not market:
            raise ColumnMissing(
                "数据中没有可用的市场 P50（mkt_p50）列",
                hint="请改用 midpoint_source='current_median'，"
                     "或用 'market_custom' 手动传入市场中位值。",
                details={"levels": list(levels),
                         "columns": [str(c) for c in df.columns]},
            )
        base = {}
        for lv in levels:
            if lv in market:
                base[lv] = market[lv]
            else:
                base[lv] = current.get(lv, float("nan"))
                notes.append(f"{lv} 缺少市场 P50，已回退为现状中位数。")
        return base, notes

    if midpoint_source == "current_median":
        if not current:
            raise ColumnMissing(
                "无法按职级计算现状月薪中位数",
                hint="请确认 level 与 monthly_salary 已映射且数值解析成功。",
                details={"levels": list(levels)},
            )
        return {lv: current.get(lv, float("nan")) for lv in levels}, notes

    # 理论上到不了这里（generate_band 入口已校验）
    raise InvalidParameter(
        f"不支持的 midpoint_source：{midpoint_source}",
        hint=f"可选值：{list(MIDPOINT_SOURCES)}",
        details={"midpoint_source": midpoint_source},
    )


@tool_guard
def generate_band(session_id: str,
                  mode: str = "optimize",
                  levels: Optional[List[str]] = None,
                  midpoint_source: str = "current_median",
                  midpoint_custom: Optional[Dict[str, float]] = None,
                  midpoint_diff: Optional[float] = None,
                  spread: Union[str, float, Dict[str, float]] = "auto",
                  round_to: int = 100,
                  anchor_level: Optional[str] = None,
                  export: bool = True,
                  output_dir: Optional[str] = None,
                  write_back: bool = True) -> Dict[str, Any]:
    """
    生成薪酬带宽表（含重叠度诊断），导出 CSV，并回写到会话供下游使用。

    参数
    ----------
    session_id : str
        load_salary_data 返回的会话 ID（**映射必须已确认**）。
    mode : {"optimize", "new"}, default "optimize"
        optimize = 基于现状优化：中位值取现状/市场值，阶梯只作**偏离度诊断**
                   （哪些职级偏离了平滑梯度 → 套改风险点）。
        new      = 全新设计：中位值直接取「锚点 + 固定级差」的等比阶梯，
                   并按**人数加权做成本中性拟合**（总薪酬包不变），
                   适用于从零搭体系或做大规模套改。
    levels : list[str], optional
        参与设计的职级序列；留空则用数据中出现的全部职级（自动按 P1<...<M3 排序）。
    midpoint_source : {"current_median","market_p50","market_custom","explicit"}
        current_median  现状月薪中位数（**内部公平性视角**，默认）
        market_p50      表内 mkt_p50（**外部竞争力视角**）
        market_custom   midpoint_custom 传入的市场锚（外部购买数据/策略调整值）
        explicit        midpoint_custom 直接作为最终中位值
    midpoint_custom : dict, optional
        {职级: 中位值}。market_custom / explicit 模式下必填。
    midpoint_diff : float, optional
        中位值级差（如 0.15 = 每级 +15%）。**new 模式的核心参数**；
        optimize 模式下只用于生成对照阶梯。缺省取 schemas.DEFAULT_MIDPOINT_DIFF(0.15)。
    spread : "auto" | float | dict, default "auto"
        "auto"  = **按职级分层**取 schemas.get_level_tier()（基层 25% → 高层 60%）
        0.35    = 全局统一幅度
        {"P1":0.25,"M3":0.60} = 逐职级指定，未指定的回退分层默认值
    round_to : int, default 100
        取整步长（100 = 取整到百元）。传 0 表示不取整（用于精确验证公式）。
    anchor_level : str, optional
        market_custom 模式下的锚点职级；留空则用最低职级。
    export : bool, default True
        是否导出 CSV 到 data/band_{timestamp}.csv。
    output_dir : str, optional
        CSV 输出目录；缺省用项目根下的 data/。
    write_back : bool, default True
        是否把带宽三列回写到会话 DataFrame（**下游 analyze_current_state 依赖它**）。

    返回
    -------
    {
      "ok": True,
      "mode", "midpoint_source", "midpoint_diff", "round_to",
      "levels": [...],
      "band_table": [ {...}, ... ],       每行含现状统计 + 带宽 + 级差 + 重叠度 + 覆盖率
      "overlaps": [ {...}, ... ],         相邻职级重叠度（双口径）
      "spreads": {职级: 幅度},
      "checks": {                         自检（供 QA 与报告引用）
          "mid_identity_max_error": 0.0,         (min+max)/2 与 mid 的最大相对误差
          "midpoints_increasing": True,          相邻职级中位值是否严格递增
          "spread_max_abs_error": ...,           实占幅度与政策幅度的最大偏差
          "penetration_at_cr1": 0.5              CR=1.0 对应的渗透率（应恒为 0.5）
      },
      "output_csv": "C:/.../data/band_20260830_120000.csv",
      "notes": [...], "warnings": [...], "hint": "..."
    }
    """
    # ---- 0) 入参校验 ----------------------------------------------------------
    if mode not in BAND_MODES:
        raise InvalidParameter(
            f"mode 只接受 {list(BAND_MODES)}，收到 {mode!r}",
            hint="new = 全新设计（按级差阶梯）；optimize = 基于现状优化（尊重现状中位数）。",
            details={"mode": mode},
        )
    if midpoint_source not in MIDPOINT_SOURCES:
        raise InvalidParameter(
            f"midpoint_source 只接受 {list(MIDPOINT_SOURCES)}，收到 {midpoint_source!r}",
            details={"midpoint_source": midpoint_source},
        )
    if not isinstance(round_to, int) or round_to < 0:
        raise InvalidParameter(
            f"round_to 必须是非负整数（100=取整到百元，0=不取整），收到 {round_to!r}",
            details={"round_to": round_to},
        )

    # ---- 1) 取会话（映射必须已确认）------------------------------------------
    session = require_session(session_id, need_mapping=True)
    df = session.df
    require_columns(df, ["level", "monthly_salary"], tool_name="generate_band")

    # ---- 2) 确定职级序列（按 P1<P2<...<M1<M2<M3 排序）-------------------------
    # 未识别职级（UNKNOWN）不纳入带宽设计：它没有可解析的带宽，且会被 classify_cr
    # 排除出 CR 诊断；其人数/成本仍计入基数，并在报告中单列「未识别」。
    n_unknown = int((df["level"].astype(str).str.strip().str.upper() == "UNKNOWN").sum())
    if levels:
        lv_list = [str(x).strip().upper() for x in levels if str(x).strip().upper() != "UNKNOWN"]
        unknown = [lv for lv in lv_list if lv not in set(df["level"].astype(str))]
        if unknown:
            raise InvalidParameter(
                f"指定的职级在数据里不存在：{unknown}",
                hint=f"数据中出现的职级：{sorted(df['level'].astype(str).unique())}",
                details={"unknown_levels": unknown},
            )
    else:
        lv_list = [lv for lv in sorted(
            df["level"].astype(str).str.strip().str.upper().unique(),
            key=level_sort_key) if lv != "UNKNOWN"]
    if not lv_list:
        raise CompToolError(
            "数据中没有任何可识别职级，无法生成带宽",
            hint="所有职级均为 UNKNOWN（职级未识别）。请补全 level 列或 job_title 列后重跑。",
        )

    diff = DEFAULT_MIDPOINT_DIFF if midpoint_diff is None else float(midpoint_diff)
    if not (0 < diff <= 2):
        raise InvalidParameter(
            f"midpoint_diff={diff} 不在合理区间 (0, 2]",
            hint="经验值：基层 10%-15%、专业/中层 15%-25%、高层 25%-40%。",
            details={"midpoint_diff": diff},
        )

    # ---- 3) 解析原始中位值 ----------------------------------------------------
    base_mids, notes = _resolve_base_midpoints(df, lv_list, midpoint_source,
                                               midpoint_custom, diff)
    if n_unknown:
        notes.append(
            f"有 {n_unknown} 人职级为 UNKNOWN（无法识别），未纳入带宽设计（不参与重叠度诊断）；"
            "其 CR 在 analyze_current_state 中单列「未识别」，但已计入人数与成本基数。"
        )
    stats = _level_stats(df)
    weights = {lv: float(stats.get(lv, {}).get("n", 1)) for lv in lv_list}

    # 缺失中位值的职级：用相邻职级插值补齐，否则带宽表会出现空洞
    for lv in lv_list:
        v = base_mids.get(lv)
        if v is None or not np.isfinite(float(v)) or float(v) <= 0:
            filled = _interpolate_missing(lv, lv_list, base_mids)
            if filled is None:
                raise CompToolError(
                    f"职级 {lv} 的中位值无法解析，且无相邻职级可用于插值",
                    hint="请检查该职级的月薪/市场 P50 是否可解析（见 coerce_report）。",
                    details={"level": lv},
                )
            base_mids[lv] = filled
            notes.append(f"{lv} 的中位值缺失，已用相邻职级插值补齐为 {round(filled)}。")

    # ---- 4) 生成最终中位值 ----------------------------------------------------
    #  对照阶梯：始终计算，用于诊断"现状是否偏离平滑梯度"
    if midpoint_source in ("market_custom", "explicit") and midpoint_custom:
        anchor_val = midpoint_custom.get(anchor_level or lv_list[0])
        ladder = suggest_midpoints(lv_list, midpoint_diff=diff,
                                   anchor_level=anchor_level or lv_list[0],
                                   anchor_value=anchor_val if anchor_val is not None else None,
                                   fit_targets=base_mids, weights=weights,
                                   round_to=round_to)
    else:
        ladder = suggest_midpoints(lv_list, midpoint_diff=diff,
                                   fit_targets=base_mids, weights=weights,
                                   round_to=round_to)

    if mode == "new":
        # 全新设计：直接用阶梯作为最终中位值
        final_mids = dict(ladder)
        notes.append(
            f"new 模式：中位值完全按「级差 {diff:.0%}」的等比阶梯生成，"
            f"并做了人数加权的成本中性拟合（总薪酬包口径不变）。"
        )
    else:
        # 基于现状优化：以解析值为准，阶梯仅作对照
        if midpoint_source == "explicit":
            final_mids = {lv: _round_step(float(base_mids[lv]), round_to) for lv in lv_list}
            notes.append("explicit 模式：中位值按入参原样采用，未做阶梯平滑。")
        else:
            final_mids = {lv: _round_step(float(base_mids[lv]), round_to) for lv in lv_list}
            notes.append(
                "optimize 模式：中位值取" +
                ("现状月薪中位数" if midpoint_source == "current_median" else "市场 P50") +
                f"，同时给出「级差 {diff:.0%}」的平滑阶梯作为偏离度对照。"
            )

    # ---- 5) 幅度 + 带宽表 -----------------------------------------------------
    spreads = resolve_spreads(lv_list, spread)
    band = compute_band_table(final_mids, spreads, round_to=round_to, levels=lv_list)

    # 挂上现状统计、阶梯对照与偏离度
    cur_min = band["level"].map(lambda lv: stats.get(lv, {}).get("min"))
    cur_p25 = band["level"].map(lambda lv: stats.get(lv, {}).get("p25"))
    cur_med = band["level"].map(lambda lv: stats.get(lv, {}).get("median"))
    cur_mean = band["level"].map(lambda lv: stats.get(lv, {}).get("mean"))
    cur_p75 = band["level"].map(lambda lv: stats.get(lv, {}).get("p75"))
    cur_max = band["level"].map(lambda lv: stats.get(lv, {}).get("max"))
    cnt = band["level"].map(lambda lv: int(stats.get(lv, {}).get("n", 0)))

    band.insert(2, "n", cnt)
    band["current_min"] = cur_min
    band["current_p25"] = cur_p25
    band["current_median"] = cur_med
    band["current_mean"] = cur_mean
    band["current_p75"] = cur_p75
    band["current_max"] = cur_max
    band["ladder_mid_reference"] = band["level"].map(lambda lv: ladder.get(lv))
    band["deviation_vs_ladder"] = (
        (band["band_mid"] - band["ladder_mid_reference"]) / band["ladder_mid_reference"]
    ).round(6)

    # 现状覆盖率：该职级有多少人的月薪落在**新带宽**内（衡量套改冲击面）
    cov = []
    for lv in band["level"]:
        g = pd.to_numeric(df.loc[df["level"].astype(str) == str(lv), "monthly_salary"],
                          errors="coerce").dropna()
        row = band.loc[band["level"] == lv].iloc[0]
        if len(g) == 0:
            cov.append(None)
        else:
            inside = ((g >= float(row["band_min"])) & (g <= float(row["band_max"]))).sum()
            cov.append(round(float(inside) / len(g), 6))
    band["coverage_rate"] = cov

    # ---- 6) 重叠度 -------------------------------------------------------------
    overlaps = compute_overlap(band)
    ov_by_level: Dict[str, Dict[str, Any]] = {}
    for ov in overlaps:
        ov_by_level[ov["upper_level"]] = {
            "vs_prev_pair": ov["pair"],
            "overlap_amount": ov["overlap_amount"],
            "overlap_lower": ov["overlap_lower"],
            "overlap_span": ov["overlap_span"],
            "gap": ov["gap"],
            "assessment": ov["assessment"],
        }
    band["overlap_lower_vs_prev"] = band["level"].map(
        lambda lv: ov_by_level.get(lv, {}).get("overlap_lower"))
    band["overlap_span_vs_prev"] = band["level"].map(
        lambda lv: ov_by_level.get(lv, {}).get("overlap_span"))
    band["overlap_amount_vs_prev"] = band["level"].map(
        lambda lv: ov_by_level.get(lv, {}).get("overlap_amount"))
    band["overlap_assessment_vs_prev"] = band["level"].map(
        lambda lv: ov_by_level.get(lv, {}).get("assessment"))

    # ---- 7) 自检（数值护栏）----------------------------------------------------
    #  7.1 带宽恒等式：(下限+上限)/2 == 中位值（相对误差应 < 1e-9）
    identity_err = ((band["band_min"] + band["band_max"]) / 2 - band["band_mid"]).abs()
    rel_identity = (identity_err / band["band_mid"]).max()
    #  7.2 相邻中位值严格递增
    mids = band["band_mid"].tolist()
    increasing = all(mids[i] < mids[i + 1] for i in range(len(mids) - 1))
    #  7.3 实占幅度与政策幅度的偏差（取整造成，应 ≤1%）
    spread_err = float(band["spread_error"].abs().max())
    #  7.4 CR=1.0 对应的渗透率应恒为 50%
    #      推导：pen = (CR·(1+s/2) - 1)/s，代入 CR=1 得 (s/2)/s = 0.5，与幅度无关。
    pen_at_cr1_series = ((1.0 * (1 + band["spread_target"] / 2.0)) - 1.0) / band["spread_target"]
    pen_at_cr1_max = float(pen_at_cr1_series.max())

    checks = {
        "mid_identity_max_abs_error": float(identity_err.max()),
        "mid_identity_max_rel_error": float(rel_identity),
        "midpoints_increasing": bool(increasing),
        "spread_max_abs_error": round(spread_err, 6),
        "penetration_at_cr1": pen_at_cr1_max,
        "n_levels": int(len(band)),
        "round_to": int(round_to),
    }

    warnings: List[str] = []
    if rel_identity > 1e-9:
        warnings.append(
            f"带宽恒等式 (下限+上限)/2 == 中位值 的最大相对误差为 {rel_identity:.2e}，超过 1e-9。"
        )
    if not increasing:
        bad = [f"{band.loc[i, 'level']}->{band.loc[i + 1, 'level']}"
               for i in range(len(mids) - 1) if mids[i] >= mids[i + 1]]
        warnings.append(
            f"相邻职级中位值未严格递增：{bad}。高职级中位值低于低职级会造成"
            "「晋升反而降定价」，建议改用 mode='new' 按固定级差重排。"
        )
    if spread_err > 0.02:
        warnings.append(
            f"取整导致实占幅度与政策幅度最大偏差 {spread_err:.2%}，超过 2%，建议减小 round_to。"
        )
    gaps = [o for o in overlaps if o["gap"] > 0]
    if gaps:
        warnings.append(
            "存在带宽断档：" + "、".join(f"{g['pair']}({g['gap']:.0f}元)" for g in gaps)
        )

    # ---- 8) 导出 CSV ----------------------------------------------------------
    output_csv: Optional[str] = None
    if export:
        target_dir = os.path.abspath(output_dir or DATA_DIR)
        try:
            os.makedirs(target_dir, exist_ok=True)
            output_csv = os.path.join(target_dir, f"band_{_timestamp_compact()}.csv")
            band.to_csv(output_csv, index=False, encoding="utf-8-sig")
        except Exception as exc:  # noqa: BLE001
            # 导出失败不应让整个分析失败 —— 降级为警告
            warnings.append(f"带宽表 CSV 导出失败：{exc}")
            output_csv = None

    # ---- 9) 回写会话（下游 analyze_current_state 直接依赖带宽三列）------------
    if write_back:
        try:
            merged = df.copy()
            merged["level"] = merged["level"].astype(str).str.strip().str.upper()
            # 先清掉旧的带宽列（重新生成带宽时必须覆盖，避免新旧混杂）
            for c in ("band_min", "band_mid", "band_max"):
                if c in merged.columns:
                    merged = merged.drop(columns=[c])
            merged = merged.merge(
                band[["level", "band_min", "band_mid", "band_max"]],
                on="level", how="left")
            store = get_store()
            store.save_df(session_id, merged)
            store.set_meta(session_id, {
                "band": {
                    "generated_at": _now(),
                    "mode": mode,
                    "midpoint_source": midpoint_source,
                    "midpoint_diff": diff,
                    "spreads": spreads,
                    "midpoints": {r["level"]: float(r["band_mid"])
                                  for _, r in band.iterrows()},
                    "round_to": int(round_to),
                    "output_csv": output_csv,
                    "checks": checks,
                }
            })
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"带宽回写会话失败（不影响本次结果）：{exc}")

    hint = ("带宽已生成并回写到会话。下一步调用 analyze_current_state 做现状诊断"
            "（红绿圈/渗透率/成本测算）。")
    if mode == "optimize":
        hint += "；若 deviation_vs_ladder 偏差超过 ±15%，说明该职级定价偏离平滑梯度，建议改 mode='new' 重排。"

    return ok_result(
        session_id=session_id,
        mode=mode,
        midpoint_source=midpoint_source,
        midpoint_diff=diff,
        round_to=int(round_to),
        levels=lv_list,
        spreads=spreads,
        band_table=band.where(pd.notna(band), None).to_dict(orient="records"),
        overlaps=overlaps,
        checks=checks,
        output_csv=output_csv,
        notes=notes,
        warnings=warnings,
        hint=hint,
    )


def _interpolate_missing(level: str, levels: Sequence[str],
                         mids: Dict[str, float]) -> Optional[float]:
    """
    用**相邻职级**的中位值为缺失职级插值（几何平均，保持等比结构）。

    为什么用几何平均而不是算术平均？
    薪酬阶梯是**等比**结构（每级 ×(1+级差)），等比序列中间项的几何平均
    才等于真实中间值；算术平均会系统性偏高。
    """
    if level not in levels:
        return None
    i = levels.index(level)
    prev_v = next((float(mids[levels[j]]) for j in range(i - 1, -1, -1)
                   if np.isfinite(float(mids.get(levels[j], float("nan"))))
                   and float(mids.get(levels[j], 0)) > 0), None)
    next_v = next((float(mids[levels[j]]) for j in range(i + 1, len(levels))
                   if np.isfinite(float(mids.get(levels[j], float("nan"))))
                   and float(mids.get(levels[j], 0)) > 0), None)
    if prev_v and next_v:
        return float(math.sqrt(prev_v * next_v))
    return prev_v or next_v

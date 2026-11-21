# -*- coding: utf-8 -*-
"""
jobeval.py — 岗位价值评估（Job Evaluation）与职级校验
================================================================================

岗位价值评估回答一个问题：**"这个岗位值多少钱 / 该落在哪个职级？"**
它独立于个人，只看岗位本身的职责大小，是带宽（band）与职级体系的设计依据。

支持两套模型（由 model 切换，**方法深度刻意不同**）：
    - hay    ：海氏三要素法 —— **Guide Chart 点数法**（非线性查表）：
               总分 = KH点数 + KH×PS% + ACC点数。查表刻度按海氏公开文献的
               结构原理自建校准（商业版 Guide Chart 为 Korn Ferry 专有 IP，
               本项目不使用也不复制），显式 dict 查表、逐格可审计。
    - mercer ：美世 IPE 四因素法 —— **线性近似打分卡**（如实标注）：
               正版 IPE 阶梯表为 Mercer 专有 IP，公开文献只有要素框架，
               故按 1-10 线性加权近似，只能反映相对排序。

⚠️ P0 量纲修复（2026-08-30 强制，线性链路仍适用）
--------------------------------------------------------------------------------
美世线性链路：子维度打分是 **1–10 量纲**；而岗位评估总分是
**0–1600 百分制刻度**。历史上有一个致命 bug：直接把 1–10 的加权分 W 喂给
score_to_level()，因为分段表 JOB_SCORE_LEVEL_BANDS 是 0–1600 区间，W∈[1,10]
恒落入 (0,300) → **所有岗位一律判为 P1**，模块 6 完全失效。

正确链路（mercer 线性链路严格遵守）：
    子维度(1–10) → 要素内平均 → 加权得 W(1–10)
                → schemas.weighted_to_total(W) = W × 160.0  → 总分(0–1600)
                → schemas.score_to_level(总分)              → 建议职级

即：**绝不能直接把 1–10 的 W 传给 score_to_level**。
实测校验（见自验）：W=8.875 → 1420 → M3；W=3.14 → 502.4 → P3。
海氏 Guide Chart 链路无此问题：compute_hay_total 直接输出 0–1600 总分。

⚠️ 子维度缺失告警（2026-11-21 修复历史静默缺陷）
--------------------------------------------------------------------------------
- hay    ：KH 子维度缺失按 0 点计（总分被低估）并告警；PS/ACC 缺失按
           中位档/乘数 1.0 兜底并告警。逐行告警聚合为"【N/M 行】"条目。
- mercer ：要素整列缺失 → **权重重归一化**（其余要素按权重占比重分摊）+
           告警，不再静默按 0 分参与；要素内子维度部分缺失 → 用已有列
           平均 + 告警。

数据源（优先级）
--------------------------------------------------------------------------------
1. 子维度列：若 DataFrame 含模型对应的子因素/子维度列（如「知识技能」「解决问题」），
   则走查表/加权链路（最能体现方法本身）。
2. job_score 列：否则若该列存在，**自动判定量纲**——若最大值 ≤ 10 视为 1–10 加权分
   W 需经 weighted_to_total 换算；否则视为已是 0–1600 百分制总分，直接 score_to_level。
   （data/sample_salary.csv 的 job_score 实测 187~1414，已为总分刻度。）
   ⚠️ 该降级路径只有线性量纲含义——海氏模型走此路径时 Guide Chart 结构未生效，
   结果会携带告警提示。

输出
--------------------------------------------------------------------------------
- 给每行写回 job_level（评估建议职级）。
- meta['jobeval']：模型、量纲换算说明、与现状 level 的错配统计（建议职级≠现状职级）、
  错配样例（受 meta 50 行上限，只存最显著的若干条）。
- 返回值含完整逐岗对照表（不落盘）。

安全纪律：@tool_guard；会话访问走 loader.require_session。
"""

from __future__ import annotations

from .timefmt import now_str as _now
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .errors import (
    InvalidParameter,
    UpstreamMissing,
    ok_result,
    tool_guard,
)
from .loader import require_session
from .schemas import (
    JOB_EVAL_MODELS,
    JOB_SCORE_LEVEL_BANDS,
    JOB_SCORE_SCALE,
    compute_hay_total,
    get_job_eval_anchors,
    weighted_to_total,
    score_to_level,
    normalize_col as _norm,
)
from .session import get_store

# 现状 level 与建议 level 之间的「错配」程度排序（供 meta 只存最显著的样例）
_LEVEL_RANK = {lv: i for i, lv in enumerate(
    ["P1", "P2", "P3", "P4", "P5", "P6", "M1", "M2", "M3"])}


def w_to_level(w: float, model: str = "hay") -> str:
    """
    量纲修复入口（P0 关键函数，供自验直接调用）：
    把 1–10 量纲的加权分 W 换算成百分制总分，再映射到建议职级。

        level = score_to_level(weighted_to_total(W))

    ⚠️ 绝不可写成 score_to_level(W) —— 那会恒判 P1。
    """
    total = weighted_to_total(float(w))
    return score_to_level(total)


def _subscore_columns(df: pd.DataFrame, model: str) -> Dict[str, List[str]]:
    """
    在 DataFrame 里按归一化列名匹配模型各要素的子维度列。
    返回 {要素名: [列名, ...]}；匹配不到的子维度留空（该要素用 0 处理）。
    """
    cfg = JOB_EVAL_MODELS[model]
    cols_norm = {_norm(c): c for c in df.columns}
    matched: Dict[str, List[str]] = {}
    for factor, subfactors in cfg["subfactors"].items():
        hit: List[str] = []
        for sf in subfactors:
            ns = _norm(sf)
            if ns in cols_norm:
                hit.append(cols_norm[ns])
        matched[factor] = hit
    return matched


def _evaluate_from_subscores(
        df: pd.DataFrame, model: str) -> "tuple[pd.Series, List[str]]":
    """
    子维度列 → 每行总分（0-1600 刻度）。按 model 分发：

    - hay    ：Guide Chart 点数法（schemas.compute_hay_total，非线性查表）；
               KH 子维度缺失按 0 点计、PS/ACC 缺失按中位档/乘数 1.0 兜底，
               全部逐行记入 warnings 并按条目聚合计数。
    - mercer ：线性近似打分卡；要素整列缺失 → **权重重归一化**（其余要素按
               权重占比重分摊）+ 告警，不再静默按 0 分参与（修复历史缺陷）；
               要素内子维度列部分缺失 → 仍用已有列平均 + 告警。

    返回 (总分 Series, warnings 列表)。若无任何子维度列则抛 UpstreamMissing。
    """
    cfg = JOB_EVAL_MODELS[model]
    matched = _subscore_columns(df, model)
    if not any(matched.values()):
        raise UpstreamMissing(
            f"数据中找不到模型 {model}（{cfg['label']}）的任一子维度列，无法按子维度评估",
            hint="请提供如「知识技能」「解决问题」「应负责任」等子维度打分列，"
                 "或确保数据含已算好的 job_score 列。",
        )

    n_rows = len(df)
    warnings: List[str] = []

    # ---------------- hay：Guide Chart 点数法 --------------------------------
    if model == "hay":
        # 子维度名 → 数据列名（只保留真实匹配到的）
        sf_col: Dict[str, str] = {}
        for factor, sfs in cfg["subfactors"].items():
            for sf, col in zip(sfs, matched[factor]):
                if col:
                    sf_col[sf] = col
        totals = pd.Series(np.nan, index=df.index, dtype="float64")
        warn_counter: Dict[str, int] = {}
        for i in range(n_rows):
            row_scores: Dict[str, float] = {}
            for sf, col in sf_col.items():
                v = pd.to_numeric(df[col].iloc[i], errors="coerce")
                row_scores[sf] = float(v) if pd.notna(v) else None
            res = compute_hay_total(row_scores)
            totals.iloc[i] = res["total"]
            for w in res["warnings"]:
                warn_counter[w] = warn_counter.get(w, 0) + 1
        # 行级告警聚合成"共 N 行"条目，避免 150 行 × N 条刷屏
        for w, c in sorted(warn_counter.items()):
            warnings.append(f"【{c}/{n_rows} 行】{w}")
        return totals, warnings

    # ---------------- mercer：线性近似打分卡（含权重重归一化）----------------
    weights = cfg["weights"]
    present = {f: cols for f, cols in matched.items() if cols}
    missing = [f for f in cfg["factors"] if f not in present]
    if missing:
        w_sum_present = sum(float(weights[f]) for f in present)
        if w_sum_present <= 0:
            raise UpstreamMissing(
                f"模型 mercer 的全部要素列缺失，无法评估",
                hint="请至少提供「影响」「沟通」「创新」「知识」之一的子维度列。",
            )
        warnings.append(
            f"要素 {missing} 整列缺失，权重已重归一化："
            + ", ".join(f"{f}={float(weights[f]) / w_sum_present:.2f}" for f in present)
        )
    eff_weights = {
        f: float(weights[f]) / sum(float(weights[g]) for g in present)
        for f in present
    }

    totals = pd.Series(0.0, index=df.index, dtype="float64")
    for factor, cols in present.items():
        # 要素内子维度列部分缺失 → 用已有列平均 + 告警（不静默）
        expected = len(cfg["subfactors"][factor])
        if len(cols) < expected:
            warnings.append(
                f"要素「{factor}」子维度列部分缺失（{len(cols)}/{expected}），"
                f"按已有列 {'、'.join(cols)} 的平均计入"
            )
        sub = df[cols].apply(pd.to_numeric, errors="coerce")
        factor_score = sub.mean(axis=1)  # 1-10 量纲
        totals = totals + factor_score * eff_weights[factor]
    # 线性链路：W(1-10) → weighted_to_total 升维到 0-1600
    totals = totals.map(
        lambda w: weighted_to_total(float(w)) if np.isfinite(w) else np.nan)
    return totals, warnings


# =============================================================================
# 主工具
# =============================================================================

@tool_guard
def calc_job_score(session_id: str, model: str = "hay",
                   write_back: bool = True) -> Dict[str, Any]:
    """
    计算岗位价值评估得分与建议职级，写回 job_level 列并落 meta['jobeval']。

    参数
    ----------
    session_id : str
        load_salary_data 返回的会话 ID（映射必须已确认）。
    model : {"hay", "mercer"}, default "hay"
        评估模型：海氏三要素法 / 美世 IPE 四因素法。
    write_back : bool, default True
        是否把 job_level 列回写到会话 DataFrame，并写 meta['jobeval']。

    返回
    -------
    {
      "ok": True,
      "session_id", "model", "model_label",
      "scale_note": "量纲换算说明（P0 修复链路）",
      "n_jobs", "mismatch_count", "mismatch_pct",
      "table": [ {emp_id, job_title, job_score, current_level, suggested_level, gap}, ... ],
      "hint": "..."
    }
    """
    if model not in JOB_EVAL_MODELS:
        raise InvalidParameter(
            f"model 只接受 {list(JOB_EVAL_MODELS)}，收到 {model!r}",
            hint="hay = 海氏三要素法；mercer = 美世 IPE 四因素法。",
            details={"model": model},
        )

    session = require_session(session_id, need_mapping=True)
    df = session.df
    cfg = JOB_EVAL_MODELS[model]

    # ---- 1) 取得每行的总分（0–1600 刻度）-------------------------------------
    # 优先走子维度链路；否则用 job_score 列（仅线性近似模型可用的降级路径）
    total_scores: Optional[pd.Series] = None
    source = ""
    eval_warnings: List[str] = []
    try:
        total_scores, eval_warnings = _evaluate_from_subscores(df, model)
        source = "subscore"
    except UpstreamMissing:
        if "job_score" in df.columns:
            raw = pd.to_numeric(df["job_score"], errors="coerce")
            if raw.notna().any():
                # 自动判定量纲：最大值 ≤ 10 → 视为 1–10 加权分 W，需升维
                if float(raw.max()) <= 10.0:
                    total_scores = raw.map(
                        lambda w: weighted_to_total(float(w)) if np.isfinite(w) else np.nan
                    )
                    source = "job_score(1-10->weighted_to_total)"
                else:
                    total_scores = raw  # 已是 0–1600 总分刻度
                    source = "job_score(0-1600)"
                if model == "hay":
                    eval_warnings.append(
                        "无子维度列，按已算好的 job_score 列评估——该降级路径只有"
                        "线性量纲含义，Guide Chart 点数法结构（KH+KH×PS%+ACC）未生效；"
                        "如需走海氏查表链路请提供子维度打分列。"
                    )
        if total_scores is None or total_scores.isna().all():
            raise UpstreamMissing(
                "既无模型子维度列，也无可用的 job_score 列，无法评估岗位价值",
                hint="请确认数据含 job_score（已算好的评估总分），或补上子维度打分列。",
                details={"columns": [str(c) for c in df.columns]},
            )

    # ---- 2) 总分 → 建议职级（关键：score_to_level 吃的是 0–1600 总分）--------
    suggested = total_scores.map(
        lambda s: score_to_level(float(s)) if np.isfinite(s) else None
    )

    # ---- 3) 写回 DataFrame -----------------------------------------------------
    if write_back:
        merged = df.copy()
        merged["job_level"] = suggested.values
        try:
            get_store().save_df(session_id, merged)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": {
                    "code": "COMP_ERROR",
                    "message": f"岗位评估建议职级回写会话失败：{exc}",
                    "hint": "请确认会话可写；评估计算本身已完成。",
                },
            }

    # ---- 4) 与现状 level 的错配统计（仅取最显著样例进 meta）-------------------
    emp_id = df["emp_id"] if "emp_id" in df.columns else pd.Series(
        [f"ROW_{i + 1}" for i in range(len(df))], index=df.index)
    job_title = df["job_title"] if "job_title" in df.columns else pd.Series(
        [None] * len(df), index=df.index)
    current = df["level"] if "level" in df.columns else pd.Series(
        [None] * len(df), index=df.index)

    table: List[Dict[str, Any]] = []
    for i in range(len(df)):
        ts = total_scores.iloc[i]
        sg = suggested.iloc[i]
        if sg is None or (isinstance(sg, float) and np.isnan(sg)):
            continue
        cur = current.iloc[i]
        gap = None
        if cur is not None and str(cur).strip() and str(cur).strip().upper() in _LEVEL_RANK:
            gap = _LEVEL_RANK[str(cur).strip().upper()] - _LEVEL_RANK[str(sg)]
        table.append({
            "emp_id": str(emp_id.iloc[i]),
            "job_title": (None if pd.isna(job_title.iloc[i]) else str(job_title.iloc[i])),
            "job_score": (None if pd.isna(ts) else round(float(ts), 2)),
            "current_level": (None if pd.isna(cur) else str(cur)),
            "suggested_level": str(sg),
            "gap": gap,  # >0 表示现状高于建议（可能溢价）；<0 表示现状低于建议（低估）
        })

    mismatch = [t for t in table if t["gap"] not in (0, None)]
    mismatch_count = len(mismatch)
    n_jobs = len(table)
    mismatch_pct = round(mismatch_count / n_jobs * 100, 2) if n_jobs else 0.0
    # 错配样例：只存最显著的（gap 绝对值最大），受 meta 50 行上限约束
    mismatch_sorted = sorted(mismatch, key=lambda t: abs(t["gap"] or 0), reverse=True)
    mismatch_examples = mismatch_sorted[:50]

    if model == "hay":
        scale_note = (
            "量纲说明（Guide Chart 点数法）：子维度 1–10 等级档 → 查表"
            "（KH 点数表 / PS% 表 / ACC 点数×乘数表）→ "
            "总分 = KH + KH×PS% + ACC（非线性，0–1600 刻度）"
            " → schemas.score_to_level(总分) → 建议职级。"
            " 查表刻度为按海氏公开文献结构原理自建校准（商业版 Guide Chart 为"
            " Korn Ferry 专有 IP，本项目不使用）。"
        )
    else:
        scale_note = (
            "量纲换算（线性近似打分卡）：子维度打分 1–10 量纲 → 要素内平均 → 按"
            "（缺失要素重归一化后的）权重加权得 W(1–10)"
            f" → schemas.weighted_to_total(W) = W × {JOB_SCORE_SCALE:.0f} → 总分(0–1600)"
            " → schemas.score_to_level(总分) → 建议职级。"
            " 绝不可把 1–10 的 W 直接喂给 score_to_level（会恒判 P1）。"
            " 该链路是美世 IPE 要素命名的线性近似，非正式 IPE 评估。"
        )

    if write_back:
        get_store().set_meta(session_id, {
            "jobeval": {
                "generated_at": _now(),
                "model": model,
                "model_label": cfg["label"],
                "method": cfg["method"],
                "method_note": cfg["method_note"],
                "score_source": source,
                "scale_note": scale_note,
                "warnings": eval_warnings,
                "n_jobs": n_jobs,
                "mismatch_count": mismatch_count,
                "mismatch_pct": mismatch_pct,
                "level_bands": [
                    {"lo": lo, "hi": hi, "level": lv}
                    for lo, hi, lv in JOB_SCORE_LEVEL_BANDS
                ],
                "mismatch_examples": mismatch_examples,
            }
        })

    hint = (
        f"岗位评估完成（{cfg['label']}，数据源：{source}）。"
        f"共 {n_jobs} 个岗位，其中 {mismatch_count} 个（{mismatch_pct}%）现状职级与建议职级错配。"
        "错配岗位见返回 table / meta['jobeval'].mismatch_examples，可用于校验职级体系合理性。"
        + (f" 评估告警 {len(eval_warnings)} 条，见 meta['jobeval'].warnings。" if eval_warnings else "")
    )

    return ok_result(
        session_id=session_id,
        model=model,
        model_label=cfg["label"],
        method=cfg["method"],
        method_note=cfg["method_note"],
        score_source=source,
        scale_note=scale_note,
        warnings=eval_warnings,
        n_jobs=n_jobs,
        mismatch_count=mismatch_count,
        mismatch_pct=mismatch_pct,
        table=table,
        hint=hint,
    )


# =============================================================================
# 评估指南工具（无会话依赖，供模型/打分人打分前查阅）
# =============================================================================

@tool_guard
def get_job_eval_guide(model: str = "hay") -> Dict[str, Any]:
    """
    返回指定评估模型的完整打分指南：方法说明、子维度清单、1-10 等级锚定
    描述（BARS）与（海氏）查表刻度。打分前先查本工具，分数才有可辩护依据。

    参数
    ----
    model : {"hay", "mercer"}, default "hay"

    返回
    -------
    {
      "ok": True, "model", "model_label", "method", "method_note",
      "factors": [要素名...],
      "subfactors": {要素: [子维度...]},
      "anchors": {子维度: {"1-3": 描述, "4-6": ..., "7-8": ..., "9-10": ...}},
      "hay_tables": 仅 model="hay" 时返回三张查表（KH 点数 / PS% / ACC 点数乘数），
      "hint": "..."
    }
    """
    if model not in JOB_EVAL_MODELS:
        raise InvalidParameter(
            f"model 只接受 {list(JOB_EVAL_MODELS)}，收到 {model!r}",
            hint="hay = 海氏三要素法；mercer = 美世 IPE 四因素法。",
            details={"model": model},
        )
    cfg = JOB_EVAL_MODELS[model]
    payload: Dict[str, Any] = {
        "model": model,
        "model_label": cfg["label"],
        "method": cfg["method"],
        "method_note": cfg["method_note"],
        "factors": cfg["factors"],
        "subfactors": cfg["subfactors"],
        "anchors": get_job_eval_anchors(model),
    }
    if model == "hay":
        payload["hay_tables"] = {
            "kh_point_table": "子维度等级 1-10 → 点数（三子维相加），见 schemas.HAY_KH_POINT_TABLE",
            "ps": "总分% = HAY_PS_ENV_TABLE[思维环境] + HAY_PS_CHALLENGE_TABLE[思维难度]，封顶 0.87",
            "acc": "点数 = HAY_ACC_FREEDOM_TABLE[行动自由度] × HAY_ACC_IMPACT_MULT[影响] × HAY_ACC_MAGNITUDE_MULT[规模]",
            "total_formula": "total = KH + KH × PS% + ACC",
        }
    hint = (
        f"{cfg['label']} 打分指南：共 {len(cfg['factors'])} 个要素、"
        f"{sum(len(v) for v in cfg['subfactors'].values())} 个子维度，"
        "每个子维度按锚定描述在 1-10 档位打分；"
        "建议由评估委员会对照锚定语独立打分后再校准。"
    )
    return ok_result(hint=hint, **payload)

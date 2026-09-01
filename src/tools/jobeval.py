# -*- coding: utf-8 -*-
"""
jobeval.py — 岗位价值评估（Job Evaluation）与职级校验
================================================================================

岗位价值评估回答一个问题：**"这个岗位值多少钱 / 该落在哪个职级？"**
它独立于个人，只看岗位本身的职责大小，是带宽（band）与职级体系的设计依据。

支持两套业界主流方法（由 model 切换）：
    - hay    ：海氏三要素法（知识技能 / 解决问题 / 应负责任）
    - mercer ：美世 IPE 四因素法（影响 / 沟通 / 创新 / 知识）

⚠️ P0 量纲修复（2026-08-30 强制）
--------------------------------------------------------------------------------
子维度打分是 **1–10 量纲**（每个子因素 1~10 分）；而岗位评估总分是
**0–1600 百分制**（海氏/美世通行刻度）。两者量纲完全不同！

历史上有一个致命 bug：直接把 1–10 的加权分 W 喂给 score_to_level()，
因为分段表 JOB_SCORE_LEVEL_BANDS 是 0–1600 区间，W∈[1,10] 恒落入 (0,300)
→ **所有岗位一律判为 P1**，模块 6 完全失效。

正确链路（本模块严格遵守）：
    子维度(1–10) → 要素内平均 → 按权重加权得 W(1–10)
                → schemas.weighted_to_total(W) = W × 160.0  → 百分制总分(0–1600)
                → schemas.score_to_level(总分)                → 建议职级

即：**绝不能直接把 1–10 的 W 传给 score_to_level**。
实测校验（见自验）：W=8.875 → 1420 → M3；W=3.14 → 502.4 → P3。

数据源（优先级）
--------------------------------------------------------------------------------
1. 子维度列：若 DataFrame 含模型对应的子因素/子维度列（如「知识技能」「解决问题」），
   则真实走「子维度(1–10) → 加权 → weighted_to_total → level」链路（最能体现 P0 修复）。
2. job_score 列：否则若该列存在，**自动判定量纲**——若最大值 ≤ 10 视为 1–10 加权分
   W 需经 weighted_to_total 换算；否则视为已是 0–1600 百分制总分，直接 score_to_level。
   （data/sample_salary.csv 的 job_score 实测 187~1414，已为总分刻度。）

输出
--------------------------------------------------------------------------------
- 给每行写回 job_level（评估建议职级）。
- meta['jobeval']：模型、量纲换算说明、与现状 level 的错配统计（建议职级≠现状职级）、
  错配样例（受 meta 50 行上限，只存最显著的若干条）。
- 返回值含完整逐岗对照表（不落盘）。

安全纪律：@tool_guard；会话访问走 loader.require_session。
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

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
    JOB_EVAL_MODELS,
    JOB_SCORE_LEVEL_BANDS,
    JOB_SCORE_SCALE,
    weighted_to_total,
    score_to_level,
    normalize_col as _norm,
)
from .session import get_store

# 现状 level 与建议 level 之间的「错配」程度排序（供 meta 只存最显著的样例）
_LEVEL_RANK = {lv: i for i, lv in enumerate(
    ["P1", "P2", "P3", "P4", "P5", "P6", "M1", "M2", "M3"])}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def _evaluate_from_subscores(df: pd.DataFrame, model: str) -> pd.Series:
    """
    走完整 P0 修复链路：子维度(1–10) → 要素平均 → 加权 W → weighted_to_total → 总分。
    返回每行的总分 Series（0–1600 刻度）。若无任何子维度列则抛 UpstreamMissing。
    """
    cfg = JOB_EVAL_MODELS[model]
    weights = cfg["weights"]
    matched = _subscore_columns(df, model)
    if not any(matched.values()):
        raise UpstreamMissing(
            f"数据中找不到模型 {model}（{cfg['label']}）的任一子维度列，无法按子维度评估",
            hint="请提供如「知识技能」「解决问题」「应负责任」等子维度打分列，"
                 "或确保数据含已算好的 job_score 列。",
        )
    totals = pd.Series(np.nan, index=df.index, dtype="float64")
    for factor, cols in matched.items():
        if not cols:
            continue
        # 要素内：取该要素全部子维度列的平均值（1–10）
        sub = df[cols].apply(pd.to_numeric, errors="coerce")
        factor_score = sub.mean(axis=1)  # 仍属 1–10 量纲
        totals = totals.fillna(0.0) + (factor_score * weights[factor])
    # totals 目前是 1–10 量纲的加权分 W，必须经 weighted_to_total 升维
    return totals.map(lambda w: weighted_to_total(float(w)) if np.isfinite(w) else np.nan)


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
    # 优先走子维度链路（最能体现 P0 修复）；否则用 job_score 列
    total_scores: Optional[pd.Series] = None
    source = ""
    try:
        total_scores = _evaluate_from_subscores(df, model)
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

    scale_note = (
        "量纲换算（P0 修复链路）：子维度打分 1–10 量纲 → 要素内平均 → 按权重加权得 W(1–10)"
        f" → schemas.weighted_to_total(W) = W × {JOB_SCORE_SCALE:.0f} → 百分制总分(0–1600)"
        " → schemas.score_to_level(总分) → 建议职级。"
        " 绝不可把 1–10 的 W 直接喂给 score_to_level（会恒判 P1）。"
    )

    if write_back:
        get_store().set_meta(session_id, {
            "jobeval": {
                "generated_at": _now(),
                "model": model,
                "model_label": cfg["label"],
                "score_source": source,
                "scale_note": scale_note,
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
        f"岗位评估完成（模型 {cfg['label']}，数据源：{source}）。"
        f"共 {n_jobs} 个岗位，其中 {mismatch_count} 个（{mismatch_pct}%）现状职级与建议职级错配。"
        "错配岗位见返回 table / meta['jobeval'].mismatch_examples，可用于校验职级体系合理性。"
    )

    return ok_result(
        session_id=session_id,
        model=model,
        model_label=cfg["label"],
        score_source=source,
        scale_note=scale_note,
        n_jobs=n_jobs,
        mismatch_count=mismatch_count,
        mismatch_pct=mismatch_pct,
        table=table,
        hint=hint,
    )

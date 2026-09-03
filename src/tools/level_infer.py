# -*- coding: utf-8 -*-
"""
level_infer.py — 职位名称 → 职级 启发式推断（R1）
================================================================================

背景
--------------------------------------------------------------------------------
真实 HR 工资表常见「有岗位(job_title)但没填职级(level)」的情况。而 `level` 是整条
诊断流水线的**分组主键**——带宽 / CR / 市场对标全部按 level 分组。

`loader.clean_dataframe` 在 level 缺失时会**整行剔除**这些员工，导致能诊断的人数
骤降（实测发放表开箱只用上 4/9）。本模块在 `confirm_mapping` 阶段、当映射里
「有 job_title 但无 level」时，用确定性启发式把 job_title 推断成 level，把
「缺 level 被全删」升级为「推断 + 覆盖率透明披露」，让发放表开箱即用。

设计纪律（与本项目「确定性优先 / 绝不静默」一致）
--------------------------------------------------------------------------------
1. 纯 Python + pandas，确定性、可单测、零外部依赖。
2. **绝不静默**：推断覆盖率、未命中样本逐条进日志与报告 warnings，
   由用户拍板「推断够不够用」——本模块只补数据，不替用户做可信度判断。
3. 不覆盖用户显式映射的 level（只有真的缺 level 才补）。
4. 多规则冲突按「职级越高越优先」收敛，不随机；罗马数字后缀用于同族内细分。
5. 阶梯口径与 `schemas.level_sort_key` 完全一致（O / P / S / M 四段）。

职级阶梯
--------------------------------------------------------------------------------
  O 段（作业 / 助理层）：O1 实习, O2 助理
  P 段（专业 / 技术层）：P1 初级, P2 工程师（默认专业岗）, P3 高级, P4 资深 / 专家
  S 段（主管 supervisory）：S1 主管 / 组长
  M 段（管理层）：M1 经理, M2 高级经理, M3 总监 / 部长, M4 副总及以上

用法
--------------------------------------------------------------------------------
  from tools.level_infer import infer_levels
  df2, report = infer_levels(df, title_col="job_title")
  # df2 新增标准列 "level"；report = {"covered": 8, "total": 9, "coverage": 0.889, ...}
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# =============================================================================
# 一、职级阶梯与关键词映射
# =============================================================================

# 各标准职级对应的「资深度排名」，数字越大越 senior；
# 命中多个关键词时取排名最高（最资深）的那个，避免「高级实习生」被误判成实习。
_LEVEL_RANK = {
    "O1": 1, "O2": 2,
    "P1": 3, "P2": 4, "P3": 5, "P4": 6,
    "S1": 7,
    "M1": 8, "M2": 9, "M3": 10, "M4": 11,
}

# 关键词 → 职级（正则，大小写不敏感；中文 / 英文 / 缩写都覆盖）。
# 顺序无关紧要：运行时按 _LEVEL_RANK 取最资深命中。
_KEYWORD_LEVEL: List[Tuple[str, str]] = [
    # —— 实习 / 助理层 ——
    (r"实习|intern|trainee|学徒", "O1"),
    (r"助理|assistant|文员|clerk|助工", "O2"),
    # —— 专业 / 技术层 ——
    (r"初级|junior|\bjr\b", "P1"),
    (r"工程师|engineer|技术员|technician|专员|specialist|分析|analyst|"
     r"经办|操作员|operato|研发|researcher|顾问|consultant|设计师|designer", "P2"),
    (r"高级|senior|\bsr\b|主[办任]|chief", "P3"),
    (r"资深|staff|principal|专家|expert|教授|professor", "P4"),
    # —— 主管层 ——
    (r"主管|组长|领班|supervisor|lead|班长|线长", "S1"),
    # —— 管理层 ——
    (r"经理|manager|课长|科长|科级|\bpm\b|项目经理|project manager", "M1"),
    (r"高级经理|senior manager|大客户总监", "M2"),
    (r"总监|director|部长|处长|division|总经理\(?集团", "M3"),
    (r"副总|副总裁|副总经理|vice|总经理|general manager|总裁|"
     r"cxo|\bceo\b|\bcoo\b|\bcfo\b|\bcto\b|局长|所长", "M4"),
]

# 罗马数字后缀 → 同族内 +1 细分（仅对 P 族生效，避免跨族乱跳）。
#   "工程师Ⅰ" → P2；"工程师Ⅲ" → P4（P2 + 2）
# 其它族不细分，保持阶梯稳定。
_ROMAN_RE = re.compile(r"[\s（(]*(Ⅰ|Ⅱ|Ⅲ|IV|III|II|I)[\s）)]*", re.IGNORECASE)
_ROMAN_BUMP = {"Ⅰ": 1, "II": 2, "III": 3, "Ⅳ": 1, "IV": 4,
               "i": 1, "ii": 2, "iii": 3, "iv": 4}
# 大写映射（re.IGNORECASE 已处理大小写，这里只归一）
_ROMAN_BUMP.update({k.upper(): v for k, v in list(_ROMAN_BUMP.items())})

_P_FAMILY = ("P1", "P2", "P3", "P4")


def _normalize_title(value: Any) -> str:
    """职位名归一：去空、全角→半角、小写，便于正则匹配。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    # 全角转半角（与 loader.parse_number 同思路）
    s = s.translate(str.maketrans(
        "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
        "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz",
    ))
    return s.lower()


def infer_level_from_title(title: Any) -> Optional[str]:
    """
    单条职位名 → 职级（纯函数，便于单测）。

    返回标准职级串（"P2" / "M1" ...）或 None（无法识别）。

    判定优先级
    ----------
    1. 若标题本身已是 level 形态（如 "P3" / "M2" / "o1"）→ 直接归一返回，不二次推断。
    2. 否则按关键词取「最资深命中」；再叠加罗马数字后缀（仅 P 族）。
    """
    raw = "" if title is None else str(title).strip()
    if not raw or (isinstance(title, float) and title != title):  # NaN
        return None

    # 1) 标题本身就是 level 写法 → 归一
    m = re.match(r"^\s*([OPSMopsm])[\s\-_]*(\d{1,2})\s*$", raw, re.IGNORECASE)
    if m:
        return f"{m.group(1).upper()}{int(m.group(2))}"

    t = _normalize_title(raw)
    if not t:
        return None

    # 2) 关键词取最资深命中
    best_level: Optional[str] = None
    best_rank = -1
    for pattern, level in _KEYWORD_LEVEL:
        if re.search(pattern, t):
            rk = _LEVEL_RANK.get(level, -1)
            if rk > best_rank:
                best_rank, best_level = rk, level

    if best_level is None:
        return None

    # 3) 罗马数字后缀细分（仅 P 族）
    if best_level in _P_FAMILY:
        rm = _ROMAN_RE.search(raw)
        if rm:
            bump = _ROMAN_BUMP.get(rm.group(1).upper())
            if bump:
                idx = _P_FAMILY.index(best_level) + (bump - 1)
                idx = max(0, min(len(_P_FAMILY) - 1, idx))
                best_level = _P_FAMILY[idx]
    return best_level


# =============================================================================
# 二、DataFrame 级接口（confirm_mapping 用）
# =============================================================================

def infer_levels(
    df: pd.DataFrame,
    title_col: str = "job_title",
    level_col: str = "level",
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    给 DataFrame 补一列推断的 level（当 level 缺失、且存在 title_col 时）。

    参数
    ----------
    df : pd.DataFrame
        原始（尚未清洗）的表。
    title_col : str
        职位列名（已是标准字段名 job_title，或原始列名——本函数只按列名取）。
    level_col : str
        输出列名，默认 "level"。

    返回
    -------
    (df_out, report)
        df_out  : 新增 level_col 的副本（已映射成标准 job_title 的列原样保留）。
        report  : {
            "title_col", "level_col",
            "total", "inferred", "already_had", "unresolved", "coverage",
            "unresolved_samples": [前若干条未识别的职位名],
        }

    行为约定
    ----------
    - df 已有 level_col 且非空 → 不覆盖，直接返回，coverage 记为 1.0（已具备）。
    - 已 mapped 出 level（调用方已把 level 放进映射）→ 调用方不会传进本函数，
      本函数只负责「真的缺 level」的补全。
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"infer_levels 需要 DataFrame，收到 {type(df).__name__}")

    out = df.copy()
    total = int(len(out))
    report: Dict[str, Any] = {
        "title_col": title_col,
        "level_col": level_col,
        "total": total,
        "inferred": 0,
        "already_had": 0,
        "unresolved": 0,
        "coverage": 0.0,
        "unresolved_samples": [],
    }

    # 已有 level 列且非全空 → 不覆盖，避免丢用户数据
    if level_col in out.columns:
        existing = out[level_col].astype("object")
        non_empty = int((~existing.isna() &
                         existing.map(lambda v: str(v).strip() != "" if v is not None else False)).sum())
        if non_empty:
            report["already_had"] = non_empty
            report["inferred"] = non_empty
            report["unresolved"] = total - non_empty
            report["coverage"] = round(non_empty / total, 4) if total else 0.0
            # 仍收集未识别样本（供用户复核原有 level 的完整性）
            if report["unresolved"]:
                report["unresolved_samples"] = [
                    str(v)[:30] for v in existing.dropna()
                    if str(v).strip() == ""
                ][:8]
            return out, report

    if title_col not in out.columns:
        report["unresolved"] = total
        return out, report

    titles = out[title_col].astype("object")
    levels = titles.map(infer_level_from_title)

    out[level_col] = levels

    inferred = int(levels.notna().sum())
    unresolved_mask = levels.isna()
    unresolved = int(unresolved_mask.sum())

    report["inferred"] = inferred
    report["unresolved"] = unresolved
    report["coverage"] = round(inferred / total, 4) if total else 0.0
    report["unresolved_samples"] = [
        str(v)[:30] for v in titles[unresolved_mask].dropna().head(8).tolist()
    ]
    return out, report


# =============================================================================
# 三、模块自检
# =============================================================================

if __name__ == "__main__":  # pragma: no cover
    demo = pd.DataFrame({
        "job_title": [
            "初级工程师", "高级软件工程师", "资深技术专家", "生产主管",
            "人力资源经理", "财务总监", "实习生", "行政助理",
            "销售副总裁", "保洁员",  # 「保洁员」无关键词 → None
        ],
    })
    d2, rep = infer_levels(demo)
    print("覆盖率:", rep["coverage"])
    for t, lv in zip(demo["job_title"], d2["level"]):
        print(f"  {t} -> {lv}")
    print("未识别样本:", rep["unresolved_samples"])

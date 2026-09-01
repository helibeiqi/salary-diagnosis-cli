# -*- coding: utf-8 -*-
"""
loader.py — 数据加载、字段映射、脱敏与清洗
================================================================================

本模块是整条诊断流水线的**入口**，负责把「HR 电脑里那张格式随意的工资表」
变成「字段名规范、数值干净、可直接计算的标准 DataFrame」。

对外 4 个工具函数（对应注册给模型的 2 个 tool + 1 个安全工具）：
--------------------------------------------------------------------------------
1. ``load_salary_data(file_path, sheet_name=None, session_id=None)``
   读 csv/xlsx/xls → 建会话 → 返回「前 5 行预览 + 字段映射建议 + 每列画像」。
   **这一轮不落任何结论**，只把信息摆给模型，由模型（结合语义）决定映射。

2. ``confirm_mapping(session_id, mapping, source_file=None)``
   模型拍板映射后调用 → 校验必填字段 → 改名 → 清洗 → 落盘。
   至此会话里的 DataFrame 才是「标准字段 + 干净数值」，下游全部工具依赖它。

3. ``desensitize(file_path, output_path=None, ...)``
   对**真实**数据做脱敏：薪资乘随机系数（总额守恒）、姓名打码、
   员工 ID 加盐哈希、司龄分箱、删除直接标识列。
   **本函数绝不打印任何明细行。**

4. 内部工具：``apply_mapping`` / ``clean_dataframe`` / ``preview_dataframe``。

为什么「机器建议 + 模型拍板」要拆成两步
--------------------------------------------------------------------------------
真实企业表头五花八门："基本工资" / "月薪" / "Base Pay" / "月固定薪" 都是同一件事，
而 "下限" 既可能是带宽下限，也可能是市场 P25。

v2 起表头匹配升级为**词根路由引擎**（`schemas.parse_column_semantics()`，本模块
同名转出）：语义词根（薪资/带宽/市场/分位…）× 维度修饰（月/年、下限/中位/上限、
25/50/75）查路由表定标准字段 —— 确定性、纯 Python、可被 fixture 逐条钉死。
**只有当程序自己判定 `ambiguous=True` 时才交给模型/用户拍板**，例如：
  · "薪资"    → 有语义无时间维度 → monthly_salary？annual_total_cash？
  · "下限"    → 有维度无语义     → band_min？mkt_p25？
  · "评估"    → 缺对象           → job_score？perf_grade？
这正是本项目「AI 负责理解、Python 负责计算」分工的第一处体现 ——
**确定性优先：能纯 Python 判死的绝不问模型；真歧义才升级。**

方法论口径（务必与报告一致）
--------------------------------------------------------------------------------
- 金额单位统一到**元**（"1.25万" → 12500；"¥12,500" → 12500）。
- 百分比字符串（"80%"）按**小数**解析 → 0.80，因为固浮比、达成率等后续全部按小数计算。
- "待定" / "—" / "N/A" 等无法解析的值 → **NaN**，绝不填 0（填 0 会让月薪均值被严重拉低）。
- 月薪异常值（<500 或 >1,000,000）**不裁剪**，只在清洗报告里提示 ——
  异常值往往正是红圈/绿圈的成因，剪掉等于毁掉诊断结论。
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .errors import (
    ColumnMissing,
    CompToolError,
    FileNotFound,
    InvalidParameter,
    MappingNotConfirmed,
    error_result,
    ok_result,
    tool_guard,
)
from .schemas import (
    CANONICAL_FIELDS,
    NUMERIC_FIELDS,
    REQUIRED_FIELDS,
    auto_suggest_mapping,
    normalize_col,
    parse_column_semantics,
)

# `parse_column_semantics` 的实现放在 schemas.py（与三层词根表、路由表同处，
# 避免"表在 A 文件、扫表逻辑在 B 文件"的割裂），但按需求 §1.3 从 loader 转出，
# 使 `from tools.loader import parse_column_semantics` 与
# `from tools.schemas import parse_column_semantics` 两条路径等价可用。
__all__ = [
    "read_table", "profile_columns", "preview_dataframe", "apply_mapping",
    "clean_dataframe", "load_salary_data", "confirm_mapping", "desensitize",
    "require_session", "require_columns", "parse_column_semantics",
    "auto_suggest_mapping", "parse_number", "coerce_numeric",
]
from .session import Session, get_store

# =============================================================================
# 一、常量与口径配置
# =============================================================================

#: 支持的表格扩展名 -> (pandas 读取引擎, 说明)
_SHEET_ENGINE = {
    ".xlsx": ("openpyxl", "Excel 2007+ 工作簿"),
    ".xlsm": ("openpyxl", "启用宏的 Excel 工作簿"),
    ".xls": ("xlrd", "Excel 97-2003 工作簿（需 xlrd）"),
}

#: 读取 CSV 时依次尝试的编码（中文 Windows 导出的 CSV 常是 GBK，必须兜底）
_CSV_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1")

#: 明确的「空值/占位」写法 —— 全部转 NaN，绝不填 0
NULL_TOKENS = {
    "", "-", "--", "—", "——", "/", "\\", "n/a", "na", "nan", "none", "null",
    "nil", "待定", "待确认", "待补", "暂无", "无", "未知", "不明", "未填写",
    "未定", "tbd", "tba", "?", "？", "不计", "不适用",
}

#: 货币符号与千分位等需要剥离的噪声字符
_NOISE_CHARS_RE = re.compile(r"[,\s，　_¥￥$€£元人民币RMBrmbCNYcny]")

#: 数值单位后缀 -> 换算到「元」的倍率
#  注意顺序：长单位必须在短单位之前匹配（"千万" 要先于 "千"）
_UNIT_MULTIPLIERS: Sequence[Tuple[str, float]] = (
    ("万亿", 1e12), ("亿元", 1e8), ("千万", 1e7), ("百万", 1e6),
    ("万", 1e4), ("萬", 1e4), ("w", 1e4), ("k", 1e3), ("千", 1e3),
)

#: 全角数字/字母 -> 半角（"１２３" / "ＡＢ" 在 HR 表里很常见）
_FULLWIDTH_MAP = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ．％",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz.%",
)

#: 各金额/数值字段的合理区间，用于**异常值提示**（只报告，不裁剪）
#  业务含义：超出区间不代表数据错，而是提醒分析师「这里可能有历史遗留高薪或录入错误」
OUTLIER_BOUNDS: Dict[str, Tuple[float, float]] = {
    "monthly_salary": (500.0, 1_000_000.0),      # 月薪：低于 500 多为兼职/实习，高于 100 万需核实
    "annual_total_cash": (6_000.0, 12_000_000.0),
    "mkt_p25": (500.0, 1_000_000.0),
    "mkt_p50": (500.0, 1_000_000.0),
    "mkt_p75": (500.0, 1_000_000.0),
    "band_min": (500.0, 1_000_000.0),
    "band_mid": (500.0, 1_000_000.0),
    "band_max": (500.0, 1_000_000.0),
    "tenure_years": (0.0, 50.0),                  # 司龄：>50 年基本是录入错误
    "job_score": (0.0, 3000.0),                   # 海氏/美世总分上限约 1500，放宽到 3000
}

#: 绩效等级的中英文写法归一（HR 表里 "a" / "A " / "优秀" 都要收敛到 A/B/C/D）
_PERF_ALIASES: Dict[str, str] = {
    "A": "A", "优秀": "A", "卓越": "A", "杰出": "A", "S": "A",
    "B": "B", "良好": "B", "优良": "B", "达标": "B", "胜任": "B",
    "C": "C", "合格": "C", "一般": "C", "待改进": "C", "基本合格": "C",
    "D": "D", "不合格": "D", "差": "D", "不达标": "D",
}

#: 直接标识符列（脱敏时直接删除，保留它们脱敏就失去意义）
_PII_PATTERNS = (
    "身份证", "证件号", "护照", "社保号", "手机号", "手机", "电话", "联系方式",
    "邮箱", "邮件", "email", "住址", "家庭地址", "银行卡", "银行账号", "开户行",
)

#: 薪资类字段（脱敏时加随机扰动）
_SALARY_LIKE_FIELDS = (
    "monthly_salary", "annual_total_cash", "band_min", "band_mid", "band_max",
    "mkt_p25", "mkt_p50", "mkt_p75",
)

#: 司龄分箱边界（左闭右开），标签即业务含义
_TENURE_BINS: Sequence[Tuple[float, float, str]] = (
    (0.0, 1.0, "0-1年"), (1.0, 3.0, "1-3年"), (3.0, 5.0, "3-5年"),
    (5.0, 10.0, "5-10年"), (10.0, 999.0, "10年以上"),
)


def _now() -> str:
    """统一时间格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_compact() -> str:
    """紧凑时间戳，用于输出文件名（保证可按名排序）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# =============================================================================
# 二、数值解析（B1 数值清洗的核心）
# =============================================================================


def parse_number(value: Any) -> float:
    """
    把**单个**单元格的值解析成 float（单位统一到「元」），无法解析返回 NaN。

    支持的写法（全部来自真实 HR 表的踩坑清单）：
        "12,500"      → 12500.0    千分位逗号
        "¥12,500"     → 12500.0    货币符号
        "1.25万"      → 12500.0    中文数量单位
        "1.25万元"    → 12500.0    单位带「元」
        "12.5k"       → 12500.0    英文数量单位
        "80%"         → 0.80       百分比按**小数**解析
        "(1,200)"     → -1200.0    括号表示负数（财务表惯例）
        "待定" / "—"  → NaN        占位符，**绝不填 0**
        12500 / 12500.0            → 原样返回

    为什么要「百分比转小数」而不是「保留 80」？
        因为固浮比、业绩达成率在后续公式里一律以小数参与运算
        （例如 实际总收入 = 固定 + 浮动 × 达成率，达成率 1.0 表示 100%）。
        若这里保留 80，下游每个公式都得再除以 100，是 bug 的温床。
        口径统一在入口做一次，后面全部干净。
    """
    if value is None:
        return float("nan")
    # 先处理真正的数值与 pandas 缺失标记（NaN/NaT/pd.NA 互不相等，须逐个判）
    if isinstance(value, (int, float, np.integer, np.floating)):
        f = float(value)
        return f if np.isfinite(f) else float("nan")
    if value is pd.NaT or (isinstance(value, float) and not np.isfinite(value)):
        return float("nan")
    try:
        if pd.isna(value):
            return float("nan")
    except (TypeError, ValueError):
        pass

    s = str(value).strip()
    if s.lower() in NULL_TOKENS:
        return float("nan")

    # 全角转半角（"１２，５００" → "12,500"）
    s = s.translate(_FULLWIDTH_MAP)

    # 括号负数：(1,200) → -1200
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()

    # 剥离货币符号、千分位、空格、下划线等噪声
    s = _NOISE_CHARS_RE.sub("", s)
    if not s:
        return float("nan")

    # 百分比：80% → 0.80
    if s.endswith("%"):
        body = s[:-1].strip()
        try:
            v = float(body) / 100.0
        except ValueError:
            return float("nan")
        return -v if negative else v

    # 数量单位：1.25万 → 12500
    for suffix, mult in _UNIT_MULTIPLIERS:
        if s.endswith(suffix):
            body = s[: -len(suffix)].strip()
            try:
                v = float(body) * mult
            except ValueError:
                return float("nan")
            return -v if negative else v

    # 纯数字
    try:
        v = float(s)
    except ValueError:
        return float("nan")
    return -v if negative else v


def coerce_numeric(series: pd.Series, field: Optional[str] = None) -> Tuple[pd.Series, Dict[str, Any]]:
    """
    把整列强制转成 float，并产出**转换报告**（B1 的核心产出）。

    与 `pd.to_numeric(errors="coerce")` 的区别：
    pandas 只会把 "12,500" 变成 NaN，而真实表里这种写法占了脏数据的大头；
    本函数先按业务写法解析（万/千/货币符/百分号），再退回 pandas，
    把「本可以救回来的数据」救回来，同时把**真的救不回来的**逐条报告出来。

    返回
    -------
    (float_series, report)
        report 结构：
            {
              "field": 字段名,
              "n_input": 输入行数,
              "n_non_null": 非空个数,
              "n_ok": 成功解析个数,
              "n_failed": 解析失败个数（含原本就空的？不含 —— 空值单独计 n_null）,
              "n_null": 原本为空/占位符的个数,
              "failed_samples": [{"row": 行号(1基), "raw": "待定"}, ...]  ≤5 条,
              "outliers": {"too_low": n, "too_high": n, "samples": [...]},
            }
    """
    raw = series
    values = [parse_number(v) for v in raw.tolist()]
    out = pd.Series(values, index=raw.index, dtype="float64")

    # 判定「解析失败」：原值非空（不是 NaN、不是占位符），但解析结果是 NaN
    raw_str = raw.astype("object")
    is_input_empty = raw.isna() | raw_str.map(
        lambda v: isinstance(v, str) and v.strip().lower() in NULL_TOKENS
    )
    failed_mask = (~is_input_empty) & out.isna()

    failed_rows = [
        {"row": int(idx) + 1, "raw": str(raw.iloc[pos])[:40]}
        for pos, idx in enumerate(raw.index)
        if bool(failed_mask.iloc[pos])
    ]

    report: Dict[str, Any] = {
        "field": field or str(getattr(raw, "name", "")),
        "n_input": int(len(raw)),
        "n_non_null": int((~is_input_empty).sum()),
        "n_ok": int(out.notna().sum()),
        "n_null": int(is_input_empty.sum()),
        "n_failed": int(failed_mask.sum()),
        "failed_samples": failed_rows[:5],   # 最多 5 条，避免报告被脏数据淹没
    }

    # 异常值：**只报告不裁剪**（裁剪会毁掉红圈/绿圈诊断）
    bounds = OUTLIER_BOUNDS.get(field or "")
    if bounds is not None:
        lo, hi = bounds
        too_low = out.notna() & (out < lo)
        too_high = out.notna() & (out > hi)
        samples = [
            {"row": int(idx) + 1, "value": float(out.iloc[pos])}
            for pos, idx in enumerate(raw.index)
            if bool((too_low | too_high).iloc[pos])
        ]
        report["outliers"] = {
            "lower_bound": lo,
            "upper_bound": hi,
            "too_low": int(too_low.sum()),
            "too_high": int(too_high.sum()),
            "samples": samples[:5],
            "note": "异常值仅提示，不做裁剪 —— 极端高薪往往正是红圈成因，剪掉等于掩盖问题。",
        }
    return out, report


# =============================================================================
# 三、文件读取
# =============================================================================


def _read_csv_any(path: str) -> pd.DataFrame:
    """
    读 CSV，自动依次尝试 utf-8-sig / utf-8 / gbk / gb18030 / latin-1。

    为什么必须多编码兜底：
    中文 Windows 上的 Excel「另存为 CSV」默认输出 **GBK**，
    直接用 utf-8 读会抛 UnicodeDecodeError —— 这是 HR 场景最高频的报错。
    """
    last_err: Optional[Exception] = None
    for enc in _CSV_ENCODINGS:
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
        except Exception as exc:  # noqa: BLE001 - 其他解析错误直接抛出，换编码也没用
            raise CompToolError(
                f"CSV 解析失败：{path}",
                hint="请确认文件是标准逗号分隔的 CSV；若是用分号分隔，请先另存为逗号分隔。",
                details={"path": path, "encoding": enc, "reason": str(exc)},
            ) from exc
    raise FileNotFound(
        f"CSV 编码无法识别：{path}（已尝试 {', '.join(_CSV_ENCODINGS)}）",
        hint="请用 Excel 打开后另存为「CSV UTF-8（逗号分隔）」再上传。",
        details={"path": path, "reason": str(last_err)},
    )


def _read_excel_any(path: str, sheet_name: Optional[str] = None) -> Tuple[pd.DataFrame, List[str]]:
    """
    读 xlsx / xls，返回 (DataFrame, 全部 sheet 名列表)。

    .xls 需要 xlrd（≥2.0 版本**只**支持 .xls，不再支持 .xlsx），
    缺失时给出明确的安装提示，而不是抛一句用户看不懂的 ImportError。
    """
    ext = os.path.splitext(path)[1].lower()
    engine, desc = _SHEET_ENGINE.get(ext, (None, "未知格式"))
    if engine is None:
        raise FileNotFound(
            f"不支持的文件类型：{ext}",
            hint="请提供 .csv / .xlsx / .xls 文件。",
            details={"path": path, "ext": ext},
        )

    try:
        book = pd.ExcelFile(path, engine=engine)
    except ImportError as exc:
        raise FileNotFound(
            f"读取 {desc} 缺少依赖引擎 {engine}：{path}",
            hint=f"请执行：C:/ProgramData/anaconda3/python.exe -m pip install {engine}",
            details={"path": path, "engine": engine, "reason": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise FileNotFound(
            f"Excel 读取失败：{path}",
            hint="文件可能损坏，或正被 Excel 独占打开 —— 请关闭 Excel 后重试。",
            details={"path": path, "engine": engine, "reason": str(exc)},
        ) from exc

    sheets = list(book.sheet_names)
    target = sheet_name if sheet_name else sheets[0]
    if sheet_name and sheet_name not in sheets:
        raise InvalidParameter(
            f"工作表 {sheet_name!r} 不存在",
            hint=f"该文件包含的工作表：{sheets}。请传其中一个，或留空使用第一个。",
            details={"path": path, "available_sheets": sheets, "requested": sheet_name},
        )
    try:
        df = pd.read_excel(book, sheet_name=target)
    except Exception as exc:  # noqa: BLE001
        raise CompToolError(
            f"读取工作表 {target!r} 失败：{exc}",
            hint="请确认该 sheet 是规则的二维表格（首行为表头）。",
            details={"path": path, "sheet": target, "reason": str(exc)},
        ) from exc
    return df, sheets


def read_table(file_path: str, sheet_name: Optional[str] = None
               ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    统一读表入口，返回 (DataFrame, 文件信息)。

    文件信息含：路径、大小、扩展名、sheet 列表（Excel）、读取耗时等，
    用于写进报告的「数据来源」章节，保证结论可追溯。
    """
    if not file_path or not isinstance(file_path, str):
        raise InvalidParameter(
            "file_path 必须是非空字符串",
            hint="请传入文件的绝对路径，Windows 下建议用 C:/... 正斜杠。",
            details={"file_path": str(file_path)},
        )
    if not os.path.exists(file_path):
        raise FileNotFound(
            f"文件不存在：{file_path}",
            hint="请检查路径是否写全。Windows 下建议复制文件属性里的完整路径，并用正斜杠 C:/...",
            details={"file_path": file_path},
        )
    if not os.path.isfile(file_path):
        raise FileNotFound(
            f"路径不是文件：{file_path}",
            hint="请指向具体的文件，而不是目录。",
            details={"file_path": file_path},
        )

    started = datetime.now()
    ext = os.path.splitext(file_path)[1].lower()
    info: Dict[str, Any] = {
        "file_path": os.path.abspath(file_path),
        "file_name": os.path.basename(file_path),
        "ext": ext,
        "size_kb": round(os.path.getsize(file_path) / 1024.0, 1),
    }
    if ext == ".csv":
        df = _read_csv_any(file_path)
    elif ext in _SHEET_ENGINE:
        df, sheets = _read_excel_any(file_path, sheet_name)
        info["available_sheets"] = sheets
        info["sheet_used"] = sheet_name or (sheets[0] if sheets else None)
    else:
        raise FileNotFound(
            f"不支持的文件类型：{ext or '(无扩展名)'}",
            hint="请提供 .csv / .xlsx / .xls 文件。",
            details={"file_path": file_path},
        )

    if df is None or df.empty:
        raise CompToolError(
            f"文件里没有数据：{info['file_name']}",
            hint="请确认表头在第一行，且数据区不为空。",
            details=info,
        )
    # 去掉「整行全空」的尾巴（Excel 常见的空白行），避免行数虚高
    df = df.dropna(how="all").reset_index(drop=True)
    # 去掉未命名的索引列（pandas 读 Excel 常自动补 "Unnamed: 0"）
    df = df.loc[:, [not str(c).startswith("Unnamed:") for c in df.columns]]

    info["rows"] = int(df.shape[0])
    info["cols"] = int(df.shape[1])
    info["columns"] = [str(c) for c in df.columns]
    info["read_seconds"] = round((datetime.now() - started).total_seconds(), 3)
    return df, info


# =============================================================================
# 四、列画像与预览
# =============================================================================


def _infer_type(series: pd.Series) -> str:
    """
    推断列的语义类型：number / text / date / empty。

    判定逻辑（先看 dtype，再看内容，避免把「全是字符串的月薪列」误判成文本）：
    1. 全空 → empty
    2. pandas 已识别为数值/日期 → number / date
    3. 内容是字符串，但抽样里 ≥80% 能被 `parse_number` 解析 → "number(需转换)"
       —— 这正是 "12,500" / "1.25万" 这类「看起来是文本、其实是金额」的列，
          必须提前识别出来，否则后续所有计算都会静默失效。
    4. 否则 → text
    """
    if series.isna().all():
        return "empty"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"

    sample = series.dropna().astype(str).head(50)
    if len(sample) == 0:
        return "empty"
    parsed = sum(1 for v in sample if not np.isnan(parse_number(v)))
    if parsed / len(sample) >= 0.8:
        return "number(需转换)"
    # 日期字符串（如 "2019-07-01"）做一个轻量识别，便于报告里说明该列未被使用
    date_like = sum(1 for v in sample
                    if re.match(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}", str(v).strip()))
    if date_like / len(sample) >= 0.8:
        return "date"
    return "text"


def profile_columns(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    给每一列画像：缺失率 / 唯一数 / 推断类型。

    **安全纪律**：只输出统计量，**不输出样例值**。
    因为列画像会进入模型上下文，一旦带上样例，薪资明细就泄露进对话了。
    """
    profiles: List[Dict[str, Any]] = []
    n = len(df)
    for col in df.columns:
        s = df[col]
        missing = int(s.isna().sum())
        try:
            n_unique = int(s.nunique(dropna=True))
        except TypeError:  # 列里含不可哈希对象（极少见）
            n_unique = -1
        profiles.append({
            "column": str(col),
            "missing_count": missing,
            "missing_rate": round(missing / n * 100, 2) if n else 0.0,
            "n_unique": n_unique,
            "inferred_type": _infer_type(s),
        })
    return profiles


def preview_dataframe(df: pd.DataFrame, n: int = 5) -> List[Dict[str, Any]]:
    """
    返回前 n 行预览（转成原生 dict 列表，保证可 JSON 序列化）。

    说明：预览**必然**包含薪资数值 —— 这是用户主动要求查看自己的表，
    属于合理展示；但默认只取 5 行，把暴露面压到最小。
    """
    if not isinstance(df, pd.DataFrame):
        raise InvalidParameter(
            f"preview_dataframe 需要 DataFrame，收到 {type(df).__name__}",
            details={"type": type(df).__name__},
        )
    n = int(n)
    if n <= 0:
        raise InvalidParameter("预览行数 n 必须为正整数", details={"n": n})
    head = df.head(n).copy()
    # NaN 在 JSON 里是 NaN（非法），统一替换成 None
    head = head.where(pd.notna(head), None)
    return head.to_dict(orient="records")


def _column_sample_values(df: pd.DataFrame, column: Any, n: int = 5) -> List[Any]:
    """
    取某一列的**前 n 个非空**示例值，供 LLM 边界契约里的"看着值再拍板"使用。

    与 preview_dataframe 的区别：preview 是行式（一行一个 dict，含全列），
    这里的列级示例值供"歧义列单独决策"直接消费，免去模型自行十字交叉。

    只取非空值：歧义列常见于脏表（前几行恰好是空/表头残留），
    取空值会让模型拿到 5 个 None，等于没给信息。
    取不到时返回 []（而不是 None）—— [] 在 JSON schema 里类型稳定。
    """
    try:
        series = df[column]
    except (KeyError, TypeError):
        return []
    vals = series.dropna().head(int(n)).tolist()
    out: List[Any] = []
    for v in vals:
        # numpy 标量（np.int64 等）不是 JSON 原生类型，统一转 Python 原生
        if hasattr(v, "item"):
            try:
                v = v.item()
            except (ValueError, AttributeError):
                v = str(v)
        if isinstance(v, float) and (v != v):  # NaN 兜底
            continue
        out.append(v)
    return out


# =============================================================================
# 五、字段映射
# =============================================================================


def apply_mapping(df: pd.DataFrame, mapping: Dict[str, str]) -> pd.DataFrame:
    """
    按 {原始列名: 标准字段名} 重命名列 —— 全链路「只认标准字段名」的关键一步。

    设计要点：
    1. **未映射的列原样保留**（不丢数据），并在返回值里由调用方报告 unmapped_columns；
    2. 两个原始列映射到同一标准字段 → 抛错（静默覆盖会丢数据，宁可让调用方改）；
    3. 映射后若出现同名列（原始列名恰好等于另一个标准字段名）→ 抛错。

    参数
    ----------
    df : pd.DataFrame
        原始表（列名是 HR 表里的写法）。
    mapping : dict
        {"基本工资(元/月)": "monthly_salary", "工号": "emp_id", ...}
        值必须是 `schemas.CANONICAL_FIELDS` 里的标准字段名。
    """
    if not isinstance(df, pd.DataFrame):
        raise InvalidParameter(
            f"apply_mapping 需要 DataFrame，收到 {type(df).__name__}",
            details={"type": type(df).__name__},
        )
    if not isinstance(mapping, dict) or not mapping:
        raise InvalidParameter(
            "mapping 必须是非空 dict，形如 {'基本工资': 'monthly_salary'}",
            hint="请先调用 load_salary_data 获取 suggested_mapping，再按需调整。",
            details={"type": type(mapping).__name__},
        )

    cols = [str(c) for c in df.columns]
    colset = set(cols)

    unknown_src = [k for k in mapping if str(k) not in colset]
    if unknown_src:
        raise ColumnMissing(
            f"映射里引用了不存在的列：{unknown_src}",
            hint=f"文件中的列名是：{cols}。请核对后重新调用 confirm_mapping。",
            details={"unknown_columns": unknown_src, "available_columns": cols},
        )

    bad_targets = [v for v in mapping.values() if v not in CANONICAL_FIELDS]
    if bad_targets:
        raise InvalidParameter(
            f"映射目标不是标准字段：{bad_targets}",
            hint=f"合法标准字段：{sorted(CANONICAL_FIELDS)}",
            details={"invalid_targets": bad_targets,
                     "valid_fields": sorted(CANONICAL_FIELDS)},
        )

    # 冲突检测：多个原始列 → 同一标准字段
    seen: Dict[str, List[str]] = {}
    for src, tgt in mapping.items():
        seen.setdefault(tgt, []).append(str(src))
    conflict = {t: s for t, s in seen.items() if len(s) > 1}
    if conflict:
        raise InvalidParameter(
            f"多个原始列被映射到同一标准字段：{conflict}",
            hint="每个标准字段只能对应一个原始列；请保留信息最全的那一列。",
            details={"conflicts": conflict},
        )

    rename = {str(k): v for k, v in mapping.items()}
    # 重命名后可能与未参与映射的原始列撞名
    remaining = [c for c in cols if c not in rename]
    dup = [c for c in remaining if c in set(rename.values())]
    if dup:
        raise InvalidParameter(
            f"映射后会产生重名列：{dup}（这些原始列名与标准字段名相同）",
            hint="请把这些列也纳入映射，或在映射中显式指向它们对应的标准字段。",
            details={"duplicate_after_rename": dup},
        )

    out = df.rename(columns=rename)
    return out


def _normalize_perf(value: Any) -> Optional[str]:
    """把绩效列的任意写法归一到 A/B/C/D（无法识别原样返回大写串）。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip().upper()
    if not s:
        return None
    if s in _PERF_ALIASES:
        return _PERF_ALIASES[s]
    # 取首字母（"A " / "a(优秀)" 之类）
    for ch in s:
        if ch in _PERF_ALIASES:
            return _PERF_ALIASES[ch]
    return s


def _normalize_pay_mix(value: Any) -> Optional[str]:
    """
    固浮比归一成 "整数:整数" 的字符串（如 "70:30"）。

    兼容写法："70:30" / "7:3" / "0.7" / "70%" / "70/30"。
    统一成整数比，避免下游解析时反复猜格式。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)$", s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
    else:
        # 单个数字：小数视为固定占比；百分数同样处理
        v = parse_number(s)
        if np.isnan(v):
            return s
        a, b = (v * 100, (1 - v) * 100) if v <= 1.0 else (v, 100 - v)
    total = a + b
    if total <= 0:
        return s
    fa, fb = a / total * 100, b / total * 100
    return f"{int(round(fa))}:{int(round(fb))}"


def _normalize_level(value: Any) -> Optional[str]:
    """职级归一：去空格 + 大写（"p3" / " P3 " → "P3"），保证分组与排序稳定。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip().upper()
    return s or None


def clean_dataframe(df: pd.DataFrame, mapping: Optional[Dict[str, str]] = None
                    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    清洗标准字段表 —— 下游一切计算的**唯一入口**，必须保证幂等。

    参数
    ----------
    df : pd.DataFrame
        待清洗的表。若传了 mapping，会先做 `apply_mapping` 再清洗。
    mapping : dict, optional
        {原始列名: 标准字段名}。

    返回
    -------
    **(df_clean, coerce_report)** —— 注意是**二元组**，调用方必须解包：

        df_clean, report = clean_dataframe(df)

    coerce_report 结构（同时也是报告「数据质量」章节的数据源）：
        {
          "rows_in": 输入行数,
          "rows_out": 输出行数,
          "dropped": {"missing_level": n, "missing_salary": n, "total": n},
          "duplicate_ids": 重复 emp_id 被丢弃的行数,
          "synthetic_ids": 合成 emp_id 的个数,
          "numeric": {字段名: 该列转换报告, ...},
          "notes": [说明文字, ...],
        }

    清洗动作（顺序很重要，见下方逐条注释）
    --------------------------------------------------------------------------
    """
    if not isinstance(df, pd.DataFrame):
        raise InvalidParameter(
            f"clean_dataframe 需要 DataFrame，收到 {type(df).__name__}",
            details={"type": type(df).__name__},
        )

    # ---- 0) 先做字段映射（若给了 mapping）-----------------------------------
    if mapping:
        df = apply_mapping(df, mapping)

    out = df.copy()
    rows_in = int(len(out))
    report: Dict[str, Any] = {
        "rows_in": rows_in,
        "rows_out": rows_in,
        "dropped": {"missing_level": 0, "missing_salary": 0, "total": 0},
        "duplicate_ids": 0,
        "synthetic_ids": 0,
        "numeric": {},
        "notes": [],
    }

    if rows_in == 0:
        report["notes"].append("输入表为空，无需清洗。")
        return out, report

    # ---- 1) emp_id：字符串化 + 缺失合成 -------------------------------------
    #  业务含义：emp_id 是「去重主键」与「结果回写主键」。
    #  缺失时**不抛错也不丢行**（丢行会让人数对不上），而是合成 ROW_{序号}，
    #  保证每一行都有可追踪的标识，同时用 synthetic_ids 提醒数据质量有问题。
    if "emp_id" not in out.columns:
        out["emp_id"] = [f"ROW_{i + 1}" for i in range(len(out))]
        report["synthetic_ids"] = int(len(out))
        report["notes"].append("数据中缺少员工ID列，已按行号合成 ROW_n 作为标识。")
    else:
        ids = out["emp_id"].astype("object")
        blank = ids.isna() | ids.map(lambda v: str(v).strip().lower() in NULL_TOKENS
                                     or str(v).strip() == "")
        n_blank = int(blank.sum())
        if n_blank:
            # 只对空位合成，已有 ID 保持原样（保住可追溯性）
            ids = ids.copy()
            ids[blank] = [f"ROW_{i + 1}" for i in np.where(blank)[0]]
            report["synthetic_ids"] = n_blank
            report["notes"].append(
                f"有 {n_blank} 行员工ID为空，已合成 ROW_n 占位（未丢弃这些行）。"
            )
        out["emp_id"] = ids.map(lambda v: str(v).strip())

    # ---- 2) 去重：重复 emp_id 保留首行 --------------------------------------
    #  业务含义：同一员工在表里出现多次（历史调薪记录/多岗位兼岗）时，
    #  若不处理会让人数虚高、CR 分布被重复计权。
    #  保留「首行」是约定俗成（多数导出按时间倒序，首行即最新状态）。
    dup_mask = out["emp_id"].duplicated(keep="first")
    n_dup = int(dup_mask.sum())
    if n_dup:
        out = out.loc[~dup_mask].copy()
        report["duplicate_ids"] = n_dup
        report["notes"].append(
            f"发现 {n_dup} 行员工ID重复，已保留每个 ID 的首行、丢弃后续重复行。"
        )

    # ---- 3) 文本类字段归一化 -------------------------------------------------
    for col in ("name", "dept", "job_title", "job_family", "level"):
        if col in out.columns:
            s = out[col].astype("object")
            s = s.map(lambda v: None if (v is None or (not isinstance(v, str) and pd.isna(v)))
                      else str(v).strip())
            s = s.map(lambda v: None if (v is None or v.lower() in NULL_TOKENS) else v)
            out[col] = s
    if "level" in out.columns:
        out["level"] = out["level"].map(_normalize_level)
    if "perf_grade" in out.columns:
        out["perf_grade"] = out["perf_grade"].map(_normalize_perf)
        report["notes"].append("绩效等级已归一到 A/B/C/D（含中英文写法与大小写统一）。")
    if "pay_mix" in out.columns:
        out["pay_mix"] = out["pay_mix"].map(_normalize_pay_mix)

    # ---- 4) 数值列强制转换（B1 核心）----------------------------------------
    #  只处理「标准数值字段」且表里真实存在的列；
    #  非标准字段（未映射的原始列）保持原样，不擅自改动用户数据。
    for col in NUMERIC_FIELDS:
        if col in out.columns:
            converted, col_report = coerce_numeric(out[col], field=col)
            out[col] = converted
            if col_report["n_failed"] or col_report.get("outliers", {}).get("too_low", 0) \
                    or col_report.get("outliers", {}).get("too_high", 0) or col_report["n_null"]:
                report["numeric"][col] = col_report

    # ---- 5) 必填字段缺失行的处理 --------------------------------------------
    #  口径：level（分组主键）与 monthly_salary（计算分子）缺失的行无法参与诊断，
    #  必须剔除；但 emp_id 缺失不剔除（见第 1 步，已合成）。
    if "level" in out.columns:
        miss_level = out["level"].isna()
        n = int(miss_level.sum())
        if n:
            out = out.loc[~miss_level].copy()
            report["dropped"]["missing_level"] = n
    else:
        report["notes"].append("数据中缺少职级列，所有按职级的分析（带宽/CR）将无法进行。")

    if "monthly_salary" in out.columns:
        miss_sal = out["monthly_salary"].isna()
        n = int(miss_sal.sum())
        if n:
            out = out.loc[~miss_sal].copy()
            report["dropped"]["missing_salary"] = n
            report["notes"].append(
                f"有 {n} 行月薪为空或无法解析（如填写了「待定」），已剔除；"
                "这些员工无法参与 CR 与带宽诊断，建议补齐后重跑。"
            )
    else:
        report["notes"].append("数据中缺少月薪列，无法进行任何薪酬计算。")

    out = out.reset_index(drop=True)
    report["dropped"]["total"] = rows_in - int(len(out))
    report["rows_out"] = int(len(out))

    # ---- 6) 年度总现金缺失时的提示（不自动填补，避免口径污染）----------------
    if "annual_total_cash" in out.columns and "monthly_salary" in out.columns:
        n_missing = int(out["annual_total_cash"].isna().sum())
        if n_missing:
            report["notes"].append(
                f"有 {n_missing} 行年度总现金缺失，下游将按「月薪×12」保守推算（不含奖金）。"
            )
    return out, report


# =============================================================================
# 六、工具 1：load_salary_data
# =============================================================================


@tool_guard
def load_salary_data(file_path: str, sheet_name: Optional[str] = None,
                     session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    读取薪酬文件并返回「预览 + 映射建议 + 列画像」，同时建立（或复用）会话。

    这是整个流程的**第一步**。模型拿到结果后，结合列的实际取值语义，
    决定每一列对应哪个标准字段，再调用 `confirm_mapping` 固化。

    参数
    ----------
    file_path : str
        csv / xlsx / xls 的路径。Windows 下建议 `C:/...` 正斜杠。
    sheet_name : str, optional
        Excel 工作表名；留空用第一个 sheet。
    session_id : str, optional
        传入则**复用**已有会话（覆盖其数据表），留空则新建。

    返回
    -------
    {
      "ok": True,
      "session_id": "s_20260830_...",
      "file_info": {路径/大小/sheet列表/行列数},
      "columns": [原始列名...],
      "preview": [前 5 行 dict...],
      "suggested_mapping": {原始列名: {suggest, confidence, label, ambiguous, candidates}},
      "columns_profile": [{column, missing_rate, n_unique, inferred_type}...],
      "required_fields": ["emp_id", "level", "monthly_salary"],
      "auto_confidence": {"ready": bool, "missing": [...], "low_confidence": [...]},
      "hint": 给模型的下一步指令
    }
    """
    df, info = read_table(file_path, sheet_name=sheet_name)
    store = get_store()

    # 会话要么复用、要么新建；两种路径都把原始表落盘（后续清洗过的表会覆盖它）
    if session_id:
        try:
            store.save_df(session_id, df)
        except Exception as exc:  # noqa: BLE001 - SessionNotFound 等会在此转成失败返回
            return error_result(exc)
    else:
        session_id = store.create(df, {})

    columns = [str(c) for c in df.columns]
    suggested = auto_suggest_mapping(columns)
    profiles = profile_columns(df)

    # 自动置信度：必填字段是否都被建议到了、是否存在低置信/歧义列
    mapped_targets = {v.get("suggest") for v in suggested.values() if v.get("suggest")}
    missing_required = [f for f in REQUIRED_FIELDS if f not in mapped_targets]
    low_conf = [c for c, v in suggested.items()
                if not v.get("suggest") or v.get("ambiguous") or v.get("confidence", 0) < 60]

    meta_patch = {
        "source_file": info["file_path"],
        "sheet_name": info.get("sheet_used"),
        "file_info": info,
        "raw_columns": columns,
        "suggested_mapping": suggested,
        "columns_profile": profiles,
        "loaded_at": _now(),
        # 关键：一旦换了数据源，旧映射与旧结论全部作废，防止模型拿旧 mapping 套新表
        "mapping": {},
        "coerce_report": {},
        "shape": {"rows": int(df.shape[0]), "cols": int(df.shape[1])},
    }
    try:
        store.set_meta(session_id, meta_patch)
    except Exception as exc:  # noqa: BLE001
        return error_result(exc)

    ready = not missing_required
    hint = (
        f"已读取 {info['rows']} 行 × {info['cols']} 列。请查看 suggested_mapping，"
        "结合列名语义与 preview 的实际取值确认映射（尤其注意 ambiguous=True 的列），"
        f"然后调用 confirm_mapping(session_id='{session_id}', mapping={{...}})。"
        if not ready else
        f"必填字段（{', '.join(REQUIRED_FIELDS)}）均已被自动建议，"
        f"但仍建议你复核 ambiguous=True 或 confidence<60 的列后，"
        f"调用 confirm_mapping(session_id='{session_id}', mapping={{...}}) 固化映射。"
    )

    return ok_result(
        session_id=session_id,
        file_info=info,
        columns=columns,
        preview=preview_dataframe(df, 5),
        suggested_mapping=suggested,
        columns_profile=profiles,
        required_fields=list(REQUIRED_FIELDS),
        auto_confidence={
            "ready": ready,
            "missing_required": missing_required,
            "low_confidence_columns": low_conf,
            # v2 新增：程序自己判定"必须问人/问模型"的列（附具体原因）。
            # 这是 LLM 边界契约的**唯一授权范围** —— 不在此列表里的列不得改动。
            # 每列额外附 sample_values：契约要求「决策输入包含该列前 5 行示例值」，
            # 这里直接给到列级别，省得模型拿 preview（行式 dict）去十字交叉取值。
            "ambiguous_columns": [
                {"column": c,
                 "suggest": v.get("suggest"),
                 "confidence": v.get("confidence"),
                 "reasons": v.get("ambiguous_reasons", []),
                 "candidates": [x.get("field") for x in v.get("candidates", [])],
                 "sample_values": _column_sample_values(df, c, 5)}
                for c, v in suggested.items() if v.get("ambiguous")
            ],
        },
        hint=hint,
    )


# =============================================================================
# 七、工具 2：confirm_mapping
# =============================================================================


@tool_guard
def confirm_mapping(session_id: str, mapping: Dict[str, str],
                    source_file: Optional[str] = None) -> Dict[str, Any]:
    """
    确认并固化字段映射，随即可选地落 mapping、执行清洗、覆盖会话数据表。

    参数
    ----------
    session_id : str
        `load_salary_data` 返回的会话 ID。
    mapping : dict
        {**原始列名**: 标准字段名}，例如
        `{"工号": "emp_id", "职务级别": "level", "基本工资(元/月)": "monthly_salary"}`。
        值必须来自 `schemas.CANONICAL_FIELDS`。
    source_file : str, optional
        数据来源说明（若与 load 时不同，比如数据被手工补过）。

    返回
    -------
    {
      "ok": True,
      "session_id": ...,
      "mapped_fields": {"emp_id": "工号", ...},    标准字段 -> 原始列名
      "unmapped_columns": [...],                   未参与映射的原始列（会被原样保留）
      "missing_required": [...],                   仍缺的必填字段（为空才算通过）
      "rows_in" / "rows_out": 清洗前后行数,
      "coerce_report": {...},                      数值转换与去重/合成报告
      "warnings": [...],
      "hint": 下一步建议
    }
    """
    store = get_store()
    try:
        session: Session = store.load(session_id)
    except Exception as exc:  # noqa: BLE001
        return error_result(exc)

    raw_columns = [str(c) for c in session.df.columns]

    # ---- 1) 逐列校验存在性（一次性报全部缺失列，避免模型反复试错）----------
    if not isinstance(mapping, dict) or not mapping:
        raise InvalidParameter(
            "mapping 必须是非空 dict，形如 {'工号': 'emp_id'}",
            hint="可从 load_salary_data 返回的 suggested_mapping 里取用 suggest 字段。",
        )
    unknown = [str(k) for k in mapping if str(k) not in set(raw_columns)]
    if unknown:
        raise ColumnMissing(
            f"映射引用了数据中不存在的列：{unknown}",
            hint=f"该文件实际列名：{raw_columns}",
            details={"unknown": unknown, "available": raw_columns},
        )
    bad_targets = [v for v in mapping.values() if v not in CANONICAL_FIELDS]
    if bad_targets:
        raise InvalidParameter(
            f"映射目标不是标准字段：{bad_targets}",
            hint=f"合法标准字段：{sorted(CANONICAL_FIELDS)}",
            details={"invalid_targets": bad_targets},
        )
    dup_targets = _find_dup_targets(mapping)
    if dup_targets:
        raise InvalidParameter(
            f"多个原始列映射到同一标准字段：{dup_targets}",
            hint="每个标准字段只能对应一个原始列，请保留信息最全的一列。",
            details={"conflicts": dup_targets},
        )

    # ---- 2) 必填字段齐备性 ---------------------------------------------------
    covered = set(mapping.values())
    missing_required = [f for f in REQUIRED_FIELDS if f not in covered]

    # ---- 3) 应用映射 + 清洗 + 落盘 ------------------------------------------
    #  设计判断（偏离架构的显式说明）：
    #  架构把 clean_dataframe 列为内部函数，但若不在这里顺带清洗，
    #  会话里存的就是「改了名但没洗干净」的表，下游每个工具都得记得再洗一次，
    #  迟早漏掉。因此这里**一次做到位**：映射 → 清洗 → 存清洗后的表。
    #  clean_dataframe 保持幂等，单独再调一次结果完全一致。
    df_clean, coerce_report = clean_dataframe(session.df, mapping=mapping)

    mapped_fields = {tgt: src for src, tgt in mapping.items()}
    unmapped_columns = [c for c in raw_columns if str(c) not in {str(k) for k in mapping}]

    warnings: List[str] = []
    if missing_required:
        warnings.append(
            f"必填字段仍缺失：{missing_required}。"
            f"缺 {', '.join(missing_required)} 时，依赖该字段的分析将无法进行。"
        )
    for f in ("band_min", "band_mid", "band_max"):
        if f not in covered:
            warnings.append(
                f"未提供 {f}（{CANONICAL_FIELDS[f][0]}）：将先用现状中位数生成建议带宽再诊断。"
            )
            break
    if not any(f in covered for f in ("mkt_p25", "mkt_p50", "mkt_p75")):
        warnings.append("未提供任何市场分位数据：市场对标模块将跳过，仅做内部公平性诊断。")

    meta_patch = {
        "mapping": {
            "map": {str(k): v for k, v in mapping.items()},
            "mapped_fields": mapped_fields,
            "unmapped_columns": unmapped_columns,
            "confirmed_at": _now(),
            "source_file": source_file or session.meta.get("source_file"),
            "missing_required": missing_required,
        },
        "coerce_report": coerce_report,
        "shape": {"rows": int(df_clean.shape[0]), "cols": int(df_clean.shape[1])},
    }
    store.save_df(session_id, df_clean)
    store.set_meta(session_id, meta_patch)

    if missing_required:
        hint = (f"映射已保存，但缺少必填字段 {missing_required}。"
                "请补充映射后重新调用 confirm_mapping；"
                f"当前数据列名：{raw_columns}")
    else:
        hint = ("映射已确认且数据已清洗落盘。"
                "下一步：若表里有带宽三列可直接 analyze_current_state；"
                "否则先调用 generate_band 生成建议带宽。")

    return ok_result(
        session_id=session_id,
        mapped_fields=mapped_fields,
        unmapped_columns=unmapped_columns,
        missing_required=missing_required,
        rows_in=int(coerce_report.get("rows_in", len(session.df))),
        rows_out=int(coerce_report.get("rows_out", len(df_clean))),
        coerce_report=coerce_report,
        warnings=warnings,
        hint=hint,
    )


def _find_dup_targets(mapping: Dict[str, str]) -> Dict[str, List[str]]:
    """找出「多个原始列 → 同一标准字段」的冲突。"""
    seen: Dict[str, List[str]] = {}
    for src, tgt in mapping.items():
        seen.setdefault(tgt, []).append(str(src))
    return {t: s for t, s in seen.items() if len(s) > 1}


# =============================================================================
# 八、工具 3：desensitize（脱敏）
# =============================================================================

# --- 脱敏子过程 ---------------------------------------------------------------


def _mask_name(value: Any) -> Optional[str]:
    """
    姓名脱敏：**只保留姓，其余打码** —— "张三" → "张**"，"欧阳修" → "欧**"。

    为什么保留姓？
    完全匿名化后，HR 无法把诊断结果对回员工去做沟通（"那位绿圈同事是谁"）。
    保留姓 + 打码名，在「可沟通」与「不可直接识别」之间取平衡，
    这是薪酬项目实操中最常见的脱敏粒度。
    英文名取首字母，同样保留可指代性。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if not s or s.lower() in NULL_TOKENS:
        return None
    return s[0] + "**"


def _hash_id(value: Any, salt: str) -> str:
    """
    员工 ID 加盐哈希：`S` + sha256(salt|原ID)[:10]。

    为什么用「加盐哈希」而不是「序号」？
    序号（S0001...）是可逆的 —— 只要拿到原表顺序就能还原身份；
    而加盐哈希**不可逆**，salt 每次随机生成且不落盘（除非指定 seed），
    即使脱敏文件外泄也无法反推员工。
    哈希值稳定，同一员工在同一批次的多次分析中 ID 一致，仍可跨表关联。
    """
    raw = "" if value is None else str(value).strip()
    digest = hashlib.sha256(f"{salt}|{raw}".encode("utf-8")).hexdigest()
    return "S" + digest[:10]


def _bin_tenure(value: Any) -> Optional[str]:
    """司龄分箱：连续年数 → 区间标签（降低个体可识别性，同时便于分组统计）。"""
    v = parse_number(value)
    if np.isnan(v):
        return None
    for lo, hi, label in _TENURE_BINS:
        if lo <= v < hi:
            return label
    return _TENURE_BINS[-1][2]


def _is_pii_column(col: str) -> bool:
    """判断列名是否属于直接标识符（命中即整列删除）。"""
    c = normalize_col(str(col))
    return any(normalize_col(p) in c for p in _PII_PATTERNS)


@tool_guard
def desensitize(file_path: str, output_path: Optional[str] = None,
                salary_jitter: float = 0.15, seed: Optional[int] = None,
                keep_ratio: bool = True, id_mode: str = "hash",
                sheet_name: Optional[str] = None) -> Dict[str, Any]:
    """
    对真实薪酬文件做脱敏，产出可安全外发/用于演示的副本。

    脱敏动作（每一项都对应一类再识别风险）
    --------------------------------------------------------------------------
    1. **薪资乘随机扰动**：`v' = v × (1 + U(-jitter, +jitter))`
       风险来源：精确薪资是强标识（"月薪 23,700 的那个人"全公司就一个）。
    2. **总额守恒回缩**（keep_ratio=True）：
       扰动后按 `总原值 / 总新值` 整体缩放，使**薪资总额恢复到原值**。
       这一步很关键：只乘不补会让总额漂掉几个百分点，
       导致成本测算、调薪预算全部失真；回缩后
       「个体不可还原」与「总额口径不变」两个目标同时成立。
    3. **姓名 → 姓 + `**`**
    4. **员工 ID → 加盐哈希**（默认）或序号（id_mode="seq"）
    5. **司龄分箱**：连续年数 → "3-5年" 区间
    6. **删除直接标识符列**：身份证/手机号/邮箱/银行卡/住址

    参数
    ----------
    file_path : str
        待脱敏的原始文件。
    output_path : str, optional
        输出路径；缺省在同目录生成 `{原名}_desensitized.csv`。
    salary_jitter : float, default 0.15
        扰动幅度，0.15 表示 ±15%。越大越不可还原、但统计特征失真越多。
    seed : int, optional
        随机种子；给出则**可复现**（面试演示/回归测试用）。生产环境建议留空。
    keep_ratio : bool, default True
        是否做总额守恒回缩。**强烈建议保持 True**。
    id_mode : str, default "hash"
        "hash" = 加盐哈希（不可逆，推荐）；"seq" = 序号（可逆，仅内部流转用）。
    sheet_name : str, optional
        Excel 工作表名。

    返回
    -------
    {
      "ok": True,
      "output_path": ...,
      "rows": 行数,
      "columns_dropped": [被删除的 PII 列],
      "salary_columns": [被扰动的列],
      "scale_factors": {"月薪列": 1.0234, ...},    总额守恒的回缩系数
      "total_before" / "total_after": 校验用总额,
      "id_mode": ...,
      "warnings": [...]
    }

    **安全纪律：本函数绝不打印任何数据明细。**
    """
    if not isinstance(salary_jitter, (int, float)) or not (0 <= float(salary_jitter) < 1):
        raise InvalidParameter(
            f"salary_jitter 必须是 [0, 1) 区间内的数字，收到 {salary_jitter!r}",
            hint="常用取值 0.10 ~ 0.20；0 表示不扰动（仅做姓名/ID 脱敏）。",
            details={"salary_jitter": salary_jitter},
        )
    if id_mode not in ("hash", "seq"):
        raise InvalidParameter(
            f"id_mode 只接受 'hash' 或 'seq'，收到 {id_mode!r}",
            details={"id_mode": id_mode},
        )

    df, info = read_table(file_path, sheet_name=sheet_name)
    rng = np.random.default_rng(seed)
    # salt 不落盘：除非调用方显式给了 seed（需要复现），否则每次随机
    salt = f"seed{seed}" if seed is not None else secrets.token_hex(16)

    out = df.copy()
    warnings: List[str] = []

    # ---- 1) 删除直接标识符列 -------------------------------------------------
    dropped = [str(c) for c in out.columns if _is_pii_column(str(c))]
    if dropped:
        out = out.drop(columns=dropped)
        warnings.append(f"已删除直接标识符列：{dropped}")

    # ---- 2) 识别列角色（用别名词典自动判断，不依赖固定表头）------------------
    suggestion = auto_suggest_mapping([str(c) for c in out.columns])
    col2field = {c: v.get("suggest") for c, v in suggestion.items() if v.get("suggest")}

    # ---- 3) 薪资扰动 + 总额守恒 ----------------------------------------------
    salary_cols = [c for c in out.columns if col2field.get(str(c)) in _SALARY_LIKE_FIELDS]
    # 兜底：若别名词典没识别出月薪列，再用列名关键字补一次
    if not salary_cols:
        salary_cols = [c for c in out.columns
                       if any(k in normalize_col(str(c))
                              for k in ("月薪", "工资", "薪资", "salary", "pay", "现金"))]
        if salary_cols:
            warnings.append(
                f"未按标准词典识别出月薪列，改按列名关键字匹配到：{salary_cols}，请人工复核。"
            )

    scale_factors: Dict[str, float] = {}
    total_before: Dict[str, float] = {}
    total_after: Dict[str, float] = {}
    for col in salary_cols:
        numeric, _ = coerce_numeric(out[col])
        valid = numeric.notna()
        if not valid.any():
            continue
        original = float(numeric[valid].sum())
        if original <= 0:
            continue

        # 3.1 逐值乘 (1 + 均匀分布扰动)
        noise = rng.uniform(-float(salary_jitter), float(salary_jitter), size=int(valid.sum()))
        jittered = numeric[valid].to_numpy(dtype="float64") * (1.0 + noise)

        # 3.2 总额守恒：整体回缩，使扰动后的合计恢复到 original
        #     数学表达：factor = Σ原值 / Σ扰动值；v'' = v' × factor
        #     效果：个体值仍带随机扰动（不可反推），但合计与原表一致（成本口径不失真）
        if keep_ratio:
            factor = original / float(jittered.sum())
        else:
            factor = 1.0
        final_values = jittered * factor

        # 3.3 取整到百元（真实工资表惯例，也进一步抹掉尾数特征）
        final_values = np.round(final_values / 100.0) * 100.0

        # 3.4 取整残差补偿：取整本身会带来 ±50 元/人的漂移，
        #     对 140 人的表累积起来可能有万分之几的总额偏差。
        #     这里把残差按「100 元」为最小单位分摊回若干个值，使总额**精确**回到原值。
        #     （分摊到哪些人无所谓 —— 值已被随机扰动过，再动 100 元不影响不可还原性）
        if keep_ratio:
            residual = original - float(final_values.sum())
            n_adj = int(round(residual / 100.0))
            if n_adj != 0:
                n_adj = int(np.clip(n_adj, -len(final_values), len(final_values)))
                order = rng.permutation(len(final_values))[: abs(n_adj)]
                final_values[order] += 100.0 * (1 if n_adj > 0 else -1)

        filled = numeric.copy()
        filled[valid] = final_values
        out[col] = filled
        scale_factors[str(col)] = round(float(factor), 6)
        total_before[str(col)] = round(original, 2)
        total_after[str(col)] = round(float(final_values.sum()), 2)

    if not salary_cols:
        warnings.append("未识别出任何薪资列，本次只做了姓名/ID/司龄脱敏。")

    # ---- 4) 姓名打码 ----------------------------------------------------------
    for col in out.columns:
        if col2field.get(str(col)) == "name":
            out[col] = out[col].map(_mask_name)

    # ---- 5) 员工 ID 脱敏 ------------------------------------------------------
    for col in out.columns:
        if col2field.get(str(col)) == "emp_id":
            if id_mode == "hash":
                out[col] = out[col].map(lambda v: _hash_id(v, salt))
            else:
                out[col] = [f"S{i + 1:04d}" for i in range(len(out))]
            warnings.append(
                f"员工ID已按 {id_mode} 模式脱敏"
                + ("" if id_mode == "hash" else "（序号模式可逆，请勿外发）")
            )

    # ---- 6) 司龄分箱 ----------------------------------------------------------
    for col in out.columns:
        if col2field.get(str(col)) == "tenure_years":
            out[col] = out[col].map(_bin_tenure)

    # ---- 7) 输出 --------------------------------------------------------------
    if output_path:
        target = os.path.abspath(output_path)
    else:
        stem = os.path.splitext(os.path.abspath(file_path))[0]
        target = f"{stem}_desensitized.csv"
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"无法创建输出目录：{parent}",
                details={"path": parent, "reason": str(exc)},
            ) from exc
    try:
        out.to_csv(target, index=False, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        raise CompToolError(
            f"写出脱敏文件失败：{target}",
            hint="目标文件可能正被 Excel 打开，请关闭后重试。",
            details={"path": target, "reason": str(exc)},
        ) from exc

    if keep_ratio and total_before:
        warnings.append(
            "薪资已做总额守恒回缩，合计金额与原表一致（个体带随机扰动，不可反推）。"
        )
    if seed is not None:
        warnings.append("本次使用了固定 seed，脱敏结果可复现；生产环境请留空 seed。")

    return ok_result(
        output_path=target,
        rows=int(len(out)),
        cols=int(out.shape[1]),
        columns_dropped=dropped,
        salary_columns=[str(c) for c in salary_cols],
        scale_factors=scale_factors,
        total_before=total_before,
        total_after=total_after,
        id_mode=id_mode,
        salary_jitter=float(salary_jitter),
        keep_ratio=bool(keep_ratio),
        source_file=info["file_path"],
        warnings=warnings,
        # 安全纪律：返回值里**不含**任何明细行，只有聚合数字与路径
        hint="脱敏文件已生成。出于数据安全纪律，这里不返回任何数据明细；"
             "请对新文件重新调用 load_salary_data 开始分析。",
    )


# =============================================================================
# 九、内部辅助：取会话并要求映射已确认
# =============================================================================


def require_session(session_id: str, need_mapping: bool = True) -> Session:
    """
    取会话；`need_mapping=True` 时，若 mapping 未确认则抛 MappingNotConfirmed。

    所有下游计算工具（band / diagnose / market ...）都应走这个函数拿会话，
    把「会话不存在」「映射未确认」两类高频错误的处理收敛到一处。
    """
    store = get_store()
    session = store.load(session_id)
    if need_mapping:
        mapping = (session.meta.get("mapping") or {}).get("map")
        if not mapping:
            raise MappingNotConfirmed(
                f"会话 {session_id} 尚未确认字段映射，无法进行数值计算。",
                hint="请先调用 load_salary_data 查看列画像，再调用 confirm_mapping 固化映射。",
                details={
                    "session_id": session_id,
                    "raw_columns": session.meta.get("raw_columns", []),
                    "suggested_mapping": session.meta.get("suggested_mapping", {}),
                },
            )
    return session


def require_columns(df: pd.DataFrame, fields: Sequence[str],
                    tool_name: str = "本工具") -> None:
    """
    校验 DataFrame 里存在指定标准字段，缺则抛 ColumnMissing（一次性报全部缺失）。
    """
    missing = [f for f in fields if f not in df.columns]
    if missing:
        raise ColumnMissing(
            f"{tool_name} 缺少必需字段：{missing}",
            hint="请用 confirm_mapping 把这些标准字段映射到实际列；"
                 f"当前表里的列：{[str(c) for c in df.columns]}",
            details={"missing": missing,
                     "available": [str(c) for c in df.columns],
                     "meaning": {f: CANONICAL_FIELDS[f][0] for f in missing
                                 if f in CANONICAL_FIELDS}},
        )

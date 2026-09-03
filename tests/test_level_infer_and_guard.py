# -*- coding: utf-8 -*-
"""
test_level_infer_and_guard.py — R1（职位→职级推断）+ R2（敏感数据护栏）回归测试
================================================================================
运行：
    cd <repo> && python -m pytest tests/test_level_infer_and_guard.py -q
    # 或单独跑：python tests/test_level_infer_and_guard.py
不依赖 markdown / plotly / 网络：纯函数与 HTML 字符串断言，可在无 GUI 环境验证。
"""
from __future__ import annotations

import importlib
import sys
import os

import pandas as pd

# 让 src/ 进入 sys.path（包是 src/tools，直接 python 跑本文件也能 import tools）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
for _p in (_SRC, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  → {detail}" if (detail and not cond) else ""))


# ---------------------------------------------------------------------------
# 0) 所有工具模块可导入（不破坏流水线）
# ---------------------------------------------------------------------------
def test_imports() -> None:
    mods = [
        "tools.loader", "tools.report", "tools.charts", "tools.level_infer",
        "tools.increase", "tools.band", "tools.diagnose", "tools.market",
        "tools.paymix", "tools.jobeval", "tools.schemas", "tools.errors",
        "tools.session", "tools.registry",
    ]
    for m in mods:
        try:
            importlib.import_module(m)
            check(f"import {m}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"import {m}", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 1) R1：infer_level_from_title 单条推断
# ---------------------------------------------------------------------------
def test_infer_level_from_title() -> None:
    from tools.level_infer import infer_level_from_title

    cases = [
        ("实习生", "O1"),
        ("行政助理", "O2"),
        ("初级工程师", "P2"),       # 工程师(P2) 资深度 > 初级(P1)
        ("高级软件工程师", "P3"),
        ("资深技术专家", "P4"),
        ("生产主管", "S1"),
        ("人力资源经理", "M1"),
        ("财务总监", "M3"),
        ("销售副总裁", "M4"),
        ("保洁员", "O2"),           # 辅助岗 → O2（Plan C·B 扩展词表）
        ("P3", "P3"),               # 已是 level 写法 → 归一
        ("m2", "M2"),               # 小写 level 写法
        (None, None),
    ]
    for title, expect in cases:
        got = infer_level_from_title(title)
        ok = got == expect
        check(f"infer({title!r}) == {expect!r}", ok, f"got {got!r}")
        if not ok:
            print(f"     期望 {expect!r}，实际 {got!r}")


# ---------------------------------------------------------------------------
# 2) R1：infer_levels DataFrame 覆盖率
# ---------------------------------------------------------------------------
def test_infer_levels_coverage() -> None:
    from tools.level_infer import infer_levels

    df = pd.DataFrame({
        "job_title": [
            "初级工程师", "高级软件工程师", "资深技术专家", "生产主管",
            "人力资源经理", "财务总监", "实习生", "行政助理", "外包人员",
        ],
    })
    out, rep = infer_levels(df, title_col="job_title")
    check("infer_levels 返回 level 列", "level" in out.columns)
    check("inferred == 8", rep["inferred"] == 8, str(rep["inferred"]))
    check("total == 9", rep["total"] == 9, str(rep["total"]))
    check("coverage ~ 0.889", rep["coverage"] == round(8 / 9, 4), str(rep["coverage"]))
    check("unresolved == 1", rep["unresolved"] == 1, str(rep["unresolved"]))
    check("unresolved_samples 含 '外包人员'",
          any("外包人员" in str(s) for s in rep["unresolved_samples"]),
          str(rep["unresolved_samples"]))
    # Plan C·A：未识别职位标为 UNKNOWN 单列保留，而非置 NaN/被删行
    check("未识别职位 level == 'UNKNOWN'（不丢行）",
          out["level"].iloc[8] == "UNKNOWN", str(out["level"].iloc[8]))
    check("level 列无 NaN", out["level"].isna().sum() == 0,
          str(out["level"].isna().sum()))

    # 已有 level 列且不空 → 不覆盖
    df2 = pd.DataFrame({"job_title": ["工程师"], "level": ["P9"]})
    out2, rep2 = infer_levels(df2)
    check("已有 level 不覆盖", out2["level"].iloc[0] == "P9", str(out2["level"].iloc[0]))
    check("已有 level coverage == 1.0", rep2["coverage"] == 1.0, str(rep2["coverage"]))


# ---------------------------------------------------------------------------
# 3) R2：数据分类检测
# ---------------------------------------------------------------------------
def test_detect_classification() -> None:
    from tools.report import _detect_salary_classification as det

    check("显式 sanitized", det({"data_classification": "sanitized"}) == "sanitized")
    check("显式 real", det({"data_classification": "real"}) == "real")
    check("默认 real（空 meta）", det({}) == "real")
    check("默认 real（None）", det(None) == "real")
    check("文件名后缀 _desensitized.csv → sanitized",
          det({"source_file": "D:/x/工资_desensitized.csv"}) == "sanitized")
    check("普通源文件 → real",
          det({"source_file": "D:/x/工资表.csv"}) == "real")


# ---------------------------------------------------------------------------
# 4) R2：报告水印（真实 vs 脱敏）
# ---------------------------------------------------------------------------
def test_report_watermark() -> None:
    from tools.report import _md_to_html

    real_html = _md_to_html("# 报告", "薪酬诊断报告", "副标题", [], classification="real")
    san_html = _md_to_html("# 报告", "薪酬诊断报告", "副标题", [], classification="sanitized")

    # 用完整标签匹配（避免与 _CSS 里的 .watermark-banner 类名定义误命中）
    check("真实报告含顶部红字水印 banner",
          '<div class="watermark-banner">' in real_html)
    check("真实报告页脚含 '含真实薪酬数据'",
          "含真实薪酬数据" in real_html)
    check("真实报告不再谎称 '数据均为脱敏模拟数据'",
          "数据均为脱敏模拟数据" not in real_html)
    check("脱敏报告无真实水印 banner 标签",
          '<div class="watermark-banner">' not in san_html)
    check("脱敏报告页脚含 '数据已脱敏处理'",
          "数据已脱敏处理" in san_html)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== R1/R2 回归自测 ===")
    test_imports()
    test_infer_level_from_title()
    test_infer_levels_coverage()
    test_detect_classification()
    test_report_watermark()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")

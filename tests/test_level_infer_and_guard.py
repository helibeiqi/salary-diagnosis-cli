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
    check("显式 synthetic", det({"data_classification": "synthetic"}) == "synthetic")
    check("显式 simulated → 归一 synthetic", det({"data_classification": "simulated"}) == "synthetic")
    check("默认 real（空 meta）", det({}) == "real")
    check("默认 real（None）", det(None) == "real")
    check("文件名后缀 _desensitized.csv → sanitized",
          det({"source_file": "D:/x/工资_desensitized.csv"}) == "sanitized")
    check("普通源文件 → real",
          det({"source_file": "D:/x/工资表.csv"}) == "real")
    check("文件名含 sample → synthetic",
          det({"source_file": "data/sample_salary.csv"}) == "synthetic")
    check("文件名含 messy → synthetic",
          det({"source_file": "data/messy_salary.csv"}) == "synthetic")


# ---------------------------------------------------------------------------
# 4) R2：报告水印（真实 vs 脱敏）
# ---------------------------------------------------------------------------
def test_report_watermark() -> None:
    from tools.report import _md_to_html

    real_html = _md_to_html("# 报告", "薪酬诊断报告", "副标题", [], classification="real")
    san_html = _md_to_html("# 报告", "薪酬诊断报告", "副标题", [], classification="sanitized")
    syn_html = _md_to_html("# 报告", "薪酬诊断报告", "副标题", [], classification="synthetic")

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
    # P1-4：模拟数据报告——中性横幅，绝不能出现真实数据的红字措辞
    check("模拟报告含中性模拟横幅",
          "watermark-banner-synthetic" in syn_html)
    check("模拟报告标注 '模拟数据'",
          "模拟数据" in syn_html)
    check("模拟报告无真实红字水印 banner",
          '<div class="watermark-banner">' not in syn_html)
    check("模拟报告页脚不含 '含真实薪酬数据'",
          "含真实薪酬数据" not in syn_html)


# ---------------------------------------------------------------------------
# 5) P1-4：模拟数据加载 → synthetic 分级，报告不去红字 / 不写 data_guard
# ---------------------------------------------------------------------------
def test_synthetic_load_and_report() -> None:
    import os as _os
    from tools.loader import load_salary_data, confirm_mapping
    from tools.report import generate_report

    sample = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                           "data", "sample_salary.csv")
    if not _os.path.exists(sample):
        check("sample 文件存在（跳过）", False, sample)
        return

    res = load_salary_data(sample)
    check("load_salary_data ok", res.get("ok"), str(res.get("code")))
    sid = res["session_id"]
    from tools.session import get_store
    cls = get_store().get_meta(sid).get("data_classification")
    check("sample 自动判为 synthetic（不误标 real）",
          cls == "synthetic", str(cls))

    # 显式覆盖：传 real 时确实变 real
    res2 = load_salary_data(sample, session_id=sid, data_classification="real")
    cls2 = get_store().get_meta(sid).get("data_classification")
    check("显式 data_classification=real 生效", cls2 == "real", str(cls2))

    # 复位回 synthetic 再跑报告：前面用同一 sid 验过 real 覆盖，会持久化进 meta，
    # 若不复位，报告会按 real 生成护栏，使下面的 synthetic 断言失真。
    load_salary_data(sample, session_id=sid, data_classification="synthetic")
    # 复用同会话（已复位为 synthetic）跑报告：不应生成 data_guard.md
    mp = {c: v["suggest"] for c, v in res["suggested_mapping"].items() if v.get("suggest")}
    confirm_mapping(sid, mp)
    rep = generate_report(sid, fmt="html", title="模拟数据诊断")
    check("synthetic 报告无 data_guard 护栏文件",
          rep.get("data_guard_path") is None, str(rep.get("data_guard_path")))
    html_path = rep.get("html_path")
    if html_path and _os.path.exists(html_path):
        txt = open(html_path, encoding="utf-8").read()
        check("synthetic 报告 HTML 不含真实红字水印",
              "watermark-banner-synthetic" in txt and "含真实薪酬数据" not in txt)
    # 自洁：本测试生成的报告产物不留在 tests/_out，避免干扰 qa_e2e 的 FIX3 污染判定
    for _p in (html_path, rep.get("report_path")):
        if _p and _os.path.exists(_p):
            try:
                _os.remove(_p)
            except OSError:
                pass


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
    test_synthetic_load_and_report()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：", FAIL)
        sys.exit(1)
    print("全部通过 ✅")

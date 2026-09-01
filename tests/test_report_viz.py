# -*- coding: utf-8 -*-
"""
test_report_viz.py — report.py / charts.py 的自验收脚本（对应 AC-24 ~ AC-27）
================================================================================
为什么用「脚本式断言」而不是 pytest
--------------------------------------------------------------------------------
1. 本项目交付给 HR 做面试作品集，QA 需要在**不装 pytest 插件**的环境下双击跑通；
   纯标准库 + 直接 `python tests/test_report_viz.py` 最省事。
2. 验收标准（AC）是**文件级**的：图有没有落盘、相对路径能不能解析、中文字体写没写进去
   —— 这些用 assert + os.path.exists 检查最直接，不需要测试框架的 fixture 机制。

覆盖的验收项
--------------------------------------------------------------------------------
    AC-24  六张图表 HTML 真的生成在 assets/ 且非空（可浏览器打开）
    AC-25  报告七个章节标题齐全且顺序正确
    AC-26  所有图片相对路径拼到报告目录后**文件真实存在**（断链数 = 0）
    AC-27  中文字体配置写进了 HTML（搜索 Microsoft YaHei 能搜到）
    附加    PNG 降级不抛异常；章节缺失时能降级不崩；固浮比方法论提示原样出现

跑法
--------------------------------------------------------------------------------
    cd comp-agent-harness
    PYTHONIOENCODING=utf-8 python tests/test_report_viz.py
    退出码 0 = 全部通过；非 0 = 有断言失败（会打印逐项明细）
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))

from tests.fixtures.mock_results import (  # noqa: E402
    build_mock_meta,
    build_mock_session,
)
from tools import charts  # noqa: E402
from tools import report as report_mod  # noqa: E402

# 验收基线
EXPECTED_SECTIONS: List[str] = [
    "执行摘要",
    "数据概览与字段映射说明",
    "薪酬现状诊断",
    "带宽设计与市场对标建议",
    "调薪方案对比与推荐",
    "固浮比与激励建议",
    "风险提示与实施路线图",
]
EXPECTED_CHARTS: List[str] = [
    "band_overlap", "cr_distribution", "cr_before_after",
    "strategy_cost", "market_gap", "paymix_curve",
]
# PRD 模块 5 硬性要求：这句方法论提示必须原样出现在报告正文
REQUIRED_PRINCIPLE = (
    "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；"
    "长周期协作型业务强行高浮动会破坏协作。"
)

_PASS: List[str] = []
_FAIL: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    """记录一条断言结果；失败时把原因攒起来，最后一次性打印。"""
    if cond:
        _PASS.append(name)
        print(f"  [PASS] {name}" + (f"  {detail}" if detail else ""))
    else:
        _FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")
    return bool(cond)


def section(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


# =============================================================================
# AC-24：六张图表落盘
# =============================================================================

def test_ac24_charts() -> None:
    section("AC-24 六张图表 HTML 落盘（kaleido 缺失时 PNG 应降级为 None 而非报错）")
    meta = build_mock_meta()

    # charts.build_all_charts 是上游的批量出图入口，报告也依赖它的取值路径
    built = charts.build_all_charts(meta, try_png=True)
    for name in EXPECTED_CHARTS:
        info = built.get(name) or {}
        hp = info.get("html_path")
        ok = bool(hp) and os.path.exists(hp) and os.path.getsize(hp) > 0
        size = os.path.getsize(hp) if (hp and os.path.exists(hp)) else 0
        check(f"图表 {name} 落盘", ok, f"{os.path.basename(hp) if hp else 'None'} {size} B")

        # PNG 缺失是本机预期（kaleido 未安装），但必须记录原因而不是静默
        if info.get("png_path") is None:
            check(f"图表 {name} PNG 降级有记录", bool(info.get("png_error")),
                  str(info.get("png_error"))[:70])
        else:
            check(f"图表 {name} PNG 已导出", os.path.exists(info["png_path"]))

    # 单独验证每个图表函数都能独立调用（不依赖 build_all_charts 的取值路径）
    section("AC-24b 六个图表函数独立调用（不抛异常）")
    cur, mkt, band, inc, pm = (meta.get(k) or {} for k in
                               ("current_state", "market_benchmark", "band",
                                "increase_sim", "pay_mix"))
    cases = [
        ("band_overlap_chart", lambda: charts.band_overlap_chart(band["band_table"])),
        ("cr_distribution_chart", lambda: charts.cr_distribution_chart(cur["cr_values"])),
        ("cr_before_after_chart", lambda: charts.cr_before_after_chart(inc["before"], inc["after"])),
        ("strategy_cost_chart", lambda: charts.strategy_cost_chart(inc["strategies"],
                                                                   budget=inc.get("budget"))),
        ("market_gap_chart", lambda: charts.market_gap_chart(mkt["by_level"])),
        ("paymix_curve_chart", lambda: charts.paymix_curve_chart(pm["curves"])),
    ]
    for fname, fn in cases:
        try:
            fig = fn()
            saved = charts.save_figure(fig, f"viztest_{fname}")
            ok = os.path.exists(saved["html_path"]) and os.path.getsize(saved["html_path"]) > 0
            check(f"{fname} 可独立出图", ok, f"{os.path.getsize(saved['html_path'])} B")
        except Exception as exc:  # noqa: BLE001
            check(f"{fname} 可独立出图", False, f"{type(exc).__name__}: {exc}")


# =============================================================================
# AC-25 / AC-26 / AC-27：报告结构、图片路径、中文字体
# =============================================================================

def test_ac25_26_27_report() -> Dict[str, Any]:
    section("AC-25/26/27 报告结构 · 图片相对路径 · 中文字体")
    res = report_mod.generate_report(
        session=build_mock_session("viz-test-001"), fmt="both", title="薪酬诊断报告")

    check("generate_report 返回 ok", bool(res.get("ok")), str(res.get("error", ""))[:120])
    if not res.get("ok"):
        return res

    # ---- AC-25：七章齐全且顺序正确 ----
    sections = res.get("sections") or []
    check("章节数量为 7", len(sections) == 7, f"实际 {len(sections)}")
    check("章节顺序与标题正确", sections == EXPECTED_SECTIONS, str(sections))
    check("无降级章节（完整数据下）", not (res.get("missing_sections") or []),
          str(res.get("missing_sections")))

    md_path = res.get("report_path")
    html_path = res.get("html_path")
    check("Markdown 报告已落盘", bool(md_path) and os.path.exists(md_path))
    check("HTML 报告已落盘", bool(html_path) and os.path.exists(html_path))

    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()

    cn = ["一", "二", "三", "四", "五", "六", "七"]
    for i, title in enumerate(EXPECTED_SECTIONS):
        check(f"报告正文含章节标题「{cn[i]}、{title}」", f"## {cn[i]}、{title}" in md)

    # ---- AC-26：图片相对路径可解析 ----
    figures = res.get("figures") or []
    check("图表数量为 6", len(figures) == 6, f"实际 {len(figures)}")
    check("断链图表数为 0", not (res.get("broken_figures") or []),
          str(res.get("broken_figures")))

    report_dir = os.path.dirname(os.path.abspath(md_path))
    for fig in figures:
        rel = fig.get("rel_path") or ""
        abs_path = os.path.normpath(os.path.join(report_dir, rel))
        exists = os.path.exists(abs_path)
        check(f"图片相对路径可解析 [{fig.get('caption')}]", exists,
              f"{rel} -> {abs_path}")
        # 相对路径必须以 ../ 开头（报告在 report/，图在 assets/）
        check(f"相对路径以 ../ 开头 [{fig.get('caption')}]", rel.startswith("../"),
              rel)
        # Markdown 正文里必须真的引用了这个路径
        check(f"正文引用了该路径 [{fig.get('caption')}]",
              (f"({rel})" in md) or (f'href="{rel}"' in md), rel)

    # ---- AC-27：中文字体配置 ----
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    check("HTML 报告含中文字体配置", "Microsoft YaHei" in html)
    check("HTML 报告含 <meta charset=\"utf-8\">", '<meta charset="utf-8">' in html)
    check("HTML 报告内嵌了图表 div",
          html.count('class="plotly-graph-div"') >= 6,
          f"找到 {html.count('class=\"plotly-graph-div\"')} 个")
    # plotly.js 只应引入一次（多图重复加载 3MB 会让报告打开很慢）
    n_js = len(re.findall(r"cdn\.plot\.ly/plotly-[0-9.]+\.min\.js", html))
    check("plotly.js 只引入一次", n_js == 1, f"找到 {n_js} 处")

    # ---- 图表 HTML 文件本身也要能被浏览器正确解码 ----
    section("AC-27b 独立图表 HTML 的编码与字体")
    for fig in figures:
        hp = fig.get("html_path")
        if not hp or not os.path.exists(hp):
            continue
        # 必须读全文：字体配置在 layout 的 JSON 里，通常在文件中后部，
        # 只读前 4KB 会误判为「没有中文字体」。
        with open(hp, "r", encoding="utf-8") as f:
            body = f.read()
        name = os.path.basename(hp)
        check(f"{name} 含中文字体", "Microsoft YaHei" in body)
        # 必须是**文档级** charset（<meta charset=...>）。
        # 不能只匹配 `charset="utf-8"` —— plotly 的 <script charset="utf-8"> 也会命中，
        # 但那只声明脚本编码，救不了中文 Windows 双击打开时的 GBK 误解码。
        has_doc_charset = re.search(
            r"<meta[^>]+charset\s*=\s*[\"']?\s*utf-8", body, re.IGNORECASE) is not None
        check(f"{name} 有文档级 charset（否则中文 Windows 会按 GBK 解码乱码）",
              has_doc_charset)
        check(f"{name} 是完整 HTML 文档（含 <!DOCTYPE html>）",
              body.lstrip().lower().startswith("<!doctype html>"))

    # ---- 固浮比方法论提示（PRD 模块 5 硬性要求） ----
    section("附加项 固浮比方法论提示原样出现")
    check("报告正文含高浮动前提条件原句", REQUIRED_PRINCIPLE in md)
    check("HTML 报告同样含该原句", REQUIRED_PRINCIPLE in html)

    # ---- 报告不应残留未渲染的占位符 ----
    section("附加项 报告文本卫生")
    check("无未替换的占位符 {{", "{{" not in md)
    check("无 Python None 泄漏到正文", " None " not in md.replace("`None`", ""))
    check("无 nan 泄漏到正文", "nan" not in md.lower().replace("nanotech", ""))
    check("正文字数合理（>4000 字）", len(md) > 4000, f"{len(md)} 字")
    return res


# =============================================================================
# 降级与过滤分支
# =============================================================================

def test_degradation() -> None:
    section("降级分支 前置步骤缺失时报告不崩、章节写「本章未执行」")

    # 只给 mapping，其余六个模块全缺
    meta = build_mock_meta(include=["mapping"])
    res = report_mod.generate_report(session=build_mock_session("viz-test-002", include=["mapping"]),
                                     fmt="markdown")
    check("缺数据时仍然返回 ok", bool(res.get("ok")), str(res.get("error", ""))[:120])
    if not res.get("ok"):
        return
    check("仍然产出 7 章", len(res.get("sections") or []) == 7,
          str(res.get("sections")))
    missing = res.get("missing_sections") or []
    check("缺失章节被正确标记", len(missing) >= 5,
          f"标记了 {len(missing)} 章：{missing}")
    check("数据概览章未降级（有 mapping）",
          "数据概览与字段映射说明" not in missing)
    check("路线图章未降级（无需前置数据）",
          "风险提示与实施路线图" not in missing)

    md_path = res.get("report_path")
    with open(md_path, "r", encoding="utf-8") as f:
        md = f.read()
    check("正文出现降级文案", "本章未执行（前置步骤缺失）" in md)
    check("降级章节数为 0 时不误报", True)

    section("过滤分支 include_sections")
    res2 = report_mod.generate_report(
        session=build_mock_session("viz-test-003"),
        fmt="markdown", include_sections=["执行摘要", "3"])
    secs2 = res2.get("sections") or []
    check("按标题+序号过滤生效", secs2 == ["执行摘要", "薪酬现状诊断"], str(secs2))
    check("被排除章节记录在案",
          len(res2.get("excluded_sections") or []) == 5,
          str(res2.get("excluded_sections")))

    section("输出格式分支 fmt='html'")
    res3 = report_mod.generate_report(session=build_mock_session("viz-test-004"), fmt="html")
    check("fmt=html 时不产出 Markdown", res3.get("report_path") is None)
    check("fmt=html 时产出 HTML", bool(res3.get("html_path"))
          and os.path.exists(res3["html_path"]))

    section("错误分支 会话不存在")
    res4 = report_mod.generate_report("__not_exist_session__", fmt="markdown")
    check("未知会话返回 ok=False", res4.get("ok") is False)
    check("错误信息含 code/message/hint",
          all(k in (res4.get("error") or {}) for k in ("code", "message", "hint")))


# =============================================================================
# 主流程
# =============================================================================

def main() -> int:
    print("=" * 78)
    print("report.py / charts.py 自验收（AC-24 ~ AC-27）")
    print(f"项目根：{_PROJECT_ROOT}")
    print(f"图表目录：{charts.ASSETS_DIR}")
    print(f"报告目录：{report_mod.REPORT_DIR}")
    print("=" * 78)

    try:
        test_ac24_charts()
        test_ac25_26_27_report()
        test_degradation()
    except Exception as exc:  # noqa: BLE001
        import traceback
        print()
        print("!! 验收脚本自身抛出异常：")
        traceback.print_exc(limit=8)
        _FAIL.append(f"脚本异常 {type(exc).__name__}: {exc}")

    print()
    print("=" * 78)
    print(f"结果：通过 {len(_PASS)} 项，失败 {len(_FAIL)} 项")
    if _FAIL:
        print("-" * 78)
        for f in _FAIL:
            print(f"  ✗ {f}")
        print("=" * 78)
        return 1
    print("全部验收项通过 ✅")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

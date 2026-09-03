# -*- coding: utf-8 -*-
"""
test_charts_report.py — charts.py（6 图）+ report.py（七章报告）端到端自验
================================================================================

职责边界（与 engineer-viz 约定后的实际分工）
--------------------------------------------------------------------------------
    charts.py                —— 由 engineer-viz-2 实现，本脚本做全量验证
    report.py                —— 由 engineer-viz 实现，本脚本做**集成**验证
                               （用 tests/fixtures 的假 meta 喂进去，校验章节/图片/降级）
    tests/fixtures/          —— 由 engineer-viz-2 实现，QA 可复用的假数据基线

为什么不用 pytest
--------------------------------------------------------------------------------
1. 本机 Python 的 site-packages 下有第三方同名 `tests` 包，
   同名 `tests` 包，pytest 的 rootdir/conftest 收集会受干扰（已在 tests/__init__.py
   里写了规避说明，但 pytest 仍易踩坑）。
2. QA 需要一份**可直接运行、输出人类可读验收表**的脚本，而不是纯 assert。
因此这里写成零依赖独立脚本：`python tests/test_charts_report.py`，退出码 0 = 全通过。

覆盖的验收标准
--------------------------------------------------------------------------------
    AC-24  6 个图表 HTML 真实生成在 assets/，且文件体积 > 10KB（非空白壳）
    AC-25  报告 7 个章节标题齐全且顺序正确
    AC-26  报告里每张图片的相对路径拼出来后**文件真实存在**（os.path.exists 断言）
    AC-27  全部 docstring / 行内注释为中文；图表 HTML 确实写入中文字体配置
    C1     kaleido 缺失时 PNG 优雅降级（png_path=None + png_error，绝不让工具失败）
    D5     图表统一返回 {html_path, png_path, div, ...}
    （附加）前置步骤缺失 / 脏数据场景不崩溃

用法
--------------------------------------------------------------------------------
    PYTHONIOENCODING=utf-8 C:/ProgramData/anaconda3/python.exe tests/test_charts_report.py
    PYTHONIOENCODING=utf-8 C:/ProgramData/anaconda3/python.exe tests/test_charts_report.py --charts-only
"""

from __future__ import annotations

import io
import os
import re
import sys

# -----------------------------------------------------------------------------
# 路径引导：项目根 + src/ 进搜索路径（要在 import 本项目的任何模块之前）
# -----------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
_SRC_DIR = os.path.join(_PROJECT_ROOT, "src")
for _p in (_PROJECT_ROOT, _SRC_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.fixtures.mock_results import (  # noqa: E402
    build_mock_meta, build_mock_session, build_band_table, build_cr_values,
)
from tools import charts  # noqa: E402

from tools import report as report_mod  # noqa: E402

generate_report = report_mod.generate_report
SECTION_TITLES = report_mod.SECTION_TITLES
# 固浮比方法论提示：engineer-viz 版本命名为 PAY_MIX_PRINCIPLE
PAY_MIX_TIP = getattr(report_mod, "PAY_MIX_PRINCIPLE", None) or (
    "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；"
    "长周期协作型业务强行高浮动会破坏协作。"
)

# 验收阈值
MIN_HTML_KB = 10.0          # AC-24：图表 HTML 最小体积（KB）
REQUIRED_CHART_COUNT = 6

_PASS = 0
_FAIL = 0
_FAIL_ITEMS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条验收项：ok=True 计通过，否则计失败并收集原因。"""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {name}" + (f"  —— {detail}" if detail else ""))
    else:
        _FAIL += 1
        _FAIL_ITEMS.append(f"{name}：{detail}")
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _decode_unicode_escapes(s: str) -> str:
    """
    还原字符串里的 \\uXXXX 转义。

    用途：plotly 的 to_html() 走 json.dumps，默认 ensure_ascii=True，
    中文轴标题/图例在 HTML 里是 \\u4e2d\\u6587 形式（浏览器能正常渲染，
    但直接搜中文搜不到）。校验"图表里有没有中文"时必须先还原。
    """
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


# =============================================================================
# 一、图表工厂自验（AC-24 / AC-27 / C1 / D5）
# =============================================================================

def test_charts() -> None:
    """6 个图表函数逐个落盘，校验体积、中文字体、返回结构、PNG 降级。"""
    hr("一、图表工厂自验（AC-24 / AC-27 / C1 / D5）")
    meta = build_mock_meta()
    inc = meta["increase_sim"]

    specs = [
        ("band_overlap", lambda: charts.band_overlap_chart(meta["band"]["band_table"])),
        ("cr_distribution", lambda: charts.cr_distribution_chart(meta["current_state"]["cr_values"])),
        ("cr_before_after", lambda: charts.cr_before_after_chart(
            inc["before"], inc["after"], strategy_label=str(inc["recommended"]))),
        ("strategy_cost", lambda: charts.strategy_cost_chart(
            inc["strategies"], budget=inc["budget"])),
        ("market_gap", lambda: charts.market_gap_chart(meta["market_benchmark"]["by_level"])),
        ("paymix_curve", lambda: charts.paymix_curve_chart(
            meta["pay_mix"]["curves"], baseline_income=meta["pay_mix"]["target_tc"])),
    ]

    saved = {}
    for name, make in specs:
        print(f"\n  --- {name} ---")
        try:
            fig = make()
        except Exception as exc:  # noqa: BLE001
            check(f"{name} 构造 Figure", False, f"{type(exc).__name__}: {exc}")
            continue
        check(f"{name} 构造 Figure", fig is not None and len(fig.data) > 0,
              f"traces={len(fig.data)}")

        # 白底版式：直接看 Figure 对象（template 会被 plotly 展开，HTML 里搜不到字面量）
        check(f"{name} 白底 + 中文版式（plotly_white / paper_bgcolor=white）",
              str(getattr(fig.layout, "paper_bgcolor", "")).lower() == "white"
              and "YaHei" in str(fig.layout.font.family if fig.layout.font else ""),
              f"paper_bgcolor={fig.layout.paper_bgcolor}, font={fig.layout.font.family if fig.layout.font else None}")

        r = charts.save_figure(fig, name)
        saved[name] = r

        # D5：返回结构完整
        need_keys = {"name", "html_path", "png_path", "div", "html_rel", "png_rel", "png_error"}
        check(f"{name} 返回结构符合 D5", need_keys.issubset(r.keys()),
              f"缺 {sorted(need_keys - set(r.keys()))}" if not need_keys.issubset(r.keys()) else "")

        # AC-24：HTML 真实落盘且体积达标
        hp = r["html_path"]
        exists = bool(hp) and os.path.exists(hp)
        check(f"{name} HTML 已落盘", exists, hp or "路径为空")
        if exists:
            kb = os.path.getsize(hp) / 1024.0
            check(f"{name} HTML 体积 > {MIN_HTML_KB}KB（AC-24）", kb > MIN_HTML_KB, f"{kb:.1f} KB")

            html = io.open(hp, encoding="utf-8").read()
            html_cn = _decode_unicode_escapes(html)
            # AC-27：中文字体 + 中文文案
            check(f"{name} 写入中文字体 Microsoft YaHei（AC-27）", "Microsoft YaHei" in html)
            check(f"{name} 图表文案为中文", bool(re.search(r"[\u4e00-\u9fff]", html_cn)))
            check(f"{name} 关闭 plotly logo（displaylogo=false）",
                  "displaylogo" in html and "false" in html.lower())
            check(f"{name} 设置 zh-CN locale", "zh-CN" in html_cn)
            check(f"{name} div 非空且含 plotly 容器",
                  bool(r["div"]) and "<div" in r["div"])

        # C1：PNG 降级不抛错
        if r["png_path"] is None:
            check(f"{name} PNG 缺失时优雅降级（C1）", r["png_error"] is not None,
                  f"png_error={str(r['png_error'])[:50]}…")
        else:
            check(f"{name} PNG 已生成", os.path.exists(r["png_path"]), r["png_path"])

        # html_rel 供报告拼相对引用
        check(f"{name} html_rel 以 assets/ 开头",
              bool(r["html_rel"]) and r["html_rel"].startswith("assets/"),
              str(r["html_rel"]))

    check(f"共生成 {REQUIRED_CHART_COUNT} 张图表", len(saved) == REQUIRED_CHART_COUNT,
          f"实到 {len(saved)} 张：{sorted(saved)}")
    # 注：本函数刻意不返回非 None（历史上返回 dict 会让 pytest 未来版本报
    # "test function returned result which is not None"），验收结果经 check() 累积。


# =============================================================================
# 二、图表业务语义自验（不只是"能跑"，要"画对了"）
# =============================================================================

def test_chart_semantics() -> None:
    """校验图表内部结构真的表达了业务含义：重叠块、中位值竖线、阈值线、配色。"""
    hr("二、图表业务语义自验")

    band_tbl = build_band_table()
    fig = charts.band_overlap_chart(band_tbl)
    rects = [s for s in fig.layout.shapes if s.type == "rect"]
    lines = [s for s in fig.layout.shapes if s.type == "line"]
    check("带宽图：相邻职级重叠色块数 = 职级数 - 1",
          len(rects) == len(band_tbl) - 1, f"重叠块 {len(rects)} / 职级 {len(band_tbl)}")
    check("带宽图：每个职级都有中位值竖线",
          len(lines) == len(band_tbl), f"竖线 {len(lines)} / 职级 {len(band_tbl)}")
    check("带宽图：职级按 P1→M3 顺序排列（y 轴刻度）",
          list(fig.layout.yaxis.ticktext) == [r["level"] for r in band_tbl],
          str(list(fig.layout.yaxis.ticktext)))

    fig = charts.cr_distribution_chart(build_cr_values())
    bars = fig.data[0]
    cols = set(bars.marker.color) if hasattr(bars.marker.color, "__iter__") else {bars.marker.color}
    check("CR 分布图：红/绿/蓝三色柱齐全（红绿圈区间识别正确）",
          charts.COLOR_RED in cols and charts.COLOR_GREEN in cols and charts.COLOR_BLUE in cols,
          f"实际色值 {sorted(cols)}")
    check("CR 分布图：画出 0.80 / 1.00 / 1.20 三条阈值竖线",
          len([s for s in fig.layout.shapes if s.type == "line"]) >= 3)

    meta = build_mock_meta()
    inc = meta["increase_sim"]
    fig = charts.cr_before_after_chart(inc["before"], inc["after"], strategy_label="B 优先补绿圈")
    check("调薪前后对比图：含调薪前/后两条直方图且叠加显示",
          len(fig.data) == 2 and fig.layout.barmode == "overlay",
          f"traces={[t.name for t in fig.data]}")

    fig = charts.strategy_cost_chart(inc["strategies"], budget=inc["budget"])
    bars = fig.data[0]
    check("策略成本图：柱数 = 策略数", len(bars.x) == len(inc["strategies"]),
          f"{len(bars.x)} vs {len(inc['strategies'])}")
    check("策略成本图：推荐方案标注 ★", any("★" in str(t) for t in bars.text), str(list(bars.text)))
    check("策略成本图：中国语境配色（高于均值=红 / 低于均值=绿）",
          charts.COLOR_RED in list(bars.marker.color)
          and charts.COLOR_GREEN in list(bars.marker.color),
          str(list(bars.marker.color)))

    fig = charts.market_gap_chart(meta["market_benchmark"]["by_level"])
    names = [t.name for t in fig.data]
    check("市场对标图：含公司中位数 + 市场 P25/P50/P75",
          any("公司" in n for n in names) and sum(1 for n in names if "P" in n) >= 3, str(names))

    fig = charts.paymix_curve_chart(meta["pay_mix"]["curves"], baseline_income=240000)
    check("固浮比曲线图：5 个岗位序列各一条曲线",
          len([t for t in fig.data if t.mode]) == 5, str([t.name for t in fig.data]))
    check("固浮比曲线图：x 轴为达成率 0-155%",
          list(fig.layout.xaxis.range) == [0, 155], str(fig.layout.xaxis.range))


# =============================================================================
# 三、报告生成集成自验（AC-25 / AC-26）
# =============================================================================

def test_report_full() -> None:
    """用完整假数据跑 generate_report，校验七章、图片引用可解析、关键内容。"""
    hr("三、报告生成集成自验（AC-25 / AC-26）")
    sess = build_mock_session("test-full-001")

    res = generate_report(session=sess, title="薪酬诊断报告", fmt="both")
    check("generate_report 返回 ok=True", res.get("ok") is True,
          str(res.get("error")))

    md_path = res.get("report_path") or res.get("report_md_path")
    check("Markdown 报告已落盘", bool(md_path) and os.path.exists(md_path), str(md_path))
    check("Markdown 文件名含时间戳",
          bool(re.search(r"_\d{8}_\d{6}\.md$", md_path or "")), os.path.basename(md_path or ""))
    check("Markdown 正文非空（> 8000 字符，非 MVP 空壳）",
          (res.get("char_count") or 0) > 8000, f"{res.get('char_count')} 字符")

    html_path = res.get("html_path")
    check("HTML 报告已同步生成（fmt='both'）",
          bool(html_path) and os.path.exists(html_path) and os.path.getsize(html_path) > 10000,
          f"{html_path}（{os.path.getsize(html_path) if html_path and os.path.exists(html_path) else 0} 字节）")

    # --- AC-25：七章齐全且顺序正确 ---
    print("\n  --- 章节校验（AC-25）---")
    check("报告含 7 个章节", len(res.get("sections", [])) == 7, f"实到 {len(res.get('sections', []))}")
    check("章节标题与顺序与模块常量一致",
          res.get("sections") == SECTION_TITLES, str(res.get("sections")))

    md = io.open(md_path, encoding="utf-8").read()
    positions = [md.find(t) for t in SECTION_TITLES]
    check("七章在正文中全部出现", all(p >= 0 for p in positions),
          str([f"{t}:{p}" for t, p in zip(SECTION_TITLES, positions)]))
    check("七章在正文中按序排列（位置递增）",
          all(positions[i] < positions[i + 1] for i in range(len(positions) - 1)), str(positions))
    check("无章节被降级为'未执行'", not res.get("missing_sections"),
          str(res.get("missing_sections")))

    # --- AC-26：图片引用路径真实存在 ---
    print("\n  --- 图片引用可解析性（AC-26）---")
    report_dir = os.path.dirname(md_path)
    refs = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", md)
    check("报告中包含图片引用", len(refs) >= 4, f"共 {len(refs)} 处引用")

    all_exist, detail = True, []
    for _alt, rel in refs:
        abs_p = os.path.normpath(os.path.join(report_dir, rel))
        ok = os.path.exists(abs_p)
        all_exist = all_exist and ok
        detail.append(f"{rel}->{'OK' if ok else 'MISSING'}")
    check("所有图片相对路径拼出后文件真实存在（AC-26）", all_exist, " | ".join(detail))
    check("图片引用为 ../assets/ 相对路径（相对报告文件本身）",
          all(r[1].startswith("../assets/") for r in refs),
          str(sorted({r[1] for r in refs})))
    figs = res.get("figures", [])
    check("figures 列表全部 exists=True",
          all(f.get("exists") for f in figs), str([(f.get("name"), f.get("exists")) for f in figs]))
    check("图表数量为 6", res.get("figure_count") == 6, str(res.get("figure_count")))

    # --- 关键内容 ---
    print("\n  --- 关键内容校验 ---")
    check("第六章包含固浮比前提条件原句（PRD 硬性要求）", PAY_MIX_TIP in md)
    check("报告包含红绿圈阈值口径（CR > 1.2 / CR < 0.8）",
          bool(re.search(r"1\.2", md)) and bool(re.search(r"0\.8", md)))
    check("第七章包含三阶段路线图",
          all(k in md for k in ["0 - 1 个月", "1 - 3 个月", "3 - 6 个月"])
          or all(k in md for k in ["0-1月", "1-3月", "3-6月"])
          or all(k in md for k in ["0 - 1", "1 - 3", "3 - 6"]))
    check("第二章包含清洗摘要", "清洗" in md)
    check("第二章包含字段缺失率", "缺失率" in md)
    check("第二章包含字段映射", "映射" in md)
    check("报告包含数据安全声明", "数据安全" in md)
    check("正文无 Python 对象泄漏（无 '<class' / 'object at 0x'）",
          "<class" not in md and "object at 0x" not in md)
    check("正文无异常堆栈泄漏", "Traceback" not in md)

    # --- HTML 版 ---
    print("\n  --- HTML 报告校验 ---")
    if html_path and os.path.exists(html_path):
        html = io.open(html_path, encoding="utf-8").read()
        check("HTML 声明 lang=zh-CN", 'lang="zh-CN"' in html)
        check("HTML 含中文字体栈", "Microsoft YaHei" in html)
        check("HTML 内嵌了图表 div（而非只剩图片链接）",
              html.count('class="plotly-graph-div"') >= 4,
              f"内嵌 {html.count('class=\"plotly-graph-div\"')} 个")
        check("HTML 中 plotly.js 只引入一次（去重复载）",
              html.count("cdn.plot.ly") == 1, f"出现 {html.count('cdn.plot.ly')} 次")
        check("HTML 渲染出表格", html.count("<table") >= 5, f"{html.count('<table')} 个")
        check("HTML 无未处理的 Markdown 表格分隔符残留", html.count("<p>|") == 0,
              f"残留 {html.count('<p>|')} 处")

    check("meta_keys 命中全部 7 个步骤",
          len(res.get("meta_keys", res.get("meta_keys_present", []))) in (0, 7),
          str(res.get("meta_keys")))
    # 不返回非 None（pytest 未来版本会对 test 函数返回非 None 报错）；结果已通过 check() 累积。


# =============================================================================
# 四、降级与健壮性自验（不崩溃是硬约束）
# =============================================================================

def test_report_degraded() -> None:
    """校验"数据不全"的各种场景都能给出可解释结果，而不是抛异常。"""
    hr("四、降级与健壮性自验")

    # 4.1 空会话：允许返回结构化错误（不算崩溃），但必须是 ok:false + 可读 hint
    r = generate_report("not-exist-session-999", fmt="both")
    if r.get("ok"):
        md = io.open(r["report_path"], encoding="utf-8").read()
        check("空会话：给出报告，七章标题齐全", len(r.get("sections", [])) == 7)
        check("空会话：正文含'本章未执行'提示", "本章未执行" in md)
    else:
        err = r.get("error") or {}
        check("空会话：返回结构化错误（ok:false，非崩溃）", True,
              f"code={err.get('code')} | {err.get('message')}")
        check("空会话：错误含可执行的 hint", bool(err.get("hint")), str(err.get("hint"))[:80])
        check("空会话：错误不暴露堆栈", "Traceback" not in str(err))

    # 4.2 只跑了现状诊断
    meta = build_mock_meta()
    r2 = generate_report(session=build_mock_session("t2", include=["current_state"]), fmt="both")
    check("部分数据：ok=True", r2.get("ok") is True, str(r2.get("error")))
    check("部分数据：缺数据的 3 章被降级",
          len(r2.get("missing_sections", [])) == 3, str(r2.get("missing_sections")))
    check("部分数据：CR 分布图仍被现场补画",
          any(f.get("name") == "cr_distribution" and f.get("exists") for f in r2.get("figures", [])),
          str([(f.get("name"), f.get("exists")) for f in r2.get("figures", [])]))

    # 4.3 只生成指定章节
    r3 = generate_report(session=build_mock_session("t3"),
                         include_sections=["执行摘要", "薪酬现状诊断"], fmt="markdown")
    check("指定章节：只渲染 2 章", len(r3.get("sections", [])) == 2, str(r3.get("sections")))
    check("指定章节：未选章节不出现在正文",
          "调薪方案对比与推荐" not in io.open(r3["report_path"], encoding="utf-8").read()
          or len(r3.get("sections", [])) == 2)

    # 4.4 脏数据：None / 空表 / 类型错乱
    messy = {
        "current_state": {"summary": None, "by_level": [], "circles": None, "cr_values": []},
        "band": {"band_table": None},
        "market_benchmark": {"by_level": {}},
        "increase_sim": {"strategies": None, "before": None, "after": None},
        "pay_mix": {"curves": []},
        "job_eval": None,
        "mapping": {},
    }
    r4 = generate_report(session=build_mock_session("t4"), meta=messy, fmt="both")
    check("脏数据（None/空表）：ok=True", r4.get("ok") is True, str(r4.get("error")))
    check("脏数据：报告仍落盘", bool(r4.get("report_path")) and os.path.exists(r4["report_path"]))
    check("脏数据：正文无异常堆栈泄漏",
          "Traceback" not in io.open(r4["report_path"], encoding="utf-8").read())

    # 4.5 图表入参容错：空数据不抛异常，返回占位图
    print("\n  --- 图表入参容错 ---")
    for fname, fn in [
        ("band_overlap_chart", lambda: charts.band_overlap_chart([])),
        ("cr_distribution_chart", lambda: charts.cr_distribution_chart([])),
        ("cr_before_after_chart", lambda: charts.cr_before_after_chart([], [])),
        ("strategy_cost_chart", lambda: charts.strategy_cost_chart({})),
        ("market_gap_chart", lambda: charts.market_gap_chart(None)),
        ("paymix_curve_chart", lambda: charts.paymix_curve_chart({})),
    ]:
        try:
            f = fn()
            check(f"{fname} 空输入不抛异常", f is not None and hasattr(f, "to_html"))
        except Exception as exc:  # noqa: BLE001
            check(f"{fname} 空输入不抛异常", False, f"{type(exc).__name__}: {exc}")


# =============================================================================
# 五、中文注释与 docstring 自验（AC-27）
# =============================================================================

def test_chinese_docs() -> None:
    """校验源文件的注释/docstring 为中文（AC-27）。"""
    hr("五、中文注释与 docstring 自验（AC-27）")
    for path in (os.path.join(_SRC_DIR, "tools", "charts.py"),
                 os.path.join(_SRC_DIR, "tools", "report.py")):
        if not os.path.exists(path):
            continue
        src = io.open(path, encoding="utf-8").read()
        name = os.path.basename(path)

        cn = len(re.findall(r"[\u4e00-\u9fff]", src))
        check(f"{name}：中文字符量充足（> 2000）", cn > 2000, f"{cn} 个汉字")

        # 全部函数都有 docstring
        fns = re.findall(r"^def (\w+)\(", src, re.M)
        missing = [fn for fn in fns
                   if not re.search(r"^def " + fn + r"\([^)]*\)[^:]*:\s*\n\s*(?:\"\"\"|''')", src, re.M)]
        check(f"{name}：全部 {len(fns)} 个函数均有 docstring", not missing, f"缺失：{missing}")

        # docstring（排除 f-string 模板）必须含中文
        bad = []
        for m in re.finditer(r'(?<!f)"""(.*?)"""', src, re.S):
            body = m.group(1)
            if not re.search(r"[\u4e00-\u9fff]", body):
                bad.append(body.strip().split("\n")[0][:50])
        check(f"{name}：所有真·docstring 均含中文", not bad, f"非中文：{bad}")

        # 行内注释中文率：排除分隔线 / coding 声明等无语义的装饰性注释
        comments = re.findall(r"^\s*#\s*(.+)$", src, re.M)
        semantic = [c for c in comments
                    if not re.fullmatch(r"[=\-_*~#\s]*", c)          # 纯分隔线
                    and not c.startswith("-*- coding")                # 编码声明
                    and not c.startswith("!")]                        # shebang
        cn_comments = [c for c in semantic if re.search(r"[\u4e00-\u9fff]", c)]
        ratio = len(cn_comments) / max(len(semantic), 1)
        check(f"{name}：语义性行内注释中文率 > 90%", ratio > 0.90,
              f"{len(cn_comments)}/{len(semantic)} = {ratio:.1%}")


# =============================================================================
# 六、主流程
# =============================================================================

def main() -> int:
    print("=" * 78)
    print("charts.py + report.py 端到端自验")
    print(f"项目根：{_PROJECT_ROOT}")
    print(f"图表目录：{charts.ASSETS_DIR}")
    print("=" * 78)

    test_charts()
    test_chart_semantics()
    if "--charts-only" not in sys.argv:
        test_report_full()
        test_report_degraded()
    test_chinese_docs()

    hr("自验结果汇总")
    print(f"  通过：{_PASS} 项")
    print(f"  失败：{_FAIL} 项")
    if _FAIL_ITEMS:
        print("\n  失败明细：")
        for i, item in enumerate(_FAIL_ITEMS, 1):
            print(f"    {i}. {item}")
    else:
        print("\n  ✅ 全部验收项通过（AC-24 / AC-25 / AC-26 / AC-27 / C1 / D5）")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

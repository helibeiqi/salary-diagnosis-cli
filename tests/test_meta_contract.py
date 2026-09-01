# -*- coding: utf-8 -*-
"""
test_meta_contract.py —— 会话 meta 分区键契约回归测试
================================================================================

## 它防的是哪一类 bug

2026-08-30 实测：工具链九步全部返回 OK，金标准测试全绿，而生成的七章报告里
六章是空的 —— 因为六个写入模块写的是 `diagnose / market / increase / paymix /
jobeval / band`，而 `report.py` 读的是 `current_state / market_benchmark /
increase_sim / pay_mix / job_eval / band`，五个键全对不上。

这类 bug 有三个特征，决定了它必须靠**端到端行为测试**来拦：

1. **单元级测不出来**：每个工具的返回值都对，错的是「A 写的键 B 不认识」；
2. **静默降级**：report 对缺失分区的处理是打印「本章未执行」，不报错、不抛异常，
   于是"生成成功"和"生成了一堆空壳"在返回码上无法区分；
3. **只在集成时显形**：串行开发时各模块自测都过，一拼起来才发现对不上。

所以本测试做的是：**真跑一遍完整链路 → 断言每个契约分区非空 → 断言报告正文里
既不出现「本章未执行」也不出现「生成异常」→ 抽样断言关键数字确实落到了纸上**。
它不关心任何模块的内部实现，只关心"最后那份报告里有没有真东西"。

## 与 report_adapter 的关系

`report_adapter.py` 负责把写入方的字段名翻译成报告方的词汇；本测试负责盯住
这条翻译链**持续有效**。两边缺一不可：
- 只有适配层没有测试 → 上游再改键名，报告又会静默变空；
- 只有测试没有适配层 → 红灯会亮，但每次都要人去改字段名。
"""

from __future__ import annotations

import os
import sys
import shutil
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.tools import registry                       # noqa: E402
from src.tools.schemas import META_SECTION_KEYS      # noqa: E402

SAMPLE = os.path.join(PROJECT_ROOT, "data", "sample_salary.csv")


@pytest.fixture(autouse=True)
def _isolated_store():
    """每个用例用独立的临时状态目录，避免与其他测试共享 .state/ 造成的串扰。

    本测试直接断言「完整链路跑出来的报告非空、关键数字落纸」，对会话存储的
    状态极其敏感；共享单例一旦被别的测试的 reset_store / 会话残留影响，就会
    误判报告为空。隔离后无论单机还是整套跑，结论都稳定。
    """
    from src.tools.session import reset_store
    tmp = tempfile.mkdtemp(prefix="meta_contract_")
    reset_store(tmp)
    yield
    shutil.rmtree(tmp, ignore_errors=True)
    reset_store()  # 还原默认单例，避免影响后续测试


# -----------------------------------------------------------------------------
# 夹具：跑完整链路，返回 (session_id, meta, report_md_text)
# -----------------------------------------------------------------------------

def _run_pipeline():
    """按需求文档的九段式跑一遍，返回 (sid, meta, 报告正文)。"""
    env = registry.call_tool("load_salary_data", {"file_path": SAMPLE})
    assert env.get("ok"), f"load_salary_data 失败：{env.get('code')} {env.get('message')}"
    sid = env["meta"]["session_id"]

    suggested = env["data"]["suggested_mapping"]
    mapping = {
        raw: info["suggest"]
        for raw, info in suggested.items()
        if info.get("suggest") and float(info.get("confidence") or 0) >= 60
    }
    env = registry.call_tool("confirm_mapping",
                             {"session_id": sid, "mapping": mapping})
    assert env.get("ok"), f"confirm_mapping 失败：{env.get('code')}"

    steps = [
        ("generate_band", {"mode": "optimize"}),
        ("analyze_current_state", {}),
        ("market_benchmark", {}),
        ("simulate_increase", {"budget_pct": 0.05, "strategy": "C"}),
        ("simulate_pay_mix", {}),
        ("calc_job_score", {"model": "hay"}),
    ]
    for name, args in steps:
        r = registry.call_tool(name, {"session_id": sid, **args})
        assert r.get("ok"), f"{name} 失败：{r.get('code')} {r.get('message')}"

    r = registry.call_tool("generate_report",
                           {"session_id": sid, "formats": ["md"]})
    assert r.get("ok"), f"generate_report 失败：{r.get('code')} {r.get('message')}"

    md_path = r["data"]["report_path"]
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    from src.tools.session import get_store
    return sid, get_store().get_meta(sid), text, r["data"]


# -----------------------------------------------------------------------------
# 用例
# -----------------------------------------------------------------------------

def test_all_meta_sections_written():
    """契约表里登记的分区，跑完链路后必须都存在且非空。"""
    _, meta, _, _ = _run_pipeline()
    missing = [k for k in META_SECTION_KEYS if not meta.get(k)]
    assert not missing, (
        f"以下 meta 分区未写入或为空：{missing}。"
        f"若某模块改了写入的键名，请同步改 schemas.META_SECTION_KEYS "
        f"与 report.py —— 这正是本测试要拦的静默错位。"
    )


def test_no_empty_or_failed_section_in_report():
    """报告正文里不得出现「本章未执行」或「生成异常」。

    这是整个契约的最终判据：**工具全 OK 而报告是空壳** 的状态在这里无处可藏。
    """
    _, _, text, _ = _run_pipeline()
    assert "本章未执行" not in text, (
        "报告存在空章节 —— 通常是 meta 分区键漂移，或 report_adapter 未覆盖新字段。"
        "请检查各章节的降级文案触发条件。")
    assert "生成异常" not in text, (
        "报告存在章节级异常（章节函数抛错被兜底成一段提示文字）。"
        "异常详情就在正文中，请按其中的 traceback 定位。")


def test_report_figures_present():
    """报告应至少嵌入 1 张图表（图表是这份作品集的主视觉）。"""
    _, _, text, data = _run_pipeline()
    assert data.get("figure_count", 0) >= 1, "报告一张图都没有，图表链路可能断了"
    assert text.count("![") >= 1, "报告正文未嵌入任何图片引用"


def test_key_numbers_rendered():
    """关键数字必须真的落到纸上，而不是显示成「—」。

    只挑 4 个不会随造数随机种子漂移的**结构性**断言：
    样本量、红绿圈人数、市场整体差距、调薪预算。
    """
    _, meta, text, _ = _run_pipeline()

    counts = meta["diagnose"]["counts"]
    red, green = counts["红圈"], counts["绿圈"]

    assert f"{red} 人" in text or f"| {red} |" in text, (
        f"执行摘要里找不到红圈人数 {red}")
    assert f"{green} 人" in text or f"| {green} |" in text, (
        f"执行摘要里找不到绿圈人数 {green}")

    # 市场整体差距取 mean（AC-15 口径 −3.23%），报告里以百分数呈现
    gap = meta["market"]["overall_gap"]["mean"]
    gap_txt = f"{gap * 100:.1f}%"
    assert gap_txt in text, f"报告里找不到市场整体差距 {gap_txt}"

    # 调薪预算：AC-19 三档的中档 1,958,520（5%）
    budget = meta["increase"]["budget_amount"]
    assert f"{budget / 10000:,.1f} 万元" in text, (
        f"报告里找不到调薪预算 {budget}")


def test_exec_summary_not_all_dashes():
    """执行摘要表格里「—」占比过高即判定取数失败。

    单看某几行是否为「—」容易漏，这里用**比例**兜底：
    一页 13 行的关键数字表，如果有 1/3 以上取不到，这块基本是废的。
    """
    _, _, text, _ = _run_pipeline()
    head = text.split("## 二、")[0]
    lines = [ln for ln in head.splitlines() if ln.startswith("|")]
    # 只看数据行：跳过表头（| 指标 | 数值 | ...）与分隔行（| :--- | ---: |）
    rows = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 2 and set(cells[1]) <= set(":- "):
            continue        # 分隔行
        if cells[0] in ("指标", ""):
            continue        # 表头
        rows.append(cells)

    assert rows, "执行摘要区没有解析到任何数据行"
    # 「数值」列 = 第 2 列（索引 1）
    dash = sum(1 for c in rows if c[1] == "—")
    assert dash <= len(rows) // 3, (
        f"执行摘要 {len(rows)} 行里有 {dash} 行取不到值（数值列显示「—」），"
        f"超过 1/3，判定为取数链路失效而非个别字段缺失。")


if __name__ == "__main__":  # pragma: no cover
    for fn in (test_all_meta_sections_written,
               test_no_empty_or_failed_section_in_report,
               test_report_figures_present,
               test_key_numbers_rendered,
               test_exec_summary_not_all_dashes):
        fn()
        print(f"✓ {fn.__name__}")
    print("\nmeta 契约回归测试全部通过")

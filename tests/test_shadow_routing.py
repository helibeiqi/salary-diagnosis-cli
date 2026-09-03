# -*- coding: utf-8 -*-
"""
test_shadow_routing.py —— Phase 4 影子路由 + Golden 评分回归（自主优化架构师）
================================================================================

防的是哪一类 bug
--------------------------------------------------------------------------------
Phase 4 是"系统自我进化"的开关层，但它**碰都不能碰真实薪资路径**。本文件逐条钉死：

1. **红线**：``classification=="real"`` 时，无论 mapping/narration 子任务，
   路由结果必须是 baseline，且 ``gated=True``（绝不晋升/切候选/上云）。
2. **子任务边界**：narration 子任务即便已晋升也永远 baseline（大模型语义）。
3. **晋升门控**：仅 sanitized/simulated 分级下，已晋升的 mapping 才走候选；
   unknown/none 保守走 baseline。
4. **晋升资格**：候选在 ≥1000 次影子执行上满足 准降≤2% & 成本↓≥50% & 延迟↓≥40%
   → 自动晋升；否则不晋升。
5. **异常回滚**：候选准确率骤降 → 自动回滚 baseline + 写告警。
6. **Golden 评分**：完美预测命中率=1.0、全错=0.0；composite 随延迟/成本下降。

运行方式（刻意保留无 pytest 模式，与 test_column_mapping.py 同口径）
--------------------------------------------------------------------------------
    pytest tests/test_shadow_routing.py -v
    python tests/test_shadow_routing.py          # 无 pytest 也能跑
"""
from __future__ import annotations

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ★ 隔离状态落盘：测试把 .state 指向临时目录，绝不污染真实仓库的 shadow_router.json
_TMP = tempfile.mkdtemp(prefix="shadow_test_")
os.environ["COMP_STATE_DIR"] = _TMP

from src.tools.golden import GoldenSet                                   # noqa: E402
from src.tools.shadow_router import ShadowRouter, _DEF, _RECENT_N         # noqa: E402

try:
    import pytest
except ImportError:  # 独立部署环境可能没有 pytest
    pytest = None  # type: ignore[assignment]


# =============================================================================
# Golden 评分
# =============================================================================
def _test_golden_scoring() -> int:
    print("\n--- Golden 集评分 ---")
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    gs = GoldenSet.from_repo()
    check("Golden 集非空", len(gs.cases) > 0)
    print(f"    样本数={len(gs.cases)}，歧义列={len(gs.ambiguous_columns())}")

    perfect = {c.raw_column: c.expected for c in gs.cases}
    wrong = {c.raw_column: ("name" if c.expected != "name" else "emp_id")
             for c in gs.cases}
    partial = dict(perfect)
    if gs.ambiguous_columns():
        partial[gs.ambiguous_columns()[0]] = "name"  # 故意错一个歧义列

    s_perfect = gs.score(perfect)
    s_wrong = gs.score(wrong)
    s_partial = gs.score(partial)

    check("完美预测命中率=1.0", abs(s_perfect.mapping_hit_rate - 1.0) < 1e-9)
    check("完美预测歧义召回=1.0", abs(s_perfect.ambiguity_recall - 1.0) < 1e-9)
    check("全错预测命中率=0.0", abs(s_wrong.mapping_hit_rate - 0.0) < 1e-9)

    # composite：延迟/成本越低分越高；同准确率下更快更便宜 → 高分
    c_fast = s_perfect.composite(latency_ms=600, cost_usd=0.001)
    c_slow = s_perfect.composite(latency_ms=1200, cost_usd=0.004)
    check("更快更便宜 → composite 更高", c_fast > c_slow)

    # partial 的命中率应介于 0 与 1 之间，且低于 perfect
    check("partial 命中率 < 完美",
          s_partial.mapping_hit_rate < s_perfect.mapping_hit_rate)
    check("partial 歧义召回 < 1（错了一个歧义列）",
          s_partial.ambiguity_recall < 1.0)
    print(f"    完美/全错/部分 hit="
          f"{s_perfect.mapping_hit_rate:.3f}/{s_wrong.mapping_hit_rate:.3f}/"
          f"{s_partial.mapping_hit_rate:.3f}  "
          f"recall={s_perfect.ambiguity_recall:.3f}/{s_wrong.ambiguity_recall:.3f}/"
          f"{s_partial.ambiguity_recall:.3f}")
    return fails


# =============================================================================
# 影子路由门控
# =============================================================================
def _test_routing_gates() -> int:
    print("\n--- 影子路由分级门控 ---")
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    r = ShadowRouter(_DEF)
    r.reset_state()

    # 红线：real 分级任何子任务 → baseline + gated
    d = r.route("mapping", "real")
    check("real/mapping → baseline & gated", d["provider"] == r.baseline and d["gated"])
    d = r.route("narration", "real")
    check("real/narration → baseline", d["provider"] == r.baseline)

    # narration 永远 baseline（即便晋升）
    r._state["promoted"] = True
    d = r.route("narration", "simulated")
    check("narration 即便晋升也 baseline", d["provider"] == r.baseline and not d["promoted"])

    # 未晋升 mapping → baseline
    r._state["promoted"] = False
    d = r.route("mapping", "simulated")
    check("未晋升 mapping → baseline", d["provider"] == r.baseline)

    # 晋升后 simulated mapping → 候选；real 仍 baseline
    r._state["promoted"] = True
    d = r.route("mapping", "simulated")
    check("晋升后 simulated/mapping → 候选", d["provider"] == r.candidate and d["promoted"])
    d = r.route("mapping", "real")
    check("晋升后 real/mapping → 强制 baseline", d["provider"] == r.baseline and d["gated"])

    # unknown 保守：不路由候选
    d = r.route("mapping", "unknown")
    check("unknown 分级不路由候选", d["provider"] == r.baseline)
    return fails


# =============================================================================
# 晋升资格 + 回滚
# =============================================================================
def _test_promotion() -> int:
    print("\n--- 晋升资格与异常回滚 ---")
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    # 满足四项阈值 → 自动晋升
    r = ShadowRouter(_DEF)
    r.reset_state()
    n = int(_DEF["promotion"]["min_shadow_runs"])
    for _ in range(n):
        r.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.95,      # 仅低1% < 2%
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,  # ↓50%
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,  # ↓75%
            "classification": "simulated",
        })
    st = r.show_state()
    check("满足阈值后自动晋升", st["promoted"] is True)
    check("晋升权重 mapping→candidate", st["weights"]["mapping"]["candidate"] == 1.0)

    # 异常回滚：候选骤降（近窗口持续劣化）。需喂满近窗口才触发，避免单点抖动误伤
    for _ in range(_RECENT_N):
        r.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.80,
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,
            "classification": "simulated",
        })
    st = r.show_state()
    check("候选骤降触发回滚", st["promoted"] is False)
    check("回滚权重复位", st["weights"]["mapping"]["baseline"] == 1.0)
    check("回滚写告警", st.get("last_alert") is not None)

    # 不足次数不晋升
    r2 = ShadowRouter(_DEF)
    r2.reset_state()
    for _ in range(10):
        r2.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.95,
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,
            "classification": "simulated",
        })
    check("次数不足不晋升", r2.show_state()["promoted"] is False)

    # 代价未达标不晋升（成本仅降 10% < 50%）
    r3 = ShadowRouter(_DEF)
    r3.reset_state()
    for _ in range(n):
        r3.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.955,
            "baseline_latency_ms": 1200, "candidate_latency_ms": 1150,
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.0036,  # 仅↓10%
            "classification": "simulated",
        })
    check("成本降幅不足不晋升", r3.show_state()["promoted"] is False)
    return fails


# =============================================================================
def main() -> int:
    print("=" * 70)
    print("Phase 4 影子路由 + Golden 评分回归")
    print("=" * 70)
    fails = 0
    fails += _test_golden_scoring()
    fails += _test_routing_gates()
    fails += _test_promotion()
    print("\n" + "=" * 70)
    print(f"汇总：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    print("=" * 70)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

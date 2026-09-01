# -*- coding: utf-8 -*-
"""
verify_golden.py — PRD 金标准数值对账闸门（AC-05 ~ AC-24b）

用途
----
PRD §「验证命令」章节将本脚本列为 AC-05~AC-29 的官方机检入口。
它对 sample_salary.csv 跑完整链路，逐条断言 PRD §4 钉死的**数值金标准**，
输出 PASS/FAIL 汇总表，并以退出码表达结果（0=全通过，1=有 FAIL）。

与其它测试的分工
----------------
- tests/test_*.py         ：结构/契约/异常路径（能不能跑、字段在不在）
- tests/qa_e2e.py         ：端到端流程与产物（报告/图表/落盘）
- **tests/verify_golden.py**：数值对账（算出来的数字对不对）  ← 本文件

⚠️ 口径前提（踩过的坑，改本脚本前必读）
--------------------------------------
1. **带宽必须 round_to=0（不取整）**。PRD K9：「判定与 CR 计算一律用未取整值；
   round_to 取整只作用于对外发布的带宽表」，AC-05 亦注明「不取整」。
   若用 round_to=100 生成带宽，红绿圈人数会漂移（31/26/93 → 26/26/98），
   导致 AC-05/22/23/24 全线误判。这是**测试配置错误**，不是模块 bug。

2. **AC-22 的第①步成本 ≠ AC-07 的 green_to_min**。两者目标值不同：
     AC-07 green_to_min  → 补到 band_min              = 420,881
     AC-22 第①步 step1   → 补到 max(band_min,0.8×band_mid)×1.001 = 472,889
   当某职级 spread > 0.4 时 0.8×band_mid > band_min，故 step1 更大。
   曾因把这两个数混为一谈而误报 AC-22 FAIL。

3. **market 的占比分母是全量薪资基数**（K10「覆盖率 100%」），不是「有市场值的行」。
   mkt_p25 存在缺失，by_family 下 3 行无法对标，但分母仍是全量 39,170,400。

运行
----
    cd comp-agent-harness && python tests/verify_golden.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.tools.loader import load_salary_data, confirm_mapping          # noqa: E402
from src.tools.band import generate_band                                 # noqa: E402
from src.tools.diagnose import analyze_current_state                     # noqa: E402
from src.tools import market as market_mod                               # noqa: E402
from src.tools import increase as increase_mod                           # noqa: E402
from src.tools.session import get_store                                  # noqa: E402

SAMPLE = os.path.join(ROOT, "data", "sample_salary.csv")
CANONICAL = [
    "emp_id", "name", "dept", "level", "job_title", "job_family", "job_score",
    "monthly_salary", "annual_total_cash", "tenure_years", "perf_grade",
    "pay_mix", "band_min", "band_mid", "band_max", "mkt_p25", "mkt_p50", "mkt_p75",
]

# 结果收集
ROWS: list[dict] = []


def check(ac: str, name: str, ok: bool, expected="", actual="", note: str = "") -> bool:
    """记录一条断言结果并即时打印。"""
    ROWS.append({"ac": ac, "name": name, "ok": bool(ok),
                 "expected": expected, "actual": actual, "note": note})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {ac:<7s} {name}")
    if expected != "":
        print(f"         期望: {expected}")
        print(f"         实际: {actual}")
    if note:
        print(f"         说明: {note}")
    return bool(ok)


def close(a, b, tol):
    """浮点近似相等。"""
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# =============================================================================
# 准备：load → mapping → band（round_to=0，见文件头「口径前提 1」）
# =============================================================================
def setup():
    res = load_salary_data(SAMPLE)
    sid = res["session_id"]
    cols = set(res["columns"])
    confirm_mapping(sid, {c: c for c in CANONICAL if c in cols})
    generate_band(sid, mode="optimize", midpoint_source="current_median",
                  spread="auto", round_to=0, write_back=True)
    return sid


# =============================================================================
# AC-05 / AC-06 / AC-07 — 现状诊断
# =============================================================================
def verify_diagnose(sid: str):
    d = analyze_current_state(sid, write_back=True)
    counts = d.get("red_green", {}).get("counts", {})
    red, green, normal = counts.get("红圈"), counts.get("绿圈"), counts.get("合理")

    check("AC-05", "红绿圈人数 31/26/93（带宽不取整）",
          (red, green, normal) == (31, 26, 93) and (red + green + normal) == 150,
          "红31 / 绿26 / 合理93，合计150",
          f"红{red} / 绿{green} / 合理{normal}，合计{(red or 0)+(green or 0)+(normal or 0)}")

    cs = d.get("cr_summary", {})
    exp06 = {"mean": 1.0141, "std": 0.1757, "min": 0.6467,
             "p25": 0.9222, "median": 1.0000, "p75": 1.1158, "max": 1.5227}
    bad06 = {k: (cs.get(k), v) for k, v in exp06.items() if not close(cs.get(k), v, 1e-4)}
    check("AC-06", "CR 分布统计量（容差 1e-4）", not bad06,
          " ".join(f"{k}={v}" for k, v in exp06.items()),
          " ".join(f"{k}={cs.get(k)}" for k in exp06),
          "" if not bad06 else f"偏差项: {bad06}")

    cost = d.get("cost", {})
    okl = close(cost.get("red_overflow_monthly"), 79191, 1.0)
    okg = close(cost.get("green_to_min_monthly"), 35073, 1.0)
    check("AC-07", "红圈溢出 79,191/月；绿圈补足下限 35,073/月", okl and okg,
          "红圈 79,191 / 绿圈 35,073",
          f"红圈 {cost.get('red_overflow_monthly')} / 绿圈 {cost.get('green_to_min_monthly')}")


# =============================================================================
# AC-15 ~ AC-18 — 市场对标
# =============================================================================
def verify_market(sid: str):
    m50 = market_mod.market_benchmark(sid, strategy="P50", write_back=False)
    gp = m50.get("overall_gap", {})
    check("AC-15", "整体市场差距（个人均值口径）−3.23%",
          close(gp.get("mean"), -0.0323, 1e-4),
          "-0.0323", f"{gp.get('mean')}")

    costs = {}
    for strat in ("P25", "P50", "P75", "by_family"):
        m = market_mod.market_benchmark(sid, strategy=strat, write_back=False)
        costs[strat] = m["cost"]["monthly"]

    def _one(ac, strat, exp_m, exp_h, exp_p):
        m = market_mod.market_benchmark(sid, strategy=strat, write_back=False)
        c = m["cost"]
        ok = (close(c["monthly"], exp_m, 0.5)
              and c["headcount"] == exp_h
              and close(round(c["pct_of_payroll"], 4), exp_p, 1e-4))
        check(ac, f"{strat} 达标成本",
              ok,
              f"{exp_m} 元/月, {exp_h} 人, {exp_p:.2%}",
              f"{c['monthly']} 元/月, {c['headcount']} 人, {c['pct_of_payroll']:.4%}")

    _one("AC-16", "P50", 315900, 96, 0.0968)
    _one("AC-17", "P75", 857400, 135, 0.2627)
    _one("AC-18", "by_family", 498000, 99, 0.1526)

    check("AC-18", "单调性 cost(P25) < cost(by_family) < cost(P75)",
          costs["P25"] < costs["by_family"] < costs["P75"],
          "P25 < by_family < P75",
          f"{costs['P25']:.0f} < {costs['by_family']:.0f} < {costs['P75']:.0f}")


# =============================================================================
# AC-19 ~ AC-24b — 调薪模拟
# =============================================================================
def verify_increase(sid: str):
    exp_budget = {0.03: 1175112.0, 0.05: 1958520.0, 0.08: 3133632.0}

    # --- AC-19 预算守恒（4 策略 × 3 档）-------------------------------------
    bad = []
    for st in ("A", "B", "C", "D"):
        for bp in (0.03, 0.05, 0.08):
            o = increase_mod.simulate_increase(sid, st, budget_pct=bp, write_back=False)
            if not (close(o["budget_amount"], exp_budget[bp], 0.01)
                    and abs(o["total_cost"] - exp_budget[bp]) / exp_budget[bp] <= 1e-6
                    and o["usage_rate"] == 1.0):
                bad.append((st, bp, o["budget_amount"], o["total_cost"], o["usage_rate"]))
    check("AC-19", "预算守恒 4策略×3档（usage_rate==1.0）", not bad,
          "12 组全部守恒；3%/5%/8% = 1,175,112 / 1,958,520 / 3,133,632",
          f"12 组{'全部通过' if not bad else '存在偏差'}",
          "" if not bad else f"偏差: {bad}")

    # --- AC-20 策略 A 均匀性 --------------------------------------------------
    o = increase_mod.simulate_increase(sid, "A", budget_pct=0.05, write_back=True)
    df = get_store().load(sid).df
    rates = df["raise_rate"].values
    check("AC-20", "策略 A 每人调薪率均 == 5%（std≈0）",
          bool(np.allclose(rates, 0.05)) and np.std(rates) < 1e-9,
          "全部 5.000000%，std = 0",
          f"std = {np.std(rates):.2e}")

    # --- AC-21 策略 C 绩效梯度 ------------------------------------------------
    o = increase_mod.simulate_increase(sid, "C", budget_pct=0.05, write_back=False)
    pp = o.get("per_perf_rate", {})
    exp_c = {"A": 0.09060064, "B": 0.06040042, "C": 0.02516684, "D": 0.0}
    ok21 = all(close(pp.get(k), v, 5e-9) for k, v in exp_c.items())
    # 注意：1.8:1.2:0.5:0 是 A:B:C:D 的**权重比**，不是相邻两项的商。
    #   A/B = 1.8/1.2 = 1.5 ；B/C = 1.2/0.5 = 2.4 ；A/C = 1.8/0.5 = 3.6
    # （早期版本误写成 A/B == 1.8，是把权重比当成了相邻比值 —— 测试自身缺陷，非模块问题。）
    rA, rB, rC = pp.get("A", 0), pp.get("B", 1), pp.get("C", 1)
    ratio_ok = (close(rA / rB, 1.8 / 1.2, 1e-5)
                and close(rB / rC, 1.2 / 0.5, 1e-5)
                and close(rA / rC, 1.8 / 0.5, 1e-5))
    check("AC-21", "策略 C 绩效梯度 9.06/6.04/2.52/0% 且比值 1.8:1.2:0.5:0",
          ok21 and ratio_ok,
          "A=9.060064% B=6.040042% C=2.516684% D=0%；A/B=1.5 B/C=2.4 A/C=3.6",
          " ".join(f"{k}={pp.get(k, 0):.6%}" for k in ("A", "B", "C", "D"))
          + f"；A/B={rA / rB:.6f} B/C={rB / rC:.6f} A/C={rA / rC:.6f}")

    # --- AC-22 策略 B 消除绿圈 + 第①步成本 -----------------------------------
    # 注意：主口径取 scenarios[0]（rebase_band=False，见 PRD K7 / AC-24b）。
    # rebase_band=True 情景下带宽按平均调薪率上移，而策略 B 是差异化调薪，
    # 低于均值者必然重新落入绿圈 —— 这是**预期行为**，不是缺陷，故不在这里断言。
    greens = []
    for bp in (0.03, 0.05, 0.08):
        o = increase_mod.simulate_increase(sid, "B", budget_pct=bp, write_back=False)
        greens.append(o["scenarios"][0]["after"]["green"])
    check("AC-22", "策略 B 调薪后绿圈人数 == 0（3%/5%/8%，主口径）",
          all(g == 0 for g in greens), "0 / 0 / 0", " / ".join(map(str, greens)))

    o = increase_mod.simulate_increase(sid, "B", budget_pct=0.05, write_back=False)
    check("AC-22", "第①步补绿圈成本 472,889 元 / 26 人",
          close(o.get("step1_cost"), 472889, 1.0) and o.get("step1_headcount") == 26,
          "472,889 元，26 人",
          f"{o.get('step1_cost')} 元，{o.get('step1_headcount')} 人",
          "目标为 max(band_min, 0.8×band_mid)×1.001；≠ AC-07 的 green_to_min(420,881)")

    # --- AC-23 策略 D 冻结红圈 ------------------------------------------------
    o = increase_mod.simulate_increase(sid, "D", budget_pct=0.05, write_back=True)
    dfd = get_store().load(sid).df
    red_mask = (dfd["flag"] == "红圈").fillna(False).values
    ra = dfd["raise_annual"].values
    red_all_zero = bool(np.allclose(ra[red_mask], 0))
    nonred_sum = float(ra[~red_mask].sum())
    check("AC-23", "策略 D 红圈调薪额全 0，非红圈之和 == 预算",
          red_all_zero and close(nonred_sum, o["budget_amount"], 1.0),
          f"红圈 {int(red_mask.sum())} 人调薪额全 0；非红圈和 = 1,958,520",
          f"红圈 {int(red_mask.sum())} 人全 0={red_all_zero}；非红圈和 = {nonred_sum:,.2f}")

    # --- AC-24 rebase_band=True 恒等性 ---------------------------------------
    o = increase_mod.simulate_increase(sid, "A", budget_pct=0.05,
                                       rebase_band=True, write_back=False)
    after = o["scenarios"][0]["after"]
    check("AC-24", "rebase_band=True 后红/绿/合理 == 31/26/93（与调薪前恒等）",
          (after["red"], after["green"], after["normal"]) == (31, 26, 93),
          "31 / 26 / 93",
          f"{after['red']} / {after['green']} / {after['normal']}")

    # --- AC-24b 双情景并列 ----------------------------------------------------
    o = increase_mod.simulate_increase(sid, "A", budget_pct=0.05,
                                       rebase_band=False, write_back=False)
    sc = o["scenarios"]
    ok24b = (len(sc) == 2
             and sc[0]["rebase_band"] is False and sc[1]["rebase_band"] is True
             and close(sc[0]["total_cost"], sc[1]["total_cost"], 1e-6))
    check("AC-24b", "双情景：len==2、False 为主口径、两栏 total_cost 相同",
          ok24b,
          "len=2, flags=[False,True], 两栏 cost 相同",
          f"len={len(sc)}, flags={[s['rebase_band'] for s in sc]}, "
          f"costs={[s['total_cost'] for s in sc]}")


# =============================================================================
def main() -> int:
    print("=" * 100)
    print("PRD 金标准数值对账 — verify_golden.py")
    print("口径: 带宽 round_to=0（K9 不取整）；市场分母全量薪资基数（K10）")
    print("=" * 100)

    sid = setup()

    print("\n--- 现状诊断 ---")
    verify_diagnose(sid)
    print("\n--- 市场对标 ---")
    verify_market(sid)
    print("\n--- 调薪模拟 ---")
    verify_increase(sid)

    n_pass = sum(1 for r in ROWS if r["ok"])
    n_fail = len(ROWS) - n_pass

    print("\n" + "=" * 100)
    print(f"汇总：PASS = {n_pass}   FAIL = {n_fail}")
    if n_fail:
        print("\n未通过的条目：")
        for r in ROWS:
            if not r["ok"]:
                print(f"  - {r['ac']} {r['name']}: 期望 {r['expected']} / 实际 {r['actual']}")
    print("=" * 100)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

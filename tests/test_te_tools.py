# -*- coding: utf-8 -*-
"""
tests/test_te_tools.py — increase / paymix / jobeval 三模块自验脚本
================================================================================
纯标准库 + assert，无需 pytest。直接从项目根目录运行：

    python tests/test_te_tools.py

流程：load_salary_data → confirm_mapping → generate_band →
      simulate_increase → simulate_pay_mix → calc_job_score

断言重点：
    [预算金标准] 5% 预算下总增量成本 = 1,958,520（= 0.05 × Σ月薪 × 12）
    [量纲修复]   w_to_level(8.875) == "M3"；w_to_level(3.14) == "P3"；
                 评估映射正确（非恒 P1）
    [固浮比守恒] base_salary + variable_salary == monthly_salary（逐行精确）
    [回写]       base_salary / variable_salary / job_level 列确实落盘
    [方法论]     meta['paymix'] 含「方法论前提与风险」PRD 金句

结果同时打印（供 CI）并写入 /tmp/te_test_report.txt（供本机核验）。
"""

import os
import sys
import json

# 必须在导入 tools 之前设置状态目录（session.STATE_DIR 在 import 时求值）
_TMP_STATE = "/tmp/comp_te_test_state"
os.environ["COMP_STATE_DIR"] = _TMP_STATE

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

from tools.loader import load_salary_data, confirm_mapping  # noqa: E402
from tools.band import generate_band  # noqa: E402
from tools.increase import simulate_increase  # noqa: E402
from tools.paymix import simulate_pay_mix  # noqa: E402
from tools.jobeval import calc_job_score, w_to_level  # noqa: E402
from tools.session import reset_store, get_store  # noqa: E402
from tools.schemas import JOB_SCORE_LEVEL_BANDS  # noqa: E402

_SAMPLE = os.path.join(_ROOT, "data", "sample_salary.csv")
_GOLD_BUDGET = 1958520.0
_GOLD_ANNUAL = 39170400.0

_RESULTS = []


def check(name, cond, detail=""):
    _RESULTS.append((name, bool(cond), detail))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def main():
    reset_store(_TMP_STATE)
    os.makedirs(_TMP_STATE, exist_ok=True)

    # ---- 1) load + confirm ----------------------------------------------------
    load_res = load_salary_data(_SAMPLE)
    assert load_res["ok"], f"load 失败: {load_res}"
    sid = load_res["session_id"]

    mapping = {col: v["suggest"] for col, v in load_res["suggested_mapping"].items()
               if v.get("suggest")}
    conf = confirm_mapping(sid, mapping)
    assert conf["ok"], f"confirm 失败: {conf}"
    check("confirm_mapping 成功", conf["ok"],
          f"rows_out={conf.get('rows_out')}")

    # ---- 2) generate_band ----------------------------------------------------
    band_res = generate_band(sid, mode="optimize", midpoint_source="current_median")
    assert band_res["ok"], f"band 失败: {band_res}"
    check("generate_band 成功", band_res["ok"],
          f"levels={band_res.get('levels')}")

    # ---- 3) increase ---------------------------------------------------------
    inc = simulate_increase(sid, strategy="A", budget_pct=0.05, recommended="C")
    assert inc["ok"], f"increase 失败: {inc}"
    check("increase.ok", inc["ok"])
    check("预算金标准 current_annual_total==39,170,400",
          abs(inc["current_annual_total"] - _GOLD_ANNUAL) < 1e-6,
          f"{inc['current_annual_total']}")
    check("预算金标准 budget==1,958,520",
          abs(inc["budget"] - _GOLD_BUDGET) < 1e-6,
          f"{inc['budget']}")
    check("策略A总增量==预算(未超)",
          abs(inc["strategies"]["A"]["total_cost"] - _GOLD_BUDGET) < 1e-6
          and not inc["strategies"]["A"]["over_budget"],
          f"total={inc['strategies']['A']['total_cost']}")
    check("策略C(推荐)总增量==预算(未超)",
          abs(inc["strategies"]["C"]["total_cost"] - _GOLD_BUDGET) < 1e-6
          and not inc["strategies"]["C"]["over_budget"],
          f"total={inc['strategies']['C']['total_cost']}")
    check("逐人明细 150 行", len(inc["by_employee"]) == 150,
          f"n={len(inc['by_employee'])}")

    # ---- 4) paymix -----------------------------------------------------------
    pm = simulate_pay_mix(sid, write_back=True)
    assert pm["ok"], f"paymix 失败: {pm}"
    check("paymix.ok", pm["ok"])
    check("paymix 含 PRD 方法论金句",
          "高浮动比例的前提条件是" in pm["methodology_premise_risks"],
          "")
    # 回写列 + 守恒校验：重新 load 看 df
    store = get_store()
    sess = store.load(sid)
    df = sess.df
    has_cols = ("base_salary" in df.columns) and ("variable_salary" in df.columns)
    check("paymix 回写 base/variable 列", has_cols)
    if has_cols:
        sal = df["monthly_salary"].astype(float)
        base = df["base_salary"].astype(float)
        var = df["variable_salary"].astype(float)
        max_err = float((base + var - sal).abs().max())
        check("固浮比总额守恒(base+variable==月薪)", max_err < 0.011,
              f"max_err={max_err:.4f}")
        # 总额维度守恒
        total_err = abs(float((base + var).sum() - sal.sum()))
        check("固浮比总额求和守恒", total_err < 1e-6,
              f"sum_err={total_err:.4f}")

    # ---- 5) jobeval ----------------------------------------------------------
    # 5.0 量纲修复单元测试（不依赖数据）
    check("量纲修复 w_to_level(8.875)=='M3'",
          w_to_level(8.875) == "M3", w_to_level(8.875))
    check("量纲修复 w_to_level(3.14)=='P3'",
          w_to_level(3.14) == "P3", w_to_level(3.14))
    check("量纲修复 不用 W 直接喂 score_to_level(否则恒P1)",
          w_to_level(8.875) != "P1")

    je = calc_job_score(sid, model="hay", write_back=True)
    assert je["ok"], f"jobeval 失败: {je}"
    check("jobeval.ok", je["ok"])
    # 样本里并非每人都有 job_score（空白行不参评），故 n_jobs 应等于有分人数，
    # 区间为 (0, 150]；错配率应在 [0,100]
    check("jobeval 错配统计合理 (0<n_jobs<=150, 0<=mismatch%<=100)",
          0 < je["n_jobs"] <= 150 and 0 <= je["mismatch_pct"] <= 100,
          f"n={je['n_jobs']}, mismatch%={je['mismatch_pct']}")

    # 回写 job_level 列
    sess2 = store.load(sid)
    df2 = sess2.df
    check("jobeval 回写 job_level 列", "job_level" in df2.columns)
    if "job_level" in df2.columns:
        levels = df2["job_level"].dropna().astype(str).tolist()
        distinct = set(levels)
        check("评估映射非恒 P1（修复生效）", "P1" not in distinct or len(distinct) > 1,
              f"distinct={sorted(distinct)}")
        valid = {lv for _, _, lv in JOB_SCORE_LEVEL_BANDS}
        check("所有 job_level 落在合法集合", distinct.issubset(valid),
              f"distinct={sorted(distinct)}")

    # ---- 汇总 -----------------------------------------------------------------
    n_fail = sum(1 for _, ok, _ in _RESULTS if not ok)
    summary = {
        "total": len(_RESULTS),
        "passed": len(_RESULTS) - n_fail,
        "failed": n_fail,
        "gold_budget": inc["budget"],
        "w_8_875_level": w_to_level(8.875),
        "w_3_14_level": w_to_level(3.14),
        "details": [{"name": n, "ok": ok, "detail": d} for n, ok, d in _RESULTS],
    }
    with open("/tmp/te_test_report.txt", "w", encoding="utf-8") as f:
        f.write("TE TOOLS SELF-TEST REPORT\n")
        for n, ok, d in _RESULTS:
            f.write(f"[{'PASS' if ok else 'FAIL'}] {n} {d}\n")
        f.write(f"\nTOTAL {summary['passed']}/{summary['total']} passed\n")
        f.write(f"GOLD budget = {summary['gold_budget']}\n")
    print(f"\nTOTAL {summary['passed']}/{summary['total']} passed; "
          f"gold_budget={summary['gold_budget']}")
    return n_fail


if __name__ == "__main__":
    n_fail = main()
    sys.exit(1 if n_fail else 0)

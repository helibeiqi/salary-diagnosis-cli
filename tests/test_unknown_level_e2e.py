# -*- coding: utf-8 -*-
"""
Plan C 端到端验证：未识别职位 → UNKNOWN 单列保留（不删行）
================================================================================

场景：一张只有 职位(job_title) 没有 职级(level) 的发放表，含 12 个可识别职位 +
3 个无法识别职位（外包人员 / 临时支援 / 机动岗），以及 2 个辅助岗（保安 / 保洁员，
验证 Plan C·B 词表扩展 → O2）。

验证目标（P0-2 根因修复）：
  1. 未识别职位**不再被整行剔除**：清洗后行数 == 15（旧逻辑会掉到 12，基数错算）。
  2. 未识别职位标为 level="UNKNOWN"，纳入人数/成本基数。
  3. generate_band 排除 UNKNOWN（不给它生成带宽），但报告其人数。
  4. analyze_current_state 中 UNKNOWN 行 flag="未识别"，cr=NaN，不参与红绿圈/成本测算，
     但 counts 含「未识别」且 headcount 计入全部 15 人。
  5. 辅助岗（保安/保洁员）正确推断为 O2（Plan C·B）。

跑法：
    cd salary-diagnosis-cli
    PYTHONIOENCODING=utf-8 C:/Users/helib/envs/quant/python.exe tests/test_unknown_level_e2e.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.tools.loader import confirm_mapping, load_salary_data  # noqa: E402
from src.tools.band import generate_band  # noqa: E402
from src.tools.diagnose import analyze_current_state  # noqa: E402
from src.tools.session import get_store  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


def hr(title: str) -> None:
    print("\n" + "=" * 86)
    print(title)
    print("=" * 86)


CSV = os.path.join(ROOT, "data", "minimal_unknown_test.csv")


def main() -> int:
    hr("Plan C 端到端：未识别职位 → UNKNOWN 不删行")
    assert os.path.exists(CSV), f"缺少测试数据 {CSV}"

    # ---- 1) 载入 ----------------------------------------------------------------
    res = load_salary_data(CSV)
    assert res.get("ok"), res
    sid = res["session_id"]

    # 显式映射：有 job_title、无 level（触发 R1 启发式推断）
    mapping = {"工号": "emp_id", "姓名": "name", "职位": "job_title", "月薪": "monthly_salary"}

    # ---- 2) 确认映射（R1 推断 + 清洗）----------------------------------------
    cm = confirm_mapping(sid, mapping)
    assert cm.get("ok"), cm
    rep = cm["coerce_report"]
    rows_out = cm["rows_out"]

    check("清洗后行数 == 15（未识别职位不删行）",
          rows_out == 15, f"实际 {rows_out}")
    check("dropped.total == 0（无人被剔除）",
          rep["dropped"]["total"] == 0, str(rep["dropped"]["total"]))
    # 校验 level 列分布
    sess = get_store().load(sid)
    lv_counts = sess.df["level"].value_counts().to_dict()

    # 未识别计数：infer_levels 在清洗前已把 UNKNOWN 标好，故 clean_dataframe 不会再计数；
    # 这里直接用 level 列分布与 level_inference 报告（存于 session meta）双重校验。
    li = (sess.meta or {}).get("level_inference") or {}
    if li.get("unresolved") is not None:
        check("level_inference.unresolved == 3（3 个职位无法识别）",
              li.get("unresolved") == 3, str(li.get("unresolved")))
    else:
        check("level_inference 报告存在且记录未识别样本",
              bool(li.get("unresolved_samples")), str(li))
    check("level 含 UNKNOWN 且计数 3",
          lv_counts.get("UNKNOWN") == 3, str(lv_counts))
    check("辅助岗 行政助理/保安/保洁员 → O2（Plan C·B）",
          lv_counts.get("O2") == 3, str(lv_counts.get("O2")))
    check("可识别职级覆盖 O1/O2/P2/P3/P4/S1/M1/M3",
          {"O1", "O2", "P2", "P3", "P4", "S1", "M1", "M3"}.issubset(set(lv_counts)),
          str(sorted(lv_counts)))

    # ---- 3) 生成带宽（排除 UNKNOWN）-----------------------------------------
    gb = generate_band(sid, mode="optimize", midpoint_source="current_median",
                       spread="auto", export=False)
    assert gb.get("ok"), gb
    band_levels = {row["level"] for row in gb["band_table"]}
    check("带宽表不含 UNKNOWN 行",
          "UNKNOWN" not in band_levels, str(band_levels))
    note_has_unknown = any("UNKNOWN" in str(n) for n in gb.get("notes", []))
    check("generate_band notes 披露 UNKNOWN 人数",
          note_has_unknown, str(gb.get("notes", [])[:1]))

    # ---- 4) 现状诊断（UNKNOWN → 未识别，不参与 CR）--------------------------
    ac = analyze_current_state(sid)
    assert ac.get("ok"), ac
    counts = ac["red_green"]["counts"]
    check("诊断 counts 含「未识别」且 == 3",
          counts.get("未识别") == 3, str(counts))
    check("诊断 headcount == 15（UNKNOWN 计入基数）",
          ac["red_green"]["counts"].get("合理") is not None
          and sum(ac["red_green"]["counts"].values()) == 15,
          str(ac["red_green"]["counts"]))

    enriched = get_store().load(sid).df
    unknown_rows = enriched[enriched["level"] == "UNKNOWN"]
    check("UNKNOWN 行 flag == '未识别'",
          bool((unknown_rows["flag"] == "未识别").all()),
          str(unknown_rows["flag"].unique().tolist()))
    check("UNKNOWN 行 cr 为 NaN（不参与 CR 诊断）",
          bool(unknown_rows["cr"].isna().all()),
          str(unknown_rows["cr"].unique().tolist()))
    # 有真实带宽的行不应是 未识别
    real_rows = enriched[enriched["level"] != "UNKNOWN"]
    check("真实职级行均参与 CR（flag ∈ 红/绿/合理）",
          bool(real_rows["flag"].isin(["红圈", "绿圈", "合理"]).all()),
          str(real_rows["flag"].value_counts().to_dict()))

    # 汇总成本不受 UNKNOWN 污染（UNKNOWN 无带宽，不应计入红/绿圈成本）
    cost = ac["cost"]
    check("成本测算字段存在且未因 UNKNOWN 报错",
          "red_overflow_annual" in cost and "green_to_min_annual" in cost,
          str({k: cost[k] for k in ("red_overflow_annual", "green_to_min_annual")}))

    # ---- 5) 幂等性：clean_dataframe 对已标 UNKNOWN 的表再洗一次不应丢行 -----
    from src.tools.loader import clean_dataframe
    df_idem, rep_idem = clean_dataframe(sess.df)
    check("clean_dataframe 幂等（UNKNOWN 已标，行数仍为 15）",
          len(df_idem) == 15 and rep_idem["dropped"]["total"] == 0,
          f"行数 {len(df_idem)} 剔除 {rep_idem['dropped']['total']}")

    # 清理会话
    try:
        get_store().delete(sid)
    except Exception:  # noqa: BLE001
        pass

    failed = [r for r in _results if r[0] == FAIL]
    print(f"\n通过 {sum(1 for r in _results if r[0] == PASS)} 项，"
          f"失败 {len(failed)} 项")
    if failed:
        for r in failed:
            print("  FAIL:", r[1], "—", r[2])
        return 1
    print("Plan C 端到端全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

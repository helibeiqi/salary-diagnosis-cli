# -*- coding: utf-8 -*-
"""
test_column_mapping.py —— 表头匹配引擎 v2 的六场景回归 fixture
================================================================================

## 它防的是哪一类 bug

表头匹配是整条链路的**第一道闸门**：这一步猜错列，后面 band / market / increase
全部算得又快又准，但算的是错东西 —— 而且不会报错。比"报错"更危险的是"静默猜错"：
把 "下限" 猜成 band_min，实际是市场 P25，红绿圈人数会全盘失真却毫无迹象。

v1 的扁平别名词典有两个死穴，本文件逐条钉死修复后的行为：

1. **组合爆炸**：薪资 × {月/年} × {下限/中位/上限} × {带宽/市场} …
   每加一个维度就要手写一遍全排列，漏一个就静默失配；
2. **无法表达"我不知道"**：词典只会返回"最像的那个"，
   于是 "薪资"（没说月还是年）也会被自信地映射成 monthly_salary。

v2 改成「语义词根 × 维度修饰 → 路由表」，并把第 2 类情形显式标成
``ambiguous=True`` + 具体原因，交给人/模型拍板。**本文件的核心断言不是
"猜得准"，而是"该拍板的拍板、该认不知道的认不知道"。**

## 六个场景与它们各自防的东西

| # | 场景             | 防的是 |
|---|------------------|--------|
| 1 | 标准表头直命中     | 规范表头出现任何歧义标记 = 引擎过度敏感，会把用户逼进无意义的确认循环 |
| 2 | 乱列名英文混用     | 中英混排 / 括号单位 被归一化环节吃掉语义（'基本工资(元/月)' 丢掉"月"） |
| 3 | 歧义列名需 LLM     | **静默猜错** —— 有语义无维度时必须停下来问，且置信度必须低于自动确认阈值 |
| 4 | 带宽+市场共存消歧   | 两套"下限/中位/上限"混在一张表里被互相污染（band_min ↔ mkt_p25 串线） |
| 5 | 脏数据无关列       | 把备注/审批意见/Unnamed 占位列硬塞进标准字段（曾实测 'Unnamed: 12'
|   |                  | 因 un**NAME**d 里的 'name' 被判成姓名列，置信度 94） |
| 6 | 极端简写缩写       | HR 口语缩写（年包/固浮比/岗级/TC/P50）整列失配 |

## 运行方式

    pytest tests/test_column_mapping.py -v        # 标准方式
    python tests/test_column_mapping.py           # 无 pytest 也能跑（自带 runner）

第二种方式是刻意保留的：本项目改造为**独立 Python 产品**后，交付环境里可能
只有 pandas/numpy 而没有 pytest，而表头匹配是纯字符串逻辑、零第三方依赖，
不应该因为缺一个测试框架就没法自证。
"""
from __future__ import annotations

import csv
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 刻意从 loader 导入（而非 schemas）：需求 §1.3 指定的对外路径是 loader，
# 这行本身就是"转出没断"的回归 —— 实现搬到 schemas 后这里会立刻红灯。
from src.tools.loader import parse_column_semantics          # noqa: E402
from src.tools.schemas import (                              # noqa: E402
    CANONICAL_FIELDS,
    auto_suggest_mapping,
)

try:
    import pytest
except ImportError:  # 独立部署环境可能没有 pytest，见模块 docstring
    pytest = None  # type: ignore[assignment]


# =============================================================================
# 一、阈值常量（与 schemas.py / main.py 保持同一套口径）
# =============================================================================
# 自动确认阈值：main.py 的 `_auto_mapping(min_confidence=60)` 用的就是这个数。
# 歧义列的置信度**必须严格小于**它，否则"标了 ambiguous 却仍被自动确认"，
# 歧义标记形同虚设（2026-09-01 实测踩过：alias fallback 把歧义列抬到正好 60）。
AUTO_CONFIRM_THRESHOLD = 60
# 精确命中的下限：路由表命中给 90 + 覆盖率加成，最低 90。
EXACT_MIN_CONFIDENCE = 90


# =============================================================================
# 二、六个场景
# =============================================================================
# 每个 case 的字段含义：
#   expect        : {列名: 期望标准字段}，值为 None 表示"必须拒绝映射"
#   ambiguous     : 必须被标 ambiguous=True 的列（**全等**比较，多标少标都算失败）
#   min_confidence: 非歧义列的置信度下限
#   max_ambiguous : 允许的歧义列数量上限（防引擎变成"什么都不敢判"）

CASES: List[Dict[str, Any]] = [
    # ---------------------------------------------------------------- 场景 1
    {
        "id": "F1-标准表头直命中",
        "why": "18 列规范英文表头必须零歧义、零人工介入，否则引擎过度敏感",
        "expect": {
            "emp_id": "emp_id",
            "name": "name",
            "dept": "dept",
            "level": "level",
            "job_title": "job_title",
            "job_family": "job_family",
            "job_score": "job_score",
            "monthly_salary": "monthly_salary",
            "annual_total_cash": "annual_total_cash",
            "tenure_years": "tenure_years",
            "perf_grade": "perf_grade",
            "pay_mix": "pay_mix",
            "band_min": "band_min",
            "band_mid": "band_mid",
            "band_max": "band_max",
            "mkt_p25": "mkt_p25",
            "mkt_p50": "mkt_p50",
            "mkt_p75": "mkt_p75",
        },
        "ambiguous": set(),
        "min_confidence": EXACT_MIN_CONFIDENCE,
        "max_ambiguous": 0,
    },
    # ---------------------------------------------------------------- 场景 2
    {
        "id": "F2-乱列名英文混用",
        "why": "中英混排与括号单位不得丢维度；'基本工资(元/月)' 的'月'藏在单位里，"
               "而 normalize_col() 会整段删括号 —— 必须靠单位补捞救回来",
        "expect": {
            "Emp ID": "emp_id",
            "员工Name": "name",
            "Monthly薪资": "monthly_salary",
            "年度Total Cash": "annual_total_cash",
            "Band Min": "band_min",
            "市场P50": "mkt_p50",
            "Perf Grade": "perf_grade",
            "Pay Mix": "pay_mix",
            "Job Family": "job_family",
            "Dept": "dept",
            "司龄(年)": "tenure_years",
            "基本工资(元/月)": "monthly_salary",
            "岗位评估得分": "job_score",
        },
        "ambiguous": set(),
        "min_confidence": EXACT_MIN_CONFIDENCE,
        "max_ambiguous": 0,
    },
    # ---------------------------------------------------------------- 场景 3
    {
        "id": "F3-歧义列名需LLM拍板",
        "why": "有语义无维度 = 程序**必须**拒绝拍板：'薪资'没说月/年、'分位'没说"
               "25/50/75、'评估'没说岗位/绩效、'下限'没说带宽/市场。"
               "四列都必须 ambiguous 且置信度 < 60（否则会被 CLI 自动确认掉）",
        "expect": {
            # suggest 仍给"最可能"作为模型的起点，但 ambiguous 标记与低置信是硬要求
            "薪资": "monthly_salary",
            "分位": "mkt_p25",
            "评估": "job_score",
            "下限": "band_min",
        },
        "ambiguous": {"薪资", "分位", "评估", "下限"},
        "min_confidence": 0,
        "max_ambiguous": 4,
        # 每个歧义列的原因里必须出现的关键词（防原因文案退化成"未知"这类废话）
        "reason_keywords": {
            "薪资": ["月", "年"],
            "分位": ["P25", "P50", "P75"],
            "评估": ["岗位", "绩效"],
            "下限": ["带宽", "市场"],
        },
        # 候选必须真的把两种可能都列出来，模型才有得选
        "expect_candidates": {
            "薪资": {"monthly_salary", "annual_total_cash"},
            "下限": {"band_min", "mkt_p25"},
            "评估": {"job_score", "perf_grade"},
        },
    },
    # ---------------------------------------------------------------- 场景 4
    {
        "id": "F4-带宽与市场共存消歧",
        "why": "同一张表里两套 min/mid/max：带宽走 band_*、市场走 mkt_p25/50/75。"
               "语义前缀在就必须精确分流、绝不串线；前缀不在（裸'下限'）才升级歧义",
        "expect": {
            "带宽下限": "band_min",
            "带宽中位": "band_mid",
            "带宽上限": "band_max",
            "市场下限": "mkt_p25",
            "市场中位": "mkt_p50",
            "市场上限": "mkt_p75",
            "市场25分位": "mkt_p25",
            "市场75分位": "mkt_p75",
            "薪酬带下限": "band_min",
            "下限": "band_min",   # 裸修饰：suggest 只是起点，真正的断言是 ambiguous
        },
        "ambiguous": {"下限"},
        "min_confidence": EXACT_MIN_CONFIDENCE,
        "max_ambiguous": 1,
        "expect_candidates": {"下限": {"band_min", "mkt_p25"}},
    },
    # ---------------------------------------------------------------- 场景 5
    {
        "id": "F5-脏数据无关列",
        "why": "无关列必须明确返回 None 而不是硬找一个最像的。"
               "'Unnamed: 12' 是实测踩过的坑：un**NAME**d 里的 'name' 命中 person "
               "词根，曾以 94 分被判成姓名列",
        "expect": {
            "备注": None,
            "数据状态": None,
            "入职日期": None,
            "填表人": None,
            "审批意见": None,
            "序号": None,
            "Unnamed: 12": None,
            "Unnamed: 0": None,
            "—": None,
            "": None,
        },
        "ambiguous": set(),
        "min_confidence": 0,
        "max_ambiguous": 0,
    },
    # ---------------------------------------------------------------- 场景 6
    {
        "id": "F6-极端简写缩写",
        "why": "HR 口语缩写必须能命中：年包=年度总现金、固浮比=固浮比例、"
               "岗级=职级、职等=职级。而真·两义缩写（TC / P50）保持歧义，"
               "但歧义列不得超过 3 个 —— 否则引擎退化成'什么都不敢判'",
        "expect": {
            "工号": "emp_id",
            "姓名": "name",
            "部门": "dept",
            "职级": "level",
            "月薪": "monthly_salary",
            "年包": "annual_total_cash",
            "固浮比": "pay_mix",
            "司龄": "tenure_years",
            "绩效": "perf_grade",
            "岗级": "level",
            "职等": "level",
            "职族": "job_family",
            "TC": "annual_total_cash",
            "P50": "mkt_p50",
        },
        "ambiguous": {"TC", "P50"},
        "min_confidence": EXACT_MIN_CONFIDENCE,
        "max_ambiguous": 3,
    },
]


# =============================================================================
# 三、断言执行器（返回失败清单，供 pytest 与独立 runner 共用）
# =============================================================================


def check_case(case: Dict[str, Any]) -> List[str]:
    """跑一个场景，返回**全部**失败信息（不用 assert 早退，一次看全所有问题）。"""
    fails: List[str] = []
    expect: Dict[str, Optional[str]] = case["expect"]
    want_ambiguous = set(case["ambiguous"])
    got_ambiguous = set()

    for col, want in expect.items():
        info = parse_column_semantics(col)
        got = info["suggest"]
        conf = int(info["confidence"])
        amb = bool(info["ambiguous"])
        reasons = list(info["ambiguous_reasons"])
        cands = [c["field"] for c in info["candidates"]]

        if amb:
            got_ambiguous.add(col)

        # --- 1. suggest 必须一致（None 也必须精确为 None）---------------------
        if got != want:
            fails.append(
                f"[suggest] {col!r}: 期望 {want!r}，实际 {got!r}"
                f"（source={info['match_source']}, roots={info['matched_roots']},"
                f" mods={info['matched_modifiers']}）")

        # --- 2. 置信度 -------------------------------------------------------
        if want is None:
            if conf != 0:
                fails.append(f"[confidence] {col!r}: 无关列应为 0，实际 {conf}")
        elif amb:
            # 歧义列的硬要求：必须低于自动确认阈值，否则 ambiguous 标记没有作用
            if conf >= AUTO_CONFIRM_THRESHOLD:
                fails.append(
                    f"[confidence] {col!r}: 歧义列置信度 {conf} >= 自动确认阈值 "
                    f"{AUTO_CONFIRM_THRESHOLD}，会被 CLI 静默自动确认")
        else:
            if conf < case["min_confidence"]:
                fails.append(f"[confidence] {col!r}: {conf} < 下限 "
                             f"{case['min_confidence']}")

        # --- 3. 歧义列必须给出**非空且具体**的原因 ----------------------------
        if amb and not reasons:
            fails.append(f"[reasons] {col!r}: ambiguous=True 但 ambiguous_reasons 为空")
        if not amb and reasons:
            fails.append(f"[reasons] {col!r}: ambiguous=False 却带原因 {reasons}")

        for kw in case.get("reason_keywords", {}).get(col, []):
            if not any(kw in r for r in reasons):
                fails.append(f"[reasons] {col!r}: 原因里应出现 {kw!r}，实际 {reasons}")

        # --- 4. 候选集合 -----------------------------------------------------
        want_cands = case.get("expect_candidates", {}).get(col)
        if want_cands and not want_cands.issubset(set(cands)):
            fails.append(f"[candidates] {col!r}: 应包含 {sorted(want_cands)}，"
                          f"实际 {cands}")

        # --- 5. 返回的字段名必须是真实标准字段（防拼错字段名 ）----------------
        if got is not None and got not in CANONICAL_FIELDS:
            fails.append(f"[schema] {col!r}: suggest={got!r} 不在 CANONICAL_FIELDS 中")

    # --- 6. 歧义集合必须**全等** ---------------------------------------------
    if got_ambiguous != want_ambiguous:
        missing = sorted(want_ambiguous - got_ambiguous)
        extra = sorted(got_ambiguous - want_ambiguous)
        fails.append(f"[ambiguous集合] 漏标={missing} 多标={extra}")

    if len(got_ambiguous) > case["max_ambiguous"]:
        fails.append(f"[ambiguous数量] {len(got_ambiguous)} > 上限 "
                      f"{case['max_ambiguous']}（引擎过度保守）")

    return fails


# =============================================================================
# 四、pytest 入口
# =============================================================================

if pytest is not None:

    @pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
    def test_column_semantics(case: Dict[str, Any]) -> None:
        fails = check_case(case)
        assert not fails, (
            f"\n场景 {case['id']} 失败 {len(fails)} 项\n"
            f"（本场景防的是：{case['why']}）\n  - " + "\n  - ".join(fails))

    def test_root_table_coverage() -> None:
        """
        词根路由表覆盖率闸门（需求验收标准 §5.5：≥80%）。

        统计口径刻意写死为"逐列判定 match_source"，而不是抽头尾几列目测 ——
        覆盖率这类指标必须逐值统计，抽样会给出完全错误的结论。
        """
        stat = measure_coverage()
        assert stat["routing_pct"] >= 80.0, (
            f"词根路由命中率 {stat['routing_pct']:.1f}% < 80%\n"
            f"明细：{stat}")

    def test_auto_suggest_mapping_backward_compatible() -> None:
        """
        `auto_suggest_mapping()` 的返回结构必须**向后兼容**。

        它有现存调用方（loader.load_salary_data / loader.desensitize / registry），
        v1 的 5 个键少一个都会让上游静默拿到 None。新增键只能加、不能改。
        """
        got = auto_suggest_mapping(["月薪", "带宽下限", "备注", "薪资"])
        assert set(got) == {"月薪", "带宽下限", "备注", "薪资"}
        for col, v in got.items():
            for key in ("suggest", "confidence", "label", "ambiguous", "candidates"):
                assert key in v, f"{col}: v1 键 {key} 丢失（破坏向后兼容）"
            for key in ("ambiguous_reasons", "matched_roots",
                        "matched_modifiers", "match_source"):
                assert key in v, f"{col}: v2 新增键 {key} 缺失"
            assert isinstance(v["candidates"], list)
            assert isinstance(v["ambiguous_reasons"], list)
        # 抽两个语义相反的列，确认标记方向没反
        assert got["月薪"]["ambiguous"] is False
        assert got["薪资"]["ambiguous"] is True
        assert got["备注"]["suggest"] is None


# =============================================================================
# 五、覆盖率统计（pytest 与独立 runner 共用；也是交付物里那份统计表的来源）
# =============================================================================


def _read_header(path: str) -> List[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [c for c in next(csv.reader(fh))]


def measure_coverage(files: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """
    在真实数据文件的**全部**列名上统计各匹配来源的占比。

    分子只算 routing / routing_ambiguous（词根路由表参与了判定），
    alias_fallback 与 noise/none 都算未命中 —— 这样这个指标才真的在
    衡量"词根表够不够用"，而不是"最后有没有蒙到一个答案"。
    """
    if files is None:
        files = [os.path.join(PROJECT_ROOT, "data", "sample_salary.csv"),
                 os.path.join(PROJECT_ROOT, "data", "messy_salary.csv")]
    buckets: Dict[str, List[str]] = {"routing": [], "routing_ambiguous": [],
                                     "alias_fallback": [], "none": [], "noise": []}
    per_file: Dict[str, Dict[str, int]] = {}
    for path in files:
        if not os.path.exists(path):
            continue
        counts: Dict[str, int] = {}
        for col in _read_header(path):
            src = str(parse_column_semantics(col)["match_source"])
            buckets.setdefault(src, []).append(col)
            counts[src] = counts.get(src, 0) + 1
        per_file[os.path.basename(path)] = counts

    total = sum(len(v) for v in buckets.values())
    routing = len(buckets["routing"]) + len(buckets["routing_ambiguous"])
    return {
        "total_columns": total,
        "routing_hit": routing,
        "routing_pct": (100.0 * routing / total) if total else 0.0,
        "exact": len(buckets["routing"]),
        "ambiguous": len(buckets["routing_ambiguous"]),
        "alias_fallback": len(buckets["alias_fallback"]),
        "unmapped": len(buckets["none"]) + len(buckets["noise"]),
        "unmapped_columns": buckets["none"] + buckets["noise"],
        "alias_fallback_columns": buckets["alias_fallback"],
        "per_file": per_file,
    }


# =============================================================================
# 六、独立 runner（无 pytest 环境）
# =============================================================================


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 老终端没有 reconfigure，忽略即可
        pass

    print("=" * 78)
    print("表头匹配引擎 v2 —— 六场景 fixture")
    print("=" * 78)
    failed = 0
    for case in CASES:
        fails = check_case(case)
        n = len(case["expect"])
        if fails:
            failed += 1
            print(f"[FAIL] {case['id']}  ({n} 列, {len(fails)} 项不符)")
            for f in fails:
                print(f"        - {f}")
        else:
            print(f"[PASS] {case['id']}  ({n} 列全部符合期望，"
                  f"歧义 {len(case['ambiguous'])} 列)")

    print("-" * 78)
    stat = measure_coverage()
    ok = stat["routing_pct"] >= 80.0
    print(f"[{'PASS' if ok else 'FAIL'}] 词根路由覆盖率  "
          f"{stat['routing_hit']}/{stat['total_columns']} = "
          f"{stat['routing_pct']:.1f}%  (闸门 ≥80%)")
    print(f"        精确命中 {stat['exact']} / 歧义 {stat['ambiguous']} / "
          f"词典兜底 {stat['alias_fallback']} / 未映射 {stat['unmapped']}")
    if stat["unmapped_columns"]:
        print(f"        未映射列：{stat['unmapped_columns']}")
    if stat["alias_fallback_columns"]:
        print(f"        词典兜底列：{stat['alias_fallback_columns']}")
    if not ok:
        failed += 1

    print("=" * 78)
    print(f"汇总：场景 {len(CASES) - min(failed, len(CASES))}/{len(CASES)} 通过"
          f"{'' if failed == 0 else f'，共 {failed} 项失败'}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

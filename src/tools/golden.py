# -*- coding: utf-8 -*-
"""
golden.py — Golden 集加载器 + 数学化 LLM-as-Judge 评分（Phase 4，自主优化架构师）
================================================================================

本模块是「影子测试 + 模型路由」(架构 §4.3) 的**可确定性评分**部分。

为什么可确定性评分
--------------------------------------------------------------------------------
映射子任务（``confirm_mapping`` 的白名单列）的正确答案**独立于模型**——
它就是一个「脏表头 → 标准字段」的标注答案键。因此我们不需要"人工 judge"，
也不需要"另一个 LLM 来评判 LLM"（那会引入二次不确定性与二次成本），
直接用 golden 标注集算**精确命中率**即可。这正是 LLM-as-Judge 自评分的完美场景：
把主观的"映射好不好"翻译成数学上可复现的分数。

评分公式（设计文档 §4.3，非主观）
--------------------------------------------------------------------------------
    score = 5 · mapping_hit_rate + 3 · ambiguity_recall − w₁ · latency_s − w₂ · cost_usd

  * mapping_hit_rate  —— 全部 golden case 中，模型输出 == expected 的比例
  * ambiguity_recall  —— 仅「真正歧义」(ambiguous=true) 的 case 中，模型输出 == expected 的比例
                          （度量模型在"确定性引擎认不了、必须靠语义"的地方靠不靠谱）
  * latency_s / cost_usd —— 该次执行的延迟(秒)与单位成本(美元)，由调用方上报

零 PII 纪律
--------------------------------------------------------------------------------
Golden 集**只含列名、候选字段名、标注答案**，绝不含任何薪资数值或员工 ID。
本模块不读薪资文件、不碰 DataFrame、不发起任何网络调用。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 项目根（src/tools/golden.py → 上两级）。用 abspath(__file__) 避免相对 __file__
# 在脚本直跑时把路径走到仓库外（已踩坑）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

# Golden 集文件名（不含 data/ 前缀；路径统一由 _DATA_DIR 拼接，避免 data/data 重复）
_CURATED_GOLDEN = "mapping_golden.json"
_FULL_ANSWER_KEY = "ambiguous_salary.mapping.json"


@dataclass
class GoldenCase:
    """一条 golden 标注样本。"""

    raw_column: str
    expected: str
    ambiguous: bool
    candidates: List[str] = field(default_factory=list)
    sample_values: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class MappingScore:
    """一次映射评分的结果。"""

    n: int
    n_correct: int
    mapping_hit_rate: float
    ambiguity_recall: float
    ambiguous_n: int = 0
    ambiguous_correct: int = 0

    def composite(self, latency_ms: float = 0.0, cost_usd: float = 0.0,
                  w1: float = 0.01, w2: float = 1e-4) -> float:
        """
        合成分数（架构 §4.3）。

        :param latency_ms: 该次执行延迟（毫秒）
        :param cost_usd:   该次执行单位成本（美元）
        :param w1:         延迟权重（按秒计）
        :param w2:         成本权重（按美元计）
        """
        latency_s = max(0.0, float(latency_ms)) / 1000.0
        return (5.0 * self.mapping_hit_rate
                + 3.0 * self.ambiguity_recall
                - w1 * latency_s
                - w2 * cost_usd)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "n_correct": self.n_correct,
            "mapping_hit_rate": round(self.mapping_hit_rate, 6),
            "ambiguity_recall": round(self.ambiguity_recall, 6),
            "ambiguous_n": self.ambiguous_n,
            "ambiguous_correct": self.ambiguous_correct,
        }


class GoldenSet:
    """一组 golden 标注样本，提供确定性评分。"""

    def __init__(self, cases: List[GoldenCase]) -> None:
        self.cases: List[GoldenCase] = list(cases)

    # ------------------------------------------------------------------ 加载
    @classmethod
    def load(cls, path: Optional[str] = None) -> "GoldenSet":
        """
        加载 curated golden 文件（必含 candidates + ambiguous 标记）。
        若 path 为 None，加载仓库默认 ``data/mapping_golden.json``。
        """
        p = path or os.path.join(_DATA_DIR, _CURATED_GOLDEN)
        cases: List[GoldenCase] = []
        with open(p, "r", encoding="utf-8") as f:
            arr = json.load(f)
        for c in arr:
            cases.append(GoldenCase(
                raw_column=str(c["raw_column"]),
                expected=str(c["expected"]),
                ambiguous=bool(c.get("ambiguous", True)),
                candidates=list(c.get("candidates", [c["expected"]])),
                sample_values=list(c.get("sample_values", [])),
                note=str(c.get("note", "")),
            ))
        return cls(cases)

    @classmethod
    def from_repo(cls, data_dir: Optional[str] = None) -> "GoldenSet":
        """
        仓库级 Golden 集：curated 歧义集 + 全量映射答案键的并集。

        1) 先以 ``data/ambiguous_salary.mapping.json``（全量答案键）建立
           deterministic 基线（ambiguous=false）；
        2) 再用 ``data/mapping_golden.json``（curated 歧义集）覆盖同名列，
           补上 candidates 与 ambiguous 标记。

        这样既复用既有标注数据，又保留"哪些列是确定性引擎领域、
        哪些是真正歧义"的语义区分，使 ambiguity_recall 有意义。
        """
        dd = data_dir or _DATA_DIR
        cases: Dict[str, GoldenCase] = {}

        # 1) 全量答案键（确定性基线）
        full_path = os.path.join(dd, _FULL_ANSWER_KEY)
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            for raw, std in obj.items():
                cases[raw] = GoldenCase(
                    raw_column=str(raw),
                    expected=str(std),
                    ambiguous=False,
                    candidates=[str(std)],
                )

        # 2) curated 歧义集覆盖
        curated_path = os.path.join(dd, _CURATED_GOLDEN)
        if os.path.exists(curated_path):
            with open(curated_path, "r", encoding="utf-8") as f:
                arr = json.load(f)
            for c in arr:
                cases[str(c["raw_column"])] = GoldenCase(
                    raw_column=str(c["raw_column"]),
                    expected=str(c["expected"]),
                    ambiguous=bool(c.get("ambiguous", True)),
                    candidates=list(c.get("candidates", [c["expected"]])),
                    sample_values=list(c.get("sample_values", [])),
                    note=str(c.get("note", "")),
                )

        if not cases:
            raise FileNotFoundError(
                f"找不到任何 golden 数据：请检查 {full_path} 与 {curated_path}")
        return cls(list(cases.values()))

    # ------------------------------------------------------------------ 评分
    def score(self, prediction: Dict[str, str]) -> MappingScore:
        """
        用 golden 标注集对一次映射预测打分。

        :param prediction: ``{raw_column: 模型选择的标准字段名}``
                           未出现的列视为"未作答"（判错）。
        :return: :class:`MappingScore`
        """
        n = len(self.cases)
        n_correct = 0
        amb_n = 0
        amb_correct = 0
        for c in self.cases:
            chosen = prediction.get(c.raw_column)
            if chosen == c.expected:
                n_correct += 1
                if c.ambiguous:
                    amb_correct += 1
            if c.ambiguous:
                amb_n += 1

        hit = (n_correct / n) if n else 0.0
        rec = (amb_correct / amb_n) if amb_n else 0.0
        return MappingScore(
            n=n, n_correct=n_correct,
            mapping_hit_rate=hit,
            ambiguity_recall=rec,
            ambiguous_n=amb_n,
            ambiguous_correct=amb_correct,
        )

    # ------------------------------------------------------------------ 工具
    def ambiguous_columns(self) -> List[str]:
        """返回 golden 中被标记为真正歧义的列名（供影子测试构造 prompt 用）。"""
        return [c.raw_column for c in self.cases if c.ambiguous]

    def as_predictions_template(self, fill: Optional[str] = None) -> Dict[str, str]:
        """
        生成一份"预测骨架"。``fill`` 为 None 时不作答（全错基线）；
        否则所有列都填 ``fill``（用于构造对照实验）。
        """
        if fill is None:
            return {}
        return {c.raw_column: fill for c in self.cases}


if __name__ == "__main__":
    # 自证：加载 + 评分（无 pytest 也能跑）
    gs = GoldenSet.from_repo()
    print(f"[golden] 加载 {len(gs.cases)} 条样本（歧义 {len(gs.ambiguous_columns())} 条）")

    perfect = {c.raw_column: c.expected for c in gs.cases}
    wrong = {c.raw_column: ("name" if c.expected != "name" else "emp_id")
             for c in gs.cases}
    partial = dict(perfect)
    if gs.ambiguous_columns():
        partial[gs.ambiguous_columns()[0]] = "name"  # 故意错一个歧义列

    for label, pred in (("完美", perfect), ("全错", wrong), ("部分", partial)):
        s = gs.score(pred)
        print(f"  [{label}] hit={s.mapping_hit_rate:.3f} recall={s.ambiguity_recall:.3f} "
              f"composite={s.composite(120, 0.002):.4f}")

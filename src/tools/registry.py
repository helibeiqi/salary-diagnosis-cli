# -*- coding: utf-8 -*-
"""
registry.py — 11 个工具的中央注册表（集成层的唯一入口）
================================================================================

本模块是 **Python 计算核心** 与 **两个消费方** 之间的唯一咽喉：

    ┌──────────────┐        ┌──────────────┐        ┌──────────────────┐
    │ server.py    │──┐     │              │        │ loader / band /  │
    │ (dsh 插件)   │  ├────>│ registry.py  │───────>│ diagnose / market│
    ├──────────────┤  │     │              │        │ / increase / ... │
    │ main.py      │──┘     └──────────────┘        └──────────────────┘
    │ (本地 CLI)   │           ↑ 同一份 handler
    └──────────────┘        sandbox.py 也走这里

为什么必须有这一层（而不是让 server 直接 import 各模块）
--------------------------------------------------------------------------------
1. **口径一致性**：模型直接调 `market_benchmark`、和在 PTC 沙箱里写
   `tools.market_benchmark(...)`，必须走**完全相同的代码**。注册表是这个保证的载体
   （架构 §5.4「tools 绑定」）。
2. **信封归一化**：计算模块用的是 `errors.py` 的 `{"ok":True, ...业务字段}` /
   `{"ok":False,"error":{...}}` 两种形态；而架构 §4.0 规定对 dsh 暴露的是
   `{ok, code, message, data, artifacts, charts, warnings, meta}` 统一信封。
   两者的翻译**只允许发生在这一个文件里** —— 否则 11 个工具会长出 11 种信封。
3. **无损 JSON 兜底**（架构 §8 风险 1，最高风险）：pandas / numpy 的
   `NaN` / `inf` / `np.int64` / `Timestamp` 一旦直接进 `json.dumps`，
   `NaN` 会变成裸 `NaN` 字面量（非法 JSON）或被宿主判定 invalid output。
   `to_lossless()` 在**唯一出口**做规整，比要求 11 个模块各自小心可靠得多。
4. **抗签名漂移**：计算模块由另一位工程师并行开发，函数签名可能与契约有偏差。
   注册表按被调函数的真实签名过滤 kwargs 并把丢弃项记入 `warnings`，
   使「契约小偏差」退化为一条警告，而不是一次 TypeError 崩溃。

设计纪律
--------------------------------------------------------------------------------
* **懒加载**：`importlib` 在 `call_tool` 时才导入目标模块。
  ⇒ ① 避免 registry ←→ 计算模块的循环导入；
     ② `market.py` / `increase.py` 等尚未交付时，`tools/list` 依然能列全 11 个工具，
        只有真正调用才返回 `NOT_IMPLEMENTED`（带 hint 告知模型换路径）。
* **参数契约以本文件为准**：JSON Schema 由本文件的扁平 DSL 生成，
  TS 插件侧 `schemas.ts` 必须与之镜像（QA 用 `tools/list` 做 diff）。
"""

from __future__ import annotations

import importlib
import inspect
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 本文件**刻意不 import** loader/band/diagnose/... —— 全部懒加载，见模块 docstring
from .errors import CompToolError, error_payload

CONTRACT_VERSION = "1.0.0"

# 单个 data 节点里内联的最大行数（明细一律落盘走 artifacts，见架构 §5.3 R1）
MAX_INLINE_ROWS = 50
# summary_md 缺省兜底文案的最大长度
MAX_SUMMARY_CHARS = 8000


# =============================================================================
# 一、错误码翻译表：errors.py 的内部码 → 架构 §4.11 的对外码
# =============================================================================
# 为什么要翻译：`errors.py` 的码面向 Python 内部调用者（COLUMN_MISSING 强调"列没了"），
# 而 §4.11 的码面向**模型的纠错决策**（MISSING_REQUIRED_FIELD 暗示"去问用户哪一列"）。
# 对外码是 README / QA / 前端共同引用的公共词表，必须收敛在一处翻译。
CODE_ALIASES: Dict[str, str] = {
    "COMP_ERROR": "INTERNAL_ERROR",
    "COLUMN_MISSING": "MISSING_REQUIRED_FIELD",
    "SESSION_NOT_FOUND": "NO_SESSION",
    "INVALID_PARAMETER": "INVALID_PARAMS",
    # 以下内部码与对外码同名，列出以示"已审阅过，不是漏了"
    "FILE_NOT_FOUND": "FILE_NOT_FOUND",
    "MAPPING_NOT_CONFIRMED": "MAPPING_NOT_CONFIRMED",
    "UPSTREAM_MISSING": "UPSTREAM_MISSING",
}

# 每个对外错误码配一句「模型下一步该做什么」，用于上游没给 hint 时兜底
DEFAULT_HINTS: Dict[str, str] = {
    "FILE_NOT_FOUND": "请向用户确认文件路径（Windows 下需写成 C:/... 正斜杠形式）。",
    "UNSUPPORTED_FORMAT": "请让用户把文件另存为 .csv 或 .xlsx 后重试。",
    "MISSING_REQUIRED_FIELD": "请回问用户这些标准字段对应表里哪一列，再调用 confirm_mapping。",
    "NO_SESSION": "请先调用 load_salary_data(file_path=...) 建立会话。",
    "SESSION_EXPIRED": "会话已失效，请重新调用 load_salary_data 与 confirm_mapping。",
    "MAPPING_NOT_CONFIRMED": "请先调用 confirm_mapping(session_id, mapping) 固化字段映射。",
    "INVALID_PARAMS": "请按本条 message 修正参数类型/取值后重试。",
    "UPSTREAM_MISSING": "请先完成前置工具（通常是 generate_band），再重试本工具。",
    "NO_MARKET_DATA": "表中没有市场分位列，请跳过市场对标，改用带宽分析。",
    "NO_BAND_DATA": "请先调用 generate_band 生成带宽。",
    "UNKNOWN_TOOL": "请从 tools/list 返回的工具名中选择。",
    "NOT_IMPLEMENTED": "该工具尚未交付，请改用其它工具完成当前目标，并向用户说明。",
    "SANDBOX_VIOLATION": "请改写代码：禁止文件读写、网络、子进程与白名单外的 import。",
    "SANDBOX_TIMEOUT": "请缩小数据范围或把逻辑拆成多次执行。",
    "INTERNAL_ERROR": "请记录 detail 并向用户说明，可尝试更简单的参数重跑。",
}


# =============================================================================
# 二、工具规格
# =============================================================================


@dataclass(frozen=True)
class ToolSpec:
    """
    一个工具的完整对外规格 + 内部绑定。

    字段分两类：
      * 对外（进 JSON Schema / tools/list）：name / title / description / parameters
      * 内部（只有 registry 用）：module / attr / arg_map / adapter / timeout_s ...
    """

    name: str
    title: str
    description: str
    module: str                                   # 相对 src.tools 的模块名
    attr: str                                     # 模块里的函数名
    parameters: Dict[str, Dict[str, Any]]         # 扁平 DSL（见 §4 契约）
    stage: int                                    # 推荐调用顺序，main.py 的流水线用
    timeout_s: int = 30
    concurrency_safe: bool = False                # 对应 TS 侧 isConcurrencySafe
    arg_map: Dict[str, str] = field(default_factory=dict)   # 契约名 → 真实 kwarg 名
    adapter: Optional[str] = None                 # 本模块内的适配函数名（复杂改写）


# ---------------------------------------------------------------------------
# 参数适配器：把「对模型友好的扁平参数」翻译成「计算函数的真实 kwargs」
# ---------------------------------------------------------------------------
# 为什么需要适配器而不是直接暴露真实签名：
#   计算函数有若干 Union 类型入参（如 band.generate_band 的
#   spread: Union[str, float, Dict[str, float]]）。JSON Schema 里给模型一个
#   "可能是字符串也可能是对象" 的参数，模型出错率极高；
#   拆成两个语义明确的参数、在这里合并，是对模型最友好、也最不易错的写法。


def _adapt_generate_band(args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """把 spread_mode / spread_uniform / spread_by_level 三参数合并成 band 的 spread。"""
    warns: List[str] = []
    out = dict(args)
    mode = str(out.pop("spread_mode", "tier_default") or "tier_default")
    uniform = out.pop("spread_uniform", None)
    by_level = out.pop("spread_by_level", None)

    if mode == "uniform":
        if uniform is None:
            warns.append("spread_mode=uniform 但未给 spread_uniform，已回退按职级分层默认幅度。")
            out["spread"] = "auto"
        else:
            out["spread"] = float(uniform)
    elif mode == "explicit":
        if not isinstance(by_level, dict) or not by_level:
            warns.append("spread_mode=explicit 但未给 spread_by_level，已回退按职级分层默认幅度。")
            out["spread"] = "auto"
        else:
            out["spread"] = {str(k): float(v) for k, v in by_level.items()}
    else:
        out["spread"] = "auto"
    return out, warns


def _adapt_desensitize(args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    脱敏工具作用于**文件**（loader.desensitize 的真实契约）。
    模型往往只记得 session_id，因此这里允许省略 file_path，由会话 meta 回填。
    """
    warns: List[str] = []
    out = dict(args)
    sid = out.pop("session_id", None)
    if not out.get("file_path"):
        if not sid:
            raise CompToolError(
                "脱敏需要 file_path（待脱敏的原始文件），或提供 session_id 以便自动定位来源文件。",
                hint="请补 file_path，或先 load_salary_data 后把返回的 session_id 传进来。",
            )
        from .session import get_store  # 懒加载，避免顶层循环导入
        session = get_store().load(str(sid))
        src = session.meta.get("source_file")
        if not src:
            raise CompToolError(
                f"会话 {sid} 的 meta 里没有 source_file，无法定位待脱敏文件。",
                hint="请显式传入 file_path。",
            )
        out["file_path"] = src
        warns.append(f"file_path 未给出，已按会话 {sid} 的来源文件回填：{src}")
    return out, warns


def _adapt_generate_report(args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """formats:['md','html'] → report.generate_report 的 fmt:'both'。"""
    warns: List[str] = []
    out = dict(args)
    formats = out.pop("formats", None)
    if formats:
        vals = {str(f).strip().lower() for f in formats}
        vals = {"markdown" if v in ("md", "markdown") else v for v in vals}
        if {"markdown", "html"} <= vals:
            out["fmt"] = "both"
        elif "html" in vals:
            out["fmt"] = "html"
        else:
            out["fmt"] = "markdown"
    return out, warns


def _adapt_simulate_increase(args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """green_target 允许 'band_min' 或数字字符串（如 '0.85'）；数字形态转 float。"""
    warns: List[str] = []
    out = dict(args)
    gt = out.get("green_target")
    if isinstance(gt, str) and gt.strip() and gt.strip() != "band_min":
        try:
            out["green_target"] = float(gt)
        except ValueError:
            warns.append(f"green_target={gt!r} 无法解析为数字，已回退 'band_min'。")
            out["green_target"] = "band_min"
    return out, warns


ADAPTERS: Dict[str, Callable[[Dict[str, Any]], Tuple[Dict[str, Any], List[str]]]] = {
    "_adapt_generate_band": _adapt_generate_band,
    "_adapt_desensitize": _adapt_desensitize,
    "_adapt_generate_report": _adapt_generate_report,
    "_adapt_simulate_increase": _adapt_simulate_increase,
}


# =============================================================================
# 三、11 个工具的规格清单（对外契约的单一真理源）
# =============================================================================
# stage 用于 main.py 的「一键全流程」排序，也提示模型的推荐调用顺序。

# -----------------------------------------------------------------------------
# LLM 边界契约（confirm_mapping 专用）—— 「确定性优先」原则的可执行版本
# -----------------------------------------------------------------------------
# 设计意图：表头匹配是**确定性算法问题**，不是语义理解问题。词根路由引擎已经
# 把 90%+ 的列精确命中了，模型不该在这部分"发挥"；模型唯一的价值在于处理引擎
# 主动标记为 ambiguous 的那几列（引擎知道自己不知道，这才是真需要语义的地方）。
#
# 因此这里把模型可改动的列**收成一个白名单**，而不是留一句"请复核映射"的软建议
# —— 软建议在实践中必然被模型理解为"我可以随便改"，最终把 100 分的精确命中
# 改成 70 分的似是而非。白名单 = auto_confidence.ambiguous_columns。
#
# 本常量同时被 main.py 的交互式确认复用（程序问人与模型问人同口径），
# 避免两份文案各自漂移。
LLM_MAPPING_CONTRACT: Tuple[str, ...] = (
    # 1) 授权范围：只准动 ambiguous 列
    "【不得覆盖】只有 load_salary_data 返回的 auto_confidence.ambiguous_columns "
    "中列出的列，才允许你给出与 suggested_mapping.suggest 不同的映射；"
    "其余列——无论 confidence 高低、无论你觉得是否更合理——都必须原样采用 Python 的建议。"
    "理由：非 ambiguous 列是词根路由引擎精确命中（confidence>=90）的结果，"
    "属于确定性结论，模型的'语感'在这里只会降低准确率。",

    # 2) 选择空间：只能从 candidates 里选
    "【封闭候选】ambiguous 列的取值只能来自该列 candidates 列表中的标准字段名，"
    "不得自行拼写字段名、不得映射到 candidates 之外的任何标准字段。"
    "确需映射到 candidates 之外时，说明引擎的候选集有遗漏，应回问用户而非自行扩展。",

    # 3) 决策依据：必须看示例值
    "【看值不看名】决策前必须读取该列的 sample_values（该列前 5 个非空示例值）"
    "与 reasons（引擎判歧义的具体原因），结合量纲、格式、取值分布来判断，"
    "不能只凭列名字面意思猜。例：列名叫'薪资'但示例值是 180000 → 是年薪不是月薪。",

    # 4) 兜底：判不了就回问，不许猜
    "【不许硬猜】若 sample_values 仍不足以判定（全为空、取值与所有候选都对不上、"
    "或存在两种以上合理解释），返回 need_user_clarification 并向用户回问该列含义，"
    "回问时须同时给出 candidates 供用户选择、给出 sample_values 供用户对照。"
    "严禁在候选里'挑一个最接近的'蒙混过关——薪酬表字段搞错的代价是整份诊断结论错误。",
)


TOOL_SPECS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name="load_salary_data",
        title="加载薪酬数据",
        description=(
            "读取薪酬表（csv/xlsx），自动清洗脏数据（重复行、非数字薪资、绩效等级大小写）"
            "并给出字段映射建议。必须在其他分析工具之前调用。"
            "只返回前 5 行预览与统计摘要，绝不返回全量明细。"
            "返回的 auto_confidence.ambiguous_columns 是**唯一允许你修改映射的列白名单**，"
            "其余列照抄 suggested_mapping 即可（详见 confirm_mapping 的边界契约）。"
        ),
        module="loader", attr="load_salary_data", stage=1, timeout_s=60,
        concurrency_safe=False,
        parameters={
            "file_path": {"type": "string", "required": True,
                          "description": "csv/xlsx 绝对路径，Windows 下用 C:/... 正斜杠"},
            "sheet_name": {"type": "string", "description": "xlsx 的 sheet 名，缺省第一个"},
            "session_id": {"type": "string",
                           "description": "传入则复用并覆盖该会话的数据表；留空新建会话"},
        },
    ),
    ToolSpec(
        name="confirm_mapping",
        title="确认字段映射",
        description=(
            "确认或修正字段映射，把原始列名标准化为内部 18 个标准字段。"
            "映射固化后全部下游分析工具才可用。mapping 的键是原始列名、值是标准字段名。"
            "\n\n【LLM 边界契约 —— 表头匹配是确定性算法问题，模型的授权范围被严格限制】\n"
            + "\n".join(f"{i}. {c}" for i, c in enumerate(LLM_MAPPING_CONTRACT, 1))
            + "\n\n一句话总结：非 ambiguous 列照抄 suggested_mapping，"
            "ambiguous 列在 candidates 里挑，看 sample_values 再挑，挑不出来就问人。"
        ),
        module="loader", attr="confirm_mapping", stage=2,
        concurrency_safe=False,
        parameters={
            "session_id": {"type": "string", "required": True,
                           "description": "load_salary_data 返回的会话 ID"},
            "mapping": {"type": "object", "required": True,
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "原始列名 → 标准字段名，如 {'工号':'emp_id','基本工资(元/月)':'monthly_salary'}。"
                            "值必须来自 schemas.CANONICAL_FIELDS；"
                            "对 ambiguous 列，还必须是该列 candidates 之一（见边界契约第 2 条）。"
                        )},
            "source_file": {"type": "string", "description": "数据来源说明（与加载时不同才需传）"},
        },
    ),
    ToolSpec(
        name="desensitize_data",
        title="生成脱敏副本",
        description=(
            "对薪酬文件生成脱敏副本用于外发/演示：姓名→姓+**、员工 ID→加盐哈希、"
            "薪资乘随机扰动并做总额守恒回缩、司龄分箱、删除身份证/手机号等直接标识列。"
            "只写出新文件，不改动会话内的原数据。"
        ),
        module="loader", attr="desensitize", stage=0, timeout_s=60,
        concurrency_safe=True, adapter="_adapt_desensitize",
        parameters={
            "file_path": {"type": "string",
                          "description": "待脱敏的原始文件；省略时按 session_id 的来源文件自动回填"},
            "session_id": {"type": "string", "description": "用于自动定位来源文件"},
            "output_path": {"type": "string",
                            "description": "输出路径；缺省在同目录生成 {原名}_desensitized.csv"},
            "salary_jitter": {"type": "number",
                              "description": "薪资扰动幅度，0.15=±15%（默认）。越大越不可还原、统计特征失真越多"},
            "keep_ratio": {"type": "boolean",
                           "description": "是否做总额守恒回缩使薪资总额恢复原值，默认 true（强烈建议保持）"},
            "id_mode": {"type": "string", "enum": ["hash", "seq"],
                        "description": "hash=加盐哈希（不可逆，默认）；seq=序号（可逆，仅内部流转）"},
            "seed": {"type": "number", "description": "随机种子；给出则可复现（演示/回归用），生产建议留空"},
            "sheet_name": {"type": "string", "description": "xlsx 的 sheet 名"},
        },
    ),
    ToolSpec(
        name="analyze_current_state",
        title="薪酬现状诊断",
        description=(
            "薪酬现状诊断：为每位员工计算 CR（月薪/带宽中位值）与带宽渗透率，"
            "判定红圈（超上限/CR 过高）、绿圈（低于下限/CR 过低），并测算年化成本溢出与补足金额。"
            "前置：必须已 generate_band。"
        ),
        module="diagnose", attr="analyze_current_state", stage=4,
        concurrency_safe=True,
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID"},
            "red_cr": {"type": "number", "description": "红圈 CR 阈值，缺省 1.20"},
            "green_cr": {"type": "number", "description": "绿圈 CR 阈值，缺省 0.80"},
            "level_field": {"type": "string", "description": "职级列名，缺省 level"},
            "salary_field": {"type": "string", "description": "月薪列名，缺省 monthly_salary"},
            "annual_months": {"type": "integer", "description": "年化月数，缺省 12"},
            "write_back": {"type": "boolean", "description": "是否把 CR/渗透率/圈层列回写会话，缺省 true"},
        },
    ),
    ToolSpec(
        name="generate_band",
        title="生成薪酬带宽",
        description=(
            "生成薪酬带宽表并诊断相邻职级重叠度。"
            "下限=中位值/(1+带宽幅度/2)，上限=下限×(1+带宽幅度)；"
            "带宽幅度采用相对下限口径 (上限−下限)/下限。"
            "mode=optimize 基于现状中位数优化（默认）；mode=new 按锚点+级差全新设计并做成本中性拟合。"
        ),
        module="band", attr="generate_band", stage=3, timeout_s=60,
        concurrency_safe=True, adapter="_adapt_generate_band",
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID（映射须已确认）"},
            "mode": {"type": "string", "enum": ["optimize", "new"],
                     "description": "optimize=基于现状优化（默认）；new=全新设计（等比阶梯+成本中性拟合）"},
            "levels": {"type": "array", "items": {"type": "string"},
                       "description": "参与设计的职级序列；缺省用数据中出现的全部职级并自动排序"},
            "midpoint_source": {"type": "string",
                                "enum": ["current_median", "market_p50", "market_custom", "explicit"],
                                "description": "中位值来源：current_median=现状中位数（内部公平，默认）；"
                                               "market_p50=表内市场 P50（外部竞争力）；"
                                               "market_custom/explicit=用 midpoint_custom"},
            "midpoint_custom": {"type": "object", "additionalProperties": {"type": "number"},
                                "description": "{职级: 中位值}，market_custom / explicit 模式必填"},
            "midpoint_diff": {"type": "number",
                              "description": "相邻职级中位值级差，如 0.15 表示每级 +15%；缺省 0.15"},
            "spread_mode": {"type": "string", "enum": ["tier_default", "uniform", "explicit"],
                            "description": "带宽幅度来源：tier_default=按职级分层（基层 25%→高层 60%，默认）；"
                                           "uniform=全局统一用 spread_uniform；explicit=用 spread_by_level"},
            "spread_uniform": {"type": "number", "description": "spread_mode=uniform 时的统一带宽幅度，如 0.35"},
            "spread_by_level": {"type": "object", "additionalProperties": {"type": "number"},
                                "description": "spread_mode=explicit 时逐职级指定，如 {'P1':0.25,'M3':0.60}"},
            "anchor_level": {"type": "string", "description": "new 模式的锚点职级；缺省最低职级"},
            "round_to": {"type": "integer", "description": "取整步长，100=取整到百元（默认）；0=不取整"},
            "export": {"type": "boolean", "description": "是否导出带宽表 CSV，缺省 true"},
            "write_back": {"type": "boolean", "description": "是否把带宽三列回写会话，缺省 true"},
        },
    ),
    ToolSpec(
        name="market_benchmark",
        title="市场对标",
        description=(
            "把公司各职级/岗位序列薪资与市场分位值（P25/P50/P75）对标，"
            "按薪酬策略计算差距率、市场对标指数与追平目标分位的年化成本。"
            "策略 auto 按岗位序列取（销售/技术 P75、管理/职能 P50、操作 P25）。"
        ),
        module="market", attr="market_benchmark", stage=5,
        concurrency_safe=True,
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID"},
            "strategy": {"type": "string", "enum": ["auto", "P25", "P50", "P75"],
                         "description": "全局对标分位；auto=按岗位序列默认策略（缺省）"},
            "strategy_by_family": {"type": "object", "additionalProperties": {"type": "string"},
                                   "description": "岗位序列 → 分位，如 {'销售':'P75','职能':'P50'}"},
            "group_by": {"type": "array", "items": {"type": "string"},
                         "description": "分组维度，取值 level / job_family / dept；缺省 ['level']"},
            "make_charts": {"type": "boolean", "description": "是否出市场差距图，缺省 true"},
        },
    ),
    ToolSpec(
        name="simulate_increase",
        title="调薪模拟",
        description=(
            "在给定调薪总预算下模拟 4 种分配策略并对比："
            "A=平均分配、B=优先补绿圈、C=按绩效加权、D=优先保留（红圈冻结）。"
            "输出各策略成本、涨幅分布、调薪前后红绿圈人数与 CR 变化，并给出推荐策略与理由。"
            "预算守恒：各策略总成本严格等于预算额。"
        ),
        module="increase", attr="simulate_increase", stage=6, timeout_s=120,
        concurrency_safe=True, adapter="_adapt_simulate_increase",
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID"},
            "budget_pct": {"type": "number", "required": True,
                           "description": "调薪预算占调薪前年度薪资总额的比例，如 0.06 = 6%"},
            "strategies": {"type": "array", "items": {"type": "string", "enum": ["A", "B", "C", "D"]},
                           "description": "要模拟的策略，缺省 ['A','B','C','D']"},
            "custom_weights": {"type": "object", "additionalProperties": {"type": "number"},
                               "description": "自定义绩效权重，如 {'A':2.0,'B':1.2,'C':0.4,'D':0}；缺省取 schemas.PERF_WEIGHTS"},
            "cap_pct": {"type": "number", "description": "个人单次涨幅上限，如 0.20（缺省 0.20）"},
            "green_target": {"type": "string",
                             "description": "策略 B 的补底目标：'band_min'（缺省）或 CR 数值字符串如 '0.85'"},
            "frozen_for_red": {"type": "boolean",
                               "description": "策略 D 下红圈是否完全冻结且不占调薪池，缺省 true"},
            "make_charts": {"type": "boolean", "description": "是否出前后对比图与成本图，缺省 true"},
        },
    ),
    ToolSpec(
        name="simulate_pay_mix",
        title="固浮比模拟",
        description=(
            "按岗位序列模拟固定/浮动薪酬结构：固定部分=年度总现金×固定占比，"
            "浮动目标=年度总现金×浮动占比，实际总收入=固定部分+浮动目标×业绩达成率。"
            "输出各序列现状与目标固浮比、收入-达成率曲线、下行风险与上行空间。"
        ),
        module="paymix", attr="simulate_pay_mix", stage=7,
        concurrency_safe=True,
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID"},
            "target_mix": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "岗位序列 → 目标固浮比 '固定:浮动'，如 {'销售':'40:60'}；缺省取 schemas.JOB_FAMILY_PAY_MIX"},
            "mix_source": {"type": "string", "enum": ["family_default", "existing", "explicit"],
                           "description": "固浮比来源，缺省 family_default"},
            "achievement_range": {"type": "array", "items": {"type": "number"},
                                  "description": "业绩达成率扫描区间 [min,max,step]，缺省 [0,1.5,0.1]"},
            "make_charts": {"type": "boolean", "description": "是否出收入曲线图，缺省 true"},
        },
    ),
    ToolSpec(
        name="calc_job_score",
        title="岗位价值评估",
        description=(
            "用海氏三要素法（hay）或美世 IPE 四因素法（mercer）给岗位打分，"
            "输出各要素加权得分、岗位分值（job_score = round(原始分×160)）与建议职级。"
            "子维度按 1-10 分打分；缺失维度按同要素均值兜底并记警告。"
            "注意：本项目为简易打分表，非正式认证评估。"
        ),
        module="jobeval", attr="calc_job_score", stage=8,
        concurrency_safe=True,
        parameters={
            "session_id": {"type": "string", "description": "会话 ID（批量打分或回写职级建议时需要）"},
            "model": {"type": "string", "enum": ["hay", "mercer"], "required": True,
                      "description": "hay=海氏三要素；mercer=美世 IPE 四因素"},
            "position": {"type": "string", "description": "岗位名称，用于输出标题"},
            "scores": {"type": "object", "additionalProperties": {"type": "number"},
                       "description": "子维度 → 1-10 分，子维度名见 schemas.JOB_EVAL_MODELS[model]"},
            "batch_file": {"type": "string", "description": "批量打分表路径（csv/xlsx），与 scores 二选一"},
            "template_out": {"type": "string", "description": "指定则导出空白打分模板到该路径"},
        },
    ),
    ToolSpec(
        name="generate_report",
        title="生成诊断报告",
        description=(
            "汇总全部上游分析结果，生成七章结构的薪酬诊断报告"
            "（执行摘要/数据概览/现状诊断/带宽与市场/调薪模拟/固浮比/落地路线图），"
            "输出 Markdown 与内嵌图表的 HTML。缺数据的章节会降级为提示文案而不中断。"
        ),
        module="report", attr="generate_report", stage=9, timeout_s=120,
        concurrency_safe=False, adapter="_adapt_generate_report",
        parameters={
            "session_id": {"type": "string", "required": True, "description": "会话 ID"},
            "title": {"type": "string", "description": "报告标题，缺省「薪酬诊断报告」"},
            "include_sections": {"type": "array", "items": {"type": "string"},
                                 "description": "只生成指定章节（中文标题/子串或 '1'-'7' 序号）；缺省全部 7 章"},
            "formats": {"type": "array", "items": {"type": "string", "enum": ["md", "html"]},
                        "description": "输出格式，缺省 ['md','html']"},
        },
    ),
    ToolSpec(
        name="run_comp_code",
        title="运行薪酬分析代码",
        description=(
            "在受限 Python 沙箱中执行一段分析代码，用于批量编排薪酬工具、或直接用 pandas 做自定义分析。"
            "可用：pandas as pd、numpy as np、math、statistics、json；"
            "已注入 tools 对象（其余 10 个薪酬工具的同步封装）与 comp 会话对象（comp.df() 取只读数据副本）。"
            "禁止：文件读写、网络、子进程、导入白名单外模块、访问 __class__/__globals__ 等双下划线属性。"
            "每次运行全新环境，只有 print() 的内容与赋值给 result 的对象会返回。超时 15 秒。"
        ),
        module="sandbox", attr="run_comp_code", stage=10, timeout_s=30,
        concurrency_safe=False,
        parameters={
            "code": {"type": "string", "required": True, "description": "Python 代码（同步，不支持 async）"},
            "description": {"type": "string", "required": True, "description": "一句话说明这段代码做什么"},
            "session_id": {"type": "string", "description": "会话 ID，沙箱内通过 comp/tools 访问该会话"},
            "timeout_s": {"type": "number", "description": "超时秒数，缺省 15，上限 60"},
        },
    ),
)

TOOLS_BY_NAME: Dict[str, ToolSpec] = {s.name: s for s in TOOL_SPECS}
TOOL_NAMES: Tuple[str, ...] = tuple(s.name for s in TOOL_SPECS)

assert len(TOOL_SPECS) == 11, f"工具数必须是 11，当前 {len(TOOL_SPECS)}"


# =============================================================================
# 四、无损 JSON 规整（架构 §8 风险 1 的唯一防线）
# =============================================================================


def to_lossless(obj: Any, _depth: int = 0) -> Any:
    """
    把任意 Python/pandas/numpy 对象转成**可无损 json.dumps 的纯 JSON 值**。

    处理的每一类都对应一次真实事故：
      * `float('nan')` / `inf`  → None：`json.dumps` 默认会写出裸 `NaN`（非法 JSON），
        而 dsh 侧 `JSON.parse` 直接抛错；即便宿主容忍，`NaN` 也会在 TS 侧变成 `null`
        导致「Python 发出的」与「TS 收到的」不一致 —— 往返校验失败。
      * `np.int64` / `np.float32` → int/float：`json.dumps` 不认识 numpy 标量。
      * `pd.Timestamp` / `datetime` → ISO 字符串。
      * `pd.DataFrame` / `Series` → records，并**截断到 MAX_INLINE_ROWS**：
        明细必须落盘走 artifacts（架构 §5.3 R1），这里的截断是兜底而非正路。
      * `set` / `tuple` → list；非 str 的 dict key → str（JSON 只允许字符串键）。
      * 递归深度上限 12：防御自引用结构造成栈溢出。
    """
    if _depth > 12:
        return "<max-depth>"

    # --- 标量 ---------------------------------------------------------------
    # bool 必须在 int 之前判（bool 是 int 的子类，否则 True 会变成 1）
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else float(obj)

    # --- numpy / pandas（懒 import，避免无 pandas 环境下不可用）--------------
    try:
        import numpy as np
    except Exception:  # pragma: no cover - 环境无 numpy 时退化为纯 Python 处理
        np = None  # type: ignore
    if np is not None:
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            v = float(obj)
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(obj, np.ndarray):
            return [to_lossless(x, _depth + 1) for x in obj.tolist()]

    try:
        import pandas as pd
    except Exception:  # pragma: no cover
        pd = None  # type: ignore
    if pd is not None:
        if obj is pd.NaT:
            return None
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if isinstance(obj, pd.DataFrame):
            head = obj.head(MAX_INLINE_ROWS)
            payload = {
                "__kind__": "dataframe",
                "rows": int(obj.shape[0]),
                "cols": int(obj.shape[1]),
                "columns": [str(c) for c in obj.columns],
                "records": to_lossless(head.to_dict(orient="records"), _depth + 1),
            }
            if obj.shape[0] > MAX_INLINE_ROWS:
                payload["truncated"] = True
                payload["note"] = f"仅内联前 {MAX_INLINE_ROWS} 行；完整明细请取 artifacts 里的 CSV。"
            return payload
        if isinstance(obj, pd.Series):
            return to_lossless(obj.to_dict(), _depth + 1)
        try:
            if pd.isna(obj):  # 兜住 pd.NA 等标量缺失值
                return None
        except (TypeError, ValueError):
            pass

    # --- 日期 ---------------------------------------------------------------
    import datetime as _dt
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()

    # --- 容器 ---------------------------------------------------------------
    if isinstance(obj, dict):
        return {str(k): to_lossless(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_lossless(x, _depth + 1) for x in obj]
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    # --- 兜底：不可序列化对象一律转字符串（绝不让它到 json.dumps 那一步才崩）--
    return str(obj)


# =============================================================================
# 五、信封归一化：errors.py 形态 → 架构 §4.0 统一信封
# =============================================================================

# ok=True 时这些键有专门归属，不进 data
_RESERVED_TOP_KEYS = {"ok", "error", "code", "message", "hint", "detail",
                      "warnings", "artifacts", "charts", "meta", "session_id"}

# 这些键的值是路径，自动升级为 artifacts / charts 句柄
_ARTIFACT_KEYS = {
    "output_csv": ("table", "带宽表 / 明细表 CSV"),
    "report_path": ("markdown", "Markdown 报告"),
    "html_path": ("html", "HTML 报告"),
    "output_path": ("file", "输出文件"),
    "template_path": ("file", "打分模板"),
}


def make_envelope(ok: bool, code: str, message: str, *,
                  data: Optional[Dict[str, Any]] = None,
                  hint: Optional[str] = None,
                  detail: Optional[str] = None,
                  artifacts: Optional[List[Dict[str, Any]]] = None,
                  charts: Optional[List[Dict[str, Any]]] = None,
                  warnings: Optional[Sequence[str]] = None,
                  meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造架构 §4.0 信封。成功与失败**同构**，模型只需看 ok / code 两个字段分支。"""
    env: Dict[str, Any] = {
        "ok": bool(ok),
        "code": code,
        "message": message,
        "data": data if isinstance(data, dict) else {},
        "artifacts": list(artifacts or []),
        "charts": list(charts or []),
        "warnings": [str(w) for w in (warnings or [])],
        "meta": dict(meta or {}),
    }
    if hint:
        env["hint"] = hint
    if detail:
        env["detail"] = detail
    return env


def error_envelope(code: str, message: str, *, hint: Optional[str] = None,
                   detail: Optional[str] = None,
                   meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """失败信封。`hint` 缺省时按错误码取默认「下一步指令」。"""
    return make_envelope(False, code, message,
                         hint=hint or DEFAULT_HINTS.get(code),
                         detail=detail, meta=meta)


def _collect_artifacts(raw: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    从工具原始返回里挑出产物路径，升级成 artifacts / charts 句柄。

    这样做的价值：模型上下文里只留一句「明细见 assets/xxx.csv」，
    而不是 150 行薪资明细 —— 既省 token 又兑现「薪资明细不进模型上下文」的安全承诺。
    """
    artifacts: List[Dict[str, Any]] = []
    charts: List[Dict[str, Any]] = []

    for key, (kind, label) in _ARTIFACT_KEYS.items():
        path = raw.get(key)
        if isinstance(path, str) and path:
            artifacts.append({"id": key, "kind": kind, "label": label, "path": path})

    # report.py 的 figures：[{name, caption, html_path, png_path, rel_path, exists, is_png}]
    figures = raw.get("figures")
    if isinstance(figures, list):
        for fig in figures:
            if not isinstance(fig, dict):
                continue
            path = fig.get("html_path") or fig.get("png_path") or fig.get("rel_path")
            if not path:
                continue
            charts.append({
                "id": str(fig.get("name") or fig.get("caption") or "chart"),
                "kind": "png" if fig.get("is_png") else "html",
                "label": str(fig.get("caption") or fig.get("name") or ""),
                "path": str(path),
            })

    # charts.py 的 build_all_charts 形态：{图名: {html_path, png_path, ...}}
    chart_map = raw.get("charts")
    if isinstance(chart_map, dict):
        for name, fig in chart_map.items():
            if not isinstance(fig, dict):
                continue
            path = fig.get("html_path") or fig.get("png_path")
            if path:
                charts.append({"id": str(name), "kind": "html" if fig.get("html_path") else "png",
                               "label": str(name), "path": str(path)})
    return artifacts, charts


def _fallback_summary(tool: str, spec: Optional[ToolSpec], data: Dict[str, Any],
                      message: str) -> str:
    """
    上游没给 `summary_md` 时兜底生成一份。

    **这是降级路径，不是正路** —— 架构 §4.0 要求每个工具自己产出含表格的中文
    `summary_md`（模型真正读到的就是它）。这里只保证「即使工程师漏写，模型也不至于
    读到一个空字符串」，并显式打上降级标记，便于 QA 一眼发现哪个工具漏了。
    """
    title = spec.title if spec else tool
    lines = [f"### {title}", "", message or "执行成功。", ""]
    scalars = [(k, v) for k, v in data.items()
               if isinstance(v, (str, int, float, bool)) and k != "summary_md"]
    if scalars:
        lines += ["| 字段 | 值 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in scalars[:15]]
        lines.append("")
    lines.append(f"> ⚠️ 本摘要由集成层兜底生成（`{tool}` 未提供 summary_md），信息量有限。")
    return "\n".join(lines)[:MAX_SUMMARY_CHARS]


def normalize_result(tool: str, raw: Any, *, session_id: Optional[str] = None,
                     elapsed_ms: int = 0,
                     extra_warnings: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """
    把计算模块的返回值翻译成架构 §4.0 信封，并做无损 JSON 规整。

    输入可能是三种形态（都来自 errors.py 的既有约定）：
      1. `{"ok": True, ...业务字段}`             → 业务字段归入 data
      2. `{"ok": False, "error": {code,...}}`   → 翻译错误码
      3. 非 dict（理论上不该出现）              → 包成 data.value 并记警告
    """
    spec = TOOLS_BY_NAME.get(tool)
    warns: List[str] = [str(w) for w in (extra_warnings or [])]
    meta: Dict[str, Any] = {
        "tool": tool,
        "session_id": session_id,
        "elapsed_ms": int(elapsed_ms),
        "contract_version": CONTRACT_VERSION,
    }

    # --- 形态 3：非 dict --------------------------------------------------
    if not isinstance(raw, dict):
        warns.append(f"{tool} 返回了非字典值（{type(raw).__name__}），已包装。集成层期望 dict。")
        env = make_envelope(True, "OK", "执行成功。",
                            data={"value": to_lossless(raw)}, warnings=warns, meta=meta)
        env["data"]["summary_md"] = _fallback_summary(tool, spec, env["data"], "执行成功。")
        return env

    # --- 形态 2：失败 ------------------------------------------------------
    if raw.get("ok") is False:
        err = raw.get("error") if isinstance(raw.get("error"), dict) else {}
        inner_code = str(err.get("code") or raw.get("code") or "COMP_ERROR")
        code = CODE_ALIASES.get(inner_code, inner_code)
        message = str(err.get("message") or raw.get("message") or "工具执行失败。")
        details = err.get("details") or raw.get("details")
        env = error_envelope(
            code, message,
            hint=err.get("hint") or raw.get("hint"),
            detail=None, meta=meta,
        )
        if details:
            env["data"] = {"error_details": to_lossless(details)}
        if warns:
            env["warnings"] = warns
        # 失败也要有 summary_md：模型读 content 时才能看到「为什么失败 + 下一步」
        env["data"]["summary_md"] = (
            f"### {spec.title if spec else tool} 执行失败\n\n"
            f"- **错误码**：`{code}`\n- **原因**：{message}\n"
            f"- **下一步**：{env.get('hint') or '请检查参数后重试。'}\n"
        )
        return env

    # --- 形态 1：成功 ------------------------------------------------------
    artifacts, charts = _collect_artifacts(raw)
    up_warnings = raw.get("warnings")
    if isinstance(up_warnings, (list, tuple)):
        warns.extend(str(w) for w in up_warnings)
    elif isinstance(up_warnings, str) and up_warnings:
        warns.append(up_warnings)

    data = {k: to_lossless(v) for k, v in raw.items()
            if k not in _RESERVED_TOP_KEYS and k not in ("figures",)}
    # figures 的路径已升级为 charts，但张数/说明对模型仍有用
    if isinstance(raw.get("figures"), list):
        data["figure_count"] = len(raw["figures"])

    message = str(raw.get("message") or (raw.get("hint") or "执行成功。"))
    if "summary_md" not in data or not str(data.get("summary_md") or "").strip():
        data["summary_md"] = _fallback_summary(tool, spec, data, message)
        warns.append(f"{tool} 未提供 summary_md，已由集成层兜底生成（请工程师补齐）。")

    sid = raw.get("session_id") or session_id
    meta["session_id"] = sid
    if isinstance(raw.get("meta"), dict):
        meta["tool_meta"] = to_lossless(raw["meta"])

    return make_envelope(True, "OK", message, data=data, hint=raw.get("hint"),
                         artifacts=artifacts, charts=charts, warnings=warns, meta=meta)


# =============================================================================
# 六、扁平 DSL → JSON Schema（与 dsh-tools 的编译规则同构）
# =============================================================================

# dsh `output.schema` 只支持 8 个结构关键字 + 4 个注解关键字（架构 §2.4）。
# 入参 schema 走的是另一条路径，但我们**主动对齐同一份白名单**，
# 这样 TS 侧无论用 defineTool（编译）还是 register（直传）都不会踩到不支持的关键字。
_ALLOWED_SCHEMA_KEYS = {
    "type", "oneOf", "properties", "required", "additionalProperties", "items",
    "enum", "const", "description", "title", "default", "examples",
}


def to_json_schema(parameters: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    把扁平 DSL 编译成顶层 JSON Schema。

    规则（与 dsh-tools 的 `parameterSchemaSpecToJsonSchema` 一致）：
      * 顶层键是**参数名**，不是 JSON Schema 关键字；
      * 各参数里的 `required: true` 被提取到顶层 `required` 数组；
      * 顶层 `additionalProperties: false`（拒绝模型臆造参数）。
    """
    props: Dict[str, Any] = {}
    required: List[str] = []
    for pname, pspec in parameters.items():
        node = {k: v for k, v in pspec.items()
                if k != "required" and k in _ALLOWED_SCHEMA_KEYS}
        unknown = [k for k in pspec if k != "required" and k not in _ALLOWED_SCHEMA_KEYS]
        if unknown:  # 白名单外关键字在宿主侧会直接报 unsupported，这里提前拦住
            raise ValueError(f"参数 {pname} 使用了不受支持的 schema 关键字：{unknown}")
        props[pname] = node
        if pspec.get("required") is True:
            required.append(pname)
    schema: Dict[str, Any] = {"type": "object", "properties": props,
                              "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


ENVELOPE_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "code": {"type": "string"},
        "message": {"type": "string"},
        "hint": {"type": "string"},
        "detail": {"type": "string"},
        "data": {},                                    # 开放节点：任意 JSON
        "artifacts": {"type": "array", "items": {}},
        "charts": {"type": "array", "items": {}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "meta": {},
    },
    "required": ["ok", "code", "message"],
    "additionalProperties": False,
}


def list_tools(*, with_json_schema: bool = True) -> List[Dict[str, Any]]:
    """
    返回 11 个工具的对外描述（`tools/list` 的响应体）。

    **不触发任何计算模块的 import** —— 因此即使 market.py 尚未交付，
    工具清单也永远是完整的 11 个（架构 §8 风险 2：插件不能因缺模块而少注册工具）。
    """
    out: List[Dict[str, Any]] = []
    for spec in TOOL_SPECS:
        item: Dict[str, Any] = {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "parameters": spec.parameters,
            "stage": spec.stage,
            "timeout_s": spec.timeout_s,
            "concurrency_safe": spec.concurrency_safe,
        }
        if with_json_schema:
            item["input_schema"] = to_json_schema(spec.parameters)
            item["output_schema"] = ENVELOPE_OUTPUT_SCHEMA
        out.append(item)
    return out


# =============================================================================
# 七、调用：懒加载 handler + 参数适配 + 计时 + 异常兜底
# =============================================================================

_HANDLER_CACHE: Dict[str, Callable[..., Any]] = {}


def resolve_handler(spec: ToolSpec) -> Callable[..., Any]:
    """
    懒加载并缓存 handler。

    抛 `CompToolError(NOT_IMPLEMENTED)` 而不是 ImportError —— 因为
    「模块还没交付」对模型来说是一个**可绕过的业务状况**（换个工具继续），
    而不是一次系统故障。
    """
    if spec.name in _HANDLER_CACHE:
        return _HANDLER_CACHE[spec.name]
    try:
        module = importlib.import_module(f".{spec.module}", package=__package__)
    except ImportError as exc:
        err = CompToolError(
            f"工具 {spec.name} 依赖的模块 src/tools/{spec.module}.py 尚不可用：{exc}",
            hint=DEFAULT_HINTS["NOT_IMPLEMENTED"],
            details={"module": spec.module, "tool": spec.name},
        )
        err.code = "NOT_IMPLEMENTED"  # type: ignore[misc]
        raise err from exc
    fn = getattr(module, spec.attr, None)
    if not callable(fn):
        err = CompToolError(
            f"模块 src/tools/{spec.module}.py 里找不到函数 {spec.attr}()。",
            hint=DEFAULT_HINTS["NOT_IMPLEMENTED"],
            details={"module": spec.module, "attr": spec.attr, "tool": spec.name},
        )
        err.code = "NOT_IMPLEMENTED"  # type: ignore[misc]
        raise err
    _HANDLER_CACHE[spec.name] = fn
    return fn


def _prepare_kwargs(spec: ToolSpec, fn: Callable[..., Any],
                    arguments: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    契约参数 → 真实 kwargs：改名 → 适配器 → 按真实签名过滤。

    最后一步的过滤是**抗签名漂移**的关键：计算模块由并行开发的工程师交付，
    若某个可选参数还没实现，宁可丢弃并记一条 warning，也不要因 TypeError
    让整个工具调用失败（模型看到 TypeError 通常无法自行纠正）。
    """
    warns: List[str] = []
    args = {k: v for k, v in arguments.items() if v is not None}

    for contract_name, real_name in spec.arg_map.items():
        if contract_name in args:
            args[real_name] = args.pop(contract_name)

    if spec.adapter:
        args, adapter_warns = ADAPTERS[spec.adapter](args)
        warns.extend(adapter_warns)

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - 内建/C 函数才会走到
        return args, warns

    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in sig.parameters.values())
    if accepts_kwargs:
        return args, warns

    known = set(sig.parameters)
    dropped = sorted(set(args) - known)
    if dropped:
        warns.append(
            f"{spec.name} 的实现暂不支持参数 {dropped}，已忽略（结果按默认行为计算）。"
        )
        args = {k: v for k, v in args.items() if k in known}
    return args, warns


def call_tool(name: str, arguments: Optional[Dict[str, Any]] = None, *,
              session_id: Optional[str] = None) -> Dict[str, Any]:
    """
    调用一个工具，**永远返回架构 §4.0 信封**（绝不抛异常）。

    这是 server.py / main.py / sandbox.py 的唯一调用口。
    """
    started = time.perf_counter()
    arguments = dict(arguments or {})
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return error_envelope(
            "UNKNOWN_TOOL", f"没有名为 {name!r} 的工具。",
            detail=f"可用工具：{', '.join(TOOL_NAMES)}",
            meta={"tool": name, "contract_version": CONTRACT_VERSION},
        )

    # session_id 三级回退（架构 §5.3）：显式传参 > 宿主会话 id > 'default'
    sid = arguments.get("session_id") or session_id
    if sid and "session_id" in spec.parameters:
        arguments["session_id"] = sid

    try:
        fn = resolve_handler(spec)
        kwargs, warns = _prepare_kwargs(spec, fn, arguments)
        raw = fn(**kwargs)
    except CompToolError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        code = CODE_ALIASES.get(exc.code, exc.code)
        env = error_envelope(code, exc.message, hint=exc.hint,
                             meta={"tool": name, "session_id": sid,
                                   "elapsed_ms": elapsed,
                                   "contract_version": CONTRACT_VERSION})
        if exc.details:
            env["data"] = {"error_details": to_lossless(exc.details)}
        env["data"]["summary_md"] = (
            f"### {spec.title} 执行失败\n\n- **错误码**：`{code}`\n"
            f"- **原因**：{exc.message}\n- **下一步**：{exc.hint}\n"
        )
        return env
    except TypeError as exc:
        # 参数过滤后仍然 TypeError → 多半是缺必填参数，属于模型可自行纠正的情况
        elapsed = int((time.perf_counter() - started) * 1000)
        return error_envelope(
            "INVALID_PARAMS", f"调用 {name} 的参数不合法：{exc}",
            detail=f"该工具的参数定义见 tools/list 的 input_schema。",
            meta={"tool": name, "session_id": sid, "elapsed_ms": elapsed,
                  "contract_version": CONTRACT_VERSION},
        )
    except Exception as exc:  # noqa: BLE001 - 集成层必须兜住一切
        elapsed = int((time.perf_counter() - started) * 1000)
        return error_envelope(
            "INTERNAL_ERROR",
            f"{name} 执行时发生未预期错误：{type(exc).__name__}: {exc}",
            detail=f"{type(exc).__name__}: {exc}",
            meta={"tool": name, "session_id": sid, "elapsed_ms": elapsed,
                  "contract_version": CONTRACT_VERSION},
        )

    elapsed = int((time.perf_counter() - started) * 1000)
    return normalize_result(name, raw, session_id=sid, elapsed_ms=elapsed,
                            extra_warnings=warns)


def tool_names() -> List[str]:
    """返回全部工具名（沙箱注入 tools 对象时用）。"""
    return list(TOOL_NAMES)


def implemented_status() -> Dict[str, bool]:
    """
    探测每个工具的 handler 是否已可用（QA 与 main.py 的 --status 用）。
    不执行任何计算，只做 import 探测。
    """
    status: Dict[str, bool] = {}
    for spec in TOOL_SPECS:
        try:
            resolve_handler(spec)
            status[spec.name] = True
        except Exception:  # noqa: BLE001
            status[spec.name] = False
    return status


__all__ = [
    "CONTRACT_VERSION", "ToolSpec", "TOOL_SPECS", "TOOLS_BY_NAME", "TOOL_NAMES",
    "ENVELOPE_OUTPUT_SCHEMA", "CODE_ALIASES", "DEFAULT_HINTS",
    "to_lossless", "make_envelope", "error_envelope", "normalize_result",
    "to_json_schema", "list_tools", "call_tool", "resolve_handler",
    "tool_names", "implemented_status",
]

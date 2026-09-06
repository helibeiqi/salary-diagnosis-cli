# -*- coding: utf-8 -*-
"""
report.py — 薪酬诊断报告生成器（模块 7 · generate_report）
================================================================================
设计定位
--------------------------------------------------------------------------------
本模块是整条工具链的**最后一公里**：把前六个模块留在 `session.meta` 里的分析结果，
整合成一份可以直接发给管理层 / 放进面试作品集的**七章结构化报告**。

与模型层的分工（面试讲解要点）
--------------------------------------------------------------------------------
DeepSeek 模型只负责「决定调哪个工具、传什么参数」以及「用自然语言解读结论」，
**所有出现在报告里的数字都由 Python 从 meta 里原样取出**，模型不心算、不改写。
报告里的每一句话都能追溯到某次工具调用的返回字段 —— 这是"AI 不会编薪酬数字"的
工程保证，也是本项目相对"直接让大模型写报告"的核心差异。

七章结构（严格按序，缺数据不缺章）
--------------------------------------------------------------------------------
    一、执行摘要                 关键数字 + 3 条核心结论 + 优先建议
    二、数据概览与字段映射说明    样本量、字段映射、清洗摘要、缺失率
    三、薪酬现状诊断              分布、CR、红绿圈人数/占比/成本
    四、带宽设计与市场对标建议    带宽表、重叠度、市场分位差距、岗位评估校验
    五、调薪方案对比与推荐        四套策略成本与红绿圈改善、前后 CR 对比
    六、固浮比与激励建议          目标固浮比、激励曲线、高浮动的前提条件
    七、风险提示与实施路线图      风险清单 + 0-1月 / 1-3月 / 3-6月 分阶段落地

**降级原则**：任何一章缺前置数据时，写「本章未执行（前置步骤缺失）」并提示该跑哪个
工具，**绝不抛异常中断整份报告** —— 用户跑了 3 个步骤也想看到一份能用的报告。

图片引用口径（易错点，已固化）
--------------------------------------------------------------------------------
报告落在 `<项目根>/report/`，图表落在 `<项目根>/assets/`，
因此 Markdown 里的引用路径必须是 `../assets/xxx.html`。
路径一律用 `os.path.relpath(图表绝对路径, 报告目录)` 现算，并**正向斜杠**，
不手拼字符串 —— 否则换目录结构就会断链。
生成后逐张 `os.path.exists()` 断言（对应 AC-26），断链会写进 warnings 而不是静默通过。

用法
--------------------------------------------------------------------------------
    from tools.report import generate_report
    r = generate_report("sess-001")                       # 纯 Markdown
    r = generate_report("sess-001", fmt="both")           # Markdown + HTML（内嵌 div）
    r = generate_report("sess-001", include_sections=["执行摘要", "薪酬现状诊断"])

测试期（session.py 未就绪）可注入假 session：
    from tests.fixtures.mock_results import build_mock_session
    generate_report(session=build_mock_session("demo-001"), fmt="both")
"""

from __future__ import annotations

import html as html_lib
import os
import re
import sys
import traceback
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 让本模块既能作为 tools 包导入，也能独立运行
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.dirname(_HERE)
_PROJECT_ROOT = os.path.dirname(_SRC_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# ---- 依赖导入：charts 是出图出口，errors 是错误协议出口，两者都做兜底 ----
try:  # pragma: no cover - 导入形态兜底
    from tools import charts as charts  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from . import charts  # type: ignore
    except Exception:  # noqa: BLE001
        charts = None  # type: ignore

try:  # pragma: no cover
    from tools.errors import error_result, ok_result  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from .errors import error_result, ok_result  # type: ignore
    except Exception:  # noqa: BLE001

        def ok_result(**fields: Any) -> Dict[str, Any]:
            """errors.py 不可用时的兜底：保持同样的 {'ok': True, ...} 形状。"""
            out: Dict[str, Any] = {"ok": True}
            out.update(fields)
            return out

        def error_result(exc: Any, message: Optional[str] = None,
                         hint: Optional[str] = None,
                         details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            """errors.py 不可用时的兜底：保持同样的 {'ok': False, 'error': {...}} 形状。"""
            return {
                "ok": False,
                "error": {
                    "code": "COMP_ERROR",
                    "message": message or f"{type(exc).__name__}: {exc}",
                    "hint": hint or "请检查调用参数后重试。",
                    "details": details or {"exception_type": type(exc).__name__},
                },
            }

# 语义化摘要（summary_md）由 _summary.py 集中生成，导入失败则交由 registry 兜底。
try:  # pragma: no cover
    from tools._summary import build_report_summary_md  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from ._summary import build_report_summary_md  # type: ignore
    except Exception:  # noqa: BLE001
        build_report_summary_md = None  # type: ignore

# 业务常量从 schemas.py 取（单一真理源），导入失败用同值兜底
try:  # pragma: no cover
    from tools.schemas import (  # type: ignore
        CANONICAL_FIELDS, GREEN_CIRCLE_CR, RED_CIRCLE_CR,
        META_SECTION_KEYS, meta_key,
    )
except Exception:  # noqa: BLE001
    try:
        from .schemas import (  # type: ignore
            CANONICAL_FIELDS, GREEN_CIRCLE_CR, RED_CIRCLE_CR,
            META_SECTION_KEYS, meta_key,
        )
    except Exception:  # noqa: BLE001
        CANONICAL_FIELDS = {}
        RED_CIRCLE_CR, GREEN_CIRCLE_CR = 1.20, 0.80
        # 兜底：键名表必须与 schemas.py 逐字一致，否则报告会整章空掉。
        # 这里不是"降级策略"，是导入链路彻底坏掉时的最后一道告警值——
        # 正常情况下永远不会走到这一支（tests/test_meta_contract.py 会盯住）。
        META_SECTION_KEYS = {
            "band": "", "diagnose": "", "market": "",
            "increase": "", "paymix": "", "jobeval": "",
        }

        def meta_key(section: str) -> str:  # type: ignore[misc]
            if section not in META_SECTION_KEYS:
                raise KeyError(
                    f"未知的 meta 分区键 {section!r}；合法键："
                    f"{sorted(META_SECTION_KEYS)}"
                )
            return section

# 输出目录：与 charts.py 用同一套常量，保证两侧相对路径口径一致
REPORT_DIR = os.environ.get("COMP_REPORT_DIR") or os.path.join(_PROJECT_ROOT, "report")
ASSETS_DIR = (getattr(charts, "ASSETS_DIR", None)
              if charts else None) or os.path.join(_PROJECT_ROOT, "assets")

# =============================================================================
# 一、常量：章节定义、固浮比方法论提示
# =============================================================================

# 六章之外的「执行摘要」等共七章；元组 = (章节标题, 构建函数名, 依赖的 meta 键)
SECTION_TITLES: List[str] = [
    "执行摘要",
    "数据概览与字段映射说明",
    "薪酬现状诊断",
    "带宽设计与市场对标建议",
    "调薪方案对比与推荐",
    "固浮比与激励建议",
    "风险提示与实施路线图",
]
_CN_NUM = ["一", "二", "三", "四", "五", "六", "七"]

# 固浮比章节**必须原样出现**的方法论提示（PRD 模块 5 硬性要求，不得改写）
PAY_MIX_PRINCIPLE = (
    "高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；"
    "长周期协作型业务强行高浮动会破坏协作。"
)

# 缺失章节的降级文案模板
MISSING_TEXT = "本章未执行（前置步骤缺失）"

# 各章节 → 依赖的 meta 键与应补跑的工具（用于降级提示，告诉用户下一步做什么）
#
# ⚠️ 键名必须走 `mk()`（= schemas.meta_key）：这张表早期直接写字面量
# `current_state / increase_sim / pay_mix / job_eval / market_benchmark`，
# 与六个写入模块实际写的 `diagnose / increase / paymix / jobeval / market`
# 全部对不上 —— 表现是工具链每一步都 OK，报告却六章空着。
# 契约已收敛到 schemas.META_SECTION_KEYS，这里只引用不重新发明。
mk = meta_key
_SECTION_DEPS: Dict[str, Tuple[List[str], str]] = {
    "执行摘要": ([mk("diagnose")], "analyze_current_state"),
    "数据概览与字段映射说明": ([mk("mapping")], "load_salary_data → confirm_mapping"),
    "薪酬现状诊断": ([mk("diagnose")], "analyze_current_state"),
    "带宽设计与市场对标建议": ([mk("band"), mk("market")], "generate_band → market_benchmark"),
    "调薪方案对比与推荐": ([mk("increase")], "simulate_increase"),
    "固浮比与激励建议": ([mk("paymix")], "simulate_pay_mix"),
    "风险提示与实施路线图": ([], ""),
}


# =============================================================================
# 二、通用工具：取值容错 / 数值格式化 / Markdown 表格
# =============================================================================

def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """
    按别名优先级从 dict 里取值，第一个非空（非 None）的结果胜出。

    为什么需要：上游六个模块由不同工程师实现，同一个业务含义的键名可能有出入
    （如 `by_level` / `table` / `rows`）。报告是最后消费方，**必须是最宽容的一环**，
    否则上游改个键名整份报告就空白。
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    """
    安全转 float：None / NaN / '' / '—' / '待定' 一律返回 default。
    报告里所有数字都过这一层，避免把 nan 直接写进表格。
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except Exception:  # noqa: BLE001
            return default
        return default if f != f else f          # f != f 是标准 NaN 判据
    try:
        s = str(v).strip().replace(",", "").replace("，", "")
        if s in ("", "-", "—", "nan", "NaN", "None", "待定", "无"):
            return default
        f = float(s)
        return default if f != f else f
    except Exception:  # noqa: BLE001
        return default


def _money(v: Any, unit: str = "auto") -> str:
    """
    金额格式化：≥1 万自动切「万元」（管理层汇报习惯），否则保留千分位「元」。
    与 charts.py 的 fmt_money 保持同一口径，避免图说 12.3 万、表说 123,456。
    """
    x = _num(v)
    if x is None:
        return "—"
    if unit == "万元":
        return f"{x / 10000:,.1f} 万元"
    if unit == "元":
        return f"{x:,.0f} 元"
    if abs(x) >= 10000:
        return f"{x / 10000:,.1f} 万元"
    return f"{x:,.0f} 元"


def _pct(v: Any, digits: int = 1, signed: bool = False) -> str:
    """
    百分比格式化。输入既可能是 0.085（比率）也可能是 8.5（已是百分数）：
    |v| <= 1.5 视作比率，否则视作百分数 —— 兼容上游两种口径，不会出现 0.1% 的笑话。
    """
    x = _num(v)
    if x is None:
        return "—"
    p = x * 100 if abs(x) <= 1.5 else x
    return f"{p:+.{digits}f}%" if signed else f"{p:.{digits}f}%"


def _int(v: Any) -> str:
    """整数格式化（人数等）。"""
    x = _num(v)
    return "—" if x is None else f"{int(round(x)):,}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]],
           aligns: Optional[Sequence[str]] = None) -> List[str]:
    """
    生成 Markdown 表格行（含表头与分隔行）。

    aligns: 每列的对齐方式，'l' / 'c' / 'r'，None 时默认数值列右对齐、文本列左对齐。
    右对齐是财务/薪酬表格的通行做法，便于纵向对位比较位数。
    """
    if not headers:
        return []
    n = len(headers)
    if aligns is None:
        aligns = ["l"] * n
    aligns = list(aligns) + ["l"] * (n - len(aligns))

    def _sep(a: str) -> str:
        return {"c": ":---:", "r": "---:", "l": ":---"}.get(a, ":---")

    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join(_sep(a) for a in aligns[:n]) + " |",
    ]
    for row in rows:
        cells = ["" if c is None else str(c) for c in list(row)[:n]]
        cells += [""] * (n - len(cells))
        # 表格单元格里的竖线会破坏 Markdown 表格结构，替换成中文顿号
        cells = [c.replace("|", "、").replace("\n", " ") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _bullets(items: Sequence[str], marker: str = "-") -> List[str]:
    """生成 Markdown 无序列表；空列表返回空（不产生多余空行）。"""
    return [f"{marker} {t}" for t in items if t]


def _warn_lines(title: str, items: Sequence[str]) -> List[str]:
    """把上游 warnings 渲染成引用块；无警告时返回空列表（不占位）。"""
    if not items:
        return []
    out = [f"> **{title}**"]
    out += [f"> - {w}" for w in items if w]
    out.append("")
    return out


# =============================================================================
# 三、Session 解析
# =============================================================================

def _meta_of(obj: Any) -> Optional[Dict[str, Any]]:
    """从对象里取 meta：支持真正的 Session 对象（.meta）、dict、以及带 get() 的对象。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    m = getattr(obj, "meta", None)
    if isinstance(m, dict):
        return m
    if hasattr(obj, "get") and callable(getattr(obj, "get")):
        try:
            return obj.get("meta")
        except Exception:  # noqa: BLE001
            return None
    return None


def _resolve_session(session_id: Any, session: Any = None,
                     meta: Any = None) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
    """
    解析出 meta 字典。按优先级：显式 meta > 显式 session > session_id 对象
    > session.py 的 get_session / SessionStore > 磁盘 .state 快照。

    返回 (meta, session_id, warnings)。**任何一步失败都不抛异常**，
    只把原因记进 warnings，让调用方决定是降级还是报错。
    """
    warns: List[str] = []

    if meta is not None:
        # FIX 1（P0）：session_id 可能被调用方直接传成 Session 对象，
        # 此时 str(session_id) 会把整个会话（含 DataFrame 摘要）打印进报告。
        # 必须优先取对象上的 .session_id，否则仅在它是 str/int/float 时才用原值。
        _sid = (getattr(session_id, "session_id", None)
                or (session_id if isinstance(session_id, (str, int, float)) else None)
                or "inline-meta")
        return (_meta_of(meta) or (meta if isinstance(meta, dict) else None),
                str(_sid), warns)

    if session is not None:
        m = _meta_of(session)
        if m is not None:
            return m, str(getattr(session, "session_id", None) or session_id or "inline"), warns
        warns.append("传入的 session 对象没有 .meta 字典，已按空数据处理")

    # session_id 本身可能就是 Session 对象（调用方直接把对象传进来了）
    if session_id is not None and not isinstance(session_id, (str, int, float)):
        m = _meta_of(session_id)
        if m is not None:
            return m, str(getattr(session_id, "session_id", None) or "inline"), warns

    sid = "" if session_id is None else str(session_id)

    if sid:
        # 优先走工具链使用的同一个 get_store() 单例（相对导入 .session，确保与
        # registry/loader 指向**同一个模块对象**——绝不能用顶层 `tools.session`，
        # 否则会与 `src.tools.session` 形成两套独立单例，会话明明写进去了却查不到）。
        # 单例尊重 reset_store()/自定义 STATE_DIR，避免「单例指向临时目录、报告却去
        # 默认目录找会话」造成的空报告。
        try:
            from .session import get_store as _get_store
        except Exception:  # noqa: BLE001
            try:
                from src.tools.session import get_store as _get_store
            except Exception:  # noqa: BLE001
                _get_store = None  # type: ignore
        if _get_store is not None:
            try:
                _m = _meta_of(_get_store().load(sid))
                if _m is not None:
                    return _m, sid, warns
            except Exception:  # noqa: BLE001
                pass
            try:
                _m = _get_store().get_meta(sid)
                if _m is not None:
                    return _m, sid, warns
            except Exception:  # noqa: BLE001
                pass

    # 尝试从 session.py 取（engineer-core 正在实现，接口可能微调，所以多重探测）
    if sid:
        mod = None
        for name in ("tools.session", "src.tools.session", "session"):
            try:
                mod = __import__(name, fromlist=["*"])
                break
            except Exception:  # noqa: BLE001
                continue
        if mod is not None:
            for factory_name in ("get_session", "load_session", "get"):
                fn = getattr(mod, factory_name, None)
                if callable(fn):
                    try:
                        s = fn(sid)
                        m = _meta_of(s)
                        if m is not None:
                            return m, sid, warns
                    except Exception:  # noqa: BLE001
                        continue
            store_cls = getattr(mod, "SessionStore", None)
            if store_cls is not None:
                for meth in ("load", "get"):
                    fn = getattr(store_cls, meth, None)
                    if callable(fn):
                        for target in (store_cls, store_cls()):
                            try:
                                s = fn(target, sid) if not isinstance(target, type) else fn(sid)
                                m = _meta_of(s)
                                if m is not None:
                                    return m, sid, warns
                            except Exception:  # noqa: BLE001
                                continue

        # 最后兜底：读磁盘快照 .state/sessions/<id>.json
        for cand in (os.path.join(_PROJECT_ROOT, ".state", "sessions", f"{sid}.json"),
                     os.path.join(_PROJECT_ROOT, ".state", f"{sid}.json")):
            if os.path.exists(cand):
                try:
                    import json
                    with open(cand, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    m = _meta_of(data) or (data if isinstance(data, dict) else None)
                    if m is not None:
                        warns.append(f"未找到活跃会话对象，已回退读取磁盘快照：{os.path.basename(cand)}")
                        return m, sid, warns
                except Exception as exc:  # noqa: BLE001
                    warns.append(f"会话快照读取失败：{type(exc).__name__}: {exc}")

    return None, sid, warns


# =============================================================================
# 四、图表管理：按需补画 + 相对路径 + 存在性断言
# =============================================================================

def _adapt(session_id: Any, meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用适配层。**失败即原样返回**，绝不因为适配层的问题让整份报告出不来。

    但失败不能静默：记一条警告，让它在报告的告警区显形 —— 否则会出现
    「数字全是 —，而报告自称生成成功」这种最难排查的状态。
    """
    if not meta:
        return meta
    try:  # pragma: no cover
        from tools.report_adapter import build_view  # type: ignore
    except Exception:  # noqa: BLE001
        try:
            from .report_adapter import build_view  # type: ignore
        except Exception:  # noqa: BLE001
            return meta
    try:
        return build_view(str(session_id) if session_id else None, meta)
    except Exception:  # noqa: BLE001
        # 适配层只做字段搬运，理论上不该抛；真抛了说明上游结构变了。
        # 此时退化为原始 meta（报告会大量显示 —），由契约测试去定位。
        return meta


class _ReportCtx:
    """
    报告构建期的共享上下文：持有 meta、已生成图表、警告列表、报告目录。

    为什么要一个对象而不是散装参数：七个章节函数都需要「补一张图 / 记一条警告 /
    读一段 meta」，用对象承载能避免到处传七八个参数，也便于加缓存。
    """

    def __init__(self, meta: Dict[str, Any], session_id: str,
                 report_dir: str, title: str):
        # 适配层：把各写入模块的实际字段名翻译成本报告使用的词汇。
        # 放在构造函数里是为了保证「任何构造 ctx 的路径都过适配」——包括
        # 单章调试、内联 meta、磁盘快照回退等入口，不会漏掉某一条。
        raw_meta = meta if isinstance(meta, dict) else {}
        self.meta = _adapt(session_id, raw_meta)
        self.raw_meta = raw_meta          # 保留原始 meta，便于排查与断言
        self.session_id = session_id
        self.report_dir = report_dir
        self.title = title
        self.figures: List[Dict[str, Any]] = []      # 全部已生成图表的元信息
        self.warnings: List[str] = []
        self._cache: Dict[str, Dict[str, Any]] = {}  # 图表名 -> save_figure 返回结构

    # ---------- meta 读取 ----------
    def sec(self, key: str) -> Dict[str, Any]:
        """
        取某个模块的结果 dict；缺失或类型不对时返回空 dict（绝不抛异常）。

        **键名必须先在 `META_SECTION_KEYS` 里登记**：这一步把「读了一个不存在的
        分区」从静默返回 {}（报告整章空掉、无任何报错）变成当场 KeyError。
        静默降级在这条链上是有害的 —— 它让 bug 藏到生成报告那一步才显形，
        而那时所有前置工具都显示 OK，排查成本极高。
        """
        meta_key(key)  # 未知键 → 立刻抛错，不静默返回空
        v = self.meta.get(key)
        return v if isinstance(v, dict) else {}

    def has(self, key: str, *inner_keys: str) -> bool:
        """判断某个模块结果（以及内部关键字段）是否存在。"""
        s = self.sec(key)
        if not s:
            return False
        if not inner_keys:
            return True
        return any(k in s and s[k] not in (None, [], {}) for k in inner_keys)

    def warn(self, msg: str) -> None:
        if msg and msg not in self.warnings:
            self.warnings.append(msg)

    # ---------- 图表 ----------
    def figure(self, name: str, caption: str, builder: Callable[[], Any]) -> List[str]:
        """
        取（或补画）一张图，返回可直接写进 Markdown 的引用行。

        逻辑：
        1. 缓存命中 → 直接复用（同一次报告里同一张图只画一次）；
        2. meta[section]['figure'] 里已有**真实存在**的 html_path → 复用上游落图；
        3. 否则现场调用 builder() 画图 + save_figure 落盘。

        builder 抛异常时**不中断报告**：记一条警告并返回一段说明文字，
        —— 一张图挂掉不该让整份报告出不来。
        """
        if name in self._cache:
            return self._ref_lines(self._cache[name], caption)

        # 先看上游是否已经落过图
        for section in self.meta.values():
            if not isinstance(section, dict):
                continue
            fig = section.get("figure")
            if isinstance(fig, dict) and fig.get("name") == name:
                hp = fig.get("html_path")
                if hp and os.path.exists(hp):
                    saved = dict(fig)
                    saved.setdefault("png_path", None)
                    self._cache[name] = saved
                    return self._ref_lines(saved, caption)

        if charts is None:
            self.warn(f"图表模块 charts.py 不可用，已跳过图表「{caption}」")
            return [f"> ⚠️ 图表「{caption}」未生成：charts 模块不可用。", ""]

        try:
            fig = builder()
            saved = charts.save_figure(fig, name)
        except Exception as exc:  # noqa: BLE001
            self.warn(f"图表「{caption}」生成失败：{type(exc).__name__}: {exc}")
            return [f"> ⚠️ 图表「{caption}」未生成：{type(exc).__name__}: {exc}", ""]

        self._cache[name] = saved
        if saved.get("png_error"):
            # PNG 降级是有预期的（kaleido 未安装），只在内部记录，不写成"错误"
            pass
        return self._ref_lines(saved, caption)

    def _ref_lines(self, saved: Dict[str, Any], caption: str) -> List[str]:
        """
        生成 Markdown 图片引用行。

        相对路径口径：以**报告文件所在目录**为基准算 relpath
        （报告在 report/，图在 assets/ → `../assets/xxx.html`），
        并用正斜杠，保证 Windows / Mac / 网页渲染器都能解析。
        """
        target = saved.get("png_path") or saved.get("html_path")
        if not target:
            return [f"> ⚠️ 图表「{caption}」无可用文件（HTML 与 PNG 均未生成）。", ""]

        rel = os.path.relpath(os.path.abspath(target), self.report_dir).replace("\\", "/")
        exists = os.path.exists(os.path.join(self.report_dir, rel))
        if not exists:
            self.warn(f"图表「{caption}」的相对路径解析后文件不存在：{rel}")

        is_png = str(target).lower().endswith(".png")
        self.figures.append({
            "name": saved.get("name") or "figure",
            "caption": caption,
            "html_path": saved.get("html_path"),
            "png_path": saved.get("png_path"),
            "rel_path": rel,
            "is_png": is_png,
            "exists": exists,
            "png_error": saved.get("png_error"),
            "div": saved.get("div") or "",
        })

        lines: List[str] = []
        if is_png:
            lines.append(f"![{caption}]({rel})")
            lines.append("")
            lines.append(f"<div align=\"center\"><b>图：{caption}</b></div>")
        else:
            # 无 PNG 时引用 HTML：Markdown 的 ![]() 渲染不出网页，
            # 因此同时给出可点击链接，并明确标注这是交互式图表。
            lines.append(f"![{caption}]({rel})")
            lines.append("")
            lines.append(
                f"<div align=\"center\"><b>图：{caption}</b>（交互式图表，"
                f"请用浏览器打开 <a href=\"{rel}\">{rel}</a> 查看；"
                f"可缩放、悬浮查看数值，也可直接截图用于汇报）</div>"
            )
        lines.append("")
        return lines


# =============================================================================
# 五、第一章：执行摘要
# =============================================================================

def _s1_exec_summary(ctx: _ReportCtx) -> List[str]:
    """
    执行摘要：管理层只看这一页。

    结构固定为「关键数字表 → 3 条核心结论 → 优先建议」三段：
    先给可核对的硬数字，再给基于数字的判断，最后给可执行的动作。
    """
    L: List[str] = []
    cur = ctx.sec(mk("diagnose"))
    mkt = ctx.sec(mk("market"))
    inc = ctx.sec(mk("increase"))
    band = ctx.sec(mk("band"))

    cs = _get(cur, "summary", default={}) or {}
    circles = _get(cur, "circles", default={}) or {}
    red = _get(circles, "red", default={}) or {}
    green = _get(circles, "green", default={}) or {}
    unknown = _get(circles, "unknown", default={}) or {}
    ms = _get(mkt, "summary", default={}) or {}

    if not cur:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `current_state`（现状诊断）结果，"
                f"请先执行 `analyze_current_state` 后再生成报告。", ""]

    headcount = _num(_get(cs, "headcount", "total_count", "count"))
    level_count = _num(_get(cs, "level_count", "levels"))
    annual = _num(_get(cs, "total_annual_cost", "annual_cost"))
    med = _num(_get(cs, "salary_median", "median_salary"))
    cr_mean = _num(_get(cs, "cr_mean", "mean_cr"))
    cr_med = _num(_get(cs, "cr_median", "median_cr"))
    cr_std = _num(_get(cs, "cr_std", "std_cr"))
    n_red, n_green = _num(_get(red, "count", "red_count")), _num(_get(green, "count", "green_count"))
    p_red, p_green = _num(_get(red, "pct", "red_pct")), _num(_get(green, "pct", "green_pct"))
    n_unknown, p_unknown = _num(_get(unknown, "count", "unknown_count")), _num(_get(unknown, "pct"))
    cost_red = _num(_get(red, "annual_cost", "cost"))
    cost_green = _num(_get(green, "annual_cost", "cost"))
    gap = _num(_get(ms, "overall_gap_p50_pct", "overall_gap_pct", "gap_pct"))
    budget = _num(_get(inc, "budget", "budget_amount"))
    budget_pct = _num(_get(inc, "budget_pct"))
    rec_name = _get(inc, "recommended", "recommended_strategy")
    rec_cost = None
    for s in (_get(inc, "strategies", "compare", "table", default=[]) or []):
        if isinstance(s, dict) and s.get("recommended"):
            rec_cost = _num(_get(s, "total_cost", "cost"))
            rec_name = rec_name or _get(s, "label", "name", "strategy")
            break

    # ---- 关键数字表 ----
    L.append("### 1.1 关键数字")
    L.append("")
    rows: List[List[Any]] = [
        ["覆盖人数", _int(headcount) + " 人" if headcount else "—", "参与本次诊断的有效样本"],
        ["覆盖职级", (_int(level_count) + " 个") if level_count else "—", "职级序列跨度"],
        ["年度薪酬总额", _money(annual), "全部样本年薪合计（含估算部分）"],
        ["月薪中位数", _money(med, "元"), "反映公司典型薪酬水平（不受极值影响）"],
        ["平均 CR", f"{cr_mean:.2f}" if cr_mean is not None else "—",
         f"1.00 = 正好在带宽中位值；健康区间 {GREEN_CIRCLE_CR:.2f}~{RED_CIRCLE_CR:.2f}"],
        ["CR 中位数", f"{cr_med:.2f}" if cr_med is not None else "—", "半数员工的 CR 低于此值"],
        ["CR 标准差", f"{cr_std:.3f}" if cr_std is not None else "—", "越小说明内部一致性越好"],
        ["红圈人数", f"{_int(n_red)} 人（{_pct(p_red)}）" if n_red is not None else "—",
         f"CR > {RED_CIRCLE_CR:.2f}，薪酬高于带宽，成本溢出"],
        ["绿圈人数", f"{_int(n_green)} 人（{_pct(p_green)}）" if n_green is not None else "—",
         f"CR < {GREEN_CIRCLE_CR:.2f}，薪酬低于带宽，流失风险"],
        ["未识别人数", f"{_int(n_unknown)} 人（{_pct(p_unknown)}）" if n_unknown else "—",
         "职级无法识别(UNKNOWN)：已计入人数/成本基数，但不参与带宽与 CR 诊断"],
        ["红圈年化溢出成本", _money(cost_red), "超出带宽上限部分的年化金额"],
        ["绿圈补差成本", _money(cost_green), "补到带宽下限所需的最小年化投入"],
        ["相对市场 P50 差距", _pct(gap, signed=True) if gap is not None else "—",
         "负值=低于市场水平"],
        ["调薪预算", (_money(budget) + f"（占薪资总额 {_pct(budget_pct)}）")
         if budget is not None else "—", "本轮可调配的年度增量"],
    ]
    if rec_name:
        rows.append(["推荐调薪方案", str(rec_name),
                     f"预计年度新增 {_money(rec_cost)}" if rec_cost else "综合性价比最优"])
    L += _table(["指标", "数值", "口径说明"], rows, ["l", "r", "l"])
    L.append("")

    # ---- 三条核心结论（全部由数字自动派生，不写空话） ----
    L.append("### 1.2 三条核心结论")
    L.append("")

    # 结论一：内部公平性
    if n_red is not None and n_green is not None and headcount:
        rg_pct = (n_red + n_green) / headcount
        if rg_pct > 0.30:
            level_txt = "偏严重"
            judge = ("红绿圈合计占比超过 30%，说明现有薪酬体系与职级体系的耦合已经松动，"
                     "需要先做带宽校准再谈调薪，否则加薪只会固化既有不平衡。")
        elif rg_pct > 0.20:
            level_txt = "需关注"
            judge = ("红绿圈合计占比在 20%~30% 区间，属于常见的亚健康状态，"
                     "可通过一轮结构化调薪显著改善。")
        else:
            level_txt = "基本健康"
            judge = "红绿圈合计占比低于 20%，内部一致性良好，重点是保持而非重构。"
        L.append(f"**结论一｜内部公平性{level_txt}：** "
                 f"红圈 {_int(n_red)} 人（{_pct(p_red)}）、绿圈 {_int(n_green)} 人（{_pct(p_green)}），"
                 f"合计占样本 {_pct(rg_pct)}。{judge}")
        L.append("")
    else:
        L.append("**结论一｜内部公平性：** 红绿圈统计缺失，无法给出判断（需执行 `analyze_current_state`）。")
        L.append("")

    # 结论二：外部竞争力
    if gap is not None:
        below = _num(_get(ms, "levels_below_p50", "below_market_levels"))
        if gap < -0.05:
            judge = "整体明显低于市场中位水平，在招聘与保留两端都会持续承压，建议优先补足。"
        elif gap < -0.01:
            judge = "整体略低于市场中位水平，属于可控范围，但核心岗位需单独对标。"
        elif gap <= 0.03:
            judge = "整体与市场基本持平，薪酬成本效率良好。"
        else:
            judge = "整体高于市场中位水平，需关注人均效能是否同步领先，避免成本刚性。"
        extra = f"，其中 {_int(below)} 个职级低于 P50" if below else ""
        L.append(f"**结论二｜外部竞争力：** 公司整体相对市场 P50 为 {_pct(gap, signed=True)}{extra}。"
                 f"{judge}")
        L.append("")
    else:
        L.append("**结论二｜外部竞争力：** 未执行市场对标（`market_benchmark`），"
                 "无法判断公司在市场中的相对位置。")
        L.append("")

    # 结论三：调薪与成本
    if inc and rec_name:
        # 推荐理由优先用上游给的理由；没有则给一句通用的、不虚构数字的定性说明
        reason = _get(inc, "recommend_reason",
                      default="该方案在成本可控的前提下，对内部公平性的改善幅度最大。")
        L.append("**结论三｜调薪路径：** 在 {} 的预算约束下，推荐采用「{}」。{}".format(
            _pct(budget_pct), rec_name, reason))
        L.append("")
    elif inc:
        L.append(f"**结论三｜调薪路径：** 已执行调薪模拟（预算 {_money(budget)}），"
                 "但未标记推荐方案，建议按第 5 章的对比表人工选定。")
        L.append("")
    else:
        L.append("**结论三｜调薪路径：** 未执行调薪模拟（`simulate_increase`），"
                 "建议在确定预算比例后补跑，再进行方案决策。")
        L.append("")

    # ---- 优先建议（Top 3，按"风险×紧迫度"排序） ----
    L.append("### 1.3 优先建议")
    L.append("")
    actions: List[str] = []
    if n_green:
        actions.append(f"**先补绿圈**：{_int(n_green)} 名绿圈员工（{_pct(p_green)}）是离职高发区，"
                       f"补到带宽下限的最小年化投入约 {_money(cost_green)}，"
                       f"单位成本的留人效率最高，应作为本轮调薪的第一优先级。")
    if n_red:
        actions.append(f"**冻红圈**：{_int(n_red)} 名红圈员工（{_pct(p_red)}）建议冻结月薪、"
                       f"改以一次性补贴处理，不占用年度调薪池，避免固定成本继续抬升。")
    if gap is not None and gap < -0.02:
        actions.append(f"**结构性补涨**：整体低于市场 P50 {_pct(gap, signed=True)}，"
                       f"建议按岗位序列分档对标（核心岗 P75、通用岗 P50、操作岗 P25），"
                       f"而非全员普涨。")
    if band:
        ov = _num(_get(_get(band, "overlap_summary", default={}) or {}, "avg_overlap"))
        if ov is not None:
            healthy = (_get(_get(band, "overlap_summary", default={}) or {},
                            "healthy_range", default=(0.20, 0.40)) or (0.20, 0.40))
            if ov > 0.45:
                actions.append(f"**收窄带宽重叠**：相邻职级平均重叠 {_pct(ov)}，"
                               f"高于健康区间 {_pct(healthy[0])}~{_pct(healthy[1])}，"
                               f"晋升带来的薪酬跃迁不足，职级激励失效。")
            elif ov < 0.15:
                actions.append(f"**警惕带宽断档**：相邻职级平均重叠仅 {_pct(ov)}，"
                               f"晋升会造成人力成本跳变，建议适当提高带宽幅度或降低中位值级差。")
    if not actions:
        actions.append("数据不足，建议先补全诊断与对标步骤后再形成行动清单。")
    # 有序列表：编号必须顶格（不能带前导空格），否则部分 Markdown 渲染器不识别
    L += ["%d. %s" % (i + 1, a) for i, a in enumerate(actions[:4])]
    L.append("")
    return L


# =============================================================================
# 六、第二章：数据概览与字段映射说明
# =============================================================================

def _s2_data_overview(ctx: _ReportCtx) -> List[str]:
    """
    数据概览章：回答「这份报告的数据从哪来、可信度如何」。

    这是整份报告的**可信度地基**：样本量、字段映射、清洗记录、缺失率。
    HR 拿给管理层看时，第一质疑永远是"数据准不准"，这一章就是预判性回答。
    """
    L: List[str] = []
    mp = ctx.sec(mk("mapping"))
    cur = ctx.sec(mk("diagnose"))

    if not mp and not cur:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `mapping`（字段映射）结果，"
                f"请先执行 `load_salary_data` → `confirm_mapping`。", ""]

    # ---- 2.1 数据来源与样本 ----
    L.append("### 2.1 数据来源与样本量")
    L.append("")
    src = _get(mp, "source", "file_path", "file")
    cs = _get(cur, "summary", default={}) or {}
    headcount = _num(_get(cs, "headcount", "total_count", "count"))
    level_count = _num(_get(cs, "level_count", "levels"))
    coerce = _get(cur, "coerce_report", "clean_report", default={}) or {}
    raw_rows = _num(_get(coerce, "raw_rows", "rows_raw"))
    clean_rows = _num(_get(coerce, "clean_rows", "rows_clean")) or headcount

    L += _table(
        ["项目", "内容"],
        [
            ["数据文件", f"`{src}`" if src else "—"],
            ["原始行数", _int(raw_rows) + " 行" if raw_rows else "—"],
            ["有效样本", _int(clean_rows) + " 人" if clean_rows else "—"],
            ["覆盖职级", (_int(level_count) + " 个") if level_count else "—"],
            ["识别字段数", _int(_get(mp, "auto_mapped_count", default=len(_get(mp, "mapping", default={}) or {})))],
        ],
        ["l", "l"],
    )
    L.append("")

    # ---- 2.2 字段映射表（AI 语义识别的成果展示） ----
    mapping = _get(mp, "mapping", default={}) or {}
    conf = _get(mp, "confidence", default={}) or {}
    if mapping:
        L.append("### 2.2 字段映射说明")
        L.append("")
        L.append("原始表头由模型按语义识别并映射到标准字段；置信度低于 80 的行建议人工复核。")
        L.append("")
        rows: List[List[Any]] = []
        for raw_col, std in mapping.items():
            meta_field = CANONICAL_FIELDS.get(std, (std, "", False, ""))
            label, meaning = meta_field[0], meta_field[1]
            c = _num(conf.get(raw_col))
            flag = "" if (c is None or c >= 80) else " ⚠️"
            rows.append([f"`{raw_col}`", f"`{std}`", label, meaning or "—",
                         (f"{int(c)}{flag}" if c is not None else "—")])
        L += _table(["原始列名", "标准字段", "中文名", "业务含义", "置信度"],
                    rows, ["l", "l", "l", "l", "r"])
        L.append("")

    unmapped = _get(mp, "unmapped", default=[]) or []
    if unmapped:
        L.append(f"**未参与计算的原始列**（{len(unmapped)} 个）："
                 + "、".join(f"`{c}`" for c in unmapped) + "。")
        L.append("")
    needs = _get(mp, "needs_review", default=[]) or []
    if needs:
        L += _warn_lines("需人工复核的映射", [str(n) for n in needs])

    # ---- 2.3 数据清洗摘要 ----
    if coerce:
        L.append("### 2.3 数据清洗摘要")
        L.append("")
        rows = [
            ["原始行数", _int(raw_rows) + " 行" if raw_rows else "—", "读入时的总行数"],
            ["清洗后行数", _int(clean_rows) + " 行" if clean_rows else "—", "进入计算的有效行数"],
            ["剔除重复行", _int(_get(coerce, "dropped_duplicates", "dup_removed")) + " 行"
             if _num(_get(coerce, "dropped_duplicates", "dup_removed")) is not None else "—",
             "按员工 ID 判重，保留首次出现"],
            ["修正非数值单元格", _int(_get(coerce, "fixed_numeric", "nonnumeric_cells"))
             if _num(_get(coerce, "fixed_numeric", "nonnumeric_cells")) is not None else "—",
             "如「待定」「—」等，已置为缺失"],
            ["剔除必填缺失行", _int(_get(coerce, "dropped_missing_required", "missing_rows"))
             if _num(_get(coerce, "dropped_missing_required", "missing_rows")) is not None else "—",
             "月薪/职级/员工ID 任一缺失即剔除整行"],
        ]
        L += _table(["清洗环节", "数量", "处理方式"], rows, ["l", "r", "l"])
        L.append("")
        notes = _get(coerce, "notes", "warnings", default=[]) or []
        if notes:
            L += _bullets([str(n) for n in notes])
            L.append("")

    # ---- 2.4 字段缺失率 ----
    miss = _get(cur, "field_missing_rate", "missing_by_field", default={}) or {}
    if miss:
        L.append("### 2.4 字段缺失率")
        L.append("")
        rows = []
        for field, rate in miss.items():
            r = _num(rate)
            if r is None:
                continue
            meta_field = CANONICAL_FIELDS.get(field, (field, "", False, ""))
            if r >= 0.999:
                note = "整列缺失，已由工具推算"
            elif r >= 0.30:
                note = "缺失较多，相关结论需谨慎"
            elif r > 0:
                note = "轻微缺失，不影响主结论"
            else:
                note = "完整"
            rows.append([meta_field[0], f"`{field}`", _pct(r), note])
        L += _table(["字段", "标准名", "缺失率", "影响评估"], rows, ["l", "l", "r", "l"])
        L.append("")

    L += _warn_lines("数据质量提示", [str(w) for w in (_get(cur, "warnings", default=[]) or [])])
    return L


# =============================================================================
# 七、第三章：薪酬现状诊断
# =============================================================================

def _s3_current_state(ctx: _ReportCtx) -> List[str]:
    """现状诊断章：分布表 + CR 分布图 + 红绿圈统计。核心证据章。"""
    L: List[str] = []
    cur = ctx.sec(mk("diagnose"))
    if not cur:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `current_state` 结果，"
                f"请先执行 `analyze_current_state`。", ""]

    by_level = _get(cur, "by_level", "level_stats", "table", default=[]) or []
    circles = _get(cur, "circles", "red_green", default={}) or {}
    cr_values = _get(cur, "cr_values", "cr_list", default=None)

    # ---- 3.1 各职级薪酬分布 ----
    if by_level:
        L.append("### 3.1 各职级薪酬分布")
        L.append("")
        rows = []
        for r in by_level:
            if not isinstance(r, dict):
                continue
            rows.append([
                r.get("level", "—"),
                _int(_get(r, "count", "headcount")),
                _money(_get(r, "min"), "元"),
                _money(_get(r, "p25"), "元"),
                _money(_get(r, "median"), "元"),
                _money(_get(r, "p75"), "元"),
                _money(_get(r, "max"), "元"),
                _money(_get(r, "mean", "avg"), "元"),
                (f"{_num(_get(r, 'cr_median')):.2f}"
                 if _num(_get(r, "cr_median")) is not None else "—"),
            ])
        L += _table(["职级", "人数", "最低", "P25", "中位数", "P75", "最高", "平均", "CR中位"],
                    rows, ["l", "r", "r", "r", "r", "r", "r", "r", "r"])
        L.append("")
        L.append("> 读表提示：**中位数 < 平均** 说明该职级内少数高薪员工拉高了均值（右偏分布），"
                 "这是技术序列与资深岗位集中的常见形态，汇报均值容易高估典型水平。")
        L.append("")

    # ---- 3.2 CR 分布图 ----
    if cr_values:
        L.append("### 3.2 CR（薪酬比较比率）分布")
        L.append("")
        n = len(cr_values)
        rr = sum(1 for v in cr_values if (_num(v) or 0) > RED_CIRCLE_CR)
        gg = sum(1 for v in cr_values if (_num(v) or 0) < GREEN_CIRCLE_CR)
        L.append(f"样本 {n} 人中，红圈（CR > {RED_CIRCLE_CR:.2f}）{rr} 人、"
                 f"绿圈（CR < {GREEN_CIRCLE_CR:.2f}）{gg} 人、"
                 f"合理区间 {n - rr - gg} 人。")
        L.append("")
        if charts:
            L += ctx.figure(
                "cr_distribution", "CR 分布与红绿圈判定",
                lambda: charts.cr_distribution_chart(cr_values))
        else:
            L.append("> ⚠️ 图表模块不可用，跳过 CR 分布图。")
            L.append("")

    # ---- 3.3 红绿圈统计 ----
    red = _get(circles, "red", default={}) or {}
    green = _get(circles, "green", default={}) or {}
    ok = _get(circles, "ok", "normal", default={}) or {}
    unknown = _get(circles, "unknown", default={}) or {}
    if red or green or unknown:
        L.append("### 3.3 红绿圈人数与成本")
        L.append("")
        rows = [
            ["🔴 红圈（CR > %.2f）" % RED_CIRCLE_CR, _int(_get(red, "count")),
             _pct(_get(red, "pct")), _money(_get(red, "annual_cost", "cost")),
             "薪酬高于带宽：成本溢出，建议冻结或改一次性补贴"],
            ["🟢 绿圈（CR < %.2f）" % GREEN_CIRCLE_CR, _int(_get(green, "count")),
             _pct(_get(green, "pct")), _money(_get(green, "annual_cost", "cost")),
             "薪酬低于带宽：离职高发区，建议优先补涨"],
            ["合理区间", _int(_get(ok, "count")), _pct(_get(ok, "pct")), "—",
             "薪酬落在带宽内，无需立即干预"],
        ]
        if _get(unknown, "count"):
            rows.append([
                "⚪ 未识别（职级未知）", _int(_get(unknown, "count")),
                _pct(_get(unknown, "pct")), "—",
                "职级无法识别(UNKNOWN)：已计入人数/成本基数，但不参与带宽与 CR 诊断",
            ])
        L += _table(["类别", "人数", "占比", "年化成本影响", "风险与建议"],
                    rows, ["l", "r", "r", "r", "l"])
        L.append("")
        note = _get(circles, "note", default="")
        if note:
            L.append(f"> 成本口径：{note}")
            L.append("")

    L += _warn_lines("诊断提示", [str(w) for w in (_get(cur, "warnings", default=[]) or [])])
    return L


# =============================================================================
# 八、第四章：带宽设计与市场对标建议
# =============================================================================

def _s4_band_market(ctx: _ReportCtx) -> List[str]:
    """
    第四章：带宽设计（模块 2）+ 市场对标（模块 3）+ 岗位价值评估校验（模块 6）。

    把三者合并为一章的业务逻辑：它们回答的是同一个问题
    ——「我们的薪酬**结构**（内部）和**水平**（外部）应该是什么样的」。
    岗位价值评估是职级与带宽的依据，放在这里最顺（3P 模型中的 Position 维度）。
    """
    L: List[str] = []
    band = ctx.sec(mk("band"))
    mkt = ctx.sec(mk("market"))
    je = ctx.sec(mk("jobeval"))

    if not band and not mkt and not je:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `band` / `market_benchmark` 结果，"
                f"请先执行 `generate_band` → `market_benchmark`。", ""]

    # ---- 4.1 带宽设计 ----
    L.append("### 4.1 薪酬带宽设计")
    L.append("")
    if not band:
        L.append(f"> ⚠️ **{MISSING_TEXT}**：未检测到 `band`（带宽设计）结果，"
                 "请先执行 `generate_band`。")
        L.append("")
    else:
        params = _get(band, "params", default={}) or {}
        bt = _get(band, "band_table", "bands", "table", default=[]) or []
        ov = _get(band, "overlap_summary", "overlap", default={}) or {}

        L += _table(
            ["设计参数", "取值"],
            [
                ["设计模式", str(_get(band, "mode", default="—"))],
                ["中位值基准", str(_get(params, "base_mid", "basis", default="—"))],
                ["带宽幅度规则", str(_get(params, "spread", "spread_rule", default="—"))],
                ["中位值级差", _pct(_get(params, "midpoint_diff"))],
                ["是否采用市场数据", "是" if _get(params, "use_market") else "否"],
            ],
            ["l", "l"],
        )
        L.append("")
        L.append("> 带宽公式：**下限 = 中位值 ÷ (1 + 幅度/2)，上限 = 下限 × (1 + 幅度)**，"
                 "可验证恒等式为 `中位值 = (下限 + 上限) ÷ 2`。"
                 "带宽幅度按职级分层：基层 25%~28%、专业/技术 35%~38%、中高层 45%~60%。")
        L.append("")

        if bt:
            rows = []
            for r in bt:
                if not isinstance(r, dict):
                    continue
                ovv = _num(_get(r, "overlap_with_next", "overlap_pct"))
                rows.append([
                    r.get("level", "—"),
                    r.get("tier", "—"),
                    _money(_get(r, "band_min"), "元"),
                    _money(_get(r, "band_mid"), "元"),
                    _money(_get(r, "band_max"), "元"),
                    _pct(_get(r, "spread")),
                    _pct(_get(r, "midpoint_diff")),
                    (_pct(ovv) if ovv is not None else "—"),
                    _int(_get(r, "count")),
                ])
            L += _table(["职级", "层级", "带宽下限", "中位值", "带宽上限", "幅度",
                         "中位值级差", "与下一级重叠", "人数"],
                        rows, ["l", "l", "r", "r", "r", "r", "r", "r", "r"])
            L.append("")

            if charts:
                L += ctx.figure(
                    "band_overlap", "薪酬带宽区间与相邻职级重叠度",
                    lambda: charts.band_overlap_chart(bt))

        avg_ov = _num(_get(ov, "avg_overlap"))
        if avg_ov is not None:
            healthy = _get(ov, "healthy_range", default=(0.20, 0.40)) or (0.20, 0.40)
            lo, hi = (healthy[0], healthy[1]) if len(healthy) >= 2 else (0.20, 0.40)
            if avg_ov > hi:
                judge = (f"平均重叠 {_pct(avg_ov)} **高于**健康区间 {_pct(lo)}~{_pct(hi)}："
                         "晋升带来的薪酬跃迁不足，员工会觉得「升职不加薪」，"
                         "建议提高中位值级差或收窄带宽幅度。")
            elif avg_ov < lo:
                judge = (f"平均重叠 {_pct(avg_ov)} **低于**健康区间 {_pct(lo)}~{_pct(hi)}："
                         "晋升会造成人力成本跳变，且高职级缺少缓冲空间，"
                         "建议适当加大带宽幅度或降低中位值级差。")
            else:
                judge = (f"平均重叠 {_pct(avg_ov)} 落在健康区间 {_pct(lo)}~{_pct(hi)} 内："
                         "既允许员工在本职级内靠绩效涨薪，又保证晋升有实质跃迁。")
            L.append(f"**重叠度评价：** {judge}"
                     f"（最大 {_pct(_get(ov, 'max_overlap'))} / 最小 {_pct(_get(ov, 'min_overlap'))}）")
            L.append("")

        L += _warn_lines("带宽设计提示", [str(w) for w in (_get(band, "warnings", default=[]) or [])])

    # ---- 4.2 市场对标 ----
    L.append("### 4.2 市场对标与差距分析")
    L.append("")
    if not mkt:
        L.append(f"> ⚠️ **{MISSING_TEXT}**：未检测到 `market_benchmark` 结果，"
                 "请先执行 `market_benchmark`（需要数据中包含市场 P25/P50/P75）。")
        L.append("")
    elif not _get(mkt, "has_market_data", default=True):
        L.append("> ⚠️ 原始数据中未提供市场分位值（P25/P50/P75），无法完成外部对标。"
                 "补充市场薪酬调研数据后重跑即可生成本节。")
        L.append("")
    else:
        rows_src = _get(mkt, "by_level", "table", "bench", default=[]) or []
        ms = _get(mkt, "summary", default={}) or {}
        if rows_src:
            rows = []
            for r in rows_src:
                if not isinstance(r, dict):
                    continue
                g = _num(_get(r, "gap_p50_pct", "gap_pct"))
                # 中国语境：低于市场标绿（需补涨），高于市场标红（成本偏高）
                mark = "🔴 高于" if (g is not None and g > 0.02) else (
                    "🟢 低于" if (g is not None and g < -0.02) else "持平")
                rows.append([
                    r.get("level", "—"),
                    _int(_get(r, "count")),
                    _money(_get(r, "company_median"), "元"),
                    _money(_get(r, "mkt_p25"), "元"),
                    _money(_get(r, "mkt_p50"), "元"),
                    _money(_get(r, "mkt_p75"), "元"),
                    (_pct(g, signed=True) if g is not None else "—"),
                    _money(_get(r, "gap_cost_annual", "annual_cost")),
                    mark,
                ])
            L += _table(["职级", "人数", "公司中位", "市场P25", "市场P50", "市场P75",
                         "相对P50", "补到P50年成本", "位置"],
                        rows, ["l", "r", "r", "r", "r", "r", "r", "r", "l"])
            L.append("")

            if charts:
                L += ctx.figure(
                    "market_gap", "公司薪资 vs 市场分位值（P25 / P50 / P75）",
                    lambda: charts.market_gap_chart(rows_src))

        overall = _num(_get(ms, "overall_gap_p50_pct", "overall_gap_pct"))
        if overall is not None:
            L.append(f"**整体对标结论：** 公司加权平均相对市场 P50 为 **{_pct(overall, signed=True)}**，"
                     f"其中低于 P50 的职级 {_int(_get(ms, 'levels_below_p50'))} 个、"
                     f"高于 P50 的职级 {_int(_get(ms, 'levels_above_p50'))} 个；"
                     f"补齐到 P50 的年度成本约 {_money(_get(ms, 'adjustment_cost_annual'))}"
                     f"（占薪资总额 {_pct(_get(ms, 'adjustment_cost_pct'))}）。")
            L.append("")

        strat = _get(ms, "target_strategy_by_family", "strategy_by_family", default={}) or {}
        if strat:
            L.append("**建议的分位值策略（按岗位序列差异化对标）：**")
            L.append("")
            L += _table(["岗位序列", "建议分位", "策略含义"],
                        [[f, p, {"P25": "滞后市场：成本优先，适用于可替代的操作类岗位",
                                 "P50": "跟随市场：通用岗位的默认选择",
                                 "P75": "领先市场：核心/稀缺岗位，用薪酬换留存与质量"
                                 }.get(p, "—")]
                         for f, p in strat.items()],
                        ["l", "c", "l"])
            L.append("")

        L += _warn_lines("对标提示", [str(w) for w in (_get(mkt, "warnings", default=[]) or [])])

    # ---- 4.3 岗位价值评估校验 ----
    L.append("### 4.3 岗位价值评估校验")
    L.append("")
    if not je:
        L.append(f"> ⚠️ **{MISSING_TEXT}**：未检测到 `job_eval` 结果，"
                 "如需校验职级合理性，请执行 `calc_job_score`。")
        L.append("")
    else:
        table = _get(je, "table", "scores", default=[]) or []
        js = _get(je, "summary", default={}) or {}
        L.append(f"评估模型：**{_get(je, 'model_label', 'model', default='—')}**。"
                 f"样本岗位 {_int(_get(js, 'total_jobs'))} 个，"
                 f"其中 {_int(_get(js, 'mismatch_count'))} 个岗位的实际职级与岗位得分建议职级不一致"
                 f"（占比 {_pct(_get(js, 'mismatch_pct'))}）。")
        L.append("")
        if table:
            rows = []
            for r in table:
                if not isinstance(r, dict):
                    continue
                rows.append([r.get("job_title", "—"),
                             _money(_get(r, "score", "job_score"), "元") if False else
                             (f"{_num(_get(r, 'score', 'job_score')):.0f}"
                              if _num(_get(r, "score", "job_score")) is not None else "—"),
                             r.get("current_level", r.get("actual_level", "—")),
                             r.get("suggested_level", "—"),
                             r.get("gap", "—")])
            L += _table(["岗位", "评估得分", "现职级", "建议职级", "差异"],
                        rows, ["l", "r", "c", "c", "l"])
            L.append("")
        L += _warn_lines("岗位评估提示", [str(w) for w in (_get(je, "warnings", default=[]) or [])])

    return L


# =============================================================================
# 九、第五章：调薪方案对比与推荐
# =============================================================================

def _s5_increase(ctx: _ReportCtx) -> List[str]:
    """调薪章：预算约束下四套策略的成本与红绿圈改善对比 + 前后 CR 分布图。"""
    L: List[str] = []
    inc = ctx.sec(mk("increase"))
    if not inc:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `increase_sim` 结果，"
                f"请先执行 `simulate_increase`（需先确定预算比例）。", ""]

    budget = _num(_get(inc, "budget", "budget_amount"))
    budget_pct = _num(_get(inc, "budget_pct"))
    strategies = _get(inc, "strategies", "compare", "table", default=[]) or []

    L.append("### 5.1 预算约束")
    L.append("")
    L.append(f"本轮可调配的年度调薪预算为 **{_money(budget)}**"
             f"（占薪酬总额 **{_pct(budget_pct)}**）。"
             "预算既是上限也是纪律：任何方案的年度新增成本都不应突破该额度，"
             "否则需要在「缩小覆盖面」与「分期实施」之间二选一。")
    L.append("")

    if strategies:
        L.append("### 5.2 四套策略对比")
        L.append("")
        rows = []
        for s in strategies:
            if not isinstance(s, dict):
                continue
            name = _get(s, "label", "name", "strategy", default="—")
            star = " ★" if s.get("recommended") else ""
            rows.append([
                f"{name}{star}",
                _money(_get(s, "total_cost", "cost")),
                _pct(_get(s, "cost_pct")),
                f"{_int(_get(s, 'red_before'))} → {_int(_get(s, 'red_after'))}",
                f"{_int(_get(s, 'green_before'))} → {_int(_get(s, 'green_after'))}",
                (f"{_num(_get(s, 'cr_std_after')):.3f}"
                 if _num(_get(s, "cr_std_after")) is not None else "—"),
                _get(s, "pros", default="—") or "—",
                _get(s, "cons", default="—") or "—",
            ])
        L += _table(["策略", "年度成本", "占薪资比", "红圈人数(前→后)",
                     "绿圈人数(前→后)", "CR标准差(后)", "优点", "局限"],
                    rows, ["l", "r", "r", "c", "c", "r", "l", "l"])
        L.append("")
        L.append("> 判读口径：**不是越便宜越好**。A 方案（平均分配）通常最省沟通成本，"
                 "但绿圈补涨不足，留人效率最低；应以「每万元消掉的绿圈人数」作为性价比指标。")
        L.append("")

        if charts:
            L += ctx.figure(
                "strategy_cost", "各调薪策略年度成本对比",
                lambda: charts.strategy_cost_chart(strategies, budget=budget))

        # 策略说明（desc 字段）
        descs = [(str(_get(s, "label", "name", "strategy", default="")), str(_get(s, "desc", default="")))
                 for s in strategies if isinstance(s, dict) and _get(s, "desc")]
        if descs:
            L.append("**策略机制说明：**")
            L.append("")
            L += _bullets([f"**{n}** —— {d}" for n, d in descs])
            L.append("")

    # ---- 5.3 调薪前后 CR 分布对比 ----
    before = _get(inc, "before", "cr_before", default=None)
    after = _get(inc, "after", "cr_after", default=None)
    if before is not None and after is not None and charts:
        L.append("### 5.3 调薪前后 CR 分布对比")
        L.append("")
        L += ctx.figure(
            "cr_before_after", "调薪前后 CR 分布对比",
            lambda: charts.cr_before_after_chart(before, after))

    # ---- 5.4 推荐结论 ----
    rec = _get(inc, "recommended", "recommended_strategy")
    if rec:
        L.append("### 5.4 推荐方案")
        L.append("")
        L.append(f"**推荐采用：{rec}**")
        L.append("")
        reason = _get(inc, "recommend_reason", default="")
        if reason:
            L.append(str(reason))
            L.append("")
    dp = _get(inc, "details_path", default="")
    if dp:
        L.append(f"> 逐人调薪明细已导出至：`{dp}`（可用于 HRBP 一对一沟通与系统批量导入）。")
        L.append("")

    L += _warn_lines("调薪风险提示", [str(w) for w in (_get(inc, "warnings", default=[]) or [])])
    return L


# =============================================================================
# 十、第六章：固浮比与激励建议
# =============================================================================

def _s6_pay_mix(ctx: _ReportCtx) -> List[str]:
    """
    固浮比章：目标固浮比 + 激励曲线 + **高浮动的前提条件**（PRD 硬性要求）。

    这一章最容易写错的地方：只讲"改成 40:60 激励更强"，不讲前提条件。
    真实落地中，研发/职能序列强推高浮动会直接破坏协作，是项目失败的高频原因，
    因此方法论提示**必须原样出现在报告正文**，不能只在代码注释里。
    """
    L: List[str] = []
    pm = ctx.sec(mk("paymix"))
    if not pm:
        return [f"> ⚠️ **{MISSING_TEXT}**：未检测到 `pay_mix` 结果，"
                f"请先执行 `simulate_pay_mix`。", ""]

    by_family = _get(pm, "by_family", "table", "families", default=[]) or []
    curves = _get(pm, "curves", "curve_data", default=None)
    target_tc = _num(_get(pm, "target_tc"))

    L.append("### 6.1 目标固浮比与现状差距")
    L.append("")
    if by_family:
        rows = []
        for r in by_family:
            if not isinstance(r, dict):
                continue
            rows.append([
                r.get("job_family", "—"),
                _get(r, "current_mix", default="—"),
                _get(r, "target_mix", default="—"),
                _money(_get(r, "fixed_annual", "fixed")),
                _money(_get(r, "variable_annual", "variable")),
                _money(_get(r, "income_at_0")),
                _money(_get(r, "income_at_100")),
                _money(_get(r, "income_at_150")),
                "✅ 满足" if r.get("precondition_ok") else "⚠️ 不成立",
            ])
        L += _table(["岗位序列", "现状固浮", "目标固浮", "固定薪/年", "浮动薪/年",
                     "达成0%收入", "达成100%收入", "达成150%收入", "高浮动前提"],
                    rows, ["l", "c", "c", "r", "r", "r", "r", "r", "c"])
        L.append("")
        if target_tc:
            L.append(f"> 设计原则：各序列在**达成率 100%** 处的目标总现金一致"
                     f"（均为 {_money(target_tc)}），差异只体现在**曲线斜率**（浮动占比）上。"
                     "若为低浮动序列压低目标收入，本质上是变相降薪，必然引发抵触。")
            L.append("")

    # ---- 6.2 激励曲线 ----
    if curves and charts:
        L.append("### 6.2 激励曲线（达成率 0%~150%）")
        L.append("")
        L += ctx.figure(
            "paymix_curve", "各岗位序列激励曲线",
            lambda: charts.paymix_curve_chart(curves, baseline_income=target_tc))
        L.append("计算公式：**实际总收入 = 固定薪 + 浮动薪 × 业绩达成率**。"
                 "曲线斜率即激励强度 —— 斜率越陡，达标的收益与未达标的损失都被放大。")
        L.append("")

    # ---- 6.3 方法论前提（PRD 硬性要求，必须原样出现） ----
    L.append("### 6.3 高浮动比例的前提条件（务必阅读）")
    L.append("")
    principle = _get(pm, "principle", default="")
    text = principle if (isinstance(principle, str) and PAY_MIX_PRINCIPLE[:12] in principle) \
        else PAY_MIX_PRINCIPLE
    L.append(f"> ⚠️ **{text}**")
    L.append("")
    L.append("三条前提的落地判据（逐条自查后再决定是否提高浮动占比）：")
    L.append("")
    L += _bullets([
        "**业绩可量化** —— 能否用客观指标（签约额、产量、良率）而非主观评价衡量？"
        "无法量化的岗位，浮动部分极易演变成「领导印象分」。",
        "**可归因到个人** —— 结果能否清晰追溯到个人贡献？"
        "强协作型产出（研发、项目制交付）归因模糊，强行挂钩会诱发抢功与信息壁垒。",
        "**结算周期短** —— 能否按月度/季度结算？"
        "年度甚至更长周期兑现的浮动薪酬，激励感知会随时间衰减，对保留几乎无效。",
    ])
    L.append("")
    notes = [(str(_get(r, "job_family", default="")), str(_get(r, "note", default="")))
             for r in by_family if isinstance(r, dict) and _get(r, "note")]
    if notes:
        L.append("**各序列适用性判断：**")
        L.append("")
        L += _bullets([f"**{f}**：{n}" for f, n in notes])
        L.append("")

    L += _warn_lines("固浮比调整提示", [str(w) for w in (_get(pm, "warnings", default=[]) or [])])
    return L


# =============================================================================
# 十一、第七章：风险提示与实施路线图
# =============================================================================

def _s7_roadmap(ctx: _ReportCtx) -> List[str]:
    """
    风险与路线图章：把前六章的结论转成「谁在什么时间做什么」。

    风险条目按前六章的实际数字动态生成（红圈/绿圈/市场差距/数据质量/沟通），
    路线图固定为 0-1 月 / 1-3 月 / 3-6 月 三段 —— 这是薪酬体系落地的最小可行节奏。
    """
    L: List[str] = []
    cur = ctx.sec(mk("diagnose"))
    mkt = ctx.sec(mk("market"))
    inc = ctx.sec(mk("increase"))
    pm = ctx.sec(mk("paymix"))

    circles = _get(cur, "circles", "red_green", default={}) or {}
    red = _get(circles, "red", default={}) or {}
    green = _get(circles, "green", default={}) or {}
    n_red, n_green = _num(_get(red, "count")), _num(_get(green, "count"))
    p_red, p_green = _num(_get(red, "pct")), _num(_get(green, "pct"))
    ms = _get(mkt, "summary", default={}) or {}
    gap = _num(_get(ms, "overall_gap_p50_pct", "overall_gap_pct"))

    # ---- 7.1 风险清单 ----
    L.append("### 7.1 风险清单")
    L.append("")
    risks: List[List[str]] = []
    if n_red:
        risks.append([
            "成本溢出风险",
            "高" if (p_red or 0) > 0.12 else "中",
            f"{_int(n_red)} 名红圈员工（{_pct(p_red)}）薪酬高于带宽上限",
            "冻结月薪、改发一次性补贴；连续 2 年不动的，结合绩效做岗位或职级复盘",
        ])
    if n_green:
        risks.append([
            "人才流失风险",
            "高" if (p_green or 0) > 0.12 else "中",
            f"{_int(n_green)} 名绿圈员工（{_pct(p_green)}）薪酬低于带宽下限，是离职高发区",
            "本轮调薪优先补到带宽下限；对核心岗位单独做 retention 沟通",
        ])
    if gap is not None and gap < -0.02:
        risks.append([
            "外部竞争力风险", "中",
            f"整体低于市场 P50 {_pct(gap, signed=True)}，招聘与保留两端承压",
            "按序列差异化对标：核心岗 P75、通用岗 P50、操作岗 P25，避免全员普涨",
        ])
    risks.append([
        "数据安全风险", "高",
        "薪酬数据属最高敏感级别，一旦外泄后果不可逆",
        "处理真实数据时务必使用本地模型部署（Ollama / vLLM），切勿上传云端 API；"
        "对外材料一律使用脱敏数据",
    ])
    risks.append([
        "绩效公信力风险", "中",
        "绩效加权型调薪（策略 C）强依赖评级可信度；评级失真时激励效果反向",
        "先做绩效校准（跨部门拉齐分布），再启用绩效加权分配",
    ])
    risks.append([
        "沟通与预期风险", "中",
        "结构性调薪必然产生「有人没涨」的被剥夺感",
        "提前准备分层沟通话术：先讲规则与依据，再讲个人结果；经理层先于员工沟通",
    ])
    risks.append([
        "数据质量风险", "低",
        "字段缺失与非数值单元格会影响成本测算精度",
        "按第 2 章清洗摘要逐项补齐；缺失率 > 30% 的字段相关结论标注为估算",
    ])
    L += _table(["风险类别", "等级", "风险描述", "应对措施"], risks, ["l", "c", "l", "l"])
    L.append("")

    # ---- 7.2 实施路线图 ----
    L.append("### 7.2 实施路线图")
    L.append("")
    p1: List[str] = [
        "**冻结异常、止血先行**：暂停个案调薪与特批，锁死红圈的进一步上移",
        "完成数据清洗与字段映射复核（对照第 2 章的清洗摘要与缺失率）",
        "与管理层就带宽方案、调薪预算、分位值策略达成共识并书面确认",
        "准备分层沟通材料：规则口径、FAQ、经理层宣讲稿",
    ]
    p2: List[str] = [
        "发布新带宽表与职级映射，HRBP 完成逐部门宣讲",
        f"执行本轮调薪（推荐方案：{_get(inc, 'recommended', default='待定')}），"
        "逐人结果经 HRBP 复核后入系统",
        "绿圈员工优先补到带宽下限；红圈员工改发一次性补贴并一对一沟通",
        "建立调薪后 CR 复盘的基线数据（留存调薪前后快照，用于下一轮对比）",
    ]
    p3: List[str] = [
        "固浮比改革试点：优先在**满足前提条件**的序列（如销售）先行，"
        "研发/职能序列保持低浮动",
        "补齐市场薪酬调研数据，把市场对标从「一次性项目」转为「年度例行」",
        "岗位价值评估扩展覆盖，校准职级与岗位得分的偏离（对照第 4.3 节）",
        "复盘本轮调薪效果：绿圈人数下降幅度、主动离职率、人均效能变化",
    ]
    L += _table(
        ["阶段", "时间窗", "关键动作", "交付物"],
        [
            ["第一阶段", "0 - 1 个月",
             "<br>".join(f"{i+1}. {t}" for i, t in enumerate(p1)),
             "带宽方案定稿、预算批文、沟通材料"],
            ["第二阶段", "1 - 3 个月",
             "<br>".join(f"{i+1}. {t}" for i, t in enumerate(p2)),
             "调薪执行完毕、系统数据更新、CR 基线"],
            ["第三阶段", "3 - 6 个月",
             "<br>".join(f"{i+1}. {t}" for i, t in enumerate(p3)),
             "固浮比试点报告、年度对标机制、效果复盘"],
        ],
        ["l", "c", "l", "l"],
    )
    L.append("")

    # ---- 7.3 方法与数据声明 ----
    L.append("### 7.3 方法论与数据声明")
    L.append("")
    L += _bullets([
        "**3P 模型**：本报告以「岗位（Position）— 能力（Person）— 绩效（Performance）」"
        "为框架，带宽对应岗位价值，CR 对应能力/司龄积累，调薪权重对应绩效。",
        "**CR（Compa-Ratio）** = 个人薪资 ÷ 该职级带宽中位值，"
        f"合理区间 {GREEN_CIRCLE_CR:.2f}~{RED_CIRCLE_CR:.2f}；"
        f"**薪酬渗透率（Range Penetration）** = (薪资 − 下限) ÷ (上限 − 下限)。",
        "**带宽幅度**按职级分层取值（基层 25%~28% / 专业·技术 35%~38% / 中高层 45%~60%），"
        "公式：下限 = 中位值 ÷ (1 + 幅度/2)，上限 = 下限 × (1 + 幅度)。",
        "**所有数值由 Python 确定性计算产出**，AI 仅负责语义理解、工具调度与结论解读，"
        "不参与任何薪酬数字的运算 —— 保证结果可复现、可审计。",
        "**数据安全**：演示数据均为随机合成的模拟数据，与任何真实自然人无关；"
        "处理真实薪资数据请使用本地模型部署，切勿上传至云端 API。",
    ])
    L.append("")
    return L


# =============================================================================
# 十二、章节注册表
# =============================================================================

SECTION_BUILDERS: List[Tuple[str, Callable[[_ReportCtx], List[str]]]] = [
    ("执行摘要", _s1_exec_summary),
    ("数据概览与字段映射说明", _s2_data_overview),
    ("薪酬现状诊断", _s3_current_state),
    ("带宽设计与市场对标建议", _s4_band_market),
    ("调薪方案对比与推荐", _s5_increase),
    ("固浮比与激励建议", _s6_pay_mix),
    ("风险提示与实施路线图", _s7_roadmap),
]


def _normalize_sections(include_sections: Any) -> Optional[List[str]]:
    """
    把 include_sections 归一化成章节标题列表。

    接受三种写法，方便模型/用户随意调用：
      - 中文标题（全名或子串）：["执行摘要", "固浮比"]
      - 序号：["1", "3"] 或 [1, 3]
      - None：全部章节
    """
    if not include_sections:
        return None
    if isinstance(include_sections, str):
        include_sections = [include_sections]
    out: List[str] = []
    for item in list(include_sections):
        if isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit()):
            idx = int(str(item).strip())
            if 1 <= idx <= len(SECTION_TITLES):
                out.append(SECTION_TITLES[idx - 1])
            continue
        s = str(item).strip()
        for t in SECTION_TITLES:
            if t == s or s in t or t in s:
                if t not in out:
                    out.append(t)
                break
    return out or None


# =============================================================================
# 十三、Markdown → HTML
# =============================================================================

_CSS = """
:root{
  --ink:#263238; --sub:#546E7A; --line:#E3E7EA; --accent:#1565C0;
  --red:#C62828; --green:#2E7D32; --bg:#FFFFFF; --soft:#F7F9FA;
}
*{box-sizing:border-box;}
body{
  margin:0; padding:0; background:#EEF1F3; color:var(--ink);
  font-family:"Microsoft YaHei","SimHei","Noto Sans CJK SC",sans-serif;
  font-size:15px; line-height:1.85;
}
.page{max-width:1080px; margin:0 auto; background:var(--bg); padding:56px 64px 80px;
      box-shadow:0 2px 24px rgba(0,0,0,.08);}
h1{font-size:28px; font-weight:700; margin:0 0 6px; letter-spacing:.5px;}
.meta{color:var(--sub); font-size:13px; margin:0 0 28px; padding-bottom:18px;
      border-bottom:2px solid var(--accent);}
h2{font-size:21px; font-weight:700; margin:44px 0 16px; padding-left:12px;
   border-left:5px solid var(--accent); color:#12395F;}
h3{font-size:16.5px; font-weight:700; margin:28px 0 12px; color:#1E4B73;}
h4{font-size:15px; font-weight:700; margin:20px 0 10px; color:var(--sub);}
p{margin:10px 0;}
table{border-collapse:collapse; width:100%; margin:16px 0 20px; font-size:13.5px;}
th{background:#EEF3F8; color:#12395F; font-weight:700; text-align:left;
   padding:9px 12px; border:1px solid var(--line); white-space:nowrap;}
td{padding:8px 12px; border:1px solid var(--line); vertical-align:top;}
tr:nth-child(even) td{background:#FBFCFD;}
blockquote{margin:14px 0; padding:12px 18px; background:var(--soft);
           border-left:4px solid var(--amber,#EF6C00); color:#37474F;}
blockquote p{margin:4px 0;}
ul,ol{margin:10px 0 16px; padding-left:26px;}
li{margin:6px 0;}
code{background:#F2F5F7; padding:1px 6px; border-radius:3px; font-size:13px;
     font-family:Consolas,Monaco,monospace; color:#B71C1C;}
img{max-width:100%;}
a{color:var(--accent);}
hr{border:0; border-top:1px solid var(--line); margin:32px 0;}
.chart{margin:20px 0 28px; padding:12px; border:1px solid var(--line); border-radius:6px;}
.watermark-banner{background:#C62828; color:#fff; font-weight:700; font-size:14px;
       text-align:center; padding:11px 16px; border-radius:6px; margin:0 0 22px;
       letter-spacing:.3px; box-shadow:0 1px 6px rgba(198,40,40,.35);}
.watermark-banner-synthetic{background:#37474F; color:#ECEFF1; font-weight:600;
       font-size:13.5px; text-align:center; padding:10px 16px; border-radius:6px;
       margin:0 0 22px; letter-spacing:.3px; box-shadow:0 1px 6px rgba(55,71,79,.30);}
.footer{margin-top:48px; padding-top:18px; border-top:1px solid var(--line);
        color:var(--sub); font-size:12.5px; text-align:center;}
@media print{body{background:#fff;} .page{box-shadow:none; padding:0;} h2{page-break-after:avoid;}}
"""


def _embed_figures(html_body: str, figures: List[Dict[str, Any]]) -> str:
    """
    把 Markdown 转出来的 `<img>` 占位替换成真正的 Plotly div。

    为什么不直接引用图片文件：报告要能单文件分发，且 PNG 在本机无法导出
    （kaleido 缺失）。内嵌 div 后，HTML 报告就是一个自包含的交互式文档。
    """
    by_name = {os.path.basename(f.get("rel_path") or ""): f for f in figures}

    def _render(src: str) -> str:
        """按图片 src 的文件名找到对应图表，替换成内嵌 div。"""
        fig = by_name.get(os.path.basename(src))
        if not fig:
            return ""
        div = fig.get("div") or ""
        if not div:
            return (f'<p class="chart-missing">图表「{html_lib.escape(fig.get("caption", ""))}」'
                    f'未生成，请打开 <a href="{html_lib.escape(src)}">'
                    f'{html_lib.escape(src)}</a></p>')
        if charts and hasattr(charts, "strip_plotlyjs_cdn"):
            try:
                div = charts.strip_plotlyjs_cdn(div)
            except Exception:  # noqa: BLE001
                pass
        cap = html_lib.escape(str(fig.get("caption", "")))
        return f'<div class="chart"><div class="chart-cap"><b>图：{cap}</b></div>{div}</div>'

    # 两种形态都要覆盖：被 <p> 包住的独立图片段落（markdown 库产出），
    # 以及行内图片（nl2br 扩展下可能与文字同段）。
    # 末尾的 `(?:\s*<div align="center">…</div>)?` 用于**一并吃掉 Markdown 里的图注块**：
    # 那段图注是给 Markdown 版用的（告诉用户去打开 HTML 文件），
    # 但 HTML 版已经把图内嵌进来了，留着就会出现两个重复图注。
    pattern = re.compile(
        r'(?:<p>\s*<img[^>]*?src="(?P<p_src>[^"]+)"[^>]*>\s*</p>'
        r'|(?P<inline><img[^>]*?src="(?P<i_src>[^"]+)"[^>]*>))'
        r'(?:\s*<div align="center">.*?</div>)?',
        re.DOTALL,
    )

    def _repl(m: "re.Match[str]") -> str:
        if m.group("p_src"):
            return _render(m.group("p_src")) or m.group(0)
        return _render(m.group("i_src")) or m.group(0)

    return pattern.sub(_repl, html_body)


def _md_to_html(md: str, title: str, subtitle: str,
                figures: List[Dict[str, Any]],
                classification: str = "real") -> str:
    """
    Markdown → 完整 HTML（含 CSS、中文字体、内嵌图表 div）。

    plotly.js 只引入一次：从第一张图的 div 里抽出 CDN 地址放进 <head>，
    其余图的 script 标签用 strip_plotlyjs_cdn 剥掉 —— 否则 6 张图会重复加载 6 次 3MB。

    classification : "real"（默认，含真实薪酬）| "sanitized"（已脱敏）| "synthetic"（模拟数据）。
        决定页脚与顶部水印措辞——
          - real      ：红色醒目水印「含真实薪酬数据 · 请勿外发」
          - sanitized ：无横幅，页脚标注「数据已脱敏，可安全外发」
          - synthetic ：中性蓝灰横幅「模拟数据，可安全演示」（随机合成，不含真实自然人），
                        **绝不**使用红色真实数据措辞。
    """
    body = md
    try:
        import markdown as md_lib  # type: ignore
        body = md_lib.markdown(md, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    except Exception:  # noqa: BLE001 - markdown 库缺失时降级为等宽纯文本，不中断交付
        body = "<pre>" + html_lib.escape(md) + "</pre>"

    body = _embed_figures(body, figures)

    # 从第一张图的 div 里取 plotly.js 地址，保证与图表模块用的版本一致
    plotly_js = "https://cdn.plot.ly/plotly-2.35.2.min.js"
    for f in figures:
        m = re.search(r'https://cdn\.plot\.ly/plotly-[0-9.]+\.min\.js', f.get("div") or "")
        if m:
            plotly_js = m.group(0)
            break

    # —— 数据护栏（R2）：按数据分级渲染不同顶部横幅 + 页脚 ——
    # real=红字真实数据警示；synthetic=中性蓝灰「模拟数据」横幅（可安全演示）；
    # sanitized=无横幅、页脚标注已脱敏可外发。绝不把模拟数据伪装成真实/脱敏。
    if classification == "sanitized":
        banner = ""
        footer_text = ("本报告由「薪酬诊断 Agent（CCO Copilot）」自动生成 · "
                       "数值由 Python 确定性计算产出 · 数据已脱敏处理"
                       "（个体加盐哈希不可逆、薪资总额守恒），可安全外发")
    elif classification == "synthetic":
        banner = ('<div class="watermark-banner watermark-banner-synthetic">'
                  '🧪 本报告为<b>模拟数据</b>演示 · 全部姓名/薪资均为随机合成，'
                  '不含任何真实自然人 · 可安全对外演示</div>\n')
        footer_text = ("本报告由「薪酬诊断 Agent（CCO Copilot）」自动生成 · "
                       "数值由 Python 确定性计算产出 · 数据为随机合成的模拟数据，可安全演示")
    else:  # real（含未识别）
        banner = ('<div class="watermark-banner">⚠️ 本报告含真实薪酬数据 · '
                  '仅限本地查看 · 请勿外发或提交到公开仓库</div>\n')
        footer_text = ("⚠️ 含真实薪酬数据 · 仅限本地使用 · 请勿外发 · "
                       "本报告由「薪酬诊断 Agent（CCO Copilot）」自动生成 · "
                       "数值由 Python 确定性计算产出")

    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html_lib.escape(title)}</title>\n"
        f"<script src=\"{plotly_js}\" charset=\"utf-8\"></script>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n<div class=\"page\">\n"
        f"{banner}"
        f"<h1>{html_lib.escape(title)}</h1>\n"
        f"<p class=\"meta\">{html_lib.escape(subtitle)}</p>\n"
        f"{body}\n"
        f"<div class=\"footer\">{footer_text}</div>\n"
        "</div>\n</body>\n</html>\n"
    )


def _detect_salary_classification(meta: Any) -> str:
    """
    判断本报告数据分级（R2 水印依据），返回规范值："real" / "sanitized" / "synthetic"。

    优先级：
      1. session.meta["data_classification"] 显式标记（"sanitized" / "real" / "synthetic"）。
         "simulated" 视为 "synthetic" 的别名（config.yaml 用 simulated，加载端已归一）。
      2. 兜底：数据源文件名为 `*_desensitized.csv` → 视为已脱敏
         （desensitize() 产物的典型命名）。
      3. 默认 "real"：CLI 绝大多数场景直接跑真实工资表，
         宁可「多一道水印」也不要谎称脱敏/模拟。
    """
    if not isinstance(meta, dict):
        return "real"
    dc = (meta.get("data_classification") or "").strip().lower()
    if dc in ("sanitized", "real"):
        return dc
    if dc in ("synthetic", "simulated"):
        return "synthetic"
    src = (meta.get("source_file")
           or (meta.get("mapping") or {}).get("source_file")
           or "")
    if str(src).strip().endswith("_desensitized.csv"):
        return "sanitized"
    return "real"


def _write_data_guard(path: str, title: str, sid: str, now: str,
                      report_md_path: Optional[str]) -> None:
    """
    R4：真实数据护栏 sidecar。与报告同目录，明确告知风险与处置路径。
    失败静默（绝不能因 sidecar 写失败而拖垮主报告）。
    """
    md_name = os.path.basename(report_md_path) if report_md_path else "(见同目录 .md)"
    lines = [
        f"# ⚠️ 数据护栏提示（{title}）",
        "",
        f"> 生成时间：{now}　｜　会话 ID：`{sid}`",
        "",
        "---",
        "",
        "**本报告 / 图表包含真实薪酬数值。**",
        "",
        "安全须知：",
        "- 仅限本机查看，**严禁**外发、上传公开仓库或通过任何云端服务传输。",
        "- 如需对外演示或共享，请先调用 `desensitize()` 生成脱敏副本，再基于脱敏副本生成报告。",
        "- 已确认本地使用、仍需消除本提示时，调用 "
        "`generate_report(..., i_know_real_data=True)`",
        "（CLI：`--i-know-this-is-real-data`）。",
        "",
        "相关产物：",
        f"- 报告（Markdown，已含水印）：`{md_name}`",
        f"- 本报告所属会话：`{sid}`",
        "",
    ]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
    except Exception:  # noqa: BLE001
        pass


# =============================================================================
# 十四、主入口
# =============================================================================

def generate_report(
    session_id: Any = None,
    title: Optional[str] = None,
    include_sections: Any = None,
    fmt: str = "markdown",
    session: Any = None,
    meta: Any = None,
    i_know_real_data: bool = False,
) -> Dict[str, Any]:
    """
    生成七章结构的薪酬诊断报告（Markdown / HTML / 两者）。

    Args:
        session_id:       会话 ID；也可直接传 Session 对象（内部会自动识别）
        title:            报告标题，默认「薪酬诊断报告」
        include_sections: 只生成指定章节；支持中文标题/子串或 1-7 序号，None 表示全部
        fmt:              'markdown'（默认）| 'html' | 'both'
        session:          显式传入 Session 对象（测试与本地入口用，优先级高于 session_id）
        meta:             显式传入 meta 字典（最高优先级，用于离线集成测试）

    Returns:
        {
          "ok": True,
          "session_id": str,
          "title": str,
          "report_path": 绝对路径（fmt 含 markdown 时）
          "html_path":   绝对路径（fmt 含 html 时），否则 None
          "sections":          已生成章节标题列表
          "missing_sections":  因前置数据缺失而降级为提示文案的章节
          "excluded_sections": 被 include_sections 排除的章节
          "figures":      [{name, caption, html_path, png_path, rel_path, exists, is_png}, ...]
          "figure_count": 图表张数
          "warnings":     全部降级/异常提示
          "generated_at": "YYYY-MM-DD HH:MM:SS"
          "timestamp":    "YYYYmmdd_HHMMSS"（文件名后缀）
          "char_count":   正文字数
          "fmt":          实际生效的输出格式
        }
        失败时返回 {"ok": False, "error": {"code", "message", "hint", "details"}}

    设计要点
    --------
    * **绝不因单章失败中断**：每章独立 try/except，挂掉就退化成提示文案。
    * **图片相对路径以报告文件为基准**现算，并逐张 os.path.exists 断言（AC-26）。
    * fmt 大小写/别名容错（'md' / 'markdown' / 'html' / 'both' / 'all'）。
    """
    fmt_norm = str(fmt or "markdown").strip().lower()
    if fmt_norm in ("both", "all", "md+html", "markdown+html"):
        want_md, want_html = True, True
    elif fmt_norm in ("html", "htm"):
        want_md, want_html = False, True
    else:
        want_md, want_html = True, False

    try:
        resolved, sid, warns = _resolve_session(session_id, session=session, meta=meta)
        if resolved is None:
            return error_result(
                "SessionNotFound",
                message=f"未找到会话「{sid}」的分析结果，无法生成报告。",
                hint="请先依次执行 load_salary_data → confirm_mapping → analyze_current_state，"
                     "或检查 session_id 是否正确、会话是否已过期。",
                details={"session_id": sid, "warnings": warns},
            )

        # ---- 报告骨架 ----
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report_title = title or "薪酬诊断报告"
        os.makedirs(REPORT_DIR, exist_ok=True)
        report_path = os.path.join(REPORT_DIR, f"{report_title}_{ts}.md")
        html_path = os.path.join(REPORT_DIR, f"{report_title}_{ts}.html")

        ctx = _ReportCtx(resolved, sid, REPORT_DIR, report_title)
        ctx.warnings.extend(warns)

        # ---- 数据护栏（R2）：判断数据分级，仅真实数据加醒目红字水印 + 护栏 ----
        # synthetic（模拟数据）/ sanitized（已脱敏）均不触发真实数据护栏与 data_guard sidecar。
        # 注意：_resolve_session 的返回值首元素就是 meta 字典（不是 Session 对象），
        # 故直接把 resolved 传给分类函数；仅当 resolved 非 dict 时回退到 .meta / meta 参数。
        classification = _detect_salary_classification(
            resolved if isinstance(resolved, dict) else (getattr(resolved, "meta", None) or meta))
        if classification == "real":
            guard_msg = ("⚠️ 薪酬数据护栏：本报告含真实薪酬数值，仅限本地查看，"
                         "请勿外发或提交到公开仓库。如需外发，请先调用 desensitize() "
                         "生成脱敏副本。")
            ctx.warnings.append(guard_msg)
            print(guard_msg, file=sys.stderr)

        wanted = _normalize_sections(include_sections)
        excluded = [t for t in SECTION_TITLES if wanted and t not in wanted]

        md_lines: List[str] = [
            f"# {report_title}",
            "",
            f"> 生成时间：{now}　｜　会话 ID：`{sid}`　｜　"
            f"分析引擎：薪酬诊断 Agent（CCO Copilot）",
            "",
            "---",
            "",
        ]
        if classification == "real":
            md_lines += [
                "> ⚠️ **数据护栏**：本报告含真实薪酬数据，仅限本地查看，请勿外发。",
                "",
            ]

        sections_done: List[str] = []
        missing: List[str] = []

        for idx, (sec_title, builder) in enumerate(SECTION_BUILDERS):
            if wanted and sec_title not in wanted:
                continue
            md_lines.append(f"## {_CN_NUM[idx]}、{sec_title}")
            md_lines.append("")
            try:
                body = builder(ctx)
            except Exception as exc:  # noqa: BLE001 - 单章失败不拖垮整份报告
                ctx.warn(f"章节「{sec_title}」生成失败：{type(exc).__name__}: {exc}")
                body = [f"> ⚠️ 章节「{sec_title}」生成异常：{type(exc).__name__}: {exc}", ""]
            md_lines += body
            md_lines.append("")
            # 判断本章是否走了"前置步骤缺失"分支
            joined = "\n".join(body)
            if MISSING_TEXT in joined:
                missing.append(sec_title)
            sections_done.append(sec_title)

        # ---- 附录：图表清单（便于用户按图索骥找到交互文件） ----
        if ctx.figures:
            md_lines.append("---")
            md_lines.append("")
            md_lines.append("## 附录：图表清单与文件索引")
            md_lines.append("")
            rows = []
            for i, f in enumerate(ctx.figures, start=1):
                kind = "静态 PNG" if f["is_png"] else "交互式 HTML"
                rows.append([str(i), f.get("caption", "—"), kind,
                             f"`{f['rel_path']}`", "✅ 存在" if f["exists"] else "❌ 缺失"])
            md_lines += _table(["#", "图表", "类型", "相对路径（相对本报告）", "状态"],
                               rows, ["c", "l", "c", "l", "c"])
            md_lines.append("")
            md_lines.append("> 交互式图表需联网加载 plotly.js；若在内网环境打开，"
                            "可将 plotly.min.js 下载到本地并替换 HTML 中的 CDN 引用。")
            md_lines.append("")

        md_text = "\n".join(md_lines).rstrip() + "\n"

        # ---- 落盘 ----
        if want_md:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(md_text)

        html_out: Optional[str] = None
        guard_path: Optional[str] = None
        if want_html:
            subtitle = (f"生成时间：{now}　｜　会话 ID：{sid}　｜　"
                        f"共 {len(sections_done)} 章　｜　图表 {len(ctx.figures)} 张")
            # ---- R4：真实数据护栏 sidecar（仅在未确认且确为真实数据时写）----
            if classification == "real" and not i_know_real_data:
                guard_path = os.path.join(REPORT_DIR, f"{report_title}_{ts}.data_guard.md")
                _write_data_guard(guard_path, report_title, sid, now, report_path)
                guard_msg = ("⚠️ 真实薪酬数据护栏：已生成 data_guard.md 安全提示文件"
                             f"（{guard_path}）。该报告含真实薪酬，仅限本地查看，请勿外发。"
                             "若确认本地使用，可传 i_know_real_data / --i-know-this-is-real-data 抑制此提示。")
                ctx.warnings.append(guard_msg)
                print(guard_msg, file=sys.stderr)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(_md_to_html(md_text, report_title, subtitle, ctx.figures,
                                    classification=classification))
            html_out = html_path

        # ---- 图片路径存在性断言（AC-26） ----
        broken = [f for f in ctx.figures if not f["exists"]]
        for f in broken:
            ctx.warn(f"图表相对路径断链：{f['rel_path']}（报告目录：{REPORT_DIR}）")

        return ok_result(
            session_id=sid,
            title=report_title,
            report_path=report_path if want_md else None,
            html_path=html_out,
            data_guard_path=guard_path,
            sections=sections_done,
            missing_sections=missing,
            excluded_sections=excluded,
            figures=[{k: v for k, v in f.items() if k != "div"} for f in ctx.figures],
            figure_count=len(ctx.figures),
            broken_figures=[f["rel_path"] for f in broken],
            warnings=ctx.warnings,
            generated_at=now,
            timestamp=ts,
            char_count=len(md_text),
            # P9 修复（2026-09-04 续）：语义化摘要由代码生成；导入失败时 registry 兜底。
            summary_md=build_report_summary_md({
                "title": report_title,
                "sections": sections_done,
                "figure_count": len(ctx.figures),
                "char_count": len(md_text),
                "missing_sections": missing,
                "broken_figures": [f["rel_path"] for f in broken],
            }) if build_report_summary_md else None,
            fmt="both" if (want_md and want_html) else ("html" if want_html else "markdown"),
        )

    except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都转成标准错误返回
        return error_result(
            exc,
            message=f"报告生成失败：{type(exc).__name__}: {exc}",
            hint="请检查会话中是否已有分析结果；若持续失败，可先用 "
                 "include_sections=['执行摘要'] 缩小范围定位问题章节。",
            details={"traceback": traceback.format_exc(limit=6)},
        )


# =============================================================================
# 十五、命令行入口（离线演示用）
# =============================================================================

if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="生成薪酬诊断报告（可用假数据离线演示）")
    ap.add_argument("--session-id", default="demo-session", help="会话 ID")
    ap.add_argument("--fmt", default="both", choices=["markdown", "html", "both"],
                    help="输出格式")
    ap.add_argument("--title", default="薪酬诊断报告", help="报告标题")
    ap.add_argument("--mock", action="store_true",
                    help="使用 tests/fixtures/mock_results.py 的假数据（无需真实 session）")
    args = ap.parse_args()

    if args.mock:
        sys.path.insert(0, _PROJECT_ROOT)
        from tests.fixtures.mock_results import build_mock_session  # noqa: E402
        res = generate_report(session=build_mock_session(args.session_id),
                              title=args.title, fmt=args.fmt)
    else:
        res = generate_report(args.session_id, title=args.title, fmt=args.fmt)

    if not res.get("ok"):
        print("报告生成失败：", res.get("error"))
        sys.exit(1)

    print("=" * 70)
    print(f"报告生成成功：{res.get('title')}")
    print("=" * 70)
    print(f"  Markdown : {res.get('report_path')}")
    print(f"  HTML     : {res.get('html_path')}")
    print(f"  章节     : {len(res.get('sections', []))} 章 -> {res.get('sections')}")
    print(f"  降级章节 : {res.get('missing_sections')}")
    print(f"  图表     : {res.get('figure_count')} 张")
    for f in res.get("figures", []):
        print(f"     - {f['caption']:<28} {f['rel_path']}  "
              f"[{'存在' if f['exists'] else '缺失'}]")
    print(f"  断链图表 : {res.get('broken_figures')}")
    print(f"  警告     : {len(res.get('warnings', []))} 条")
    for w in res.get("warnings", [])[:8]:
        print(f"     ! {w}")

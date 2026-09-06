# -*- coding: utf-8 -*-
"""
charts.py — 薪酬诊断 Plotly 图表工厂（模块 2/3/4/5 的视觉出口）
================================================================================

设计目标
--------------------------------------------------------------------------------
1. **每一张图都要回答一个具体的业务问题**，不是“为了好看而画”。
   每个函数的 docstring 第一行都写明「这张图回答什么业务问题」，
   HR 拿去汇报时可以直接把这句话当作图注。

2. **中国语境配色（重要纪律）**：
   中国 A 股/职场语境里 —— **红 = 涨/偏高/需要警惕，绿 = 跌/偏低/需要补**。
   本项目**严禁**使用欧美习惯的“绿涨红跌”，否则红圈（薪酬过高，成本风险）
   会被画成绿色，业务含义完全相反。
     - 红圈（CR > 1.20）：`#C62828` 深红 —— 薪酬高于带宽，成本溢出
     - 绿圈（CR < 0.80）：`#2E7D32` 深绿 —— 薪酬低于带宽，流失风险
     - 正常/基准：`#1565C0` 蓝 —— 中性、专业，适合做基准线与主数据色

3. **白底 + 中文友好**：
     - `template='plotly_white'`，适合直接截图进 PPT / 汇报材料
     - 字体统一 `Microsoft YaHei, SimHei, sans-serif`（Windows 自带，无需装字体）
     - `config` 关闭 plotly logo、`locale='zh-CN'`，图例/轴标题全中文

4. **HTML 优先 + PNG 可选降级（架构决策 C1）**：
   本机 `kaleido` 未安装，静态 PNG 导出必然失败。
   `save_figure()` 用 try-except 包住 `fig.write_image()`，
   失败时 `png_path` 返回 None 并记 `png_error`，**绝不让整个工具调用失败**。
   调用方只需判断 png_path 是否为 None 来决定报告里引用 PNG 还是 HTML。

5. **输入宽容**：每个函数都接受 `DataFrame` / `list[dict]` / `dict` 三种形态，
   列名也有别名容错（如 CR 列认 `cr` / `compa_ratio` / `CR值`）。
   上游工具直接把中间结果丢进来即可，不必特意转格式。

统一的图表返回结构（架构决策 D5）
--------------------------------------------------------------------------------
    {
        "name":       图表名（也是落盘文件名，不含扩展名）
        "html_path":  交互式 HTML 的绝对路径
        "png_path":   静态 PNG 的绝对路径；kaleido 缺失或导出失败时为 None
        "div":        可内嵌到报告 HTML 的 <div> 片段（include_plotlyjs='cdn'）
        "html_rel":   相对项目根的路径（如 "assets/band_overlap.html"）
        "png_rel":    相对项目根的路径；无 PNG 时为 None
        "png_error":  PNG 导出失败原因；成功时为 None
    }

用法示例
--------------------------------------------------------------------------------
    from tools.charts import band_overlap_chart, save_figure

    fig = band_overlap_chart(band_df)               # 先拿 Figure 对象
    saved = save_figure(fig, "band_overlap")         # 再落盘 + 取返回结构
    meta["band"]["figure"] = saved
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# =============================================================================
# 零、常量与基础工具
# =============================================================================

# 项目根：src/tools/charts.py -> 上三级
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 图表落盘目录（可用环境变量覆盖，便于测试隔离）
ASSETS_DIR = os.environ.get("COMP_ASSETS_DIR") or os.path.join(PROJECT_ROOT, "assets")
# 报告目录（report.py 会用到；这里一起定义，保证两侧相对路径口径一致）
REPORT_DIR = os.environ.get("COMP_REPORT_DIR") or os.path.join(PROJECT_ROOT, "report")

# -----------------------------------------------------------------------------
# 配色规范（中国语境：红=涨/偏高，绿=跌/偏低）
# -----------------------------------------------------------------------------
COLOR_RED = "#C62828"        # 红圈 / 偏高 / 高于市场 —— 成本风险、需要关注
COLOR_GREEN = "#2E7D32"      # 绿圈 / 偏低 / 低于市场 —— 流失风险、需要补涨
COLOR_BLUE = "#1565C0"       # 正常 / 基准 / 主数据色 —— 中性专业
COLOR_AMBER = "#EF6C00"      # 推荐方案 / 需要决策的事项
COLOR_GREY = "#78909C"       # 对照序列 / 市场分位辅助线
COLOR_GREY_LIGHT = "#B0BEC5"  # 更浅的对照色（P25）
COLOR_GREY_DARK = "#455A64"  # 更深的对照色（P75）
COLOR_INK = "#263238"        # 正文/刻度文字色

# 多序列曲线用的定性色板（避开红绿，防止与红绿圈语义混淆）
QUAL_COLORS = ["#1565C0", "#EF6C00", "#6A1B9A", "#00838F", "#AD1457", "#5D4037"]

# 中文字体栈：Windows 自带雅黑/黑体，Linux/Mac 自动退到 Noto，最后 sans-serif
CN_FONT = "Microsoft YaHei, SimHei, Noto Sans CJK SC, Source Han Sans SC, sans-serif"

# 统一的 plotly 交互配置：去 logo、中文 locale、保留另存为 PNG（依赖浏览器端 plotly）
PLOTLY_CONFIG: Dict[str, Any] = {
    "displaylogo": False,
    "locale": "zh-CN",
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2},
}

# 红绿圈阈值（与 schemas.py 保持同一口径；此处做导入容错，避免循环依赖）
try:  # pragma: no cover - 导入路径在不同运行方式下不同
    from .schemas import (  # type: ignore
        RED_CIRCLE_CR, GREEN_CIRCLE_CR, level_sort_key, JOB_FAMILY_PAY_MIX,
    )
except Exception:  # noqa: BLE001 - 兜底：作为脚本直接运行时相对导入不可用
    try:
        from tools.schemas import (  # type: ignore
            RED_CIRCLE_CR, GREEN_CIRCLE_CR, level_sort_key, JOB_FAMILY_PAY_MIX,
        )
    except Exception:  # noqa: BLE001
        try:
            from src.tools.schemas import (  # type: ignore
                RED_CIRCLE_CR, GREEN_CIRCLE_CR, level_sort_key, JOB_FAMILY_PAY_MIX,
            )
        except Exception:  # noqa: BLE001 - 最后兜底：硬编码同值常量
            RED_CIRCLE_CR = 1.20
            GREEN_CIRCLE_CR = 0.80
            JOB_FAMILY_PAY_MIX = {}

            def level_sort_key(level: str):
                return (0, 0, str(level))


def _ensure_assets_dir() -> str:
    """确保图表目录存在（幂等，可并发调用）。"""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    return ASSETS_DIR


def _to_df(data: Union[pd.DataFrame, List[dict], Dict[str, Any], None]) -> pd.DataFrame:
    """
    把常见的数据形态统一成 DataFrame，方便后续按列名取值。

    支持：
      - DataFrame          原样返回副本
      - list[dict]         每行一条记录
      - dict[str, list]    列式字典，如 {"level": [...], "band_min": [...]}
      - dict[str, dict]    记录字典，如 {"A平均分配": {"total_cost": 123}}
      - dict[str, scalar]  单条记录，如 {"total_cost": 123, "cost_pct": 1.2}
      - None / 空          返回空 DataFrame（后续章节会降级，不抛错）
    """
    if data is None:
        return pd.DataFrame()
    if isinstance(data, pd.DataFrame):
        return data.copy()
    if isinstance(data, list):
        return pd.DataFrame([d for d in data if isinstance(d, dict)])
    if isinstance(data, dict):
        if not data:
            return pd.DataFrame()
        values = list(data.values())
        # 列式字典：每个值都是序列 -> 直接构造
        if all(isinstance(v, (list, tuple, pd.Series, np.ndarray)) for v in values):
            try:
                return pd.DataFrame(data)
            except Exception:  # noqa: BLE001 - 长度不齐时退化为记录列表
                pass
        # 记录字典：每个值是 dict -> 摊平
        if all(isinstance(v, dict) for v in values):
            return pd.DataFrame(values)
        # 标量字典：视为单行，把键名作为一列 key 保留下来
        return pd.DataFrame([{"key": k, "value": v} for k, v in data.items()])
    return pd.DataFrame()


def _pick_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    """
    在 DataFrame 里按候选名（大小写/空格/下划线不敏感）找第一个命中的列。

    用途：上游工具的列名可能是 `cr` / `compa_ratio` / `CR值` / `Compa Ratio`，
    统一走这里做容错，避免因为一个列名差异导致整张图画不出来。
    """
    if df is None or df.empty:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[\s_\-()（）:：/\\.]", "", str(s)).strip().lower()

    cols = {norm(c): c for c in df.columns}
    for cand in candidates:
        hit = cols.get(norm(cand))
        if hit is not None:
            return hit
    # 退一步：候选名是列名的子串也算命中（如 'cr' 命中 'cr_value'）
    for cand in candidates:
        nc = norm(cand)
        for nc_col, col in cols.items():
            if nc and nc in nc_col:
                return col
    return None


def _numeric(s: Any) -> pd.Series:
    """强制转数值（非数值 -> NaN），并去掉无穷值，保证 plotly 不会因脏值报错。"""
    arr = pd.to_numeric(pd.Series(s), errors="coerce")
    return arr.replace([np.inf, -np.inf], np.nan)


def _clean_num(x: Any) -> Optional[float]:
    """把任意值转成 float；失败或空值返回 None（用于 hover / 标注里的数值）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def _apply_base_layout(
    fig: go.Figure,
    title: str,
    xaxis_title: str = "",
    yaxis_title: str = "",
    height: int = 460,
    width: int = 980,
    legend_title: str = "",
) -> go.Figure:
    """
    套用统一的中文/白底/无 logo 版式。所有图表函数都必须走这里，
    保证 6 张图的字号、字体、底色、边距完全一致（截图进 PPT 才不会花）。
    """
    fig.update_layout(
        template="plotly_white",
        title=dict(
            text=title,
            x=0.01,          # 标题左对齐，符合中文报告阅读习惯
            xanchor="left",
            font=dict(size=17, color=COLOR_INK, family=CN_FONT),
        ),
        font=dict(family=CN_FONT, size=12, color=COLOR_INK),
        xaxis_title=dict(text=xaxis_title, font=dict(size=13, color=COLOR_INK)),
        yaxis_title=dict(text=yaxis_title, font=dict(size=13, color=COLOR_INK)),
        legend=dict(
            title=dict(text=legend_title, font=dict(size=12)) if legend_title else None,
            orientation="h",      # 图例横排放在图下方，避免右侧图例挤压绘图区
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=12),
        ),
        margin=dict(l=90, r=40, t=90, b=70),
        height=height,
        width=width,
        hovermode="closest",
        hoverlabel=dict(font_family=CN_FONT, font_size=12, bgcolor="white"),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )
    return fig


def _fmt_money(v: Optional[float]) -> str:
    """
    金额格式化：>= 1 万元用「万元」口径（HR 汇报习惯），否则保留元。
    例：123456 -> "12.3 万“；8800 -> ”8,800 元"
    """
    if v is None or not np.isfinite(v):
        return "—"
    if abs(v) >= 10000:
        return f"{v / 10000:,.1f} 万"
    return f"{v:,.0f} 元"


def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    """比率格式化：0.123 -> "12.3%"。输入既支持 0.123 也支持 12.3（自动识别）。"""
    if v is None or not np.isfinite(v):
        return "—"
    val = v * 100 if abs(v) <= 1.5 else v
    return f"{val:.{digits}f}%"


def _safe_name(name: str) -> str:
    """把图表名清洗成安全的文件名（去掉路径分隔符与空白）。"""
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(name)).strip("_")
    return s or "figure"


# =============================================================================
# 一、落盘工具（D5 统一返回结构 + C1 PNG 可选降级）
# =============================================================================

def save_figure(
    fig: go.Figure,
    name: str,
    assets_dir: Optional[str] = None,
    png_width: int = 1200,
    png_height: int = 640,
    try_png: bool = True,
) -> Dict[str, Any]:
    """
    把 Plotly Figure 落盘为交互式 HTML，并**尽力**导出静态 PNG。

    业务含义：这是所有图表唯一的出口。调用方拿到返回结构后，
    直接塞进 session.meta[x]['figure']，报告生成器再据此拼 Markdown 图片引用。

    PNG 降级逻辑（架构决策 C1）：
        本机 kaleido 未安装，`fig.write_image()` 必然抛异常。
        这里用 try-except 吞掉异常，只把原因记进 `png_error`，
        **绝不向上抛出** —— 图表工具失败会导致整个诊断流程中断，代价远大于少一张 PNG。

    Args:
        fig:        Plotly Figure 对象
        name:       图表名，同时作为文件名（如 "band_overlap"）
        assets_dir: 落盘目录；None 时用项目根下的 assets/
        png_width / png_height: PNG 导出尺寸（HTML 不受影响）
        try_png:    False 时跳过 PNG 导出（批量出图时可加速）

    Returns:
        dict，结构见本文件顶部「统一的图表返回结构」。
    """
    _ensure_assets_dir()
    target_dir = assets_dir or ASSETS_DIR
    os.makedirs(target_dir, exist_ok=True)

    base = _safe_name(name)
    html_path = os.path.join(target_dir, f"{base}.html")
    png_path = os.path.join(target_dir, f"{base}.png")

    # 1) 交互式 HTML：使用 CDN 版 plotly.js，产出的 div 体积小、可直接邮件发送
    #    （若用 include_plotlyjs=True 内嵌全量 plotly.js，单文件会有 3MB+）
    div = fig.to_html(
        include_plotlyjs="cdn",
        full_html=False,
        config=PLOTLY_CONFIG,
        div_id=f"div_{base}",
    )

    # 独立 HTML 文件必须自带 <meta charset="utf-8">。
    # 原因（实测坑）：中文 Windows 上双击打开无编码声明的 HTML 时，
    # Chrome/Edge 会回退到系统 locale 编码（GBK）解析 UTF-8 字节，
    # 图标题、轴标签、图例会全部变成乱码。注意 plotly 自带的
    # `<script charset="utf-8">` 只声明脚本编码，**不能替代**文档级 charset。
    title_txt = (str(base).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    doc = (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{title_txt}</title>\n'
        '<style>body{margin:16px;background:#fff;'
        "font-family:'Microsoft YaHei','SimHei',sans-serif;}</style>\n"
        "</head>\n<body>\n"
        f"{div}\n</body>\n</html>\n"
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(doc)

    # 2) 静态 PNG：可选降级，失败不影响主流程
    png_error: Optional[str] = None
    png_ok = False
    if try_png:
        try:
            fig.write_image(png_path, width=png_width, height=png_height, scale=2)
            png_ok = True
        except Exception as exc:  # noqa: BLE001 - kaleido 缺失/渲染引擎异常一律降级
            png_error = f"{type(exc).__name__}: {exc}"
            png_path_final: Optional[str] = None
        else:
            png_path_final = png_path
    else:
        png_path_final = None
        png_error = "已跳过 PNG 导出（try_png=False）"

    if try_png and not png_ok:
        png_path_final = None

    return {
        "name": base,
        "html_path": os.path.abspath(html_path),
        "png_path": os.path.abspath(png_path_final) if png_path_final else None,
        "div": div,
        "html_rel": os.path.relpath(os.path.abspath(html_path), PROJECT_ROOT).replace("\\", "/"),
        "png_rel": (
            os.path.relpath(os.path.abspath(png_path_final), PROJECT_ROOT).replace("\\", "/")
            if png_path_final else None
        ),
        "png_error": png_error,
    }


def strip_plotlyjs_cdn(div: str) -> str:
    """
    去掉 div 里的 plotly.js CDN <script> 标签。

    业务含义：报告 HTML 会把多张图的 div 拼进同一个页面，
    如果每个 div 都带一次 CDN 引用，浏览器会重复加载 3MB 的 plotly.js。
    拼装时只保留第一处，其余调用本函数剥离，由报告页面在 <head> 统一引入。
    """
    return re.sub(
        r'<script[^>]*src=["\']https://cdn\.plot\.ly/[^"\']+["\'][^>]*>\s*</script>',
        "",
        div or "",
    )


# =============================================================================
# 二、模块 2：薪酬带宽设计图
# =============================================================================

def band_overlap_chart(
    band_df: Union[pd.DataFrame, List[dict], Dict[str, Any]],
    title: str = "薪酬带宽区间与相邻职级重叠度",
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    设计出来的薪酬带宽长什么样？相邻职级的带宽重叠了多少？
    每个职级的「中位值」在哪，带宽幅度（上限/下限-1）是否合理？

    业务判读要点：
      - 重叠度 20%-40% 属健康区间：既能让高绩效低职级者薪酬高于低绩效高职级者
        （避免“晋升是唯一涨薪通道”），又不至于让职级失去区分度。
      - 重叠度 > 50%：职级形同虚设，晋升的薪酬激励不足。
      - 重叠度 < 10%：晋升即大幅涨薪，容易引发“熬年头等晋升”与人力成本跳变。
      - 竖线为中位值（CR 的分母），竖线间距即中位值级差（Midpoint Differential）。

    Args:
        band_df: 带宽表。至少含 level / band_min / band_mid / band_max 四列；
                 有 overlap_with_next / overlap_pct 时会在图上标注重叠度。
        title:   图标题

    Returns:
        go.Figure（未落盘，交给 save_figure 落盘）
    """
    df = _to_df(band_df)
    c_level = _pick_col(df, ["level", "职级", "薪级", "grade"])
    c_min = _pick_col(df, ["band_min", "下限", "带宽下限", "min", "薪酬下限"])
    c_mid = _pick_col(df, ["band_mid", "中位值", "带宽中位值", "mid", "midpoint", "中值"])
    c_max = _pick_col(df, ["band_max", "上限", "带宽上限", "max", "薪酬上限"])

    fig = go.Figure()

    if df.empty or not all([c_level, c_min, c_max]):
        # 数据缺失时不能抛错：给一张“无数据”占位图，保证报告排版不断裂
        _apply_base_layout(fig, title, "月薪（元）", "职级")
        fig.add_annotation(
            text="暂无带宽数据（前置步骤 generate_band 未执行）",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    # 按职级顺序排（P1 < P2 < ... < M3），保证“相邻职级”的语义正确
    df = df.copy()
    df["_sort"] = df[c_level].astype(str).map(lambda x: level_sort_key(x))
    df = df.sort_values("_sort").reset_index(drop=True)

    levels = df[c_level].astype(str).tolist()
    lo = _numeric(df[c_min])
    hi = _numeric(df[c_max])
    mid = _numeric(df[c_mid]) if c_mid else (lo + hi) / 2

    # y 轴用数值索引而非类别名：这样 add_shape 能精确定位到两条带之间的重叠区
    y_idx = list(range(len(levels)))
    spread = (hi - lo)  # 带宽绝对幅度（元）

    # --- 主图形：横向区间条（base=下限，长度=上限-下限） ---
    fig.add_trace(go.Bar(
        x=spread,
        y=y_idx,
        base=lo,
        orientation="h",
        width=0.55,
        marker=dict(
            color=COLOR_BLUE,
            opacity=0.85,
            line=dict(color="white", width=1.5),
        ),
        name="带宽区间（下限→上限）",
        text=[f"{lv}" for lv in levels],
        customdata=np.stack([
            lo.values, mid.values, hi.values,
            (spread / lo.replace(0, np.nan)).values,
        ], axis=-1),
        hovertemplate=(
            "职级 %{text}<br>"
            "下限 %{customdata[0]:,.0f} 元<br>"
            "中位值 %{customdata[1]:,.0f} 元<br>"
            "上限 %{customdata[2]:,.0f} 元<br>"
            "带宽幅度 %{customdata[3]:.1%}"
            "<extra></extra>"
        ),
    ))

    # --- 相邻职级重叠区着色（本图的核心视觉） ---
    # 重叠公式（WorldatWork 标准口径）：
    #   重叠度 = 相邻两级的带宽重叠区间宽度 / 两个职级带宽的平均宽度
    #   重叠区间 = [max(下限_i, 下限_j), min(上限_i, 上限_j)]，宽度为负则无重叠
    c_ov = _pick_col(df, ["overlap_with_next", "重叠度", "overlap", "overlap_pct"])
    n_overlap_drawn = 0
    for i in range(len(levels) - 1):
        lo_i, hi_i = lo.iloc[i], hi.iloc[i]
        lo_j, hi_j = lo.iloc[i + 1], hi.iloc[i + 1]
        if not all(np.isfinite(v) for v in (lo_i, hi_i, lo_j, hi_j)):
            continue
        ov_lo, ov_hi = max(lo_i, lo_j), min(hi_i, hi_j)
        if ov_hi <= ov_lo:
            continue  # 无重叠（带宽断裂），不画色块

        # 重叠度：优先用上游算好的值，否则现场按标准公式算
        ov_pct = None
        if c_ov:
            ov_pct = _clean_num(df[c_ov].iloc[i])
        if ov_pct is None:
            avg_width = ((hi_i - lo_i) + (hi_j - lo_j)) / 2
            ov_pct = (ov_hi - ov_lo) / avg_width if avg_width > 0 else None

        # 重叠度过高（>50%）标红警示，健康区间（20%-40%）用琥珀，过低用绿
        if ov_pct is None:
            fill = COLOR_AMBER
        elif ov_pct > 0.50:
            fill = COLOR_RED
        elif ov_pct < 0.10:
            fill = COLOR_GREEN
        else:
            fill = COLOR_AMBER

        fig.add_shape(
            type="rect",
            x0=ov_lo, x1=ov_hi,
            y0=y_idx[i] - 0.275, y1=y_idx[i + 1] + 0.275,
            fillcolor=fill, opacity=0.18,
            line=dict(width=0), layer="below",
        )
        n_overlap_drawn += 1
        if ov_pct is not None:
            fig.add_annotation(
                x=(ov_lo + ov_hi) / 2,
                y=(y_idx[i] + y_idx[i + 1]) / 2,
                text=f"重叠 {ov_pct:.0%}",
                showarrow=False,
                font=dict(size=10, color=COLOR_INK),
                bgcolor="rgba(255,255,255,0.75)",
                borderpad=2,
            )

    # --- 中位值竖线（CR 的分母所在位置，也是市场对标的锚点） ---
    for i, (m, lvl) in enumerate(zip(mid.tolist(), levels)):
        if not np.isfinite(m):
            continue
        fig.add_shape(
            type="line",
            x0=m, x1=m, y0=i - 0.30, y1=i + 0.30,
            line=dict(color=COLOR_INK, width=2.5),
            layer="above",
        )

    # 图例里补一条“中位值”示意（用散点画在坐标外，仅用于产生图例项）
    fig.add_trace(go.Scatter(
        x=[None], y=[None], mode="lines",
        line=dict(color=COLOR_INK, width=2.5),
        name="带宽中位值（CR 分母）",
    ))
    if n_overlap_drawn:
        fig.add_trace(go.Scatter(
            x=[None], y=[None], mode="markers",
            marker=dict(size=12, color=COLOR_AMBER, opacity=0.35, symbol="square"),
            name="相邻职级重叠区",
        ))

    height = max(380, 62 * len(levels) + 140)
    _apply_base_layout(
        fig, title,
        xaxis_title="月薪（元）",
        yaxis_title="职级",
        height=height,
    )
    fig.update_yaxes(tickmode="array", tickvals=y_idx, ticktext=levels, autorange="reversed")
    fig.update_xaxes(tickformat=",.0f", gridcolor="#EEEEEE")
    # x 轴留白，避免最左侧/最右侧的数值被裁掉
    if len(lo) and np.isfinite(lo.min()) and np.isfinite(hi.max()):
        pad = (hi.max() - lo.min()) * 0.06
        fig.update_xaxes(range=[max(0, lo.min() - pad), hi.max() + pad])
    return fig


# =============================================================================
# 三、模块 1：CR 分布与红绿圈
# =============================================================================

def cr_distribution_chart(
    diag_df: Union[pd.DataFrame, List[dict], Dict[str, Any], Sequence[float]],
    title: str = "薪酬比较比率（CR）分布与红绿圈判定",
    nbins: int = 28,
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    公司薪酬的内部公平性如何？有多少人薪酬偏高（红圈，成本溢出）、
    多少人偏低（绿圈，流失风险）？分布是集中还是两极分化？

    业务判读要点：
      - CR = 个人薪资 / 职级带宽中位值，1.00 表示正好在中位值。
      - CR > 1.20 为红圈：薪酬高于带宽，通常是司龄长/历史遗留高薪/快速晋升导致。
        处理方式一般是冻结涨薪或发一次性补贴，不占用年度调薪池。
      - CR < 0.80 为绿圈：薪酬低于带宽，是**离职高发区**，应优先补涨。
      - 健康的分布形态：单峰、集中在 0.9-1.1，红绿圈合计不超过 25%。

    Args:
        diag_df: 诊断明细表（含 CR 列），也可直接传 CR 数值序列。
        title:   图标题
        nbins:   直方图分箱数

    Returns:
        go.Figure（未落盘）
    """
    # 允许直接传数值序列（上游常直接给 cr 列表）
    if isinstance(diag_df, (list, tuple)) and (len(diag_df) == 0 or not isinstance(diag_df[0], dict)):
        cr = _numeric(pd.Series(list(diag_df)))
    elif isinstance(diag_df, (pd.Series, np.ndarray)):
        cr = _numeric(pd.Series(diag_df))
    else:
        df = _to_df(diag_df)
        c_cr = _pick_col(df, ["cr", "compa_ratio", "CR", "cr_value", "CR值", "比较比率", "compa-ratio"])
        if c_cr is None:
            c_cr = _pick_col(df, ["cr"])
        cr = _numeric(df[c_cr]) if c_cr else _numeric(pd.Series([], dtype=float))

    cr = cr.dropna()
    # 极端值截尾：CR > 3 的样本会严重拉伸 x 轴，但不删数据（只限显示范围）
    cr = cr[(cr > 0)]

    fig = go.Figure()
    if cr.empty:
        _apply_base_layout(fig, title, "CR（个人薪资 / 带宽中位值）", "人数")
        fig.add_annotation(
            text="暂无 CR 数据（前置步骤 analyze_current_state 未执行）",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    hi = float(min(cr.max() * 1.05, max(RED_CIRCLE_CR * 1.6, cr.quantile(0.99) * 1.1)))
    lo = float(max(0.0, min(cr.min() * 0.95, GREEN_CIRCLE_CR * 0.6)))
    counts, edges = np.histogram(cr, bins=nbins, range=(lo, hi))
    centers = (edges[:-1] + edges[1:]) / 2
    widths = np.diff(edges) * 0.92

    # 逐柱染色：绿圈区间染绿、红圈区间染红、合理区间染蓝 —— 一眼看出结构
    colors = [
        COLOR_GREEN if c < GREEN_CIRCLE_CR else (COLOR_RED if c > RED_CIRCLE_CR else COLOR_BLUE)
        for c in centers
    ]

    fig.add_trace(go.Bar(
        x=centers, y=counts, width=widths,
        marker=dict(color=colors, line=dict(color="white", width=1)),
        name="员工人数",
        hovertemplate="CR 区间 %{x:.2f}<br>人数 %{y:.0f} 人<extra></extra>",
        showlegend=False,
    ))

    # 红绿圈背景区（layer='below' 压在柱子下面，不遮挡数据）
    fig.add_vrect(
        x0=0, x1=GREEN_CIRCLE_CR,
        fillcolor=COLOR_GREEN, opacity=0.07, line_width=0, layer="below",
        annotation_text="绿圈区 CR<0.80", annotation_position="top left",
        annotation=dict(font=dict(size=11, color=COLOR_GREEN)),
    )
    fig.add_vrect(
        x0=RED_CIRCLE_CR, x1=hi,
        fillcolor=COLOR_RED, opacity=0.07, line_width=0, layer="below",
        annotation_text="红圈区 CR>1.20", annotation_position="top right",
        annotation=dict(font=dict(size=11, color=COLOR_RED)),
    )

    # 三条阈值竖线：绿圈上限 / 中位值基准 / 红圈下限
    for x, color, label in [
        (GREEN_CIRCLE_CR, COLOR_GREEN, f"绿圈线 {GREEN_CIRCLE_CR:.2f}"),
        (1.00, COLOR_GREY_DARK, "中位值 1.00"),
        (RED_CIRCLE_CR, COLOR_RED, f"红圈线 {RED_CIRCLE_CR:.2f}"),
    ]:
        fig.add_vline(
            x=x, line=dict(color=color, width=1.8, dash="dash"),
            annotation_text=label,
            annotation_position="top",
            annotation_font=dict(size=11, color=color),
        )

    # 统计摘要放副标题：HR 不用看图就能拿到三个关键数
    n = len(cr)
    n_red = int((cr > RED_CIRCLE_CR).sum())
    n_green = int((cr < GREEN_CIRCLE_CR).sum())
    subtitle = (
        f"样本 {n} 人 ｜ 红圈 {n_red} 人（{n_red / n:.1%}）"
        f" ｜ 绿圈 {n_green} 人（{n_green / n:.1%}）"
        f" ｜ CR 中位数 {cr.median():.2f} ｜ 均值 {cr.mean():.2f}"
    )
    _apply_base_layout(fig, title, "CR（个人薪资 / 带宽中位值）", "人数", height=480)
    fig.add_annotation(
        text=subtitle, xref="paper", yref="paper",
        x=0.01, y=1.10, xanchor="left", showarrow=False,
        font=dict(size=12, color=COLOR_GREY_DARK),
    )
    fig.update_xaxes(range=[lo, hi], tickformat=".2f", gridcolor="#EEEEEE")
    fig.update_yaxes(gridcolor="#EEEEEE")
    return fig


# =============================================================================
# 四、模块 4：调薪前后 CR 分布对比
# =============================================================================

def cr_before_after_chart(
    before: Union[pd.DataFrame, List[dict], Dict[str, Any], Sequence[float]],
    after: Union[pd.DataFrame, List[dict], Dict[str, Any], Sequence[float]],
    title: str = "调薪前后 CR 分布对比",
    strategy_label: str = "推荐方案",
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    这一版调薪方案到底有没有解决内部公平性问题？
    调薪后红圈/绿圈人数各减少了多少人？CR 分布是更集中了还是更分散了？

    业务判读要点：
      - 看**绿圈区（左侧）面积的收缩**：收缩越多，说明低薪人群被补涨得越充分，
        这是调薪预算最该花的地方（绿圈是离职高发区）。
      - 看**红圈区（右侧）面积的收缩**：红圈减少意味着成本溢出收敛。
      - 看**分布是否向 1.00 收敛**：CR 标准差下降 = 内部公平性改善。
      - 若调薪后分布只是整体右移（均值涨了但形状没变），说明钱“撒胡椒面”了，
        没有解决结构性不公平 —— 这是最常见也最该避免的调薪失败形态。

    Args:
        before: 调薪前 CR 数据（DataFrame 含 cr 列 / dict 含 cr 键 / 数值序列）
        after:  调薪后 CR 数据，形态同上
        title:  图标题
        strategy_label: 图例中“调薪后”序列的名字（可填具体策略名）

    Returns:
        go.Figure（未落盘）
    """
    def _extract(x) -> pd.Series:
        """从多种输入形态里取出 CR 数值序列。"""
        if x is None:
            return pd.Series([], dtype=float)
        if isinstance(x, (pd.Series, np.ndarray)):
            return _numeric(pd.Series(x))
        if isinstance(x, (list, tuple)):
            if len(x) == 0 or not isinstance(x[0], dict):
                return _numeric(pd.Series(list(x)))
            return _extract(pd.DataFrame(list(x)))
        if isinstance(x, dict):
            # {"cr": [...]} / {"before": {...}} / {"cr_list": [...]} 都兼容
            for k in ("cr", "compa_ratio", "CR", "cr_values", "cr_list", "values"):
                if k in x:
                    return _numeric(pd.Series(x[k]))
            return _numeric(pd.Series(x))
        df = _to_df(x)
        c = _pick_col(df, ["cr", "compa_ratio", "CR", "cr_value", "CR值", "调整后CR", "调整前CR"])
        return _numeric(df[c]) if c else pd.Series([], dtype=float)

    b = _extract(before).dropna()
    a = _extract(after).dropna()
    b = b[b > 0]
    a = a[a > 0]

    fig = go.Figure()
    if b.empty and a.empty:
        _apply_base_layout(fig, title, "CR", "人数")
        fig.add_annotation(
            text="暂无调薪前后 CR 数据（前置步骤 simulate_increase 未执行）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    # 两组共用同一套分箱，否则前后对比的柱子对不齐，视觉上无法比较
    allv = pd.concat([b, a])
    lo = float(max(0.0, min(allv.min() * 0.95, GREEN_CIRCLE_CR * 0.6)))
    hi = float(min(allv.max() * 1.05, max(RED_CIRCLE_CR * 1.6, allv.quantile(0.99) * 1.1)))
    bins = dict(start=lo, end=hi, size=(hi - lo) / 26.0)

    fig.add_trace(go.Histogram(
        x=b, xbins=bins, name="调薪前",
        marker=dict(color=COLOR_GREY, opacity=0.55, line=dict(color="white", width=1)),
        hovertemplate="CR %{x:.2f}<br>调薪前 %{y:.0f} 人<extra></extra>",
    ))
    fig.add_trace(go.Histogram(
        x=a, xbins=bins, name=f"调薪后（{strategy_label}）",
        marker=dict(color=COLOR_BLUE, opacity=0.75, line=dict(color="white", width=1)),
        hovertemplate="CR %{x:.2f}<br>调薪后 %{y:.0f} 人<extra></extra>",
    ))

    fig.update_layout(barmode="overlay")   # 叠加模式：两组柱子共用 x 轴位置便于对比

    fig.add_vrect(x0=0, x1=GREEN_CIRCLE_CR, fillcolor=COLOR_GREEN, opacity=0.06,
                  line_width=0, layer="below")
    fig.add_vrect(x0=RED_CIRCLE_CR, x1=hi, fillcolor=COLOR_RED, opacity=0.06,
                  line_width=0, layer="below")
    for x, color, label in [
        (GREEN_CIRCLE_CR, COLOR_GREEN, f"绿圈线 {GREEN_CIRCLE_CR:.2f}"),
        (1.00, COLOR_GREY_DARK, "中位值 1.00"),
        (RED_CIRCLE_CR, COLOR_RED, f"红圈线 {RED_CIRCLE_CR:.2f}"),
    ]:
        fig.add_vline(x=x, line=dict(color=color, width=1.6, dash="dash"),
                      annotation_text=label, annotation_position="top",
                      annotation_font=dict(size=11, color=color))

    # 副标题：直接给出红绿圈人数的前后变化（这是 HR 最关心的结论）
    parts = []
    if not b.empty and not a.empty:
        parts.append(f"绿圈 {int((b < GREEN_CIRCLE_CR).sum())} → {int((a < GREEN_CIRCLE_CR).sum())} 人")
        parts.append(f"红圈 {int((b > RED_CIRCLE_CR).sum())} → {int((a > RED_CIRCLE_CR).sum())} 人")
        parts.append(f"CR 标准差 {b.std():.3f} → {a.std():.3f}（越小越公平）")
        parts.append(f"CR 均值 {b.mean():.2f} → {a.mean():.2f}")
    subtitle = " ｜ ".join(parts)

    _apply_base_layout(fig, title, "CR（个人薪资 / 带宽中位值）", "人数", height=480,
                       legend_title="")
    if subtitle:
        fig.add_annotation(
            text=subtitle, xref="paper", yref="paper",
            x=0.01, y=1.10, xanchor="left", showarrow=False,
            font=dict(size=12, color=COLOR_GREY_DARK),
        )
    fig.update_xaxes(range=[lo, hi], tickformat=".2f", gridcolor="#EEEEEE")
    fig.update_yaxes(gridcolor="#EEEEEE")
    return fig


# =============================================================================
# 五、模块 4：调薪策略成本对比
# =============================================================================

def strategy_cost_chart(
    strategies: Union[pd.DataFrame, List[dict], Dict[str, Any]],
    title: str = "各调薪策略总成本对比",
    budget: Optional[float] = None,
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    同样的预算下，几种调薪策略各要花多少钱？哪个方案性价比最高（花最少的钱，
    把最多绿圈员工补进带宽）？预算够不够？

    业务判读要点：
      - 柱高 = 该策略的年度调薪总成本（元）。
      - 高于均值的柱染**红**（成本偏高，中国语境红=高），低于均值的染**绿**。
      - 虚线是预算上限；柱子超过虚线说明方案超预算，需要砍或分期。
      - 推荐方案用琥珀色边框 + ★ 标注，但**推荐不等于最便宜** ——
        最便宜的往往是平均分配（A 策略），它解决不了内部公平性；
        推荐方案通常是 B（优先补绿圈）或 C（绩效加权）。

    Args:
        strategies: 策略对比表。每行一条策略，需含名称与成本两列；
                    列名容错：策略名认 strategy/name/方案/策略，
                    成本认 total_cost/cost/总成本/annual_cost/调薪成本。
        title:  图标题
        budget: 预算上限（元），给定时画一条红色虚线

    Returns:
        go.Figure（未落盘）
    """
    df = _to_df(strategies)
    if df.empty:
        fig = _apply_base_layout(go.Figure(), title, "策略", "年度调薪总成本（元）")
        fig.add_annotation(
            text="暂无调薪策略数据（前置步骤 simulate_increase 未执行）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    c_name = _pick_col(df, ["strategy", "name", "label", "方案", "策略", "策略名称"])
    c_cost = _pick_col(df, [
        "total_cost", "cost", "annual_cost", "total_increase_cost",
        "总成本", "调薪总成本", "年度调薪成本", "年度成本", "value",
    ])
    c_pct = _pick_col(df, ["cost_pct", "budget_pct", "占比", "成本占比", "调薪率"])
    c_rec = _pick_col(df, ["recommended", "is_recommended", "推荐", "是否推荐"])
    c_red = _pick_col(df, ["red_after", "red_count_after", "调薪后红圈人数"])
    c_green = _pick_col(df, ["green_after", "green_count_after", "调薪后绿圈人数"])

    if c_name is None:
        df = df.reset_index().rename(columns={"index": "_idx"})
        c_name = "_idx"
    if c_cost is None:
        # 没有成本列时，退化为取第一个数值列，尽量不放弃出图
        num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        if not num_cols:
            fig = _apply_base_layout(go.Figure(), title, "策略", "年度调薪总成本（元）")
            fig.add_annotation(text="策略数据中未找到成本列", xref="paper", yref="paper",
                               x=0.5, y=0.5, showarrow=False,
                               font=dict(size=14, color=COLOR_GREY))
            return fig
        c_cost = num_cols[0]

    names = df[c_name].astype(str).tolist()
    costs = _numeric(df[c_cost]).fillna(0.0)
    pcts = _numeric(df[c_pct]) if c_pct else None
    mean_cost = float(costs.mean()) if len(costs) else 0.0

    # 中国语境：成本高于均值=红，低于均值=绿
    colors = [COLOR_RED if v > mean_cost else COLOR_GREEN for v in costs]

    # 推荐方案：优先读 recommended 标记，没有则取“绿圈减少最多”的兜底
    rec_idx = None
    if c_rec:
        flags = df[c_rec].astype(str).str.lower().isin(["true", "1", "yes", "y", "是"])
        if flags.any():
            rec_idx = int(flags.values.argmax())

    line_colors = [COLOR_AMBER if i == rec_idx else "rgba(0,0,0,0)" for i in range(len(names))]
    line_widths = [3.0 if i == rec_idx else 0.0 for i in range(len(names))]

    # hover 里带上红绿圈改善情况，方便 HR 对比“花钱买到什么”
    hover_extra = []
    for i in range(len(df)):
        bits = []
        if c_red:
            v = _clean_num(df[c_red].iloc[i])
            if v is not None:
                bits.append(f"调薪后红圈 {v:.0f} 人")
        if c_green:
            v = _clean_num(df[c_green].iloc[i])
            if v is not None:
                bits.append(f"调薪后绿圈 {v:.0f} 人")
        if pcts is not None:
            v = _clean_num(pcts.iloc[i])
            if v is not None:
                bits.append(f"占薪资总额 {_fmt_pct(v)}")
        hover_extra.append("<br>".join(bits))

    text_labels = []
    for i, v in enumerate(costs):
        label = _fmt_money(float(v))
        if i == rec_idx:
            label = "★ " + label
        text_labels.append(label)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=names, y=costs,
        marker=dict(
            color=colors, opacity=0.88,
            line=dict(color=line_colors, width=line_widths),
        ),
        text=text_labels,
        textposition="outside",
        textfont=dict(size=12, color=COLOR_INK),
        customdata=hover_extra,
        hovertemplate="%{x}<br>年度总成本 %{y:,.0f} 元<br>%{customdata}<extra></extra>",
        name="年度调薪总成本",
        showlegend=False,
    ))

    # 预算线：超过即超支，红虚线警示
    if budget and np.isfinite(budget):
        fig.add_hline(
            y=float(budget),
            line=dict(color=COLOR_RED, width=2, dash="dash"),
            annotation_text=f"预算上限 {_fmt_money(float(budget))}",
            annotation_position="top right",
            annotation_font=dict(size=12, color=COLOR_RED),
        )

    # 图例项：用两个空散点补出“高于/低于均值”的配色说明
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                             marker=dict(size=11, color=COLOR_RED, symbol="square"),
                             name="高于平均成本（红=偏高）"))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                             marker=dict(size=11, color=COLOR_GREEN, symbol="square"),
                             name="低于平均成本（绿=偏低）"))
    if rec_idx is not None:
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                                 marker=dict(size=11, color="rgba(0,0,0,0)",
                                             line=dict(color=COLOR_AMBER, width=3), symbol="square"),
                                 name="★ 推荐方案"))

    y_max = float(max(costs.max() if len(costs) else 0, budget or 0))
    _apply_base_layout(fig, title, "调薪策略", "年度调薪总成本（元）", height=480)
    fig.update_yaxes(tickformat=",.0f", gridcolor="#EEEEEE",
                     range=[0, y_max * 1.18 if y_max > 0 else 1])
    return fig


# =============================================================================
# 六、模块 3：市场对标差距
# =============================================================================

def market_gap_chart(
    bench_df: Union[pd.DataFrame, List[dict], Dict[str, Any]],
    title: str = "公司薪资水平 vs 市场分位值（P25/P50/P75）",
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    公司每个职级的薪酬，在市场上到底站在什么位置？
    是全面落后，还是“基层高于市场、核心岗低于市场”的结构性错配？

    业务判读要点：
      - 蓝柱 = 公司该职级薪资中位数；三根灰柱 = 市场 P25/P50/P75。
      - 折线 = 公司中位数相对市场 P50 的差距百分比（右轴）。
        中国语境：**为正（高于市场）染红，为负（低于市场）染绿**。
      - 最危险的信号是“核心岗/高职级反而低于市场”：
        高职级员工市场流动性最强，一旦低于 P50，流失的是最难替代的人。
      - 反之，操作类/可替代岗位高于 P75 则是成本浪费，属于要收敛的部分。

    Args:
        bench_df: 对标表。需含 level 与 公司中位数、市场 P25/P50/P75；
                  公司列认 company_median/company/公司中位数/中位数/median_salary，
                  市场列认 mkt_p25/mkt_p50/mkt_p75 或 市场P25/市场P50/市场P75。
        title: 图标题

    Returns:
        go.Figure（未落盘）
    """
    df = _to_df(bench_df)
    c_level = _pick_col(df, ["level", "职级", "薪级", "grade", "job_title", "岗位"])
    c_comp = _pick_col(df, [
        "company_median", "company_p50", "company", "current_median",
        "公司中位数", "公司薪资中位数", "公司中位值", "中位数", "median", "median_salary",
    ])
    c_p25 = _pick_col(df, ["mkt_p25", "市场P25", "市场25分位", "p25", "market_p25"])
    c_p50 = _pick_col(df, ["mkt_p50", "市场P50", "市场50分位", "p50", "market_p50"])
    c_p75 = _pick_col(df, ["mkt_p75", "市场P75", "市场75分位", "p75", "market_p75"])
    c_gap = _pick_col(df, ["gap_p50_pct", "gap_pct", "gap", "差距", "市场差距", "gap_vs_p50"])

    fig = go.Figure()
    if df.empty or c_level is None or c_comp is None:
        _apply_base_layout(fig, title, "职级", "月薪（元）")
        fig.add_annotation(
            text="暂无市场对标数据（前置步骤 market_benchmark 未执行或数据缺市场分位）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    df = df.copy()
    df["_sort"] = df[c_level].astype(str).map(lambda x: level_sort_key(x))
    df = df.sort_values("_sort").reset_index(drop=True)

    levels = df[c_level].astype(str).tolist()
    comp = _numeric(df[c_comp])
    p25 = _numeric(df[c_p25]) if c_p25 else pd.Series([np.nan] * len(df))
    p50 = _numeric(df[c_p50]) if c_p50 else pd.Series([np.nan] * len(df))
    p75 = _numeric(df[c_p75]) if c_p75 else pd.Series([np.nan] * len(df))

    # 差距%：优先用上游算好的；缺失则现场算 (公司 - P50) / P50
    if c_gap:
        gap = _numeric(df[c_gap])
        # 上游可能给的是 0.12（比例）或 12（百分数），归一到百分数
        gap = gap.apply(lambda v: v * 100 if (np.isfinite(v) and abs(v) <= 1.5) else v)
    else:
        gap = (comp - p50) / p50.replace(0, np.nan) * 100

    # 柱状：公司 + 三个市场分位（灰阶递进，突出公司这一根蓝柱）
    fig.add_trace(go.Bar(
        x=levels, y=comp, name="公司中位数",
        marker=dict(color=COLOR_BLUE, opacity=0.9, line=dict(color="white", width=1)),
        hovertemplate="%{x}<br>公司中位数 %{y:,.0f} 元<extra></extra>",
    ))
    if p25.notna().any():
        fig.add_trace(go.Bar(
            x=levels, y=p25, name="市场 P25",
            marker=dict(color=COLOR_GREY_LIGHT, opacity=0.95, line=dict(color="white", width=1)),
            hovertemplate="%{x}<br>市场 P25 %{y:,.0f} 元<extra></extra>",
        ))
    if p50.notna().any():
        fig.add_trace(go.Bar(
            x=levels, y=p50, name="市场 P50（对标锚点）",
            marker=dict(color=COLOR_GREY, opacity=0.95, line=dict(color="white", width=1)),
            hovertemplate="%{x}<br>市场 P50 %{y:,.0f} 元<extra></extra>",
        ))
    if p75.notna().any():
        fig.add_trace(go.Bar(
            x=levels, y=p75, name="市场 P75",
            marker=dict(color=COLOR_GREY_DARK, opacity=0.95, line=dict(color="white", width=1)),
            hovertemplate="%{x}<br>市场 P75 %{y:,.0f} 元<extra></extra>",
        ))

    # 折线（右轴）：相对 P50 的差距%
    if gap.notna().any():
        colors = [COLOR_RED if (np.isfinite(v) and v > 0) else COLOR_GREEN for v in gap]
        fig.add_trace(go.Scatter(
            x=levels, y=gap, mode="lines+markers+text", yaxis="y2",
            name="公司 vs 市场P50 差距",
            line=dict(color=COLOR_AMBER, width=2.5),
            marker=dict(size=9, color=colors, line=dict(color="white", width=1.5)),
            text=[f"{v:+.1f}%" if np.isfinite(v) else "" for v in gap],
            textposition="top center",
            textfont=dict(size=11),
            hovertemplate="%{x}<br>相对市场 P50 %{y:+.1f}%<extra></extra>",
        ))
        fig.add_hline(y=0, yref="y2", line=dict(color=COLOR_GREY, width=1, dash="dot"))

    _apply_base_layout(fig, title, "职级", "月薪（元）", height=500)
    fig.update_layout(
        barmode="group",
        bargap=0.28,
        yaxis2=dict(
            title=dict(text="相对市场 P50 差距（%）", font=dict(size=13, color=COLOR_AMBER)),
            overlaying="y", side="right", showgrid=False,
            zeroline=False, tickfont=dict(color=COLOR_AMBER),
        ),
    )
    fig.update_yaxes(tickformat=",.0f", gridcolor="#EEEEEE")
    fig.update_xaxes(gridcolor="#FFFFFF")
    return fig


# =============================================================================
# 七、模块 5：固浮比与激励曲线
# =============================================================================

def paymix_curve_chart(
    curves: Union[pd.DataFrame, List[dict], Dict[str, Any]],
    title: str = "各岗位序列：业绩达成率 → 实际总收入",
    baseline_income: Optional[float] = None,
) -> go.Figure:
    """
    【这张图回答什么业务问题】
    如果把固定浮动比从 70:30 改成 40:60，销售/技术/职能序列的员工
    在不同业绩达成率下的实际总收入会怎样变化？激励强度够不够？风险在哪？

    业务判读要点：
      - 横轴 = 业绩达成率（0%-150%），纵轴 = 年度实际总收入
        （实际总收入 = 固定薪 + 浮动薪 × 达成率）。
      - **曲线斜率 = 激励强度**。浮动占比越高，曲线越陡，激励越强，
        但达成率低于 100% 时收入下滑也越快 —— 这是员工承担的风险。
      - 100% 达成率处画竖线（目标线）：曲线在 100% 处的取值即“目标总现金”，
        设计时应让所有序列在 100% 处的收入与市场对标一致，
        差异只应体现在斜率上（而不是把低浮动序列的目标收入压低）。
      - ⚠️ **高浮动比例的前提条件**：业绩可量化、可归因到个人、结算周期短。
        长周期协作型业务（如研发、职能）强行高浮动会破坏协作 ——
        这条前提不满足时，图上再漂亮的曲线落地都会失败。

    Args:
        curves: 曲线数据，三种形态都接受：
                1. 长表 DataFrame / list[dict]：列含 job_family / achievement / total_income
                2. 嵌套 dict：{序列名: {"achievement": [...], "total_income": [...]}}
                3. 嵌套 dict：{序列名: {达成率: 收入}}（键为达成率数值或 "80%"）
        title: 图标题
        baseline_income: 目标总现金基准线（达成率 100% 的收入），给定时画水平参考线

    Returns:
        go.Figure（未落盘）
    """
    # ---- 把三种输入形态统一成长表 ----
    rows: List[dict] = []
    if isinstance(curves, dict):
        for fam, payload in curves.items():
            if isinstance(payload, dict):
                # 形态 2：{"achievement": [...], "total_income": [...]}
                ach = None
                inc = None
                for k, v in payload.items():
                    nk = str(k).strip().lower()
                    if nk in ("achievement", "达成率", "achieve", "x", "rate"):
                        ach = v
                    elif nk in ("total_income", "income", "总收入", "实际总收入", "y", "total"):
                        inc = v
                if ach is not None and inc is not None:
                    for a, t in zip(list(ach), list(inc)):
                        rows.append({"job_family": fam, "achievement": a, "total_income": t})
                else:
                    # 形态 3：{"80": 12345, "100": 15000} —— 键是达成率
                    for k, v in payload.items():
                        try:
                            a = float(str(k).replace("%", "").strip())
                        except (TypeError, ValueError):
                            continue
                        rows.append({"job_family": fam, "achievement": a, "total_income": v})
            elif isinstance(payload, (list, tuple)):
                for item in payload:
                    if isinstance(item, dict):
                        d = dict(item)
                        d.setdefault("job_family", fam)
                        rows.append(d)
    else:
        rows = _to_df(curves).to_dict("records")

    df = pd.DataFrame(rows)
    fig = go.Figure()

    if df.empty:
        _apply_base_layout(fig, title, "业绩达成率（%）", "年度实际总收入（元）")
        fig.add_annotation(
            text="暂无固浮比曲线数据（前置步骤 simulate_pay_mix 未执行）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=14, color=COLOR_GREY),
        )
        return fig

    c_fam = _pick_col(df, ["job_family", "family", "序列", "岗位序列", "职族"])
    c_ach = _pick_col(df, ["achievement", "达成率", "achieve", "rate", "x", "业绩达成率"])
    c_inc = _pick_col(df, ["total_income", "income", "实际总收入", "总收入", "y", "total", "年度总收入"])
    c_mix = _pick_col(df, ["pay_mix", "mix", "固浮比", "target_mix"])

    if c_fam is None or c_ach is None or c_inc is None:
        _apply_base_layout(fig, title, "业绩达成率（%）", "年度实际总收入（元）")
        fig.add_annotation(
            text=f"固浮比曲线缺少必需列（需要 序列/达成率/总收入，实到 {list(df.columns)}）",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=13, color=COLOR_RED),
        )
        return fig

    df["_ach"] = _numeric(df[c_ach])
    df["_inc"] = _numeric(df[c_inc])
    # 达成率可能是 0-1 的小数，统一放大到百分数显示
    if df["_ach"].notna().any() and df["_ach"].max() <= 1.5:
        df["_ach"] = df["_ach"] * 100
    df = df.dropna(subset=["_ach", "_inc"]).sort_values("_ach")

    families = list(dict.fromkeys(df[c_fam].astype(str).tolist()))

    for i, fam in enumerate(families):
        sub = df[df[c_fam].astype(str) == fam]
        color = QUAL_COLORS[i % len(QUAL_COLORS)]
        # 图例里带上固浮比：优先用数据里的，缺失时回落到 schemas 的目标固浮比基准
        name = fam
        mixes = []
        if c_mix and c_mix in sub.columns:
            mixes = [str(v) for v in sub[c_mix].dropna().unique()]
        if not mixes:
            mix = JOB_FAMILY_PAY_MIX.get(fam)
            if mix:
                mixes = [f"{mix[0]}:{mix[1]}"]
        if mixes:
            name = f"{fam}（固浮 {mixes[0]}）"
        fig.add_trace(go.Scatter(
            x=sub["_ach"], y=sub["_inc"],
            mode="lines+markers",
            name=name,
            line=dict(color=color, width=2.8),
            marker=dict(size=6, color=color),
            hovertemplate="%{fullData.name}<br>达成率 %{x:.0f}%<br>实际总收入 %{y:,.0f} 元<extra></extra>",
        ))

    # 100% 达成率竖线：所有序列在这一点的收入应等于“目标总现金”
    fig.add_vline(
        x=100, line=dict(color=COLOR_GREY_DARK, width=1.8, dash="dash"),
        annotation_text="目标达成 100%", annotation_position="top",
        annotation_font=dict(size=12, color=COLOR_GREY_DARK),
    )

    # 目标总现金基准线（若上游给了）
    if baseline_income and np.isfinite(baseline_income):
        fig.add_hline(
            y=float(baseline_income),
            line=dict(color=COLOR_AMBER, width=1.8, dash="dot"),
            annotation_text=f"目标总现金 {_fmt_money(float(baseline_income))}",
            annotation_position="bottom right",
            annotation_font=dict(size=12, color=COLOR_AMBER),
        )
    else:
        # 未给基准时，用 100% 处的收入均值画一条参考线
        at100 = df[(df["_ach"] >= 99) & (df["_ach"] <= 101)]["_inc"]
        if not at100.empty:
            base = float(at100.mean())
            fig.add_hline(
                y=base, line=dict(color=COLOR_AMBER, width=1.5, dash="dot"),
                annotation_text=f"100% 达成处均值 {_fmt_money(base)}",
                annotation_position="bottom right",
                annotation_font=dict(size=11, color=COLOR_AMBER),
            )

    _apply_base_layout(fig, title, "业绩达成率（%）", "年度实际总收入（元）", height=520,
                       legend_title="岗位序列")
    fig.update_xaxes(ticksuffix="%", gridcolor="#EEEEEE", range=[0, 155])
    fig.update_yaxes(tickformat=",.0f", gridcolor="#EEEEEE")
    return fig


# =============================================================================
# 八、批量出图（报告生成器用）
# =============================================================================

def build_all_charts(
    meta: Dict[str, Any],
    prefix: str = "",
    try_png: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    从 session.meta 里能拿到的所有数据，一次性生成全量图表。

    业务含义：报告生成器需要保证“只要有数据就有图”。
    上游工具可能没落图（或落图失败），这里做一次兜底补画，
    确保报告不会出现“章节有数据但没配图”的尴尬。

    Args:
        meta:     session.meta 全量字典
        prefix:   文件名前缀（多会话并存时用于隔离，如 "s123_"）
        try_png:  是否尝试导出 PNG（kaleido 缺失时会失败并降级）

    Returns:
        {图表名: save_figure 返回结构}，只含成功生成的图表
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(meta, dict):
        return out

    def _save(fig: go.Figure, name: str):
        try:
            out[name] = save_figure(fig, f"{prefix}{name}", try_png=try_png)
        except Exception as exc:  # noqa: BLE001 - 单图失败不影响其余图表
            out[name] = {"name": name, "html_path": None, "png_path": None,
                         "div": None, "html_rel": None, "png_rel": None,
                         "png_error": f"图表生成失败：{type(exc).__name__}: {exc}"}

    band = meta.get("band") or {}
    if isinstance(band, dict):
        tbl = band.get("band_table") or band.get("bands") or band.get("table")
        if tbl:
            _save(band_overlap_chart(tbl), "band_overlap")

    cur = meta.get("current_state") or {}
    if isinstance(cur, dict):
        cr_data = cur.get("cr_values") or cur.get("cr_list") or cur.get("diagnose_df") or cur.get("table")
        if cr_data is not None:
            _save(cr_distribution_chart(cr_data), "cr_distribution")

    mkt = meta.get("market_benchmark") or {}
    if isinstance(mkt, dict):
        tbl = mkt.get("by_level") or mkt.get("table") or mkt.get("bench_table")
        if tbl:
            _save(market_gap_chart(tbl), "market_gap")

    inc = meta.get("increase_sim") or {}
    if isinstance(inc, dict):
        strategies = inc.get("strategies") or inc.get("compare") or inc.get("table")
        if strategies:
            budget = inc.get("budget") or inc.get("budget_amount")
            _save(strategy_cost_chart(strategies, budget=budget), "strategy_cost")
        before = inc.get("before") or inc.get("cr_before")
        after = inc.get("after") or inc.get("cr_after")
        if before is not None and after is not None:
            _save(cr_before_after_chart(before, after), "cr_before_after")

    pm = meta.get("pay_mix") or {}
    if isinstance(pm, dict):
        curves = pm.get("curves") or pm.get("curve_data") or pm.get("table")
        if curves:
            _save(paymix_curve_chart(curves), "paymix_curve")

    return out


# =============================================================================
# 九、模块自检（python -m tools.charts 或 python charts.py）
# =============================================================================

if __name__ == "__main__":  # pragma: no cover
    print("charts.py 自检：本模块不产生业务数据，请用 tests/test_charts_report.py 做端到端验证。")
    print(f"图表输出目录：{ASSETS_DIR}")
    print(f"报告输出目录：{REPORT_DIR}")

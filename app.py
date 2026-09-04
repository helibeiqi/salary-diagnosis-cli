# coding: utf-8
"""
app.py — salary-diagnosis-cli 的 Streamlit Web UI
================================================================================
本地运行的网页界面，让不懂命令行的同事也能用：

  * 一键演示：内置合成模拟数据，零交互跑通完整诊断链（外发零风险）。
  * 诊断我的工资表：上传 .csv/.xlsx → 自动映射 → 生成报告 → 网页预览 + 下载。

设计原则（与 CLI 一致）：
  - 全部计算在本机完成，薪酬数据**不出内网**（绝不回传任何云端）。
  - 不重新实现计算逻辑，只复用 `src.main.main(argv)` 编程式调用底层诊断链。
  - 真实数据报告仅限本地查看，页面明确提示「切勿外发」。

启动：  streamlit run app.py
依赖：  pip install streamlit   （或 pip install -e ".[web]"）
"""
from __future__ import annotations

import glob
import io
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Windows 控制台默认 GBK，中文摘要会直接抛 UnicodeEncodeError（同 run_agent.py）。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

import streamlit as st
from src.main import main

REPORT_DIR = os.path.join(PROJECT_ROOT, "report")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def latest_report(ext: str) -> str | None:
    """取 report/ 下最新生成的诊断报告（按修改时间）。

    排除 *.data_guard.md 安全护栏 sidecar：它与真实报告同秒写入、mtime 更晚，
    若纳入会顶掉真实报告（数据护栏 sidecar 不是报告主体）。
    同时兼容「薪酬诊断报告_*」与「模拟数据诊断_*」两种命名前缀。
    """
    patterns = [f"薪酬诊断报告_*{ext}", f"模拟数据诊断_*{ext}"]
    files = []
    for pat in patterns:
        files += glob.glob(os.path.join(REPORT_DIR, pat))
    files = [f for f in files if not f.endswith(".data_guard.md")]
    return max(files, key=os.path.getmtime) if files else None


def run_and_capture(argv) -> tuple[int, str]:
    """编程式调用底层诊断链，捕获 stdout 日志。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


def render_report():
    """读取并渲染最新报告，提供 md/html 下载。"""
    md = latest_report(".md")
    if not md:
        st.error("未生成报告。常见原因：工资表列名无法自动映射。"
                 "请用标准工资表（含 姓名/工号/部门/岗位/职级/月薪 等列），或先在本机用 CLI 生成映射文件。")
        return
    st.subheader("诊断报告")
    st.markdown(open(md, encoding="utf-8").read(), unsafe_allow_html=True)
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("下载报告 (Markdown)", open(md, "rb").read(),
                           os.path.basename(md), mime="text/markdown")
    html = latest_report(".html")
    if html:
        with col2:
            st.download_button("下载报告 (HTML，含图表)", open(html, "rb").read(),
                               os.path.basename(html), mime="text/html")


# ---------------------------------------------------------------------------
st.set_page_config(page_title="薪酬诊断 CLI · 本地 Web UI", layout="wide")
st.title("薪酬诊断 CLI · 本地 Web UI")
st.caption("全部计算在本机完成，薪酬数据不出内网。")

mode = st.sidebar.radio("选择模式", ["一键演示（零风险）", "诊断我的工资表"])

if mode.startswith("一键演示"):
    st.header("一键演示")
    st.info("使用内置合成模拟数据，零交互跑通完整诊断链（带宽/红绿圈/市场对标/调薪/"
            "固浮比/岗位评估）。报告带「模拟数据」横幅，**可安全外发**。")
    if st.button("▶ 运行演示", type="primary"):
        with st.spinner("诊断中…"):
            rc, log = run_and_capture(["--demo", "--json"])
        st.subheader("运行日志")
        st.code(log, language="text")
        render_report()

else:
    st.header("诊断我的工资表")
    uploaded = st.file_uploader("上传工资表（.csv / .xlsx）", type=["csv", "xlsx"])
    budget = st.number_input("调薪预算 (%)", min_value=0.0, max_value=100.0,
                             value=5.0, step=0.5,
                             help="本次调薪的总预算占薪酬总额的比例")
    strategy = st.selectbox("调薪策略", ["A", "B", "C", "D"], index=0,
                            help="A/B/C/D 为四种调薪分配策略，详见报告说明")
    job_model = st.selectbox("岗位评估模型", ["hay", "mercer"], index=0,
                             help="海氏(Hay) 或美世(Mercer) 岗位评估法")
    st.warning("⚠️ 真实薪酬数据敏感：报告仅限本地查看，**切勿外发**；"
               "如需对外分享，请先调用脱敏流程生成脱敏副本。")

    if st.button("▶ 运行诊断", type="primary", disabled=not uploaded):
        os.makedirs(DATA_DIR, exist_ok=True)
        ext = os.path.splitext(uploaded.name)[1]
        tmp = os.path.join(DATA_DIR, f"web_upload_{datetime.now():%Y%m%d_%H%M%S}{ext}")
        with open(tmp, "wb") as f:
            f.write(uploaded.getbuffer())

        argv = ["--pipeline", "--file", tmp,
                "--budget-pct", str(budget),
                "--strategy", strategy,
                "--job-model", job_model,
                "--auto-confirm",            # 零交互：自动固化字段映射
                "--i-know-this-is-real-data",  # 明确知情，抑制护栏提示
                "--json"]
        with st.spinner("诊断中…"):
            rc, log = run_and_capture(argv)
        st.subheader("运行日志")
        st.code(log, language="text")
        render_report()

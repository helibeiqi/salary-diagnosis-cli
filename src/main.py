# -*- coding: utf-8 -*-
"""
main.py — 本地离线入口（不经过 dsh）
================================================================================

为什么必须有这一层（架构 §3）
--------------------------------------------------------------------------------
如果只能通过 dsh + 大模型驱动，那么每次调试都要经历
「改 Python → 重启 dsh → 提示模型 → 等模型决定调哪个工具 → 看结果」，
一次循环几分钟，且**模型的不确定性会掩盖代码的确定性 bug**。

本文件把 11 个工具变成可直接命令行调用的对象，于是：
  * QA 能写脚本做端到端回归（不消耗任何 token）；
  * 面试演示时可以先跑 `--pipeline` 出一份完整报告，再讲「模型是怎么自己走完这条链的」；
  * 出问题时能立刻判定是**计算错**还是**模型没调对工具**。

用法
--------------------------------------------------------------------------------
    python run_agent.py --list                       # 列出 11 个工具
    python run_agent.py --status                      # 各工具 handler 是否已交付
    python run_agent.py --schema generate_band        # 看某工具的 JSON Schema
    python run_agent.py --call analyze_current_state --args '{"session_id":"s_..."}'
    # 一键全流程三种映射确认方式（确定性优先）：
    python run_agent.py --pipeline --file data/sample_salary.csv \
        --budget-pct 3 --strategy B --auto-confirm   # 自动映射，歧义列取引擎猜测
    python run_agent.py --pipeline --file data/ambiguous_salary.csv \
        --mapping-file mapping.json                   # 用人工确认的映射文件
    python run_agent.py --pipeline --file data/ambiguous_salary.csv \
        --budget-pct 3 --strategy B                   # 有歧义列→交互式暂停询问
    python run_agent.py --demo                        # 一键演示：自带合成模拟样例，
                                                      # 零交互跑完整链，外发零风险
    python run_agent.py --serve                       # 以 stdio worker 模式运行（供 dsh 插件 spawn）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # pragma: no cover
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from src.tools import registry  # type: ignore
else:
    from .tools import registry


# =============================================================================
# 输出helpers：终端可读优先，同时保留 --json 机读模式
# =============================================================================


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _print_envelope(env: Dict[str, Any], as_json: bool = False) -> None:
    """人读模式只打摘要与产物路径 —— 与模型看到的内容保持一致，便于对照。"""
    if as_json:
        print(_dump(env))
        return
    flag = "✓" if env.get("ok") else "✗"
    print(f"\n{flag} [{env.get('code')}] {env.get('message')}")
    if env.get("hint"):
        print(f"  → 下一步：{env['hint']}")
    summary = (env.get("data") or {}).get("summary_md")
    if summary:
        print("\n" + "-" * 72)
        print(summary)
        print("-" * 72)
    for art in env.get("artifacts") or []:
        print(f"  · 产物 {art.get('id')}: {art.get('path')}")
    for ch in env.get("charts") or []:
        print(f"  · 图表 {ch.get('id')}: {ch.get('path')}")
    for w in env.get("warnings") or []:
        print(f"  ! {w}")
    meta = env.get("meta") or {}
    if meta.get("elapsed_ms") is not None:
        print(f"  ({meta.get('tool')} 耗时 {meta['elapsed_ms']} ms)")


# =============================================================================
# 子命令
# =============================================================================


def cmd_list(as_json: bool) -> int:
    tools = registry.list_tools(with_json_schema=False)
    if as_json:
        print(_dump(tools))
        return 0
    status = registry.implemented_status()
    print(f"\n共 {len(tools)} 个工具（契约版本 {registry.CONTRACT_VERSION}）：\n")
    print(f"{'序':<3} {'工具名':<24} {'就绪':<5} {'并发':<5} 说明")
    print("-" * 100)
    for spec in sorted(registry.TOOL_SPECS, key=lambda s: s.stage):
        ready = "✓" if status.get(spec.name) else "×"
        conc = "✓" if spec.concurrency_safe else "×"
        desc = spec.description.replace("\n", " ")[:52]
        print(f"{spec.stage:<3} {spec.name:<24} {ready:<5} {conc:<5} {desc}")
    print("\n就绪=× 表示对应计算模块尚未交付，调用会返回 NOT_IMPLEMENTED。")
    return 0


def cmd_status(as_json: bool) -> int:
    status = registry.implemented_status()
    if as_json:
        print(_dump(status))
        return 0
    ready = [k for k, v in status.items() if v]
    pending = [k for k, v in status.items() if not v]
    print(f"\nhandler 就绪 {len(ready)}/{len(status)}")
    for name in ready:
        print(f"  ✓ {name}")
    for name in pending:
        print(f"  × {name}  （src/tools/{registry.TOOLS_BY_NAME[name].module}.py 未交付）")
    return 0 if not pending else 0            # 未交付不算失败，只是提示


def cmd_schema(name: str) -> int:
    spec = registry.TOOLS_BY_NAME.get(name)
    if spec is None:
        print(f"没有名为 {name!r} 的工具。可用：{', '.join(registry.TOOL_NAMES)}")
        return 2
    print(_dump({
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "input_schema": registry.to_json_schema(spec.parameters),
        "output_schema": registry.ENVELOPE_OUTPUT_SCHEMA,
        "timeout_s": spec.timeout_s,
        "concurrency_safe": spec.concurrency_safe,
    }))
    return 0


def cmd_call(name: str, args_json: Optional[str], as_json: bool,
             i_know_real_data: bool = False) -> int:
    try:
        arguments = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as exc:
        print(f"--args 不是合法 JSON：{exc}")
        return 2
    if not isinstance(arguments, dict):
        print("--args 必须是 JSON 对象。")
        return 2
    # 真实数据确认开关透传（仅对 generate_report 生效）
    if i_know_real_data and name == "generate_report":
        arguments = {**arguments, "i_know_real_data": True}
    env = registry.call_tool(name, arguments)
    _print_envelope(env, as_json)
    return 0 if env.get("ok") else 1


# ---------------------------------------------------------------------------
# 一键全流程
# ---------------------------------------------------------------------------


# 自动映射阈值：与 loader.schemas 的 "ROOT(55) 必须低于自动确认阈值" 自检一致
#（main.py 不依赖该常量值，这里独立声明，改一处需同步另一处）。
_AUTO_CONFIRM_THRESHOLD = 60


def _is_interactive() -> bool:
    """
    是否处于可交互的终端：stdin 与 stdout **同时**是 tty 才算。

    只用 sys.stdin.isatty() 不够——在「管道/捕获输出」场景下 stdout 不是 tty
    （拿不到回显、用户也看不到提问），此时即使 stdin 是 tty 也应走非交互分支，
    否则 input() 会直接 EOFError 崩溃。CI、重定向、subprocess 都属此列。
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _auto_mapping(load_env: Dict[str, Any], min_confidence: int = 60,
                  include_ambiguous: bool = False) -> Dict[str, str]:
    """
    从 load_salary_data 的建议里自动挑映射（仅用于离线自测/演示）。

    **刻意只在 main.py 里做这件事**：真实链路必须由模型结合列语义来确认映射
    （架构把 confirm_mapping 单列一步就是为了让人/模型对字段负责）。
    这里的自动映射是为了让回归测试不必人工介入，不是产品行为。

    include_ambiguous=True（--auto-confirm 模式）时，歧义列也取引擎最佳猜测
    （suggest）自动映射；否则歧义列被排除，交由交互式或 mapping-file 处理。
    """
    suggested = (load_env.get("data") or {}).get("suggested_mapping") or {}
    mapping: Dict[str, str] = {}
    for raw, info in suggested.items():
        if not isinstance(info, dict):
            continue
        field = info.get("suggest")
        if not field:
            continue
        conf = info.get("confidence") or 0
        is_amb = bool(info.get("ambiguous"))
        if is_amb:
            if include_ambiguous:
                mapping[str(raw)] = str(field)      # 自动确认取引擎最佳猜测
            continue
        if float(conf) >= min_confidence:
            mapping[str(raw)] = str(field)
    return mapping


def _load_mapping_file(path: str) -> Dict[str, str]:
    """
    读取 --mapping-file：支持 .json / .yaml(.yml)，返回 {原始列名: 标准字段}。
    该文件是「人工确认的映射」的固化载体——比交互式输入更可复现，CI 与交付都用它。
    """
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--mapping-file 指定的文件不存在：{path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError("--mapping-file 用了 YAML 但环境未装 PyYAML；"
                               "请改用 JSON 或 pip install pyyaml")
        obj = yaml.safe_load(text)
    else:
        obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("--mapping-file 顶层必须是 {原始列名: 标准字段名} 的对象")
    return {str(k): str(v) for k, v in obj.items()}


def _interactive_resolve(load_env: Dict[str, Any]) -> tuple:
    """
    交互式映射确认：只对引擎标记为 ambiguous 的列暂停，让用户/模型基于
    candidates + reasons + sample_values 拍板——这正是 LLM_MAPPING_CONTRACT
    的"授权范围"（非 ambiguous 列照抄引擎建议，不在交互里出现）。

    返回 (mapping, quit_flag)。mapping 含：自动确认的高置信列 + 用户为歧义列选的字段。
    """
    suggested = (load_env.get("data") or {}).get("suggested_mapping") or {}
    ambiguous = (load_env.get("data") or {}).get("auto_confidence", {}).get("ambiguous_columns") or []
    # 1) 先收齐非歧义的高置信映射
    mapping = _auto_mapping(load_env, _AUTO_CONFIRM_THRESHOLD, include_ambiguous=False)
    if not ambiguous:
        return mapping, False

    print("\n" + "-" * 78)
    print("⚠ 以下列程序无法自动判定（确定性引擎主动标记 ambiguous），需人工拍板：")
    print("-" * 78)
    for a in ambiguous:
        col = a["column"]
        cands = a.get("candidates") or []
        reasons = a.get("reasons") or []
        samples = a.get("sample_values") or []
        print(f"\n● 列「{col}」   引擎建议：{a.get('suggest')}（conf={a.get('confidence')}）")
        print(f"  歧义原因：{'; '.join(reasons) if reasons else '（未说明）'}")
        if samples:
            print(f"  前 5 行示例值：{samples}")
        if not cands:
            raw_in = input(f"  候选字段：无（引擎候选集可能缺失）。"
                           f"请输入标准字段名 / 回车跳过 / 'q'退出：").strip()
            if raw_in.lower() == "q":
                return mapping, True
            if raw_in:
                mapping[col] = raw_in
            continue
        print("  候选字段：")
        for i, c in enumerate(cands, 1):
            print(f"    {i}. {c}")
        raw_in = input(f"  选择(1-{len(cands)}) / 回车用建议{a.get('suggest')} / "
                       f"'s'跳过 / 'q'退出：").strip()
        if raw_in.lower() == "q":
            return mapping, True
        if raw_in.lower() == "s" or raw_in == "":
            if a.get("suggest"):
                mapping[col] = a["suggest"]
            continue
        try:
            idx = int(raw_in)
            if 1 <= idx <= len(cands):
                mapping[col] = cands[idx - 1]
            else:
                print("  编号越界，跳过该列。")
        except ValueError:
            # 允许直接输入字段名（confirm_mapping 会二次校验是否为合法标准字段）
            mapping[col] = raw_in
    return mapping, False


def _resolve_required_params(strategy, job_model, budget_pct):
    """
    必填参数（调薪策略 / 岗位评估模型 / 调薪预算）在交互模式下未提供则询问，
    非交互模式回退默认值并告警。需求 §3.4：这些参数要么必填、要么交互询问，
    不能用"悄悄默认"掩盖来源——报告里的调薪结论必须有明确出处。
    返回 (strategy, job_model, budget_pct)，均为最终值。
    """
    tty = _is_interactive()

    # 调薪策略
    if strategy is None:
        if tty:
            strategy = input("调薪分配策略 [A/B/C/D]（缺省 C 按绩效加权）：").strip().upper() or "C"
        else:
            print("提示：--strategy 未提供，非交互模式默认 C（按绩效加权）。")
            strategy = "C"
    if strategy not in ("A", "B", "C", "D"):
        print(f"--strategy 非法值 {strategy!r}，回退 C。")
        strategy = "C"

    # 岗位评估模型
    if job_model is None:
        if tty:
            job_model = input("岗位评估模型 [hay/mercer]（缺省 hay 海氏三要素）：").strip().lower() or "hay"
        else:
            print("提示：--job-model 未提供，非交互模式默认 hay。")
            job_model = "hay"
    if job_model not in ("hay", "mercer"):
        print(f"--job-model 非法值 {job_model!r}，回退 hay。")
        job_model = "hay"

    # 调薪预算比例
    if budget_pct is None:
        if tty:
            try:
                budget_pct = float(input("调薪预算比例（如 0.06=6%，缺省 0.06）：").strip() or "0.06")
            except ValueError:
                budget_pct = 0.06
        else:
            print("提示：--budget-pct 未提供，非交互模式默认 0.06。")
            budget_pct = 0.06
    return strategy, job_model, float(budget_pct)


def cmd_pipeline(file_path: str, budget_pct, as_json: bool,
                 stop_on_error: bool = False, strategy=None, job_model=None,
                 auto_confirm: bool = False, mapping_file: Optional[str] = None,
                 i_know_real_data: bool = False,
                 synthetic: bool = False) -> int:
    """
    按 stage 顺序跑完整条诊断链，模拟模型的理想调用序列。

    链路顺序严格对齐需求全文的九段式：
        load → confirm_mapping → generate_band → analyze_current_state
        → market_benchmark → simulate_increase → simulate_pay_mix
        → calc_job_score → generate_report

    出错不中断（除 --strict）：这正是要观察的东西 —— 哪一步降级、降级后
    下游是否还能出报告。一条「任一环节失败就整体失败」的链在演示时毫无价值。

    映射确认分三路（确定性优先，LLM 只做最后拍板）：
        1. --mapping-file 指定人工确认的映射（最可复现，CI/交付首选）；
        2. --auto-confirm 自动映射，歧义列取引擎最佳猜测（离线自测/演示）；
        3. 否则：有歧义列 → 交互式暂停询问；非交互模式 → 提示加 --auto-confirm 退出。
    """
    # 必填/交互确定策略、模型、预算
    strategy, job_model, budget_pct = _resolve_required_params(
        strategy, job_model, budget_pct)

    print("=" * 78)
    print(f"薪酬诊断全流程  数据源：{file_path}")
    print("=" * 78)

    steps: List[Dict[str, Any]] = []
    artifacts_all: List[Dict[str, Any]] = []
    charts_all: List[Dict[str, Any]] = []

    def run(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        print(f"\n▶ [{name}]")
        env = registry.call_tool(name, arguments)
        _print_envelope(env, as_json)
        steps.append({"tool": name, "ok": env.get("ok"), "code": env.get("code")})
        for art in env.get("artifacts") or []:
            artifacts_all.append({"tool": name, **art})
        for ch in env.get("charts") or []:
            charts_all.append({"tool": name, **ch})
        return env

    env = run("load_salary_data", {"file_path": file_path})
    if not env.get("ok"):
        print("\n加载失败，流程终止。")
        return 1
    session_id = (env.get("meta") or {}).get("session_id") \
        or (env.get("data") or {}).get("session_id")
    print(f"\n会话 ID：{session_id}")

    # --demo 合成模拟数据：标记会话分级为 synthetic，使报告生成器跳过真实数据护栏
    # （红字水印 + data_guard.md sidecar），兑现「外发零风险」承诺。
    # 失败静默：分级标记只是报告呈现层，绝不影响诊断计算本身。
    if synthetic and session_id:
        try:
            from src.tools.session import get_store
            get_store().set_meta(session_id, {"data_classification": "synthetic"})
        except Exception:  # noqa: BLE001
            pass

    # —— 映射确认：三条路径 ——
    if mapping_file:
        try:
            mapping = _load_mapping_file(mapping_file)
        except Exception as exc:  # noqa: BLE001 - 文件/解析错误直接告知用户
            print(f"\n读取 --mapping-file 失败：{exc}")
            return 1
    elif auto_confirm:
        mapping = _auto_mapping(env, _AUTO_CONFIRM_THRESHOLD, include_ambiguous=True)
    else:
        ambiguous = (env.get("data") or {}).get("auto_confidence", {}).get("ambiguous_columns") or []
        if ambiguous and not _is_interactive():
            print("\n⚠ 该表存在 %d 个歧义列，需人工确认映射。" % len(ambiguous))
            print("  非交互模式下无法询问，请加 --auto-confirm 走自动猜测，")
            print("  或 --mapping-file 指定人工确认的映射后再跑。")
            for a in ambiguous:
                print(f"   · 「{a['column']}」候选 {a.get('candidates')}  原因：{a.get('reasons')}")
            return 3
        mapping, quit = _interactive_resolve(env)
        if quit:
            print("\n已退出，未固化映射。")
            return 1

    if not mapping:
        print("\n无法推断字段映射，流程终止（真实链路应由模型确认映射）。")
        return 1
    print(f"\n已确定映射 {len(mapping)} 列（歧义列由人工/--auto-confirm 决定）：")
    for k, v in mapping.items():
        print(f"    {k} -> {v}")

    env = run("confirm_mapping", {"session_id": session_id, "mapping": mapping})
    if not env.get("ok"):
        return 1

    sid = {"session_id": session_id}
    plan: List[tuple] = [
        # 口径前提（tests/verify_golden.py「口径前提 1」）：判定前一律不取整（K3），
        # round_to=0 与金标准对账脚本完全一致；取整只允许发生在展示层。
        ("generate_band", {**sid, "mode": "optimize", "round_to": 0}),
        ("analyze_current_state", sid),
        ("market_benchmark", sid),
        # strategy / job_model / budget_pct 已由 _resolve_required_params 收紧为最终值，
        # 缺参属于调用方 bug，不该由 handler 兜底放宽。
        ("simulate_increase", {**sid, "budget_pct": budget_pct,
                               "strategy": strategy}),
        ("simulate_pay_mix", sid),
        ("calc_job_score", {**sid, "model": job_model}),
        ("generate_report", {**sid, "formats": ["md", "html"],
                              "i_know_real_data": i_know_real_data}),
    ]
    for name, arguments in plan:
        env = run(name, arguments)
        if stop_on_error and not env.get("ok"):
            print(f"\n--strict 模式：{name} 失败，流程终止。")
            return 1

    print("\n" + "=" * 78)
    print("流程小结")
    print("=" * 78)
    for s in steps:
        print(f"  {'✓' if s['ok'] else '✗'} {s['tool']:<24} {s['code']}")
    failed = [s for s in steps if not s["ok"]]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} 步成功。")

    # 产物清单（需求 §3.5：报告生成后打印报告路径与图表路径清单）
    if artifacts_all or charts_all:
        print("\n" + "-" * 78)
        print("产物清单")
        print("-" * 78)
        for art in artifacts_all:
            print(f"  文件 [{art.get('id')}]  ({art.get('tool')}): {art.get('path')}")
        for ch in charts_all:
            print(f"  图表 [{ch.get('id')}]  ({ch.get('tool')}): {ch.get('path')}")

    return 0 if not failed else 1


def cmd_demo(as_json: bool = False) -> int:
    """
    --demo 一键演示（ROADMAP P2「CLI 易用性」）：零交互跑通完整诊断链。

    * 数据源固定为 data/sample_salary.csv（合成模拟数据，synthetic 分级）；
      样例缺失时自动调用根目录 mock_data.py 重新生成（随机种子固定，产出可复现）。
    * 固定参数：预算 5% · 策略 A（平均分配）· 海氏岗位评估 · 自动映射
      —— 不触发任何交互式询问，投影仪/录屏场景可直接跑。
    * 数据分级为 synthetic：报告渲染中性「模拟数据」横幅，不触发真实数据
      护栏（无红字水印、无 data_guard.md），外发零风险。
    """
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sample = os.path.join(root, "data", "sample_salary.csv")

    if not os.path.exists(sample):
        mock_script = os.path.join(root, "mock_data.py")
        print(f"[demo] 未找到 {sample}，正在调用 mock_data.py 生成模拟数据…")
        r = subprocess.run([sys.executable, mock_script])
        if r.returncode != 0 or not os.path.exists(sample):
            print("[demo] 模拟数据生成失败，请手动运行：python mock_data.py")
            return 1

    print()
    print("#" * 78)
    print("# DEMO 一键演示（零交互 · 外发零风险）")
    print("#   数据源 : data/sample_salary.csv —— 合成模拟数据（synthetic 分级），")
    print("#            与任何真实自然人无关，报告可安全外发 / 投屏")
    print("#   固定参数 : 预算 5% · 策略 A（平均分配）· 海氏评估 · 自动映射")
    print("#" * 78)

    rc = cmd_pipeline(sample, 0.05, as_json,
                      strategy="A", job_model="hay", auto_confirm=True,
                      synthetic=True)

    if rc == 0 and not as_json:
        print()
        print("[demo] 完成。报告位于 report/ 目录（md + html 双格式，html 可直接双击打开）。")
        print("       数据分级为 synthetic：报告带中性「模拟数据」横幅，不触发真实数据")
        print("       护栏，可安全外发；若要诊断真实工资表，请用 --pipeline --file <路径>。")
    return rc


# =============================================================================
# 入口
# =============================================================================


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run_agent.py",
        description="薪酬诊断 Agent 本地入口（离线，不经 dsh / 不消耗 token）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出全部 11 个工具")
    g.add_argument("--status", action="store_true", help="各工具 handler 是否已交付")
    g.add_argument("--schema", metavar="TOOL", help="打印某工具的 JSON Schema")
    g.add_argument("--call", metavar="TOOL", help="调用某个工具")
    g.add_argument("--pipeline", action="store_true", help="跑完整诊断链")
    g.add_argument("--demo", action="store_true",
                   help="一键演示：自带合成模拟样例零交互跑完整链（外发零风险，"
                        "固定 预算5%/策略A/海氏/自动映射，忽略 --file 等定制参数）")
    g.add_argument("--serve", action="store_true",
                   help="以 stdio worker 模式运行（供 dsh 插件 spawn）")

    ap.add_argument("--args", metavar="JSON", help="--call 的参数（JSON 对象）")
    ap.add_argument("--file", metavar="PATH", default="data/sample_salary.csv",
                    help="--pipeline 的数据文件，缺省 data/sample_salary.csv")
    ap.add_argument("--budget-pct", type=float, default=None,
                    help="--pipeline 的调薪预算比例（如 0.06）；省略则交互询问/非交互默认 0.06")
    ap.add_argument("--strategy", default=None, choices=["A", "B", "C", "D"],
                    help="--pipeline 的调薪分配策略；省略则交互询问/非交互默认 C")
    ap.add_argument("--job-model", default=None, choices=["hay", "mercer"],
                    help="--pipeline 的岗位评估模型；省略则交互询问/非交互默认 hay")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="--pipeline 自动映射（歧义列取引擎最佳猜测），不进入交互确认")
    ap.add_argument("--mapping-file", metavar="PATH", default=None,
                    help="--pipeline 指定人工确认的映射文件（JSON/YAML：{原始列名: 标准字段}）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON（机读）")
    ap.add_argument("--strict", action="store_true", help="--pipeline 任一步失败即终止")
    ap.add_argument("--i-know-this-is-real-data", action="store_true",
                    help="真实薪酬数据确认：生成 HTML 报告时抑制 data_guard.md 安全提示"
                         "（仅限本机本地使用，代表操作者已知晓数据敏感性）")
    args = ap.parse_args(argv)

    if args.serve:
        # 委托给常驻 worker：本地入口与 dsh 插件走同一个 server 实现，
        # 避免「命令行能跑、插件里跑不通」这类只在集成时才暴露的差异。
        from src.tools import server as server_mod
        return server_mod.serve(framing_mode="auto", emit_ready=True)

    if args.list:
        return cmd_list(args.json)
    if args.status:
        return cmd_status(args.json)
    if args.schema:
        return cmd_schema(args.schema)
    if args.call:
        return cmd_call(args.call, args.args, args.json)
    if args.demo:
        return cmd_demo(args.json)
    if args.pipeline:
        return cmd_pipeline(args.file, args.budget_pct, args.json, args.strict,
                            args.strategy, args.job_model,
                            auto_confirm=args.auto_confirm,
                            mapping_file=args.mapping_file)
    return 2


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
sandbox.py — PTC-L2 沙箱：第 11 个工具 `run_comp_code`
================================================================================

它解决什么问题（为什么值得多写一个工具）
--------------------------------------------------------------------------------
Function Calling 的成本在**编排**上：模型想知道「哪些职级同时红圈超 3 人且市场
差距超 10%」，就得把 `analyze_current_state` 和 `market_benchmark` 的完整结果
都拉进上下文再自己比对 —— 几千 token，且模型手算容易错。
PTC（Python Tool Calling / Code Mode）让模型**写一段代码**在服务端跑完，
只把结论（几十字）带回上下文。这是 token 与准确率的双赢。

威胁模型（诚实声明，README 同款措辞）
--------------------------------------------------------------------------------
**这是纵深防御的防呆层，不是对抗恶意代码的安全边界。**
与 dsh 官方对 worker-thread 沙箱的表述一致（「这是隔离措施，而非安全边界」）。
目标是**防止模型误写危险代码造成破坏或泄密**，而非防御蓄意攻击者。
真正的边界是进程边界 + 无 `open` + 无任意 `import` + 超时。

四层防护（架构 §5.4）
--------------------------------------------------------------------------------
    L1  AST 静态预检（父进程内，零成本拒绝）
        拒绝：白名单外 import、双下划线属性、危险内置名、global/nonlocal、超长源码
    L2  受限命名空间（子进程内）
        exec 时只给 SAFE_BUILTINS + pd/np/math/statistics/json + tools/comp
        关键：**不提供 open** —— 没有 open，文件读写就没有可替代路径
    L3  导入拦截
        __import__ 换成白名单导入器，非白名单模块抛 ImportError
    L4  进程隔离 + 资源上限
        一次性子进程；超时 kill；stdout 截断；env 剔除含 KEY/TOKEN/SECRET 的变量

口径一致性（这一条最重要）
--------------------------------------------------------------------------------
沙箱里的 `tools.market_benchmark(...)` 走的是 `registry.call_tool` ——
**与模型直接调用该工具执行完全相同的代码**。
因此 PTC 不会长出「第二套薪酬口径」，这是 PTC 敢引入的前提。
（架构 §5.4 原设计是子进程通过 IPC 回调主 worker；实现上改为子进程直接 import
 registry + 从 `.state/` 读会话 —— 同样是同一份 handler 代码，但省掉一条
 双向 IPC 通道。代价：沙箱内的会话写回对主 worker 内存不可见，
 因此沙箱内的工具调用应视为**只读分析**，这一点已写进工具 description。）
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):  # pragma: no cover - 子进程直跑时补包上下文
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from src.tools.errors import CompToolError, ok_result, tool_guard  # type: ignore
else:
    from .errors import CompToolError, ok_result, tool_guard

# circuit_breaker 放在 errors 块之后：脚本直跑时 errors 块已把仓库根加入 sys.path，
# 此处 `src.tools.circuit_breaker` 才能在 __main__ / --runner 两种模式下都解析成功。
if __package__ in (None, ""):  # pragma: no cover - 子进程 / 直跑时补包上下文
    from src.tools.circuit_breaker import ACTION_HARD_STOP, get_breaker
else:
    from .circuit_breaker import ACTION_HARD_STOP, get_breaker

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_TIMEOUT_S = 15
MAX_TIMEOUT_S = 60
MAX_CODE_CHARS = 8000
MAX_LOG_BYTES = 8 * 1024        # 进模型上下文的 print 输出上限
MAX_RESULT_BYTES = 64 * 1024    # result 对象序列化后上限

# L1/L3 共用的模块白名单：纯计算库，无 IO、无网络、无进程
ALLOWED_MODULES = {
    "pandas", "numpy", "math", "statistics", "json",
    "decimal", "itertools", "collections", "functools", "re", "datetime",
}

# L1 拒绝的内置名（都是能绕开其它三层的"逃逸梯子"）
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "exit", "quit", "help", "memoryview", "breakpoint", "super",
}


class SandboxViolation(CompToolError):
    """代码含被禁语法/模块 —— 在执行前被 AST 预检拦下。"""

    code = "SANDBOX_VIOLATION"
    default_hint = "请改写代码：禁止文件读写、网络、子进程，禁止 import 白名单外模块，禁止访问双下划线属性。"


class SandboxTimeout(CompToolError):
    """执行超时。"""

    code = "SANDBOX_TIMEOUT"
    default_hint = "请缩小数据范围、去掉循环内的重复计算，或把逻辑拆成多次执行。"


class SandboxQuotaExceeded(CompToolError):
    """超过沙箱并发 / 每会话配额（Phase 2 治理器）。"""

    code = "SANDBOX_QUOTA_EXCEEDED"
    default_hint = (
        "沙箱调用过于频繁或并发过高。请合并分析逻辑、减少 run_comp_code 调用次数，"
        "或稍后重试；沙箱配额用于防止失控循环耗尽本机资源。")


class SandboxHardStop(CompToolError):
    """真实数据会话下沙箱不可用 → 安全硬停（Phase 3 熔断器，绝不外发）。"""

    code = "SANDBOX_HARD_STOP"
    default_hint = (
        "沙箱常驻 worker 不可用，且当前为真实数据会话，已按安全策略硬停。"
        "请检查本地 Python 环境后重试；真实薪酬数据不会被发往任何外部服务。")


# =============================================================================
# L1：AST 静态预检
# =============================================================================


def precheck(code: str) -> List[str]:
    """
    执行前的静态预检，返回警告列表；命中硬禁项直接抛 SandboxViolation。

    为什么放在父进程：**零成本拒绝**。一次子进程冷启动 2-3 秒，
    而 90% 的违规代码（`import os`、`open(...)`）在 AST 层一眼可判，
    没有理由为它付一次 spawn。
    """
    if not isinstance(code, str) or not code.strip():
        raise SandboxViolation("code 不能为空。", hint="请给出要执行的 Python 代码。")
    if len(code) > MAX_CODE_CHARS:
        raise SandboxViolation(
            f"代码长度 {len(code)} 超过上限 {MAX_CODE_CHARS} 字符。",
            hint="请把逻辑拆成多次执行，或先用工具拿到聚合结果再做少量计算。")

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxViolation(
            f"代码存在语法错误：第 {exc.lineno} 行 {exc.msg}",
            hint="请修正语法后重试（沙箱只支持同步代码，不支持 async/await）。",
            details={"lineno": exc.lineno, "offset": exc.offset},
        ) from exc

    warnings: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            for raw in names:
                root = (raw or "").split(".")[0]
                if root not in ALLOWED_MODULES:
                    raise SandboxViolation(
                        f"禁止导入模块 {raw!r}。",
                        hint=f"沙箱只允许：{', '.join(sorted(ALLOWED_MODULES))}。"
                             f"数据分析请直接用已注入的 pd / np / tools / comp。",
                        details={"module": raw, "allowed": sorted(ALLOWED_MODULES)},
                    )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise SandboxViolation(
                    f"禁止访问双下划线属性 {node.attr!r}。",
                    hint="`__class__` / `__globals__` / `__subclasses__` 等是常见的沙箱逃逸路径，已被禁止。",
                )
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                raise SandboxViolation(
                    f"禁止使用 {node.id!r}。",
                    hint="文件读写、动态执行与反射类内置函数均被禁止；"
                         "请用 comp.df() 取数据、用 tools.* 调用薪酬工具。",
                )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise SandboxViolation("禁止使用 global / nonlocal。",
                                   hint="沙箱内请用局部变量，最终把结论赋值给 result。")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor,
                               ast.AsyncWith)):
            raise SandboxViolation("沙箱不支持 async / await。",
                                   hint="请改写为同步代码。")

    if "result" not in code and "print" not in code:
        warnings.append(
            "代码里既没有 print() 也没有给 result 赋值 —— 本次执行不会有任何内容返回。")
    return warnings


# =============================================================================
# 主工具
# =============================================================================


@tool_guard
def run_comp_code(code: str, description: str = "",
                  session_id: Optional[str] = None,
                  timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """
    在受限 Python 子进程中执行一段分析代码。

    参数
    ----
    code : str
        同步 Python 代码。可用 `pd` / `np` / `math` / `statistics` / `json`，
        以及注入对象 `tools`（10 个薪酬工具的同步封装）与 `comp`（会话只读视图）。
    description : str
        一句话说明这段代码做什么（进 summary_md，便于人复核模型意图）。
    session_id : str, optional
        会话 ID；给出后 `comp.df()` 才能取到数据。
    timeout_s : float
        超时秒数，缺省 15，上限 60。

    返回
    ----
    ok_result 包裹：logs（print 输出）、result（赋值给 result 的对象）、
    truncated、elapsed_ms、sandbox（执行环境自述）。
    """
    started = time.perf_counter()
    timeout = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    warnings = precheck(code)                       # L1：可能直接抛 SandboxViolation
    resident_on = _resident_enabled()

    # --- Phase 2 治理器（信号量 + 每会话配额）---
    _GOVERNOR.acquire(session_id)
    try:
        # --- Phase 2 常驻沙箱 + Phase 3 数据分级熔断 ---
        if resident_on:
            try:
                from .registry import _resolve_classification as _rc
                cls = _rc(session_id)
            except Exception:  # noqa: BLE001
                cls = "unknown"
            inner = _RESIDENT.execute(code, session_id, timeout)
            if inner is None:
                # 常驻 worker 传输失败 → 咨询熔断器按数据分级决策
                br = get_breaker()
                br.report_failure("sandbox_worker")
                decision = br.decide("sandbox_worker", cls)
                if decision.action == ACTION_HARD_STOP:
                    raise SandboxHardStop(
                        "沙箱常驻 worker 不可用（真实数据会话，按安全策略硬停）。",
                        details={"breaker": decision.to_dict()})
                # 非真实数据路径：降级回退一次性 subprocess（安全兜底）
                inner = _run_oneshot(code, session_id, timeout)
            else:
                get_breaker().record_success("sandbox_worker")
        else:
            inner = _run_oneshot(code, session_id, timeout)
    finally:
        _GOVERNOR.release()

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if not inner.get("ok"):
        raise CompToolError(
            inner.get("message") or "沙箱内代码执行失败。",
            hint=inner.get("hint") or "请根据 error_type 修正代码后重试。",
            details={"error_type": inner.get("error_type"),
                     "logs": inner.get("logs", [])[:20]},
        )

    logs: List[str] = inner.get("logs") or []
    result_obj = inner.get("result")
    summary_lines = [
        "### 沙箱执行结果",
        "",
        f"- **意图**：{description or '（未说明）'}",
        f"- **耗时**：{elapsed_ms} ms（超时上限 {timeout:.0f}s）",
        f"- **输出行数**：{len(logs)}",
        "",
    ]
    if logs:
        summary_lines += ["**print 输出**：", "```", *logs[:40], "```", ""]
    if result_obj is not None:
        preview = json.dumps(result_obj, ensure_ascii=False)[:1200]
        summary_lines += ["**result**：", "```json", preview, "```", ""]
    if inner.get("truncated"):
        summary_lines.append("> ⚠️ 输出过长已截断，请缩小输出范围。")
    if warnings:
        summary_lines += [f"> ⚠️ {w}" for w in warnings]
    summary_lines.append("")
    summary_lines.append("> 下一步：把上面的结论用于后续工具调用，或直接向用户汇报。")

    return ok_result(
        summary_md="\n".join(summary_lines),
        logs=logs,
        result=result_obj,
        truncated=bool(inner.get("truncated")),
        elapsed_ms=elapsed_ms,
        warnings=warnings + list(inner.get("warnings") or []),
        session_id=session_id,
        sandbox={
            "python": sys.executable.replace("\\", "/"),
            "timeout_s": timeout,
            "mode": "resident" if resident_on else "subprocess",
            "isolated": "subprocess" if not resident_on else "resident-worker",
            "allowed_modules": sorted(ALLOWED_MODULES),
            "note": "纵深防御的防呆层，非对抗恶意代码的安全边界。",
        },
    )


# =============================================================================
# 子进程 runner（L2 / L3 在这里生效）
# =============================================================================

SAFE_BUILTIN_NAMES = (
    "abs min max sum len round sorted zip enumerate range list dict set tuple "
    "str int float bool isinstance issubclass type repr any all filter map "
    "reversed divmod pow abs format hash id slice frozenset next iter "
    "True False None NotImplemented Ellipsis "
    "Exception ValueError TypeError KeyError IndexError ZeroDivisionError "
    "ArithmeticError AttributeError StopIteration RuntimeError"
).split()


def _build_safe_builtins() -> Dict[str, Any]:
    """从真实 builtins 里**白名单摘取**（而不是黑名单剔除）。"""
    import builtins
    safe: Dict[str, Any] = {}
    for name in SAFE_BUILTIN_NAMES:
        if hasattr(builtins, name):
            safe[name] = getattr(builtins, name)
    return safe


def _guarded_import(name: str, globals_=None, locals_=None, fromlist=(), level=0):
    """L3：白名单导入器。非白名单模块一律 ImportError（附可用清单）。"""
    root = (name or "").split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(
            f"module {name!r} is not allowed in the comp sandbox; "
            f"allowed: {sorted(ALLOWED_MODULES)}")
    import importlib
    return importlib.import_module(name)


class _CompView:
    """会话只读视图：只暴露取数与产物句柄，**不暴露任何写回能力**。"""

    def __init__(self, session_id: Optional[str]) -> None:
        self.session_id = session_id
        self._session = None

    def _load(self):
        if self._session is None:
            if not self.session_id:
                raise ValueError("没有 session_id，无法取会话数据；"
                                 "请在调用 run_comp_code 时传入 session_id。")
            from src.tools.session import get_store
            self._session = get_store().load(self.session_id)
        return self._session

    def df(self):
        """返回会话 DataFrame 的**副本**（改它不会影响真实会话）。"""
        return self._load().df.copy()

    def meta(self) -> Dict[str, Any]:
        """返回会话 meta 的浅拷贝（含 band / diagnose 等上游聚合结论）。"""
        return dict(self._load().meta)

    def get_artifact(self, key: str) -> Any:
        return dict(self._load().meta).get(key)

    def __repr__(self) -> str:
        return f"<comp session_id={self.session_id!r}>"


class _ToolsProxy:
    """
    10 个薪酬工具的同步封装 —— 全部转发到 `registry.call_tool`。

    ⇒ 沙箱里调 `tools.market_benchmark(session_id=..)` 与模型直接调该工具
      执行的是同一份代码、同一套口径（架构 §5.4）。
    """

    def __init__(self, session_id: Optional[str]) -> None:
        self._sid = session_id
        from src.tools import registry
        self._registry = registry
        for name in registry.tool_names():
            if name == "run_comp_code":          # 禁止递归自调用
                continue
            setattr(self, name, self._make(name))

    def _make(self, name: str):
        def _call(**kwargs: Any) -> Dict[str, Any]:
            if "session_id" not in kwargs and self._sid:
                kwargs["session_id"] = self._sid
            return self._registry.call_tool(name, kwargs, session_id=self._sid)
        _call.__name__ = name
        return _call

    def __repr__(self) -> str:
        return f"<tools {len(self._registry.tool_names()) - 1} 个薪酬工具>"


def execute_in_namespace(code: str, session_id: Optional[str]) -> Dict[str, Any]:
    """
    在受限命名空间里执行代码，返回 **一行 JSON 友好的 dict**（不负责 IO）。

    这是一次性 ``--runner`` 子进程与常驻沙箱 worker **共用的执行核心**：
    两者复用同一套 L2/L3 防护，区别在于"每请求起一个进程"还是
    "一个进程内每请求换全新 namespace"。

    返回 dict 含键：``ok`` / ``logs`` / ``result`` / ``truncated`` / ``warnings``；
    失败时额外含 ``message`` / ``error_type`` / ``hint``。
    """
    logs: List[str] = []
    truncated = False
    log_bytes = 0

    def _print(*parts: Any, sep: str = " ", end: str = "\n") -> None:
        """把 print 重定向进 logs（并做体积上限），保证 stdout 只有协议帧。"""
        nonlocal truncated, log_bytes
        line = sep.join(str(p) for p in parts) + ("" if end == "\n" else end)
        size = len(line.encode("utf-8"))
        if log_bytes + size > MAX_LOG_BYTES:
            truncated = True
            return
        log_bytes += size
        logs.append(line.rstrip("\n"))

    safe_builtins = _build_safe_builtins()
    safe_builtins["__import__"] = _guarded_import       # L3
    safe_builtins["print"] = _print

    namespace: Dict[str, Any] = {"__builtins__": safe_builtins, "print": _print,
                                 "result": None}
    warnings: List[str] = []
    try:
        import pandas as pd
        import numpy as np
        import math as _math
        import statistics as _stats
        namespace.update({"pd": pd, "np": np, "math": _math,
                          "statistics": _stats, "json": json})
        namespace["comp"] = _CompView(session_id)
        namespace["tools"] = _ToolsProxy(session_id)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"部分注入对象不可用：{type(exc).__name__}: {exc}")

    try:
        compiled = compile(code, "<comp_sandbox>", "exec")
        exec(compiled, namespace)                        # L2：受限命名空间
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "error_type": type(exc).__name__,
            "logs": logs,
            "truncated": truncated,
            "warnings": warnings,
            "hint": "请检查代码逻辑；可用对象：pd / np / math / statistics / json / tools / comp。",
        }

    # result 必须能无损序列化 —— 复用集成层的同一个规整器
    try:
        from src.tools.registry import to_lossless
        result_val = to_lossless(namespace.get("result"))
    except Exception:  # noqa: BLE001 - registry 不可用时退化为字符串
        result_val = None if namespace.get("result") is None else str(namespace.get("result"))

    dumped = json.dumps(result_val, ensure_ascii=False, default=str)
    if len(dumped.encode("utf-8")) > MAX_RESULT_BYTES:
        result_val = {"__truncated__": True,
                      "preview": dumped[:2000],
                      "note": f"result 超过 {MAX_RESULT_BYTES} 字节已截断，请只把结论赋给 result。"}
        truncated = True

    return {"ok": True, "logs": logs, "result": result_val,
            "truncated": truncated, "warnings": warnings}


def _runner() -> int:
    """
    一次性子进程入口（``--runner``）：从 stdin 读 {code, session_id}，
    调用共用执行核心 ``execute_in_namespace``，把 **一行 JSON** 写到 stdout。

    纪律：stdout 只允许出现这一行 JSON（沙箱内的 print 被重定向到 logs 列表），
    否则父进程无法区分「代码的输出」与「协议的返回」。
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "message": f"入参解析失败：{exc}"}))
        return 1

    code = payload.get("code") or ""
    session_id = payload.get("session_id")
    out = execute_in_namespace(code, session_id)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
    return 0


# =============================================================================
# 一次性子进程执行 + 治理层（Phase 2：配额 / 常驻沙箱 / Phase 3：熔断接入）
# =============================================================================

def _run_oneshot(code: str, session_id: Optional[str], timeout_s: float) -> Dict[str, Any]:
    """
    一次性子进程执行（旧行为 / 常驻 worker 不可用时的安全回退路径）。

    返回 ``inner`` dict；传输层失败抛 ``SandboxTimeout`` / ``CompToolError``。
    """
    timeout = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    payload = json.dumps({"code": code, "session_id": session_id}, ensure_ascii=False)
    env = {k: v for k, v in os.environ.items()
           if not any(tok in k.upper() for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = PROJECT_ROOT
    env["COMP_SANDBOX"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--runner"],
            input=payload.encode("utf-8"),
            capture_output=True, timeout=timeout,
            cwd=PROJECT_ROOT, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxTimeout(
            f"代码执行超过 {timeout:.0f} 秒已被终止。",
            details={"timeout_s": timeout},
        ) from exc

    raw_out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 and not raw_out:
        # 子进程直接崩了（段错误 / os._exit）—— 进程隔离在这里体现价值：
        # 主 worker 与会话状态毫发无伤
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-1200:]
        raise CompToolError(
            f"沙箱子进程异常退出（exit_code={proc.returncode}）。",
            hint="请简化代码后重试；若持续失败，改用薪酬工具直接获取结果。",
            details={"exit_code": proc.returncode, "stderr_tail": stderr_tail},
        )
    try:
        inner = json.loads(raw_out.splitlines()[-1]) if raw_out else {}
    except Exception as exc:  # noqa: BLE001
        raise CompToolError(
            f"沙箱返回内容无法解析：{exc}",
            hint="这通常意味着代码往 stdout 打了非 JSON 内容；请只用 print() 输出结论。",
            details={"stdout_tail": raw_out[-800:]},
        ) from exc
    return inner


class _SandboxGovernor:
    """
    沙箱治理器（Phase 2）：全局并发信号量 + 每会话滑动窗口配额。

    杜绝失控循环反复 spawn 子进程耗尽本机 CPU/内存
    —— 这是"token-draining 攻击"的**本地等价物**（架构 §3 P0）。
    """

    def __init__(self, max_concurrent: int = 2, max_calls: int = 50,
                 window_s: int = 60) -> None:
        self._sem = threading.Semaphore(max(1, int(max_concurrent)))
        self._lock = threading.Lock()
        self._calls: Dict[str, List[float]] = {}
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_calls = max(1, int(max_calls))
        self.window_s = max(1, int(window_s))

    def acquire(self, session_id: Optional[str]) -> None:
        """获取执行许可；超限抛 SandboxQuotaExceeded（由 tool_guard 转错误信封）。"""
        if not self._sem.acquire(blocking=False):
            raise SandboxQuotaExceeded(
                f"沙箱并发已达上限（{self.max_concurrent}），请等待其他任务完成。",
                hint="模型不应并行发起大量沙箱调用；请串行执行分析。")
        try:
            self._check_quota(session_id)
        except Exception:
            self._sem.release()
            raise

    def release(self) -> None:
        try:
            self._sem.release()
        except Exception:  # noqa: BLE001
            pass

    def _check_quota(self, session_id: Optional[str]) -> None:
        sid = session_id or "default"
        now = time.time()
        with self._lock:
            ts = self._calls.setdefault(sid, [])
            cutoff = now - self.window_s
            self._calls[sid] = [t for t in ts if t >= cutoff]
            if len(self._calls[sid]) >= self.max_calls:
                raise SandboxQuotaExceeded(
                    f"会话 {sid} 在 {self.window_s}s 内沙箱调用已达 {self.max_calls} 次上限。",
                    hint="请合并多次分析为单次代码执行；沙箱调用受配额保护，"
                         "避免失控循环耗尽本机资源。",
                    details={"session_id": sid, "max_calls": self.max_calls,
                             "window_s": self.window_s})
            self._calls[sid].append(now)


_WORKER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sandbox_worker.py")


class _ResidentSandbox:
    """
    常驻沙箱子进程控制器（Phase 2）。

    懒启动一个长期驻留的 worker 进程，复用解释器（pandas 只 import 一次），
    之后每个请求只在全新 namespace 里 exec，冷启从 2–3s 降到 ms 级（架构 §4.4）。
    进程死亡自动重启（受 30s 窗口重启上限保护，防死循环）；
    任何传输失败对调用方返回 ``None``，由 ``run_comp_code`` 决定熔断 / 回退。
    """

    def __init__(self, max_restarts_per_30s: int = 3) -> None:
        self._proc = None
        self._lock = threading.Lock()
        self._restarts: List[float] = []
        self._max_restarts = max_restarts_per_30s

    def _spawn(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if not any(tok in k.upper()
                          for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = PROJECT_ROOT
        env["COMP_SANDBOX"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, _WORKER_SCRIPT, "--resident"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=PROJECT_ROOT, env=env,
        )

    def _kill(self) -> None:
        try:
            if self._proc is not None:
                self._proc.kill()
                self._proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        self._proc = None

    def _ensure_alive(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._proc = None
        now = time.time()
        self._restarts = [t for t in self._restarts if now - t < 30]
        if len(self._restarts) >= self._max_restarts:
            return False
        try:
            self._spawn()
            self._restarts.append(time.time())
            return self._proc is not None
        except Exception:  # noqa: BLE001
            return False

    def execute(self, code: str, session_id: Optional[str],
                timeout_s: float) -> Optional[Dict[str, Any]]:
        """
        发送一次请求并返回响应 dict；传输失败（含超时 / 进程死亡）返回 ``None``。
        """
        with self._lock:
            if not self._ensure_alive():
                return None
            if self._proc is None or self._proc.stdin is None:
                self._kill()
                return None
            req = (json.dumps({"code": code, "session_id": session_id},
                              ensure_ascii=False) + "\n").encode("utf-8")
            try:
                self._proc.stdin.write(req)
                self._proc.stdin.flush()
            except Exception:  # noqa: BLE001
                self._kill()
                return None

            # 带超时读取响应（防用户代码死循环把 worker 卡死）
            bucket: List[bytes] = [b""]

            def _read() -> None:
                try:
                    if self._proc is None or self._proc.stdout is None:
                        bucket[0] = b""
                        return
                    bucket[0] = self._proc.stdout.readline()
                except Exception:  # noqa: BLE001
                    bucket[0] = b""

            th = threading.Thread(target=_read, daemon=True)
            th.start()
            th.join(timeout=float(timeout_s or DEFAULT_TIMEOUT_S))
            if th.is_alive():
                self._kill()          # 超时：杀掉卡死的 worker，强制重启
                return None
            line = bucket[0]
            if not line:
                self._kill()          # EOF / 进程已死
                return None
            try:
                return json.loads(line.decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                return None


def _resident_enabled() -> bool:
    """常驻沙箱是否启用：env 优先（COMP_SANDBOX_ONESHOT=1 强制一次性），否则读 config。"""
    if os.environ.get("COMP_SANDBOX_ONESHOT"):
        return False
    try:
        from .config_access import get as cfg_get
        return bool(cfg_get("sandbox.resident", True))
    except Exception:  # noqa: BLE001
        return True


def _build_governor() -> _SandboxGovernor:
    try:
        from .config_access import get as cfg_get
        return _SandboxGovernor(
            max_concurrent=int(cfg_get("sandbox.max_concurrent", 2)),
            max_calls=int(cfg_get("sandbox.quota.max_calls", 50)),
            window_s=int(cfg_get("sandbox.quota.window_s", 60)),
        )
    except Exception:  # noqa: BLE001
        return _SandboxGovernor()


# 进程内单例：standalone CLI 与 dsh stdio worker 共用
_GOVERNOR = _build_governor()
_RESIDENT = _ResidentSandbox()


TOOL_META = {
    "name": "run_comp_code",
    "description": "在受限 Python 沙箱中执行分析代码（PTC-L2）。",
    "parameters": {
        "code": {"type": "string", "description": "Python 代码", "required": True},
        "description": {"type": "string", "description": "一句话说明意图", "required": True},
        "session_id": {"type": "string", "description": "会话 ID", "required": False},
    },
}


if __name__ == "__main__":
    if "--runner" in sys.argv:
        sys.exit(_runner())
    # 手工自检：跑一段安全代码 + 一段违规代码
    demo = run_comp_code(code="x = sum([1, 2, 3])\nprint('sum =', x)\nresult = {'sum': x}",
                         description="自检：求和")
    print(json.dumps(demo, ensure_ascii=False, indent=2)[:1500])
    bad = run_comp_code(code="import os\nos.system('echo hi')", description="自检：违规")
    print(json.dumps(bad, ensure_ascii=False, indent=2)[:800])

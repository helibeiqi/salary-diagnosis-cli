# -*- coding: utf-8 -*-
"""
sandbox_worker.py — PTC 沙箱常驻 worker（Phase 2）
================================================================================

由 ``sandbox.run_comp_code`` 懒启动并长期驻留。复用解释器：
pandas / numpy 只 import 一次，之后每个请求只在**全新 namespace** 里 ``exec``，
冷启从 2–3s 降到 ms 级（架构 §4.4）。

协议（stdout 只许出现合法帧，纪律同 server.py P4）
--------------------------------------------------------------------------------
  请求：一行 JSON  ``{"code": "...", "session_id": "..."}``
  响应：一行 JSON  （与一次性 ``--runner`` 的返回 dict 同构）
所有诊断一律走 stderr。

威胁模型：沿用 sandbox.py 的四层防护（AST 预检在父进程完成；L2/L3 在
``execute_in_namespace`` 内生效）。本进程**不提供 open / 任意 import**，与一次性
子进程同一套安全边界。
"""
from __future__ import annotations

import json
import os
import sys

# 包上下文补全（允许 `python sandbox_worker.py --resident` 直跑）
if __package__ in (None, ""):
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from src.tools.sandbox import execute_in_namespace  # 共用执行核心（L2/L3）


def _serve() -> int:
    """常驻循环：逐行读请求，执行，写响应；stdin 关闭（EOF）即退出。"""
    stdin = sys.stdin.buffer
    out = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            break  # EOF：父进程关闭了 stdin，worker 退出
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            _write(out, {"ok": False, "message": f"入参解析失败：{exc}"})
            continue
        code = payload.get("code") or ""
        session_id = payload.get("session_id")
        result = execute_in_namespace(code, session_id)
        _write(out, result)
    return 0


def _write(out, obj: dict) -> None:
    try:
        out.write((json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        out.flush()
    except Exception:  # noqa: BLE001 - 写失败不应让 worker 崩溃
        pass


if __name__ == "__main__":
    if "--resident" in sys.argv:
        sys.exit(_serve())
    sys.stderr.write("sandbox_worker 仅支持 --resident 模式\n")
    sys.exit(2)

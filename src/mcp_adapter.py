# -*- coding: utf-8 -*-
"""
src/mcp_adapter.py — 把 11 个薪酬诊断工具封装为 MCP Server（适配器骨架）

设计定位
--------
本文件是「编排层可插拔」战略在 salary-diagnosis-cli 上的落地：把确定性 Python 引擎
（src/tools/registry.py 的 11 个工具）原样暴露为 Model Context Protocol (MCP) 工具，
使得**任何** MCP 客户端（Claude Desktop / WorkBuddy / Cursor / 自研 agent）都能直接驱动，
而不再依赖 dsh 的 TS 插件这一层胶水。

为什么是「骨架」而非完整实现
-----------------------------
1. 纯标准库实现（不依赖官方 mcp 包），开箱即跑——避免离线环境装包失败；
   后续可平滑替换为官方 `mcp.server` 的 High-level Server API，协议形状一致。
2. 工具定义 100% 复用 registry.TOOL_SPECS + to_json_schema，绝不重复维护 schema；
   执行 100% 走 registry.call_tool，计算口径与路由契约（schemas/registry 主体）零改动。
3. 只实现 MCP 必需方法：initialize / ping / tools/list / tools/call。
   未实现 resources / prompts / 采样(sampling) 等高级能力——按需扩展。

协议要点（stdio 传输）
---------------------
- 传输：stdin/stdout，换行分隔的 JSON-RPC 2.0（MCP stdio 默认约定）。
- 握手：客户端先发 initialize → 服务端回 capabilities → 客户端发
  notifications/initialized（通知，无响应）→ 之后 tools/list / tools/call。
- 安全：仅 stdio，无任何网络出口；真实薪资数据不经本适配器上云
  （是否上云由 registry 内部的数据分级 + config.yaml 的 local 标记决定）。

运行
----
    cd <repo_root>
    python src/mcp_adapter.py          # 由 MCP 客户端作为子进程 spawn
MCP 客户端配置模板见仓库根 .mcp.json.example。
"""
from __future__ import annotations

import io
import json
import os
import sys
from typing import Any, Dict, List, Optional

# —— 路径引导：保证无论从哪个 cwd 启动都能 import src.tools.registry ——
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.tools import registry  # noqa: E402

# MCP 协议常量
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "salary-diagnosis-mcp"
SERVER_VERSION = getattr(registry, "CONTRACT_VERSION", "0.0.0")


def build_mcp_tools() -> List[Dict[str, Any]]:
    """把 registry 的 11 个工具规格转为 MCP tools/list 的 tools 数组。

    工具名 / 描述 / inputSchema 全部来自 registry.TOOL_SPECS，
    绝不在此处重写 schema（单一真理源原则）。单个工具转换失败时跳过，
    避免一个坏工具拖垮整个 tools/list。
    """
    tools: List[Dict[str, Any]] = []
    for spec in registry.TOOL_SPECS:
        try:
            tools.append({
                "name": spec.name,
                "description": spec.description,
                "inputSchema": registry.to_json_schema(spec.parameters),
            })
        except Exception as exc:  # noqa: BLE001 - 单个工具 schema 异常不影响其他
            sys.stderr.write(
                f"[mcp] 跳过工具 {spec.name!r}：schema 编译失败 - {exc}\n")
    return tools


def _tool_result(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """把 registry.call_tool 返回的 envelope 包成 MCP tool result。

    - 整个 envelope 序列化为 text 内容，便于客户端直接展示/解析；
    - ok=False 时置 isError=True，让 MCP 客户端正确呈现失败。
    """
    text = json.dumps(envelope, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": not bool(envelope.get("ok", False)),
    }


def _error(code: int, message: str, req_id: Any) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理单条 JSON-RPC 请求，返回响应 dict；通知类（无 id）返回 None。"""
    if not isinstance(req, dict):
        return _error(-32600, "Invalid Request: not an object", None)

    method = req.get("method")
    req_id = req.get("id")  # 通知（notification）没有 id
    params = req.get("params") or {}

    # 通知：不回响应（如 notifications/initialized）
    if req_id is None:
        return None

    if method == "initialize":
        # 握手：声明服务端能力（此处只暴露 tools）
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": build_mcp_tools()},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return _error(-32602, "Invalid params: 'name' required", req_id)
        if not isinstance(arguments, dict):
            return _error(-32602, "Invalid params: 'arguments' must be object",
                          req_id)
        # call_tool 永远返回信封、绝不抛异常；这里仍包一层防御以兜底未知错误
        try:
            envelope = registry.call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001
            envelope = {
                "ok": False, "code": "TOOL_CRASH",
                "message": f"{type(exc).__name__}: {exc}",
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": _tool_result(envelope)}

    # 未知方法
    return _error(-32601, f"Method not found: {method}", req_id)


def serve() -> None:
    """stdio 主循环：逐行读 JSON-RPC（换行分隔），回写响应，EOF 即退出。"""
    stdin = (io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
             if hasattr(sys.stdin, "buffer") else sys.stdin)
    stdout = (io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
              if hasattr(sys.stdout, "buffer") else sys.stdout)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(-32700, "Parse error", None),
                                    ensure_ascii=False) + "\n")
            stdout.flush()
            continue
        resp = handle_request(req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
    # EOF：客户端关闭 stdin，正常退出


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())

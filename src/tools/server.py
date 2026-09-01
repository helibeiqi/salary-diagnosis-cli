# -*- coding: utf-8 -*-
"""
server.py — 常驻 stdio worker：Content-Length 分帧 + JSON-RPC 2.0 分发
================================================================================

角色
--------------------------------------------------------------------------------
本进程由 TS 插件（`src/plugins/comp-tool/src/python-bridge.ts`）以
`spawn(python, [server.py])` 启动并**长期驻留**，通过 stdin/stdout 收发帧。

为什么必须常驻（架构 §5.2）
--------------------------------------------------------------------------------
`import pandas` 在 Anaconda 下冷启动 2-3 秒。一次薪酬诊断会话要调 10+ 次工具，
每次 spawn = 30 秒纯等待，交互体验不可用。常驻还有一个更本质的好处：
**会话 DataFrame 天然留在内存里**，跨工具传递零序列化。

分帧格式（与 LSP / MCP 的 stdio 传输同构，架构 §5.2.1）
--------------------------------------------------------------------------------
    Content-Length: <N>\\r\\n
    \\r\\n
    <恰好 N 字节的 UTF-8 JSON>

选它而不是 NDJSON 的决定性理由：**stdout 可能被第三方库的 stray print / warning
污染**。NDJSON 一旦遇到污染就永久失步，唯一恢复手段是 kill worker —— 而 worker
持有用户已经做完的字段映射与中间结果，重启等于让用户白干。Content-Length 能扫描
到下一个合法头部并自愈。

四条编码纪律（违反任一条都会产生极难复现的间歇故障）
--------------------------------------------------------------------------------
    P1  Content-Length 必须是 **UTF-8 字节数**，不是 len(str) 字符数。
        本项目 payload 全是中文：len("红圈")==2 但 UTF-8 是 6 字节，
        用字符数会少读、帧边界错位。→ 本文件一律 len(body_bytes)。
    P2  只写 sys.stdout.buffer（二进制）。Windows 文本模式会把 \\n 翻成 \\r\\n，
        让头部的 \\r\\n\\r\\n 变成 \\r\\r\\n\\r\\r\\n。
    P3  头 + 体必须在**一次** write 里发出，避免与其它输出交错。
    P4  所有诊断信息走 stderr，stdout 只允许出现合法帧。

对 NDJSON 的读取兼容（刻意保留）
--------------------------------------------------------------------------------
读侧同时接受「裸的一行 JSON」，且**响应会镜像请求的分帧方式**。
这样既满足 TS 桥的 Content-Length 契约，又让人可以直接
`echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m src.tools.server`
做手工验证 —— 这是 QA 与面试演示时最常用的一条命令。

并发模型
--------------------------------------------------------------------------------
**单线程串行**（架构 §5.2「worker 内单线程串行处理请求队列 FIFO」）。
未使用 asyncio：在 Windows 的 ProactorEventLoop 上把 stdin 接成 asyncio 管道
本身就是一个已知的坑，而串行语义下 asyncio 带不来任何吞吐收益 —— 
并发度的控制点在插件侧的 isConcurrencySafe，不在这里。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# 允许 `python src/tools/server.py` 直接运行（此时没有包上下文）
if __package__ in (None, ""):  # pragma: no cover - 仅直跑时生效
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(os.path.dirname(_HERE))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    from src.tools import registry as _registry  # type: ignore
else:
    from . import registry as _registry

PROTOCOL_VERSION = "1.0"
SERVER_NAME = "comp-agent-python-worker"

# 单帧上限：超过直接拒帧且**不缓冲**，保护内存（架构 §5.2.1 C2）
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024

_HEADER_SPLIT = re.compile(rb"\r?\n\r?\n")
_CONTENT_LENGTH = re.compile(rb"Content-Length\s*:\s*(\d+)", re.IGNORECASE)

_STARTED_AT = time.time()


# =============================================================================
# 一、写侧：分帧输出（P1-P4 全部落在这一个函数里）
# =============================================================================


def log(*parts: Any) -> None:
    """诊断输出 —— 一律 stderr（纪律 P4）。stdout 只许出现合法帧。"""
    msg = " ".join(str(p) for p in parts)
    sys.stderr.write(f"[comp-worker] {msg}\n")
    sys.stderr.flush()


def write_message(payload: Dict[str, Any], framing: str = "content-length") -> None:
    """
    把一个 JSON 对象作为一帧发出。

    `ensure_ascii=False` 让中文按 UTF-8 原样编码 —— 体积约为 \\uXXXX 转义的 1/3，
    对 `summary_md` 这类中文长文本是显著的带宽与 token 收益。
    正因为如此，P1（字节数而非字符数）才成为必须遵守的纪律。
    """
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    out = sys.stdout.buffer                                   # P2：二进制
    if framing == "line":
        out.write(body + b"\n")                               # P3：一次写
    else:
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")  # P1：字节数
        out.write(header + body)                              # P3：头+体一次写
    out.flush()


# =============================================================================
# 二、读侧：容错分帧解码（与 TS 侧 FrameDecoder 行为对称）
# =============================================================================


class FrameReader:
    """
    从字节缓冲里切出完整帧，容忍两种分帧与污染。

    容错行为（与架构 §5.2.1 的表格逐条对应）：
      * 帧前有单行垃圾（`WARNING: ...\\n`）→ 垃圾落在 header 区被正则忽略；
      * 多行垃圾（traceback 自带空行）→ 头部区找不到 Content-Length → 丢弃该段，
        下一轮命中真帧；
      * 声明长度超上限 → 丢帧并记诊断，**不缓冲**；
      * 半包/粘包 → 缓冲跨读取保持状态，逐帧切出。
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    def __len__(self) -> int:
        return len(self._buf)

    def next_frame(self) -> Optional[Tuple[bytes, str]]:
        """
        取出下一帧：返回 `(body_bytes, framing)`；不足一帧时返回 None。
        `framing` 用于**镜像响应分帧**，让手工 NDJSON 测试得到 NDJSON 回包。
        """
        while True:
            if not self._buf:
                return None

            # --- 形态 A：裸 JSON 行（NDJSON 兼容路径）------------------------
            head = self._buf.lstrip()[:1]
            if head in (b"{", b"["):
                nl = self._buf.find(b"\n")
                if nl < 0:
                    return None                              # 行还没收完
                line = bytes(self._buf[:nl]).strip()
                del self._buf[:nl + 1]
                if not line:
                    continue
                return line, "line"

            # --- 形态 B：Content-Length 帧 ---------------------------------
            m = _HEADER_SPLIT.search(self._buf)
            if m is None:
                if len(self._buf) > 8192:
                    # 8KB 都没出现头部分隔符 → 全是垃圾，丢弃避免无限增长
                    log("丢弃 8KB 无头部字节（stdout 污染？）")
                    del self._buf[:]
                return None

            header_blob = bytes(self._buf[:m.start()])
            cl = _CONTENT_LENGTH.search(header_blob)
            if cl is None:
                # 头部区没有 Content-Length → 整段（含分隔符）都是垃圾
                log("丢弃无 Content-Length 的头部段：", header_blob[:120])
                del self._buf[:m.end()]
                continue

            length = int(cl.group(1))
            if length > MAX_PAYLOAD_BYTES:
                log(f"payload-too-large：声明 {length} 字节 > 上限 {MAX_PAYLOAD_BYTES}，丢帧")
                del self._buf[:m.end()]
                continue

            body_start = m.end()
            if len(self._buf) - body_start < length:
                return None                                  # 半包，等更多字节
            body = bytes(self._buf[body_start:body_start + length])
            del self._buf[:body_start + length]
            return body, "content-length"


# =============================================================================
# 三、JSON-RPC 2.0 分发
# =============================================================================

# 协议级错误码（JSON-RPC 2.0 标准段）。
# 关键设计：**业务失败不用 JSON-RPC error**，而是放在 result 里返回
# `{"ok": false, "code": "NO_SESSION", ...}` 信封。
# 理由：业务失败对模型是"可自行纠正的状况"，必须带上 code/hint/summary_md；
# 而 JSON-RPC error 只有 code+message 两个字段，装不下补救指令。
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


def _rpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str,
               data: Optional[Any] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _env_info() -> Dict[str, Any]:
    """运行环境自述 —— 出现「本地能跑、dsh 里不能跑」时第一手排查依据。"""
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable.replace("\\", "/"),
        "cwd": os.getcwd().replace("\\", "/"),
        "pid": os.getpid(),
    }
    for mod in ("pandas", "numpy", "plotly"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            info[mod] = None
    return info


def handle_request(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    处理一条 JSON-RPC 请求，返回响应（通知类消息返回 None）。

    支持的方法：
        initialize   握商：返回协议版本、环境自述、工具数
        tools/list   列出 11 个工具（含 input_schema / output_schema）
        tools/call   执行工具，result 即架构 §4.0 信封
        status       各工具 handler 是否已可用（QA 用）
        ping         心跳
        shutdown     优雅退出
    """
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(req_id, ERR_INVALID_PARAMS, "params 必须是对象。")

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocol_version": PROTOCOL_VERSION,
            "server": SERVER_NAME,
            "contract_version": _registry.CONTRACT_VERSION,
            "tools_count": len(_registry.TOOL_SPECS),
            "tool_names": list(_registry.TOOL_NAMES),
            "framing": "content-length",
            "env": _env_info(),
        })

    if method in ("tools/list", "tools.list"):
        return _rpc_result(req_id, {
            "tools": _registry.list_tools(
                with_json_schema=bool(params.get("with_schema", True))),
            "count": len(_registry.TOOL_SPECS),
            "contract_version": _registry.CONTRACT_VERSION,
        })

    if method in ("tools/call", "tools.call"):
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _rpc_error(req_id, ERR_INVALID_PARAMS,
                              "tools/call 需要字符串参数 name。")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _rpc_error(req_id, ERR_INVALID_PARAMS,
                              "tools/call 的 arguments 必须是对象。")
        envelope = _registry.call_tool(name, arguments,
                                       session_id=params.get("session_id"))
        return _rpc_result(req_id, envelope)

    if method == "status":
        return _rpc_result(req_id, {
            "implemented": _registry.implemented_status(),
            "uptime_s": round(time.time() - _STARTED_AT, 3),
            "env": _env_info(),
        })

    if method == "ping":
        return _rpc_result(req_id, {"pong": True,
                                    "uptime_s": round(time.time() - _STARTED_AT, 3)})

    if method == "shutdown":
        return _rpc_result(req_id, {"bye": True})

    return _rpc_error(req_id, ERR_METHOD_NOT_FOUND, f"未知方法：{method!r}",
                      data={"supported": ["initialize", "tools/list", "tools/call",
                                          "status", "ping", "shutdown"]})


# =============================================================================
# 四、主循环
# =============================================================================


def serve(framing_mode: str = "auto", emit_ready: bool = True) -> int:
    """
    阻塞式主循环：读 stdin → 切帧 → 分发 → 写 stdout。

    `framing_mode`:
        auto            响应镜像请求的分帧（默认，兼顾 TS 桥与手工测试）
        content-length  一律 Content-Length
        line            一律 NDJSON
    """
    reader = FrameReader()
    stdin = sys.stdin.buffer
    default_framing = "line" if framing_mode == "line" else "content-length"

    if emit_ready:
        # 就绪通知（无 id 的通知）。插件等到这一帧才放行首个请求，
        # 从而把「pandas 冷启动 2-3s」与「首个工具调用超时」两件事解耦。
        write_message({"jsonrpc": "2.0", "method": "ready",
                       "params": {"server": SERVER_NAME,
                                  "protocol_version": PROTOCOL_VERSION,
                                  "tools_count": len(_registry.TOOL_SPECS),
                                  "env": _env_info()}},
                      default_framing)

    while True:
        chunk = stdin.read1(65536) if hasattr(stdin, "read1") else stdin.read(65536)
        if not chunk:                                   # stdin 关闭 → 宿主退出
            log("stdin 已关闭，worker 退出。")
            return 0
        reader.feed(chunk)

        while True:
            frame = reader.next_frame()
            if frame is None:
                break
            body, seen_framing = frame
            out_framing = (seen_framing if framing_mode == "auto"
                           else default_framing)
            try:
                msg = json.loads(body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                log("JSON 解析失败：", exc)
                write_message(_rpc_error(None, ERR_PARSE, f"JSON 解析失败：{exc}"),
                              out_framing)
                continue

            if not isinstance(msg, dict) or "method" not in msg:
                write_message(_rpc_error(msg.get("id") if isinstance(msg, dict) else None,
                                         ERR_INVALID_REQUEST,
                                         "请求必须是含 method 字段的对象。"),
                              out_framing)
                continue

            try:
                response = handle_request(msg)
            except Exception as exc:  # noqa: BLE001 - 主循环绝不因单条请求崩掉
                log("分发异常：", traceback.format_exc(limit=6))
                response = _rpc_error(msg.get("id"), ERR_INTERNAL,
                                      f"服务端内部错误：{type(exc).__name__}: {exc}")

            if response is not None:
                write_message(response, out_framing)

            if msg.get("method") == "shutdown":
                log("收到 shutdown，worker 退出。")
                return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="薪酬诊断 Agent 的 Python 常驻 worker（stdio JSON-RPC 2.0）")
    ap.add_argument("--framing", choices=["auto", "content-length", "line"],
                    default="auto",
                    help="响应分帧方式；auto=镜像请求分帧（默认）")
    ap.add_argument("--no-ready", action="store_true",
                    help="不发就绪通知（手工 echo 测试时输出更干净）")
    ap.add_argument("--self-test", action="store_true",
                    help="不读 stdin，直接自检 tools/list 与分帧器")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        return serve(framing_mode=args.framing, emit_ready=not args.no_ready)
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except Exception:  # noqa: BLE001 - 顶层兜底，保证退出码可读
        log("worker 致命错误：", traceback.format_exc(limit=8))
        return 1


def self_test() -> int:
    """
    离线自检（不依赖 stdin，也不依赖任何计算模块交付完成）：
      1. tools/list 必须列出 11 个工具，且每个 schema 都能编译；
      2. FrameReader 必须能在「污染 + 半包 + 粘包」下正确切出全部帧。
    """
    ok = True

    tools = _registry.list_tools()
    log(f"tools/list → {len(tools)} 个工具")
    if len(tools) != 11:
        log("✗ 工具数不是 11"); ok = False
    for t in tools:
        if not t.get("input_schema", {}).get("properties"):
            log("✗ 缺 input_schema：", t["name"]); ok = False
    log("工具清单：", ", ".join(t["name"] for t in tools))

    # 分帧器：垃圾前导 + 粘包 + 半包
    reader = FrameReader()
    f1 = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
    f2 = '{"jsonrpc":"2.0","id":2,"method":"ping","params":{"note":"中文测试"}}'.encode("utf-8")
    reader.feed(b"WARNING: some library printed this\n")
    reader.feed(f"Content-Length: {len(f1)}\r\n\r\n".encode() + f1)
    reader.feed(f"Content-Length: {len(f2)}\r\n\r\n".encode() + f2[:10])
    got = []
    while True:
        fr = reader.next_frame()
        if fr is None:
            break
        got.append(fr[0])
    reader.feed(f2[10:])                     # 半包补齐
    while True:
        fr = reader.next_frame()
        if fr is None:
            break
        got.append(fr[0])
    if got != [f1, f2]:
        log(f"✗ 分帧器结果不符：{got}"); ok = False
    else:
        log("✓ 分帧器通过（前导垃圾 / 粘包 / 半包 / 中文字节数）")

    # 状态探测
    status = _registry.implemented_status()
    ready = [k for k, v in status.items() if v]
    pending = [k for k, v in status.items() if not v]
    log(f"handler 就绪 {len(ready)}/11：{', '.join(ready)}")
    if pending:
        log(f"handler 待交付：{', '.join(pending)}")

    log("自检结果：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

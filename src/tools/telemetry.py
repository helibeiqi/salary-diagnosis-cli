# -*- coding: utf-8 -*-
"""
telemetry.py — 零 PII 调用遥测总线（Phase 0，自主优化架构师）

设计纪律（与项目整体安全基调一致）
--------------------------------------------------------------------------------
* **零行级 PII**：telemetry 里**绝不**写任何薪资数值或员工 ID。可记录的只有
  ``{tool, ok, code, elapsed_ms, session_classification, ts}``。
* **失败静默**：任何异常（磁盘满 / parquet 损坏 / 权限不足）都必须被吞掉，
  遥测**绝不能**让一次正常的工具调用失败或变慢到可感知。
* **可关闭（独立模式零痕迹开关）**：
    - 环境变量 ``COMP_TELEMETRY_OFF=1`` —— 一键零痕迹（独立模式演示必备）。
    - ``config.yaml`` 的 ``telemetry.enabled: false`` —— 仓库级默认关闭。
  env 优先于 config；两者都未禁用时默认开启（测量无害、零 PII）。

落盘：``.state/telemetry.parquet``（已 gitignore）。采用内存缓冲 + 周期性落盘，
避免每次调用都全表读改写（O(n)）拖慢高吞吐的 stdio worker。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List

import pandas as pd

# —— 状态目录解析：复用 session.STATE_DIR 的同一套规则（COMP_STATE_DIR 优先） ——
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_STATE_DIR = os.environ.get("COMP_STATE_DIR") or os.path.join(_PROJECT_ROOT, ".state")

_TELEMETRY_OFF_ENV = "COMP_TELEMETRY_OFF"
_FLUSH_EVERY = 20

# 允许记录的字段白名单（任何超出此集合的键都会被丢弃，双保险防 PII 泄漏）
_ALLOWED_KEYS = frozenset({
    "tool", "ok", "code", "elapsed_ms", "session_classification", "ts",
})

_lock = threading.Lock()
_buffer: List[Dict[str, Any]] = []
_config_cache: Dict[str, Any] = {}


def _enabled() -> bool:
    """env 优先；否则读 config.yaml 的 telemetry.enabled（缺省 True，fail-open）。"""
    if os.environ.get(_TELEMETRY_OFF_ENV):
        return False
    try:
        import yaml  # 延迟导入，避免 --list 等轻路径也拉起 yaml
        cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
        if cfg_path not in _config_cache:
            with open(cfg_path, "r", encoding="utf-8") as f:
                _config_cache[cfg_path] = yaml.safe_load(f) or {}
        return bool(_config_cache[cfg_path].get("telemetry", {}).get("enabled", True))
    except Exception:
        return True  # 读取失败 → fail-open（测量无害）


def _telemetry_path() -> str:
    return os.path.join(_STATE_DIR, "telemetry.parquet")


def emit(event: Dict[str, Any]) -> None:
    """追加一条调用记录到遥测总线。任何异常都被吞掉。"""
    if not _enabled():
        return
    rec = {k: v for k, v in event.items() if k in _ALLOWED_KEYS}
    if "ts" not in rec:
        rec["ts"] = time.time()
    with _lock:
        _buffer.append(rec)
        if len(_buffer) >= _FLUSH_EVERY:
            _flush_locked()


def _flush_locked() -> None:
    """把内存缓冲落盘（调用方需持有 _lock）。"""
    if not _buffer:
        return
    path = _telemetry_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = pd.read_parquet(path) if os.path.exists(path) else pd.DataFrame()
        merged = pd.concat([existing, pd.DataFrame(_buffer)], ignore_index=True)
        merged.to_parquet(path, index=False)
        _buffer.clear()
    except Exception:  # noqa: BLE001 - 遥测失败绝不影响主流程
        pass


def flush_telemetry() -> None:
    """进程退出 / worker 关闭时调用，确保残余缓冲落盘（best-effort）。"""
    with _lock:
        _flush_locked()

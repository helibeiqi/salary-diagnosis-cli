# -*- coding: utf-8 -*-
"""
config_access.py — 轻量 config.yaml 读取（治理层共用，零副作用）
================================================================================

只在需要时（治理层读取 sandbox / fallback 配置）才加载 PyYAML，
避免 ``--list`` / 轻路径无谓拉起依赖。

纪律：**fail-open**。配置缺失 / 解析失败时返回空 dict / 调用方缺省值，
绝不因配置问题拖垮主调用（与 telemetry 模块同款纪律）。
"""
from __future__ import annotations

import os
from typing import Any, Dict

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_CONFIG_CACHE: Dict[str, Any] = {"__loaded__": False, "__data__": {}}


def load_config() -> Dict[str, Any]:
    """读取并缓存 config.yaml（进程内只解析一次）。"""
    if _CONFIG_CACHE["__loaded__"]:
        return _CONFIG_CACHE["__data__"]
    data: Dict[str, Any] = {}
    try:
        import yaml  # 延迟导入：治理层之外的路径不依赖 PyYAML
        cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 - 配置不可用时 fail-open
        data = {}
    _CONFIG_CACHE["__data__"] = data
    _CONFIG_CACHE["__loaded__"] = True
    return data


def get(path: str, default: Any = None) -> Any:
    """
    按点路径读取嵌套配置，例如 ``get("sandbox.max_concurrent", 2)``。

    任一层缺失 / 非 dict → 返回 ``default``。
    """
    node: Any = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

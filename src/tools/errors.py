# -*- coding: utf-8 -*-
"""
errors.py — 薪酬诊断 Agent 的统一异常体系与工具返回值包装
================================================================================

设计目的
--------------------------------------------------------------------------------
在 Function Calling 架构里，**Python 工具抛出的裸异常对模型是灾难**：
栈回溯既消耗 Token，又无法指导模型「下一步该干什么」。
因此本项目定下一条硬纪律（架构 §8 B2）：

    任何工具函数都不得向外抛裸异常；一律返回
        {"ok": False, "error": {"code": ..., "message": ..., "hint": ..., "details": ...}}

其中 `hint` 是**给模型的补救指令**（例如「请先调用 load_salary_data 建立会话」），
模型读到 hint 后能自行纠正调用链，这正是 Agent 工具与普通过程函数的本质区别。

同时，模块内部仍保留异常类体系，原因有三：
1. 底层函数（如 session 读写）需要能被上层 try-except 按类型捕获；
2. 异常对象携带 `details`（结构化上下文，如缺失的列名列表），比字符串更好机读；
3. `tool_guard` 装饰器能把「异常写法」自动降级成「返回值写法」，
   让底层代码可以写得自然，对外仍然安全。

异常层级
--------------------------------------------------------------------------------
    CompToolError                 基类，code="COMP_ERROR"
      ├─ FileNotFound             文件不存在 / 路径不可读，code="FILE_NOT_FOUND"
      ├─ ColumnMissing            必需的列在数据里找不到，code="COLUMN_MISSING"
      ├─ MappingNotConfirmed      字段映射尚未确认就调用下游计算，code="MAPPING_NOT_CONFIRMED"
      ├─ InvalidParameter         入参类型/取值非法，code="INVALID_PARAMETER"
      ├─ SessionNotFound          会话 ID 不存在或已过期，code="SESSION_NOT_FOUND"
      └─ UpstreamMissing          前置步骤的产物缺失（如还没生成带宽就要算 CR），code="UPSTREAM_MISSING"
"""

from __future__ import annotations

import functools
import traceback
from typing import Any, Callable, Dict, Optional

# 工具返回值的两个固定键名，全项目统一，便于插件层与前端解析
KEY_OK = "ok"
KEY_ERROR = "error"


# =============================================================================
# 一、异常类体系
# =============================================================================


class CompToolError(Exception):
    """
    所有项目异常的基类。

    参数
    ----------
    message : str
        面向人/模型的中文错误描述。
    hint : str, optional
        补救建议 —— **这是给模型的行动指令**，例如「请先调用 confirm_mapping 确认字段映射」。
    details : dict, optional
        结构化上下文（缺失列名、尝试过的路径等），便于模型与日志机读。
    """

    code: str = "COMP_ERROR"
    default_hint: str = "请检查调用参数后重试。"

    def __init__(self, message: str, hint: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = str(message)
        self.hint = hint or self.default_hint
        self.details: Dict[str, Any] = details or {}

    def to_payload(self) -> Dict[str, Any]:
        """转成放进返回值的 error 字典。"""
        return error_payload(self.code, self.message, self.hint, self.details)

    def to_result(self) -> Dict[str, Any]:
        """转成完整的工具返回值 {"ok": False, "error": {...}}。"""
        return {KEY_OK: False, KEY_ERROR: self.to_payload()}

    def __str__(self) -> str:  # pragma: no cover - 仅在日志/调试时用到
        return f"[{self.code}] {self.message}"


class FileNotFound(CompToolError):
    """文件不存在、路径不可读，或 Excel 缺少读取引擎。"""

    code = "FILE_NOT_FOUND"
    default_hint = "请检查文件路径是否写全（Windows 下建议用 C:/... 正斜杠），并确认文件未被 Excel 独占占用。"


class ColumnMissing(CompToolError):
    """必需的列在数据表里找不到（字段映射未覆盖到必填字段时会触发）。"""

    code = "COLUMN_MISSING"
    default_hint = "请调用 load_salary_data 查看列名，再用 confirm_mapping 把实际列名映射到标准字段。"


class MappingNotConfirmed(CompToolError):
    """字段映射尚未确认就调用了下游计算。"""

    code = "MAPPING_NOT_CONFIRMED"
    default_hint = "请先调用 confirm_mapping(session_id, mapping) 确认字段映射，再执行本步骤。"


class InvalidParameter(CompToolError):
    """入参类型错误、取值越界，或必填入参缺失。"""

    code = "INVALID_PARAMETER"
    default_hint = "请检查参数类型与取值范围（详见该工具的 JSON Schema 说明）。"


class SessionNotFound(CompToolError):
    """会话 ID 不存在 / 已被清理 / 状态文件损坏。"""

    code = "SESSION_NOT_FOUND"
    default_hint = "会话可能已过期，请重新调用 load_salary_data 建立新会话。"


class UpstreamMissing(CompToolError):
    """前置步骤产物缺失（例如还没生成带宽就要判定红绿圈）。"""

    code = "UPSTREAM_MISSING"
    default_hint = "请先完成前置步骤（如 generate_band 生成带宽），再调用本工具。"


# 常见异常类名的英文别名，方便跨模块书写习惯统一
FileNotFoundError_ = FileNotFound        # 避免与内建 FileNotFoundError 混淆时的显式别名
ColumnMissingError = ColumnMissing
MappingNotConfirmedError = MappingNotConfirmed
InvalidParameterError = InvalidParameter
SessionNotFoundError = SessionNotFound
UpstreamMissingError = UpstreamMissing


# =============================================================================
# 二、统一返回值构造
# =============================================================================


def error_payload(code: str, message: str, hint: Optional[str] = None,
                  details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    构造 error 字段的标准结构。

    四个字段各有明确分工，缺一不可：
        code    —— 机读错误码，前端/测试据此分支处理
        message —— 人读错误描述，说清「哪里错了」
        hint    —— 给模型的补救指令，说清「下一步做什么」
        details —— 结构化上下文（缺失列、异常样例等），便于模型做参数修正
    """
    payload: Dict[str, Any] = {
        "code": code,
        "message": str(message),
        "hint": hint or "请检查调用参数后重试。",
    }
    if details:
        payload["details"] = details
    return payload


def error_result(exc: Any, message: Optional[str] = None,
                 hint: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    把「异常对象」或「错误码字符串」打包成标准工具返回值。

    参数
    ----------
    exc : CompToolError | BaseException | str
        - CompToolError：直接取其 code/message/hint/details
        - 其他 Exception：统一降级为 COMP_ERROR，原始异常类型放进 details 便于排查
        - str：当作错误码 message 使用
    """
    if isinstance(exc, CompToolError):
        payload = exc.to_payload()
        if details:
            payload.setdefault("details", {}).update(details)
        if hint:
            payload["hint"] = hint
        if message:
            payload["message"] = message
        return {KEY_OK: False, KEY_ERROR: payload}

    if isinstance(exc, BaseException):
        # 非项目异常：不外泄栈回溯（含路径信息且有安全风险），只保留类型与摘要
        merged = {"exception_type": type(exc).__name__}
        if details:
            merged.update(details)
        return {
            KEY_OK: False,
            KEY_ERROR: error_payload(
                code="COMP_ERROR",
                message=message or f"处理过程中发生未预期错误：{type(exc).__name__}: {exc}",
                hint=hint or "请根据 message 调整输入重试；若持续失败，请简化参数或分步执行。",
                details=merged,
            ),
        }

    # exc 是字符串：当作错误描述
    return {
        KEY_OK: False,
        KEY_ERROR: error_payload(
            code=(details or {}).pop("code", "COMP_ERROR") if details else "COMP_ERROR",
            message=message or str(exc),
            hint=hint or "请检查调用参数后重试。",
            details=details,
        ),
    }


def ok_result(**fields: Any) -> Dict[str, Any]:
    """
    构造成功返回值：{"ok": True, ...业务字段}。

    统一用关键字参数传入业务字段，避免各工具自己拼字典导致键名风格不一致。
    """
    result: Dict[str, Any] = {KEY_OK: True}
    result.update(fields)
    return result


def tool_guard(func: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    """
    装饰器：把「会抛异常的底层函数」包装成「永远返回标准字典的工具函数」。

    - 若函数已返回 dict 且带 ok 键 → 原样透传（允许底层自己精确控制返回）
    - 若函数正常返回但非 dict → 包成 ok_result(data=...)
    - 若抛出 CompToolError → 转成其对应错误码的失败返回
    - 若抛出其他异常 → 降级为 COMP_ERROR，绝不把栈回溯抛给模型

    注意：被包装函数的 docstring/签名通过 functools.wraps 保留，
    便于插件层自动生成 JSON Schema 描述。
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            value = func(*args, **kwargs)
        except CompToolError as exc:
            return exc.to_result()
        except Exception as exc:  # noqa: BLE001 - 故意兜住一切，工具层不允许裸抛
            return error_result(
                exc,
                details={"where": f"{func.__module__}.{func.__qualname__}"},
            )
        if isinstance(value, dict) and KEY_OK in value:
            return value
        if isinstance(value, dict):
            return ok_result(**value)
        return ok_result(data=value)

    return wrapper


def format_traceback(exc: BaseException, limit: int = 8) -> str:
    """
    把异常栈格式化成简短字符串，仅用于**本地日志**。

    安全纪律：本函数的返回值**绝不能**放进工具返回给模型的内容，
    因为栈里会带出绝对路径与数据内容。
    """
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__, limit=limit)
    return "".join(lines[-limit:])

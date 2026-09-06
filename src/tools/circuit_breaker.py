# -*- coding: utf-8 -*-
"""
circuit_breaker.py — 数据分级感知熔断器（Phase 3 政策引擎，架构 §4.2）
================================================================================

这是"自愈 + 自进化"的守门员：**绝不**在真实薪酬数据路径上自动路由到云端。

核心语义（★ 守住红线）
--------------------------------------------------------------------------------
  * 维护每 provider 的失败计数 / 429 / 402 速率（滑动窗口）；
  * 触发跳闸后，动作**按当前会话数据分级**：
        real                       → HARD_STOP + 告警（绝不自动回退云端）
        sanitized / simulated      → FALLBACK（按 config chain 降级到最便宜安全 provider）
        unknown / none             → 保守按 FALLBACK 处理（无分级信息时不得冒险上云）
  * 本模块**只做决策，不做任何网络调用**；强制点在 TS ``service.ts`` 调用模型前咨询它
    （见设计文档 §9 / §4.2）。Python 侧同时把它用于"沙箱常驻 worker 健康"的演示性接入。
  * **零 PII**：只记录 provider 名与计数，不触碰任何薪资 / 员工数据。

为什么"裸奔式自动上云"必须否决
--------------------------------------------------------------------------------
原 ``fallback.enabled:false`` 是安全的但非自治。本熔断器把"自治容错"**适配到薪酬
敏感语境**：真实数据路径永远硬停，只有脱敏/模拟数据才允许降级，从而既守住红线、
又让演示场景具备自愈能力。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 决策动作
ACTION_ALLOW = "ALLOW"
ACTION_FALLBACK = "FALLBACK"
ACTION_HARD_STOP = "HARD_STOP"

# 受支持的数据分级（与 registry._resolve_classification 返回值对齐）
CLS_REAL = "real"
CLS_SANITIZED = "sanitized"
CLS_SIMULATED = "simulated"


@dataclass
class BreakerDecision:
    """一次熔断决策。"""

    action: str
    provider: str
    reason: str
    alert: bool = False
    fallback_provider: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "provider": self.provider,
            "reason": self.reason,
            "alert": self.alert,
            "fallback_provider": self.fallback_provider,
        }


@dataclass
class _ProviderState:
    failures: List[float] = field(default_factory=list)   # 失败时间戳
    tripped: bool = False
    tripped_at: float = 0.0
    consecutive_success: int = 0


class CircuitBreaker:
    """
    数据分级感知熔断器（单 provider 状态机）。

    线程安全：本 CLI 主链路（server.py 单线程串行）无需锁；此处仍用一把轻锁，
    以防未来并发路径误用。所有方法失败静默、绝不抛异常影响主调用。
    """

    def __init__(self,
                 failure_threshold: int = 5,
                 window_s: int = 60,
                 half_open_after_s: int = 30,
                 on_real: str = "hard_stop",
                 on_sanitized: str = "fallback",
                 fallback_chain: Optional[List[str]] = None,
                 enabled: bool = True) -> None:
        self.failure_threshold = int(failure_threshold)
        self.window_s = int(window_s)
        self.half_open_after_s = int(half_open_after_s)
        self.on_real = on_real                 # "hard_stop" | "fallback"
        self.on_sanitized = on_sanitized       # "fallback" | "hard_stop"
        self.fallback_chain = list(fallback_chain or [])
        self.enabled = bool(enabled)
        self._states: Dict[str, _ProviderState] = {}
        self._lock = None  # 延迟创建，避免无谓 import threading

    # ---- 内部：锁（懒创建）----
    def _get_lock(self):
        if self._lock is None:
            import threading
            self._lock = threading.Lock()
        return self._lock

    def _state(self, provider: str) -> _ProviderState:
        st = self._states.get(provider)
        if st is None:
            st = _ProviderState()
            self._states[provider] = st
        return st

    # ---- 事件上报 ----
    def report_failure(self, provider: str, kind: str = "error") -> None:
        """上报一次失败（kind ∈ error / 429 / 402）。"""
        if not self.enabled:
            return
        try:
            with self._get_lock():
                st = self._state(provider)
                now = time.time()
                st.failures.append(now)
                cutoff = now - self.window_s
                st.failures = [t for t in st.failures if t >= cutoff]
                st.consecutive_success = 0
                if not st.tripped and len(st.failures) >= self.failure_threshold:
                    st.tripped = True
                    st.tripped_at = now
        except Exception:  # noqa: BLE001 - 熔断逻辑本身绝不能拖垮主调用
            pass

    def record_success(self, provider: str) -> None:
        """上报一次成功（用于半开探测恢复）。"""
        if not self.enabled:
            return
        try:
            with self._get_lock():
                st = self._state(provider)
                st.consecutive_success += 1
                if (st.tripped
                        and (time.time() - st.tripped_at) >= self.half_open_after_s
                        and st.consecutive_success >= 2):
                    st.tripped = False
                    st.failures = []
                    st.consecutive_success = 0
        except Exception:  # noqa: BLE001
            pass

    def is_tripped(self, provider: str) -> bool:
        try:
            with self._get_lock():
                return self._state(provider).tripped
        except Exception:  # noqa: BLE001
            return False

    # ---- 决策 ----
    def _next_in_chain(self, provider: str) -> Optional[str]:
        if not self.fallback_chain:
            return None
        if provider not in self.fallback_chain:
            return self.fallback_chain[0]
        idx = self.fallback_chain.index(provider)
        for nxt in self.fallback_chain[idx + 1:]:
            return nxt
        return None

    def decide(self, provider: str, session_classification: Optional[str]) -> BreakerDecision:
        """
        返回对 ``provider`` 在当前数据分级下的决策。

        ``session_classification`` ∈ {real, sanitized, simulated, unknown, none}
        """
        try:
            with self._get_lock():
                tripped = self._state(provider).tripped
        except Exception:  # noqa: BLE001
            tripped = False

        if not tripped:
            return BreakerDecision(ACTION_ALLOW, provider,
                                   "provider 健康，正常放行。", alert=False)

        cls = (session_classification or "unknown")
        # real → 硬停 + 告警，绝不自动上云
        if cls == CLS_REAL:
            return BreakerDecision(
                ACTION_HARD_STOP, provider,
                "真实数据路径：provider 已跳闸，按安全策略硬停，绝不自动回退云端。",
                alert=True)
        # sanitized / simulated / unknown / none → 降级
        fb = self._next_in_chain(provider)
        if cls in (CLS_SANITIZED, CLS_SIMULATED):
            policy = self.on_sanitized
        else:
            policy = "fallback"  # unknown / none 保守降级，但绝不无脑上云
        if policy == "hard_stop" or fb is None:
            return BreakerDecision(
                ACTION_HARD_STOP, provider,
                "非真实数据路径，但 chain 已到末端或无降级目标，安全起见硬停。",
                alert=(cls in (CLS_SANITIZED, CLS_SIMULATED)))
        return BreakerDecision(
            ACTION_FALLBACK, provider,
            f"非真实数据路径：provider 已跳闸，按 chain 降级到 {fb}。",
            alert=False, fallback_provider=fb)


# =============================================================================
# 进程内单例（读取 config.yaml 的 models.fallback 段）
# =============================================================================

_BREAKER: Optional[CircuitBreaker] = None


def get_breaker() -> CircuitBreaker:
    """返回（并缓存）进程内唯一熔断器，配置来自 config.yaml。"""
    global _BREAKER
    if _BREAKER is not None:
        return _BREAKER
    try:
        from .config_access import get as cfg_get
        cb = CircuitBreaker(
            failure_threshold=cfg_get(
                "models.fallback.circuit_breaker.failure_threshold", 5),
            window_s=cfg_get("models.fallback.circuit_breaker.window_s", 60),
            half_open_after_s=cfg_get(
                "models.fallback.circuit_breaker.half_open_after_s", 30),
            on_real=cfg_get("models.fallback.circuit_breaker.on_real", "hard_stop"),
            on_sanitized=cfg_get(
                "models.fallback.circuit_breaker.on_sanitized", "fallback"),
            fallback_chain=cfg_get(
                "models.fallback.chain", ["ollama-local", "vllm-local"]),
            enabled=cfg_get("models.fallback.circuit_breaker.enabled", True),
        )
    except Exception:  # noqa: BLE001 - 配置缺失则给安全缺省值
        cb = CircuitBreaker()
    _BREAKER = cb
    return _BREAKER


def reset_breaker() -> None:
    """测试辅助：清空进程内单例缓存。"""
    global _BREAKER
    _BREAKER = None

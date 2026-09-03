# -*- coding: utf-8 -*-
"""
shadow_router.py — 影子路由骨架 + 数据分级门控（Phase 4，自主优化架构师）
================================================================================

这是「自主优化」的**路由决策引擎**：它决定某个子任务（mapping / narration）
在特定数据分级下，应该走 baseline 大模型还是候选小模型。

核心纪律（★ 守住红线，与 Phase 3 熔断器同一套安全哲学）
--------------------------------------------------------------------------------
1. **真实数据永不参与路由/晋升**：``classification == "real"`` 时，无论 mapping
   还是 narration，**永远返回 baseline**，绝不把真实薪资路径切到任何候选/
   云端，也绝不因"候选更便宜"而晋升。晋升只在 ``eligible_only_on``
   （默认 sanitized / simulated）内生效。
2. **narration 子任务永远 baseline**：叙述/解读需要大模型语义，候选小模型
   不承接（避免"省了钱丢了准"）。
3. **映射子任务可晋升到候选**：仅当影子测试证明候选在 ≥1000 次执行上
   ``准确率 ≥ baseline−2% 且 成本↓≥50% 且 延迟↓≥40%`` 时才自动晋升，
   把"映射"这类确定性可评分任务路由到便宜模型。
4. **异常即回滚**：候选准确率骤降（低于 baseline−阈值）→ 自动回滚到 baseline
   + 记录告警，**不靠人记得切回去**。
5. **零 PII**：状态文件只记 provider 名 / 分数 / 延迟 / 成本 / 分级，绝不记
   薪资数值或员工 ID。本模块**不读薪资文件、不发起任何网络调用**。

强制点仍在 TS ``service.ts``（模型调用前咨询本模块，仿 Phase 3 §9.3）：
Python 侧是决策逻辑的权威实现，既可被 TS 直译，也可经
``python -m src.tools.shadow_router --route '{...}'`` 调用同一份代码。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List, Optional

# 条件导入（兼容 `python -m src.tools.shadow_router` 与脚本直跑两种形态，
# 与 sandbox.py 同款写法）
if __package__ in (None, ""):
    from config_access import get as cfg_get
else:
    from .config_access import get as cfg_get

# 项目根 & 状态目录（复用 .state/，已 gitignore）。用 abspath(__file__) 避免
# 相对 __file__ 在脚本直跑时把路径走到仓库外。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_STATE_DIR = os.environ.get("COMP_STATE_DIR") or os.path.join(_PROJECT_ROOT, ".state")
_STATE_FILE = os.path.join(_STATE_DIR, "shadow_router.json")

# 回滚判定的「近窗口」长度：仅看最近 N 次影子评估的候选准确率，
# 以灵敏捕获「骤降」而非被长期均值稀释（避免单点抖动误伤，也避免需数百次才回滚）。
_RECENT_N = 50

# 缺省配置（config.yaml 缺段时 fail-open 用）
_DEF = {
    "enabled": True,
    "baseline_provider": "deepseek-r1:14b",
    "candidate_provider": "qwen2.5-7b",
    "task_routing": {"mapping": "candidate", "narration": "baseline"},
    "promotion": {
        "min_shadow_runs": 1000,
        "max_acc_drop_pct": 2.0,
        "min_cost_reduction_pct": 50.0,
        "min_latency_reduction_pct": 40.0,
    },
    "weights": {"mapping": {"baseline": 1.0, "candidate": 0.0}},
    "scoring": {"w1": 0.01, "w2": 1e-4},
    "eligible_only_on": ["sanitized", "simulated"],
}


# =============================================================================
# 状态结构
# =============================================================================
def _default_state() -> Dict[str, Any]:
    return {
        "promoted": False,
        "weights": {"mapping": {"baseline": 1.0, "candidate": 0.0}},
        "stats": {
            "count": 0,
            "baseline_acc_sum": 0.0,
            "candidate_acc_sum": 0.0,
            "baseline_latency_sum": 0.0,
            "candidate_latency_sum": 0.0,
            "baseline_cost_sum": 0.0,
            "candidate_cost_sum": 0.0,
            "last_classification": None,
            "recent_cand": [],
            "recent_base": [],
        },
        "last_alert": None,
        "updated_at": 0.0,
    }


# =============================================================================
# ShadowRouter
# =============================================================================
class ShadowRouter:
    """进程内单例的影子路由决策引擎（配置来自 config.yaml 的 shadow_routing 段）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(_DEF)
        if config:
            for k, v in config.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.baseline = cfg["baseline_provider"]
        self.candidate = cfg["candidate_provider"]
        self.task_routing = cfg["task_routing"]
        self.promo = cfg["promotion"]
        self.scoring = cfg["scoring"]
        self.eligible_only_on = cfg.get("eligible_only_on", ["sanitized", "simulated"])
        self._state = self._load_state()

    # ---- 状态持久化（.state/shadow_router.json，gitignore）----
    def _load_state(self) -> Dict[str, Any]:
        try:
            if os.path.exists(_STATE_FILE):
                with open(_STATE_FILE, "r", encoding="utf-8") as f:
                    st = json.load(f)
                # 合并缺省，补齐可能缺失的键
                base = _default_state()
                base.update(st)
                base["stats"] = {**_default_state()["stats"], **st.get("stats", {})}
                base["weights"] = {**_default_state()["weights"], **st.get("weights", {})}
                return base
        except Exception:  # noqa: BLE001 - 状态损坏则回到干净态，绝不拖垮主调用
            pass
        return _default_state()

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
            self._state["updated_at"] = time.time()
            with open(_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    # ---- 影子执行记录 ----
    def record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """
        记录一次影子评估（候选与 baseline 同时跑，候选只评分不生效）。

        :param rec: ``{task, baseline_acc, candidate_acc, baseline_latency_ms,
                      candidate_latency_ms, baseline_cost_usd, candidate_cost_usd,
                      classification}``
        仅作用于 sanitized/simulated 数据（调用方负责在 real 路径不上报）。
        """
        s = self._state["stats"]
        s["count"] = int(s.get("count", 0)) + 1
        s["baseline_acc_sum"] = float(s.get("baseline_acc_sum", 0.0)) + float(rec.get("baseline_acc", 0.0))
        s["candidate_acc_sum"] = float(s.get("candidate_acc_sum", 0.0)) + float(rec.get("candidate_acc", 0.0))
        s["baseline_latency_sum"] = float(s.get("baseline_latency_sum", 0.0)) + float(rec.get("baseline_latency_ms", 0.0))
        s["candidate_latency_sum"] = float(s.get("candidate_latency_sum", 0.0)) + float(rec.get("candidate_latency_ms", 0.0))
        s["baseline_cost_sum"] = float(s.get("baseline_cost_sum", 0.0)) + float(rec.get("baseline_cost_usd", 0.0))
        s["candidate_cost_sum"] = float(s.get("candidate_cost_sum", 0.0)) + float(rec.get("candidate_cost_usd", 0.0))
        s["last_classification"] = rec.get("classification")
        # 近窗口（用于回滚判定）：只保留最近 _RECENT_N 次候选/基线准确率
        rc = list(s.get("recent_cand", []))
        rb = list(s.get("recent_base", []))
        rc.append(float(rec.get("candidate_acc", 0.0)))
        rb.append(float(rec.get("baseline_acc", 0.0)))
        while len(rc) > _RECENT_N:
            rc.pop(0)
        while len(rb) > _RECENT_N:
            rb.pop(0)
        s["recent_cand"] = rc
        s["recent_base"] = rb
        self._save_state()
        return self.evaluate()

    # ---- 晋升资格判定 + 自动晋升/回滚 ----
    def evaluate(self) -> Dict[str, Any]:
        """
        基于累计影子统计，重新计算晋升资格；必要时自动晋升或回滚。
        返回判定详情（含本次动作）。
        """
        s = self._state["stats"]
        n = int(s.get("count", 0))
        detail: Dict[str, Any] = {
            "count": n,
            "baseline_acc": None,
            "candidate_acc": None,
            "cost_reduction_pct": None,
            "latency_reduction_pct": None,
            "eligible": False,
            "action": "none",
            "promoted": bool(self._state.get("promoted", False)),
        }
        if n == 0:
            return detail

        b_acc = s["baseline_acc_sum"] / n
        c_acc = s["candidate_acc_sum"] / n
        b_lat = s["baseline_latency_sum"] / n
        c_lat = s["candidate_latency_sum"] / n
        b_cost = s["baseline_cost_sum"] / n
        c_cost = s["candidate_cost_sum"] / n
        cost_red = ((b_cost - c_cost) / b_cost * 100.0) if b_cost > 0 else 0.0
        lat_red = ((b_lat - c_lat) / b_lat * 100.0) if b_lat > 0 else 0.0

        # 近窗口均值（回滚判定用，灵敏捕获骤降）
        rc = [x for x in s.get("recent_cand", []) if x is not None]
        rb = [x for x in s.get("recent_base", []) if x is not None]
        recent_cand = (sum(rc) / len(rc)) if rc else c_acc
        recent_base = (sum(rb) / len(rb)) if rb else b_acc

        detail.update({
            "baseline_acc": round(b_acc, 6),
            "candidate_acc": round(c_acc, 6),
            "cost_reduction_pct": round(cost_red, 2),
            "latency_reduction_pct": round(lat_red, 2),
            "recent_cand_acc": round(recent_cand, 6),
            "recent_base_acc": round(recent_base, 6),
        })

        min_runs = float(self.promo.get("min_shadow_runs", 1000))
        max_drop = float(self.promo.get("max_acc_drop_pct", 2.0))
        min_cost = float(self.promo.get("min_cost_reduction_pct", 50.0))
        min_lat = float(self.promo.get("min_latency_reduction_pct", 40.0))

        # 近窗口也必须健康才允许晋升：防止「长期均值尚可但近期骤降」时反复横跳
        # （回滚后又被 aggregate 误判为达标而立刻再晋升）。
        recent_ok = recent_cand >= (recent_base - max_drop / 100.0)

        meets = (n >= min_runs
                 and c_acc >= (b_acc - max_drop / 100.0)
                 and cost_red >= min_cost
                 and lat_red >= min_lat
                 and recent_ok)

        if meets:
            if not self._state.get("promoted", False):
                self._state["promoted"] = True
                self._state["weights"]["mapping"] = {"baseline": 0.0, "candidate": 1.0}
                detail["action"] = "promoted"
                detail["promoted"] = True
                self._save_state()
            else:
                detail["action"] = "stay_promoted"
                detail["promoted"] = True
        else:
            # 异常即回滚：已晋升且**近窗口**候选准确率跌破红线 → 回退 baseline
            # 用近窗口而非长期均值，避免被历史好记录稀释而迟迟不回滚（骤降灵敏）。
            if (self._state.get("promoted", False)
                    and recent_cand < (recent_base - max_drop / 100.0)):
                self._state["promoted"] = False
                self._state["weights"]["mapping"] = {"baseline": 1.0, "candidate": 0.0}
                self._state["last_alert"] = (
                    f"[shadow] 候选近窗口准确率骤降（{recent_cand:.4f} < baseline "
                    f"{recent_base:.4f} − {max_drop}%），已自动回滚到 baseline 并告警。")
                detail["action"] = "rollback"
                detail["promoted"] = False
                self._save_state()
            else:
                detail["action"] = "not_eligible"
                detail["promoted"] = bool(self._state.get("promoted", False))
        return detail

    # ---- 路由决策（★ 分级门控）----
    def route(self, task_type: str, classification: Optional[str]) -> Dict[str, Any]:
        """
        返回某子任务在某数据分级下应走的 provider。

        :param task_type: ``mapping`` / ``narration``
        :param classification: ``real`` / ``sanitized`` / ``simulated`` / ``unknown`` / ``none``
        :return: ``{provider, reason, promoted, gated}``
        """
        cls = (classification or "unknown")

        # 红线 1：真实数据 —— 任何子任务都走 baseline，绝不晋升/切候选
        if cls == "real":
            return {
                "provider": self.baseline,
                "reason": "real 数据路径：按安全策略永远走 baseline，绝不路由候选/云端。",
                "promoted": False,
                "gated": True,
            }

        # narration 子任务永远 baseline（大模型语义，候选不承接）
        if task_type == "narration":
            return {
                "provider": self.baseline,
                "reason": "narration 子任务需大模型语义，固定走 baseline。",
                "promoted": False,
                "gated": False,
            }

        # mapping 子任务：已晋升且分级在白名单内 → 候选；否则 baseline
        if task_type == "mapping":
            promoted = bool(self._state.get("promoted", False))
            if promoted and cls in self.eligible_only_on:
                return {
                    "provider": self.candidate,
                    "reason": f"mapping 子任务已晋升且数据分级={cls}∈白名单，路由到候选小模型。",
                    "promoted": True,
                    "gated": False,
                }
            return {
                "provider": self.baseline,
                "reason": ("mapping 子任务未晋升" if not promoted
                           else f"mapping 已晋升但分级={cls} 不在白名单 {self.eligible_only_on}") +
                          "，走 baseline。",
                "promoted": promoted,
                "gated": not promoted,
            }

        # 未知子任务 → 保守 baseline
        return {
            "provider": self.baseline,
            "reason": f"未知子任务类型 {task_type!r}，保守走 baseline。",
            "promoted": False,
            "gated": False,
        }

    # ---- 调试/运维 ----
    def reset_state(self) -> None:
        """清空晋升与累计统计（测试/回滚演示用）。"""
        self._state = _default_state()
        self._save_state()

    def show_state(self) -> Dict[str, Any]:
        return self._state


# =============================================================================
# 进程内单例
# =============================================================================
_ROUTER: Optional[ShadowRouter] = None


def get_router() -> ShadowRouter:
    """返回（并缓存）进程内唯一影子路由器，配置来自 config.yaml。"""
    global _ROUTER
    if _ROUTER is not None:
        return _ROUTER
    try:
        cfg = cfg_get("shadow_routing", None)
    except Exception:  # noqa: BLE001
        cfg = None
    _ROUTER = ShadowRouter(cfg if isinstance(cfg, dict) else None)
    return _ROUTER


def reset_router() -> None:
    """测试辅助：清空单例缓存（不影响落盘状态文件）。"""
    global _ROUTER
    _ROUTER = None


# =============================================================================
# 自测（无 pytest 也能跑）
# =============================================================================
def _self_test() -> int:
    print("=" * 70)
    print("shadow_router 自测")
    print("=" * 70)
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal fails
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    # 用独立内存态路由（不污染落盘状态）：直接构造
    cfg = dict(_DEF)
    r = ShadowRouter(cfg)
    r.reset_state()

    # 1) 红线：real 分级任何子任务都 baseline
    d = r.route("mapping", "real")
    check("real 分级 mapping 永不路由候选", d["provider"] == r.baseline and d["gated"])
    d = r.route("narration", "real")
    check("real 分级 narration 走 baseline", d["provider"] == r.baseline)

    # 2) narration 永远 baseline（即便已晋升）
    r._state["promoted"] = True
    d = r.route("narration", "simulated")
    check("narration 即便晋升也走 baseline", d["provider"] == r.baseline and not d["promoted"])

    # 3) 未晋升时 mapping 走 baseline
    r._state["promoted"] = False
    d = r.route("mapping", "simulated")
    check("未晋升 mapping 走 baseline", d["provider"] == r.baseline and not d["promoted"])

    # 4) 晋升后 simulated 走候选；real 仍 baseline
    r._state["promoted"] = True
    r._state["weights"]["mapping"] = {"baseline": 0.0, "candidate": 1.0}
    d = r.route("mapping", "simulated")
    check("晋升后 simulated mapping 路由候选", d["provider"] == r.candidate and d["promoted"])
    d = r.route("mapping", "real")
    check("晋升后 real mapping 仍强制 baseline", d["provider"] == r.baseline and d["gated"])

    # 5) unknown 分级即便晋升也不路由候选（保守）
    d = r.route("mapping", "unknown")
    check("unknown 分级不路由候选（保守）", d["provider"] == r.baseline)

    # 6) 晋升资格：满足四项阈值 → promoted
    r2 = ShadowRouter(cfg)
    r2.reset_state()
    for _ in range(int(cfg["promotion"]["min_shadow_runs"])):
        r2.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.95,   # 仅低 1% < 2% 阈值
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,  # ↓50% >40%
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,   # ↓75% >50%
            "classification": "simulated",
        })
    st = r2.show_state()
    check("满足阈值后自动晋升", st["promoted"] is True)
    check("晋升后权重 mapping→candidate", st["weights"]["mapping"]["candidate"] == 1.0)

    # 7) 异常回滚：候选准确率骤降（近窗口持续劣化）→ 回滚
    #    注意：回滚基于「近窗口」均值，需喂满窗口的劣化样本才触发，
    #    避免单点抖动误伤（这正是想要的 anti-flap 行为）。
    for _ in range(_RECENT_N):
        r2.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.80,  # 远低于 baseline-2%
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,
            "classification": "simulated",
        })
    st = r2.show_state()
    check("候选准确率骤降触发回滚", st["promoted"] is False)
    check("回滚后权重复位 baseline", st["weights"]["mapping"]["baseline"] == 1.0)
    check("回滚写入告警", st.get("last_alert") is not None)

    # 8) 不足阈值次数不晋升
    r3 = ShadowRouter(cfg)
    r3.reset_state()
    for _ in range(10):  # 远小于 min_shadow_runs
        r3.record({
            "task": "mapping",
            "baseline_acc": 0.96, "candidate_acc": 0.95,
            "baseline_latency_ms": 1200, "candidate_latency_ms": 600,
            "baseline_cost_usd": 0.004, "candidate_cost_usd": 0.001,
            "classification": "simulated",
        })
    check("次数不足不晋升", r3.show_state()["promoted"] is False)

    print("=" * 70)
    print(f"自测汇总：{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    print("=" * 70)
    return 0 if fails == 0 else 1


# =============================================================================
# CLI
# =============================================================================
def _build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="影子路由决策引擎（Phase 4）")
    p.add_argument("--self-test", action="store_true", help="运行内置自测")
    p.add_argument("--route", type=str, help="JSON: {\"task_type\":\"mapping\",\"classification\":\"simulated\"}")
    p.add_argument("--eval", type=str, help="JSON: 一次影子评估记录，见 record()")
    p.add_argument("--show-state", action="store_true", help="打印当前落盘状态")
    p.add_argument("--reset-state", action="store_true", help="清空晋升与累计统计")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_cli().parse_args(argv)
    router = get_router()

    if args.reset_state:
        router.reset_state()
        print("[shadow] 状态已重置。")
        return 0
    if args.show_state:
        print(json.dumps(router.show_state(), ensure_ascii=False, indent=2))
        return 0
    if args.route:
        obj = json.loads(args.route)
        d = router.route(obj.get("task_type", "mapping"), obj.get("classification"))
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if args.eval:
        rec = json.loads(args.eval)
        detail = router.record(rec)
        print(json.dumps(detail, ensure_ascii=False, indent=2))
        return 0
    if args.self_test:
        # 自测用独立配置，不依赖落盘单例
        return _self_test()

    # 默认：展示状态
    print(json.dumps(router.show_state(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

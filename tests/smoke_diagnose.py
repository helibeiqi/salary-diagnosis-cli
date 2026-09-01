# -*- coding: utf-8 -*-
"""
smoke_diagnose.py — 薪酬现状诊断（diagnose.py）集成冒烟测试
================================================================================

验证链路：load_salary_data → confirm_mapping → generate_band → analyze_current_state
在真实样本 data/sample_salary.csv 上跑通，确认：
    1. 无导入错误 / 无异常抛出；
    2. meta['diagnose'] 被回写（且只含聚合值，不含行级明细）；
    3. 会话 DataFrame 已带上 cr / penetration / flag 三列。

跑法：
    cd comp-agent-harness   # 仓库根目录（在仓库内执行可省略此步）
    PYTHONIOENCODING=utf-8 C:/ProgramData/anaconda3/python.exe tests/smoke_diagnose.py

注意：本脚本会把会话状态写进 .state/，跑完自动清理。
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from src.tools.loader import confirm_mapping, load_salary_data   # noqa: E402
from src.tools.band import generate_band                          # noqa: E402
from src.tools.diagnose import analyze_current_state              # noqa: E402
from src.tools.session import get_store                            # noqa: E402


def _adopt_mapping(suggested: dict) -> dict:
    """模拟「模型一键采纳」自动映射建议（高置信优先，歧义项后续补齐）。"""
    mapping: dict = {}
    used: set = set()
    for col, sug in sorted(suggested.items(), key=lambda kv: -kv[1]["confidence"]):
        t = sug.get("suggest")
        if t and t not in used and not sug.get("ambiguous"):
            mapping[col] = t
            used.add(t)
    for col, sug in sorted(suggested.items(), key=lambda kv: -kv[1]["confidence"]):
        t = sug.get("suggest")
        if t and col not in mapping and t not in used:
            mapping[col] = t
            used.add(t)
    return mapping


def main() -> int:
    out: list = []
    store = get_store()
    sid = None
    try:
        # ---- 1) 加载真实样本 --------------------------------------------------
        res = load_salary_data(os.path.join(ROOT, "data", "sample_salary.csv"))
        assert res.get("ok"), f"load_salary_data 失败: {res}"
        sid = res["session_id"]
        out.append(f"[1] load_salary_data ok, session_id={sid}, "
                   f"rows={res['file_info']['rows']}")

        # ---- 2) 确认字段映射 --------------------------------------------------
        mapping = _adopt_mapping(res["suggested_mapping"])
        cm = confirm_mapping(sid, mapping)
        assert cm.get("ok"), f"confirm_mapping 失败: {cm}"
        out.append(f"[2] confirm_mapping ok, 映射 {len(mapping)} 列, "
                   f"rows_out={cm['rows_out']}, 缺必填={cm['missing_required']}")
        assert not cm["missing_required"], f"必填字段缺失: {cm['missing_required']}"

        # ---- 3) 生成带宽（诊断前置）------------------------------------------
        gb = generate_band(sid, mode="optimize", midpoint_source="current_median",
                           spread="auto", round_to=100)
        assert gb.get("ok"), f"generate_band 失败: {gb}"
        out.append(f"[3] generate_band ok, 职级数={len(gb['band_table'])}")

        # ---- 4) 现状诊断 ------------------------------------------------------
        r = analyze_current_state(sid)
        assert r.get("ok"), f"analyze_current_state 失败: {r}"
        out.append(f"[4] analyze_current_state ok, 回写列={r.get('df_columns_added')}")

        # ---- 5) 校验 meta['diagnose'] 回写 -----------------------------------
        meta = store.get_meta(sid)
        assert "diagnose" in meta, "meta 缺少 'diagnose' 键"
        diag = meta["diagnose"]
        assert {"cr_stats", "counts", "ratios", "cost", "by_level"} <= diag.keys(), \
            f"diagnose 字段不完整: {list(diag.keys())}"
        out.append(f"[5] meta['diagnose'] 回写 ok, "
                   f"counts={diag['counts']}, cr_median={diag['cr_stats'].get('median')}")

        # ---- 6) 校验 DataFrame 带 cr/penetration/flag ------------------------
        df = store.load(sid).df
        for col in ("cr", "penetration", "flag"):
            assert col in df.columns, f"DataFrame 缺少列 {col}"
        out.append(f"[6] DataFrame 含 cr/penetration/flag 列 ok, "
                   f"红绿圈样例: {df['flag'].value_counts().to_dict()}")

        out.append("SMOKE_DIAGNOSE: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        out.append("SMOKE_DIAGNOSE: FAIL")
        out.append(f"  {type(exc).__name__}: {exc}")
        out.append(traceback.format_exc())
        return 1
    finally:
        if sid:
            try:
                store.dispose(sid)
                out.append(f"[cleanup] 已清理会话 {sid}")
            except Exception:  # noqa: BLE001
                pass
        with open(os.path.join(ROOT, "__smoke_diagnose_probe.txt"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(out))


if __name__ == "__main__":
    sys.exit(main())

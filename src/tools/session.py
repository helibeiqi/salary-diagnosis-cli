# -*- coding: utf-8 -*-
"""
session.py — 会话状态存储（Session / SessionStore）
================================================================================

为什么需要会话
--------------------------------------------------------------------------------
Function Calling 的每一次工具调用都是**无状态**的：模型只能重新传参，
不可能把 150 行薪资表塞进下一轮对话的 JSON 里（Token 爆炸，且违反数据安全纪律）。
因此本项目引入「会话」这一层：

    load_salary_data  →  建会话（原始 DataFrame 落盘）→ 返回 session_id
    后续所有工具      →  只传 session_id（十几字节）→ 从磁盘取回 DataFrame

这样既省 Token，又保证**薪资明细全程只在本机磁盘上流转**，不进模型上下文。

存储设计（安全红线）
--------------------------------------------------------------------------------
一个会话 = 两个文件，放在项目根的 `.state/` 目录下：

    .state/{session_id}.parquet   存 DataFrame（列式压缩，读写快、体积小）
    .state/{session_id}.json      存 meta（映射关系、清洗报告、参数快照等）

**红线：JSON 里绝不存薪资明细。**
理由：parquet 是二进制、不会被日志/打印/预览顺手带出；而 JSON 是明文，
一旦被 print、被日志采集、被模型上下文引用，就会泄露真实薪酬。
因此本模块在 `set_meta` / `create` 时强制做 `_sanitize_meta()` 清洗：
    - 拒绝 pandas/numpy 容器对象（DataFrame/Series/ndarray）
    - 拒绝疑似明细的键名（rows/records/data/detail/明细/薪资 等）
    - 列表长度上限 50、字符串长度上限 2000，超出即截断

方法清单
--------------------------------------------------------------------------------
    create(df, meta)          -> session_id   新建会话
    load(session_id)          -> Session      取回会话（含 DataFrame）
    save(session)             -> None         整体回写
    save_df(session_id, df)   -> None         只回写 DataFrame（不动 meta）
    get_meta(session_id)      -> dict         只读 meta
    set_meta(session_id, patch) -> dict       meta 局部打补丁（合并后返回全量）
    list_all()                -> list[dict]   列出全部会话摘要（不含任何明细）
    dispose(session_id)       -> bool         删除会话文件

所有方法均 try-except，失败抛本项目的异常（见 errors.py），
由上层 tool_guard 转成 {"ok": False, "error": {...}}。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from .timefmt import now_str as _now
from typing import Any, Dict, List, Optional

import pandas as pd

from .errors import (
    CompToolError,
    InvalidParameter,
    SessionNotFound,
    error_result,
)

# =============================================================================
# 一、路径与常量
# =============================================================================

# 本文件位于 <项目根>/src/tools/session.py，
# parents[0]=tools, parents[1]=src, parents[2]=项目根
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: 会话状态目录（项目根/.state），可用环境变量覆盖，便于测试隔离
STATE_DIR: str = os.environ.get("COMP_STATE_DIR") or os.path.join(_PROJECT_ROOT, ".state")

#: session_id 合法字符（防目录穿越：不允许出现 / \ : .. 等）
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

#: meta 中禁止出现的键名（命中即丢弃并记 warning），防止明细数据落到明文 JSON
_FORBIDDEN_META_KEYS = {
    "rows", "records", "data", "detail", "details_data", "df", "dataframe",
    "table", "sample", "samples_rows", "salaries", "salary", "明细", "数据",
    "薪资明细", "员工明细", "raw_rows", "preview_rows",
}

#: meta 中单个列表的最大元素数（超过即截断，只保留前 N 个）
_MAX_LIST_LEN = 50
#: meta 中单个字符串的最大长度（超过即截断）
_MAX_STR_LEN = 2000


def _timestamp_compact() -> str:
    """紧凑时间戳，用于生成可排序的 session_id 与输出文件名。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def new_session_id(prefix: str = "s") -> str:
    """
    生成会话 ID：`s_20260830_120530_a1b2c3d4`。

    结构 = 前缀 + 时间戳 + 8 位随机串：
    - 时间戳保证**按文件名排序即按创建顺序排序**，便于运维排查；
    - 随机串（uuid4 前 8 位）避免同一秒内并发建会话撞名。
    """
    return f"{prefix}_{_timestamp_compact()}_{uuid.uuid4().hex[:8]}"


def _assert_valid_session_id(session_id: str) -> None:
    """
    校验 session_id 合法（防目录穿越攻击）。

    会话目录会被拼接进文件路径，若 session_id 含 `../` 就可能写到项目外，
    因此这里用白名单正则严格限制字符。
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise InvalidParameter(
            f"非法的 session_id：{session_id!r}（只允许字母、数字、下划线、短横线，长度 1-64）",
            hint="请传入 load_salary_data 返回的 session_id，不要自行拼接。",
            details={"session_id": str(session_id)[:64]},
        )


def _paths(session_id: str) -> tuple[str, str]:
    """返回 (parquet 路径, json 路径)。"""
    _assert_valid_session_id(session_id)
    base = os.path.join(STATE_DIR, session_id)
    return base + ".parquet", base + ".json"


# =============================================================================
# 二、meta 安全清洗（安全红线的执行点）
# =============================================================================


def _sanitize_meta(meta: Any, _depth: int = 0) -> tuple[Any, List[str]]:
    """
    递归清洗 meta，确保**明文 JSON 里不含薪资明细**。

    处理规则（按优先级）：
    1. pandas/numpy 容器（DataFrame/Series/ndarray）→ 直接拒绝（抛错），
       因为它们的出现几乎必然意味着「有人想把明细塞进 meta」；
    2. 禁用键名（rows/records/明细/...）→ 丢弃该键并记 warning；
    3. 列表长度 > 50 → 截断，并记 warning；
    4. 字符串长度 > 2000 → 截断，并记 warning；
    5. 其余标量、嵌套 dict 递归处理。

    返回
    -------
    (清洗后的 meta, warning 列表)
    """
    warnings: List[str] = []
    if _depth > 6:
        # 防递归炸弹：meta 本应是浅层配置字典，超过 6 层说明调用方用错了
        return "<meta 嵌套过深，已截断>", ["meta 嵌套层级超过 6 层，已截断"]

    if isinstance(meta, (pd.DataFrame, pd.Series)):
        raise InvalidParameter(
            "meta 中不允许存放 DataFrame/Series（薪资明细只能存 parquet，不能存明文 JSON）",
            hint="请把表格数据交给 session 的 DataFrame 部分，meta 只放列名、参数、统计摘要。",
            details={"type": type(meta).__name__},
        )

    if isinstance(meta, dict):
        clean: Dict[str, Any] = {}
        for k, v in meta.items():
            key = str(k)
            if key.lower() in _FORBIDDEN_META_KEYS:
                warnings.append(f"meta 键 {key!r} 疑似包含明细数据，已丢弃（安全策略）")
                continue
            clean[key], sub_warn = _sanitize_meta(v, _depth + 1)
            warnings.extend(sub_warn)
        return clean, warnings

    if isinstance(meta, (list, tuple)):
        items = list(meta)
        if len(items) > _MAX_LIST_LEN:
            warnings.append(
                f"meta 列表长度 {len(items)} 超过上限 {_MAX_LIST_LEN}，已截断（疑似明细数据）"
            )
            items = items[:_MAX_LIST_LEN]
        cleaned: List[Any] = []
        for it in items:
            c, sub_warn = _sanitize_meta(it, _depth + 1)
            cleaned.append(c)
            warnings.extend(sub_warn)
        return cleaned, warnings

    # numpy 标量在外部很常见（np.float64 之类），转成 Python 原生类型才能 JSON 序列化
    if hasattr(meta, "item") and hasattr(meta, "dtype"):
        try:
            meta = meta.item()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 兜底，转换失败就走下面的字符串化
            pass

    if isinstance(meta, str):
        if len(meta) > _MAX_STR_LEN:
            warnings.append(f"meta 字符串长度 {len(meta)} 超过上限 {_MAX_STR_LEN}，已截断")
            return meta[:_MAX_STR_LEN] + "…[已截断]", warnings
        return meta, warnings

    if isinstance(meta, (int, float, bool)) or meta is None:
        return meta, warnings

    # 其他不可序列化的对象：转成类型说明字符串，保证 JSON 一定能落盘
    warnings.append(f"meta 中出现不可序列化对象 {type(meta).__name__}，已替换为类型说明")
    return f"<{type(meta).__name__}>", warnings


# =============================================================================
# 三、Session 数据类
# =============================================================================


@dataclass
class Session:
    """
    一个会话 = 一张表 + 一份配置元数据 + 两个时间戳。

    属性
    -------
    session_id : str
        会话唯一标识。
    df : pd.DataFrame
        薪酬数据表。**列名为标准字段名**（emp_id/level/monthly_salary ...），
        因为所有下游计算只认标准字段名，绝不直接读用户原始列名。
    meta : dict
        会话元信息，典型键：
            source_file      原始文件路径
            raw_columns      原始列名列表
            suggested_mapping auto_suggest_mapping 的建议结果
            mapping          已确认的字段映射（含 confirmed_at）
            coerce_report    数据清洗报告
            band             最近一次 generate_band 的结果摘要
    created_at / updated_at : str
        创建/最近更新时间（人类可读格式）。
    """

    session_id: str
    df: pd.DataFrame
    meta: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def shape(self) -> tuple[int, int]:
        """(行数, 列数)。"""
        return self.df.shape

    def touch(self) -> None:
        """刷新 updated_at。"""
        self.updated_at = _now()

    def summary(self) -> Dict[str, Any]:
        """
        会话摘要 —— **只含结构信息，不含任何薪资数值**。

        供 list_all() 与前端会话列表使用；即使被打印也不泄露数据。
        """
        return {
            "session_id": self.session_id,
            "rows": int(self.df.shape[0]),
            "cols": int(self.df.shape[1]),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_file": self.meta.get("source_file"),
            "mapping_confirmed": bool(self.meta.get("mapping")),
        }


# =============================================================================
# 四、SessionStore
# =============================================================================


class SessionStore:
    """
    会话存储：parquet 存表、JSON 存 meta。

    参数
    ----------
    state_dir : str, optional
        状态目录；缺省用模块级 STATE_DIR（项目根/.state）。
        目录不存在时会在首次写入时自动创建。
    """

    def __init__(self, state_dir: Optional[str] = None) -> None:
        self.state_dir = os.path.abspath(state_dir or STATE_DIR)
        self._ensure_dir()

    # ---------- 基础设施 ----------

    def _ensure_dir(self) -> None:
        """确保状态目录存在（并发场景下 exist_ok 保证幂等）。"""
        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"无法创建会话状态目录：{self.state_dir}",
                hint="请确认项目目录有写权限，或设置环境变量 COMP_STATE_DIR 指向可写目录。",
                details={"state_dir": self.state_dir, "reason": str(exc)},
            ) from exc

    def _parquet_path(self, session_id: str) -> str:
        return os.path.join(self.state_dir, f"{session_id}.parquet")

    def _json_path(self, session_id: str) -> str:
        return os.path.join(self.state_dir, f"{session_id}.json")

    # ---------- 写 ----------

    def create(self, dataframe: pd.DataFrame, meta: Optional[Dict[str, Any]] = None) -> str:
        """
        新建会话并落盘，返回 session_id。

        参数
        ----------
        dataframe : pd.DataFrame
            待存储的表格（通常是刚读入、尚未改名的原始数据）。
        meta : dict, optional
            初始元信息，会先经 `_sanitize_meta()` 安全清洗。

        异常
        -------
        InvalidParameter
            dataframe 不是 DataFrame，或 meta 里混入了 DataFrame/Series。
        CompToolError
            落盘失败（磁盘满、parquet 引擎缺失等）。
        """
        if not isinstance(dataframe, pd.DataFrame):
            raise InvalidParameter(
                f"create() 需要 pandas.DataFrame，实际收到 {type(dataframe).__name__}",
                hint="请先用 pandas 读取文件得到 DataFrame 再建立会话。",
                details={"type": type(dataframe).__name__},
            )

        session_id = new_session_id()
        clean_meta, warnings = _sanitize_meta(meta or {})
        # 记录表形状，让 list_all() 不必读 parquet 就能报出行列数（省 IO）
        clean_meta.setdefault("shape", {"rows": int(dataframe.shape[0]),
                                        "cols": int(dataframe.shape[1])})
        if warnings:
            # 安全策略被触发时，把原因留在 meta 里，方便调用方自查
            clean_meta.setdefault("_meta_warnings", []).extend(warnings)

        session = Session(
            session_id=session_id,
            df=dataframe,
            meta=clean_meta,
            created_at=_now(),
            updated_at=_now(),
        )
        self.save(session)
        return session_id

    def save(self, session: Session) -> None:
        """整体回写（DataFrame + meta 一起写）。"""
        self._ensure_dir()
        try:
            session.df.to_parquet(self._parquet_path(session.session_id), index=False)
            self._write_meta(session.session_id, session.meta,
                             session.created_at, session.updated_at)
        except CompToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"保存会话失败（session_id={session.session_id}）",
                hint="请检查磁盘空间与文件占用；若 .state 目录被其他进程锁定可稍后重试。",
                details={"session_id": session.session_id, "reason": str(exc)},
            ) from exc

    def save_df(self, session_id: str, df: pd.DataFrame) -> None:
        """
        只回写 DataFrame，不动 meta（但会刷新 updated_at）。

        典型场景：confirm_mapping 之后把「已改名为标准字段名」的表覆盖回会话。
        """
        _assert_valid_session_id(session_id)
        if not isinstance(df, pd.DataFrame):
            raise InvalidParameter(
                f"save_df() 需要 pandas.DataFrame，实际收到 {type(df).__name__}",
                details={"type": type(df).__name__},
            )
        if not os.path.exists(self._parquet_path(session_id)):
            raise SessionNotFound(
                f"会话不存在：{session_id}",
                details={"session_id": session_id},
            )
        try:
            df.to_parquet(self._parquet_path(session_id), index=False)
            meta, created_at, _ = self._read_meta(session_id)
            self._write_meta(session_id, meta, created_at, _now())
        except CompToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"保存会话数据表失败（session_id={session_id}）",
                details={"session_id": session_id, "reason": str(exc)},
            ) from exc

    # ---------- 读 ----------

    def load(self, session_id: str) -> Session:
        """取回完整会话（DataFrame + meta）。"""
        _assert_valid_session_id(session_id)
        pq, js = self._parquet_path(session_id), self._json_path(session_id)
        if not os.path.exists(pq):
            raise SessionNotFound(
                f"会话不存在或已过期：{session_id}",
                hint="请重新调用 load_salary_data 建立新会话。",
                details={"session_id": session_id},
            )
        try:
            df = pd.read_parquet(pq)
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"会话数据表读取失败（session_id={session_id}）：{exc}",
                hint="parquet 文件可能损坏，请重新 load_salary_data 建立会话。",
                details={"session_id": session_id, "reason": str(exc)},
            ) from exc

        meta: Dict[str, Any] = {}
        created_at, updated_at = _now(), _now()
        if os.path.exists(js):
            meta, created_at, updated_at = self._read_meta(session_id)
        return Session(session_id=session_id, df=df, meta=meta,
                       created_at=created_at, updated_at=updated_at)

    def get_meta(self, session_id: str) -> Dict[str, Any]:
        """只读 meta（不加载 parquet，开销小，适合频繁查询状态）。"""
        _assert_valid_session_id(session_id)
        if not os.path.exists(self._json_path(session_id)):
            raise SessionNotFound(
                f"会话不存在：{session_id}",
                details={"session_id": session_id},
            )
        meta, _, _ = self._read_meta(session_id)
        return meta

    def set_meta(self, session_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """
        meta 局部打补丁（浅合并一层 + 递归更新嵌套 dict），返回合并后的全量 meta。

        之所以要「合并」而不是「覆盖」，是因为一次诊断流程里不同工具
        会依次往 meta 里写 mapping / coerce_report / band / increase_params，
        覆盖式写入会让前序信息丢失。

        安全：patch 同样先过 `_sanitize_meta()`。
        """
        _assert_valid_session_id(session_id)
        if not isinstance(patch, dict):
            raise InvalidParameter(
                f"set_meta 的 patch 必须是 dict，实际收到 {type(patch).__name__}",
                details={"type": type(patch).__name__},
            )
        if not os.path.exists(self._json_path(session_id)):
            raise SessionNotFound(
                f"会话不存在：{session_id}",
                details={"session_id": session_id},
            )

        meta, created_at, _ = self._read_meta(session_id)
        clean_patch, warnings = _sanitize_meta(patch)
        meta = _deep_update(meta, clean_patch)
        if warnings:
            meta.setdefault("_meta_warnings", []).extend(warnings)
        self._write_meta(session_id, meta, created_at, _now())
        return meta

    def list_all(self) -> List[Dict[str, Any]]:
        """
        列出全部会话摘要，**不含任何薪资明细**。

        实现上只扫 .json（体积小），行数/列数从 meta 里记录的 shape 取；
        若 meta 里没有就返回 -1，不为此去读 parquet（避免无谓 IO）。
        """
        self._ensure_dir()
        out: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.state_dir))
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"无法列目录：{self.state_dir}",
                details={"state_dir": self.state_dir, "reason": str(exc)},
            ) from exc

        for name in names:
            if not name.endswith(".json"):
                continue
            sid = name[: -len(".json")]
            try:
                meta, created_at, updated_at = self._read_meta(sid)
                shape = meta.get("shape") or {}
                out.append({
                    "session_id": sid,
                    "rows": int(shape.get("rows", -1)),
                    "cols": int(shape.get("cols", -1)),
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "source_file": meta.get("source_file"),
                    "mapping_confirmed": bool(meta.get("mapping")),
                })
            except Exception:  # noqa: BLE001 - 单个坏会话不影响整体列表
                out.append({"session_id": sid, "rows": -1, "cols": -1,
                            "created_at": "", "updated_at": "",
                            "source_file": None, "mapping_confirmed": False,
                            "corrupted": True})
        return out

    def dispose(self, session_id: str) -> bool:
        """
        删除会话（parquet + json）。返回是否确实删掉了至少一个文件。

        用于「用户上传了敏感数据、分析完立即销毁」的场景。
        """
        _assert_valid_session_id(session_id)
        removed = False
        for path in (self._parquet_path(session_id), self._json_path(session_id)):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed = True
            except Exception as exc:  # noqa: BLE001
                raise CompToolError(
                    f"删除会话文件失败：{path}",
                    hint="文件可能被占用（如 Excel 打开中），请关闭后重试。",
                    details={"path": path, "reason": str(exc)},
                ) from exc
        return removed

    # ---------- meta 的底层读写 ----------

    def _write_meta(self, session_id: str, meta: Dict[str, Any],
                    created_at: str, updated_at: str) -> None:
        """写 meta 到 JSON（含时间戳与行数，行数用于 list_all 不读 parquet）。"""
        self._ensure_dir()
        payload: Dict[str, Any] = {
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "meta": meta,
        }
        tmp = self._json_path(session_id) + ".tmp"
        try:
            # 先写临时文件再原子替换，避免进程被杀时留下半个 JSON
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self._json_path(session_id))
        except Exception as exc:  # noqa: BLE001
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise CompToolError(
                f"写入会话 meta 失败（session_id={session_id}）",
                details={"session_id": session_id, "reason": str(exc)},
            ) from exc

    def _read_meta(self, session_id: str) -> tuple[Dict[str, Any], str, str]:
        """读 meta，返回 (meta, created_at, updated_at)。"""
        path = self._json_path(session_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:  # noqa: BLE001
            raise CompToolError(
                f"会话 meta 读取失败（session_id={session_id}）：{exc}",
                hint="meta 文件可能损坏，请重新建立会话。",
                details={"session_id": session_id, "reason": str(exc)},
            ) from exc
        if not isinstance(payload, dict):
            raise CompToolError(
                f"会话 meta 格式异常（session_id={session_id}）",
                details={"session_id": session_id},
            )
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        return meta, payload.get("created_at", ""), payload.get("updated_at", "")


# =============================================================================
# 五、内部工具
# =============================================================================


def _deep_update(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """
    递归合并两个 dict：patch 覆盖 base，但**嵌套 dict 逐层合并**而非整体替换。

    举例：base={'a':{'x':1,'y':2}}, patch={'a':{'y':3}} → {'a':{'x':1,'y':3}}
    这样各工具往 meta 里补字段时不会互相抹掉。
    """
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_update(out[k], v)
        else:
            out[k] = v
    return out


# =============================================================================
# 六、模块级默认存储（绝大多数场景直接用它，无需自己 new）
# =============================================================================

_default_store: Optional[SessionStore] = None


def get_store(state_dir: Optional[str] = None) -> SessionStore:
    """
    获取模块级默认 SessionStore（懒加载单例）。

    单例的意义：状态目录只需建一次，后续所有工具共享同一份配置。
    """
    global _default_store
    if state_dir is not None:
        return SessionStore(state_dir)
    if _default_store is None:
        _default_store = SessionStore()
    return _default_store


def reset_store(state_dir: Optional[str] = None) -> SessionStore:
    """重置默认存储（测试隔离用）。"""
    global _default_store
    _default_store = SessionStore(state_dir) if state_dir else None
    return get_store()


# =============================================================================
# 七、自测入口
# =============================================================================

if __name__ == "__main__":  # pragma: no cover
    import tempfile

    tmp = tempfile.mkdtemp(prefix="comp_session_test_")
    store = SessionStore(tmp)
    df = pd.DataFrame({"emp_id": ["E1", "E2"], "monthly_salary": [10000.0, 12000.0]})
    sid = store.create(df, {"source_file": "demo.csv"})

    print("session_id :", sid)
    print("load shape :", store.load(sid).shape)
    print("meta       :", store.get_meta(sid))

    # 安全红线验证：试图把明细塞进 meta 必须被拦下
    bad = store.set_meta(sid, {"rows": [{"a": 1}] * 3})
    print("rows 是否被丢弃:", "rows" not in bad, "| warnings:", bad.get("_meta_warnings"))

    store.save_df(sid, df.head(1))
    print("save_df 后行数:", store.load(sid).shape[0])
    print("list_all :", store.list_all())
    print("dispose  :", store.dispose(sid))
    print("异常路径 :", error_result(SessionNotFound("会话不存在：xxx"))["error"]["code"])

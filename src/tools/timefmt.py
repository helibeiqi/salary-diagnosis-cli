# -*- coding: utf-8 -*-
"""
timefmt.py — 全项目统一的人类可读时间戳（唯一实现）

背景：此前 _now() 在 band / diagnose / increase / jobeval / loader /
market / paymix / session 八个模块里各有一份拷贝（部分还把
`from datetime import datetime` 写在函数体内），属典型的复制粘贴式
重复定义。现收敛为本模块的 now_str()，各模块以
`from .timefmt import now_str as _now` 保持调用点不变。

格式约定（与历史实现逐字节一致，meta/日志已依赖此格式）：
    %Y-%m-%d %H:%M:%S   本地时区，秒级精度，便于人读日志
"""
from datetime import datetime


def now_str() -> str:
    """当前本地时间的统一格式串（秒级精度）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

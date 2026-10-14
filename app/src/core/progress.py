"""耗时任务的诚实进度工具。

核心层继续兼容 `(message, fraction)`：0~1 表示可量化进度，负数表示
当前阶段没有可靠分母。这里只做纯数值归一化和阶段映射，不依赖 Qt。
"""

from __future__ import annotations

import math
from collections.abc import Callable

ProgressCallback = Callable[[str, float], None]


def valid_fraction(value: object) -> float | None:
    """返回限制到 0~1 的有限数；未知/非法值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clean = float(value)
    if not math.isfinite(clean) or clean < 0:
        return None
    return max(0.0, min(1.0, clean))


def map_fraction(value: object, start: float, end: float) -> float:
    """把子任务比例映射到父任务区间；未知值保持 -1。"""
    clean = valid_fraction(value)
    if clean is None:
        return -1.0
    lower = max(0.0, min(1.0, float(start)))
    upper = max(lower, min(1.0, float(end)))
    return lower + (upper - lower) * clean


def stage_callback(
    callback: ProgressCallback,
    start: float,
    end: float,
) -> ProgressCallback:
    """创建保持说明文字不变的阶段进度映射器。"""
    def emit(message: str, fraction: float) -> None:
        callback(message, map_fraction(fraction, start, end))

    return emit


def percent_text(value: object, *, approximate: bool = False) -> str:
    clean = valid_fraction(value)
    if clean is None:
        return "处理中"
    prefix = "约 " if approximate else ""
    return f"{prefix}{int(round(clean * 100))}%"

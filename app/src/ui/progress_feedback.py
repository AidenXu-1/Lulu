"""QProgressBar 的统一单向呈现。

未知进度保持最后一个可信位置，不再使用 Qt `setRange(0, 0)` 的循环扫动。
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QProgressBar

from ..core.progress import valid_fraction

_VALUE_PROPERTY = "luluProgressValue"
_UNKNOWN_PROPERTY = "luluProgressUnknown"


def reset_progress(bar: QProgressBar, label: QLabel | None = None) -> None:
    bar.setRange(0, 100)
    bar.setValue(0)
    bar.setProperty(_VALUE_PROPERTY, 0)
    bar.setProperty(_UNKNOWN_PROPERTY, False)
    bar.setAccessibleDescription("进度 0%")
    if label is not None:
        label.setText("0%")


def present_progress(
    bar: QProgressBar,
    fraction: object,
    *,
    label: QLabel | None = None,
    unknown_text: str = "处理中",
    approximate: bool = False,
    monotonic: bool = True,
) -> int | None:
    """呈现一次进度，返回当前百分比；未知阶段返回 None。"""
    clean = valid_fraction(fraction)
    previous = bar.property(_VALUE_PROPERTY)
    try:
        previous_value = max(0, min(100, int(previous)))
    except (TypeError, ValueError):
        previous_value = max(0, min(100, int(bar.value())))

    bar.setRange(0, 100)
    if clean is None:
        bar.setValue(previous_value)
        bar.setProperty(_VALUE_PROPERTY, previous_value)
        bar.setProperty(_UNKNOWN_PROPERTY, True)
        bar.setAccessibleDescription(
            f"{unknown_text}，已到 {previous_value}%" if previous_value else unknown_text
        )
        if label is not None:
            label.setText(unknown_text)
        return None

    value = max(0, min(100, int(round(clean * 100))))
    if monotonic:
        value = max(previous_value, value)
    bar.setValue(value)
    bar.setProperty(_VALUE_PROPERTY, value)
    bar.setProperty(_UNKNOWN_PROPERTY, False)
    text = f"约 {value}%" if approximate and value < 100 else f"{value}%"
    bar.setAccessibleDescription(f"进度 {text}")
    if label is not None:
        label.setText(text)
    return value

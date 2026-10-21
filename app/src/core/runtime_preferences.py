"""应用级运行偏好：Lulu 始终从内容提取开始。"""

from __future__ import annotations

STARTUP_TRANSCRIPTION = "transcription"


def startup_page_index(_cfg=None) -> int:
    """固定返回内容提取；旧配置中的页面记忆键不再参与启动。"""

    return 0


def remember_top_level_page(cfg, index: int) -> bool:
    """旧调用兼容 no-op；不再记录或保存顶层页面。"""

    del cfg, index
    return True

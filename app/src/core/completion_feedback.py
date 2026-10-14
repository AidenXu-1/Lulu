"""任务完成与失败的应用级反馈编排，不依赖具体业务页面。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

COMPLETION_STAY = "stay"
COMPLETION_OPEN_RESULT = "open_result"


@dataclass(frozen=True)
class CompletionTarget:
    """可选的结果定位；没有可靠目标时不猜测。"""

    page_index: int
    library_kind: str | None = None
    entry_id: str | None = None


class CompletionFeedback:
    """把全局偏好接到通知、声音和结果定位，任何失败都不伤害业务结果。"""

    def __init__(self, cfg, notifications, navigate: Callable[[CompletionTarget], None]):
        self.cfg = cfg
        self.notifications = notifications
        self.navigate = navigate

    def success(self, target: CompletionTarget | None, *, app_is_active: bool) -> None:
        try:
            self.notifications.notify_task_completed(
                str(getattr(self.cfg, "notification_policy", "never")),
                app_is_active=bool(app_is_active),
                play_sound=bool(getattr(self.cfg, "notification_sound_enabled", True)),
            )
        except Exception:  # noqa: BLE001 - 系统反馈不能改变已完成业务状态
            pass

        if target is None or _completion_action(self.cfg) != COMPLETION_OPEN_RESULT:
            return
        try:
            self.navigate(target)
        except Exception:  # noqa: BLE001 - 定位失败时停在原页，不回滚业务结果
            pass

    def failure(self, *, app_is_active: bool) -> None:
        if not bool(getattr(self.cfg, "notify_on_failure", True)):
            return
        try:
            self.notifications.notify_task_failed(
                str(getattr(self.cfg, "notification_policy", "never")),
                app_is_active=bool(app_is_active),
            )
        except Exception:  # noqa: BLE001 - 系统反馈不能覆盖原业务错误
            pass


def _completion_action(cfg) -> str:
    value = str(getattr(cfg, "completion_action", COMPLETION_STAY))
    return value if value in (COMPLETION_STAY, COMPLETION_OPEN_RESULT) else COMPLETION_STAY

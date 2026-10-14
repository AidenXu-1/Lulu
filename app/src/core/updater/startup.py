"""Business readiness for the existing updater's ready-file protocol."""
from __future__ import annotations


class UpdateReadyGate:
    """A visible window alone does not prove the task service recovered."""

    def __init__(self):
        self._ready_since: float | None = None

    def observe(self, window, *, now: float) -> bool:
        ready = (
            window.isVisible()
            and not getattr(window, "_install_preparation_pending", False)
            and (getattr(window, 'library_page', None) is None
                 or getattr(window.library_page, 'initialization_succeeded', False))
            and getattr(window, "_task_queue_store", None) is not None
            and all(getattr(window, name, True) is False for name in (
                "_close_settling", "_task_queue_initial_connection",
                "_task_queue_startup_pending", "_task_queue_store_failed",
                "_task_queue_snapshot_locked", "_core_recovery_pending",
            ))
            and not getattr(window, "_core_save_pending", False)
            and getattr(window, "_core_save_worker", None) is None
            and not getattr(window, "_result_recovery_failed", False)
            and (getattr(window, "_task_queue_recovery", None) is None
                 or getattr(window, "_task_queue_recovery_checked", False))
        )
        if not ready:
            self._ready_since = None
            return False
        if self._ready_since is None:
            self._ready_since = now
        return now - self._ready_since >= 2.0

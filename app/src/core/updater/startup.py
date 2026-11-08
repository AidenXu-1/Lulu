"""Business readiness for the existing updater's ready-file protocol."""
from __future__ import annotations


class UpdateReadyGate:
    """A visible window alone does not prove the task service recovered."""

    def __init__(self):
        self._ready_since: float | None = None
        self.pending_reasons: tuple[str, ...] = ()

    def observe(self, window, *, now: float) -> bool:
        checks = {
            'WINDOW': window.isVisible(),
            'PREPARATION': not getattr(window, "_install_preparation_pending", False),
            'LIBRARY': (getattr(window, 'library_page', None) is None
                        or getattr(window.library_page, 'initialization_succeeded', False)),
            'TASK_STORE': getattr(window, "_task_queue_store", None) is not None,
            **{name.strip('_').upper(): getattr(window, name, True) is False for name in (
                "_close_settling", "_task_queue_initial_connection",
                "_task_queue_startup_pending", "_task_queue_store_failed",
                "_task_queue_snapshot_locked", "_core_recovery_pending",
            )},
            'SAVE_PENDING': not getattr(window, "_core_save_pending", False),
            'SAVE_WORKER': getattr(window, "_core_save_worker", None) is None,
            'RESULT_RECOVERY': not getattr(window, "_result_recovery_failed", False),
            'RECOVERY_CHECK': (getattr(window, "_task_queue_recovery", None) is None
                               or getattr(window, "_task_queue_recovery_checked", False)),
        }
        self.pending_reasons = tuple(name for name, passed in checks.items() if not passed)
        ready = not self.pending_reasons
        if not ready:
            self._ready_since = None
            return False
        if self._ready_since is None:
            self._ready_since = now
        return now - self._ready_since >= 2.0

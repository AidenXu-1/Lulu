"""Explicit single-use replay stores for Phase 4 authority handles."""

from __future__ import annotations

from threading import Lock

from .contracts import AgentTrustError


class MemoryReplayStore:
    """Test-only in-memory replay store; no production fallback is implied."""

    def __init__(self) -> None:
        self._slots: set[str] = set()
        self._lock = Lock()
        self.mutation_count = 0

    def consume(self, slot: str) -> None:
        if type(slot) is not str or not slot:
            raise AgentTrustError(
                "APPROVAL_INVALID",
                exit_code=21,
                reason_code="REPLAY_SLOT_INVALID",
            )
        with self._lock:
            if slot in self._slots:
                raise AgentTrustError(
                    "APPROVAL_CONSUMED",
                    exit_code=21,
                    reason_code="AUTHORITY_HANDLE_REPLAYED",
                )
            self._slots.add(slot)
            self.mutation_count += 1

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._slots))

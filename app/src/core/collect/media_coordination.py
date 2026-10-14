"""Coordinate input preparation for one work within this application process."""
from contextlib import contextmanager
from threading import Lock

_registry_lock = Lock()
_registry = {}


@contextmanager
def prepare_media(stable_key, is_cancelled):
    # References include waiters, so removing an idle lock cannot split a queue.
    with _registry_lock:
        gate, users = _registry.get(stable_key, (Lock(), 0))
        _registry[stable_key] = (gate, users + 1)
    acquired = False
    try:
        while not acquired:
            if is_cancelled():
                raise InterruptedError("media_cancelled")
            acquired = gate.acquire(timeout=0.05)
        if is_cancelled():
            raise InterruptedError("media_cancelled")
        yield
    finally:
        if acquired:
            gate.release()
        with _registry_lock:
            _, users = _registry[stable_key]
            if users == 1:
                del _registry[stable_key]
            else:
                _registry[stable_key] = (gate, users - 1)

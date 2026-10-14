"""Explicit Core-only automatic access for newly created task material.

Existing legacy items retain their ACL. The user can separately approve system
access; this module does not claim to override that user-controlled behavior.
"""
from pathlib import Path
import re
import sys
from ..license.store import StrictKeyringStore, SecureStoreError
from .core_task_native import NativeCoreTaskStore

CORE = Path("/Applications/Lulu.app/Contents/MacOS/lulu-agent-core")


def current_core():
    from ..agent_entry.standalone_paths import frozen_app
    try:
        return frozen_app("lulu-agent-core")/"Contents/MacOS/lulu-agent-core"
    except RuntimeError as exc:
        raise SecureStoreError('JOB_CORE_IDENTITY_REQUIRED') from exc


def _assert_core_account(name):
    if (sys.platform != "darwin" or not bool(getattr(sys, "frozen", False))
        or Path(sys.executable) != current_core()):
        raise SecureStoreError("JOB_CORE_IDENTITY_REQUIRED")
    if (type(name) is not str or not (name == "lulu_core_job_registry_v1"
        or re.fullmatch(r"lulu_core_job_(?:key|anchor)_v1:[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", name))):
        raise SecureStoreError("JOB_CORE_ACCOUNT_INVALID")


class CoreTaskKeyringStore(StrictKeyringStore):
    """Core private reads verify stored access; license/legacy reads keep semantics."""
    core_generation = None
    authorized_core_identities = None

    def _native_store(self):
        store = NativeCoreTaskStore(self._keyring(), self.service, current_core())
        if self.core_generation is not None:
            if self.core_generation != store.cdhash:
                raise SecureStoreError('JOB_CORE_IDENTITY_REQUIRED')
            store.service = f'{self.service}.CoreTasks.{store.cdhash}'
            store.allowed_identities = self.authorized_core_identities
        return store

    def get_text(self, key):
        if type(key) is not str or not key.startswith('lulu_core_job_'):
            return super().get_text(key)
        _assert_core_account(key)
        try:
            return self._native_store().read(key)
        except SecureStoreError:
            raise
        except Exception as exc:
            raise SecureStoreError('JOB_CORE_MATERIAL_READ_FAILED') from exc

    def create_core_task_material(self, name, value):
        _assert_core_account(name)
        # The native protocol passed r9 create/update/reopen/OS-denial checks.
        # Runtime identity and stored ACL checks remain mandatory below.
        try:
            self._native_store().create(name, value)
        except SecureStoreError:
            raise
        except Exception as exc:
            raise SecureStoreError("JOB_CORE_MATERIAL_CREATE_FAILED") from exc

    def replace_core_task_material(self, name, expected, value):
        _assert_core_account(name)
        try:
            self._native_store().replace(name, expected, value)
        except SecureStoreError:
            raise
        except Exception as exc:
            raise SecureStoreError("JOB_CORE_MATERIAL_UPDATE_FAILED") from exc

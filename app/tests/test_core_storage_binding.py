"""Real files and ledger, fake only Core secure storage; no system Keychain."""
import base64
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.job_ledger.storage_binding import prepare_core_task_store, REGISTRY_KEY
from src.core.job_ledger.store import JobLedgerStore
from src.core.cli_runtime.keychain import KeyringJobIntegrityAnchorStore
from test_phase5_job_ledger import _create_arguments
from src.core.job_ledger.contracts import JobState


class CoreMemoryKeys:
    def __init__(self): self.values = {}; self.writes = []
    def get_text(self, name): return self.values.get(name)
    def create_core_task_material(self, name, value):
        if name in self.values: raise RuntimeError("existing material preserved")
        self.values[name] = value; self.writes.append(("create", name))
    def replace_core_task_material(self, name, expected, value):
        if self.values.get(name) != expected: raise RuntimeError("material changed")
        self.values[name] = value; self.writes.append(("replace", name))
    def set_text(self, name, value):
        # Existing old anchor writes remain the old interface after validation.
        self.values[name] = value; self.writes.append(("legacy", name))


class CoreStorageBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "Lulu"; self.root.mkdir(mode=0o700)
        self.keys = CoreMemoryKeys()

    def prepare(self, cut=None):
        return prepare_core_task_store(self.root, "product-installation", self.keys, crash_after=cut)

    def legacy(self):
        self.keys.values["lulu_agent_cli_job_hmac_key_v1:installation-001"] = base64.b64encode(b"old-synthetic-key-material-0000000").decode()
        store = JobLedgerStore(self.root / "jobs", KeyringJobIntegrityAnchorStore(self.keys, "installation-001"))
        store.create_job(**_create_arguments())
        return store

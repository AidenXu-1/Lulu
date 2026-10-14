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

    def test_new_default_preparation_has_one_key_and_survives_reopen(self):
        store = self.prepare(); store.create_job(**dict(_create_arguments(), installation_id="product-installation"))
        expected = store.list_jobs(); created = list(self.keys.writes)
        self.assertEqual(self.prepare().list_jobs(), expected)
        self.assertEqual(self.keys.writes, created)

    def test_generation_storage_never_queries_preserved_shared_task_keys(self):
        original_read = self.keys.get_text
        def read(name):
            self.assertFalse(name.startswith("lulu_agent_cli_"))
            return original_read(name)
        self.keys.get_text = read
        for _ in range(2):
            store = prepare_core_task_store(self.root, "product-installation", self.keys, legacy_storage=False)
            self.assertEqual(store.list_jobs(), ())

    def test_gui_saved_group_reopens_with_identical_jobs_and_no_key_recreation(self):
        from src.core.gui_task_view import GuiTaskViewStore
        store = self.prepare()
        store.create_job(**dict(_create_arguments(), installation_id="product-installation"))
        view = GuiTaskViewStore(self.root / "jobs")
        view.save_view(groups=[{"id": 1, "name": "原批次", "folder_id": None,
            "expanded": True, "members": [{"task_id": "job-001"}]}],
            hidden_ids=[], expected_revision=0, known_ids={"job-001"})
        expected = store.list_jobs(); raw = view.path.read_bytes(); writes = list(self.keys.writes)
        for _ in range(3):
            self.assertEqual(self.prepare().list_jobs(), expected)
            self.assertEqual(view.path.read_bytes(), raw)
            self.assertEqual(self.keys.writes, writes)

    def test_generation_storage_cannot_adopt_unregistered_existing_ledger(self):
        old = self.legacy(); before = old.journal_path.read_bytes()
        keys_before = deepcopy(self.keys.values); writes_before = list(self.keys.writes)
        from src.core.job_ledger.contracts import JobLedgerError
        with self.assertRaises(JobLedgerError) as raised:
            prepare_core_task_store(self.root, "product-installation", self.keys, legacy_storage=False)
        self.assertEqual(raised.exception.reason_code, "JOB_STORAGE_RECOVERY_REQUIRED")
        self.assertEqual(old.journal_path.read_bytes(), before)
        self.assertEqual(self.keys.values, keys_before)
        self.assertEqual(self.keys.writes, writes_before)

    def test_old_scope_add_restart_and_next_upgrade_keep_both_jobs(self):
        old = self.legacy(); prefix = old.journal_path.read_bytes(); original = old.list_jobs()[0]
        store = self.prepare()
        store.create_job(**dict(_create_arguments(), job_id="job-002", attempt_id="attempt-002",
            lineage_id="lineage-002", idempotency_key="idem-002", installation_id="product-installation"))
        for _ in range(2):
            reopened = self.prepare()
            self.assertEqual(len(reopened.list_jobs()), 2)
            self.assertEqual(reopened.list_jobs()[0], original)
            self.assertTrue(reopened.journal_path.read_bytes().startswith(prefix))

    def test_old_completed_result_survives_binding_and_reopen(self):
        from src.core.storage_layout import SharedProductStorageLayout
        from src.core.transcription_result_store import TranscriptionResultStore
        from src.core.transcription_contracts import LocalFileTranscriptionResultV1
        old = self.legacy(); original = old.list_jobs(); journal = old.journal_path.read_bytes()
        results = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(self.root.parent))
        results.write("job-001", LocalFileTranscriptionResultV1(text="保存旧逐字稿", segments=(), language="zh", engine="local_sensevoice", source_path="/isolated.wav"))
        path = self.root / "jobs/results/job-001.json"; raw = path.read_bytes()
        for _ in range(2): self.assertEqual(self.prepare().list_jobs(), original)
        self.assertEqual(old.journal_path.read_bytes(), journal)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse(any(name.startswith("lulu_core_job_key_v1:") for name in self.keys.values))

    def test_each_named_cut_resumes_same_intent_without_second_key(self):
        for legacy in (False, True):
            for cut in ("registered", "intent", "key", "binding", "ledger", "ready"):
                with self.subTest(legacy=legacy, cut=cut), TemporaryDirectory() as directory:
                    root = Path(directory).resolve() / "Lulu"; root.mkdir(mode=0o700)
                    keys = CoreMemoryKeys()
                    if legacy:
                        keys.values["lulu_agent_cli_job_hmac_key_v1:installation-001"] = base64.b64encode(b"old-synthetic-key-material-0000000").decode()
                        JobLedgerStore(root / "jobs", KeyringJobIntegrityAnchorStore(keys, "installation-001")).create_job(**_create_arguments())
                    with self.assertRaisesRegex(RuntimeError, "CORE_STORAGE_CRASH"):
                        prepare_core_task_store(root, "product-installation", keys, crash_after=cut)
                    record = keys.values[REGISTRY_KEY]
                    store = prepare_core_task_store(root, "product-installation", keys)
                    self.assertEqual(len(store.list_jobs()), 1 if legacy else 0)
                    key_creations = [name for op, name in keys.writes if op == "create" and name.startswith("lulu_core_job_key_v1:")]
                    self.assertEqual(len(key_creations), 0 if legacy else 1)

    def test_ready_binding_missing_or_tampered_never_recreated(self):
        self.prepare(); path = self.root / "task-integrity-v1/binding.json"
        original = path.read_bytes()
        for raw in (None, b"{}\n"):
            if raw is None: path.unlink()
            else: path.write_bytes(raw); path.chmod(0o600)
            before = deepcopy(self.keys.values)
            with self.assertRaises(Exception): self.prepare()
            self.assertEqual(before, self.keys.values)
            self.assertEqual(path.read_bytes() if path.exists() else None, raw)
            if path.exists(): path.unlink()
            path.write_bytes(original); path.chmod(0o600)

    def test_unknown_empty_and_incomplete_state_does_not_create_material(self):
        (self.root / "jobs").mkdir(mode=0o700)
        with self.assertRaises(Exception): self.prepare()
        self.assertEqual(self.keys.values, {})

    def test_secure_core_writer_required_without_plain_write_fallback(self):
        keys = type("ReadOnlyKeys", (), {"get_text": lambda self, name: None,
            "write_new": lambda *args: (_ for _ in ()).throw(AssertionError("generic write forbidden"))})()
        with self.assertRaises(Exception): prepare_core_task_store(self.root, "product-installation", keys)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_jobs_with_old_recovery_ticket_never_creates_new_key(self):
        self.keys.values["lulu_agent_cli_installation_ticket_v1"] = "existing-unread-recovery-ticket"
        with self.assertRaises(Exception): self.prepare()
        self.assertEqual(self.keys.writes, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_stage_and_missing_key_after_binding_are_preserved(self):
        with self.assertRaises(RuntimeError): self.prepare("binding")
        directory = self.root / "task-integrity-v1"
        stage = directory / "prepared-jobs"; stage.mkdir(mode=0o700)
        (stage / "incomplete").write_bytes(b"preserved")
        before = deepcopy(self.keys.values)
        with self.assertRaises(Exception): self.prepare()
        self.assertEqual(before, self.keys.values)
        self.assertEqual((stage / "incomplete").read_bytes(), b"preserved")
        key_name = next(name for name in self.keys.values if name.startswith("lulu_core_job_key_v1:"))
        self.keys.values.pop(key_name)
        before = deepcopy(self.keys.values)
        with self.assertRaises(Exception): self.prepare()
        self.assertEqual(before, self.keys.values)

    def test_orphaned_running_is_marked_by_existing_recovery_without_replay(self):
        from src.core.job_ledger.service import JobControlService
        store = self.prepare(); job = store.create_job(**dict(_create_arguments(), installation_id="product-installation"))
        store.transition(job.job_id, expected_revision=job.job_revision, new_state=JobState.QUEUED,
            changed_at="2026-09-06T00:00:01Z")
        queued = store.get_job(job.job_id)
        JobControlService(store, enforce_fencing=True).claim_worker(job.job_id,
            expected_revision=queued.job_revision, worker_instance_id="previous-core-worker",
            operation_receipt=None, changed_at="2026-09-06T00:00:02Z")
        prefix = store.journal_path.read_bytes()
        reopened = self.prepare(); recovered = reopened.get_job(job.job_id)
        self.assertEqual(recovered.state, JobState.RECOVERY_REQUIRED)
        self.assertTrue(reopened.journal_path.read_bytes().startswith(prefix))
        resumed = JobControlService(reopened).resume(recovered.job_id,
            expected_revision=recovered.job_revision, changed_at="2026-09-06T00:00:04Z")
        self.assertEqual(resumed.state, JobState.QUEUED)

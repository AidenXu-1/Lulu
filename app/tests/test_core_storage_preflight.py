"""Real filesystem preflight preserves ambiguous and legacy task material."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.core.job_ledger.storage_preflight import inspect_task_storage, authenticate_legacy_scope
from src.core.job_ledger.store import JobLedgerStore
from test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments


class StoragePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "Lulu"
        self.root.mkdir(mode=0o700)

    def capture(self):
        return {str(p.relative_to(self.root)): (p.lstat().st_mode,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in self.root.rglob("*")}

    def ledger(self):
        store = JobLedgerStore(self.root / "jobs", FakeIntegrityAnchorStore())
        store.create_job(**_create_arguments())
        return store

    def test_absent_and_empty_are_distinct_and_neither_creates_files(self):
        for expected in ("absent", "empty"):
            before = self.capture()
            result = inspect_task_storage(self.root)
            self.assertEqual(result.canonical_state, expected)
            self.assertEqual(before, self.capture())
            if expected == "absent": (self.root / "jobs").mkdir(mode=0o700)

    def test_existing_installer_container_mode_is_preserved_but_shared_write_rejected(self):
        self.root.chmod(0o755)
        self.assertEqual(inspect_task_storage(self.root).canonical_state, "absent")
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)
        self.root.chmod(0o777)
        with self.assertRaises(Exception): inspect_task_storage(self.root)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o777)

    def test_complete_canonical_with_wrong_empty_is_only_a_candidate(self):
        self.ledger()
        (self.root / "Lulu/jobs").mkdir(mode=0o700, parents=True)
        (self.root / "Lulu").chmod(0o700)
        before = self.capture()
        result = inspect_task_storage(self.root)
        self.assertEqual(result.canonical_state, "complete")
        self.assertEqual(result.candidate_scopes, ("installation-001",))
        self.assertTrue(result.wrong_empty)
        self.assertEqual(before, self.capture())

    def test_product_written_result_is_preserved_and_ledger_still_authenticates(self):
        from src.core.storage_layout import SharedProductStorageLayout
        from src.core.transcription_result_store import TranscriptionResultStore
        from src.core.transcription_contracts import LocalFileTranscriptionResultV1
        keys = FakeIntegrityAnchorStore()
        JobLedgerStore(self.root / "jobs", keys).create_job(**_create_arguments())
        results = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(self.root.parent))
        results.write("job-001", LocalFileTranscriptionResultV1(text="隔离旧结果", segments=(), language="zh", engine="local_sensevoice", source_path="/isolated.wav"))
        before = self.capture(); writes = keys.mutation_count
        inventory = inspect_task_storage(self.root)
        self.assertEqual(inventory.canonical_state, "complete")
        self.assertEqual(authenticate_legacy_scope(self.root, inventory, lambda _: keys)[0], "installation-001")
        self.assertEqual(before, self.capture())
        self.assertEqual(writes, keys.mutation_count)

    def test_product_written_gui_view_survives_cold_inventory_and_authentication(self):
        from src.core.gui_task_view import GuiTaskViewStore
        keys = FakeIntegrityAnchorStore()
        store = JobLedgerStore(self.root / "jobs", keys)
        store.create_job(**_create_arguments())
        view = GuiTaskViewStore(self.root / "jobs")
        view.save_view(groups=[], hidden_ids=[], expected_revision=0, known_ids={"job-001"})
        view.set_owner("job-001", 123)
        before = self.capture(); writes = keys.mutation_count
        inventory = inspect_task_storage(self.root)
        self.assertEqual(inventory.canonical_state, "complete")
        self.assertEqual(authenticate_legacy_scope(self.root, inventory, lambda _: keys)[0], "installation-001")
        self.assertEqual(before, self.capture())
        self.assertEqual(writes, keys.mutation_count)

    def test_gui_view_is_only_a_bounded_private_display_file(self):
        import os
        self.ledger()
        path = self.root / "jobs/gui-task-view-v1.json"
        valid = {"revision": 1, "groups": [], "hidden_ids": [], "owners": {}}
        for kind in ("symlink", "hardlink", "directory", "permissions", "oversize", "invalid_json", "invalid_schema", "invalid_revision", "invalid_owner"):
            with self.subTest(kind=kind):
                if kind == "symlink": path.symlink_to(self.root / "jobs/job-ledger-v1/snapshot.json")
                elif kind == "hardlink": os.link(self.root / "jobs/job-ledger-v1/snapshot.json", path)
                elif kind == "directory": path.mkdir(mode=0o700)
                else:
                    value = dict(valid)
                    if kind == "invalid_schema": value["state"] = "succeeded"
                    if kind == "invalid_revision": value["revision"] = True
                    if kind == "invalid_owner": value["owners"] = {"job-001": "123"}
                    path.write_text("{" if kind == "invalid_json" else json.dumps(value))
                    path.chmod(0o644 if kind == "permissions" else 0o600)
                    if kind == "oversize":
                        with path.open("r+b") as stream: stream.truncate(4 * 1024 * 1024 + 1)
                before = self.capture()
                with self.assertRaises(Exception): inspect_task_storage(self.root)
                self.assertEqual(before, self.capture())
                if path.is_dir() and not path.is_symlink(): path.rmdir()
                else: path.unlink()

    def test_result_structure_rejects_unsafe_or_unknown_material_without_writes(self):
        import os
        for kind in ("directory_link", "file_link", "hardlink", "wide_directory", "wide_file", "nested_directory", "temporary", "oversize"):
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "Lulu"; root.mkdir(mode=0o700)
                JobLedgerStore(root / "jobs", FakeIntegrityAnchorStore()).create_job(**_create_arguments())
                results = root / "jobs/results"
                if kind == "directory_link": results.symlink_to(root / "jobs/job-ledger-v1", target_is_directory=True)
                else:
                    results.mkdir(mode=0o700); target = results / "job-001.json"
                    if kind == "file_link": target.symlink_to(root / "jobs/job-ledger-v1/snapshot.json")
                    elif kind == "hardlink": os.link(root / "jobs/job-ledger-v1/snapshot.json", target)
                    elif kind == "nested_directory": target.mkdir(mode=0o700)
                    elif kind == "temporary": (results / ".job-001.pending.tmp").write_bytes(b"preserve")
                    else:
                        target.write_bytes(b"preserve"); target.chmod(0o600)
                        if kind == "wide_directory": results.chmod(0o755)
                        if kind == "wide_file": target.chmod(0o644)
                        if kind == "oversize":
                            with target.open("r+b") as f: f.truncate(32 * 1024 * 1024 + 1)
                def metadata():
                    return [(str(p.relative_to(root)), p.lstat().st_mode, p.lstat().st_size, p.lstat().st_mtime_ns)
                            for p in sorted(root.rglob("*"))]
                before = metadata()
                with self.assertRaises(Exception) as caught: inspect_task_storage(root)
                self.assertEqual(caught.exception.reason_code, "JOB_STORAGE_UNKNOWN_CONTENT")
                self.assertEqual(before, metadata())

    def test_wrong_nonempty_or_only_wrong_empty_preserved(self):
        for canonical in (False, True):
            with self.subTest(canonical=canonical):
                if canonical: self.ledger()
                wrong = self.root / "Lulu/jobs"
                wrong.mkdir(mode=0o700, parents=True, exist_ok=True)
                wrong.parent.chmod(0o700)
                if canonical: (wrong / "unknown").write_bytes(b"preserved")
                before = self.capture()
                with self.assertRaises(Exception): inspect_task_storage(self.root)
                self.assertEqual(before, self.capture())

    def test_pending_and_incomplete_are_rejected_without_repair(self):
        store = self.ledger()
        store.pending_path.write_bytes(b"preserved"); store.pending_path.chmod(0o600)
        before = self.capture()
        with self.assertRaises(Exception): inspect_task_storage(self.root)
        self.assertEqual(before, self.capture())
        store.pending_path.unlink(); store.snapshot_path.unlink()
        before = self.capture()
        with self.assertRaises(Exception): inspect_task_storage(self.root)
        self.assertEqual(before, self.capture())

    def test_legacy_pending_accepts_only_closed_known_empty_schema(self):
        self.ledger(); directory = self.root / "task-queue"; directory.mkdir(mode=0o700)
        path = directory / "pending.json"
        for payload, accepted in (({"schema": 2, "tasks": [], "groups": []}, True),
            ({"schema": 1, "tasks": [], "groups": []}, True),
            ({"schema": True, "tasks": [], "groups": []}, False),
            ({"schema": 2, "tasks": []}, False),
            ({"schema": 2, "tasks": ["existing"], "groups": []}, False),
            ({"schema": 2, "tasks": [], "groups": [], "migration": True}, False)):
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload)); path.chmod(0o600)
                before = self.capture()
                if accepted: self.assertTrue(inspect_task_storage(self.root).legacy_empty)
                else:
                    with self.assertRaises(Exception): inspect_task_storage(self.root)
                self.assertEqual(before, self.capture())

    def test_symlink_and_unknown_job_content_preserved(self):
        (self.root / "jobs").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Exception): inspect_task_storage(self.root)
        (self.root / "jobs").unlink()
        self.ledger(); (self.root / "jobs/unknown").write_bytes(b"existing")
        before = self.capture()
        with self.assertRaises(Exception): inspect_task_storage(self.root)
        self.assertEqual(before, self.capture())

    def test_candidate_names_never_replace_full_authentication(self):
        keys = FakeIntegrityAnchorStore()
        store = JobLedgerStore(self.root / "jobs", keys)
        store.create_job(**_create_arguments())
        store.create_job(**dict(_create_arguments(), job_id="job-002", attempt_id="attempt-002",
            lineage_id="lineage-002", idempotency_key="idem-002", installation_id="new-installation"))
        inventory = inspect_task_storage(self.root)
        before = self.capture(); writes = keys.mutation_count
        wrong = FakeIntegrityAnchorStore(); wrong.hmac_key = lambda: b"wrong-material-only-for-this-test"
        def anchor_for(scope):
            return keys if scope == "installation-001" else wrong
        scope, anchor = authenticate_legacy_scope(self.root, inventory, anchor_for)
        self.assertEqual(scope, "installation-001")
        self.assertIs(anchor, keys)
        self.assertEqual(writes, keys.mutation_count)
        self.assertEqual(before, self.capture())
        # Two authenticated candidates are ambiguous, even if their data agree.
        with self.assertRaises(Exception) as ambiguous:
            authenticate_legacy_scope(self.root, inventory, lambda scope: keys)
        self.assertEqual(ambiguous.exception.reason_code, "JOB_SCOPE_AMBIGUOUS")
        with self.assertRaises(Exception) as unverified:
            authenticate_legacy_scope(self.root, inventory, lambda scope: wrong)
        self.assertEqual(unverified.exception.reason_code, "JOB_SCOPE_UNVERIFIED")
        self.assertEqual(before, self.capture())

"""Completed Core results reach the GUI intact through bounded messages."""
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.core.gui_task_ledger import CoreTaskQueueStore
from src.core.storage_layout import SharedProductStorageLayout
from src.core.transcription_contracts import LocalFileTranscriptionResultV1
from src.core.transcription_result_store import TranscriptionResultStore, TranscriptionResultStoreError


class SharedResultDeliveryTests(unittest.TestCase):
    def test_real_core_runtime_delivers_only_completed_authenticated_job_result(self):
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.agent_entry.entrypoints import CoreCommandDispatcher
        from src.core.auth_guard import UnifiedLicenseAuthGuard
        from src.core.job_ledger.contracts import JobState
        from src.core.job_ledger.service import JobControlService
        from src.core.job_ledger.store import JobLedgerStore
        from src.core.transcription_job_application import LocalTranscriptionJobApplication
        from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore, _create_arguments
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            layout = SharedProductStorageLayout.from_application_support_root(root)
            ledger = JobLedgerStore(layout.jobs, FakeIntegrityAnchorStore())
            job = ledger.create_job(**_create_arguments())
            store = TranscriptionResultStore(layout)
            ref = store.write(job.job_id, LocalFileTranscriptionResultV1("实际 Core 读取正文", (), "zh", "local_sensevoice", "/input.wav"))["result_ref"]
            auth = SimpleNamespace(cached_status_projection=lambda: SimpleNamespace(allowed=True, code="cached_allowed"))
            application = LocalTranscriptionJobApplication(
                runtime_verification={"profile": "trusted_test", "verified": True, "signature_kind": "developer_id", "installation_id": "installation-001"},
                authorization_guard=UnifiedLicenseAuthGuard(auth), model_readiness_probe=lambda _: True,
                result_store=store)
            runtime = LocalTranscriptionCliRuntime(application, job_control=JobControlService(ledger))
            dispatcher = CoreCommandDispatcher(product_identity={}, resource_root=root, runtime_factory=lambda: runtime)
            installer = SimpleNamespace(paths=SimpleNamespace(state_root=root / "installation"))
            def dispatch(command, arguments):
                return dispatcher({"command": command, "arguments": arguments})
            with patch("src.core.agent_entry.product_install.build_default_product_installer", return_value=installer):
                with self.assertRaisesRegex(RuntimeError, "CORE_TASK_RESULT_UNAVAILABLE"):
                    CoreTaskQueueStore(dispatch).read_transcript(job.job_id, ref)
                for state in (JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED):
                    job = ledger.transition(job.job_id, expected_revision=job.job_revision, new_state=state,
                                            changed_at="2026-09-06T01:00:00Z", result_ref=ref if state is JobState.SUCCEEDED else None)
                transcript = CoreTaskQueueStore(dispatch).read_transcript(job.job_id, ref)
                self.assertEqual(transcript.text, "实际 Core 读取正文")
                for arguments in ({"job_id": job.job_id, "offset": True}, {"job_id": job.job_id, "offset": -1}, {"job_id": job.job_id, "offset": 0, "path": "/private"}):
                    with self.assertRaisesRegex(RuntimeError, "COMPONENT_EVIDENCE_REQUIRED"):
                        dispatch("gui.jobs.result", arguments)
                self.assertEqual(ledger.get_job(job.job_id).job_revision, 4)

    def test_long_result_is_reassembled_and_hash_checked_before_display(self):
        text = "完整中文逐字稿。" * 10000 + "最后一句。"
        with TemporaryDirectory() as directory:
            store = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(Path(directory).resolve()))
            ref = store.write("result-job", LocalFileTranscriptionResultV1(text, (), "zh", "local_sensevoice", "/input.wav"))["result_ref"]
            raw = (store.directory / "result-job.json").read_bytes()
            offsets = []
            def dispatch(command, arguments):
                self.assertEqual(command, "gui.jobs.result")
                self.assertEqual(arguments["job_id"], "result-job")
                offset = arguments["offset"]
                offsets.append(offset)
                return {"job_id": "result-job", "result_ref": ref, "offset": offset,
                        "total_bytes": len(raw), "data": base64.b64encode(raw[offset:offset+32768]).decode("ascii")}
            transcript = CoreTaskQueueStore(dispatch).read_transcript("result-job", ref)
            self.assertEqual(transcript.text, text)
            self.assertGreater(len(offsets), 1)
            self.assertEqual(offsets, list(range(0, len(raw), 32768)))
            self.assertEqual(transcript.engine, "local_sensevoice")

    def test_result_store_rejects_tampering_and_another_job_reference(self):
        with TemporaryDirectory() as directory:
            store = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(Path(directory).resolve()))
            ref = store.write("result-job", LocalFileTranscriptionResultV1("原文", (), "zh", "local_sensevoice", "/input.wav"))["result_ref"]
            self.assertIn("原文".encode(), store.read_verified_bytes("result-job", ref))
            with self.assertRaises(TranscriptionResultStoreError):
                store.read_verified_bytes("result-job", ref.replace("result-job", "another-job"))
            path = store.directory / "result-job.json"
            path.write_bytes(path.read_bytes().replace("原文".encode(), "改文".encode()))
            with self.assertRaises(TranscriptionResultStoreError):
                store.read_verified_bytes("result-job", ref)

    def test_gui_refuses_wrong_digest_and_nonprogressing_chunks(self):
        ref = "lulu-result:result-job:sha256:" + "0" * 64
        for data in ("", base64.b64encode(b"{}").decode()):
            def dispatch(command, arguments):
                return {"job_id": "result-job", "result_ref": ref, "offset": 0, "total_bytes": 2, "data": data}
            with self.assertRaises(RuntimeError):
                CoreTaskQueueStore(dispatch).read_transcript("result-job", ref)

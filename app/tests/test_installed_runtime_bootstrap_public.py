"""Public installer -> frozen default Core, with isolated OS boundaries only."""
import base64
import json
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_product_install_v2_public import ProductInstallV2PublicTests
from test_cli_trusted_runtime_factory import _cache_json
from src.core.agent_entry.product_install import ProductInstaller, ProductPaths
from src.core.agent_entry.component_manifest import verify_component_manifest
from src.core.agent_entry.standalone_source import uses_local_storage
from src.core.cli_runtime.factory import build_measured_core_runtime


class InstalledRuntimeBootstrapPublicTests(unittest.TestCase):
    setUp = ProductInstallV2PublicTests.setUp
    command = ProductInstallV2PublicTests.command
    install_product = ProductInstallV2PublicTests.install_product

    def _construct_default_runtime(self):
        # Real default-home layout; fake only OS home, sealed-artifact location,
        # frozen executable identity and secure storage, never the runtime.
        support = self.home / "Library/Application Support"
        support.mkdir(parents=True, mode=0o700)
        if getattr(self, "with_results", False):
            # Existing user layout, measured read-only as 0700 before this test.
            (support / "Lulu").mkdir(mode=0o700)
        self.paths = ProductPaths(self.root / "Applications/Lulu.app",
            support / "Lulu/AgentEntry", self.home / "LaunchAgents/core.plist",
            self.home / "skills/lulu-agent", self.home / "bin/lulu")
        self.installer = ProductInstaller(self.paths, key_store=self.keys,
            service=self.service, stop_clients=lambda: None, clients_stopped=lambda: True)
        self.install_product()
        # The new runtime has no license prerequisite. Optional historical bytes
        # belong to retirement regression fixtures and must remain untouched.
        self.keys.values.update(getattr(self, "legacy_license_fixture", {}))
        if getattr(self, "with_old_ticket", False):
            self.keys.values["lulu_agent_cli_installation_ticket_v1"] = "isolated-old-ticket-must-remain"
        def set_text(key, value):
            self.assertFalse(key.startswith("license_"), "retired license credential write")
            self.keys.values[key] = value
        self.keys.set_text = set_text
        self.keys.create_core_task_material = self.keys.write_new
        def replace_core(name, expected, value):
            self.assertEqual(self.keys.values.get(name), expected)
            self.keys.values[name] = value
        self.keys.replace_core_task_material = replace_core
        if getattr(self, "with_legacy", False):
            from src.core.cli_runtime.keychain import KeyringJobIntegrityAnchorStore
            from src.core.job_ledger.store import JobLedgerStore
            from test_phase5_job_ledger import _create_arguments
            self.keys.values["lulu_agent_cli_job_hmac_key_v1:installation-001"] = base64.b64encode(b"test-only-task-record-key-00000000").decode()
            old_store = JobLedgerStore(support / "Lulu/jobs", KeyringJobIntegrityAnchorStore(self.keys, "installation-001"))
            old_store.create_job(**_create_arguments())
            if getattr(self, "with_results", False):
                from src.core.storage_layout import SharedProductStorageLayout
                from src.core.transcription_result_store import TranscriptionResultStore
                from src.core.transcription_contracts import LocalFileTranscriptionResultV1
                from src.core.job_ledger.contracts import JobState
                results = TranscriptionResultStore(SharedProductStorageLayout.from_application_support_root(support))
                old_result = results.write("job-001", LocalFileTranscriptionResultV1(
                    text="历史脱敏结果", segments=(), language="zh", engine="local_sensevoice", source_path="/isolated-old.wav"))
                for revision, state in enumerate((JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED), 1):
                    old_store.transition("job-001", expected_revision=revision, new_state=state,
                        changed_at="2026-09-06T00:00:00Z", result_ref=old_result["result_ref"] if state is JobState.SUCCEEDED else None)
                result_path = results.directory / "job-001.json"; old_result_bytes = result_path.read_bytes()
                pending = support / "Lulu/task-queue"; pending.mkdir(mode=0o700)
                (pending / "pending.json").write_text(json.dumps({"schema": 2, "tasks": [], "groups": []}))
                (pending / "pending.json").chmod(0o600)
                (support / "Lulu/Lulu").mkdir(mode=0o700)
                (support / "Lulu/Lulu/jobs").mkdir(mode=0o700)
            if not getattr(self, "with_results", False):
                from src.core.job_ledger.contracts import JobState
                old_store.transition("job-001", expected_revision=1, new_state=JobState.CANCELLED,
                                     changed_at="2026-09-06T00:00:00Z")
            old_prefix = old_store.journal_path.read_bytes()
        manifest = self.paths.state_root / "product-materials-v2/Lulu.components.v2.json"
        proof = verify_component_manifest(self.paths.app_bundle, manifest)
        identity = {"adapter_id": "agent_cli", "shared": {
            "profile": "external_candidate", "product_version": proof["product_version"],
            "product_build": proof["product_build"], "source_commit": proof["source_revision"],
            "source_tree": "3" * 40, "bundle_id": "com.lulu.transcribe"}}
        from src.core.agent_entry.task_handover import seal_handover, FILENAME
        from src.core.agent_entry.ownership import verify_ownership_receipt
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        owner = verify_ownership_receipt(self.paths.state_root / "ownership.json", key_store=self.keys)
        history = []
        if getattr(self, "with_legacy", False):
            for job in old_store.list_jobs():
                history.append(dict(LocalTranscriptionCliRuntime._job_summary(job), partial_failures=list(job.partial_failures)))
        handover = seal_handover(history, keys=self.keys, owner=owner,
                                previous_manifest="d" * 64 if history else None, product_root=support / "Lulu")
        handover_path = manifest.parent / FILENAME
        handover_path.write_text(json.dumps(handover)); handover_path.chmod(0o600)
        canonical = self.paths.app_bundle
        # The fixture installs legacy materials; the host's standalone marker
        # must not redirect this synthetic Core into a different generation.
        def measured(app, path):
            self.assertEqual(app, canonical)
            self.assertEqual(path, manifest)
            return verify_component_manifest(self.paths.app_bundle, manifest)
        def fixture_storage(app):
            self.assertEqual(app, canonical)
            return uses_local_storage(self.paths.app_bundle)
        with patch.object(Path, "home", return_value=self.home), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", str(canonical / "Contents/MacOS/lulu-agent-core")), \
             patch("src.core.agent_entry.standalone_paths.installed_app",
                   side_effect=lambda app: canonical if app == canonical else self.fail("foreign app")), \
             patch("src.core.agent_entry.standalone_paths.state_for_app", return_value=self.paths.state_root), \
             patch("src.core.cli_runtime.core_task_native.measured_core", return_value="a" * 40), \
             patch("src.core.cli_runtime.core_task_keychain.CoreTaskKeyringStore", return_value=self.keys), \
             patch("src.core.agent_entry.standalone_source.uses_local_storage", side_effect=fixture_storage), \
             patch("src.core.agent_entry.installed_materials.component_manifest_path", return_value=manifest), \
             patch("src.core.agent_entry.component_manifest.verify_component_manifest", side_effect=measured):
            runtime = build_measured_core_runtime(product_identity=identity,
                resource_root=canonical / "Contents/Resources")
            if not getattr(self, "with_legacy", False):
                self.assertEqual(runtime.gui_jobs_list(), {"jobs": []})
            else:
                from src.core.gui_task_ledger import CoreTaskQueueStore
                from src.core.task import Task
                from src.agent_cli.main import main as cli_main
                from io import StringIO
                queue = CoreTaskQueueStore(lambda command, args: runtime.gui_jobs_create(args["task"]))
                task = queue.register(Task(1, "隔离追加", "/tmp/synthetic-only.wav", "file"))
                expected_ids = {"job-001", task.id}
                if getattr(self, "with_results", False):
                    source = self.root / "new-source.wav"; source.write_bytes(b"RIFF-isolated-audio-fixture")
                    def invoke(argv):
                        stdout, stderr = StringIO(), StringIO()
                        code = cli_main([*argv, "--output", "json"], stdout=stdout, stderr=stderr,
                            product_identity={"adapter_id": "agent_cli"}, local_transcription_runtime=runtime)
                        self.assertEqual((code, stderr.getvalue()), (0, ""))
                        value = json.loads(stdout.getvalue()); self.assertTrue(value["ok"])
                        return value["result"]
                    invoke(["transcribe", "prepare", str(source)])
                    produced = invoke(["transcribe", "run", str(source)])
                    self.assertEqual(produced["status"], "succeeded")
                    new_id = produced["job_receipt"]["job_id"]; expected_ids.add(new_id)
                    self.assertEqual(invoke(["jobs", "show", "--job-id", new_id])["result_ref"], produced["result_ref"])
                    invoke(["jobs", "receipt", "--job-id", new_id])
                    self.assertEqual(results.read(new_id)["transcript"]["text"], "隔离新转录结果")
                    self.assertEqual(results.read("job-001")["transcript"]["text"], "历史脱敏结果")
                for generation in ("restart", "next-upgrade"):
                    with self.subTest(generation=generation):
                        runtime = build_measured_core_runtime(product_identity=identity,
                            resource_root=canonical / "Contents/Resources")
                        stdout, stderr = StringIO(), StringIO()
                        code = cli_main(["jobs", "list", "--output", "json"], stdout=stdout,
                            stderr=stderr, product_identity={"adapter_id": "agent_cli"},
                            local_transcription_runtime=runtime)
                        self.assertEqual((code, stderr.getvalue()), (0, ""))
                        ids = {job["job_id"] for job in json.loads(stdout.getvalue())["result"]["jobs"]}
                        self.assertEqual(ids, expected_ids)
                        self.assertEqual(old_store.journal_path.read_bytes(), old_prefix)
                        if getattr(self, "with_results", False):
                            self.assertEqual(result_path.read_bytes(), old_result_bytes)
                            self.assertEqual(results.read(new_id)["transcript"]["text"], "隔离新转录结果")
                            reader = CoreTaskQueueStore(lambda command, args: runtime.gui_job_result(**args))
                            self.assertEqual(reader.read_transcript('job-001', old_result['result_ref']).text,
                                             '历史脱敏结果')
        self.assertTrue((support / "Lulu/task-generations" / ("a" * 40) / "jobs/job-ledger-v1").is_dir())
        self.assertEqual((support / "Lulu/Lulu").exists(), getattr(self, "with_results", False))


    def test_existing_results_default_bootstrap_cli_run_read_and_reopen(self):
        from src.core.cli_runtime.factory import _build_measured_core_runtime
        from src.core.models import Transcript
        self.with_legacy = self.with_results = True
        calls = []
        def pipeline(source_path, **kwargs):
            calls.append(source_path)
            return Transcript(text="隔离新转录结果", segments=[], language="zh", engine="local_sensevoice",
                source=source_path, title=None, meta={}, saved_media=None)
        def build(**kwargs):
            return _build_measured_core_runtime(**kwargs, pipeline_runner=pipeline, model_readiness_probe=lambda _: True)
        with patch("src.core.cli_runtime.factory._build_measured_core_runtime", side_effect=build):
            self._construct_default_runtime()
        self.assertEqual(len(calls), 1)

    def test_generation_handover_coexists_with_preserved_old_installation_ticket(self):
        self.with_old_ticket = True
        self.test_existing_results_default_bootstrap_cli_run_read_and_reopen()
        self.assertEqual(self.keys.values["lulu_agent_cli_installation_ticket_v1"],
                         "isolated-old-ticket-must-remain")

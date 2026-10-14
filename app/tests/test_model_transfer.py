import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core import model_transfer as mt, models_manager as mm
from src.core import artifact_trust as trust


class ModelTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b"original complete model"

    def entry(self, content=None):
        path = self.root / "weights"
        if content is not None:
            path.write_bytes(content)
        return mt.File(path, len(self.payload), hashlib.sha256(self.payload).hexdigest())

    def test_resumed_corrupt_prefix_is_not_accepted_even_with_correct_length(self):
        file = self.entry(b"X" + self.payload[1:])
        plan = mt.Snapshot("repo", "rev", self.root, [file])
        with self.assertRaisesRegex(RuntimeError, "校验未通过"):
            plan.verify(lambda: None, lambda *_: None)
        self.assertFalse(file.verified)
        self.assertFalse(file.path.exists())

    def test_complete_content_is_verified_and_counts_once(self):
        file = self.entry(self.payload)
        file.partials = (self.root / "weights.incomplete",)
        file.partials[0].write_bytes(self.payload)
        plan = mt.Snapshot("repo", "rev", self.root, [file])
        plan.verify(lambda: None, lambda *_: None)
        self.assertEqual(file.held(), len(self.payload))
        self.assertTrue(file.verified)

    def test_git_blob_hash_includes_header_and_is_distinct_from_lfs_hash(self):
        file = self.entry(self.payload)
        file.algorithm = "git-sha1"
        file.digest = hashlib.sha1(self.payload).hexdigest()
        self.assertFalse(file.valid())
        file.digest = hashlib.sha1(f"blob {len(self.payload)}\0".encode() + self.payload).hexdigest()
        self.assertTrue(file.valid())

    def test_complete_valid_candidate_does_not_require_network(self):
        file = self.entry(self.payload)
        with patch.object(mt, "probe_source", side_effect=AssertionError("offline")), patch.object(mt, "check_space"):
            with mt.Session(lambda *_: None, lambda: None) as session:
                session.add(mt.Snapshot("repo", "rev", self.root, [file]))
        self.assertTrue(file.reused)

    def test_space_failure_stops_before_content_probe(self):
        file = self.entry()
        with patch.object(mt, "probe_source") as probe, patch.object(mt, "check_space", side_effect=RuntimeError("空间不足")):
            with mt.Session(lambda *_: None, lambda: None) as session:
                with self.assertRaisesRegex(RuntimeError, "空间不足"):
                    session.add(mt.Snapshot("repo", "rev", self.root, [file]))
        probe.assert_not_called()

    def test_size_from_same_disk_is_reserved_together(self):
        session = mt.Session(lambda *_: None, lambda: None)
        session.snapshots = [mt.Snapshot("a", "rev", self.root, [self.entry()]),
                             mt.Snapshot("b", "rev", self.root, [self.entry()])]
        with patch.object(mt, "check_space") as check:
            session.prepare_space()
        self.assertEqual(check.call_args.args[1], len(self.payload) * 2 + 256 * 1024**2)

    def test_metadata_cached_for_immutable_revision_offline(self):
        with patch.object(mm, "app_data_dir", return_value=self.root):
            first = mt._metadata("hf", "repo", "fixed", lambda: [{"name": "file"}])
            again = mt._metadata("hf", "repo", "fixed", lambda: self.fail("must not access network"))
        self.assertEqual(first, again)

    def test_hash_check_is_cancellable(self):
        file = self.entry(self.payload)
        def cancel():
            raise InterruptedError("pause")
        with self.assertRaises(InterruptedError):
            file.valid(cancel)

    def test_decimal_units_are_consistent(self):
        self.assertEqual(mt.format_bytes(1_000_000_000), "1.00 GB")
        self.assertEqual(mt.format_bytes(500_000_000), "500.0 MB")

    def test_progress_excludes_reused_files_and_never_counts_retry_bytes_twice(self):
        file = self.entry()
        part = self.root / "partial"
        file.partials = (part,)
        reused = mt.File(self.root / "already", 100, "unused", reused=True, verified=True)
        session = mt.Session(lambda *_: None, lambda: None)
        session.snapshots = [mt.Snapshot("repo", "rev", self.root, [file, reused])]
        part.write_bytes(self.payload[:4])
        self.assertEqual(session.measure(), (4, len(self.payload)))
        part.write_bytes(self.payload[:4])
        self.assertEqual(session.measure(), (4, len(self.payload)))
        file.path.write_bytes(self.payload)
        self.assertEqual(session.measure(), (len(self.payload), len(self.payload)))

    def test_hf_shared_blob_is_counted_once(self):
        first = self.entry(self.payload)
        second = self.entry(self.payload)
        session = mt.Session(lambda *_: None, lambda: None)
        session.snapshots = [mt.Snapshot("repo", "rev", self.root, [first, second], local_dir=False)]
        self.assertEqual(session.measure(), (len(self.payload), len(self.payload)))

    def test_qwen_uses_its_existing_separate_lock(self):
        from src.core.synthesis.qwen3_model_lock import MODELS
        lock = MODELS["Base"]
        entry = next(e for e in lock["files"] if e["path"] == "model.safetensors")
        with patch.object(mt, "_metadata", return_value=[{
            "name": entry["path"], "size": entry["size"], "digest": entry["sha256"], "lfs": True,
        }]):
            plan = mt.hf_plan(lock["repo"], lock["revision"], self.root, local_dir=False)
        self.assertEqual(plan.files[0].digest, entry["sha256"])
        self.assertEqual(plan.files[0].algorithm, "sha256")

    def test_modelscope_200_with_exact_range_is_a_valid_probe(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.headers = {"Content-Range": "bytes 0-0/1234", "Content-Length": "1"}
        response.iter_content.return_value = iter([b"x"])
        with patch("requests.get", return_value=response):
            mt.probe_source("https://modelscope.cn/file", lambda: None, "modelscope", 1234)
        response.iter_content.assert_called_once_with(1)

    def test_probe_rejects_wrong_file_range(self):
        response = MagicMock(status_code=206)
        response.__enter__.return_value = response
        response.headers = {"Content-Range": "bytes 1-1/1234", "Content-Length": "1"}
        response.iter_content.return_value = iter([b"x"])
        with patch("requests.get", return_value=response), self.assertRaises(RuntimeError):
            mt.probe_source("https://modelscope.cn/file", lambda: None, "modelscope", 1234)

    def test_asr_preflight_can_select_the_pinned_aligner_fallback(self):
        candidate = self.root / "candidate"
        def ms(repo, revision, root):
            if repo == mm._QWEN3_ALIGNER_REPO:
                raise RuntimeError("primary unavailable")
            return mt.Snapshot(repo, revision, Path(root), [])
        def hf(repo, revision, root):
            return mt.Snapshot(repo, revision, Path(root), [])
        with patch.object(mm, "models_root", return_value=self.root), patch.object(mm, "app_data_dir", return_value=self.root), \
                patch.object(mm, "_private_candidate", return_value=(self.root, candidate)), \
                patch.object(mt, "modelscope_plan", side_effect=ms), patch.object(mt, "hf_plan", side_effect=hf), \
                patch.object(mt, "check_space"):
            with mt.Session(lambda *_: None, lambda: None) as session:
                mt.prepare_asr(session, "local_qwen3")
                self.assertIsNotNone(session.find(mm._QWEN3_ALIGNER_REPO,
                    trust.model_revision(mm._QWEN3_ALIGNER_REPO, "hf"), candidate / "_aligner"))

    def test_device_change_recovers_only_after_complete_content_check(self):
        model = self.root / "model"
        model.mkdir()
        payload = model / "weights"
        payload.write_bytes(self.payload)
        base = self.root / "receipts"
        receipt = trust.write_install_receipt("test:model", model, {"revision": "fixed"}, base)
        data = json.loads(receipt.read_text())
        data.pop('root_volume_uuid', None)  # Legacy receipt predates persistent volume identity.
        data["root_device"] += 1
        data["files"][0]["device"] += 1
        receipt.write_text(json.dumps(data))
        self.assertFalse(trust.install_receipt_valid("test:model", model, {"revision": "fixed"}, base))
        self.assertTrue(trust.recover_device_receipt("test:model", model, {"revision": "fixed"}, base))
        self.assertTrue(trust.install_receipt_valid("test:model", model, {"revision": "fixed"}, base))

        data = json.loads(receipt.read_text())
        data["root_device"] += 1
        data["files"][0]["device"] += 1
        receipt.write_text(json.dumps(data))
        before = payload.stat()
        payload.write_bytes(b"X" + self.payload[1:])
        os.utime(payload, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(trust.recover_device_receipt("test:model", model, {"revision": "fixed"}, base))

    def test_legacy_receipt_without_hash_cannot_skip_verification_on_device_change(self):
        model = self.root / "model"
        model.mkdir()
        (model / "weights").write_bytes(self.payload)
        base = self.root / "receipts"
        receipt = trust.write_install_receipt("test:model", model, {}, base)
        data = json.loads(receipt.read_text())
        del data["content_sha256"]
        data["root_device"] += 1
        receipt.write_text(json.dumps(data))
        self.assertFalse(trust.recover_device_receipt("test:model", model, {}, base))


if __name__ == "__main__":
    unittest.main()

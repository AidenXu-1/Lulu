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


if __name__ == "__main__":
    unittest.main()

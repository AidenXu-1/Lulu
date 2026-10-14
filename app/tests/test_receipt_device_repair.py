import json
import tempfile
import unittest
from pathlib import Path
from src.core import artifact_trust as at
from src.core.receipt_device_repair import plan_repair, apply_repair


class DeviceRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve(); self.root = self.base/'model'; self.root.mkdir()
        self.file = self.root/'weights'; self.file.write_bytes(b'trusted')
        self.receipts = self.base/'receipts'; self.trust = {'revision': 'pinned'}
        self.receipt = at.write_install_receipt('test', self.root, self.trust, self.receipts)
        d = json.loads(self.receipt.read_text()); d['root_device'] += 1
        d.pop('root_volume_uuid', None)  # Exercise pre-upgrade legacy receipts.
        for f in d['files']: f['device'] += 1
        self.receipt.write_text(json.dumps(d)); self.original = self.receipt.read_bytes()

    def test_repair_only_device_and_keep_exact_backup(self):
        self.assertFalse(at.install_receipt_valid('test', self.root, self.trust, self.receipts))
        plan_repair('test', self.root, self.trust, self.receipts)
        self.assertEqual(self.receipt.read_bytes(), self.original)
        self.assertTrue(apply_repair('test', self.root, self.trust, self.receipts, self.base/'backup'))
        self.assertTrue(at.install_receipt_valid('test', self.root, self.trust, self.receipts))
        self.assertEqual((self.base/'backup'/self.receipt.name).read_bytes(), self.original)
        self.assertEqual(self.file.read_bytes(), b'trusted')
        self.assertFalse(apply_repair('test', self.root, self.trust, self.receipts, self.base/'backup'))

    def test_refuses_other_changes_without_writing(self):
        for change in ('size', 'mtime_ns', 'inode', 'mode', 'device', 'path'):
            with self.subTest(change=change):
                d = json.loads(self.original)
                d['files'][0][change] = '../escape' if change == 'path' else d['files'][0][change]+1
                self.receipt.write_text(json.dumps(d)); before = self.receipt.read_bytes()
                with self.assertRaises(RuntimeError):
                    apply_repair('test', self.root, self.trust, self.receipts, self.base/'backup')
                self.assertEqual(self.receipt.read_bytes(), before)

    def test_refuses_wrong_trust_missing_files_and_symlinks(self):
        with self.assertRaises(RuntimeError):
            plan_repair('test', self.root, {'revision': 'other'}, self.receipts)
        self.file.unlink()
        with self.assertRaises(OSError):
            plan_repair('test', self.root, self.trust, self.receipts)
        self.file.symlink_to(self.receipt)
        with self.assertRaises(RuntimeError):
            plan_repair('test', self.root, self.trust, self.receipts)

    def test_refuses_directory_identity_change(self):
        d = json.loads(self.original); d['root_inode'] += 1; self.receipt.write_text(json.dumps(d))
        with self.assertRaises(RuntimeError):
            plan_repair('test', self.root, self.trust, self.receipts)

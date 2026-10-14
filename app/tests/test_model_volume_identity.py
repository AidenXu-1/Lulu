"""Reboot/mount-number changes must not uninstall an unchanged model."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core import artifact_trust as at


class VolumeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'model'
        self.root.mkdir()
        (self.root / 'weights').write_bytes(b'original')
        self.base = self.root.parent / 'receipts'
        self.volume = patch.object(at, '_volume_uuid', return_value='volume-A', create=True)
        self.volume.start()
        self.addCleanup(self.volume.stop)
        self.receipt = at.write_install_receipt('model', self.root, {}, self.base)

    def renumber(self, **changes):
        data = json.loads(self.receipt.read_text())
        data['root_device'] += 100
        for entry in data['files']:
            entry['device'] += 100
        data.update(changes)
        self.receipt.write_text(json.dumps(data))

    def valid(self):
        return at.install_receipt_valid('model', self.root, {}, self.base)

    def test_same_volume_survives_device_renumber_without_writing(self):
        self.assertEqual(json.loads(self.receipt.read_text()).get('root_volume_uuid'), 'volume-A')
        self.renumber()
        original = self.receipt.read_bytes()
        self.assertTrue(self.valid())
        self.assertEqual(self.receipt.read_bytes(), original)

    def test_different_volume_rejected_even_with_same_device_number(self):
        with patch.object(at, '_volume_uuid', return_value='volume-B', create=True):
            self.assertFalse(self.valid())

    def test_uuid_unavailable_does_not_accept_changed_device(self):
        self.renumber()
        with patch.object(at, '_volume_uuid', return_value='', create=True):
            self.assertFalse(self.valid())

    def test_changed_file_still_rejected(self):
        self.renumber()
        (self.root / 'weights').write_bytes(b'changed file')
        self.assertFalse(self.valid())

    def test_different_root_inode_still_rejected(self):
        self.renumber(root_inode=-1)
        self.assertFalse(self.valid())

    def test_file_on_different_device_still_rejected(self):
        self.renumber()
        original = at._file_entry
        def entry(root, path):
            result = original(root, path)
            result['device'] += 1
            return result
        with patch.object(at, '_file_entry', side_effect=entry):
            self.assertFalse(self.valid())

    def test_legacy_receipts_remain_strict(self):
        self.renumber()
        data = json.loads(self.receipt.read_text())
        data.pop('root_volume_uuid', None)
        self.receipt.write_text(json.dumps(data))
        self.assertFalse(self.valid())


if __name__ == '__main__':
    unittest.main()

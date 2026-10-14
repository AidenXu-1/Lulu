"""Private temporary files and fake process observations only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.updater.install import InstallTransaction
from src.core.updater.models import UpdateError, UpdateSnapshot
from src.core.updater.state import StateStore
from src.core.updater import helper_protocol as protocol


class HelperProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'updates'
        self.tx = InstallTransaction(self.root)
        self.txn = 'a' * 64
        self.path = self.tx.transactions / self.txn / 'install_plan.json'
        self.path.parent.mkdir(mode=0o700)
        self.plan = dict(txn=self.txn, nonce='b'*64, plan_sha256='c'*64,
                         target_app='/fixture/Lulu.app')
        self.tx.acquire(self.txn)
        StateStore(self.root).save(UpdateSnapshot(txn=self.txn, state='installing', ui_state='preparing'))

    def test_ack_bound_to_plan_and_live_child(self):
        with patch.object(protocol, 'current_identity', return_value=('start', '/fixture/Lulu.app/Contents/MacOS/Lulu')):
            protocol.accept(self.path, self.plan, self.tx)
        with patch.object(protocol, 'matches_process', return_value=True):
            import os
            self.assertTrue(protocol.accepted(self.path, self.plan, os.getpid()))
            self.assertFalse(protocol.accepted(self.path, {**self.plan, 'nonce':'d'*64}, os.getpid()))
            self.assertFalse(protocol.accepted(self.path, self.plan, os.getpid()+1))
        with patch.object(protocol, 'matches_process', return_value=False):
            self.assertFalse(protocol.accepted(self.path, self.plan, os.getpid()))

    def test_pre_swap_failure_durable_before_owned_lock_release(self):
        protocol.fail_before_swap(self.path, self.plan, self.tx, 'old_process_changed', 3)
        state = StateStore(self.root).load()
        self.assertEqual((state.state, state.ui_state, state.error_code), ('failed','preparation_error','old_process_changed'))
        self.assertFalse(self.tx.lock.exists())
        self.assertTrue((self.path.parent/'launch-result.json').is_file())

    def test_state_write_failure_keeps_recovery_lock(self):
        with patch.object(StateStore, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                protocol.fail_before_swap(self.path, self.plan, self.tx, 'old_process_changed', 3)
        self.assertTrue(self.tx.lock.exists())
        self.assertTrue((self.path.parent/'launch-result.json').exists())

    def test_foreign_lock_never_released_or_state_overwritten(self):
        self.tx.lock.write_text('d'*64+'\n')
        with self.assertRaises(UpdateError):
            protocol.fail_before_swap(self.path, self.plan, self.tx, 'old_process_changed', 3)
        self.assertEqual(StateStore(self.root).load().state, 'installing')
        self.assertEqual(self.tx.lock.read_text().strip(), 'd'*64)

    def test_protocol_replay_and_symlink_rejected(self):
        with patch.object(protocol, 'current_identity', return_value=('start', '/fixture/Lulu.app/Contents/MacOS/Lulu')):
            protocol.accept(self.path, self.plan, self.tx)
            with self.assertRaises(UpdateError): protocol.accept(self.path, self.plan, self.tx)
        receipt=self.path.parent/'helper-accepted.json'
        receipt.unlink(); receipt.symlink_to(self.root/'state.json')
        with self.assertRaises((OSError,UpdateError)):
            protocol.accepted(self.path, self.plan, 1)

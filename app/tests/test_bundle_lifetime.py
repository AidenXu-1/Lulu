"""Real kernel file events on temporary files; no process or user state changes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch, Mock
import unittest


class BundleLifetimeTests(unittest.TestCase):
    def test_app_rename_notifies_only_its_own_observer(self):
        from src.core.agent_entry.bundle_lifetime import watch_app_replacement
        with TemporaryDirectory() as temporary:
            root=Path(temporary); app=root/'Lulu.app'
            core=app/'Contents/MacOS/lulu-agent-core';core.parent.mkdir(parents=True);core.write_bytes(b'fixture')
            changed=Event();stop=watch_app_replacement(app,changed.set)
            try:
                (root/'unrelated').write_text('leave alone')
                self.assertFalse(changed.wait(.05))
                app.rename(root/'previous.app')
                self.assertTrue(changed.wait(2))
            finally:
                stop()

    def test_core_write_notifies_and_clean_cancellation_does_not(self):
        from src.core.agent_entry.bundle_lifetime import watch_app_replacement
        with TemporaryDirectory() as temporary:
            app=Path(temporary)/'Lulu.app';core=app/'Contents/MacOS/lulu-agent-core'
            core.parent.mkdir(parents=True);core.write_bytes(b'fixture')
            changed=Event();stop=watch_app_replacement(app,changed.set)
            core.write_bytes(b'replacement');self.assertTrue(changed.wait(2));stop()
            clean=Event();stop=watch_app_replacement(app,clean.set);stop();self.assertFalse(clean.is_set())

    def test_core_exit_failure_blocks_rollback(self):
        from src.core.updater.helper_entry import stop_update_core
        from src.core.updater.install import RollbackBlocked
        from src.core.agent_entry.standalone_source import DESCRIPTOR
        with TemporaryDirectory() as temporary:
            app=Path(temporary).resolve()/'Lulu.app';descriptor=app/DESCRIPTOR
            descriptor.parent.mkdir(parents=True);descriptor.write_bytes(b'fixture')
            with patch('src.core.agent_entry.effects.CurrentUserLaunchctlService') as service:
                service.return_value.terminate_registered_process.side_effect=RuntimeError('still alive')
                with self.assertRaises(RollbackBlocked):
                    stop_update_core({'target_app':str(app)})
                service.return_value.stop.assert_not_called()

"""Shared login invariants using temporary profiles and an in-memory secret."""
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
from src.core.collect.profiles import ProfileRegistry
from src.core.live_recording.contracts import LiveError
from tests.test_live_recording import ServiceTests

class SharedLoginTests(unittest.TestCase):
    def setUp(self):
        ServiceTests.setUp(self)
        self.secret = ''
        self.service.secret_read = lambda: self.secret
        def write(value): self.secret = value
        self.service.secret_write = write
        self.profile = ProfileRegistry(self.root).resolve('douyin', create=True).path
        self.mark = self.profile / '.logged_in'

    def call(self, **args):
        return self.service.handle('credential', {'session': self.session, **args})

    def snapshot(self):
        return self.service.snapshot('hosts', '', 'all', 1)

    def test_clear_removes_browser_profile_and_live_cookie_together(self):
        self.mark.write_text('ok')
        (self.profile/'Cookies').write_text('synthetic-profile')
        self.call(value='sessionid=synthetic')
        self.call(value='')
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.secret, '')
        self.assertFalse(self.snapshot()['credential'])
        self.assertFalse(self.snapshot()['login']['browser'])

    def test_browser_save_updates_both_pages_manual_does_not_create_marker(self):
        self.call(value='sessionid=synthetic', source='browser')
        self.assertTrue(self.mark.exists())
        self.assertEqual(self.snapshot()['login']['source'], 'browser')
        self.call(value='')
        self.call(value='sessionid=manual')
        self.assertFalse(self.mark.exists())
        self.assertEqual(self.snapshot()['login']['source'], 'manual')

    def test_failed_save_preserves_existing_profile_and_credential(self):
        self.mark.write_text('ok')
        self.call(value='sessionid=old')
        def fail(value): raise RuntimeError('do-not-disclose-synthetic')
        self.service.secret_write = fail
        with self.assertRaises(LiveError) as caught:
            self.call(value='sessionid=new', source='browser')
        self.assertNotIn('do-not-disclose', str(caught.exception))
        self.assertTrue(self.mark.exists())
        self.assertEqual(self.secret, 'sessionid=old')

    def test_failed_clear_keeps_profile_and_old_cookie(self):
        self.mark.write_text('ok')
        self.call(value='sessionid=old')
        self.service.secret_write = lambda v: (_ for _ in ()).throw(RuntimeError('synthetic'))
        with self.assertRaises(LiveError): self.call(value='')
        self.assertTrue(self.mark.exists())
        self.assertEqual(self.secret, 'sessionid=old')
        self.assertTrue(self.snapshot()['credential'])

    def test_profile_lease_blocks_clear_and_manual_replacement(self):
        self.mark.write_text('ok')
        with ProfileRegistry(self.root).acquire('douyin'):
            with self.assertRaises(LiveError): self.call(value='')
            with self.assertRaises(LiveError): self.call(value='sessionid=manual')
        self.assertTrue(self.mark.exists())

    def test_closed_browser_connection_does_not_block_login_clear(self):
        self.mark.write_text('ok')
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            with socket.create_connection(('127.0.0.1', port)) as client:
                connection, _ = listener.accept()
                connection.close()
                self.assertEqual(client.recv(1), b'')
        # Closed CDP connections still occupy the port without SO_REUSEADDR.
        with patch('src.core.collect.douyin_login.debug_port_for', return_value=port):
            self.call(value='')
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.snapshot()['login']['browser'])

    def test_open_browser_listener_still_blocks_clear_with_specific_message(self):
        self.mark.write_text('ok')
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with patch('src.core.collect.douyin_login.debug_port_for', return_value=listener.getsockname()[1]):
                with self.assertRaisesRegex(LiveError, '关闭弹出的抖音浏览器窗口'):
                    self.call(value='')
        self.assertTrue(self.mark.exists())

    def test_partial_profile_clear_is_visible_and_retry_completes(self):
        self.mark.write_text('ok')
        self.call(value='sessionid=old')
        with patch('src.core.collect.douyin_login.shutil.rmtree', side_effect=OSError('synthetic')):
            with self.assertRaises(LiveError): self.call(value='')
        self.assertTrue(self.snapshot()['login']['partial'])
        self.call(value='')
        self.assertFalse(self.snapshot()['login']['partial'])
        self.assertFalse(self.snapshot()['credential'])

    def test_passive_state_never_reads_secret_and_legacy_state_is_preserved(self):
        self.mark.write_text('ok')
        self.service.index.set_setting('credential', 'configured')
        self.service.secret_read = lambda: (_ for _ in ()).throw(AssertionError('passive read'))
        state = self.snapshot()['login']
        self.assertTrue(state['browser'])
        self.assertTrue(state['cookie'])

    def test_profile_symlink_and_unsafe_pending_clear_cannot_delete_other_files(self):
        protected = self.root/'protected'; protected.mkdir(); (protected/'keep').write_text('keep')
        self.service.index.set_setting('login_clear_pending', '../protected')
        with self.assertRaises(LiveError): self.call(value='')
        self.assertTrue((protected/'keep').exists())

    def test_write_then_error_restores_previous_secret(self):
        self.call(value='sessionid=old')
        def write(value):
            self.secret = value
            if value == 'sessionid=new':
                raise RuntimeError('write applied but not confirmed')
        self.service.secret_write = write
        with self.assertRaises(LiveError):
            self.call(value='sessionid=new', source='browser')
        self.assertEqual(self.secret, 'sessionid=old')
        self.assertFalse(self.mark.exists())
        self.assertTrue(self.snapshot()['login']['error'])

    def test_uncertain_rollback_is_visible_and_blocks_credential_use(self):
        self.call(value='sessionid=old')
        def write(value):
            self.secret = value
            raise RuntimeError('unconfirmed write')
        self.service.secret_write = write
        with self.assertRaises(LiveError): self.call(value='sessionid=new')
        self.assertTrue(self.snapshot()['login']['error'])
        with self.assertRaises(LiveError): self.service._read_credentials()

    def test_slow_explicit_credential_change_does_not_expire_idle_gui_session(self):
        import time
        clock = [time.monotonic()]
        self.service.deadline = clock[0] + 30
        def slow_write(value):
            self.secret = value
            clock[0] += 60
        self.service.secret_write = slow_write
        with patch('src.core.live_recording.service.time.monotonic', side_effect=lambda: clock[0]):
            self.call(value='sessionid=slow-fixture')
            result = self.service.handle('heartbeat', {'session': self.session})
        self.assertFalse(result['stopped'])
        self.assertFalse(self.service.closing)

class SharedCollectionLoginTests(unittest.TestCase):
    def test_full_collection_uses_shared_state_instead_of_old_browser_marker(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from src.ui.collection_page import CollectionPage
        for label, allowed in [('登录状态待检查', False), ('已保存 Cookie', False), ('已登录', True)]:
            page = SimpleNamespace(_active_collection_mode='', _douyin_login_state=lambda: (label, True),
                _on_login=Mock(), _on_full_grab=Mock())
            with self.subTest(label=label), patch('src.ui.collection_page.is_logged_in', return_value=True):
                CollectionPage._on_full_primary(page)
                self.assertEqual(page._on_full_grab.called, allowed)
                self.assertEqual(page._on_login.called, not allowed)

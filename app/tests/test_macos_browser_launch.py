"""No live Chrome, credentials, user profiles or system permissions."""
import os
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from DrissionPage import ChromiumOptions
from src.core.collect import macos_browser as m


class MacBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.exe = root / 'Browser.app/Contents/MacOS/Browser'
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text('fixture')
        self.profile = root / 'isolated profile'
        self.profile.mkdir()
        self.options = ChromiumOptions(read_file=False)
        self.options.set_browser_path(str(self.exe))
        self.options.set_user_data_path(str(self.profile))
        self.options.set_local_port(43123)
        self.process = m.system.ChromeProcess(1234, 'birth', self.exe, os.getuid(),
            f'{self.exe} --user-data-dir={self.profile} --remote-debugging-port=43123 --headless=new', 200)
        self.patches = {}
        for name, result in (
            ('_launch_process', None), ('_listener_processes', (self.process,)),
            ('_listener_hosts', {'127.0.0.1'}), ('_probe_cdp', {'Browser':'Chrome/153'}),
            ('_chrome_process_snapshot', (self.process,)),
        ):
            p = patch.object(m.system, name, return_value=result)
            self.patches[name] = p.start()
            self.addCleanup(p.stop)
        p = patch.object(m.time, 'time_ns', return_value=100)
        p.start(); self.addCleanup(p.stop)
        self.factory = Mock()

    def test_launch_attaches_only_and_preserves_security_features(self):
        with patch.object(m, '_stop_owned') as stop:
            page = m.create_page(self.options, self.factory)
            self.assertTrue(self.factory.call_args.args[0].is_existing_only)
            args = self.patches['_launch_process'].call_args.args[1]
            self.assertIn('--remote-debugging-address=127.0.0.1', args)
            self.assertFalse(any('MacAppCodeSignClone' in a for a in args))
            page.quit(timeout=.5)
            stop.assert_called_once_with(self.process)

    def test_foreign_listener_never_attached_or_stopped(self):
        stranger = replace(self.process, command_line='daily chrome', birth_epoch_ns=1)
        self.patches['_listener_processes'].return_value = (stranger,)
        self.patches['_chrome_process_snapshot'].return_value = (stranger,)
        with patch.object(m, '_stop_owned') as stop, patch.object(m.time, 'monotonic', side_effect=[0, 0, 0, 11]):
            with self.assertRaisesRegex(RuntimeError, '身份不匹配'):
                m.create_page(self.options, self.factory)
            self.factory.assert_not_called()
            stop.assert_not_called()

    def test_attach_failure_reaps_owned_browser(self):
        self.factory.side_effect = RuntimeError('attach failed')
        with patch.object(m, '_stop_owned') as stop:
            with self.assertRaisesRegex(RuntimeError, 'attach failed'):
                m.create_page(self.options, self.factory)
            stop.assert_called_once_with(self.process)

    def test_empty_or_external_listener_hosts_fail_closed(self):
        for hosts in (set(), {'*'}, {'0.0.0.0'}):
            with self.subTest(hosts=hosts), patch.object(m, '_stop_owned') as stop:
                self.patches['_listener_hosts'].return_value = hosts
                with self.assertRaisesRegex(RuntimeError, '监听范围'):
                    m.create_page(self.options, self.factory)
                self.factory.assert_not_called()
                stop.assert_called_once_with(self.process)

    def test_submitted_timeout_reaps_only_matching_profile(self):
        self.patches['_launch_process'].side_effect = TimeoutError('late submit')
        stranger = replace(self.process, pid=777, command_line='daily chrome')
        self.patches['_chrome_process_snapshot'].return_value = (stranger, self.process)
        with patch.object(m, '_stop_owned') as stop:
            with self.assertRaises(TimeoutError):
                m.create_page(self.options, self.factory)
            stop.assert_called_once_with(self.process)
            self.factory.assert_not_called()

    def test_profile_or_port_prefix_is_not_ownership(self):
        installation = m.system.ChromeInstallation(self.exe, '')
        for text in (self.process.command_line.replace(str(self.profile), str(self.profile)+'-other'),
                     self.process.command_line.replace('43123 ', '431234 ')):
            self.assertFalse(m._owned(replace(self.process, command_line=text),
                             installation, self.profile, 43123, 100))

    def test_recycled_pid_is_never_signalled(self):
        with patch.object(m.system, '_inspect_process', return_value=replace(self.process,birth_epoch_ns=201)), \
             patch.object(m.os, 'kill') as kill:
            m._stop_owned(self.process)
            kill.assert_not_called()

    def test_close_error_still_reaps_owned_process(self):
        raw = Mock(); raw.quit.side_effect = RuntimeError('close failed')
        with patch.object(m, '_stop_owned') as stop:
            with self.assertRaisesRegex(RuntimeError, 'close failed'):
                m._OwnedPage(raw, self.process).quit()
            stop.assert_called_once_with(self.process)

    def test_xhs_attach_and_close_never_start_a_replacement_browser(self):
        from types import SimpleNamespace
        from src.core.collect.xhs_process_worker import _attach_verified_session
        from src.core.collect.application import _request_xhs_normal_close
        with patch('DrissionPage.ChromiumPage', side_effect=RuntimeError('endpoint gone')) as factory:
            with self.assertRaisesRegex(RuntimeError, 'endpoint gone'):
                _attach_verified_session({'cdp_port': 43123})
            self.assertTrue(factory.call_args.args[0].is_existing_only)
            self.assertFalse(_request_xhs_normal_close(SimpleNamespace(endpoint=SimpleNamespace(port=43123))))
            self.assertTrue(factory.call_args.args[0].is_existing_only)

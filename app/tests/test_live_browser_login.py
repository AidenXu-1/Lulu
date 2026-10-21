"""Credential-free login tests; fake owned browser, explicit temporary root."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.core.collect.browser_shell import BrowserShellError
from src.core.live_recording.browser_login import cookie_header, login_cookie
from src.core.live_recording.contracts import LiveError


class LiveBrowserLoginTests(unittest.TestCase):
    def cookie(self, name='sessionid', value='isolated-fixture', domain='.douyin.com', expires=-1):
        return dict(name=name, value=value, domain=domain, expires=expires)

    def page(self, cookies):
        return SimpleNamespace(url='https://www.douyin.com/', get=Mock(), cookies=Mock(return_value=cookies),
            quit=Mock(), set=SimpleNamespace(timeouts=Mock(), retry_times=Mock(), load_mode=SimpleNamespace(eager=Mock())))

    def test_only_current_douyin_session_and_safe_header_values(self):
        values = [self.cookie(), self.cookie('other', domain='example.com'),
                  self.cookie('expired', expires=5), self.cookie('injected', 'bad\r\nvalue'),
                  self.cookie('semicolon', 'bad;value'), self.cookie('sessionid_ss', 'second')]
        self.assertEqual(cookie_header(values, now=10), 'sessionid=isolated-fixture; sessionid_ss=second')
        self.assertEqual(cookie_header([self.cookie(domain='douyin.com.attacker.example')]), '')
        self.assertEqual(cookie_header([self.cookie('ttwid')]), '')
        self.assertEqual(cookie_header([self.cookie(expires=5)], now=10), '')

    def test_oversized_session_fails_without_disclosing_value(self):
        with self.assertRaises(LiveError) as caught:
            cookie_header([self.cookie(value='fixture-'*3000)])
        self.assertNotIn('fixture-', str(caught.exception))

    def test_success_uses_owned_shell_and_always_closes(self):
        page = self.page([self.cookie()])
        shell = SimpleNamespace(open=Mock(return_value=page))
        with TemporaryDirectory() as temporary:
            result = login_cookie(Path(temporary), shell=shell)
        self.assertEqual(result, 'sessionid=isolated-fixture')
        spec = shell.open.call_args.args[0]
        self.assertEqual((spec.platform_id, spec.profile_id), ('douyin', 'default'))
        page.cookies.assert_called_once_with(all_domains=False, all_info=True)
        page.quit.assert_called_once()

    def test_cancel_before_open_and_cancel_after_navigation_never_returns_secret(self):
        shell = SimpleNamespace(open=Mock())
        self.assertEqual(login_cookie(Path('/isolated-fixture'), shell=shell, is_cancelled=lambda: True), '')
        shell.open.assert_not_called()
        cancelled = []
        page = self.page([self.cookie()])
        page.get.side_effect = lambda url: cancelled.append(True)
        shell.open.return_value = page
        self.assertEqual(login_cookie(Path('/isolated-fixture'), shell=shell, is_cancelled=lambda: bool(cancelled)), '')
        page.cookies.assert_not_called()
        page.quit.assert_called_once()

    def test_profile_contention_browser_failure_and_timeout_preserve_login(self):
        shell = SimpleNamespace(open=Mock(side_effect=BrowserShellError('profile_busy', 'fixture')))
        with self.assertRaisesRegex(LiveError, '主页采集'):
            login_cookie(Path('/isolated-fixture'), shell=shell)
        page = self.page([])
        shell.open = Mock(return_value=page)
        with self.assertRaisesRegex(LiveError, '原登录信息保留'):
            login_cookie(Path('/isolated-fixture'), shell=shell, timeout=0)
        page.quit.assert_called_once()
        page.quit.reset_mock()
        page.get.side_effect = RuntimeError('sessionid=must-not-leak-fixture')
        with self.assertRaises(LiveError) as caught:
            login_cookie(Path('/isolated-fixture'), shell=shell)
        self.assertNotIn('must-not-leak', str(caught.exception))
        page.quit.assert_called_once()

    def test_browser_missing_and_verification_errors_have_different_next_steps(self):
        for code, expected in [('browser_missing', '先安装 Google Chrome'), ('browser_unverified', '更新或修复 Chrome')]:
            shell = SimpleNamespace(open=Mock(side_effect=BrowserShellError(code, 'synthetic diagnostic')))
            with self.subTest(code=code), self.assertRaises(LiveError) as caught:
                login_cookie(Path('/isolated-fixture'), shell=shell)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('synthetic diagnostic', str(caught.exception))

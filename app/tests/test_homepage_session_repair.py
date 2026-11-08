"""Anonymous protocol and login surfaces, with isolated stores and fake I/O."""
import hashlib
import json
import unittest
from types import SimpleNamespace
from http.cookiejar import Cookie
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from src.core.collect import douyin_fetch as fetch
from tests.test_douyin_anonymous_response import payload, URL
from tests.test_homepage_login_feedback import HiddenLoginStateTests, CollectionLoginViewTests


class PresenceTests(HiddenLoginStateTests):
    def test_empty_browser_profile_is_not_saved_login(self):
        self.controller.login_presence = {'profile': True, 'browser': False, 'cookie': False}
        self.assertEqual(self.page.login_state(), ('未登录', False))

    def test_partial_clear_and_saved_cookie_still_offer_recovery(self):
        for data in ({'partial': True}, {'cookie': True, 'source': 'manual'}):
            self.controller.login_presence = data
            label, clearable = self.page.login_state()
            self.assertNotEqual(label, '未登录')
            self.assertTrue(clearable)

    def test_collection_clear_uses_shared_mutation_without_showing_native_overlay(self):
        self.controller.login_presence = {'browser': True}
        self.page._save_credential = Mock()
        report = Mock()
        self.assertTrue(self.page.collection_clear_login(report))
        self.page._save_credential.assert_called_once_with('', feedback=report)
        self.assertFalse(self.page.credentials.isVisible())

    def test_collection_clear_rechecks_busy_state_before_mutation(self):
        self.controller.login_presence = {'browser': True}
        self.controller.active = True
        self.page._save_credential = Mock()
        report = Mock()
        self.assertFalse(self.page.collection_clear_login(report))
        self.page._save_credential.assert_not_called()
        self.assertIn('直播', report.call_args.args[0])


class LoginViewTests(CollectionLoginViewTests):
    def set_login(self, label='已登录', clearable=True):
        self.page._douyin_login_state = lambda: (label, clearable)
        self.page._publish(force=True)
        self.until(lambda: self.js('document.querySelector("#loginStatus span").textContent') == label)

    def test_unsigned_surface_shows_only_login_even_if_profile_is_clearable(self):
        self.set_login('未登录', True)
        self.assertTrue(self.js('document.getElementById("clearLogin").hidden'))
        self.assertFalse(self.js('document.getElementById("loginButton").hidden'))

    def test_clear_confirmation_is_visible_inside_web_page_and_cancel_does_not_clear(self):
        self.set_login()
        self.page._on_clear_login = Mock()
        clear = self.page._browser_clear_login = Mock(return_value=True)
        self.js('document.getElementById("clearLogin").click()')
        self.until(lambda: self.js('document.getElementById("modal").open'))
        self.assertIn('清除', self.js('document.getElementById("modalTitle").textContent'))
        self.assertTrue(self.js('(()=>{let r=document.getElementById("modal").getBoundingClientRect();return r.width>200&&r.height>100&&r.left>=0&&r.top>=0})()'))
        clear.assert_not_called()
        self.page._on_clear_login.assert_not_called()
        self.js('document.getElementById("cancelClearLogin").click()')
        self.assertFalse(self.js('document.getElementById("modal").open'))
        clear.assert_not_called()

    def test_confirm_clear_dispatches_once_and_never_opens_native_overlay(self):
        self.set_login()
        self.page._on_clear_login = Mock()
        clear = self.page._browser_clear_login = Mock(return_value=True)
        self.js('document.getElementById("clearLogin").click()')
        self.until(lambda: self.js('document.getElementById("modal").open'))
        self.assertTrue(self.js('(()=>{const button=document.getElementById("confirmClearLogin");button.click();button.click();const event=new Event("cancel",{cancelable:true});document.getElementById("modal").dispatchEvent(event);return event.defaultPrevented&&document.getElementById("cancelClearLogin").disabled})()'))
        self.until(lambda: clear.called)
        self.assertEqual(clear.call_count, 1)
        self.page._on_clear_login.assert_not_called()

    def test_saved_browser_partial_collection_uses_browser_first_page_directly(self):
        self.set_login()
        self.page.input.setText(URL)
        started = []
        with patch.object(self.page, '_start_collect', side_effect=lambda command, **_:started.append(command)):
            self.page._on_grab()
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].mode, 'profile_latest')
        self.assertEqual(started[0].raw_input, URL)
        self.assertLessEqual(started[0].limit, 20)


class AnonymousProtocolTests(unittest.TestCase):
    def request(self, detail=False):
        session = fetch._AnonymousSession('ttwid=isolated', 'server-issued-visitor')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read = Mock(return_value=json.dumps(
            dict(status_code=0, aweme_detail=payload()['aweme_list'][0]) if detail else payload()).encode())
        with patch.object(fetch, '_register_ttwid', return_value=session), \
             patch.object(fetch.time, 'time', return_value=1794110000), \
             patch.object(fetch.urllib.request, 'urlopen', return_value=response) as opened:
            items = fetch.fetch_item('1234567890123456789') if detail else fetch.fetch_latest(URL, count=3)
        request = opened.call_args.args[0]
        pairs = dict(parse_qs(urlsplit(request.full_url).query, keep_blank_values=True))
        query = {key: value[0] for key,value in pairs.items()}
        signature = query.pop('x-secsdk-web-signature')
        self.assertEqual(query['uifid'], 'server-issued-visitor')
        self.assertEqual(query['timestamp'], '1794110000')
        expected = hashlib.md5(('server-issued-visitor_1794110000_A96D855A08C0A9707F8BEF0D9A527E4E_'+urlencode(query)).encode()).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(request.get_header('Cookie'), 'ttwid=isolated')
        return items

    def test_homepage_request_has_current_server_identity_and_exact_query_signature(self):
        self.assertEqual(len(self.request()), 1)

    def test_detail_request_uses_the_same_current_anonymous_protocol(self):
        self.assertEqual(self.request(detail=True).author_id, payload()['aweme_list'][0]['author']['sec_uid'])

    def test_registration_takes_visitor_identity_from_server_header_without_credentials(self):
        def opener_factory(processor, _https):
            processor.cookiejar.set_cookie(Cookie(
                version=0, name='ttwid', value='anonymous-ttwid', port=None, port_specified=False,
                domain='.douyin.com', domain_specified=True, domain_initial_dot=True,
                path='/', path_specified=True, secure=True, expires=None, discard=True,
                comment=None, comment_url=None, rest={}))
            response = Mock(headers={'x-tt-trace-host':'server-visitor-identity'})
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return SimpleNamespace(open=Mock(return_value=response))
        with patch.object(fetch.urllib.request, 'build_opener', side_effect=opener_factory), \
             patch.object(fetch, '_read_anonymous_response', return_value=b''):
            session = fetch._register_ttwid()
        self.assertEqual(session.uifid, 'server-visitor-identity')
        self.assertEqual(session.cookie, 'ttwid=anonymous-ttwid')

    def test_registration_missing_server_identity_is_not_reported_as_a_login_failure(self):
        session = fetch._AnonymousSession('ttwid=isolated', '')
        with self.assertRaises(fetch.DouyinCollectError) as caught:
            fetch._signed_anonymous_params(fetch._build_params('MS4w_test', 3), session)
        self.assertEqual(caught.exception.code, 'DOUYIN_EMPTY_RESPONSE')

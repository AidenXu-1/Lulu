import unittest
from unittest.mock import patch, Mock
from http.cookiejar import Cookie
from src.core.audio import extractor
from src.core.collect import douyin_fetch
from tests.test_douyin_single_link_fallback import WORK_ID, SHARE_URL, _video_item


class DouyinSessionRecoveryTests(unittest.TestCase):
    def test_short_link_keeps_resolved_identity_when_html_is_an_empty_shell(self):
        response = Mock()
        response.read.return_value = b'<html>public shell</html>'
        response.geturl.return_value = f'https://www.iesdouyin.com/share/video/{WORK_ID}/'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(extractor.urllib.request, 'build_opener', return_value=opener), patch(
            'src.core.collect.douyin_fetch.fetch_item', return_value=_video_item()
        ) as detail:
            result = extractor._douyin_play_url(SHARE_URL, lambda *_: None)
        self.assertEqual(result[4], WORK_ID)
        detail.assert_called_once_with(WORK_ID)
        response.__exit__.assert_called_once()

    def test_anonymous_registration_keeps_first_party_web_cookies(self):
        from http.cookiejar import CookieJar
        jar = CookieJar()
        calls = []
        def opened(req, **_kwargs):
            url = req.full_url
            calls.append(url)
            name = '__ac_nonce' if url == 'https://www.douyin.com/' else 'ttwid'
            domain = 'www.douyin.com' if name == '__ac_nonce' else '.bytedance.com'
            jar.set_cookie(Cookie(0,name,'test-value',None,False,domain,True,domain.startswith('.'), '/',True,True,None,True,None,None,{},False))
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b''
            return response
        opener = Mock()
        opener.open.side_effect = opened
        with patch.object(douyin_fetch.cookiejar, 'CookieJar', return_value=jar), patch.object(
            douyin_fetch.urllib.request, 'build_opener', return_value=opener
        ):
            cookies = douyin_fetch._register_ttwid()
        self.assertEqual(calls[0], 'https://www.douyin.com/')
        self.assertIn('__ac_nonce=test-value', cookies)
        self.assertIn('ttwid=test-value', cookies)


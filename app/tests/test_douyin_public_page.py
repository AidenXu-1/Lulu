from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from src.core.collect.douyin_public_page import fetch_rendered_item
from src.core.collect.profiles import ProfileRegistry
from src.core.collect.douyin_fetch import DouyinCollectError, fetch_item_with_page_fallback


class DouyinPublicPageTests(unittest.TestCase):
    def browser(self, bodies):
        page=Mock()
        page.url='https://www.douyin.com/video/100'
        page.listen.wait.side_effect=[SimpleNamespace(response=SimpleNamespace(body=body)) for body in bodies]
        closed=[]
        @contextmanager
        def opened(spec):
            self.assertTrue(spec.profile_id.startswith('public-'))
            ProfileRegistry(self.profile_root).resolve(spec.platform_id, spec.profile_id, create=True)
            try:
                yield page
            finally:
                closed.append(True)
        shell=Mock()
        shell.open.side_effect=opened
        def factory(root):
            self.profile_root = root
            return shell
        return page,closed,factory

    def test_only_requested_work_is_accepted_and_owned_browser_closes(self):
        body=lambda identity:{'status_code':0,'aweme_detail':{'aweme_id':identity,'desc':'正确作品','video':{'play_addr':{'url_list':['https://v26-web.douyinvod.com/a.mp4']}}}}
        page,closed,factory=self.browser([body('999'),body('100')])
        item=fetch_rendered_item('100',lambda *_:None,shell_factory=factory)
        self.assertEqual(item.work_id,'100')
        page.get.assert_called_once_with('https://www.douyin.com/video/100')
        self.assertEqual(closed,[True])

    def test_cancellation_closes_owned_browser_without_another_network_wait(self):
        page,closed,factory=self.browser([])
        def progress(message,_fraction):
            if message.startswith('等待'):
                raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):
            fetch_rendered_item('100',progress,shell_factory=factory)
        page.listen.wait.assert_not_called()
        self.assertEqual(closed,[True])

    def test_api_success_does_not_launch_browser_and_rejection_falls_back_once(self):
        with patch('src.core.collect.douyin_fetch.fetch_item',return_value='public-item'), patch(
            'src.core.collect.douyin_public_page.fetch_rendered_item') as browser:
            self.assertEqual(fetch_item_with_page_fallback('100',lambda *_:None),'public-item')
            browser.assert_not_called()
        with patch('src.core.collect.douyin_fetch.fetch_item',side_effect=DouyinCollectError('403',code='DOUYIN_ACCESS_RESTRICTED')), patch(
            'src.core.collect.douyin_public_page.fetch_rendered_item',return_value='rendered-item') as browser:
            self.assertEqual(fetch_item_with_page_fallback('100',lambda *_:None),'rendered-item')
            self.assertEqual(browser.call_count,1)

    def test_identity_error_does_not_open_browser(self):
        with patch('src.core.collect.douyin_fetch.fetch_item',side_effect=DouyinCollectError('identity conflict')), patch(
            'src.core.collect.douyin_public_page.fetch_rendered_item') as browser:
            with self.assertRaises(DouyinCollectError):
                fetch_item_with_page_fallback('100',lambda *_:None)
            browser.assert_not_called()

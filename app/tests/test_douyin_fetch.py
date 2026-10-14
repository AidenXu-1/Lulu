"""抓取层离线测试:sec_uid 解析 + aweme 列表解析(纯函数,不联网)。"""

import ssl
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect import douyin_fetch
from core.collect.douyin_fetch import (
    DouyinCollectError,
    parse_aweme_detail,
    parse_aweme_list,
    resolve_sec_uid,
)


class TestTlsVerification(unittest.TestCase):
    """安全红线:路A 必须校验 TLS 证书(防中间人),绝不能再退回 CERT_NONE。"""

    def test_cert_verification_enabled(self):
        self.assertEqual(douyin_fetch._SSL.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(douyin_fetch._SSL.check_hostname)


class TestResolveSecUid(unittest.TestCase):
    def test_from_user_url(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAAE5xQ-abc_def?from=x"
        self.assertEqual(resolve_sec_uid(url), "MS4wLjABAAAAE5xQ-abc_def")

    def test_extract_from_pasted_text(self):
        # 整段分享文案里嵌 /user/ 链接,应抠出 sec_uid
        txt = "看看这个作者 https://www.douyin.com/user/MS4wLjABAAAA_xyz-1?from=x 很棒"
        self.assertEqual(resolve_sec_uid(txt), "MS4wLjABAAAA_xyz-1")

    def test_no_sec_uid_and_not_url_raises(self):
        with self.assertRaises(DouyinCollectError):
            resolve_sec_uid("不是链接也没有secuid")


class TestParse(unittest.TestCase):
    def test_parse_and_dedupe(self):
        payload = {"aweme_list": [
             {"aweme_id": "1", "desc": "a", "create_time": 1781416800, "aweme_type": 0,
             "author": {"nickname": "作者"}, "video": {"duration": 60000,
             "play_addr": {"url_list": ["https://cdn/1.mp4"]},
             "cover": {"url_list": ["https://cdn/1.jpg"]}},
             "statistics": {"digg_count": 5, "collect_count": 2, "comment_count": 1, "share_count": 0}},
            {"aweme_id": "1", "desc": "dup"},          # 重复 id,应被去重
            {"aweme_id": "2", "desc": "b", "author": {"nickname": "作者"}},
        ]}
        items = parse_aweme_list(payload)
        self.assertEqual([i.aweme_id for i in items], ["1", "2"])
        self.assertEqual(items[0].like, 5)
        self.assertEqual(items[0].duration_text, "1:00")
        self.assertEqual(items[0].cover_url, "https://cdn/1.jpg")
        self.assertEqual(items[0].cover_status, "url_only")
        self.assertEqual(items[1].cover_status, "not_requested")

    def test_empty_payload(self):
        self.assertEqual(parse_aweme_list({}), [])

    def test_parse_detail_requires_exact_requested_work_id(self):
        payload = {
            "status_code": 0,
            "aweme_detail": {
                "aweme_id": "100",
                "desc": "作品",
                "author": {"nickname": "作者"},
                "video": {
                    "play_addr": {
                        "url_list": [
                            "https://v26-weba.douyinvod.com/video/100.mp4"
                        ]
                    }
                },
            },
        }
        item = parse_aweme_detail(payload, "100")
        self.assertEqual(item.work_id, "100")
        self.assertEqual(item.play_url, payload["aweme_detail"]["video"]["play_addr"]["url_list"][0])

        with self.assertRaises(DouyinCollectError):
            parse_aweme_detail(payload, "101")
        with self.assertRaises(DouyinCollectError):
            parse_aweme_detail({"status_code": 1}, "100")
        with self.assertRaises(DouyinCollectError):
            parse_aweme_detail({"aweme_detail": payload["aweme_detail"]}, "100")


if __name__ == "__main__":
    unittest.main()

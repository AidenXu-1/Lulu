"""Anonymous homepage response boundaries, using no network or user stores."""
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

from src.core.collect import douyin_fetch as fetch


AUTHOR = "MS4wLjABAAAA_fixture-author"
URL = "https://www.douyin.com/user/" + AUTHOR


def payload():
    return {"status_code": 0, "aweme_list": [{
        "aweme_id": "1234567890123456789", "desc": "公开作品",
        "author": {"sec_uid": AUTHOR, "nickname": "目标作者"},
        "video": {"duration": 12000},
    }]}


class AnonymousHomepageResponseTests(unittest.TestCase):
    def setUp(self):
        self.registration = patch.object(fetch, "_register_ttwid", return_value="ttwid=isolated")
        self.registration.start()
        self.addCleanup(self.registration.stop)
        signer = patch.object(fetch, "ABogus")
        self.signer = signer.start()
        self.addCleanup(signer.stop)
        self.signer.return_value.generate_abogus.return_value = ("", "isolated-signature", "isolated-UA", "")

    def returned(self, body):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        return patch.object(fetch.urllib.request, "urlopen", return_value=response)

    def assert_failure(self, body, code):
        with self.returned(body), self.assertRaises(fetch.DouyinCollectError) as caught:
            fetch.fetch_latest(URL)
        self.assertEqual(caught.exception.code, code)

    def test_query_parameters_do_not_change_target_author(self):
        with self.returned(json.dumps(payload()).encode()) as request:
            works = fetch.fetch_latest(URL + "?from_tab_name=main&vid=999", count=20)
        sent = request.call_args.args[0]
        query = parse_qs(urlsplit(sent.full_url).query)
        self.assertEqual(query["sec_user_id"], [AUTHOR])
        self.assertNotIn("vid", query)
        self.assertNotIn("from_tab_name", query)
        self.assertEqual(works[0].author_id, AUTHOR)

    def test_access_denial_rate_limit_and_server_failure_have_typed_causes(self):
        for status, code in ((401, "DOUYIN_LOGIN_REQUIRED"),
                             (403, "DOUYIN_ACCESS_RESTRICTED"),
                             (429, "DOUYIN_RATE_LIMITED"),
                             (503, "DOUYIN_SERVICE_ERROR")):
            with self.subTest(status=status):
                failure = HTTPError("https://www.douyin.com/", status, "failure", {}, io.BytesIO(b"private diagnostics"))
                with patch.object(fetch.urllib.request, "urlopen", side_effect=failure), self.assertRaises(fetch.DouyinCollectError) as caught:
                    fetch.fetch_latest(URL)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("private diagnostics", str(caught.exception))




    def test_empty_response_does_not_claim_profile_is_empty(self):
        self.assert_failure(b"", "DOUYIN_EMPTY_RESPONSE")


    def test_non_success_status_cannot_become_success(self):
        body = payload()
        body["status_code"] = 8
        self.assert_failure(json.dumps(body).encode(), "DOUYIN_SERVICE_ERROR")



    def test_wrong_author_and_missing_work_id_are_rejected(self):
        for field in ("author", "aweme_id"):
            body = payload()
            if field == "author":
                body["aweme_list"][0]["author"]["sec_uid"] = "someone-else"
            else:
                body["aweme_list"][0]["aweme_id"] = ""
            with self.subTest(field=field):
                self.assert_failure(json.dumps(body).encode(), "DOUYIN_PLATFORM_CHANGED")



if __name__ == "__main__":
    unittest.main()

"""小红书 P2C 正式 adapter 的 fixture-first 合同测试。"""

from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.adapters import (  # noqa: E402
    DouyinAdapter,
    XiaohongshuAdapter,
    resolve_adapter,
)
from core.collect.contracts import (  # noqa: E402
    CollectRequest,
    LinkIdentity,
    adapter_capability,
)
from core.collect.adapters.xiaohongshu import _SNAPSHOT_SCRIPT  # noqa: E402
from core.collect.models import TRUTHFUL_NUMERIC_FIELDS  # noqa: E402


PROFILE_URL = "https://www.xiaohongshu.com/user/profile/public_brand"


def request(
    *,
    url: str = PROFILE_URL,
    limit: int | None = None,
    mode: str = "profile_public",
    profile_id: str = "default",
    platform: str = "xiaohongshu",
    kind: str = "homepage",
) -> CollectRequest:
    return CollectRequest(
        LinkIdentity(
            platform=platform,
            kind=kind,
            canonical_url=url,
            extracted_id="public_brand",
            confidence="exact",
        ),
        mode=mode,
        limit=limit,
        profile_id=profile_id,
    )


def row(index: int, **overrides) -> dict[str, object]:
    value: dict[str, object] = {
        "work_id": f"work_{index}",
        "work_url": f"https://www.xiaohongshu.com/explore/work_{index}",
        "author_id": "brand_author",
        "author_name": "公开认证品牌",
        "title": f"公开作品 {index}",
        "create_time": 1_700_000_000 + index,
        "duration_ms": 1000,
        "like": index,
        "collect": index + 1,
        "comment": index + 2,
        "share": index + 3,
        "extra_stats": {"view": index + 4, "unapproved": "discard"},
        "content_kind": "video",
        "cover_url": f"https://sns-webpic.example/covers/{index}.jpg",
    }
    value.update(overrides)
    return value


def snapshot(
    items: list[object] | tuple[object, ...] = (),
    *,
    status: str = "ok",
    has_more: bool = False,
) -> dict[str, object]:
    return {"status": status, "items": list(items), "has_more": has_more}


class FakeSession:
    def __init__(self, snapshots=(), *, fail_get=False, fail_detach=False) -> None:
        self.snapshots = list(snapshots)
        self.fail_get = fail_get
        self.fail_detach = fail_detach
        self.get_urls: list[str] = []
        self.read_count = 0
        self.advance_count = 0
        self.detach_count = 0
        self.close_count = 0

    def get(self, url: str) -> None:
        self.get_urls.append(url)
        if self.fail_get:
            raise RuntimeError("fixture page failure")

    def run_js(self, script: str):
        if "scrollBy" in script:
            self.advance_count += 1
            return None
        self.read_count += 1
        if not self.snapshots:
            raise AssertionError("unexpected fixture read")
        value = self.snapshots.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def detach(self) -> None:
        self.detach_count += 1
        if self.fail_detach:
            raise RuntimeError("fixture detach failure")

    def close(self) -> None:
        self.close_count += 1
        raise AssertionError("adapter 不得关闭常驻浏览器")


class DrissionReturnSemanticsSession(FakeSession):
    """模拟 DrissionPage：脚本没有顶层 return 时，Python 侧只能拿到 None。"""

    def run_js(self, script: str):
        if "scrollBy" in script:
            return super().run_js(script)
        if not script.lstrip().startswith("return "):
            self.read_count += 1
            return None
        return super().run_js(script)


def adapter_for(session: FakeSession, opened: list[str] | None = None) -> XiaohongshuAdapter:
    def open_session(profile_id: str):
        if opened is not None:
            opened.append(profile_id)
        return session

    return XiaohongshuAdapter(open_session_fn=open_session)


class XiaohongshuAdapterTests(unittest.TestCase):
    _RISK_SENTINEL = "QUERY_TOKEN_MUST_NOT_ENTER_MODEL_FIXTURE"

    def _risk_row(self, index: int) -> dict[str, object]:
        sentinel = self._RISK_SENTINEL
        return row(
            index,
            work_url=f"https://www.xiaohongshu.com/explore/work_{index}?query={sentinel}#{sentinel}",
            cover_url=f"https://sns-webpic.example/covers/{index}.jpg?query={sentinel}#{sentinel}",
            author_name=sentinel,
            title=sentinel,
            extra_stats={"view": 1, "nested": {"token": sentinel}},
            nested={"query": sentinel, "fragment": sentinel},
        )

    def _serialized_result(self, result) -> str:
        return json.dumps({
            "items": [item.to_dict() for item in result.items],
            "completion": result.completion,
            "reason_code": result.reason_code,
            "next_action": result.next_action,
            "cursor": dict(result.cursor or {}),
        }, ensure_ascii=False)

    def _assert_first_round_risk_drops_cards(self, status: str, reason_code: str) -> None:
        script_guard = _SNAPSHOT_SCRIPT.index("if (status !== 'ok')")
        script_empty_return = _SNAPSHOT_SCRIPT.index("return {status, items: [], has_more: false};")
        script_card_read = _SNAPSHOT_SCRIPT.index("document.querySelectorAll('a[href]')")
        self.assertLess(script_guard, script_card_read)
        self.assertLess(script_empty_return, script_card_read)

        session = FakeSession([snapshot([self._risk_row(90)], status=status, has_more=True)])
        result = adapter_for(session).collect(request())

        self.assertEqual((result.completion, result.reason_code), ("failed", reason_code))
        self.assertEqual(result.items, ())
        self.assertEqual(session.read_count, 1)
        self.assertEqual(session.advance_count, 0)
        serialized = self._serialized_result(result)
        self.assertNotIn(self._RISK_SENTINEL, serialized)
        self.assertNotIn("?query=", serialized)

    def test_one_page_maps_public_fields_and_strips_url_transients(self):
        public_row = row(
            1,
            work_url="https://www.xiaohongshu.com/explore/work_1?ignored=1#fragment",
            cover_url="https://sns-webpic.example/covers/1.jpg?ignored=1#fragment",
        )
        session = FakeSession([snapshot([public_row])])
        result = adapter_for(session).collect(
            request(url=PROFILE_URL + "?ignored=1#fragment")
        )

        self.assertEqual(result.completion, "complete")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.stable_key, "xiaohongshu:work_1")
        self.assertEqual(item.work_url, "https://www.xiaohongshu.com/explore/work_1")
        self.assertEqual(item.homepage_url, PROFILE_URL)
        self.assertEqual(item.cover_url, "https://sns-webpic.example/covers/1.jpg")
        self.assertEqual(item.cover_status, "url_only")
        self.assertEqual(item.source_method, "dedicated_profile")
        self.assertEqual(item.extra_stats["view"], 5)
        for name in TRUTHFUL_NUMERIC_FIELDS:
            self.assertEqual(item.field_state(name), "known")
        serialized = json.dumps(item.to_dict(), ensure_ascii=False)
        self.assertNotIn("?", serialized)
        self.assertNotIn("#fragment", serialized)
        self.assertNotIn("unapproved", serialized)
        self.assertEqual(session.get_urls, [PROFILE_URL])
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_navigation_progress_is_emitted_before_the_single_bounded_get(self):
        events = []

        class OrderingSession(FakeSession):
            def get(self, url):
                events.append(("get", len(self.get_urls)))
                return super().get(url)

        session = OrderingSession([snapshot([row(1)])])
        result = adapter_for(session).collect(
            request(),
            progress=lambda label, count: events.append((label, count)),
        )

        self.assertEqual(result.completion, "complete")
        self.assertEqual(events[0], ("正在打开小红书公开主页", 0))
        self.assertEqual(events[1], ("get", 0))
        self.assertEqual(len(session.get_urls), 1)

    def test_cancel_during_bounded_navigation_finishes_within_three_seconds(self):
        navigation_started = threading.Event()
        cancelled = threading.Event()
        outcome = []

        class BoundedBlockingSession(FakeSession):
            def get(self, url):
                super().get(url)
                navigation_started.set()
                time.sleep(2.0)

        session = BoundedBlockingSession([snapshot([row(1)])])
        worker = threading.Thread(
            target=lambda: outcome.append(
                adapter_for(session).collect(
                    request(),
                    is_cancelled=cancelled.is_set,
                )
            ),
            daemon=True,
        )
        started_at = time.monotonic()
        worker.start()
        self.assertTrue(navigation_started.wait(0.5))
        cancelled.set()
        worker.join(2.9)
        elapsed = time.monotonic() - started_at

        self.assertFalse(worker.is_alive())
        self.assertLessEqual(elapsed, 3.0)
        self.assertEqual((outcome[0].completion, outcome[0].reason_code), ("cancelled", "cancelled"))
        self.assertEqual(session.read_count, 0)
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_drission_top_level_return_yields_sanitized_real_card_shape(self):
        real_shape_fixture = {
            "work_id": "6abcdef00000000000000001",
            "work_url": "https://www.xiaohongshu.com/explore/6abcdef00000000000000001",
            "author_name": "公开认证机构",
            "title": "公开卡片文字",
            "cover_url": "https://sns-webpic.example/public/cover.webp",
            "content_kind": "unknown",
        }
        session = DrissionReturnSemanticsSession([snapshot([real_shape_fixture])])

        result = adapter_for(session).collect(request())

        self.assertTrue(_SNAPSHOT_SCRIPT.lstrip().startswith("return (() =>"))
        self.assertNotIn("小米", _SNAPSHOT_SCRIPT)
        self.assertEqual(result.completion, "complete")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.work_id, "6abcdef00000000000000001")
        self.assertEqual(item.author_name, "公开认证机构")
        self.assertEqual(item.title, "公开卡片文字")
        self.assertEqual(item.content_kind, "unknown")
        for name in TRUTHFUL_NUMERIC_FIELDS:
            self.assertEqual(item.field_state(name), "unknown")
        serialized = self._serialized_result(result)
        for forbidden in ("?", "#", "token", "fragment"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_two_rounds_stop_at_twenty_and_report_progress(self):
        session = FakeSession([
            snapshot([row(index) for index in range(12)], has_more=True),
            snapshot([row(index) for index in range(10, 24)], has_more=True),
        ])
        progress = []
        result = adapter_for(session).collect(
            request(), progress=lambda label, count: progress.append((label, count))
        )

        self.assertEqual(result.completion, "complete")
        self.assertEqual([item.work_id for item in result.items], [f"work_{i}" for i in range(20)])
        self.assertEqual(session.read_count, 2)
        self.assertEqual(session.advance_count, 1)
        self.assertEqual([count for _label, count in progress], [0, 12, 20])

    def test_request_limit_below_and_above_hard_limit(self):
        for requested, expected in ((3, 3), (999, 20)):
            with self.subTest(requested=requested):
                session = FakeSession([snapshot([row(index) for index in range(25)], has_more=True)])
                result = adapter_for(session).collect(request(limit=requested))
                self.assertEqual(len(result.items), expected)
                self.assertEqual(result.completion, "complete")
                self.assertEqual(session.read_count, 1)

    def test_second_round_all_duplicates_keeps_first_order(self):
        first = [row(2), row(1), row(3)]
        session = FakeSession([
            snapshot(first, has_more=True),
            snapshot([row(3), row(2), row(1), row(1)], has_more=False),
        ])
        result = adapter_for(session).collect(request())
        self.assertEqual(result.completion, "complete")
        self.assertEqual([item.work_id for item in result.items], ["work_2", "work_1", "work_3"])
        self.assertEqual(session.read_count, 2)

    def test_same_page_duplicates_are_removed_by_work_id(self):
        duplicate_url = row(1, work_url="https://www.xiaohongshu.com/discovery/item/work_1")
        session = FakeSession([snapshot([row(1), duplicate_url, row(2)])])
        result = adapter_for(session).collect(request())
        self.assertEqual([item.work_id for item in result.items], ["work_1", "work_2"])

    def test_missing_work_id_falls_back_to_normalized_public_url(self):
        session = FakeSession([snapshot([
            row(1, work_id="", work_url="https://www.xiaohongshu.com/user/profile/author/work_1?ignored=1"),
        ])])
        result = adapter_for(session).collect(request())
        self.assertEqual(result.completion, "complete")
        self.assertEqual(result.items[0].work_id, "work_1")
        self.assertEqual(result.items[0].work_url, "https://www.xiaohongshu.com/explore/work_1")

    def test_missing_optional_fields_are_honestly_empty(self):
        minimal = {"work_id": "minimal", "work_url": "https://www.xiaohongshu.com/explore/minimal"}
        session = FakeSession([snapshot([minimal])])
        item = adapter_for(session).collect(request()).items[0]
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.title, "")
        self.assertEqual(item.create_time, 0)
        self.assertEqual(item.duration_ms, 0)
        self.assertEqual(
            {name: item.field_state(name) for name in TRUTHFUL_NUMERIC_FIELDS},
            {name: "unknown" for name in TRUTHFUL_NUMERIC_FIELDS},
        )
        self.assertEqual(item.content_kind, "unknown")
        self.assertEqual(item.cover_url, "")
        self.assertEqual(item.cover_status, "not_requested")

    def test_explicit_nonnegative_integer_zero_is_known(self):
        zero = row(
            1,
            create_time=0,
            duration_ms=0,
            like=0,
            collect=0,
            comment=0,
            share=0,
        )
        item = adapter_for(FakeSession([snapshot([zero])])).collect(request()).items[0]

        for name in TRUTHFUL_NUMERIC_FIELDS:
            self.assertEqual(item.field_state(name), "known")
            self.assertEqual(item.known_numeric(name), 0)

    def test_image_duration_is_not_applicable(self):
        image = row(1, content_kind="image", duration_ms=9999)
        item = adapter_for(FakeSession([snapshot([image])])).collect(request()).items[0]

        self.assertEqual(item.duration_ms, 0)
        self.assertEqual(item.field_state("duration_ms"), "not_applicable")
        self.assertIsNone(item.known_numeric("duration_ms"))

    def test_illegal_bool_negative_and_untyped_values_stay_unknown(self):
        invalid = row(
            1,
            create_time=True,
            duration_ms=-1,
            like="12",
            collect=1.0,
            comment=None,
            share=-5,
            extra_stats={"_field_state_like": "known", "view": 7},
        )
        item = adapter_for(FakeSession([snapshot([invalid])])).collect(request()).items[0]

        self.assertEqual(item.extra_stats["view"], 7)
        for name in TRUTHFUL_NUMERIC_FIELDS:
            self.assertEqual(item.field_state(name), "unknown")
            self.assertIsNone(item.known_numeric(name))
            self.assertEqual(getattr(item, name), 0)

    def test_invalid_cover_scheme_and_unknown_content_kind_are_dropped(self):
        session = FakeSession([snapshot([
            row(1, cover_url="http://unsafe.example/cover.jpg?ignored=1", content_kind="future"),
        ])])
        item = adapter_for(session).collect(request()).items[0]
        self.assertEqual(item.cover_url, "")
        self.assertEqual(item.cover_status, "not_requested")
        self.assertEqual(item.content_kind, "unknown")

    def test_identity_mismatch_is_platform_changed_not_empty_success(self):
        session = FakeSession([snapshot([
            row(1, work_url="https://www.xiaohongshu.com/explore/different"),
        ])])
        result = adapter_for(session).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("failed", "platform_changed"))

    def test_non_mapping_snapshot_or_items_is_platform_changed(self):
        cases = [None, [], {"status": "ok", "items": "not-a-list", "has_more": False}]
        for malformed in cases:
            with self.subTest(malformed=malformed):
                result = adapter_for(FakeSession([malformed])).collect(request())
                self.assertEqual((result.completion, result.reason_code), ("failed", "platform_changed"))

    def test_rows_present_but_none_are_valid_is_platform_changed(self):
        session = FakeSession([snapshot([{"work_id": "bad id", "work_url": "javascript:void(0)"}])])
        result = adapter_for(session).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("failed", "platform_changed"))

    def test_authoritative_empty_is_the_only_empty_complete(self):
        confirmed = adapter_for(FakeSession([snapshot(status="confirmed_empty")])).collect(request())
        unknown = adapter_for(FakeSession([snapshot()])).collect(request())
        self.assertEqual(confirmed.completion, "complete")
        self.assertTrue(confirmed.confirmed_empty)
        self.assertEqual(confirmed.items, ())
        self.assertEqual((unknown.completion, unknown.reason_code), ("failed", "no_items_unverified"))

    def test_confirmed_empty_with_items_fails_as_structure_change(self):
        result = adapter_for(FakeSession([snapshot([row(1)], status="confirmed_empty")])).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("partial", "platform_changed"))
        self.assertEqual([item.work_id for item in result.items], ["work_1"])

    def test_login_captcha_rate_limit_and_unknown_status_fail_closed(self):
        cases = {
            "login_required": "login_required",
            "login_expired": "login_expired",
            "captcha": "captcha",
            "verification_required": "captcha",
            "rate_limited": "rate_limited",
            "future_status": "platform_changed",
        }
        for status, reason in cases.items():
            with self.subTest(status=status):
                result = adapter_for(FakeSession([snapshot(status=status)])).collect(request())
                self.assertEqual((result.completion, result.reason_code), ("failed", reason))
                self.assertTrue(result.next_action)

    def test_login_expired_snapshot_with_cards_drops_risk_page_values(self):
        self._assert_first_round_risk_drops_cards("login_expired", "login_expired")

    def test_captcha_snapshot_with_cards_drops_risk_page_values(self):
        self._assert_first_round_risk_drops_cards("captcha", "captcha")

    def test_verification_required_snapshot_with_cards_drops_risk_page_values(self):
        self._assert_first_round_risk_drops_cards("verification_required", "captcha")

    def test_rate_limited_snapshot_with_cards_drops_risk_page_values(self):
        self._assert_first_round_risk_drops_cards("rate_limited", "rate_limited")

    def test_second_round_risk_drops_new_cards_and_keeps_only_prior_safe_items(self):
        session = FakeSession([
            snapshot([row(1)], has_more=True),
            snapshot([self._risk_row(91)], status="captcha", has_more=True),
        ])
        result = adapter_for(session).collect(request())

        self.assertEqual((result.completion, result.reason_code), ("partial", "captcha"))
        self.assertEqual([item.work_id for item in result.items], ["work_1"])
        self.assertEqual(dict(result.cursor or {}), {"round": 2, "item_count": 1})
        self.assertEqual(session.read_count, 2)
        self.assertEqual(session.advance_count, 1)
        serialized = self._serialized_result(result)
        self.assertNotIn(self._RISK_SENTINEL, serialized)
        self.assertNotIn("work_91", serialized)
        self.assertNotIn("?query=", serialized)

    def test_risk_on_second_round_preserves_partial_results_and_cursor(self):
        for status, reason in (("login_expired", "login_expired"), ("captcha", "captcha"),
                               ("rate_limited", "rate_limited")):
            with self.subTest(status=status):
                session = FakeSession([
                    snapshot([row(1), row(2)], has_more=True),
                    snapshot([], status=status),
                ])
                result = adapter_for(session).collect(request())
                self.assertEqual((result.completion, result.reason_code), ("partial", reason))
                self.assertEqual([item.work_id for item in result.items], ["work_1", "work_2"])
                self.assertEqual(dict(result.cursor or {}), {"round": 2, "item_count": 2})

    def test_cancel_before_open_has_no_session_effect(self):
        opened = []
        session = FakeSession([snapshot([row(1)])])
        result = adapter_for(session, opened).collect(request(), is_cancelled=lambda: True)
        self.assertEqual((result.completion, result.reason_code), ("cancelled", "cancelled"))
        self.assertEqual(opened, [])
        self.assertEqual(session.get_urls, [])

    def test_cancel_after_first_round_preserves_items_and_does_not_scroll(self):
        checks = iter((False, False, True))
        session = FakeSession([snapshot([row(1), row(2)], has_more=True)])
        result = adapter_for(session).collect(request(), is_cancelled=lambda: next(checks))
        self.assertEqual((result.completion, result.reason_code), ("cancelled", "cancelled"))
        self.assertEqual([item.work_id for item in result.items], ["work_1", "work_2"])
        self.assertEqual(session.advance_count, 0)
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_page_get_failure_is_failed_and_session_closes(self):
        session = FakeSession(fail_get=True)
        result = adapter_for(session).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("failed", "network_error"))
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_second_page_exception_preserves_partial_results(self):
        session = FakeSession([
            snapshot([row(1)], has_more=True),
            RuntimeError("fixture read failure"),
        ])
        result = adapter_for(session).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("partial", "network_error"))
        self.assertEqual([item.work_id for item in result.items], ["work_1"])
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_detach_failure_changes_complete_to_partial_without_losing_items(self):
        session = FakeSession([snapshot([row(1)])], fail_detach=True)
        result = adapter_for(session).collect(request())
        self.assertEqual((result.completion, result.reason_code), ("partial", "network_error"))
        self.assertEqual([item.work_id for item in result.items], ["work_1"])
        self.assertEqual(session.detach_count, 1)
        self.assertEqual(session.close_count, 0)

    def test_legacy_close_only_session_remains_owned_by_application(self):
        class LegacyCloseOnlySession:
            def __init__(self):
                self.close_count = 0

            def get(self, _url):
                return None

            def run_js(self, script):
                if "scrollBy" in script:
                    return None
                return snapshot([row(1)])

            def close(self):
                self.close_count += 1

        session = LegacyCloseOnlySession()
        result = XiaohongshuAdapter(
            open_session_fn=lambda _profile: session,
        ).collect(request())

        self.assertEqual(result.completion, "complete")
        self.assertEqual(session.close_count, 0)

    def test_two_round_bound_returns_bounded_success_without_third_read(self):
        session = FakeSession([
            snapshot([row(1)], has_more=True),
            snapshot([row(2)], has_more=True),
            snapshot([row(3)], has_more=False),
        ])
        result = adapter_for(session).collect(request())
        self.assertEqual(result.completion, "complete")
        self.assertEqual([item.work_id for item in result.items], ["work_1", "work_2"])
        self.assertEqual(session.read_count, 2)
        self.assertEqual(session.advance_count, 1)

    def test_invalid_request_fails_before_session_open(self):
        cases = [
            request(mode="unknown"),
            request(profile_id="other"),
            request(kind="work"),
            request(url="https://example.com/user/profile/public_brand"),
            request(url="https://www.xiaohongshu.com/explore/work_1"),
            request(platform="douyin"),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                opened = []
                result = adapter_for(FakeSession(), opened).collect(invalid)
                self.assertEqual(result.completion, "failed")
                self.assertIn(result.reason_code, {"invalid_input", "unsupported_link"})
                self.assertEqual(opened, [])

    def test_default_adapter_does_not_import_or_start_browser(self):
        result = XiaohongshuAdapter().collect(request())
        self.assertEqual((result.completion, result.reason_code), ("failed", "login_required"))

    def test_capability_and_registry_are_available_after_formal_gate(self):
        capability = adapter_capability("xiaohongshu")
        self.assertTrue(capability.available)
        self.assertEqual(capability.reason, "formal_adapter_ready")
        self.assertEqual(capability.modes, ("profile_public",))
        self.assertIsInstance(resolve_adapter("xiaohongshu"), XiaohongshuAdapter)
        self.assertIsInstance(resolve_adapter("douyin"), DouyinAdapter)


if __name__ == "__main__":
    unittest.main()

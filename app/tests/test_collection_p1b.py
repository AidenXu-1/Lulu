"""P1B 抖音 adapter 与 profile registry 的隔离安全测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect.adapters import (  # noqa: E402
    AdapterUnavailableError,
    DouyinAdapter,
    XiaohongshuAdapter,
    resolve_adapter,
)
from core.collect.contracts import (  # noqa: E402
    CollectRequest,
    LinkIdentity,
    adapter_capability,
)
from core.collect.models import CollectedItem  # noqa: E402
from core.collect.profiles import ProfileError, ProfileRegistry  # noqa: E402


def _request(mode: str, *, limit: int | None = None, profile_id: str = "default") -> CollectRequest:
    return CollectRequest(
        LinkIdentity(
            platform="douyin",
            kind="homepage",
            canonical_url="https://www.douyin.com/user/MS4w.test",
            extracted_id="MS4w.test",
            confidence="exact",
        ),
        mode=mode,
        limit=limit,
        profile_id=profile_id,
    )


class TestDouyinAdapter(unittest.TestCase):
    def test_anonymous_wraps_old_parameters_and_identity(self):
        calls = []

        def fake_fetch(url, count):
            calls.append((url, count))
            return [CollectedItem("1"), CollectedItem("2")]

        result = DouyinAdapter(fetch_latest_fn=fake_fetch).collect(
            _request("anonymous_latest", limit=1)
        )
        self.assertEqual(calls, [("https://www.douyin.com/user/MS4w.test", 1)])
        self.assertEqual(result.completion, "complete")
        self.assertEqual([item.aweme_id for item in result.items], ["1"])
        self.assertEqual(result.items[0].stable_key, "douyin:1")

    def test_anonymous_empty_is_not_fake_complete(self):
        result = DouyinAdapter(fetch_latest_fn=lambda *_: []).collect(
            _request("anonymous_latest")
        )
        self.assertEqual((result.completion, result.reason_code),
                         ("failed", "no_items_unverified"))

    def test_old_exception_copy_is_preserved_with_stable_reason(self):
        message = "抖音返回了无法解析的数据,可能是平台改版,请反馈给开发者。"

        def fail(*_args):
            raise RuntimeError(message)

        result = DouyinAdapter(fetch_latest_fn=fail).collect(_request("anonymous_latest"))
        self.assertEqual((result.completion, result.reason_code),
                         ("failed", "platform_changed"))
        self.assertEqual(result.next_action, message)

    def test_profile_chain_keeps_old_arguments_and_complete(self):
        calls = []
        progress = lambda *_: None
        cancelled = lambda: False

        def fake_all(url, progress_cb, cancel_cb, browser_path, browser_mode):
            calls.append((url, progress_cb, cancel_cb, browser_path, browser_mode))
            return [CollectedItem("1"), CollectedItem("2")], ""

        result = DouyinAdapter(collect_all_fn=fake_all).collect(
            _request("profile_all"),
            progress=progress,
            is_cancelled=cancelled,
            browser_path="/fake/browser",
            browser_mode="low_risk",
        )
        self.assertEqual(calls[0][0], "https://www.douyin.com/user/MS4w.test")
        self.assertIs(calls[0][1], progress)
        self.assertIs(calls[0][2], cancelled)
        self.assertEqual(calls[0][3:], ("/fake/browser", "low_risk"))
        self.assertEqual(result.completion, "complete")

    def test_profile_partial_keeps_items_and_original_reason(self):
        reason = "⚠️ 撞到验证码，已停止；已采集 1 条已保留。"
        adapter = DouyinAdapter(
            collect_all_fn=lambda *_: ([CollectedItem("1")], reason)
        )
        result = adapter.collect(_request("profile_all"))
        self.assertEqual((result.completion, result.reason_code), ("partial", "captcha"))
        self.assertEqual([item.aweme_id for item in result.items], ["1"])
        self.assertEqual(result.next_action, reason)

    def test_profile_cancelled_keeps_partial_items(self):
        adapter = DouyinAdapter(
            collect_all_fn=lambda *_: ([CollectedItem("1")], "已取消（已采集的已保留）。")
        )
        result = adapter.collect(_request("profile_all"))
        self.assertEqual((result.completion, result.reason_code), ("cancelled", "cancelled"))
        self.assertEqual(len(result.items), 1)

    def test_cancel_before_call_does_not_touch_chain(self):
        touched = []
        adapter = DouyinAdapter(fetch_latest_fn=lambda *_: touched.append(True))
        result = adapter.collect(_request("anonymous_latest"), is_cancelled=lambda: True)
        self.assertEqual(result.completion, "cancelled")
        self.assertEqual(touched, [])

    def test_nondefault_profile_fails_before_browser_chain(self):
        touched = []
        adapter = DouyinAdapter(collect_all_fn=lambda *_: touched.append(True))
        result = adapter.collect(_request("profile_all", profile_id="other"))
        self.assertEqual((result.completion, result.reason_code), ("failed", "invalid_input"))
        self.assertEqual(touched, [])

    def test_adapter_registry_matches_p1a_capabilities(self):
        self.assertIsInstance(resolve_adapter("douyin"), DouyinAdapter)
        self.assertIsInstance(resolve_adapter("xiaohongshu"), XiaohongshuAdapter)
        for platform in ("bilibili", "unknown"):
            with self.assertRaises(AdapterUnavailableError) as caught:
                resolve_adapter(platform)
            self.assertEqual(caught.exception.reason, adapter_capability(platform).reason)
            self.assertFalse(adapter_capability(platform).available)


class TestProfileRegistry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app_data = Path(self.temp.name) / "app-data"
        self.registry = ProfileRegistry(self.app_data)

    def test_legacy_alias_when_only_old_douyin_exists(self):
        legacy = self.app_data / "douyin_profile"
        legacy.mkdir(parents=True)
        result = self.registry.resolve("douyin")
        self.assertEqual((result.state, result.path), ("legacy_alias", legacy))

    def test_new_managed_profile_and_lazy_creation(self):
        missing = self.registry.resolve("douyin")
        self.assertEqual(missing.state, "missing")
        self.assertFalse(missing.path.exists())
        created = self.registry.resolve("douyin", create=True)
        self.assertEqual(created.state, "managed")
        self.assertTrue(created.path.is_dir())

    def test_old_and_new_douyin_conflict_preserves_both(self):
        legacy = self.app_data / "douyin_profile"
        current = self.app_data / "collection_profiles" / "douyin" / "default"
        legacy.mkdir(parents=True)
        current.mkdir(parents=True)
        with self.assertRaises(ProfileError) as caught:
            self.registry.resolve("douyin")
        self.assertEqual(caught.exception.code, "profile_conflict")
        self.assertTrue(legacy.exists())
        self.assertTrue(current.exists())

    def test_platform_directories_are_physically_isolated(self):
        douyin = self.registry.resolve("douyin", create=True)
        xhs = self.registry.resolve("xiaohongshu", create=True)
        self.assertNotEqual(douyin.path, xhs.path)
        self.assertNotEqual(douyin.path.parent, xhs.path.parent)
        self.assertEqual(douyin.path.name, xhs.path.name)

    def test_invalid_platform_profile_and_bilibili_creation_rejected(self):
        for platform, profile_id in (("unknown", "default"), ("douyin", "../x"),
                                     ("xiaohongshu", "Upper")):
            with self.assertRaises(ProfileError):
                self.registry.resolve(platform, profile_id)
        with self.assertRaises(ProfileError) as caught:
            self.registry.resolve("bilibili", create=True)
        self.assertEqual(caught.exception.code, "profile_unavailable")

    def test_filesystem_root_is_rejected(self):
        with self.assertRaises(ProfileError) as caught:
            ProfileRegistry(Path(Path.cwd().anchor))
        self.assertEqual(caught.exception.code, "unsafe_app_data")

    def test_symlink_profile_is_rejected_without_touching_target(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "cookie").write_text("keep", encoding="utf-8")
        link = self.app_data / "collection_profiles" / "douyin" / "default"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"当前文件系统不支持符号链接: {exc}")
        with self.assertRaises(ProfileError) as caught:
            self.registry.resolve("douyin")
        self.assertEqual(caught.exception.code, "symlink_rejected")
        self.assertTrue((outside / "cookie").exists())

    def test_exclusive_lease_rejects_second_owner_then_releases(self):
        self.registry.resolve("douyin", create=True)
        first = self.registry.acquire("douyin")
        self.addCleanup(first.release)
        with self.assertRaises(ProfileError) as caught:
            self.registry.acquire("douyin")
        self.assertEqual(caught.exception.code, "profile_busy")
        first.release()
        second = self.registry.acquire("douyin")
        second.release()

    def test_douyin_lease_payload_and_legacy_path_are_unchanged_by_xhs_r0(self):
        legacy = self.app_data / "douyin_profile"
        legacy.mkdir(parents=True)
        resolution = self.registry.resolve("douyin")
        lease = self.registry.acquire("douyin")
        payload = json.loads(lease.lock_path.read_text(encoding="utf-8"))
        lease.release()

        self.assertEqual((resolution.state, resolution.path), ("legacy_alias", legacy))
        self.assertEqual(set(payload), {"pid", "started_at", "token"})
        self.assertEqual(payload["pid"], os.getpid())
        self.assertNotIn("run_id", payload)
        self.assertNotIn("owner", payload)

    def test_clear_uses_fake_browser_and_leaves_other_platform_untouched(self):
        douyin = self.registry.resolve("douyin", create=True)
        xhs = self.registry.resolve("xiaohongshu", create=True)
        (douyin.path / "Cookies").write_text("fake", encoding="utf-8")
        (xhs.path / "Cookies").write_text("keep", encoding="utf-8")
        closed = []

        result = self.registry.clear(
            "douyin", close_browser=lambda resolution: closed.append(resolution.path) or True
        )
        self.assertTrue(result.success)
        self.assertEqual(result.code, "cleared")
        self.assertEqual(closed, [douyin.path])
        self.assertFalse(douyin.path.exists())
        self.assertEqual((xhs.path / "Cookies").read_text(encoding="utf-8"), "keep")

    def test_clear_fails_closed_when_browser_cannot_close(self):
        target = self.registry.resolve("douyin", create=True).path
        (target / "Cookies").write_text("keep", encoding="utf-8")
        with self.assertRaises(ProfileError) as caught:
            self.registry.clear("douyin", close_browser=lambda _resolution: False)
        self.assertEqual(caught.exception.code, "browser_busy")
        self.assertTrue((target / "Cookies").exists())

    def test_clear_fails_when_profile_is_occupied(self):
        target = self.registry.resolve("douyin", create=True).path
        lease = self.registry.acquire("douyin")
        self.addCleanup(lease.release)
        with self.assertRaises(ProfileError) as caught:
            self.registry.clear("douyin", close_browser=lambda _resolution: True)
        self.assertEqual(caught.exception.code, "profile_busy")
        self.assertTrue(target.exists())

    def test_partial_remove_failure_is_not_reported_as_success(self):
        target = self.registry.resolve("douyin", create=True).path
        (target / "Cookies").write_text("fake", encoding="utf-8")

        def partial_failure(tombstone: Path) -> None:
            (tombstone / "Cookies").unlink()
            raise PermissionError("fixture failure")

        result = self.registry.clear(
            "douyin", close_browser=lambda _resolution: True, remove_tree=partial_failure
        )
        self.assertFalse(result.success)
        self.assertEqual(result.code, "partial_clear_failed")
        self.assertFalse(target.exists())
        tombstone = self.registry.managed_root / ".trash" / result.tombstone_name
        self.assertTrue(tombstone.exists())


if __name__ == "__main__":
    unittest.main()

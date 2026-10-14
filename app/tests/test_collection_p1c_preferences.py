"""P1C collect_cover 配置与纯 Python 偏好服务测试。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config as config_module  # noqa: E402
from src.config import AppConfig, COLLECT_COVER_CONTRACT_VERSION  # noqa: E402
from src.core.collect.preferences import CollectCoverPreferenceService  # noqa: E402


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config" / "config.json"
        self.path_patch = patch.object(config_module, "_CONFIG_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)


class TestCollectCoverConfig(ConfigCase):
    def test_old_true_without_current_marker_migrates_effectively_closed(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps({"theme": "dark", "collect_cover": True}),
            encoding="utf-8",
        )
        config = AppConfig.load()
        self.assertFalse(config.collect_cover)
        self.assertEqual(config.collect_cover_contract_version, 0)
        service = CollectCoverPreferenceService(config)
        receipt = service.set(True)
        self.assertTrue(receipt.success)
        loaded = AppConfig.load()
        self.assertTrue(loaded.collect_cover)
        self.assertEqual(loaded.theme, "warm")
        self.assertEqual(
            loaded.collect_cover_contract_version,
            COLLECT_COVER_CONTRACT_VERSION,
        )

    def test_marker_alone_never_enables_cover_download(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "collect_cover": False,
                    "collect_cover_contract_version": COLLECT_COVER_CONTRACT_VERSION,
                }
            ),
            encoding="utf-8",
        )
        config = AppConfig.load()
        self.assertFalse(config.collect_cover)
        self.assertFalse(CollectCoverPreferenceService(config).value())

    def test_non_bool_json_values_fall_back_false(self):
        for index, value in enumerate(("false", 1, 0, None, [], {})):
            with self.subTest(index=index):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"collect_cover": value}), encoding="utf-8")
                self.assertFalse(AppConfig.load().collect_cover)

    def test_existing_fields_round_trip_unchanged(self):
        config = AppConfig(
            theme="warm",
            engine_name="sensevoice",
            keep_media=True,
            media_dir="/private/tmp/media",
            copywriting_prompts={"analyze": {"mode": "custom"}},
            collect_cover=True,
            collect_cover_contract_version=COLLECT_COVER_CONTRACT_VERSION,
        )
        self.assertTrue(config.save())
        loaded = AppConfig.load()
        self.assertEqual(loaded.theme, "warm")
        self.assertEqual(loaded.engine_name, "sensevoice")
        self.assertTrue(loaded.keep_media)
        self.assertEqual(loaded.media_dir, "/private/tmp/media")
        self.assertEqual(loaded.copywriting_prompts, config.copywriting_prompts)
        self.assertTrue(loaded.collect_cover)

    def _assert_atomic_failure(self, target: str, side_effect: BaseException):
        original_config = AppConfig(engine_name="sensevoice", collect_cover=False)
        self.assertTrue(original_config.save())
        original = self.path.read_bytes()
        changed = AppConfig(engine_name="local_qwen3", collect_cover=True)
        with patch.object(config_module, target, side_effect=side_effect):
            self.assertFalse(changed.save())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(changed.engine_name, "sensevoice")
        self.assertFalse(changed.collect_cover)

    def test_temp_write_failure_preserves_disk_and_rolls_back_memory(self):
        self._assert_atomic_failure("_write_config_temp", OSError("write"))

    def test_fsync_failure_preserves_disk_and_rolls_back_memory(self):
        self._assert_atomic_failure("_fsync_config_file", OSError("fsync"))

    def test_replace_failure_preserves_disk_and_rolls_back_memory(self):
        self._assert_atomic_failure("_replace_config", OSError("replace"))

    def test_post_replace_validation_failure_restores_old_bytes(self):
        old = AppConfig(engine_name="sensevoice", collect_cover=False)
        self.assertTrue(old.save())
        original = self.path.read_bytes()
        changed = AppConfig(engine_name="local_qwen3", collect_cover=True)
        real_validate = config_module._validate_config_payload
        calls = 0

        def fail_second(payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("post validation")
            return real_validate(payload)

        with patch.object(config_module, "_validate_config_payload", side_effect=fail_second):
            self.assertFalse(changed.save())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(changed.engine_name, "sensevoice")
        self.assertFalse(changed.collect_cover)


class FakeConfig:
    def __init__(
        self,
        value: bool = False,
        *,
        marker: int = COLLECT_COVER_CONTRACT_VERSION,
        outcome=True,
    ) -> None:
        self.collect_cover = value
        self.collect_cover_contract_version = marker
        self.outcome = outcome
        self.save_calls = 0

    def save(self) -> bool:
        self.save_calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return bool(self.outcome)


class TestCollectCoverPreferenceService(unittest.TestCase):
    def test_success_broadcasts_after_save_to_all_subscribers(self):
        config = FakeConfig()
        service = CollectCoverPreferenceService(config)
        events = []
        service.subscribe(lambda value: events.append(("first", value, config.save_calls)))
        service.subscribe(lambda value: events.append(("second", value, config.save_calls)))
        receipt = service.set(True)
        self.assertTrue(receipt.success)
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.code, "saved")
        self.assertEqual(events, [("first", True, 1), ("second", True, 1)])
        self.assertTrue(service.value())

    def test_false_or_exception_rolls_back_and_never_broadcasts(self):
        for outcome in (False, RuntimeError("save")):
            with self.subTest(outcome=outcome):
                config = FakeConfig(False, outcome=outcome)
                service = CollectCoverPreferenceService(config)
                events = []
                service.subscribe(events.append)
                receipt = service.set(True)
                self.assertFalse(receipt.success)
                self.assertEqual(receipt.code, "config_save_failed")
                self.assertFalse(config.collect_cover)
                self.assertEqual(
                    config.collect_cover_contract_version,
                    COLLECT_COVER_CONTRACT_VERSION,
                )
                self.assertEqual(events, [])

    def test_stale_marker_is_effectively_closed_and_save_updates_both_fields(self):
        config = FakeConfig(True, marker=0)
        service = CollectCoverPreferenceService(config)
        self.assertFalse(service.value())
        self.assertFalse(service.snapshot().include_cover)
        events = []
        service.subscribe(events.append)
        receipt = service.set(True)
        self.assertTrue(receipt.success)
        self.assertTrue(receipt.changed)
        self.assertEqual(config.collect_cover_contract_version, 1)
        self.assertTrue(service.value())
        self.assertEqual(events, [True])

    def test_stale_marker_save_failure_restores_raw_value_and_marker(self):
        config = FakeConfig(True, marker=0, outcome=False)
        service = CollectCoverPreferenceService(config)
        receipt = service.set(True)
        self.assertFalse(receipt.success)
        self.assertTrue(config.collect_cover)
        self.assertEqual(config.collect_cover_contract_version, 0)
        self.assertFalse(service.value())

    def test_same_value_is_noop(self):
        config = FakeConfig(True)
        service = CollectCoverPreferenceService(config)
        events = []
        service.subscribe(events.append)
        receipt = service.set(True)
        self.assertTrue(receipt.success)
        self.assertFalse(receipt.changed)
        self.assertEqual(receipt.code, "unchanged")
        self.assertEqual(config.save_calls, 0)
        self.assertEqual(events, [])

    def test_unsubscribe_is_idempotent(self):
        config = FakeConfig()
        service = CollectCoverPreferenceService(config)
        events = []
        unsubscribe = service.subscribe(events.append)
        unsubscribe()
        unsubscribe()
        service.set(True)
        self.assertEqual(events, [])

    def test_bad_subscriber_is_isolated_and_counted(self):
        config = FakeConfig()
        service = CollectCoverPreferenceService(config)
        events = []

        def broken(_value):
            raise RuntimeError("subscriber")

        service.subscribe(broken)
        service.subscribe(events.append)
        receipt = service.set(True)
        self.assertTrue(receipt.success)
        self.assertEqual(receipt.notification_failures, 1)
        self.assertEqual(events, [True])
        self.assertTrue(config.collect_cover)

    def test_non_bool_values_are_rejected_without_save(self):
        config = FakeConfig()
        service = CollectCoverPreferenceService(config)
        for value in (1, 0, "true", None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    service.set(value)  # type: ignore[arg-type]
        self.assertEqual(config.save_calls, 0)


if __name__ == "__main__":
    unittest.main()

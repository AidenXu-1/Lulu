import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.core.engines import providers
from src.ui.model_context import (
    NO_MODEL_CARD_COPY,
    api_model_context,
    asr_model_context,
    local_model_context,
    neutral_model_context,
)


class _RealSecretAccessForbidden(BaseException):
    """Escape product ``except Exception`` fallbacks before real secret access."""


def _forbid_real_secret_access(*_args, **_kwargs):
    raise _RealSecretAccessForbidden(
        "test_model_context must inject deterministic secret/model state"
    )


class ModelContextTests(unittest.TestCase):
    def setUp(self):
        loaded = patch("src.ui.model_context.secrets.configuration_loaded", return_value=True)
        loaded.start()
        self.addCleanup(loaded.stop)
        # This module is also run directly, outside discovery. Install a local
        # fail-fast boundary so an omitted Fake/Mock cannot silently fall back
        # to the user's real keyring. Inner per-test patches may replace these
        # targets with deterministic values; the outer mocks must remain at 0.
        targets = (
            ("secrets.get_config", "src.core.secrets.get_config"),
            ("secrets._backend", "src.core.secrets._backend"),
            ("keyring.get_password", "keyring.get_password"),
            ("keyring.set_password", "keyring.set_password"),
            ("keyring.delete_password", "keyring.delete_password"),
        )
        self._secret_guards = {}
        for name, target in targets:
            patcher = patch(target, side_effect=_forbid_real_secret_access)
            guard = patcher.start()
            self.addCleanup(patcher.stop)
            self._secret_guards[name] = guard

    def tearDown(self):
        self.assertEqual(
            {name: guard.call_count for name, guard in self._secret_guards.items()},
            {
                "secrets.get_config": 0,
                "secrets._backend": 0,
                "keyring.get_password": 0,
                "keyring.set_password": 0,
                "keyring.delete_password": 0,
            },
            "model-context tests reached a real secret/keyring entry instead of a Fake/Mock",
        )

    def test_collection_copy_uses_exact_fullwidth_exclamation(self):
        text = NO_MODEL_CARD_COPY["collection"]
        self.assertEqual(text, "把他看透！")
        self.assertTrue(text.endswith("\uff01"))

    def test_unread_cloud_config_never_requests_credentials_for_a_status_label(self):
        with patch("src.ui.model_context.secrets.configuration_loaded", return_value=False):
            for context in (
                asr_model_context(SimpleNamespace(engine_name="custom_openai")),
            ):
                self.assertEqual(context.state, "unknown")
                self.assertEqual(context.card_status_text, "大模型 API")
                self.assertNotIn("未连接", context.status_text)

    def test_local_context_distinguishes_ready_missing_and_broken(self):
        ready = local_model_context("文案提取", "SenseVoiceSmall", ready=True, purpose_index=0)
        missing = local_model_context("文案提取", "SenseVoiceSmall", ready=False, purpose_index=0)
        broken = local_model_context(
            "文案提取", "SenseVoiceSmall", ready=False, broken=True, purpose_index=0
        )

        self.assertEqual(ready.status_text, "本地模型已就绪")
        self.assertEqual(missing.status_text, "本地模型未就绪")
        self.assertEqual(broken.status_text, "本地模型需修复")
        self.assertEqual(ready.card_status_text, "本地模型 · 已就绪")
        self.assertEqual(missing.card_status_text, "本地模型 · 未就绪")
        self.assertEqual(broken.card_status_text, "本地模型 · 需修复")
        self.assertEqual(ready.model_name, "SenseVoiceSmall")
        self.assertEqual((ready.purpose_index, ready.sub_index), (0, 0))

    def test_api_context_fails_closed_without_hiding_model_name(self):
        configured = api_model_context("文案处理", "agnes-2.0-flash", configured=True, purpose_index=2)
        missing = api_model_context("文案处理", "deepseek-v4-flash", configured=False, purpose_index=2)

        self.assertEqual(configured.status_text, "大模型 API 已配置")
        self.assertEqual(missing.status_text, "大模型 API 未配置")
        self.assertEqual(configured.card_status_text, "大模型 API · 已连接")
        self.assertEqual(missing.card_status_text, "大模型 API · 未连接")
        self.assertEqual(configured.model_name, "agnes-2.0-flash")
        self.assertEqual((configured.purpose_index, configured.sub_index), (2, 1))

    def test_asr_local_uses_download_and_runtime_truth(self):
        cfg = SimpleNamespace(engine_name="local_sensevoice")
        with (
            patch("src.ui.model_context.models_manager.is_downloaded", return_value=True),
            patch("src.ui.model_context.models_manager.runtime_issue", return_value=None),
        ):
            context = asr_model_context(cfg)
        self.assertEqual(context.usage, "文案提取")
        self.assertEqual(context.status_text, "本地模型已就绪")
        self.assertEqual(context.card_status_text, "本地模型 · 已就绪")
        self.assertTrue(context.model_name)

        with (
            patch("src.ui.model_context.models_manager.is_downloaded", return_value=True),
            patch("src.ui.model_context.models_manager.runtime_issue", return_value="runtime broken"),
        ):
            context = asr_model_context(cfg)
        self.assertEqual(context.status_text, "本地模型需修复")
        self.assertEqual(context.card_status_text, "本地模型 · 需修复")

    def test_asr_api_configuration_read_failure_is_unconfigured(self):
        cfg = SimpleNamespace(engine_name="openai")
        with patch(
            "src.ui.model_context.providers.configuration_snapshot",
            side_effect=RuntimeError("keyring unavailable"),
        ):
            context = asr_model_context(cfg)
        self.assertEqual(context.status_text, "大模型 API 未配置")
        self.assertEqual(context.card_status_text, "大模型 API · 未连接")
        self.assertEqual(context.model_name, "gpt-4o-transcribe")

    def test_custom_openai_uses_real_configured_model_without_exposing_key(self):
        raw = {
            "api_key": "secret-must-not-leak",
            "base_url": "https://example.test/v1",
            "model": "vendor/real-asr-model-v2",
        }
        with patch("src.core.engines.providers.secrets.get_config", return_value=raw):
            snapshot = providers.configuration_snapshot("custom_openai")
            context = asr_model_context(SimpleNamespace(engine_name="custom_openai"))

        self.assertTrue(snapshot.configured)
        self.assertEqual(snapshot.model_name, "vendor/real-asr-model-v2")
        self.assertFalse(hasattr(snapshot, "api_key"))
        self.assertEqual(context.status_text, "大模型 API 已配置")
        self.assertEqual(context.card_status_text, "大模型 API · 已连接")
        self.assertEqual(context.model_name, "vendor/real-asr-model-v2")
        self.assertNotIn("secret-must-not-leak", repr(snapshot))
        self.assertNotIn("secret-must-not-leak", repr(context))

    def test_custom_openai_missing_model_is_neutral_unconfigured(self):
        raw = {
            "api_key": "configured-key",
            "base_url": "https://example.test/v1",
            "model": "",
        }
        with patch("src.core.engines.providers.secrets.get_config", return_value=raw):
            snapshot = providers.configuration_snapshot("custom_openai")
            context = asr_model_context(SimpleNamespace(engine_name="custom_openai"))

        self.assertFalse(snapshot.configured)
        self.assertEqual(snapshot.model_name, "")
        self.assertEqual(context.status_text, "大模型 API 未配置")
        self.assertEqual(context.card_status_text, "大模型 API · 未连接")
        self.assertEqual(context.model_name, "尚未设置模型名称")
        self.assertEqual(context.status_kind, "neutral")

    def test_custom_openai_secure_read_failure_is_neutral_unconfigured(self):
        with patch(
            "src.core.engines.providers.secrets.get_config",
            side_effect=RuntimeError("keyring locked"),
        ):
            context = asr_model_context(SimpleNamespace(engine_name="custom_openai"))

        self.assertEqual(context.status_text, "大模型 API 未配置")
        self.assertEqual(context.card_status_text, "大模型 API · 未连接")
        self.assertEqual(context.model_name, "尚未设置模型名称")
        self.assertEqual(context.status_kind, "neutral")


    def test_neutral_context_uses_only_requested_interaction_copy(self):
        context = neutral_model_context("settings")
        self.assertEqual(context.status_text, "把 Lulu 调得更顺手")
        self.assertEqual(context.deployment, "none")
        self.assertEqual(context.model_name, "把 Lulu 调得更顺手")
        self.assertEqual(context.card_status_text, "")
        self.assertNotIn("无需模型", context.model_name)


if __name__ == "__main__":
    unittest.main()

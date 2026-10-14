"""Phase 2 human-only secret input boundary contract tests."""

from __future__ import annotations

from dataclasses import fields
import inspect
import unittest


class Phase2SecretInputBoundaryTests(unittest.TestCase):
    def test_all_nonhuman_channels_return_secret_free_trusted_handoff_only(self):
        from src.core.auth_contracts import HumanSecretInputHandoffV1
        from src.core.auth_guard import HumanSecretInputPolicy

        policy = HumanSecretInputPolicy()
        channels = (
            "argv",
            "env",
            "stdin",
            "pipe",
            "pty",
            "tty",
            "config",
            "chat",
            "agent",
            "skill",
        )
        for purpose in ("license_activation", "api_key"):
            for channel in channels:
                with self.subTest(purpose=purpose, channel=channel):
                    handoff = policy.request_handoff(
                        source_channel=channel,
                        purpose=purpose,
                    )
                    self.assertIs(type(handoff), HumanSecretInputHandoffV1)
                    self.assertFalse(handoff.accepted)
                    self.assertEqual(handoff.code, "HUMAN_HANDOFF_REQUIRED")
                    self.assertEqual(handoff.destination, "trusted_gui_or_os")
                    self.assertEqual(handoff.source_channel, channel)

        parameter_names = tuple(
            inspect.signature(HumanSecretInputPolicy.request_handoff).parameters
        )
        self.assertEqual(parameter_names, ("self", "source_channel", "purpose"))
        dto_names = {field.name for field in fields(HumanSecretInputHandoffV1)}
        self.assertFalse(
            dto_names
            & {"secret", "value", "license_code", "api_key", "authorization_code"}
        )
        self.assertFalse(hasattr(policy, "__dict__"))


if __name__ == "__main__":
    unittest.main()

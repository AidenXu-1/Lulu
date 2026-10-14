import sys
import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core import secrets
from src.core.license import store as license_store


class _FakeMacOSError:
    item_not_found = -25300

    @staticmethod
    def raise_for_status(status: int) -> None:
        if status != 0:
            raise RuntimeError(status)


class _FakeSecurityFramework:
    def __init__(self, update_status: int):
        self.update_status = update_status
        self.update_calls = 0

    def SecItemUpdate(self, _query, _values) -> int:
        self.update_calls += 1
        return self.update_status


class _FakeMacOSApi:
    def __init__(self, update_status: int):
        self._sec = _FakeSecurityFramework(update_status)
        self.error = _FakeMacOSError
        self.Error = _FakeMacOSError
        self.add_calls = 0

    @staticmethod
    def k_(value: str) -> str:
        return value

    @staticmethod
    def create_query(**values):
        return values

    def SecItemAdd(self, _query, _result) -> int:
        self.add_calls += 1
        self.last_add_query = _query
        return 0


def _acl_preserving_backend(update_status: int):
    backend = object.__new__(license_store._MacOSExplicitKeyring)
    backend.api = _FakeMacOSApi(update_status)
    backend._keychain_ref = lambda: "isolated-keychain"
    backend._search_list = lambda _keychain: ["isolated-keychain"]
    backend._secret_data = lambda _value: b"opaque-secret"
    return backend


class _RecordingBackend:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}
        self.writes: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.writes.append((service, account, value))
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.deletes.append((service, account))
        self.values.pop((service, account), None)


class SecretKeychainPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        secrets._memory.clear()
        secrets._cache.clear()

    def tearDown(self) -> None:
        secrets._memory.clear()
        secrets._cache.clear()

    def test_macos_public_secret_write_uses_acl_preserving_backend(self):
        backend = _acl_preserving_backend(update_status=0)
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(
                license_store,
                "_MacOSExplicitKeyring",
                return_value=backend,
            ) as backend_factory,
            patch(
                "keyring.set_password",
                side_effect=AssertionError("delete-and-recreate backend must not be used"),
            ),
        ):
            secrets.set_key("feishu_wiki", "opaque-secret")

        backend_factory.assert_called_once_with()
        self.assertEqual(backend.api._sec.update_calls, 1)
        self.assertEqual(backend.api.add_calls, 0)
        self.assertNotIn("feishu_wiki", secrets._memory)

    def test_macos_public_secret_write_adds_only_when_item_is_missing(self):
        backend = _acl_preserving_backend(update_status=_FakeMacOSError.item_not_found)
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(
                license_store,
                "_MacOSExplicitKeyring",
                return_value=backend,
            ),
            patch(
                "keyring.set_password",
                side_effect=AssertionError("delete-and-recreate backend must not be used"),
            ),
        ):
            secrets.set_key("feishu_wiki", "opaque-secret")

        self.assertEqual(backend.api._sec.update_calls, 1)
        self.assertEqual(backend.api.add_calls, 1)

    def test_macos_public_secret_read_and_explicit_delete_use_same_backend(self):
        backend = _RecordingBackend()
        backend.values[("Lulu", "summary_llm")] = "opaque-secret"
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(
                license_store,
                "_MacOSExplicitKeyring",
                return_value=backend,
            ) as backend_factory,
            patch(
                "keyring.get_password",
                side_effect=AssertionError("generic macOS backend must not be used"),
            ),
            patch(
                "keyring.delete_password",
                side_effect=AssertionError("generic macOS backend must not be used"),
            ),
        ):
            self.assertEqual(secrets.get_key("summary_llm"), "opaque-secret")
            secrets.delete_key("summary_llm")

        self.assertEqual(backend_factory.call_count, 2)
        self.assertEqual(backend.deletes, [("Lulu", "summary_llm")])
        self.assertNotIn(("Lulu", "summary_llm"), backend.values)

    def test_non_macos_keeps_cross_platform_keyring_backend(self):
        backend = _RecordingBackend()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(
                license_store,
                "_MacOSExplicitKeyring",
                return_value=backend,
            ) as backend_factory,
            patch("keyring.set_password") as generic_set,
        ):
            secrets.set_key("summary_llm", "opaque-secret")

        generic_set.assert_called_once_with("Lulu", "summary_llm", "opaque-secret")
        backend_factory.assert_not_called()

    def test_isolated_owned_add_carries_envelope_and_requests_private_receipt_material(self):
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            _owned_item_claims,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-owned-security-adapter-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "owned-security-adapter")
            authority = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            intent = _new_ownership_intents(authority, final_acl)[0]
            backend = _acl_preserving_backend(update_status=0)
            backend._owned_result = lambda _result, _account, _final_acl: {
                "persistent_ref": b"private-persistent-ref",
                "envelope": __import__(
                    "src.core.cli_runtime.installation_ticket",
                    fromlist=["_ownership_intent_claims"],
                )._ownership_intent_claims(intent).envelope,
                "attributes_sha256": "a" * 64,
            }
            receipt = backend.create_owned_item(
                final_acl, intent, "opaque-secret"
            )
            query = backend.api.last_add_query
            self.assertIn("kSecAttrGeneric", query)
            self.assertEqual(query["kSecAttrAccess"], "explicit-final-access")
            self.assertTrue(query["kSecReturnRef"])
            self.assertTrue(query["kSecReturnAttributes"])
            self.assertTrue(query["kSecReturnPersistentRef"])
            self.assertFalse(query["kSecReturnData"])
            self.assertEqual(backend.api._sec.update_calls, 0)
            self.assertNotIn(
                b"opaque-secret", _owned_item_claims(receipt).envelope
            )
            self.assertEqual(
                _owned_item_claims(receipt).persistent_ref,
                b"private-persistent-ref",
            )


if __name__ == "__main__":
    unittest.main()

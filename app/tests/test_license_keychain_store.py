import unittest
import ctypes
from pathlib import Path
import os
import pickle
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.license import store as store_module
from src.core.license.store import (
    DEVICE_SALT_KEY,
    SecureStoreError,
    StrictKeyringStore,
    MemorySecureStore,
)


class _FakeKeyring:
    def __init__(self, *, fail_set=False):
        self.values = {}
        self.fail_set = fail_set

    def get_password(self, service, key):
        return self.values.get((service, key))

    def set_password(self, service, key, value):
        if self.fail_set:
            raise RuntimeError("backend failure")
        self.values[(service, key)] = value

    def add_password_new(self, service, key, value):
        identity = (service, key)
        if identity in self.values:
            raise RuntimeError("duplicate")
        self.values[identity] = value

    def delete_password(self, service, key, *, missing_ok=False):
        identity = (service, key)
        if identity not in self.values and not missing_ok:
            raise RuntimeError("missing")
        self.values.pop(identity, None)


class _MissingIsolatedKeyring:
    """Pure test double for a configured keychain path that cannot be opened."""

    def __init__(self):
        self.calls = []

    def set_password(self, service, key, value):
        self.calls.append((service, key, value))
        raise FileNotFoundError("isolated keychain does not exist")


class _FakeOwnedResultFoundation:
    def __init__(self, values):
        self.values = values

    def CFDictionaryGetValue(self, _dictionary, key):
        return self.values[key]


class _FakeOwnedResultApi:
    def __init__(self, values):
        self._found = _FakeOwnedResultFoundation(values)

    @staticmethod
    def k_(value):
        return value


class _FakeOwnedRuntimeError:
    item_not_found = -25300

    @staticmethod
    def raise_for_status(status):
        if status != 0:
            raise RuntimeError(status)


class _FakeOwnedRuntimeApi:
    error = _FakeOwnedRuntimeError
    Error = _FakeOwnedRuntimeError

    def __init__(self, backend):
        self.backend = backend
        self._found = _FakeOwnedResultFoundation(
            {
                "kSecValuePersistentRef": 10,
                "kSecAttrGeneric": 11,
                "kSecAttrService": 12,
                "kSecAttrAccount": 13,
                "kSecValueRef": 14,
            }
        )

    @staticmethod
    def k_(value):
        return value

    @staticmethod
    def create_query(**values):
        return values

    @staticmethod
    def _set_result(result):
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(1)

    def SecItemAdd(self, query, result):
        account = query["kSecAttrAccount"]
        if account in self.backend.items:
            return -25299
        envelope = query["kSecAttrGeneric"]
        cdhash = __import__("json").loads(envelope)["trusted_cli_cdhash"]
        item = {
            "service": query["kSecAttrService"],
            "account": account,
            "envelope": envelope,
            "persistent_ref": ("persistent:" + account).encode("utf-8"),
            "reported_persistent_ref": ("persistent:" + account).encode("utf-8"),
            "partition_ids": ("cdhash:" + cdhash,),
            "evidence_sha256": __import__("hashlib").sha256(
                ("partition:cdhash:" + cdhash).encode("ascii")
            ).hexdigest(),
        }
        if self.backend.add_mutation:
            field, value = self.backend.add_mutation
            item[field] = value
        self.backend.items[account] = item
        self.backend.current = item
        self.backend.create_count += 1
        self._set_result(result)
        return 0

    def SecItemCopyMatching(self, query, result):
        item = None
        persistent_ref = query.get("kSecValuePersistentRef")
        if persistent_ref is not None:
            item = next(
                (
                    value
                    for value in self.backend.items.values()
                    if value["persistent_ref"] == persistent_ref
                ),
                None,
            )
        else:
            item = self.backend.items.get(query.get("kSecAttrAccount"))
        if item is None:
            return self.error.item_not_found
        self.backend.current = item
        self._set_result(result)
        return 0

    def SecItemDelete(self, query):
        persistent_ref = query["kSecMatchItemList"][0]
        selected = next(
            (
                account
                for account, item in self.backend.items.items()
                if item["persistent_ref"] == persistent_ref
            ),
            None,
        )
        if selected is None:
            return self.error.item_not_found
        del self.backend.items[selected]
        self.backend.delete_count += 1
        return 0


def _isolated_macos_owned_backend():
    backend = object.__new__(store_module._MacOSExplicitKeyring)
    backend.items = {}
    backend.current = None
    backend.add_mutation = None
    backend.create_count = 0
    backend.delete_count = 0
    backend.api = _FakeOwnedRuntimeApi(backend)
    backend._keychain_ref = lambda: "isolated-keychain"
    backend._search_list = lambda _keychain: ["isolated-keychain"]
    backend._array = lambda values: list(values)
    backend._release = lambda _value: None
    backend._secret_data = lambda value: value.encode("utf-8")
    backend._bytes_data = lambda value: value
    backend._cfdata_bytes = lambda value: {
        10: backend.current["reported_persistent_ref"],
        11: backend.current["envelope"],
    }[value if isinstance(value, int) else value.value]
    backend._cfstring_text = lambda value: {
        12: backend.current["service"],
        13: backend.current["account"],
    }[value if isinstance(value, int) else value.value]
    def canonicalize_item(_item, expected):
        import copy
        import json

        payload = copy.deepcopy(expected)
        expected_partition = (
            "cdhash:"
            + payload["entries"][0]["trusted_applications"][0][
                "raw_data_utf8"
            ].split("cdhash:", 1)[1]
        )
        if backend.current.get("partition_ids") != (expected_partition,):
            payload["entries"][0]["description"] = "drifted-live-acl"
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    backend._canonicalize_item = canonicalize_item
    backend._persistent_ref_from_item = lambda _item: backend.current[
        "persistent_ref"
    ]
    def prepare_final_acl(locator_receipt, plan_receipt):
        from tests.test_cli_versioned_installation_ticket import (
            _final_acl_receipt,
        )

        return _final_acl_receipt(
            locator_receipt,
            plan_receipt,
            access_ref="isolated-explicit-final-access",
        )

    backend.prepare_final_acl_intent = prepare_final_acl
    return backend


class LicenseKeychainStoreTests(unittest.TestCase):
    def test_pyinstaller_collects_explicit_macos_security_api(self):
        spec = Path(__file__).parents[1] / "packaging" / "Lulu.spec"
        self.assertIn('"keyring.backends.macOS.api"', spec.read_text(encoding="utf-8"))

    def test_first_device_salt_creation_is_stable(self):
        backend = _FakeKeyring()
        store = StrictKeyringStore(service="Lulu.UnitProbe")
        store._keyring = lambda: backend  # type: ignore[method-assign]

        first = store.get_or_create_device_salt(lambda: "first-salt")
        second = store.get_or_create_device_salt(lambda: "unexpected")

        self.assertEqual(first, "first-salt")
        self.assertEqual(second, "first-salt")
        self.assertEqual(backend.values[("Lulu.UnitProbe", DEVICE_SALT_KEY)], "first-salt")

    def test_write_failure_is_fail_closed_without_reset_instruction(self):
        store = StrictKeyringStore(service="Lulu.UnitProbe")
        store._keyring = lambda: _FakeKeyring(fail_set=True)  # type: ignore[method-assign]

        with self.assertRaises(SecureStoreError) as caught:
            store.set_text(DEVICE_SALT_KEY, "test-value")

        message = str(caught.exception)
        self.assertIn("登录钥匙串已解锁", message)
        self.assertNotIn("还原为默认", message)
        self.assertNotIn("test-value", message)

    def test_missing_isolated_keychain_fails_closed(self):
        missing_path = str(Path("isolated") / "missing.keychain-db")
        backend = _MissingIsolatedKeyring()
        with (
            patch.object(store_module.sys, "platform", "darwin"),
            patch.object(store_module, "_MacOSExplicitKeyring", return_value=backend) as backend_factory,
        ):
            store = StrictKeyringStore(service="Lulu.UnitProbe")
            store.keychain_path = missing_path

            with self.assertRaises(SecureStoreError):
                store.set_text(DEVICE_SALT_KEY, "test-value")

        backend_factory.assert_called_once_with(missing_path)
        self.assertEqual(
            backend.calls,
            [("Lulu.UnitProbe", DEVICE_SALT_KEY, "test-value")],
        )

    def test_versioned_create_new_and_missing_ok_delete_are_exact(self):
        backend = _FakeKeyring()
        store = StrictKeyringStore(service="Lulu.UnitProbe")
        store._keyring = lambda: backend  # type: ignore[method-assign]

        store.write_new("versioned-account", "first")
        with self.assertRaises(SecureStoreError):
            store.write_new("versioned-account", "replacement")
        self.assertEqual(
            backend.values[("Lulu.UnitProbe", "versioned-account")],
            "first",
        )

        store.delete_text("versioned-account", missing_ok=True)
        store.delete_text("versioned-account", missing_ok=True)
        self.assertNotIn(("Lulu.UnitProbe", "versioned-account"), backend.values)

    def test_fake_owned_adapter_is_add_only_inventory_only_and_persistent_ref_deleted(self):
        from src.core.cli_runtime.installation_ticket import (
            _inventory_classification,
            _new_ownership_intents,
            _new_ownership_inventory_intents,
            _owned_item_claims,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-owned-store-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "owned-store")
            authority = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            store = MemorySecureStore()
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            intent = _new_ownership_intents(authority, final_acl)[0]
            created = store.create_owned_item(
                final_acl, intent, "opaque-secret"
            )
            claims = _owned_item_claims(created)
            self.assertNotIn(b"opaque-secret", claims.envelope)
            self.assertIn(b'"contains_secret_material":false', claims.envelope)
            with self.assertRaises(TypeError):
                pickle.dumps(created)
            with self.assertRaises(SecureStoreError):
                store.create_owned_item(final_acl, intent, "replacement")
            verified = store.inspect_exact_item(
                final_acl,
                _new_ownership_inventory_intents(authority, final_acl)[0],
            )
            self.assertEqual(
                _inventory_classification(
                    verified, authority=authority, account_index=0
                ),
                "owned",
            )
            self.assertEqual(store.secret_read_count, 0)
            with self.assertRaises(TypeError):
                store.delete_verified_item(
                    final_acl, {"owned": True, "persistent_ref": "forged"}
                )
            forged_type = type("ForgedVerifiedReceipt", (type(verified),), {})
            forged = object.__new__(forged_type)
            with self.assertRaises(TypeError):
                store.delete_verified_item(final_acl, forged)
            self.assertEqual(store.delete_count, 0)
            store.delete_verified_item(final_acl, verified)
            absent = store.inspect_exact_item(
                final_acl,
                _new_ownership_inventory_intents(authority, final_acl)[0],
            )
            self.assertEqual(
                _inventory_classification(
                    absent, authority=authority, account_index=0
                ),
                "absent",
            )
            self.assertEqual(store.delete_count, 1)

    def test_real_adapter_source_requests_envelope_attributes_ref_and_no_secret_inventory(self):
        source = Path(store_module.__file__).read_text(encoding="utf-8")
        for required in (
            "kSecAttrGeneric",
            "kSecAttrAccess",
            "kSecReturnRef=True",
            "kSecReturnAttributes=True",
            "kSecReturnPersistentRef=True",
            "kSecReturnData=False",
            "SecKeychainItemCopyAccess",
            "SecAccessCopyOwnerAndACL",
            "SecACLCopyContents",
            "SecACLCopyAuthorizations",
            "SecTrustedApplicationCopyData",
            "kSecMatchItemList",
        ):
            self.assertIn(required, source)
        delete_source = __import__("inspect").getsource(
            store_module._MacOSExplicitKeyring.delete_verified_item
        )
        self.assertIn("kSecMatchItemList", delete_source)
        self.assertNotIn(
            "kSecValuePersistentRef",
            delete_source.split("delete_query =", 1)[1],
        )

    def test_real_adapter_rejects_live_partition_acl_drift_instead_of_trusting_envelope(self):
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            _new_ownership_inventory_intents,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-full-acl-drift-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "full-acl-drift")
            authority = _validate_test_authority(
                locator_receipt=locator, plan_receipt=plan
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            backend = _isolated_macos_owned_backend()
            backend.create_owned_item(
                final_acl,
                _new_ownership_intents(authority, final_acl)[0],
                "opaque-secret",
            )
            backend.items[authority.accounts[0]]["partition_ids"] = (
                "cdhash:" + "b" * 40,
            )
            blocked = backend.inspect_exact_item(
                final_acl,
                _new_ownership_inventory_intents(authority, final_acl)[0],
            )
            self.assertEqual(
                __import__(
                    "src.core.cli_runtime.installation_ticket",
                    fromlist=["_inventory_classification"],
                )._inventory_classification(
                    blocked, authority=authority, account_index=0
                ),
                "foreign_or_unproven",
            )

    def test_isolated_real_adapter_live_drift_issues_no_verified_receipt_or_delete(self):
        from src.core.cli_runtime.installation_ticket import (
            _inventory_classification,
            _new_ownership_intents,
            _new_ownership_inventory_intents,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        mutations = {
            "service": lambda item: item.__setitem__("service", "Foreign"),
            "account": lambda item: item.__setitem__("account", "foreign-account"),
            "generic": lambda item: item.__setitem__("envelope", b"{}\n"),
            "persistent_ref": lambda item: item.__setitem__(
                "reported_persistent_ref", b"drifted-persistent-ref"
            ),
            "partition_acl": lambda item: (
                item.__setitem__("partition_ids", ("cdhash:" + "b" * 40,)),
                item.__setitem__("evidence_sha256", "f" * 64),
            ),
        }
        with TemporaryDirectory(prefix="lulu-real-adapter-drift-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "real-adapter-drift")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            for name, mutate in mutations.items():
                with self.subTest(drift=name):
                    backend = _isolated_macos_owned_backend()
                    created = backend.create_owned_item(
                        final_acl,
                        _new_ownership_intents(authority, final_acl)[0],
                        "opaque-secret",
                    )
                    exact = backend.inspect_exact_item(
                        final_acl,
                        _new_ownership_inventory_intents(authority, final_acl)[0],
                    )
                    self.assertEqual(
                        _inventory_classification(
                            exact,
                            authority=authority,
                            account_index=0,
                        ),
                        "owned",
                    )
                    account = authority.accounts[0]
                    mutate(backend.items[account])
                    drifted = backend.inspect_exact_item(
                        final_acl,
                        _new_ownership_inventory_intents(
                            authority, final_acl
                        )[0],
                    )
                    self.assertEqual(
                        _inventory_classification(
                            drifted, authority=authority, account_index=0
                        ),
                        "foreign_or_unproven",
                    )
                    self.assertEqual(backend.delete_count, 0)
                    with self.assertRaises((SecureStoreError, RuntimeError)):
                        backend.delete_verified_item(final_acl, exact)
                    self.assertEqual(backend.delete_count, 0)
                    self.assertIsNotNone(created)

    def test_full_acl_canonical_drift_never_signs_inventory_or_deletes(self):
        import copy
        import json
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            _new_ownership_inventory_intents,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        mutations = {
            "trusted_app_raw": lambda value: value["entries"][1][
                "trusted_applications"
            ][0].__setitem__("raw_data_sha256", "f" * 64),
            "description": lambda value: value["entries"][1].__setitem__(
                "description", "Foreign"
            ),
            "prompt": lambda value: value["entries"][1].__setitem__(
                "prompt_selector", 1
            ),
            "entry_count": lambda value: value.__setitem__("entry_count", 3),
            "authorization": lambda value: value["entries"][1].__setitem__(
                "authorizations", ["kSecACLAuthorizationDecrypt"]
            ),
            "duplicate": lambda value: value["entries"][1][
                "trusted_applications"
            ].append(copy.deepcopy(value["entries"][1]["trusted_applications"][0])),
            "unknown": lambda value: value["entries"][0][
                "trusted_applications"
            ][0].__setitem__("raw_data_utf8", "apple:"),
            "owner": lambda value: value["owner"].__setitem__("uid", 0),
        }
        with TemporaryDirectory(prefix="lulu-full-canonical-matrix-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "full-canonical-matrix")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            for name, mutate in mutations.items():
                with self.subTest(drift=name):
                    backend = _isolated_macos_owned_backend()
                    backend.create_owned_item(
                        final_acl,
                        _new_ownership_intents(authority, final_acl)[0],
                        "opaque-secret",
                    )

                    def drifted(_item, expected, mutation=mutate):
                        payload = copy.deepcopy(expected)
                        mutation(payload)
                        return (
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")

                    backend._canonicalize_item = drifted
                    drifted_receipt = backend.inspect_exact_item(
                        final_acl,
                        _new_ownership_inventory_intents(
                            authority, final_acl
                        )[0],
                    )
                    from src.core.cli_runtime.installation_ticket import (
                        _inventory_classification,
                    )

                    self.assertEqual(
                        _inventory_classification(
                            drifted_receipt,
                            authority=authority,
                            account_index=0,
                        ),
                        "foreign_or_unproven",
                    )
                    self.assertEqual(backend.delete_count, 0)

    def test_delete_before_rechecks_full_acl_and_uses_match_list(self):
        import copy
        import json
        from src.core.cli_runtime.installation_ticket import (
            _new_ownership_intents,
            _new_ownership_inventory_intents,
        )
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-delete-before-acl-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "delete-before-acl")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            backend = _isolated_macos_owned_backend()
            backend.create_owned_item(
                final_acl,
                _new_ownership_intents(authority, final_acl)[0],
                "opaque-secret",
            )
            verified = backend.inspect_exact_item(
                final_acl,
                _new_ownership_inventory_intents(authority, final_acl)[0],
            )

            def drifted(_item, expected):
                payload = copy.deepcopy(expected)
                payload["entries"][0]["description"] = "drifted-before-delete"
                return (
                    json.dumps(payload, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")

            backend._canonicalize_item = drifted
            with self.assertRaises(SecureStoreError):
                backend.delete_verified_item(final_acl, verified)
            self.assertEqual(backend.delete_count, 0)

    def test_isolated_real_adapter_created_receipt_binds_live_service_account_and_ref(self):
        from src.core.cli_runtime.installation_ticket import _new_ownership_intents
        from tests.test_cli_versioned_installation_ticket import (
            _authority,
            _final_acl_receipt,
            _validate_test_authority,
        )

        with TemporaryDirectory(prefix="lulu-real-adapter-created-") as directory:
            root = Path(directory).resolve()
            os.chmod(root, 0o700)
            locator, plan = _authority(root, "real-adapter-created")
            authority = _validate_test_authority(
                locator_receipt=locator,
                plan_receipt=plan,
            )
            final_acl = _final_acl_receipt(
                locator, plan, access_ref="explicit-final-access"
            )
            for field, value in (
                ("service", "Foreign"),
                ("account", "foreign-account"),
                ("reported_persistent_ref", b"unresolvable-ref"),
            ):
                with self.subTest(field=field):
                    backend = _isolated_macos_owned_backend()
                    backend.add_mutation = (field, value)
                    with self.assertRaises(SecureStoreError):
                        backend.create_owned_item(
                            final_acl,
                            _new_ownership_intents(authority, final_acl)[0],
                            "opaque-secret",
                        )
                    self.assertEqual(backend.delete_count, 0)


if __name__ == "__main__":
    unittest.main()

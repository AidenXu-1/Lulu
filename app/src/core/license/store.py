from __future__ import annotations

import ctypes
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from dataclasses import dataclass
from threading import RLock

from .models import LicenseCache, PendingDeactivation

SERVICE = "Lulu"
CACHE_KEY = "license_cache_v1"
DEVICE_SALT_KEY = "license_device_salt_v1"
PENDING_DEACTIVATION_KEY = "license_pending_deactivation_v1"

# Backend instances are short-lived; the system authorization queue is shared
# by the entire process. A retry must wait until the pending read has finished.
_MACOS_READ_LOCK = RLock()


class SecureStoreError(RuntimeError):
    pass


class _MacOSExplicitKeyring:
    """Small macOS backend that pins writes to an opened keychain reference.

    keyring's macOS SecItemAdd path relies on the process default keychain. A
    frozen app can fail its first add even when the user's login keychain is in
    the normal search list. Opening the default keychain and passing
    kSecUseKeychain removes that implicit lookup without changing the user's
    default/search-list settings.
    """

    def __init__(self, keychain_path: str | None = None):
        from keyring.backends.macOS import api

        self.api = api
        self.keychain_path = keychain_path
        self._configure_api()

    def _configure_api(self) -> None:
        api = self.api
        api._sec.SecKeychainCopyDefault.restype = api.OS_status
        api._sec.SecKeychainCopyDefault.argtypes = (ctypes.POINTER(ctypes.c_void_p),)
        api._sec.SecKeychainOpen.restype = api.OS_status
        api._sec.SecKeychainOpen.argtypes = (ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
        api._sec.SecItemUpdate.restype = api.OS_status
        api._sec.SecItemUpdate.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        api._found.CFArrayCreate.restype = ctypes.c_void_p
        api._found.CFArrayCreate.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
        )
        api._found.CFDataCreate.restype = ctypes.c_void_p
        api._found.CFDataCreate.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_long,
        )
        api._found.CFDictionaryGetValue.restype = ctypes.c_void_p
        api._found.CFDictionaryGetValue.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        api._found.CFArrayGetCount.restype = ctypes.c_long
        api._found.CFArrayGetCount.argtypes = (ctypes.c_void_p,)
        api._found.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        api._found.CFArrayGetValueAtIndex.argtypes = (
            ctypes.c_void_p,
            ctypes.c_long,
        )
        api._found.CFStringGetLength.restype = ctypes.c_long
        api._found.CFStringGetLength.argtypes = (ctypes.c_void_p,)
        api._found.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        api._found.CFStringGetMaximumSizeForEncoding.argtypes = (
            ctypes.c_long,
            ctypes.c_uint32,
        )
        api._found.CFStringGetCString.restype = ctypes.c_bool
        api._found.CFStringGetCString.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        )
        api._found.CFRelease.restype = None
        api._found.CFRelease.argtypes = (ctypes.c_void_p,)
        api._sec.SecKeychainItemCopyFromPersistentReference.restype = api.OS_status
        api._sec.SecKeychainItemCopyFromPersistentReference.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecKeychainItemCreatePersistentReference.restype = api.OS_status
        api._sec.SecKeychainItemCreatePersistentReference.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecKeychainItemCopyAccess.restype = api.OS_status
        api._sec.SecKeychainItemCopyAccess.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecACLCopyContents.restype = api.OS_status
        api._sec.SecACLCopyContents.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        )
        api._sec.SecTrustedApplicationCopyData.restype = api.OS_status
        api._sec.SecTrustedApplicationCopyData.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecTrustedApplicationCreateFromPath.restype = api.OS_status
        api._sec.SecTrustedApplicationCreateFromPath.argtypes = (
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecTrustedApplicationSetData.restype = api.OS_status
        api._sec.SecTrustedApplicationSetData.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        api._sec.SecAccessCreateWithOwnerAndACL.restype = ctypes.c_void_p
        api._sec.SecAccessCreateWithOwnerAndACL.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecAccessCopyOwnerAndACL.restype = api.OS_status
        api._sec.SecAccessCopyOwnerAndACL.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecACLCreateWithSimpleContents.restype = api.OS_status
        api._sec.SecACLCreateWithSimpleContents.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        )
        api._sec.SecACLUpdateAuthorizations.restype = api.OS_status
        api._sec.SecACLUpdateAuthorizations.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        api._sec.SecACLCopyAuthorizations.restype = ctypes.c_void_p
        api._sec.SecACLCopyAuthorizations.argtypes = (ctypes.c_void_p,)
        api._found.CFStringCreateWithCString.restype = ctypes.c_void_p
        api._found.CFStringCreateWithCString.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )

    @staticmethod
    def _ownership_helpers():
        from src.core.cli_runtime.installation_ticket import (
            _owned_item_claims,
            _ownership_intent_claims,
            _register_created_owned_item,
            _register_inventory_result,
        )

        return (
            _ownership_intent_claims,
            _register_created_owned_item,
            _register_inventory_result,
            _owned_item_claims,
        )

    def _keychain_ref(self) -> ctypes.c_void_p:
        ref = ctypes.c_void_p()
        if self.keychain_path:
            status = self.api._sec.SecKeychainOpen(
                str(self.keychain_path).encode("utf-8"), ctypes.byref(ref)
            )
        else:
            status = self.api._sec.SecKeychainCopyDefault(ctypes.byref(ref))
        self.api.Error.raise_for_status(status)
        if not ref.value:
            raise self.api.Error(-25294, "Default keychain reference unavailable")
        return ref

    def _search_list(self, keychain: ctypes.c_void_p) -> ctypes.c_void_p:
        values = (ctypes.c_void_p * 1)(keychain.value)
        return ctypes.c_void_p(
            self.api._found.CFArrayCreate(
                None,
                values,
                1,
                ctypes.byref(self.api._found.kCFTypeArrayCallBacks),
            )
        )

    def _secret_data(self, value: str) -> ctypes.c_void_p:
        return self._bytes_data(value.encode("utf-8"))

    def _array(self, values: list[ctypes.c_void_p | int]) -> ctypes.c_void_p:
        pointers = (ctypes.c_void_p * len(values))(
            *[
                value.value if isinstance(value, ctypes.c_void_p) else value
                for value in values
            ]
        )
        return ctypes.c_void_p(
            self.api._found.CFArrayCreate(
                None,
                pointers,
                len(values),
                ctypes.byref(self.api._found.kCFTypeArrayCallBacks),
            )
        )

    def _string(self, value: str) -> ctypes.c_void_p:
        return ctypes.c_void_p(
            self.api._found.CFStringCreateWithCString(
                None,
                value.encode("utf-8"),
                0x08000100,
            )
        )

    def _bytes_data(self, encoded: bytes) -> ctypes.c_void_p:
        buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
        return ctypes.c_void_p(self.api._found.CFDataCreate(None, buffer, len(encoded)))

    def _cfdata_bytes(self, value: ctypes.c_void_p | int) -> bytes:
        pointer = value if isinstance(value, ctypes.c_void_p) else ctypes.c_void_p(value)
        length = self.api._found.CFDataGetLength(pointer)
        address = self.api._found.CFDataGetBytePtr(pointer)
        return ctypes.string_at(address, length)

    def _cfstring_text(self, value: ctypes.c_void_p | int) -> str:
        pointer = value if isinstance(value, ctypes.c_void_p) else ctypes.c_void_p(value)
        encoding = 0x08000100  # kCFStringEncodingUTF8
        length = self.api._found.CFStringGetLength(pointer)
        capacity = (
            self.api._found.CFStringGetMaximumSizeForEncoding(length, encoding) + 1
        )
        if capacity <= 0:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        buffer = ctypes.create_string_buffer(capacity)
        if not self.api._found.CFStringGetCString(
            pointer,
            buffer,
            capacity,
            encoding,
        ):
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        try:
            return buffer.value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecureStoreError(
                "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR"
            ) from exc

    def _release(self, value: ctypes.c_void_p | int | None) -> None:
        if value:
            self.api._found.CFRelease(
                value if isinstance(value, ctypes.c_void_p) else ctypes.c_void_p(value)
            )

    def _copy_trusted_application_data(self, application: object) -> bytes:
        data = ctypes.c_void_p()
        try:
            status = self.api._sec.SecTrustedApplicationCopyData(
                application,
                ctypes.byref(data),
            )
            self.api.Error.raise_for_status(status)
            if not data.value:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
            return self._cfdata_bytes(data)
        finally:
            self._release(data)

    @staticmethod
    def _expected_raw_roles(expected: dict[str, object]) -> dict[bytes, dict[str, object]]:
        result: dict[bytes, dict[str, object]] = {}
        for entry in expected.get("entries", []):
            if type(entry) is not dict:
                continue
            for application in entry.get("trusted_applications", []):
                if type(application) is not dict:
                    continue
                try:
                    raw = (
                        base64.b64decode(application["raw_data_b64"], validate=True)
                        if "raw_data_b64" in application
                        else str(application["raw_data_utf8"]).encode("utf-8")
                    )
                except Exception:
                    continue
                if raw in result:
                    raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
                result[raw] = dict(application)
        return result

    def _authorization_name(self, value: ctypes.c_void_p | int) -> str:
        live = value.value if isinstance(value, ctypes.c_void_p) else value
        for name in (
            "kSecACLAuthorizationAny",
            "kSecACLAuthorizationPartitionID",
        ):
            expected = self.api.k_(name)
            expected_value = (
                expected.value if isinstance(expected, ctypes.c_void_p) else expected
            )
            if live == expected_value:
                return name
        raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")

    def _canonicalize_access(
        self,
        access: ctypes.c_void_p | int,
        *,
        expected: dict[str, object],
    ) -> bytes:
        """Canonicalize complete owner/ACL structure without reading a secret."""

        uid = ctypes.c_uint32()
        gid = ctypes.c_uint32()
        owner_type = ctypes.c_uint32()
        acl_list = ctypes.c_void_p()
        raw_roles = self._expected_raw_roles(expected)
        try:
            status = self.api._sec.SecAccessCopyOwnerAndACL(
                access,
                ctypes.byref(uid),
                ctypes.byref(gid),
                ctypes.byref(owner_type),
                ctypes.byref(acl_list),
            )
            self.api.Error.raise_for_status(status)
            if not acl_list.value:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
            entries: list[dict[str, object]] = []
            count = self.api._found.CFArrayGetCount(acl_list)
            for index in range(count):
                acl = self.api._found.CFArrayGetValueAtIndex(acl_list, index)
                applications = ctypes.c_void_p()
                description = ctypes.c_void_p()
                authorizations: ctypes.c_void_p | int | None = None
                prompt = ctypes.c_uint32()
                try:
                    status = self.api._sec.SecACLCopyContents(
                        acl,
                        ctypes.byref(applications),
                        ctypes.byref(description),
                        ctypes.byref(prompt),
                    )
                    self.api.Error.raise_for_status(status)
                    authorizations = self.api._sec.SecACLCopyAuthorizations(acl)
                    if not applications.value or not description.value or not authorizations:
                        raise SecureStoreError(
                            "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR"
                        )
                    tags = sorted(
                        self._authorization_name(
                            self.api._found.CFArrayGetValueAtIndex(
                                authorizations, auth_index
                            )
                        )
                        for auth_index in range(
                            self.api._found.CFArrayGetCount(authorizations)
                        )
                    )
                    if tags == ["kSecACLAuthorizationAny"]:
                        entry_role = "trusted_application"
                    elif tags == ["kSecACLAuthorizationPartitionID"]:
                        entry_role = "partition_id"
                    else:
                        raise SecureStoreError(
                            "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR"
                        )
                    live_apps: list[dict[str, object]] = []
                    for app_index in range(
                        self.api._found.CFArrayGetCount(applications)
                    ):
                        application = self.api._found.CFArrayGetValueAtIndex(
                            applications, app_index
                        )
                        raw = self._copy_trusted_application_data(application)
                        template = raw_roles.get(raw)
                        if template is None:
                            raise SecureStoreError(
                                "KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR"
                            )
                        current = dict(template)
                        current["raw_data_length"] = len(raw)
                        current["raw_data_sha256"] = hashlib.sha256(raw).hexdigest()
                        live_apps.append(current)
                    entries.append(
                        {
                            "authorizations": tags,
                            "description": self._cfstring_text(description),
                            "entry_role": entry_role,
                            "prompt_selector": prompt.value,
                            "trusted_applications": sorted(
                                live_apps,
                                key=lambda item: (
                                    str(item.get("role")),
                                    str(item.get("raw_data_b64", item.get("raw_data_utf8", ""))),
                                ),
                            ),
                        }
                    )
                finally:
                    self._release(authorizations)
                    self._release(applications)
                    self._release(description)
            payload = {
                "architecture": "arm64",
                "entries": sorted(entries, key=lambda item: str(item["entry_role"])),
                "entry_count": len(entries),
                "owner": {
                    "gid": gid.value,
                    "owner_type": ["kSecUseOnlyUID"] if owner_type.value == 1 else [],
                    "uid": uid.value,
                },
                "platform": "macOS",
                "schema_id": "lulu.agent-cli-keychain-final-acl/v1",
                "schema_revision": 1,
            }
            return (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        except SecureStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed at native boundary
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR") from exc
        finally:
            self._release(acl_list)

    def _canonicalize_item(
        self,
        item: ctypes.c_void_p | int,
        *,
        expected: dict[str, object],
    ) -> bytes:
        access = ctypes.c_void_p()
        try:
            status = self.api._sec.SecKeychainItemCopyAccess(
                item,
                ctypes.byref(access),
            )
            self.api.Error.raise_for_status(status)
            if not access.value:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
            return self._canonicalize_access(access, expected=expected)
        finally:
            self._release(access)

    def _persistent_ref_from_item(self, item: ctypes.c_void_p | int) -> bytes:
        persistent = ctypes.c_void_p()
        try:
            status = self.api._sec.SecKeychainItemCreatePersistentReference(
                item,
                ctypes.byref(persistent),
            )
            self.api.Error.raise_for_status(status)
            if not persistent.value:
                raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
            return self._cfdata_bytes(persistent)
        finally:
            self._release(persistent)

    def _trusted_application_from_path(
        self, path: Path
    ) -> tuple[ctypes.c_void_p, bytes]:
        application = ctypes.c_void_p()
        status = self.api._sec.SecTrustedApplicationCreateFromPath(
            os.fsencode(path),
            ctypes.byref(application),
        )
        self.api.Error.raise_for_status(status)
        if not application.value:
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
        return application, self._copy_trusted_application_data(application)

    def construct_final_acl(
        self, authority: object
    ) -> tuple[bytes, ctypes.c_void_p]:
        """Build and self-check the sole explicit two-entry access object."""

        app_path = Path(str(authority.installation_target))
        cli_path = app_path / "Contents/MacOS/lulu-agent-cli"
        if (
            not app_path.is_absolute()
            or not cli_path.is_file()
            or cli_path.is_symlink()
            or hashlib.sha256(cli_path.read_bytes()).hexdigest()
            != authority.candidate["canonical_cli_sha256"]
        ):
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
        app_ref: ctypes.c_void_p | None = None
        cli_ref: ctypes.c_void_p | None = None
        partition_ref: ctypes.c_void_p | None = None
        empty: ctypes.c_void_p | None = None
        trusted_apps: ctypes.c_void_p | None = None
        partition_apps: ctypes.c_void_p | None = None
        trusted_description: ctypes.c_void_p | None = None
        partition_description: ctypes.c_void_p | None = None
        trusted_authorizations: ctypes.c_void_p | None = None
        partition_authorizations: ctypes.c_void_p | None = None
        trusted_acl = ctypes.c_void_p()
        partition_acl = ctypes.c_void_p()
        access: ctypes.c_void_p | None = None
        error = ctypes.c_void_p()
        try:
            app_ref, app_raw = self._trusted_application_from_path(app_path)
            cli_ref, cli_raw = self._trusted_application_from_path(cli_path)
            partition_ref, _unused = self._trusted_application_from_path(cli_path)
            partition_raw = (
                "cdhash:" + str(authority.candidate["signature"]["cdhash"])
            ).encode("utf-8")
            partition_data = self._bytes_data(partition_raw)
            try:
                status = self.api._sec.SecTrustedApplicationSetData(
                    partition_ref,
                    partition_data,
                )
                self.api.Error.raise_for_status(status)
            finally:
                self._release(partition_data)
            if self._copy_trusted_application_data(partition_ref) != partition_raw:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")

            empty = self._array([])
            access = ctypes.c_void_p(
                self.api._sec.SecAccessCreateWithOwnerAndACL(
                    os.geteuid(),
                    os.getegid(),
                    1,
                    empty,
                    ctypes.byref(error),
                )
            )
            if not access.value or error.value:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
            trusted_apps = self._array([app_ref, cli_ref])
            partition_apps = self._array([partition_ref])
            trusted_description = self._string("Lulu Agent CLI Trusted Test")
            partition_description = self._string(
                "Lulu Agent CLI Trusted Test Partition"
            )
            status = self.api._sec.SecACLCreateWithSimpleContents(
                access,
                trusted_apps,
                trusted_description,
                0,
                ctypes.byref(trusted_acl),
            )
            self.api.Error.raise_for_status(status)
            status = self.api._sec.SecACLCreateWithSimpleContents(
                access,
                partition_apps,
                partition_description,
                0,
                ctypes.byref(partition_acl),
            )
            self.api.Error.raise_for_status(status)
            trusted_authorizations = self._array(
                [self.api.k_("kSecACLAuthorizationAny")]
            )
            partition_authorizations = self._array(
                [self.api.k_("kSecACLAuthorizationPartitionID")]
            )
            self.api.Error.raise_for_status(
                self.api._sec.SecACLUpdateAuthorizations(
                    trusted_acl, trusted_authorizations
                )
            )
            self.api.Error.raise_for_status(
                self.api._sec.SecACLUpdateAuthorizations(
                    partition_acl, partition_authorizations
                )
            )

            def path_record(role: str, path: Path, raw: bytes) -> dict[str, object]:
                return {
                    "canonical_path": str(path),
                    "raw_data_b64": base64.b64encode(raw).decode("ascii"),
                    "raw_data_length": len(raw),
                    "raw_data_sha256": hashlib.sha256(raw).hexdigest(),
                    "role": role,
                }

            expected = {
                "architecture": "arm64",
                "entries": sorted(
                    [
                        {
                            "authorizations": ["kSecACLAuthorizationAny"],
                            "description": "Lulu Agent CLI Trusted Test",
                            "entry_role": "trusted_application",
                            "prompt_selector": 0,
                            "trusted_applications": sorted(
                                [
                                    path_record("candidate_app", app_path, app_raw),
                                    path_record("installed_cli", cli_path, cli_raw),
                                ],
                                key=lambda item: (
                                    str(item["role"]), str(item["raw_data_b64"])
                                ),
                            ),
                        },
                        {
                            "authorizations": [
                                "kSecACLAuthorizationPartitionID"
                            ],
                            "description": "Lulu Agent CLI Trusted Test Partition",
                            "entry_role": "partition_id",
                            "prompt_selector": 0,
                            "trusted_applications": [
                                {
                                    "raw_data_length": len(partition_raw),
                                    "raw_data_sha256": hashlib.sha256(
                                        partition_raw
                                    ).hexdigest(),
                                    "raw_data_utf8": partition_raw.decode("utf-8"),
                                    "role": "cli_cdhash_partition",
                                }
                            ],
                        },
                    ],
                    key=lambda item: str(item["entry_role"]),
                ),
                "entry_count": 2,
                "owner": {
                    "gid": os.getegid(),
                    "owner_type": ["kSecUseOnlyUID"],
                    "uid": os.geteuid(),
                },
                "platform": "macOS",
                "schema_id": "lulu.agent-cli-keychain-final-acl/v1",
                "schema_revision": 1,
            }
            expected_bytes = (
                json.dumps(
                    expected,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if self._canonicalize_access(access, expected=expected) != expected_bytes:
                raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
            return expected_bytes, access
        except SecureStoreError:
            self._release(access)
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed native constructor
            self._release(access)
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE") from exc
        finally:
            self._release(error)
            self._release(trusted_acl)
            self._release(partition_acl)
            self._release(trusted_authorizations)
            self._release(partition_authorizations)
            self._release(trusted_apps)
            self._release(partition_apps)
            self._release(trusted_description)
            self._release(partition_description)
            self._release(empty)
            self._release(app_ref)
            self._release(cli_ref)
            self._release(partition_ref)

    def _owned_result(
        self,
        result: ctypes.c_void_p,
        account: str,
        final_acl_intent_receipt: object,
    ) -> dict[str, object]:
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )

        final_acl = _final_acl_intent_claims(final_acl_intent_receipt)
        dictionary = result.value
        if not dictionary:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        persistent = self.api._found.CFDictionaryGetValue(
            dictionary, self.api.k_("kSecValuePersistentRef")
        )
        generic = self.api._found.CFDictionaryGetValue(
            dictionary, self.api.k_("kSecAttrGeneric")
        )
        service = self.api._found.CFDictionaryGetValue(
            dictionary, self.api.k_("kSecAttrService")
        )
        live_account = self.api._found.CFDictionaryGetValue(
            dictionary, self.api.k_("kSecAttrAccount")
        )
        item_ref = self.api._found.CFDictionaryGetValue(
            dictionary, self.api.k_("kSecValueRef")
        )
        if not persistent or not generic or not service or not live_account or not item_ref:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        persistent_ref = self._cfdata_bytes(persistent)
        envelope = self._cfdata_bytes(generic)
        service_text = self._cfstring_text(service)
        account_text = self._cfstring_text(live_account)
        try:
            payload = json.loads(envelope)
            trusted_cdhash = payload["trusted_cli_cdhash"]
            expected_final_acl = payload["expected_final_acl_sha256"]
        except Exception as exc:
            raise SecureStoreError(
                "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR"
            ) from exc
        if (
            not persistent_ref
            or service_text != SERVICE
            or account_text != account
            or payload.get("service") != service_text
            or payload.get("account") != account_text
            or type(trusted_cdhash) is not str
            or len(trusted_cdhash) not in (40, 64)
            or any(value not in "0123456789abcdef" for value in trusted_cdhash)
        ):
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        try:
            expected_payload = json.loads(final_acl.canonical_bytes)
        except Exception as exc:
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR") from exc
        live_canonical = self._canonicalize_item(
            item_ref,
            expected=expected_payload,
        )
        if (
            expected_final_acl != final_acl.expected_final_acl_sha256
            or live_canonical != final_acl.canonical_bytes
            or self._persistent_ref_from_item(item_ref) != persistent_ref
        ):
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
        attributes_sha256 = hashlib.sha256(
            envelope
            + persistent_ref
            + account_text.encode("utf-8")
            + b"\0"
            + service_text.encode("utf-8")
            + b"\0"
            + final_acl.expected_final_acl_sha256.encode("ascii")
        ).hexdigest()
        return {
            "persistent_ref": persistent_ref,
            "envelope": envelope,
            "attributes_sha256": attributes_sha256,
        }

    def get_password(self, service: str, username: str) -> str | None:
        with _MACOS_READ_LOCK:
            keychain = search = query = None
            data = ctypes.c_void_p()
            try:
                keychain = self._keychain_ref()
                search = self._search_list(keychain)
                query = self.api.create_query(
                    kSecClass=self.api.k_("kSecClassGenericPassword"),
                    kSecMatchLimit=self.api.k_("kSecMatchLimitOne"),
                    kSecMatchSearchList=search,
                    kSecAttrService=service,
                    kSecAttrAccount=username or "",
                    kSecReturnData=True,
                )
                status = self.api.SecItemCopyMatching(query, ctypes.byref(data))
                if status == self.api.error.item_not_found:
                    return None
                self.api.Error.raise_for_status(status)
                return self.api.cfstr_to_str(data)
            finally:
                for ref in (data, query, search, keychain):
                    self._release(ref)

    def set_password(self, service: str, username: str, password: str) -> None:
        keychain = self._keychain_ref()
        match_query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecMatchSearchList=self._search_list(keychain),
            kSecAttrService=service,
            kSecAttrAccount=username or "",
        )
        secret_data = self._secret_data(password)
        update_values = self.api.create_query(kSecValueData=secret_data)
        status = self.api._sec.SecItemUpdate(match_query, update_values)
        if status != self.api.error.item_not_found:
            self.api.Error.raise_for_status(status)
            return

        add_query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecUseKeychain=keychain,
            kSecAttrService=service,
            kSecAttrAccount=username or "",
            kSecValueData=secret_data,
        )
        status = self.api.SecItemAdd(add_query, None)
        self.api.Error.raise_for_status(status)

    def add_password_new(self, service: str, username: str, password: str) -> None:
        keychain = self._keychain_ref()
        add_query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecUseKeychain=keychain,
            kSecAttrService=service,
            kSecAttrAccount=username or "",
            kSecValueData=self._secret_data(password),
        )
        status = self.api.SecItemAdd(add_query, None)
        self.api.Error.raise_for_status(status)

    def delete_password(self, service: str, username: str, *, missing_ok: bool = False) -> None:
        keychain = self._keychain_ref()
        query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecMatchSearchList=self._search_list(keychain),
            kSecAttrService=service,
            kSecAttrAccount=username or "",
        )
        status = self.api.SecItemDelete(query)
        if missing_ok and status == self.api.error.item_not_found:
            return
        self.api.Error.raise_for_status(status)

    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object:
        from src.core.cli_runtime.macos_keychain_acl import (
            _issue_native_final_acl_intent,
        )

        return _issue_native_final_acl_intent(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
            native_boundary=self,
        )

    def create_owned_item(
        self,
        final_acl_intent_receipt: object,
        intent_receipt: object,
        secret_value: str,
    ) -> object:
        """Add one envelope-bound item; never update or delete on duplicate."""

        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )

        intent_claims, register, _inventory, _owned = self._ownership_helpers()
        claims = intent_claims(intent_receipt)
        final_acl = _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        if claims.envelope is None or type(secret_value) is not str:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
        if final_acl.access_ref is None:
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_REQUIRED")
        keychain = self._keychain_ref()
        query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecUseKeychain=keychain,
            kSecAttrService=SERVICE,
            kSecAttrAccount=claims.authority.accounts[claims.account_index],
            kSecAttrGeneric=self._secret_data(claims.envelope.decode("utf-8")),
            kSecValueData=self._secret_data(secret_value),
            kSecAttrAccess=final_acl.access_ref,
            kSecReturnRef=True,
            kSecReturnAttributes=True,
            kSecReturnPersistentRef=True,
            kSecReturnData=False,
        )
        result = ctypes.c_void_p()
        status = self.api.SecItemAdd(query, ctypes.byref(result))
        if status == -25299:
            raise SecureStoreError("VERSIONED_ACCOUNT_CONFLICT")
        self.api.Error.raise_for_status(status)
        # The isolated adapter tests return a Python mapping.  Native CoreFoundation
        # conversion is deliberately kept inside this boundary and never exposes
        # the persistent reference to callers.
        raw = self._owned_result(
            result,
            claims.authority.accounts[claims.account_index],
            final_acl_intent_receipt,
        )
        persistent_ref = raw.get("persistent_ref")
        envelope = raw.get("envelope")
        attributes_sha256 = raw.get("attributes_sha256")
        return register(
            intent_receipt,
            final_acl_intent_receipt,
            persistent_ref=persistent_ref,
            envelope=envelope,
            attributes_sha256=attributes_sha256,
        )

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object:
        intent_claims, _register, register_inventory, _owned = (
            self._ownership_helpers()
        )
        claims = intent_claims(intent_receipt)
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )

        _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        keychain = self._keychain_ref()
        query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecMatchLimit=self.api.k_("kSecMatchLimitOne"),
            kSecMatchSearchList=self._search_list(keychain),
            kSecAttrService=SERVICE,
            kSecAttrAccount=claims.authority.accounts[claims.account_index],
            kSecReturnAttributes=True,
            kSecReturnPersistentRef=True,
            kSecReturnData=False,
            kSecReturnRef=True,
        )
        result = ctypes.c_void_p()
        status = self.api.SecItemCopyMatching(query, ctypes.byref(result))
        if status == self.api.error.item_not_found:
            return register_inventory(
                intent_receipt,
                final_acl_intent_receipt,
                persistent_ref=None,
                envelope=None,
                attributes_sha256=None,
            )
        self.api.Error.raise_for_status(status)
        try:
            raw = self._owned_result(
                result,
                claims.authority.accounts[claims.account_index],
                final_acl_intent_receipt,
            )
        except Exception:  # noqa: BLE001 - live but not fully proven is foreign
            return register_inventory(
                intent_receipt,
                final_acl_intent_receipt,
                persistent_ref=b"unproven-live-item",
                envelope=b"{}\n",
                attributes_sha256="0" * 64,
            )
        return register_inventory(
            intent_receipt,
            final_acl_intent_receipt,
            persistent_ref=raw.get("persistent_ref"),
            envelope=raw.get("envelope"),
            attributes_sha256=raw.get("attributes_sha256"),
        )

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None:
        _intent, _register, _inventory, owned_claims = self._ownership_helpers()
        claims = owned_claims(receipt)
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
        )

        _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        persistent_ref = self._bytes_data(claims.persistent_ref)
        inspect_query = self.api.create_query(
            kSecClass=self.api.k_("kSecClassGenericPassword"),
            kSecMatchLimit=self.api.k_("kSecMatchLimitOne"),
            kSecValuePersistentRef=persistent_ref,
            kSecReturnAttributes=True,
            kSecReturnPersistentRef=True,
            kSecReturnData=False,
            kSecReturnRef=True,
        )
        result = ctypes.c_void_p()
        status = self.api.SecItemCopyMatching(inspect_query, ctypes.byref(result))
        self.api.Error.raise_for_status(status)
        raw = self._owned_result(
            result,
            claims.authority.accounts[claims.account_index],
            final_acl_intent_receipt,
        )
        if (
            raw["persistent_ref"] != claims.persistent_ref
            or raw["envelope"] != claims.envelope
            or raw["attributes_sha256"] != claims.attributes_sha256
        ):
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        match_list = self._array([persistent_ref])
        delete_query = self.api.create_query(kSecMatchItemList=match_list)
        status = self.api.SecItemDelete(delete_query)
        self._release(match_list)
        self.api.Error.raise_for_status(status)


@dataclass
class StrictKeyringStore:
    service: str = SERVICE
    keychain_path: str | None = None

    def _keyring(self):
        try:
            if sys.platform == "darwin":
                return _MacOSExplicitKeyring(self.keychain_path)
            import keyring

            return keyring
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("系统安全存储不可用,请检查钥匙串后重新激活。") from exc

    def get_text(self, key: str) -> str | None:
        kr = self._keyring()
        try:
            return kr.get_password(self.service, key)
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("读取授权安全存储失败,请重新激活。") from exc

    def set_text(self, key: str, value: str) -> None:
        kr = self._keyring()
        try:
            kr.set_password(self.service, key, value)
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("写入授权安全存储失败。请确认登录钥匙串已解锁后重试。") from exc

    def write_new(self, key: str, value: str) -> None:
        kr = self._keyring()
        try:
            writer = getattr(kr, "add_password_new")
            writer(self.service, key, value)
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("创建安装安全存储失败。已有账户不会被覆盖。") from exc

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        kr = self._keyring()
        try:
            kr.delete_password(self.service, key, missing_ok=missing_ok)
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("清理授权安全存储失败。请确认登录钥匙串已解锁后重试。") from exc

    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object:
        try:
            return self._keyring().prepare_final_acl_intent(
                locator_receipt,
                plan_receipt,
            )
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason_code", "KEYCHAIN_EXPLICIT_ACCESS_UNAVAILABLE")
            raise SecureStoreError(str(reason)) from exc

    def create_owned_item(
        self,
        final_acl_intent_receipt: object,
        intent_receipt: object,
        secret_value: str,
    ) -> object:
        try:
            return self._keyring().create_owned_item(
                final_acl_intent_receipt,
                intent_receipt,
                secret_value,
            )
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason_code", "INSTALLATION_RECOVERY_REQUIRED")
            raise SecureStoreError(str(reason)) from exc

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object:
        try:
            return self._keyring().inspect_exact_item(
                final_acl_intent_receipt,
                intent_receipt,
            )
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason_code", "KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
            raise SecureStoreError(str(reason)) from exc

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None:
        try:
            self._keyring().delete_verified_item(final_acl_intent_receipt, receipt)
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason_code", "KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
            raise SecureStoreError(str(reason)) from exc

    def load_cache(self) -> LicenseCache | None:
        raw = self.get_text(CACHE_KEY)
        if not raw:
            return None
        try:
            return LicenseCache.from_json(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("授权缓存已损坏,请重新激活。") from exc

    def save_cache(self, cache: LicenseCache) -> None:
        self.set_text(CACHE_KEY, json.dumps(cache.to_json(), ensure_ascii=False, separators=(",", ":")))

    def clear_cache(self) -> None:
        if self.get_text(CACHE_KEY) is not None:
            self.delete_text(CACHE_KEY)

    def load_pending_deactivation(self) -> PendingDeactivation | None:
        raw = self.get_text(PENDING_DEACTIVATION_KEY)
        if not raw:
            return None
        try:
            return PendingDeactivation.from_json(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("停用恢复状态已损坏,Lulu 已保持锁定,请联系授权方。") from exc

    def save_pending_deactivation(self, pending: PendingDeactivation) -> None:
        self.set_text(
            PENDING_DEACTIVATION_KEY,
            json.dumps(pending.to_json(), ensure_ascii=False, separators=(",", ":")),
        )

    def clear_pending_deactivation(self) -> None:
        if self.get_text(PENDING_DEACTIVATION_KEY) is not None:
            self.delete_text(PENDING_DEACTIVATION_KEY)

    def get_or_create_device_salt(self, factory) -> str:
        salt = self.get_text(DEVICE_SALT_KEY)
        if salt:
            return salt
        salt = factory()
        self.set_text(DEVICE_SALT_KEY, salt)
        return salt


class MemorySecureStore:
    """Strict-store compatible test double. Never used by default app startup."""

    def __init__(self, *, fail: bool = False, state: dict[str, object] | None = None):
        self._state = state if state is not None else {"values": {}, "owned": {}}
        self.values: dict[str, str] = self._state["values"]  # type: ignore[assignment]
        self._owned: dict[str, dict[str, object]] = self._state["owned"]  # type: ignore[assignment]
        self.fail = fail
        self.create_count = 0
        self.delete_count = 0
        self.inventory_count = 0
        self.secret_read_count = 0

    def _maybe_fail(self):
        if self.fail:
            raise SecureStoreError("系统安全存储不可用,请检查钥匙串后重新激活。")

    def get_text(self, key: str) -> str | None:
        self._maybe_fail()
        self.secret_read_count += 1
        return self.values.get(key)

    def set_text(self, key: str, value: str) -> None:
        self._maybe_fail()
        self.values[key] = value

    def write_new(self, key: str, value: str) -> None:
        self._maybe_fail()
        if key in self.values:
            raise SecureStoreError("安装安全存储账户已存在。")
        self.values[key] = value

    def delete_text(self, key: str, *, missing_ok: bool = False) -> None:
        self._maybe_fail()
        if key not in self.values and not missing_ok:
            raise SecureStoreError("安装安全存储账户不存在。")
        self.values.pop(key, None)

    def prepare_final_acl_intent(
        self, locator_receipt: object, plan_receipt: object
    ) -> object:
        from src.core.cli_runtime.macos_keychain_acl import (
            _issue_memory_final_acl_intent,
        )

        return _issue_memory_final_acl_intent(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )

    def create_owned_item(
        self,
        final_acl_intent_receipt: object,
        intent_receipt: object,
        secret_value: str,
    ) -> object:
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
            _ownership_intent_claims,
            _register_created_owned_item,
        )

        self._maybe_fail()
        claims = _ownership_intent_claims(intent_receipt)
        final_acl = _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        account = claims.authority.accounts[claims.account_index]
        if account in self.values:
            raise SecureStoreError("VERSIONED_ACCOUNT_CONFLICT")
        if claims.envelope is None or type(secret_value) is not str:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_REQUIRED")
        persistent_ref = secrets.token_bytes(32)
        final_acl_sha256 = json.loads(claims.envelope)[
            "expected_final_acl_sha256"
        ]
        if final_acl_sha256 != final_acl.expected_final_acl_sha256:
            raise SecureStoreError("KEYCHAIN_EXPLICIT_ACCESS_INTEGRITY_ERROR")
        attrs = hashlib.sha256(
            claims.envelope
            + persistent_ref
            + account.encode("utf-8")
            + b"\0Lulu\0"
            + final_acl_sha256.encode("ascii")
        ).hexdigest()
        self.values[account] = secret_value
        self._owned[account] = {
            "envelope": claims.envelope,
            "persistent_ref": persistent_ref,
            "attributes_sha256": attrs,
            "final_acl_sha256": final_acl_sha256,
        }
        self.create_count += 1
        return _register_created_owned_item(
            intent_receipt,
            final_acl_intent_receipt,
            persistent_ref=persistent_ref,
            envelope=claims.envelope,
            attributes_sha256=attrs,
        )

    def inspect_exact_item(
        self, final_acl_intent_receipt: object, intent_receipt: object
    ) -> object:
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
            _ownership_intent_claims,
            _register_inventory_result,
        )

        self._maybe_fail()
        claims = _ownership_intent_claims(intent_receipt)
        _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        account = claims.authority.accounts[claims.account_index]
        self.inventory_count += 1
        if account not in self.values:
            return _register_inventory_result(
                intent_receipt,
                final_acl_intent_receipt,
                persistent_ref=None,
                envelope=None,
                attributes_sha256=None,
            )
        item = self._owned.get(account)
        if item is None:
            return _register_inventory_result(
                intent_receipt,
                final_acl_intent_receipt,
                persistent_ref=b"foreign",
                envelope=b"{}\n",
                attributes_sha256="0" * 64,
            )
        envelope = item.get("envelope")
        persistent_ref = item.get("persistent_ref")
        expected_attributes = (
            hashlib.sha256(
                envelope
                + persistent_ref
                + account.encode("utf-8")
                + b"\0Lulu\0"
                + str(item.get("final_acl_sha256", "")).encode("ascii")
            ).hexdigest()
            if type(envelope) is bytes and type(persistent_ref) is bytes
            else ""
        )
        attributes = item.get("attributes_sha256")
        return _register_inventory_result(
            intent_receipt,
            final_acl_intent_receipt,
            persistent_ref=persistent_ref,
            envelope=envelope,
            attributes_sha256=(
                attributes if attributes == expected_attributes else "invalid"
            ),
        )

    def delete_verified_item(
        self, final_acl_intent_receipt: object, receipt: object
    ) -> None:
        from src.core.cli_runtime.installation_ticket import (
            _final_acl_intent_claims,
            _owned_item_claims,
        )

        self._maybe_fail()
        claims = _owned_item_claims(receipt)
        final_acl = _final_acl_intent_claims(
            final_acl_intent_receipt,
            authority=claims.authority,
        )
        selected = None
        for account, item in self._owned.items():
            if item.get("persistent_ref") == claims.persistent_ref:
                selected = account
                break
        if selected is None:
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        item = self._owned[selected]
        if (
            item.get("envelope") != claims.envelope
            or item.get("attributes_sha256") != claims.attributes_sha256
            or claims.authority.accounts[claims.account_index] != selected
            or item.get("final_acl_sha256")
            != final_acl.expected_final_acl_sha256
        ):
            raise SecureStoreError("KEYCHAIN_OWNERSHIP_PROOF_INTEGRITY_ERROR")
        del self._owned[selected]
        self.values.pop(selected, None)
        self.delete_count += 1

    def load_cache(self) -> LicenseCache | None:
        raw = self.get_text(CACHE_KEY)
        return LicenseCache.from_json(json.loads(raw)) if raw else None

    def save_cache(self, cache: LicenseCache) -> None:
        self.set_text(CACHE_KEY, json.dumps(cache.to_json(), ensure_ascii=False, separators=(",", ":")))

    def clear_cache(self) -> None:
        if self.get_text(CACHE_KEY) is not None:
            self.delete_text(CACHE_KEY)

    def load_pending_deactivation(self) -> PendingDeactivation | None:
        raw = self.get_text(PENDING_DEACTIVATION_KEY)
        if not raw:
            return None
        try:
            return PendingDeactivation.from_json(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            raise SecureStoreError("停用恢复状态已损坏,Lulu 已保持锁定,请联系授权方。") from exc

    def save_pending_deactivation(self, pending: PendingDeactivation) -> None:
        self.set_text(
            PENDING_DEACTIVATION_KEY,
            json.dumps(pending.to_json(), ensure_ascii=False, separators=(",", ":")),
        )

    def clear_pending_deactivation(self) -> None:
        if self.get_text(PENDING_DEACTIVATION_KEY) is not None:
            self.delete_text(PENDING_DEACTIVATION_KEY)

    def get_or_create_device_salt(self, factory) -> str:
        salt = self.get_text(DEVICE_SALT_KEY)
        if salt:
            return salt
        salt = factory()
        self.set_text(DEVICE_SALT_KEY, salt)
        return salt

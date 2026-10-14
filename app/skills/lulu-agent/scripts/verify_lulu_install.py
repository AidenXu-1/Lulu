#!/usr/bin/env python3
"""Verify one explicitly selected Lulu installation without executing it."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
from collections.abc import Callable, Sequence


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
_CANONICAL_CLI = Path("Contents/MacOS/lulu-agent-cli")
_PRODUCT_MANIFEST = Path("Contents/Resources/agent_product_manifest.json")
_BUILD_MANIFEST = Path("Contents/Resources/build_profile.json")


class TrustAnchorError(RuntimeError):
    """The Lulu install cannot be used as a canonical CLI trust anchor."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _SealedInstallPolicy:
    """Pins loaded by trusted package/Core code, never from this script's argv."""

    allowed_app_bundles: tuple[Path, ...]
    expected_bundle_id: str
    expected_designated_requirement: str
    expected_team_id: str
    expected_product_manifest_sha256: str
    expected_build_manifest_sha256: str


def _assert_no_symlink_ancestry(path: Path, *, reason_code: str) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise TrustAnchorError(reason_code)
        except OSError as exc:
            raise TrustAnchorError("TRUST_ANCHOR_INVALID") from exc
        if current.parent == current:
            return
        current = current.parent


def _owned_path(path: Path, *, kind: str, reason_code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TrustAnchorError(reason_code) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise TrustAnchorError(reason_code)
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise TrustAnchorError(reason_code)
    if kind == "regular" and not stat.S_ISREG(metadata.st_mode):
        raise TrustAnchorError(reason_code)
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise TrustAnchorError(reason_code)
    return metadata


def _read_regular(path: Path, *, reason_code: str) -> bytes:
    _assert_no_symlink_ancestry(path, reason_code=reason_code)
    expected = _owned_path(path, kind="regular", reason_code=reason_code)
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            actual = os.fstat(descriptor)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise TrustAnchorError(reason_code)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
        finally:
            os.close(descriptor)
    except TrustAnchorError:
        raise
    except OSError as exc:
        raise TrustAnchorError(reason_code) from exc
    return content


def _read_plist(path: Path) -> dict[str, object]:
    try:
        value = plistlib.loads(_read_regular(path, reason_code="INFO_PLIST_INVALID"))
    except TrustAnchorError:
        raise
    except Exception as exc:
        raise TrustAnchorError("INFO_PLIST_INVALID") from exc
    if not isinstance(value, dict):
        raise TrustAnchorError("INFO_PLIST_INVALID")
    return value


def _run_codesign(
    runner: Callable[[list[str]], object], argv: list[str]
) -> tuple[str, str]:
    try:
        result = runner(argv)
        returncode = int(getattr(result, "returncode"))
        stdout = str(getattr(result, "stdout", ""))
        stderr = str(getattr(result, "stderr", ""))
    except Exception as exc:
        raise TrustAnchorError("CODESIGN_VERIFIER_UNAVAILABLE") from exc
    if returncode != 0:
        raise TrustAnchorError("CODESIGN_VERIFICATION_FAILED")
    return stdout, stderr


def _line_value(text: str, prefix: str, *, reason_code: str) -> str:
    values = [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise TrustAnchorError(reason_code)
    return values[0]


def _validated_app(app_bundle: Path, allowed: Sequence[Path]) -> Path:
    app = Path(app_bundle)
    if not app.is_absolute() or ".." in app.parts:
        raise TrustAnchorError("APP_PATH_NOT_ALLOWED")
    if app.is_symlink():
        raise TrustAnchorError("APP_SYMLINK_FORBIDDEN")
    _assert_no_symlink_ancestry(app, reason_code="APP_SYMLINK_FORBIDDEN")
    _owned_path(app, kind="directory", reason_code="APP_PATH_NOT_ALLOWED")
    allowed_paths = {str(Path(item)) for item in allowed}
    if str(app) not in allowed_paths:
        raise TrustAnchorError("APP_PATH_NOT_ALLOWED")
    return app


def _verify_lulu_install_with_sealed_policy(
    *,
    app_bundle: Path,
    sealed_policy: _SealedInstallPolicy,
    path_candidate: Path | None = None,
    runner: Callable[[list[str]], object],
) -> dict[str, object]:
    """Verify an App only against pins from an opaque sealed policy DTO."""

    if type(sealed_policy) is not _SealedInstallPolicy:
        raise TrustAnchorError("TRUST_BINDING_INVALID")
    if (
        not sealed_policy.expected_bundle_id
        or not sealed_policy.expected_designated_requirement
        or not sealed_policy.allowed_app_bundles
    ):
        raise TrustAnchorError("TRUST_BINDING_INVALID")
    if not _TEAM_ID.fullmatch(sealed_policy.expected_team_id):
        raise TrustAnchorError("TRUST_BINDING_INVALID")
    for digest in (
        sealed_policy.expected_product_manifest_sha256,
        sealed_policy.expected_build_manifest_sha256,
    ):
        if not _SHA256.fullmatch(digest):
            raise TrustAnchorError("TRUST_BINDING_INVALID")
    app = _validated_app(Path(app_bundle), sealed_policy.allowed_app_bundles)

    _run_codesign(
        runner,
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
    )
    _, requirement_output = _run_codesign(
        runner, ["/usr/bin/codesign", "-dr", "-", str(app)]
    )
    designated_requirement = _line_value(
        requirement_output,
        "designated => ",
        reason_code="DESIGNATED_REQUIREMENT_INVALID",
    )
    if designated_requirement != sealed_policy.expected_designated_requirement:
        raise TrustAnchorError("DESIGNATED_REQUIREMENT_MISMATCH")
    _, identity_output = _run_codesign(
        runner,
        ["/usr/bin/codesign", "--display", "--verbose=4", str(app)],
    )
    bundle_id = _line_value(
        identity_output,
        "Identifier=",
        reason_code="SIGNING_IDENTITY_INVALID",
    )
    team_id = _line_value(
        identity_output,
        "TeamIdentifier=",
        reason_code="SIGNING_IDENTITY_INVALID",
    )
    if bundle_id != sealed_policy.expected_bundle_id:
        raise TrustAnchorError("BUNDLE_ID_MISMATCH")
    if team_id != sealed_policy.expected_team_id:
        raise TrustAnchorError("TEAM_ID_MISMATCH")

    cli = app / _CANONICAL_CLI
    cli_metadata = _owned_path(cli, kind="regular", reason_code="CANONICAL_CLI_INVALID")
    if not (cli_metadata.st_mode & stat.S_IXUSR):
        raise TrustAnchorError("CANONICAL_CLI_INVALID")
    _assert_no_symlink_ancestry(cli, reason_code="CANONICAL_CLI_INVALID")

    info = _read_plist(app / "Contents/Info.plist")
    if info.get("CFBundleIdentifier") != sealed_policy.expected_bundle_id:
        raise TrustAnchorError("BUNDLE_ID_MISMATCH")

    product_bytes = _read_regular(
        app / _PRODUCT_MANIFEST, reason_code="PRODUCT_MANIFEST_INVALID"
    )
    signed_manifest_sha256 = hashlib.sha256(product_bytes).hexdigest()
    if signed_manifest_sha256 != sealed_policy.expected_product_manifest_sha256:
        raise TrustAnchorError("SIGNED_MANIFEST_MISMATCH")
    try:
        product = json.loads(product_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustAnchorError("PRODUCT_MANIFEST_INVALID") from exc
    if not isinstance(product, dict):
        raise TrustAnchorError("PRODUCT_MANIFEST_INVALID")

    build_bytes = _read_regular(
        app / _BUILD_MANIFEST,
        reason_code="BUILD_MANIFEST_INVALID",
    )
    if hashlib.sha256(build_bytes).hexdigest() != sealed_policy.expected_build_manifest_sha256:
        raise TrustAnchorError("BUILD_MANIFEST_MISMATCH")
    try:
        build = json.loads(build_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustAnchorError("BUILD_MANIFEST_INVALID") from exc
    if not isinstance(build, dict):
        raise TrustAnchorError("BUILD_MANIFEST_INVALID")
    if not _SOURCE_ID.fullmatch(str(build.get("source_commit", ""))):
        raise TrustAnchorError("BUILD_MANIFEST_INVALID")
    if not _SOURCE_ID.fullmatch(str(build.get("source_tree", ""))):
        raise TrustAnchorError("BUILD_MANIFEST_INVALID")
    if not _SHA256.fullmatch(str(build.get("product_source_sha256", ""))):
        raise TrustAnchorError("BUILD_MANIFEST_INVALID")
    if build.get("bundle_id") != sealed_policy.expected_bundle_id:
        raise TrustAnchorError("BUILD_MANIFEST_MISMATCH")
    canonical_cli_sha256 = build.get("canonical_cli_sha256")
    if not isinstance(canonical_cli_sha256, str) or not _SHA256.fullmatch(
        canonical_cli_sha256
    ):
        raise TrustAnchorError("BUILD_MANIFEST_INVALID")
    actual_cli_sha256 = hashlib.sha256(
        _read_regular(cli, reason_code="CANONICAL_CLI_INVALID")
    ).hexdigest()
    if actual_cli_sha256 != canonical_cli_sha256:
        raise TrustAnchorError("CANONICAL_CLI_HASH_MISMATCH")
    expected_product = {
        "schema_id": "lulu.agent-product-manifest/v1",
        "schema_revision": 1,
        "installation_identity_scope": "shared_lulu_installation",
        "product_version": build.get("version"),
        "product_build": build.get("build"),
        "cli_protocol_version": "1.0.0",
    }
    for field, expected in expected_product.items():
        if product.get(field) != expected:
            raise TrustAnchorError("PRODUCT_BUILD_BINDING_MISMATCH")
    adapters = product.get("adapters")
    if not isinstance(adapters, dict):
        raise TrustAnchorError("PRODUCT_MANIFEST_INVALID")
    agent_cli = adapters.get("agent_cli")
    if not isinstance(agent_cli, dict) or agent_cli.get("executable") != str(_CANONICAL_CLI):
        raise TrustAnchorError("PRODUCT_MANIFEST_INVALID")

    convenience_path: str | None = None
    convenience_equivalent = False
    if path_candidate is not None:
        candidate = Path(path_candidate)
        convenience_path = str(candidate)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise TrustAnchorError("CONVENIENCE_PATH_INVALID")
        try:
            resolved = candidate.resolve(strict=True)
            candidate_metadata = resolved.stat()
        except OSError as exc:
            raise TrustAnchorError("CONVENIENCE_PATH_INVALID") from exc
        convenience_equivalent = (
            resolved == cli.resolve(strict=True)
            and (candidate_metadata.st_dev, candidate_metadata.st_ino)
            == (cli_metadata.st_dev, cli_metadata.st_ino)
        )
        if not convenience_equivalent:
            raise TrustAnchorError("CONVENIENCE_PATH_NOT_EQUIVALENT")

    return {
        "schema_id": "lulu.install-verification/v1",
        "verified": True,
        "canonical_app": str(app),
        "canonical_cli": str(cli),
        "convenience_path": convenience_path,
        "convenience_path_equivalent": convenience_equivalent,
        "bundle_id": bundle_id,
        "designated_requirement": designated_requirement,
        "team_id": team_id,
        "signed_manifest_sha256": signed_manifest_sha256,
        "product_version": product["product_version"],
        "product_build": product["product_build"],
        "source_commit": build["source_commit"],
        "source_tree": build["source_tree"],
        "installation_identity_scope": product["installation_identity_scope"],
        "cli_protocol_version": product["cli_protocol_version"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    result = {
        "schema_id": "lulu.install-verification/v1",
        "verified": False,
        "reason_code": "TRUST_EVIDENCE_UNAVAILABLE",
    }
    json.dump(result, sys.stdout, separators=(",", ":"), sort_keys=True)
    sys.stdout.write("\n")
    return 60


if __name__ == "__main__":
    raise SystemExit(main())

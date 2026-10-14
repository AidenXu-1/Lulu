#!/usr/bin/env python3
"""Fixed GUI-only post-build authority for Lulu Trusted Test.

Public functions accept only opaque, module-issued receipts. Paths, identity,
measurements, and verifiers are derived inside this physical module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
from threading import RLock


_TASK_ID = "TASK-20260823-696D55"
_FIXED_IDENTITY = {
    "architecture": "arm64",
    "build": "20260823.4",
    "bundle_id": "com.lulu.transcribe.trusted-test",
    "channel": "trusted-test",
    "dmg_filename": "Lulu-0.3.2-20260823.4-trusted-test-arm64.dmg",
    "package_surface": "gui_only/v1",
    "product_name": "Lulu Trusted Test",
    "profile": "trusted_test",
    "version": "0.3.2",
}
_DIAGNOSTIC_IDENTITY = {
    "diagnostic_contract_id": "TASK-20260823-A0EC73/gui-not-visible-stage/v1",
    "diagnostic_only": True,
    "promotion_allowed": False,
    "retained_install_allowed": False,
}
_IDENTITY = _FIXED_IDENTITY
_APP_ROOT = Path(__file__).resolve().parents[1]
_FIXED_DIST_ROOT = _APP_ROOT / "dist/phase10-trusted-test-20260823.4"
_FIXED_APP = _FIXED_DIST_ROOT / "Lulu-Trusted-Test.app"
_FIXED_CANDIDATE_EVIDENCE = _FIXED_DIST_ROOT / "candidate-evidence.json"
_FIXED_ROOT = _APP_ROOT / (
    "release/post-build-authority/Lulu-0.3.2-20260823.4-trusted-test-arm64"
)
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CDHASH = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_FIELD_PARTS = (
    "agent_cli", "canonical_cli", "cli_cdhash", "installation_ticket", "keychain",
)
_FORBIDDEN_PATH_MARKERS = (
    "lulu-agent-cli", "run_agent_cli", "agent_product_manifest.json",
    "capability_catalog.json", "skills/lulu-agent", "gui_host_runtime_hook",
    "src/agent_cli", "src/core/cli_runtime",
)
_ABSENT_ORACLE = {
    "agent_cli_executable": True,
    "agent_skill": True,
    "agent_manifest_and_catalog": True,
    "agent_runtime_hook": True,
    "agent_python_runtime": True,
    "keychain_ticket_runtime": True,
}


class PostBuildAuthorityError(RuntimeError):
    """The fixed GUI-only authority failed closed."""


_SEALS = [object() for _ in range(8)]


class _OpaqueReceipt:
    __slots__ = ("__token",)
    _seal: object

    def __init__(self, seal: object, token: str) -> None:
        if seal is not type(self)._seal or type(token) is not str:
            raise TypeError(f"{type(self).__name__} required")
        object.__setattr__(self, "_OpaqueReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("authority receipt is immutable")

    def __reduce__(self):
        raise TypeError("authority receipt is not serializable")


class _BuildCompletedReceipt(_OpaqueReceipt): _seal = _SEALS[0]
class _ProducedCarrierReceipt(_OpaqueReceipt): _seal = _SEALS[1]
class _SecurityAppReviewReceipt(_OpaqueReceipt): _seal = _SEALS[2]
class _TestAppReviewReceipt(_OpaqueReceipt): _seal = _SEALS[3]
class _FinalizedAuthorityArtifactReceipt(_OpaqueReceipt): _seal = _SEALS[4]
class _SecurityArtifactReviewReceipt(_OpaqueReceipt): _seal = _SEALS[5]
class _TestArtifactReviewReceipt(_OpaqueReceipt): _seal = _SEALS[6]
class _PostBuildAuthorityReceipt(_OpaqueReceipt): _seal = _SEALS[7]


@dataclass(frozen=True, slots=True)
class _AppMeasurement:
    app_content_tree_sha256: str
    app_profile_tree_sha256: str
    gui_executable_sha256: str
    gui_cdhash: str
    candidate_evidence_sha256: str
    source_commit: str
    source_tree: str
    product_source_sha256: str
    signature: dict[str, object]


@dataclass(frozen=True, slots=True)
class _FileClaims:
    payload: dict[str, object]
    raw: bytes
    sha256: str
    path: str
    parent_sha256: str = ""


_LOCK = RLock()
_REGISTRIES: list[dict[str, object]] = [{} for _ in range(8)]


def _token(receipt: object, expected: type[_OpaqueReceipt]) -> str:
    if type(receipt) is not expected:
        raise TypeError(f"{expected.__name__} required")
    try:
        token = object.__getattribute__(receipt, "_OpaqueReceipt__token")
    except Exception as exc:  # noqa: BLE001
        raise TypeError(f"{expected.__name__} required") from exc
    if type(token) is not str:
        raise TypeError(f"{expected.__name__} required")
    return token


def _register(index: int, value: object, receipt_type: type[_OpaqueReceipt]) -> object:
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _REGISTRIES[index][token] = value
    return receipt_type(_SEALS[index], token)


def _claims(index: int, receipt: object, receipt_type: type[_OpaqueReceipt]) -> object:
    token = _token(receipt, receipt_type)
    with _LOCK:
        value = _REGISTRIES[index].get(token)
    if value is None:
        raise PostBuildAuthorityError("OPAQUE_RECEIPT_INVALID")
    return value


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_regular(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PostBuildAuthorityError(f"{label}_MISSING") from exc
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
        raise PostBuildAuthorityError(f"{label}_UNSAFE")
    return path.read_bytes()


def _read_json(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    raw = _read_regular(path, label)
    try:
        value = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise PostBuildAuthorityError(f"{label}_INVALID") from exc
    if type(value) is not dict:
        raise PostBuildAuthorityError(f"{label}_INVALID")
    return value, raw


def _read_product_source(path: Path) -> tuple[dict[str, object], bytes]:
    raw = _read_regular(path, "PRODUCT_SOURCE")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PostBuildAuthorityError("PRODUCT_SOURCE_INVALID")

    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, nested in pairs:
            if key in value:
                raise ValueError("duplicate JSON object key")
            value[key] = nested
        return value

    def reject_non_finite_number(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number: {value}")
        return parsed

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite_number,
            parse_float=parse_finite_float,
        )
    except Exception as exc:  # noqa: BLE001
        raise PostBuildAuthorityError("PRODUCT_SOURCE_INVALID") from exc
    if type(value) is not dict:
        raise PostBuildAuthorityError("PRODUCT_SOURCE_INVALID")
    return value, raw


def _product_source_semantic_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha(canonical)


def _write_create_only(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022):
        raise PostBuildAuthorityError("AUTHORITY_ROOT_UNSAFE")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise PostBuildAuthorityError("AUTHORITY_OUTPUT_EXISTS") from exc
    except OSError as exc:
        raise PostBuildAuthorityError("AUTHORITY_OUTPUT_CREATE_FAILED") from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_digest(root: Path) -> str:
    root_meta = root.lstat()
    if not stat.S_ISDIR(root_meta.st_mode) or stat.S_ISLNK(root_meta.st_mode):
        raise PostBuildAuthorityError("APP_UNSAFE")
    records: list[bytes] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            records.append(f"D\0{relative}\0{mode:o}\n".encode())
        elif stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            raw = path.read_bytes()
            records.append(f"F\0{relative}\0{mode:o}\0{len(raw)}\0{_sha(raw)}\n".encode())
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            try:
                path.resolve(strict=True).relative_to(root.resolve(strict=True))
            except ValueError as exc:
                raise PostBuildAuthorityError("APP_SYMLINK_ESCAPE") from exc
            records.append(f"L\0{relative}\0{mode:o}\0{target}\n".encode())
        else:
            raise PostBuildAuthorityError("APP_UNSUPPORTED_MEMBER")
    return _sha(b"".join(sorted(records)))


def _assert_no_forbidden_fields(value: object) -> None:
    if type(value) is dict:
        for key, nested in value.items():
            if key == "forbidden_surface_absent":
                if nested != _ABSENT_ORACLE:
                    raise PostBuildAuthorityError("GUI_ONLY_ABSENT_ORACLE_INVALID")
                continue
            if any(part in str(key).lower() for part in _FORBIDDEN_FIELD_PARTS):
                raise PostBuildAuthorityError("GUI_ONLY_EVIDENCE_HAS_CLI_FIELD")
            _assert_no_forbidden_fields(nested)
    elif type(value) is list:
        for nested in value:
            _assert_no_forbidden_fields(nested)


def _has_exact_diagnostic_identity(payload: object) -> bool:
    return type(payload) is dict and all(
        payload.get(key) == value
        for key, value in _DIAGNOSTIC_IDENTITY.items()
    )


def _signature_facts(app: Path) -> dict[str, object]:
    verify = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True, check=False)
    display = subprocess.run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)], capture_output=True, text=True, check=False)
    text = f"{display.stdout}\n{display.stderr}"
    match = re.search(r"(?m)^CDHash=([0-9a-fA-F]{40})$", text)
    if (verify.returncode != 0 or display.returncode != 0 or "Signature=adhoc" not in text
            or "Authority=" in text or "TeamIdentifier=not set" not in text or match is None):
        raise PostBuildAuthorityError("AD_HOC_STRICT_SIGNATURE_FAILED")
    return {"codesign_verified_deep_strict": True, "kind": "ad_hoc", "team_id": None, "gui_cdhash": match.group(1).lower()}


def _measure_app() -> _AppMeasurement:
    contents = _FIXED_APP / "Contents"
    resources = contents / "Resources"
    gui = contents / "MacOS/Lulu-Trusted-Test"
    gui_raw = _read_regular(gui, "GUI_EXECUTABLE")
    if not gui.lstat().st_mode & stat.S_IXUSR:
        raise PostBuildAuthorityError("GUI_EXECUTABLE_NOT_EXECUTABLE")
    info_raw = _read_regular(contents / "Info.plist", "INFO_PLIST")
    try:
        info = plistlib.loads(info_raw)
    except Exception as exc:  # noqa: BLE001
        raise PostBuildAuthorityError("INFO_PLIST_INVALID") from exc
    expected_info = {
        "CFBundleExecutable": "Lulu-Trusted-Test",
        "CFBundleIdentifier": _FIXED_IDENTITY["bundle_id"],
        "CFBundleShortVersionString": _FIXED_IDENTITY["version"],
        "CFBundleVersion": _FIXED_IDENTITY["build"],
        "LuluBuildIdentity": _FIXED_IDENTITY["channel"],
        "LuluBuildProfile": _FIXED_IDENTITY["profile"],
    }
    if type(info) is not dict or any(info.get(key) != value for key, value in expected_info.items()):
        raise PostBuildAuthorityError("INFO_PLIST_IDENTITY_MISMATCH")
    if info.get("LSBackgroundOnly") not in (None, False):
        raise PostBuildAuthorityError("GUI_MARKED_BACKGROUND_ONLY")
    profile, profile_raw = _read_json(resources / "build_profile.json", "BUILD_PROFILE")
    if (profile.get("profile") != "trusted_test" or profile.get("version") != "0.3.2"
            or profile.get("build") != "20260823.4"
            or profile.get("bundle_id") != "com.lulu.transcribe.trusted-test"
            or profile.get("app_name") != "Lulu Trusted Test"
            or profile.get("package_surface") != "gui_only/v1"
            or not _SOURCE_ID.fullmatch(str(profile.get("source_commit", "")))
            or not _SOURCE_ID.fullmatch(str(profile.get("source_tree", "")))
            or not _SHA256.fullmatch(str(profile.get("product_source_sha256", "")))):
        raise PostBuildAuthorityError("BUILD_PROFILE_IDENTITY_MISMATCH")
    product_source, product_source_raw = _read_product_source(
        resources / "product_source.json"
    )
    if not secrets.compare_digest(
        _product_source_semantic_sha256(product_source),
        str(profile["product_source_sha256"]),
    ):
        raise PostBuildAuthorityError("PRODUCT_SOURCE_MISMATCH")
    evidence, evidence_raw = _read_json(_FIXED_CANDIDATE_EVIDENCE, "CANDIDATE_EVIDENCE")
    _assert_no_forbidden_fields(evidence)
    expected_candidate = {
        "version": "0.3.2", "build": "20260823.4", "channel": "trusted-test",
        "release_state": "trusted_test_non_release",
        "bundle_id": "com.lulu.transcribe.trusted-test", "product": "Lulu Trusted Test",
    }
    if (evidence.get("schema_id") != "lulu.phase10-gui-only-candidate-evidence/v1"
            or evidence.get("package_surface") != "gui_only/v1"
            or evidence.get("gui_entry") != "Contents/MacOS/Lulu-Trusted-Test"
            or evidence.get("candidate_identity") != expected_candidate
            or not _has_exact_diagnostic_identity(evidence)
            or evidence.get("forbidden_surface_absent") != _ABSENT_ORACLE
            or evidence.get("gui_executable_sha256") != _sha(gui_raw)
            or evidence.get("source_commit") != profile["source_commit"]
            or evidence.get("source_tree") != profile["source_tree"]):
        raise PostBuildAuthorityError("CANDIDATE_EVIDENCE_MISMATCH")
    for path in _FIXED_APP.rglob("*"):
        relative = path.relative_to(_FIXED_APP).as_posix().lower()
        if any(marker in relative for marker in _FORBIDDEN_PATH_MARKERS):
            raise PostBuildAuthorityError("GUI_ONLY_FORBIDDEN_SURFACE_PRESENT")
    signature = _signature_facts(_FIXED_APP)
    cdhash = str(signature.get("gui_cdhash", ""))
    if not _CDHASH.fullmatch(cdhash):
        raise PostBuildAuthorityError("GUI_CDHASH_INVALID")
    content_tree = _tree_digest(_FIXED_APP)
    if evidence.get("bundle_tree_sha256") != content_tree:
        raise PostBuildAuthorityError("CANDIDATE_CONTENT_TREE_MISMATCH")
    profile_tree = _sha(b"".join(sorted((
        b"info\0" + _sha(info_raw).encode() + b"\n",
        b"profile\0" + _sha(profile_raw).encode() + b"\n",
        b"product-source\0" + _sha(product_source_raw).encode() + b"\n",
        b"gui\0" + _sha(gui_raw).encode() + b"\n",
        b"candidate-evidence\0" + _sha(evidence_raw).encode() + b"\n",
    ))))
    return _AppMeasurement(content_tree, profile_tree, _sha(gui_raw), cdhash,
        _sha(evidence_raw), str(profile["source_commit"]), str(profile["source_tree"]),
        str(profile["product_source_sha256"]), signature)


def _measure_producer_app() -> _AppMeasurement: return _measure_app()
def _measure_security_app() -> _AppMeasurement: return _measure_app()
def _measure_test_app() -> _AppMeasurement: return _measure_app()


def issue_build_completed_receipt() -> object:
    return _register(0, _measure_producer_app(), _BuildCompletedReceipt)


def produce_carrier(build_receipt: object) -> object:
    expected = _claims(0, build_receipt, _BuildCompletedReceipt)
    measured = _measure_producer_app()
    if measured != expected:
        raise PostBuildAuthorityError("APP_DRIFT_BEFORE_CARRIER")
    payload = {"schema": "lulu.phase10-gui-only-carrier/v1", "task_id": _TASK_ID,
        "identity": dict(_FIXED_IDENTITY), "app_path": str(_FIXED_APP),
        "measurement": asdict(measured), "forbidden_surface_absent": dict(_ABSENT_ORACLE),
        "contains_secret_material": False, **_DIAGNOSTIC_IDENTITY}
    _assert_no_forbidden_fields(payload)
    raw = _canonical_bytes(payload)
    path = _FIXED_ROOT / "gui-only-carrier.json"
    _write_create_only(path, raw)
    return _register(1, _FileClaims(payload, raw, _sha(raw), str(path)), _ProducedCarrierReceipt)


def _file_claims(index: int, receipt: object, receipt_type: type[_OpaqueReceipt], label: str) -> _FileClaims:
    claims = _claims(index, receipt, receipt_type)
    if type(claims) is not _FileClaims or _read_regular(Path(claims.path), label) != claims.raw:
        raise PostBuildAuthorityError(f"{label}_RECEIPT_INVALID")
    return claims


def review_carrier_security(carrier_receipt: object) -> object:
    claims = _file_claims(1, carrier_receipt, _ProducedCarrierReceipt, "CARRIER")
    if asdict(_measure_security_app()) != claims.payload["measurement"]:
        raise PostBuildAuthorityError("SECURITY_APP_REVIEW_MISMATCH")
    return _register(2, claims.sha256, _SecurityAppReviewReceipt)


def review_carrier_test(carrier_receipt: object) -> object:
    claims = _file_claims(1, carrier_receipt, _ProducedCarrierReceipt, "CARRIER")
    if asdict(_measure_test_app()) != claims.payload["measurement"]:
        raise PostBuildAuthorityError("TEST_APP_REVIEW_MISMATCH")
    return _register(3, claims.sha256, _TestAppReviewReceipt)


def finalize_authority_artifact(carrier_receipt: object, security_review: object, test_review: object) -> object:
    carrier = _file_claims(1, carrier_receipt, _ProducedCarrierReceipt, "CARRIER")
    if (_claims(2, security_review, _SecurityAppReviewReceipt) != carrier.sha256
            or _claims(3, test_review, _TestAppReviewReceipt) != carrier.sha256):
        raise PostBuildAuthorityError("APP_REVIEW_RECEIPT_MISMATCH")
    payload = {"schema": "lulu.phase10-gui-only-authority-artifact/v1", "task_id": _TASK_ID,
        "identity": dict(_FIXED_IDENTITY), "carrier_sha256": carrier.sha256,
        "measurement": carrier.payload["measurement"],
        "forbidden_surface_absent": dict(_ABSENT_ORACLE), "contains_secret_material": False,
        **_DIAGNOSTIC_IDENTITY}
    _assert_no_forbidden_fields(payload)
    raw = _canonical_bytes(payload)
    path = _FIXED_ROOT / "gui-only-authority-artifact.json"
    _write_create_only(path, raw)
    return _register(4, _FileClaims(payload, raw, _sha(raw), str(path), carrier.sha256), _FinalizedAuthorityArtifactReceipt)


def review_authority_artifact_security(artifact_receipt: object) -> object:
    artifact = _file_claims(4, artifact_receipt, _FinalizedAuthorityArtifactReceipt, "AUTHORITY_ARTIFACT")
    if asdict(_measure_security_app()) != artifact.payload["measurement"]:
        raise PostBuildAuthorityError("SECURITY_ARTIFACT_REVIEW_MISMATCH")
    return _register(5, artifact.sha256, _SecurityArtifactReviewReceipt)


def review_authority_artifact_test(artifact_receipt: object) -> object:
    artifact = _file_claims(4, artifact_receipt, _FinalizedAuthorityArtifactReceipt, "AUTHORITY_ARTIFACT")
    if asdict(_measure_test_app()) != artifact.payload["measurement"]:
        raise PostBuildAuthorityError("TEST_ARTIFACT_REVIEW_MISMATCH")
    return _register(6, artifact.sha256, _TestArtifactReviewReceipt)


def issue_post_build_authority(artifact_receipt: object, security_review: object, test_review: object) -> object:
    artifact = _file_claims(4, artifact_receipt, _FinalizedAuthorityArtifactReceipt, "AUTHORITY_ARTIFACT")
    if (_claims(5, security_review, _SecurityArtifactReviewReceipt) != artifact.sha256
            or _claims(6, test_review, _TestArtifactReviewReceipt) != artifact.sha256):
        raise PostBuildAuthorityError("ARTIFACT_REVIEW_RECEIPT_MISMATCH")
    measured = _measure_producer_app()
    if asdict(measured) != artifact.payload["measurement"]:
        raise PostBuildAuthorityError("APP_DRIFT_BEFORE_AUTHORITY")
    payload = {"schema": "lulu.phase10-gui-only-post-build-authority/v1", "task_id": _TASK_ID,
        "identity": dict(_FIXED_IDENTITY), "authority_artifact_sha256": artifact.sha256,
        "carrier_sha256": artifact.parent_sha256, "measurement": asdict(measured),
        "forbidden_surface_absent": dict(_ABSENT_ORACLE), "contains_secret_material": False,
        **_DIAGNOSTIC_IDENTITY}
    _assert_no_forbidden_fields(payload)
    raw = _canonical_bytes(payload)
    path = _FIXED_ROOT / "post-build-authority.json"
    _write_create_only(path, raw)
    return _register(7, _FileClaims(payload, raw, _sha(raw), str(path)), _PostBuildAuthorityReceipt)


def load_fixed_post_build_authority() -> object:
    path = _FIXED_ROOT / "post-build-authority.json"
    payload, raw = _read_json(path, "POST_BUILD_AUTHORITY")
    _assert_no_forbidden_fields(payload)
    artifact, artifact_raw = _read_json(
        _FIXED_ROOT / "gui-only-authority-artifact.json",
        "AUTHORITY_ARTIFACT",
    )
    carrier, carrier_raw = _read_json(
        _FIXED_ROOT / "gui-only-carrier.json",
        "CARRIER",
    )
    measured = asdict(_measure_producer_app())
    if (payload.get("schema") != "lulu.phase10-gui-only-post-build-authority/v1"
            or payload.get("identity") != _FIXED_IDENTITY
            or not _has_exact_diagnostic_identity(payload)
            or payload.get("measurement") != measured
            or payload.get("forbidden_surface_absent") != _ABSENT_ORACLE
            or payload.get("authority_artifact_sha256") != _sha(artifact_raw)
            or payload.get("carrier_sha256") != _sha(carrier_raw)
            or artifact.get("schema") != "lulu.phase10-gui-only-authority-artifact/v1"
            or artifact.get("identity") != _FIXED_IDENTITY
            or not _has_exact_diagnostic_identity(artifact)
            or artifact.get("measurement") != measured
            or artifact.get("carrier_sha256") != _sha(carrier_raw)
            or carrier.get("schema") != "lulu.phase10-gui-only-carrier/v1"
            or carrier.get("identity") != _FIXED_IDENTITY
            or not _has_exact_diagnostic_identity(carrier)
            or carrier.get("measurement") != measured):
        raise PostBuildAuthorityError("POST_BUILD_AUTHORITY_INTEGRITY_ERROR")
    return _register(7, _FileClaims(payload, raw, _sha(raw), str(path)), _PostBuildAuthorityReceipt)


def post_build_public_fingerprint(receipt: object) -> dict[str, object]:
    claims = _file_claims(7, receipt, _PostBuildAuthorityReceipt, "POST_BUILD_AUTHORITY")
    measured = claims.payload["measurement"]
    return {"schema": "lulu.phase10-gui-only-post-build-public/v1",
        "package_surface": "gui_only/v1", "authority_path": claims.path,
        "authority_sha256": claims.sha256,
        "authority_artifact_sha256": claims.payload["authority_artifact_sha256"],
        "carrier_sha256": claims.payload["carrier_sha256"],
        "app_content_tree_sha256": measured["app_content_tree_sha256"],
        "app_profile_tree_sha256": measured["app_profile_tree_sha256"],
        "gui_executable_sha256": measured["gui_executable_sha256"],
        "gui_cdhash": measured["gui_cdhash"],
        "candidate_evidence_sha256": measured["candidate_evidence_sha256"],
        "contains_secret_material": False, **_DIAGNOSTIC_IDENTITY}


def _fixed_candidate_bundle_for_installer(receipt: object) -> Path:
    _file_claims(7, receipt, _PostBuildAuthorityReceipt, "POST_BUILD_AUTHORITY")
    return _FIXED_APP


__all__ = [
    "PostBuildAuthorityError", "finalize_authority_artifact",
    "issue_build_completed_receipt", "issue_post_build_authority",
    "load_fixed_post_build_authority", "post_build_public_fingerprint",
    "produce_carrier", "review_authority_artifact_security",
    "review_authority_artifact_test", "review_carrier_security",
    "review_carrier_test",
]

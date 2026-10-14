"""Build the only protected runtime accepted by the packaged Agent CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import sys
from typing import Protocol
from uuid import uuid4

from ...agent_cli.local_transcription import LocalTranscriptionCliRuntime
from ..auth_guard import OpenProductAccessGuard
from ..job_ledger.service import JobControlService
from ..job_ledger.store import JobLedgerStore
from ..link_transcription_application import (
    LinkTranscriptionApplicationService,
    LinkTranscriptionError,
    LinkTranscriptionJobBackend,
)
from .link_host_bridge import _build_production_link_host_broker
from ..storage_layout import SharedProductStorageLayout
from ..transcription_application import (
    LocalFileTranscriptionApplicationService,
    LocalModelReadinessProbe,
    LocalTranscriptionPipelineRunner,
    probe_local_model_readiness,
    run_local_transcription_pipeline_adapter,
)
from ..transcription_job_application import (
    LocalTranscriptionJobApplication,
    LocalTranscriptionJobError,
)
from ..transcription_result_store import TranscriptionResultStore
from .approval import _stored_local_ticket_approval_is_valid
from .contracts import CliRuntimeTrustError, CodeSignatureEvidenceV1
from .keychain import (
    KeychainBoundary,
    KeyringJobIntegrityAnchorStore,
    VersionedCredentialError,
    load_versioned_installation_ticket,
    load_installation_ticket,
)
from .installation_ticket import (
    _application_support_root_from_authority,
    _reload_fixed_installed_authority,
    validate_authority,
)


_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMON_TICKET_FIELDS = frozenset(
    {
        "trust_profile",
        "bundle_id",
        "source_commit",
        "source_tree",
        "canonical_cli_sha256",
        "installation_id",
        "signature_kind",
        "certificate_sha256",
        "cdhash",
        "designated_requirement",
        "team_id",
    }
)
_LOCAL_TICKET_FIELDS = _COMMON_TICKET_FIELDS | frozenset(
    {
        "schema_id",
        "schema_revision",
        "approval_issuer_source",
        "approval_nonce",
        "approval_issued_at",
        "approval_expires_at",
        "approval_receipt_sha256",
    }
)
_PRODUCTION_TICKET_FIELDS = _COMMON_TICKET_FIELDS | frozenset(
    {
        "schema_id",
        "schema_revision",
        "created_by",
        "user_authorized",
    }
)


class SignatureVerifier(Protocol):
    def verify(
        self,
        *,
        app_bundle: Path,
        cli_path: Path,
    ) -> CodeSignatureEvidenceV1: ...


class _VerifiedInstallationApplication(LocalTranscriptionJobApplication):
    def _assert_verified_trusted_test_runtime(self) -> str:
        verification = self._runtime_verification
        installation_id = verification.get("installation_id")
        local_ticket = (
            verification.get("profile") == "trusted_test"
            and verification.get("trust_profile") == "local_trusted_test"
            and verification.get("signature_kind") == "ad_hoc"
            and verification.get("installation_ticket_verified") is True
        )
        production_signature = (
            verification.get("profile") == "external_candidate"
            and verification.get("trust_profile") == "production"
            and verification.get("signature_kind") == "developer_id"
            and bool(verification.get("team_id"))
            and verification.get("installation_ticket_verified") is True
        )
        measured_circle = (
            verification.get("profile") == "external_candidate"
            and verification.get("trust_profile") == "circle_internal"
            and verification.get("measured_component_manifest_verified") is True
            and verification.get("ownership_receipt_verified") is True
        )
        if (
            verification.get("verified") is not True
            or not (local_ticket or production_signature or measured_circle)
            or type(installation_id) is not str
            or not installation_id
        ):
            raise LocalTranscriptionJobError(
                "CAPABILITY_UNAVAILABLE", "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE"
            )
        return installation_id


def _trust_error(reason_code: str) -> CliRuntimeTrustError:
    return CliRuntimeTrustError("TRUST_ANCHOR_INVALID", reason_code)


def _signature_digest(signature: CodeSignatureEvidenceV1) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "signature_kind": signature.signature_kind,
                "certificate_sha256": signature.certificate_sha256,
                "cdhash": signature.cdhash,
                "designated_requirement": signature.designated_requirement,
                "team_id": signature.team_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _owned_regular_sha256(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _trust_error("CANONICAL_CLI_UNAVAILABLE") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise _trust_error("CANONICAL_CLI_NOT_OWNER_CONTROLLED")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _trust_error("CANONICAL_CLI_UNAVAILABLE") from exc
    return digest.hexdigest()


def _validate_paths(
    resource_root: Path,
    cli_path: Path,
) -> Path:
    root = Path(resource_root)
    cli = Path(cli_path)
    if not root.is_absolute() or not cli.is_absolute():
        raise _trust_error("INSTALLATION_PATH_NOT_ABSOLUTE")
    if root.name != "Resources" or root.parent.name != "Contents":
        raise _trust_error("RESOURCE_ROOT_OUTSIDE_APP")
    app_bundle = root.parent.parent
    if app_bundle.suffix != ".app":
        raise _trust_error("RESOURCE_ROOT_OUTSIDE_APP")
    if cli != app_bundle / "Contents/MacOS/lulu-agent-cli":
        raise _trust_error("CANONICAL_CLI_PATH_MISMATCH")
    return app_bundle


def _validate_bundle_identity(
    app_bundle: Path,
    *,
    expected_bundle_id: object,
) -> None:
    info_path = app_bundle / "Contents/Info.plist"
    try:
        metadata = info_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise _trust_error("BUNDLE_INFO_NOT_OWNER_CONTROLLED")
        with info_path.open("rb") as handle:
            payload = plistlib.load(handle)
    except CliRuntimeTrustError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _trust_error("BUNDLE_INFO_INVALID") from exc
    if (
        type(payload) is not dict
        or payload.get("CFBundleIdentifier") != expected_bundle_id
        or payload.get("CFBundlePackageType") != "APPL"
    ):
        raise _trust_error("BUNDLE_ID_MISMATCH")


def _validate_ticket(
    ticket: dict[str, object],
    *,
    shared: Mapping[str, object],
    cli_sha256: str,
    signature: CodeSignatureEvidenceV1,
) -> tuple[str, str]:
    ticket_fields = frozenset(ticket)
    if ticket_fields not in {_LOCAL_TICKET_FIELDS, _PRODUCTION_TICKET_FIELDS}:
        raise _trust_error("CLI_INSTALLATION_TICKET_INVALID")
    installation_id = ticket.get("installation_id")
    values_match = (
        ticket.get("bundle_id") == shared.get("bundle_id")
        and ticket.get("source_commit") == shared.get("source_commit")
        and ticket.get("source_tree") == shared.get("source_tree")
        and ticket.get("canonical_cli_sha256") == cli_sha256
        and ticket.get("signature_kind") == signature.signature_kind
        and ticket.get("certificate_sha256") == signature.certificate_sha256
        and ticket.get("cdhash") == signature.cdhash
        and ticket.get("designated_requirement")
        == signature.designated_requirement
        and ticket.get("team_id") == signature.team_id
        and type(installation_id) is str
        and 8 <= len(installation_id) <= 200
        and _SOURCE_ID.fullmatch(str(ticket.get("source_commit", "")))
        and _SOURCE_ID.fullmatch(str(ticket.get("source_tree", "")))
        and _SHA256.fullmatch(str(cli_sha256))
    )
    if not values_match:
        raise _trust_error("CLI_INSTALLATION_BINDING_MISMATCH")
    profile = shared.get("profile")
    trust_profile = ticket.get("trust_profile")
    local_trusted_test = (
        ticket_fields == _LOCAL_TICKET_FIELDS
        and ticket.get("schema_id") == "lulu.agent-cli-installation-ticket/v2"
        and ticket.get("schema_revision") == 2
        and profile == "trusted_test"
        and trust_profile == "local_trusted_test"
        and signature.signature_kind == "ad_hoc"
        and _stored_local_ticket_approval_is_valid(ticket)
        and not signature.team_id
    )
    production = (
        ticket_fields == _PRODUCTION_TICKET_FIELDS
        and ticket.get("schema_id") == "lulu.agent-cli-installation-ticket/v1"
        and ticket.get("schema_revision") == 1
        and profile == "external_candidate"
        and trust_profile == "production"
        and signature.signature_kind == "developer_id"
        and bool(signature.team_id)
        and ticket.get("created_by") == "verified_release_install"
        and ticket.get("user_authorized") is True
    )
    if not (local_trusted_test or production):
        raise _trust_error("SIGNATURE_PROFILE_NOT_ALLOWED")
    return str(installation_id), str(trust_profile)


def _validate_versioned_ticket(
    ticket: dict[str, object],
    *,
    shared: Mapping[str, object],
    cli_sha256: str,
    signature: CodeSignatureEvidenceV1,
    locator_receipt: object,
    plan_receipt: object,
) -> tuple[str, str]:
    authority = validate_authority(
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    candidate = authority.candidate
    expected_signature = candidate["signature"]
    if (
        ticket.get("schema_id") != "lulu.agent-cli-installation-ticket/v3"
        or ticket.get("schema_revision") != 3
        or ticket.get("installation_id") != authority.installation_id
        or ticket.get("installation_target") != authority.installation_target
        or ticket.get("accounts") != list(authority.accounts)
        or ticket.get("locator_sha256") != authority.locator_sha256
        or ticket.get("plan_sha256") != authority.plan_sha256
        or ticket.get("target_binding_sha256") != authority.target_binding_sha256
        or ticket.get("target_role") != authority.target_role
        or ticket.get("carrier_sha256") != authority.carrier_sha256
        or ticket.get("authority_artifact_sha256")
        != authority.authority_artifact_sha256
        or ticket.get("review_receipts") != authority.review_receipts
        or ticket.get("source_identity") != authority.source_identity
        or ticket.get("authority_identity") != authority.authority_identity
        or ticket.get("bundle_id") != shared.get("bundle_id")
        or ticket.get("product_version") != shared.get("product_version")
        or ticket.get("build") != shared.get("product_build")
        or ticket.get("product_name") != authority.target_identity["product_name"]
        or ticket.get("profile") != authority.target_identity["profile"]
        or ticket.get("channel") != authority.target_identity["channel"]
        or authority.source_identity.get("actual_build_source_commit")
        != shared.get("source_commit")
        or authority.source_identity.get("actual_build_source_tree")
        != shared.get("source_tree")
        or ticket.get("canonical_cli_sha256") != cli_sha256
        or ticket.get("signature") != expected_signature
        or signature.signature_kind != expected_signature["signature_kind"]
        or signature.certificate_sha256 != expected_signature["certificate_sha256"]
        or signature.cdhash != expected_signature["cdhash"]
        or signature.designated_requirement
        != expected_signature["designated_requirement"]
        or signature.team_id != expected_signature["team_id"]
        or shared.get("profile") != "trusted_test"
        or shared.get("profile") != authority.target_identity["profile"]
        or shared.get("bundle_id") != authority.target_identity["bundle_id"]
        or authority.candidate.get("target_role") != authority.target_role
        or authority.candidate.get("installation_target")
        != authority.installation_target
    ):
        raise _trust_error("CANDIDATE_IDENTITY_MISMATCH")
    return authority.installation_id, "local_trusted_test"


def _build_runtime_from_ticket(
    *,
    product_identity: Mapping[str, object],
    resource_root: Path,
    cli_path: Path,
    application_support_root: Path,
    keychain: KeychainBoundary,
    signature_verifier: SignatureVerifier,
    locator_receipt: object | None = None,
    plan_receipt: object | None = None,
    now: Callable[[], datetime] | None = None,
    pipeline_runner: LocalTranscriptionPipelineRunner = run_local_transcription_pipeline_adapter,
    model_readiness_probe: LocalModelReadinessProbe = probe_local_model_readiness,
    id_factory: Callable[[str], str] | None = None,
    link_cover_workflow: object | None = None,
) -> LocalTranscriptionCliRuntime:
    """Verify every installation binding before touching shared business state."""

    shared = product_identity.get("shared")
    if product_identity.get("adapter_id") != "agent_cli" or not isinstance(
        shared, Mapping
    ):
        raise _trust_error("PRODUCT_IDENTITY_INVALID")
    authority = None
    if locator_receipt is None and plan_receipt is None:
        pass
    elif locator_receipt is None or plan_receipt is None:
        raise _trust_error("AUTHORITATIVE_PLAN_REQUIRED")
    else:
        try:
            authority = validate_authority(
                locator_receipt=locator_receipt,
                plan_receipt=plan_receipt,
            )
        except Exception as exc:  # noqa: BLE001 - exact receipt boundary
            reason = getattr(exc, "reason_code", "AUTHORITATIVE_PLAN_REQUIRED")
            raise _trust_error(str(reason)) from exc
        resource_root = Path(authority.installation_target) / "Contents/Resources"
        cli_path = (
            Path(authority.installation_target)
            / "Contents/MacOS/lulu-agent-cli"
        )
    app_bundle = _validate_paths(Path(resource_root), Path(cli_path))
    _validate_bundle_identity(
        app_bundle,
        expected_bundle_id=shared.get("bundle_id"),
    )
    cli_sha256 = _owned_regular_sha256(Path(cli_path))
    try:
        signature = signature_verifier.verify(
            app_bundle=app_bundle,
            cli_path=Path(cli_path),
        )
    except CliRuntimeTrustError:
        raise
    except Exception as exc:  # noqa: BLE001 - injected codesign boundary
        raise _trust_error("CODE_SIGNATURE_VERIFICATION_FAILED") from exc
    if not isinstance(signature, CodeSignatureEvidenceV1):
        raise _trust_error("CODE_SIGNATURE_EVIDENCE_INVALID")
    if locator_receipt is None and plan_receipt is None:
        ticket = load_installation_ticket(keychain)
        installation_id, trust_profile = _validate_ticket(
            ticket,
            shared=shared,
            cli_sha256=cli_sha256,
            signature=signature,
        )
    else:
        try:
            ticket = load_versioned_installation_ticket(
                locator_receipt=locator_receipt,
                plan_receipt=plan_receipt,
                keychain=keychain,
            )
        except VersionedCredentialError as exc:
            code = (
                "SECURE_STORE_UNAVAILABLE"
                if exc.reason_code == "SECURE_STORE_UNAVAILABLE"
                else "TRUST_ANCHOR_INVALID"
            )
            raise CliRuntimeTrustError(code, exc.reason_code) from exc
        installation_id, trust_profile = _validate_versioned_ticket(
            ticket,
            shared=shared,
            cli_sha256=cli_sha256,
            signature=signature,
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    if shared.get("profile") != "trusted_test" or trust_profile != "local_trusted_test":
        raise CliRuntimeTrustError(
            "CAPABILITY_UNAVAILABLE", "TRUSTED_TEST_RUNTIME_ONLY"
        )

    id_source = id_factory or (lambda kind: f"{kind}-{uuid4().hex}")
    authorization_guard = OpenProductAccessGuard()
    try:
        authorization_guard.assert_local_file_transcription_allowed(
            {"capability_id": "TR-01", "operation": "runtime_bootstrap"}
        )
    except Exception as exc:  # converted at the public runtime seam
        if isinstance(exc, CliRuntimeTrustError):
            raise
        failure = getattr(exc, "failure", None)
        code = getattr(failure, "code", "AUTH_REQUIRED")
        raise CliRuntimeTrustError(code, "FRESH_CACHED_AUTH_REJECTED") from exc

    layout = SharedProductStorageLayout.from_application_support_root(
        Path(application_support_root)
    )
    for directory in (layout.product_root, layout.jobs):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700, follow_symlinks=False)
    anchor = KeyringJobIntegrityAnchorStore(keychain, installation_id)
    store = JobLedgerStore(layout.jobs, anchor)
    result_store = TranscriptionResultStore(layout)
    service = LocalFileTranscriptionApplicationService(
        authorization_guard,
        pipeline_runner=pipeline_runner,
        model_readiness_probe=model_readiness_probe,
    )
    timestamp = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    link_service = LinkTranscriptionApplicationService(
        installation_id=installation_id,
        job_backend=LinkTranscriptionJobBackend(store, now=timestamp),
        result_writer=result_store,
        cover_workflow=link_cover_workflow,
    )
    application = _VerifiedInstallationApplication(
        runtime_verification={
            "profile": shared.get("profile"),
            "trust_profile": trust_profile,
            "verified": True,
            "signature_kind": signature.signature_kind,
            "team_id": signature.team_id,
            "installation_ticket_verified": True,
            "installation_id": installation_id,
            "bundle_id": shared.get("bundle_id"),
            "source_commit": shared.get("source_commit"),
            "source_tree": shared.get("source_tree"),
            "canonical_cli_sha256": cli_sha256,
            "cdhash": signature.cdhash,
            "designated_requirement": signature.designated_requirement,
            "target_binding_sha256": (
                authority.target_binding_sha256 if authority is not None else None
            ),
            "target_role": authority.target_role if authority is not None else None,
            "installation_target": (
                authority.installation_target if authority is not None else None
            ),
        },
        authorization_guard=authorization_guard,
        model_readiness_probe=model_readiness_probe,
        transcription_service=service,
        job_store=store,
        result_store=result_store,
        id_factory=id_source,
        now=timestamp,
    )
    return LocalTranscriptionCliRuntime(
        application,
        job_control=JobControlService(store, enforce_fencing=True),
        now=timestamp,
        link_transcription_service=link_service,
        id_factory=id_source,
    )


def _build_measured_core_runtime(
    *,
    product_identity: Mapping[str, object],
    resource_root: Path,
    core_path: Path,
    application_support_root: Path,
    ownership_receipt_path: Path,
    action_receipt_path: Path,
    keychain: KeychainBoundary,
    core_generation: str | None = None,
    now: Callable[[], datetime] | None = None,
    pipeline_runner: LocalTranscriptionPipelineRunner = run_local_transcription_pipeline_adapter,
    model_readiness_probe: LocalModelReadinessProbe = probe_local_model_readiness,
    id_factory: Callable[[str], str] | None = None,
) -> LocalTranscriptionCliRuntime:
    """Build the existing local runtime after component and ownership verification."""

    from ..agent_entry.verification_cache import verify_runtime_component_manifest as verify_component_manifest
    from ..agent_entry.installed_materials import component_manifest_path
    from ..agent_entry.ownership import verify_ownership_receipt, verify_ready_action_receipt

    shared = product_identity.get("shared")
    root = Path(resource_root)
    app_bundle = root.parent.parent
    expected_core = app_bundle / "Contents/MacOS/lulu-agent-core"
    if (
        product_identity.get("adapter_id") != "agent_cli"
        or not isinstance(shared, Mapping)
        or shared.get("profile") != "external_candidate"
        or root != app_bundle / "Contents/Resources"
        or Path(core_path) != expected_core
    ):
        raise _trust_error("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    verification = verify_component_manifest(
        app_bundle,
        component_manifest_path(app_bundle, state_root=Path(ownership_receipt_path).parent),
    )
    ownership = verify_ownership_receipt(ownership_receipt_path, key_store=keychain)
    verify_ready_action_receipt(action_receipt_path, ownership=ownership)
    if (
        ownership.get("component_manifest_sha256") != verification.get("component_manifest_sha256")
        or ownership.get("app_component_set_digest") != verification.get("app_component_set_digest")
        or ownership.get("candidate_generation") != verification.get("candidate_generation")
        or shared.get("product_version") != verification.get("product_version")
        or shared.get("product_build") != verification.get("product_build")
        or shared.get("source_commit") != verification.get("source_revision")
    ):
        raise _trust_error("AGENT_ENTRY_VERSION_MISMATCH")
    installation_id = str(ownership["installation_id"])
    id_source = id_factory or (lambda kind: f"{kind}-{uuid4().hex}")
    authorization_guard = OpenProductAccessGuard()
    try:
        authorization_guard.assert_local_file_transcription_allowed(
            {"capability_id": "TR-01", "operation": "runtime_bootstrap"}
        )
    except Exception as exc:
        failure = getattr(exc, "failure", None)
        code = getattr(failure, "code", "AUTH_REQUIRED")
        raise CliRuntimeTrustError(code, "FRESH_CACHED_AUTH_REJECTED") from exc
    layout = SharedProductStorageLayout.from_application_support_root(Path(application_support_root))
    from ..job_ledger.storage_binding import prepare_core_task_store, _private_directory
    from ..job_ledger.contracts import JobLedgerError
    from ..license.store import SecureStoreError
    historical_jobs = []
    historical_context = None
    task_root = layout.product_root
    try:
        if core_generation is not None:
            if (type(core_generation) is not str or not re.fullmatch(r'[a-f0-9]{40}', core_generation)
                    or getattr(keychain, 'core_generation', None) != core_generation):
                raise SecureStoreError('JOB_CORE_IDENTITY_REQUIRED')
            from ..agent_entry.task_handover import FILENAME, open_handover
            from ..agent_entry.installed_materials import MATERIALS_DIR, _private_json
            handover = _private_json(Path(ownership_receipt_path).parent / MATERIALS_DIR / FILENAME)
            historical_jobs = open_handover(handover, keys=keychain, owner=ownership,
                                            product_root=layout.product_root)
            historical_context = handover['body'].get('gui_context')
            if getattr(keychain, 'storage_scheme', None) == 'local-integrity-v1':
                from ..local_integrity import TASK_DIRECTORY
                task_root = layout.product_root / TASK_DIRECTORY
                from ..agent_entry.local_task_storage import read_local_history
                historical_jobs, historical_context = read_local_history(keychain)
            else:
                generations = layout.product_root / 'task-generations'
                _private_directory(generations)
                task_root = generations / core_generation
            _private_directory(task_root)
        # Authenticated generation handover leaves the old shared ticket intact.
        # It cannot authorize or block a different generation's private ledger.
        store = prepare_core_task_store(task_root, installation_id, keychain,
                                       legacy_storage=core_generation is None)
        if getattr(keychain, 'storage_scheme', None) == 'local-integrity-v1':
            from ..agent_entry.local_task_storage import without_imported_history
            historical_jobs, historical_context = without_imported_history(
                (historical_jobs, historical_context), {j.job_id for j in store.list_jobs()})
    except JobLedgerError as exc:
        raise CliRuntimeTrustError("INTEGRITY_ERROR", exc.reason_code) from exc
    except SecureStoreError as exc:
        # Expose only closed status codes, never backend text or secret values.
        reason = "JOB_CORE_SECURE_WRITER_NOT_VERIFIED" if str(exc) == "JOB_CORE_SECURE_WRITER_NOT_VERIFIED" else "JOB_CORE_SECURE_STORAGE_UNAVAILABLE"
        raise CliRuntimeTrustError("INTEGRITY_ERROR", reason) from exc
    result_store = TranscriptionResultStore(layout)
    service = LocalFileTranscriptionApplicationService(
        authorization_guard,
        pipeline_runner=pipeline_runner,
        model_readiness_probe=model_readiness_probe,
    )
    timestamp = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    application = _VerifiedInstallationApplication(
        runtime_verification={
            "profile": "external_candidate",
            "trust_profile": "circle_internal",
            "verified": True,
            "measured_component_manifest_verified": True,
            "ownership_receipt_verified": True,
            "installation_id": installation_id,
            "bundle_id": shared.get("bundle_id"),
            "source_commit": shared.get("source_commit"),
            "source_tree": shared.get("source_tree"),
            "component_manifest_sha256": verification.get("component_manifest_sha256"),
            "app_component_set_digest": verification.get("app_component_set_digest"),
            "candidate_generation": 9,
        },
        authorization_guard=authorization_guard,
        model_readiness_probe=model_readiness_probe,
        transcription_service=service,
        job_store=store,
        result_store=result_store,
        id_factory=id_source,
        now=timestamp,
    )
    from ..local_synthesis_job_application import LocalSynthesisJobApplication
    synthesis = LocalSynthesisJobApplication(application=application, job_store=store,
        result_store=result_store, now=timestamp, id_factory=id_source)
    from ..link_extraction_jobs import LinkExtractionJobs
    extraction = LinkExtractionJobs(application=application, job_store=store,
        result_store=result_store, now=timestamp, id_factory=id_source)
    return LocalTranscriptionCliRuntime(
        application,
        job_control=JobControlService(store, enforce_fencing=True),
        now=timestamp,
        link_transcription_service=None,
        id_factory=id_source,
        historical_jobs=historical_jobs,
        historical_context=historical_context,
        local_synthesis_service=synthesis,
        link_extraction_service=extraction,
    )


def build_measured_core_runtime(
    *,
    product_identity: Mapping[str, object],
    resource_root: Path,
) -> LocalTranscriptionCliRuntime:
    """Production wrapper; callable only by the frozen canonical Core."""

    if not bool(getattr(sys, "frozen", False)):
        raise _trust_error("FROZEN_INSTALLATION_REQUIRED")
    from .core_task_keychain import CoreTaskKeyringStore

    app_bundle = Path(resource_root).parent.parent
    core_path = app_bundle / "Contents/MacOS/lulu-agent-core"
    from ..agent_entry.standalone_paths import installed_app, state_for_app
    installed_app(app_bundle)
    if Path(sys.executable) != core_path:
        raise _trust_error("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    support = Path.home() / "Library/Application Support/Lulu"
    from .core_task_native import measured_core
    generation = measured_core(core_path)
    from ..agent_entry.standalone_source import uses_local_storage
    if uses_local_storage(app_bundle):
        from ..local_integrity import product_local_store
        keys = product_local_store(support)
    else:
        keys = CoreTaskKeyringStore()
    keys.core_generation = generation
    from ..agent_entry.attempted_cores import current_core_identities
    if getattr(keys, 'storage_scheme', None) is None:
        keys.authorized_core_identities = current_core_identities(app_bundle,
            product_root=support, generation=generation, keys=keys)
    return _build_measured_core_runtime(
        product_identity=product_identity,
        resource_root=resource_root,
        core_path=core_path,
        application_support_root=support.parent,
        ownership_receipt_path=state_for_app(app_bundle) / "ownership.json",
        action_receipt_path=state_for_app(app_bundle) / "current.json",
        keychain=keys,
        core_generation=generation,
    )


def _build_verified_trusted_test_runtime(
    *,
    product_identity: Mapping[str, object],
    application_support_root: Path,
    keychain: KeychainBoundary,
    signature_verifier: SignatureVerifier,
    locator_receipt: object,
    plan_receipt: object,
    now: Callable[[], datetime] | None = None,
    pipeline_runner: LocalTranscriptionPipelineRunner = run_local_transcription_pipeline_adapter,
    model_readiness_probe: LocalModelReadinessProbe = probe_local_model_readiness,
    id_factory: Callable[[str], str] | None = None,
    link_cover_workflow: object | None = None,
) -> LocalTranscriptionCliRuntime:
    """Build the current candidate only from authoritative v3 receipts."""

    if locator_receipt is None or plan_receipt is None:
        raise _trust_error("AUTHORITATIVE_PLAN_REQUIRED")
    try:
        authority = validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - exact receipt boundary
        reason = getattr(exc, "reason_code", "AUTHORITATIVE_PLAN_REQUIRED")
        raise _trust_error(str(reason)) from exc
    return _build_runtime_from_ticket(
        product_identity=product_identity,
        resource_root=Path(authority.installation_target) / "Contents/Resources",
        cli_path=Path(authority.installation_target) / "Contents/MacOS/lulu-agent-cli",
        application_support_root=application_support_root,
        keychain=keychain,
        signature_verifier=signature_verifier,
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
        now=now,
        pipeline_runner=pipeline_runner,
        model_readiness_probe=model_readiness_probe,
        id_factory=id_factory,
        link_cover_workflow=link_cover_workflow,
    )


def _build_legacy_trusted_test_runtime(
    *,
    product_identity: Mapping[str, object],
    resource_root: Path,
    cli_path: Path,
    application_support_root: Path,
    keychain: KeychainBoundary,
    signature_verifier: SignatureVerifier,
    now: Callable[[], datetime] | None = None,
    pipeline_runner: LocalTranscriptionPipelineRunner = run_local_transcription_pipeline_adapter,
    model_readiness_probe: LocalModelReadinessProbe = probe_local_model_readiness,
    id_factory: Callable[[str], str] | None = None,
    link_cover_workflow: object | None = None,
) -> LocalTranscriptionCliRuntime:
    """Test-only oracle for the frozen legacy binary contract."""

    return _build_runtime_from_ticket(
        product_identity=product_identity,
        resource_root=resource_root,
        cli_path=cli_path,
        application_support_root=application_support_root,
        keychain=keychain,
        signature_verifier=signature_verifier,
        locator_receipt=None,
        plan_receipt=None,
        now=now,
        pipeline_runner=pipeline_runner,
        model_readiness_probe=model_readiness_probe,
        id_factory=id_factory,
        link_cover_workflow=link_cover_workflow,
    )


def build_production_runtime(
    *,
    product_identity: Mapping[str, object],
    resource_root: Path,
) -> LocalTranscriptionCliRuntime:
    """Build from the real frozen executable and existing system boundaries."""

    if not bool(getattr(sys, "frozen", False)):
        raise CliRuntimeTrustError(
            "CAPABILITY_UNAVAILABLE", "FROZEN_INSTALLATION_REQUIRED"
        )
    from ..license.store import StrictKeyringStore
    from .macos_signature import MacOSCodeSignatureVerifier

    shared = product_identity.get("shared")
    if not isinstance(shared, Mapping):
        raise _trust_error("PRODUCT_IDENTITY_INVALID")
    try:
        locator_receipt, plan_receipt = _reload_fixed_installed_authority()
        authority = validate_authority(
            locator_receipt=locator_receipt,
            plan_receipt=plan_receipt,
        )
    except Exception as exc:  # noqa: BLE001 - normalized authority boundary
        reason = getattr(exc, "reason_code", "AUTHORITATIVE_PLAN_REQUIRED")
        raise _trust_error(str(reason)) from exc
    app_bundle = Path(authority.installation_target)
    authority_resource_root = app_bundle / "Contents/Resources"
    authority_cli = app_bundle / "Contents/MacOS/lulu-agent-cli"
    if Path(resource_root) != authority_resource_root or Path(sys.executable) != authority_cli:
        raise _trust_error("INSTALLATION_TARGET_AUTHORITY_REQUIRED")
    signature_verifier = MacOSCodeSignatureVerifier()
    try:
        signature = signature_verifier.verify(
            app_bundle=app_bundle,
            cli_path=authority_cli,
        )
    except Exception as exc:  # noqa: BLE001 - fixed production codesign boundary
        raise _trust_error("CODE_SIGNATURE_VERIFICATION_FAILED") from exc
    try:
        application_support = _application_support_root_from_authority(authority)
    except Exception as exc:  # noqa: BLE001 - fixed account authority boundary
        reason = getattr(exc, "reason_code", "INSTALLATION_PLAN_INTEGRITY_ERROR")
        raise _trust_error(str(reason)) from exc
    runtime = _build_verified_trusted_test_runtime(
        product_identity=product_identity,
        application_support_root=application_support,
        keychain=StrictKeyringStore(),
        signature_verifier=signature_verifier,
        locator_receipt=locator_receipt,
        plan_receipt=plan_receipt,
    )
    verification = runtime._application._runtime_verification
    installation_id = verification.get("installation_id")
    identity_digest = product_identity.get("identity_digest")
    if type(installation_id) is not str or not installation_id:
        raise _trust_error("INSTALLATION_ID_INVALID")
    if type(identity_digest) is not str or not identity_digest:
        raise _trust_error("PRODUCT_IDENTITY_INVALID")
    try:
        runtime._link_host_broker = _build_production_link_host_broker(
            app_bundle=app_bundle,
            cli_path=authority_cli,
            installation_id=installation_id,
            shared_identity=shared,
            identity_digest=identity_digest,
            signature_digest=_signature_digest(signature),
            artifact_prefix="Lulu-Trusted-Test",
        )
    except LinkTranscriptionError as exc:
        raise _trust_error(exc.reason_code) from exc
    return runtime

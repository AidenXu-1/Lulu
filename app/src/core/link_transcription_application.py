"""Qt-free plan and execution seam for approved public-link transcription."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
from pathlib import PurePath
from threading import RLock
from typing import Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .collect.preferences import CollectCoverPreferenceSnapshot
from .agent_trust.canonical import domain_separated_sha256
from .link_transcription_contracts import (
    LinkTranscriptionPlanV1,
    LinkTranscriptionRequestV1,
)
from .job_ledger.contracts import OperationReceiptV1
from .job_ledger.contracts import JobState
from .job_ledger.service import JobControlService
from .job_ledger.store import JobLedgerStore


_FORMATS = frozenset({"txt", "md", "srt"})
_MEDIA_MODES = frozenset({"audio", "video"})
_SUBTITLE_FALLBACKS = frozenset({"local_transcription", "fail"})
_ENGINE = "local_sensevoice"
_DEFAULT_OUTPUT = "shared_lulu_transcript_default"
_APPROVAL_LIFETIME_SECONDS = 120.0
_RECEIPT_SEAL = object()
_APPROVAL_LOCK = RLock()


class LinkTranscriptionError(RuntimeError):
    def __init__(self, code: str, reason_code: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason_code = reason_code


class _LinkApprovalReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _RECEIPT_SEAL:
            raise TypeError("link approval receipts are Core-issued")
        object.__setattr__(self, "_LinkApprovalReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("link approval receipts are immutable")

    def __reduce__(self):
        raise TypeError("link approval receipts are not serializable")


@dataclass(frozen=True, slots=True)
class _LinkApprovalClaims:
    plan_digest: str
    installation_id: str
    job_id: str
    attempt_id: str
    lineage_id: str
    idempotency_key: str
    canonical_operation_id: str
    issuer_id: str
    principal_id: str
    session_id: str
    origin_event_id: str
    nonce: str
    issued_at: float
    expires_at: float


@dataclass(slots=True)
class _LinkApprovalState:
    claims: _LinkApprovalClaims
    state: str = "pending"
    reservation: object | None = None
    recovery_required: bool = False


@dataclass(frozen=True, slots=True)
class _LinkApprovalReservation:
    token: str
    marker: object
    claims: _LinkApprovalClaims


_LINK_APPROVALS: dict[str, _LinkApprovalState] = {}
_LINK_APPROVAL_NONCES: set[str] = set()


def run_link_transcription_pipeline_adapter(
    *,
    request: LinkTranscriptionRequestV1,
    progress: Callable[[str, float], None],
    is_cancelled: Callable[[], bool],
    cover_workflow: object | None = None,
) -> object:
    """Reuse the existing downloader/subtitle/media/cover/SenseVoice pipeline."""
    from .pipeline import transcribe_source

    return transcribe_source(
        request.source,
        engine_name=request.engine,
        enable_link_download=True,
        keep_media=request.save_media,
        media_dir=request.media_directory,
        link_media_mode=request.media_mode,
        prefer_subtitles=request.prefer_subtitles,
        subtitle_fallback=request.subtitle_fallback,
        progress=progress,
        is_cancelled=is_cancelled,
        cover_snapshot=CollectCoverPreferenceSnapshot(request.save_cover),
        cover_workflow=cover_workflow,
    )


def _issue_link_transcription_approval(
    *,
    plan: dict[str, object],
    verified_host_ticket: object,
    installation_id: str,
    job_id: str,
    attempt_id: str,
    lineage_id: str,
    idempotency_key: str,
    now: float,
) -> object:
    """Consume one broker-held, non-serializable host ticket."""
    from .cli_runtime.link_host_bridge import _VerifiedHostActionTicket

    if (
        type(plan) is not dict
        or type(verified_host_ticket) is not _VerifiedHostActionTicket
        or plan.get("installation_id") != installation_id
        or type(now) not in {int, float}
    ):
        raise LinkTranscriptionError("APPROVAL_INVALID", "HOST_ACTION_INVALID")
    host_claims = verified_host_ticket.consume(
        installation_id=installation_id,
        plan_digest=str(plan.get("plan_digest", "")),
    )
    strings = {
        "plan_digest": plan.get("plan_digest"),
        "installation_id": installation_id,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "lineage_id": lineage_id,
        "idempotency_key": idempotency_key,
        "issuer_id": host_claims.issuer_id,
        "principal_id": host_claims.principal_id,
        "session_id": host_claims.session_id,
        "origin_event_id": host_claims.origin_event_id,
        "nonce": host_claims.nonce,
    }
    if any(type(value) is not str or not value for value in strings.values()):
        raise LinkTranscriptionError("APPROVAL_INVALID", "HOST_ACTION_INVALID")
    claims = _LinkApprovalClaims(
        **strings,
        canonical_operation_id="OP_TRANSCRIBE_PUBLIC_LINK_V1",
        issued_at=float(now),
        expires_at=float(now) + _APPROVAL_LIFETIME_SECONDS,
    )
    token = secrets.token_urlsafe(32)
    with _APPROVAL_LOCK:
        if claims.nonce in _LINK_APPROVAL_NONCES:
            raise LinkTranscriptionError(
                "APPROVAL_CONSUMED", "HOST_ACTION_NONCE_REPLAYED"
            )
        _LINK_APPROVAL_NONCES.add(claims.nonce)
        _LINK_APPROVALS[token] = _LinkApprovalState(claims=claims)
    return _LinkApprovalReceipt(_RECEIPT_SEAL, token)


def _reserve_link_approval(
    receipt: object,
    *,
    plan_digest: str,
    installation_id: str,
    now: float,
) -> _LinkApprovalReservation:
    if type(receipt) is not _LinkApprovalReceipt:
        raise LinkTranscriptionError(
            "APPROVAL_INVALID", "CORE_APPROVAL_RECEIPT_INVALID"
        )
    token = object.__getattribute__(receipt, "_LinkApprovalReceipt__token")
    with _APPROVAL_LOCK:
        state = _LINK_APPROVALS.get(token)
        if state is None:
            raise LinkTranscriptionError(
                "APPROVAL_INVALID", "CORE_APPROVAL_RECEIPT_INVALID"
            )
        if state.state != "pending":
            raise LinkTranscriptionError(
                "APPROVAL_CONSUMED", "CORE_APPROVAL_ALREADY_USED"
            )
        claims = state.claims
        if float(now) < claims.issued_at or float(now) > claims.expires_at:
            raise LinkTranscriptionError("APPROVAL_EXPIRED", "CORE_APPROVAL_EXPIRED")
        if (
            claims.plan_digest != plan_digest
            or claims.installation_id != installation_id
        ):
            raise LinkTranscriptionError(
                "APPROVAL_INVALID", "CORE_APPROVAL_BINDING_MISMATCH"
            )
        marker = object()
        state.state = "reserved"
        state.reservation = marker
        return _LinkApprovalReservation(token, marker, claims)


def _release_link_approval(reservation: _LinkApprovalReservation) -> None:
    with _APPROVAL_LOCK:
        state = _LINK_APPROVALS.get(reservation.token)
        if (
            state is not None
            and state.state == "reserved"
            and state.reservation is reservation.marker
        ):
            state.state = "pending"
            state.reservation = None


def _claim_link_approval(reservation: _LinkApprovalReservation) -> None:
    with _APPROVAL_LOCK:
        state = _LINK_APPROVALS.get(reservation.token)
        if (
            state is None
            or state.state != "reserved"
            or state.reservation is not reservation.marker
        ):
            raise LinkTranscriptionError(
                "APPROVAL_CONSUMED", "CORE_APPROVAL_ALREADY_USED"
            )
        state.state = "claimed"
        state.reservation = None


def _settle_link_approval(
    reservation: _LinkApprovalReservation, *, recovery_required: bool
) -> None:
    with _APPROVAL_LOCK:
        state = _LINK_APPROVALS.get(reservation.token)
        if state is None or state.state != "claimed":
            raise LinkTranscriptionError(
                "INTEGRITY_ERROR", "CORE_APPROVAL_SETTLEMENT_INVALID"
            )
        if recovery_required:
            state.recovery_required = True
        else:
            state.state = "consumed"
            state.recovery_required = False


def _validate_job_approval_claim(
    approval_claim: object,
    *,
    job: object,
    operation_receipt: object,
) -> bool:
    """Let JobLedgerStore accept only this still-reserved Core receipt."""
    if (
        type(approval_claim) is not _LinkApprovalReservation
        or not isinstance(operation_receipt, OperationReceiptV1)
    ):
        return False
    claims = approval_claim.claims
    with _APPROVAL_LOCK:
        state = _LINK_APPROVALS.get(approval_claim.token)
        if (
            state is None
            or state.state != "reserved"
            or state.reservation is not approval_claim.marker
        ):
            return False
    return bool(
        getattr(job, "job_id", None) == claims.job_id == operation_receipt.job_id
        and getattr(job, "current_attempt_id", None)
        == claims.attempt_id
        == operation_receipt.attempt_id
        and getattr(job, "lineage_id", None)
        == claims.lineage_id
        == operation_receipt.lineage_id
        and getattr(job, "installation_id", None)
        == claims.installation_id
        == operation_receipt.installation_id
        and getattr(job, "canonical_operation_id", None)
        == claims.canonical_operation_id
        == operation_receipt.canonical_operation_id
        and getattr(job, "idempotency_key", None)
        == claims.idempotency_key
        == operation_receipt.idempotency_key
        and getattr(job, "intent_payload_digest", None)
        == claims.plan_digest
        == operation_receipt.intent_payload_digest
    )


class LinkTranscriptionJobBackend:
    """Adapter over the existing durable job ledger and fencing service."""

    def __init__(self, store: JobLedgerStore, *, now: Callable[[], str]) -> None:
        if not isinstance(store, JobLedgerStore) or not callable(now):
            raise TypeError("authoritative job backend is invalid")
        self._store = store
        self._control = JobControlService(store, enforce_fencing=True)
        self._now = now
        self._leases: dict[str, object] = {}
        self._revisions: dict[str, int] = {}

    def start(
        self,
        *,
        plan: dict[str, object],
        claims: _LinkApprovalClaims,
        operation_receipt: OperationReceiptV1,
        approval_claim: _LinkApprovalReservation,
    ) -> dict[str, int]:
        typed = plan["typed_request"]
        created = self._store.create_job(
            job_id=claims.job_id,
            attempt_id=claims.attempt_id,
            lineage_id=claims.lineage_id,
            job_kind="public_link_transcription",
            input_ref="url:sha256:"
            + domain_separated_sha256("LULU_LINK_SOURCE_V1", typed["source"]),
            frozen_policy={
                "typed_request": typed,
                "selected_profiles": plan["selected_profiles"],
                "targets_digest": domain_separated_sha256(
                    "LULU_LINK_TARGETS_V1", plan["targets"]
                ),
            },
            created_by="agent_cli",
            installation_id=claims.installation_id,
            canonical_operation_id=claims.canonical_operation_id,
            idempotency_key=claims.idempotency_key,
            intent_payload_digest=claims.plan_digest,
            created_at=self._now(),
            effect_fingerprint="effect:network-download:" + claims.plan_digest,
            account_ref="account:none",
            target_ref="target:sha256:"
            + domain_separated_sha256("LULU_LINK_TARGETS_V1", plan["targets"]),
            uncertain_response_state="pending",
        )
        if created.job_id != claims.job_id or created.job_revision != 1:
            raise LinkTranscriptionError("CONFLICT", "IDEMPOTENT_JOB_ALREADY_EXISTS")
        queued = self._store.transition(
            claims.job_id,
            expected_revision=created.job_revision,
            new_state=JobState.QUEUED,
            changed_at=self._now(),
        )
        running, lease = self._control.claim_worker(
            claims.job_id,
            expected_revision=queued.job_revision,
            worker_instance_id=f"agent-cli-link:{claims.job_id}",
            operation_receipt=operation_receipt,
            approved_external_claim=approval_claim,
            changed_at=self._now(),
        )
        self._leases[claims.job_id] = lease
        self._revisions[claims.job_id] = running.job_revision
        return {
            "job_revision": running.job_revision,
            "lease_epoch": running.attempts[-1].lease_epoch,
        }

    def complete_success(
        self, *, claims: _LinkApprovalClaims, result_ref: str
    ) -> dict[str, object]:
        completed = self._control.complete_worker(
            claims.job_id,
            expected_revision=self._revisions[claims.job_id],
            lease_token=self._leases[claims.job_id],
            new_state=JobState.SUCCEEDED,
            result_ref=result_ref,
            changed_at=self._now(),
        )
        return self._receipt(completed)

    def complete_failure(
        self, *, claims: _LinkApprovalClaims, reason_code: str
    ) -> None:
        del reason_code
        completed = self._control.complete_worker(
            claims.job_id,
            expected_revision=self._revisions[claims.job_id],
            lease_token=self._leases[claims.job_id],
            new_state=JobState.FAILED,
            result_ref=None,
            changed_at=self._now(),
        )
        self._revisions[claims.job_id] = completed.job_revision

    def mark_recovery_required(
        self, *, claims: _LinkApprovalClaims, reason_code: str
    ) -> None:
        recovered = self._store.transition(
            claims.job_id,
            expected_revision=self._revisions[claims.job_id],
            new_state=JobState.RECOVERY_REQUIRED,
            reason_code=reason_code,
            changed_at=self._now(),
            lease_token=self._leases[claims.job_id],
        )
        self._revisions[claims.job_id] = recovered.job_revision

    @staticmethod
    def _receipt(job: object) -> dict[str, object]:
        attempt = job.attempts[-1]
        return {
            "job_id": job.job_id,
            "attempt_id": attempt.attempt_id,
            "lineage_id": attempt.lineage_id,
            "job_revision": job.job_revision,
            "lease_epoch": attempt.lease_epoch,
            "state": job.state.value,
            "result_ref": job.result_ref,
            "receipt_ref": f"lulu-job:{job.job_id}:revision:{job.job_revision}",
        }


def canonicalize_public_url(value: str) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        raise LinkTranscriptionError("INVALID_ARGUMENT", "URL_INVALID")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LinkTranscriptionError("INVALID_ARGUMENT", "URL_INVALID") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LinkTranscriptionError("INVALID_ARGUMENT", "URL_INVALID")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower().rstrip(".")
    if not hostname:
        raise LinkTranscriptionError("INVALID_ARGUMENT", "URL_INVALID")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    include_port = port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    )
    netloc = f"{hostname}:{port}" if include_port else hostname
    path = parsed.path or "/"
    return urlunsplit(SplitResult(scheme, netloc, path, parsed.query, ""))


class LinkTranscriptionApplicationService:
    def __init__(
        self,
        *,
        installation_id: str,
        pipeline_runner: Callable[..., object] = run_link_transcription_pipeline_adapter,
        job_backend: object | None = None,
        result_writer: object | None = None,
        cover_workflow: object | None = None,
    ) -> None:
        if type(installation_id) is not str or not installation_id:
            raise TypeError("installation_id must be non-empty")
        if not callable(pipeline_runner):
            raise TypeError("pipeline_runner must be callable")
        self._installation_id = installation_id
        self._pipeline_runner = pipeline_runner
        self._job_backend = job_backend
        self._result_writer = result_writer
        self._cover_workflow = cover_workflow

    @property
    def activated_capability_ids(self) -> frozenset[str]:
        capability_ids = {"TR-05", "TR-06", "TR-07"}
        if self._cover_workflow is not None:
            capability_ids.add("TR-08")
        return frozenset(capability_ids)

    def prepare(
        self,
        source: str,
        *,
        engine: str = _ENGINE,
        format: str = "txt",
        prefer_subtitles: bool = False,
        subtitle_fallback: str = "local_transcription",
        save_media: bool = False,
        media_mode: str = "audio",
        media_directory: str | None = None,
        save_cover: bool = False,
        output_destination: str = _DEFAULT_OUTPUT,
    ) -> dict[str, object]:
        request = self._typed_request(
            source=source,
            engine=engine,
            format=format,
            prefer_subtitles=prefer_subtitles,
            subtitle_fallback=subtitle_fallback,
            save_media=save_media,
            media_mode=media_mode,
            media_directory=media_directory,
            save_cover=save_cover,
            output_destination=output_destination,
        )
        capability_ids = ["TR-05"]
        if request.prefer_subtitles:
            capability_ids.append("TR-06")
        if request.save_media:
            capability_ids.append("TR-07")
        if request.save_cover:
            capability_ids.append("TR-08")
        effects = {
            "profile": "EFFECT_NETWORK_DOWNLOAD",
            "network": True,
            "download_public_media": True,
            "download_subtitles": request.prefer_subtitles,
            "run_local_model_on_fallback": request.subtitle_fallback
            == "local_transcription",
            "writes_shared_transcript": True,
            "writes_media": request.save_media,
            "writes_cover": request.save_cover,
            "uploads_content": False,
            "may_charge": False,
        }
        targets = {
            "transcript": request.output_destination,
            "media_directory": request.media_directory if request.save_media else None,
            "cover_destination": (
                request.media_directory if request.save_cover else None
            ),
        }
        profiles = {
            "effect": "EFFECT_NETWORK_DOWNLOAD",
            "approval": "APPROVAL_HUMAN_RECEIPT",
            "job": "JOB_NETWORK",
            "operation": "OP_TRANSCRIBE_PUBLIC_LINK_V1",
        }
        idempotency = {
            "required_for_run": True,
            "binding": "canonical_url+all_options+targets+installation+job_attempt",
        }
        missing_questions = ["confirm_current_settings"]
        if request.save_media and request.media_directory is None:
            missing_questions.insert(0, "media_directory")
        digest_payload = {
            "plan_version": "lulu.link-transcription-plan/v1",
            "capability_ids": capability_ids,
            "installation_id": self._installation_id,
            "typed_request": request.as_dict(),
            "effects": effects,
            "targets": targets,
            "selected_profiles": profiles,
            "idempotency": idempotency,
        }
        plan_digest = domain_separated_sha256(
            "LULU_LINK_TRANSCRIPTION_PLAN_V1", digest_payload
        )
        return LinkTranscriptionPlanV1(
            plan_version="lulu.link-transcription-plan/v1",
            capability_ids=tuple(capability_ids),
            installation_id=self._installation_id,
            typed_request=request,
            effects=effects,
            targets=targets,
            selected_profiles=profiles,
            idempotency=idempotency,
            missing_questions=tuple(missing_questions),
            plan_digest=plan_digest,
        ).as_dict()

    def run(
        self,
        source: str,
        *,
        approval_receipt: object | None,
        **options: object,
    ) -> dict[str, object]:
        if approval_receipt is None:
            raise LinkTranscriptionError(
                "CONFIRMATION_REQUIRED", "CORE_APPROVAL_RECEIPT_REQUIRED"
            )
        now_value = options.pop("now", None)
        expected_idempotency_key = options.pop("expected_idempotency_key", None)
        if type(now_value) not in {int, float}:
            raise LinkTranscriptionError("APPROVAL_INVALID", "APPROVAL_CLOCK_INVALID")
        plan = self.prepare(source, **options)
        if (
            not callable(getattr(self._job_backend, "start", None))
            or not callable(getattr(self._job_backend, "complete_success", None))
            or not callable(getattr(self._job_backend, "complete_failure", None))
            or not callable(getattr(self._job_backend, "mark_recovery_required", None))
            or not (
                callable(self._result_writer)
                or callable(getattr(self._result_writer, "write_link", None))
            )
        ):
            raise LinkTranscriptionError(
                "RUNTIME_NOT_READY", "LINK_TRANSCRIPTION_RUNTIME_INCOMPLETE"
            )
        reservation = _reserve_link_approval(
            approval_receipt,
            plan_digest=str(plan["plan_digest"]),
            installation_id=self._installation_id,
            now=float(now_value),
        )
        claims = reservation.claims
        if (
            expected_idempotency_key is not None
            and expected_idempotency_key != claims.idempotency_key
        ):
            _release_link_approval(reservation)
            raise LinkTranscriptionError(
                "APPROVAL_INVALID", "IDEMPOTENCY_BINDING_MISMATCH"
            )
        created_at = datetime.fromtimestamp(
            float(now_value), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        operation_receipt = OperationReceiptV1(
            operation_receipt_id=f"operation:{claims.job_id}:{claims.attempt_id}",
            job_id=claims.job_id,
            attempt_id=claims.attempt_id,
            lineage_id=claims.lineage_id,
            installation_id=claims.installation_id,
            canonical_operation_id=claims.canonical_operation_id,
            effect_profile="EFFECT_NETWORK_DOWNLOAD",
            job_profile="JOB_NETWORK",
            approval_profile="APPROVAL_HUMAN_RECEIPT",
            idempotency_key=claims.idempotency_key,
            intent_payload_digest=claims.plan_digest,
            created_at=created_at,
        )
        try:
            start_receipt = self._job_backend.start(
                plan=plan,
                claims=claims,
                operation_receipt=operation_receipt,
                approval_claim=reservation,
            )
            if (
                type(start_receipt) is not dict
                or type(start_receipt.get("job_revision")) is not int
                or type(start_receipt.get("lease_epoch")) is not int
            ):
                raise LinkTranscriptionError(
                    "INTEGRITY_ERROR", "JOB_START_RECEIPT_INVALID"
                )
        except Exception:
            _release_link_approval(reservation)
            raise
        _claim_link_approval(reservation)
        request = LinkTranscriptionRequestV1(**plan["typed_request"])
        try:
            transcript = self._pipeline_runner(
                request=request,
                progress=lambda _message, _fraction: None,
                is_cancelled=lambda: False,
                cover_workflow=self._cover_workflow,
            )
            if callable(self._result_writer):
                stored = self._result_writer(claims.job_id, transcript)
            else:
                stored = self._result_writer.write_link(claims.job_id, transcript)
            if (
                type(stored) is not dict
                or type(stored.get("result_ref")) is not str
                or not stored["result_ref"]
                or type(stored.get("content_hash")) is not str
                or len(stored["content_hash"]) != 64
            ):
                raise LinkTranscriptionError(
                    "INTEGRITY_ERROR", "TRANSCRIPTION_RESULT_RECEIPT_INVALID"
                )
            job_receipt = self._job_backend.complete_success(
                claims=claims,
                result_ref=stored["result_ref"],
            )
        except Exception as exc:
            reason = getattr(exc, "reason_code", type(exc).__name__)
            try:
                self._job_backend.complete_failure(
                    claims=claims, reason_code=str(reason)
                )
                _settle_link_approval(reservation, recovery_required=False)
            except Exception as settlement_error:
                try:
                    self._job_backend.mark_recovery_required(
                        claims=claims, reason_code="UNKNOWN_EXTERNAL_RESULT"
                    )
                    _settle_link_approval(reservation, recovery_required=True)
                except Exception:
                    pass
                raise LinkTranscriptionError(
                    "INTEGRITY_ERROR", "LINK_FAILURE_SETTLEMENT_FAILED"
                ) from settlement_error
            raise
        if type(job_receipt) is not dict or job_receipt.get("state") != "succeeded":
            try:
                self._job_backend.mark_recovery_required(
                    claims=claims, reason_code="UNKNOWN_EXTERNAL_RESULT"
                )
                _settle_link_approval(reservation, recovery_required=True)
            finally:
                raise LinkTranscriptionError(
                    "INTEGRITY_ERROR", "JOB_SUCCESS_RECEIPT_INVALID"
                )
        _settle_link_approval(reservation, recovery_required=False)
        return {
            "status": "succeeded",
            "result_ref": stored["result_ref"],
            "content_hash": stored["content_hash"],
            "job_receipt": job_receipt,
            "operation_receipt": operation_receipt.as_dict(),
        }

    @staticmethod
    def _typed_request(
        *,
        source: str,
        engine: str,
        format: str,
        prefer_subtitles: bool,
        subtitle_fallback: str,
        save_media: bool,
        media_mode: str,
        media_directory: str | None,
        save_cover: bool,
        output_destination: str,
    ) -> LinkTranscriptionRequestV1:
        if engine != _ENGINE:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "ENGINE_UNSUPPORTED")
        if format not in _FORMATS:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "FORMAT_UNSUPPORTED")
        if type(prefer_subtitles) is not bool or type(save_media) is not bool or type(save_cover) is not bool:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "BOOLEAN_OPTION_INVALID")
        if subtitle_fallback not in _SUBTITLE_FALLBACKS:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "SUBTITLE_FALLBACK_INVALID")
        if media_mode not in _MEDIA_MODES:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "MEDIA_MODE_INVALID")
        if media_directory is not None:
            directory = PurePath(media_directory)
            if not directory.is_absolute() or ".." in directory.parts:
                raise LinkTranscriptionError("INVALID_ARGUMENT", "MEDIA_DIRECTORY_INVALID")
        if type(output_destination) is not str or not output_destination:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "OUTPUT_DESTINATION_INVALID")
        if output_destination != _DEFAULT_OUTPUT:
            destination = PurePath(output_destination)
            if not destination.is_absolute() or ".." in destination.parts:
                raise LinkTranscriptionError("INVALID_ARGUMENT", "OUTPUT_DESTINATION_INVALID")
        return LinkTranscriptionRequestV1(
            source=canonicalize_public_url(source),
            engine=engine,
            format=format,
            prefer_subtitles=prefer_subtitles,
            subtitle_fallback=subtitle_fallback,
            save_media=save_media,
            media_mode=media_mode,
            media_directory=media_directory,
            save_cover=save_cover,
            output_destination=output_destination,
        )

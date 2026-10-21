"""Qt-free application service for the packaged local transcription CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Callable, Mapping, Protocol

from .auth_contracts import AuthorizationError
from .auth_guard import OpenProductAccessGuard, UnifiedLicenseAuthGuard
from .job_ledger.contracts import JobState
from .job_ledger.service import JobControlService
from .job_ledger.store import JobLedgerStore
from .transcription_application import LocalFileTranscriptionApplicationService
from .transcription_contracts import LocalFileTranscriptionRequestV1


from .local_transcription_models import (
    DEFAULT_ENGINE, MODEL_MESSAGES, LocalModelUnavailable, normalize_engine, model_status,
)


class ModelReadinessProbe(Protocol):
    def __call__(self, engine_name: str) -> bool: ...


class TranscriptionResultWriter(Protocol):
    def write(self, job_id: str, result: object) -> Mapping[str, str]: ...


class LocalTranscriptionJobError(RuntimeError):
    def __init__(self, code: str, reason_code: str) -> None:
        super().__init__(MODEL_MESSAGES.get(reason_code, code))
        self.code = code
        self.reason_code = reason_code


class LocalTranscriptionJobApplication:
    """Own the fail-closed order shared by prepare and later job execution."""

    def __init__(
        self,
        *,
        runtime_verification: Mapping[str, object],
        authorization_guard: OpenProductAccessGuard,
        model_readiness_probe: ModelReadinessProbe,
        transcription_service: LocalFileTranscriptionApplicationService | None = None,
        job_store: JobLedgerStore | None = None,
        result_store: TranscriptionResultWriter | None = None,
        id_factory: Callable[[str], str] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._runtime_verification = dict(runtime_verification)
        self._authorization_guard = authorization_guard
        self._model_readiness_probe = model_readiness_probe
        self._transcription_service = transcription_service
        self._job_store = job_store
        self._result_store = result_store
        self._id_factory = id_factory
        self._now = now

    def prepare(self, source_path: str, engine: str = DEFAULT_ENGINE) -> dict[str, object]:
        installation_id, source_summary = self._preflight(source_path, engine)
        return {
            "status": "ready",
            "capability_id": "TR-01",
            "engine": engine,
            "installation_id": installation_id,
            "source_summary": source_summary,
            "effects": [],
        }

    def model_status(self, engine: str | None = None) -> dict[str, object]:
        self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization({'capability_id': 'TR-01', 'operation': 'model_status'})
        from .engines.local_models import LOCAL_MODELS
        if engine is not None and engine not in LOCAL_MODELS:
            raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'LOCAL_ENGINE_UNSUPPORTED')
        engines = [engine] if engine is not None else list(LOCAL_MODELS)
        return dict(default_engine=DEFAULT_ENGINE, models=[model_status(value) for value in engines])

    def read_result_bytes(self, job_id: str, result_ref: str) -> bytes:
        self.assert_job_control_allowed("JOB-06")
        reader = getattr(self._result_store, "read_verified_bytes", None)
        if not callable(reader):
            raise LocalTranscriptionJobError("RUNTIME_NOT_READY", "RESULT_READER_UNAVAILABLE")
        return reader(job_id, result_ref)

    def run(self, source_path: str, engine: str = DEFAULT_ENGINE) -> dict[str, object]:
        installation_id, source_summary = self._preflight(source_path, engine)
        if (
            not isinstance(
                self._transcription_service,
                LocalFileTranscriptionApplicationService,
            )
            or not isinstance(self._job_store, JobLedgerStore)
            or self._result_store is None
            or not callable(getattr(self._result_store, "write", None))
            or not callable(self._id_factory)
            or not callable(self._now)
        ):
            raise LocalTranscriptionJobError(
                "RUNTIME_NOT_READY", "LOCAL_TRANSCRIPTION_RUNTIME_INCOMPLETE"
            )
        intent_payload = {
            "capability_id": "TR-02",
            "engine": engine,
            "installation_id": installation_id,
            "source_summary": source_summary,
        }
        intent_digest = hashlib.sha256(
            json.dumps(
                intent_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        requested_job_id = self._id_factory("job")
        attempt_id = self._id_factory("attempt")
        lineage_id = self._id_factory("lineage")
        created = self._job_store.create_job(
            job_id=requested_job_id,
            attempt_id=attempt_id,
            lineage_id=lineage_id,
            job_kind="local_transcription",
            input_ref=f"source:sha256:{source_summary['sha256']}",
            frozen_policy={"engine": engine, "mode": "offline"},
            created_by="agent_cli",
            installation_id=installation_id,
            canonical_operation_id="OP_TRANSCRIBE_LOCAL_V1",
            idempotency_key=f"local-transcription:{intent_digest}",
            intent_payload_digest=intent_digest,
            created_at=self._now(),
            effect_fingerprint="effect:local-transcription-result",
            account_ref="account:none",
            target_ref="target:shared-lulu-jobs-results",
            uncertain_response_state="none",
        )
        if created.job_id != requested_job_id:
            raise LocalTranscriptionJobError(
                "CONFLICT", "IDEMPOTENT_JOB_ALREADY_EXISTS"
            )
        job_id = created.job_id
        queued = self._job_store.transition(
            job_id,
            expected_revision=created.job_revision,
            new_state=JobState.QUEUED,
            changed_at=self._now(),
        )
        control = JobControlService(self._job_store, enforce_fencing=True)
        running, lease = control.claim_worker(
            job_id,
            expected_revision=queued.job_revision,
            worker_instance_id=f"agent-cli:{job_id}",
            operation_receipt=None,
            changed_at=self._now(),
        )
        try:
            result = self._transcription_service.transcribe(
                LocalFileTranscriptionRequestV1(source_path=source_path, engine_name=engine),
                is_cancelled=lambda: control.show(job_id).state is not JobState.RUNNING,
            )
            if control.show(job_id).state is not JobState.RUNNING:
                raise InterruptedError("转录已取消")
            stored = dict(self._result_store.write(job_id, result))
        except Exception:
            try:
                current = control.show(job_id)
                if current.state is JobState.CANCEL_REQUESTED:
                    control.settle_cancelled(job_id, expected_revision=current.job_revision,
                        lease_token=lease, changed_at=self._now())
                else:
                    control.complete_worker(
                        job_id,
                        expected_revision=running.job_revision,
                        lease_token=lease,
                        new_state=JobState.FAILED,
                        result_ref=None,
                        changed_at=self._now(),
                    )
            except Exception as settlement_error:
                raise LocalTranscriptionJobError(
                    "INTEGRITY_ERROR", "JOB_FAILURE_SETTLEMENT_FAILED"
                ) from settlement_error
            raise
        result_ref = stored.get("result_ref")
        content_hash = stored.get("content_hash")
        if (
            type(result_ref) is not str
            or not result_ref
            or type(content_hash) is not str
            or len(content_hash) != 64
        ):
            raise LocalTranscriptionJobError(
                "INTEGRITY_ERROR", "TRANSCRIPTION_RESULT_RECEIPT_INVALID"
            )
        completed = control.complete_worker(
            job_id,
            expected_revision=running.job_revision,
            lease_token=lease,
            new_state=JobState.SUCCEEDED,
            result_ref=result_ref,
            changed_at=self._now(),
        )
        receipt_ref = f"lulu-job:{job_id}:revision:{completed.job_revision}"
        return {
            "status": completed.state.value,
            "result_ref": result_ref,
            "content_hash": content_hash,
            "job_receipt": {
                "job_id": completed.job_id,
                "job_revision": completed.job_revision,
                "state": completed.state.value,
                "last_sequence": completed.event_sequence,
                "result_ref": completed.result_ref,
                "receipt_ref": receipt_ref,
            },
        }

    def assert_job_control_allowed(self, capability_id: str) -> None:
        if capability_id not in {"JOB-01", "JOB-02", "JOB-03", "JOB-06"}:
            raise LocalTranscriptionJobError(
                "CAPABILITY_UNAVAILABLE", "JOB_CAPABILITY_NOT_ACTIVATED"
            )
        self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization(
            {"capability_id": capability_id, "operation": "job_control"}
        )

    def has_verified_trusted_test_runtime(self) -> bool:
        try:
            self._assert_verified_trusted_test_runtime()
        except LocalTranscriptionJobError:
            return False
        return True

    def verified_installation_id(self) -> str:
        """Return the sealed installation identity after the normal trust check."""
        return self._assert_verified_trusted_test_runtime()

    def assert_link_extraction_allowed(self) -> str:
        installation_id = self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization({'operation': 'public_link_extraction'})
        return installation_id

    def assert_local_synthesis_allowed(self, capability_id: str) -> str:
        if capability_id not in {"TTS-01", "TTS-02"}:
            raise LocalTranscriptionJobError("CAPABILITY_UNAVAILABLE", "TTS_CAPABILITY_NOT_ACTIVATED")
        installation_id = self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization({"capability_id": capability_id, "operation": "local_synthesis"})
        return installation_id

    def assert_library_read_allowed(self, capability_id: str) -> str:
        if capability_id not in {"LIB-01", "LIB-02", "COPY-07"}:
            raise LocalTranscriptionJobError("CAPABILITY_UNAVAILABLE", "LIBRARY_CAPABILITY_NOT_ACTIVATED")
        installation_id = self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization({"capability_id": capability_id, "operation": "local_read"})
        return installation_id

    def assert_workspace_allowed(self, capability_id: str) -> str:
        from .agent_workspace.contracts import CAPABILITIES
        if capability_id not in CAPABILITIES:
            raise LocalTranscriptionJobError('CAPABILITY_UNAVAILABLE','WORKSPACE_CAPABILITY_NOT_ACTIVATED')
        installation_id=self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization({'capability_id':capability_id,'operation':'workspace'})
        return installation_id

    def _preflight(self, source_path: str, engine: str = DEFAULT_ENGINE) -> tuple[str, dict[str, object]]:
        installation_id = self._assert_verified_trusted_test_runtime()
        self._assert_fresh_cached_authorization(source_path)
        try:
            if normalize_engine(engine) != engine:
                raise ValueError("canonical engine required")
        except ValueError as exc:
            raise LocalTranscriptionJobError("INVALID_ARGUMENT", "LOCAL_ENGINE_UNSUPPORTED") from exc
        source_summary = self._read_source_summary(source_path)
        self._assert_model_ready(engine)
        return installation_id, source_summary

    def _assert_verified_trusted_test_runtime(self) -> str:
        verification = self._runtime_verification
        installation_id = verification.get("installation_id")
        if (
            verification.get("profile") != "trusted_test"
            or verification.get("verified") is not True
            or verification.get("signature_kind") == "ad_hoc"
            or type(installation_id) is not str
            or not installation_id
        ):
            raise LocalTranscriptionJobError(
                "CAPABILITY_UNAVAILABLE",
                "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            )
        return installation_id

    def _assert_fresh_cached_authorization(self, request: object) -> None:
        if type(self._authorization_guard) not in (OpenProductAccessGuard, UnifiedLicenseAuthGuard):
            raise LocalTranscriptionJobError("AUTH_REQUIRED", "AUTH_GUARD_REQUIRED")
        try:
            self._authorization_guard.assert_local_file_transcription_allowed(request)
        except AuthorizationError as exc:
            raise LocalTranscriptionJobError(
                exc.failure.code,
                "FRESH_CACHED_AUTH_REJECTED",
            ) from exc

    @staticmethod
    def _read_source_summary(source_path: str) -> dict[str, object]:
        if type(source_path) is not str or not source_path:
            raise LocalTranscriptionJobError("INVALID_ARGUMENT", "SOURCE_INVALID")
        path = Path(source_path)
        if not path.is_absolute() or ".." in path.parts:
            raise LocalTranscriptionJobError("INVALID_ARGUMENT", "SOURCE_INVALID")
        try:
            before = os.lstat(path)
        except FileNotFoundError as exc:
            raise LocalTranscriptionJobError("NOT_FOUND", "SOURCE_NOT_FOUND") from exc
        except OSError as exc:
            raise LocalTranscriptionJobError("UNSAFE_PATH", "SOURCE_STAT_FAILED") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise LocalTranscriptionJobError("UNSAFE_PATH", "SOURCE_NOT_REGULAR")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                ):
                    raise LocalTranscriptionJobError(
                        "UNSAFE_PATH", "SOURCE_IDENTITY_CHANGED"
                    )
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                os.close(descriptor)
        except LocalTranscriptionJobError:
            raise
        except OSError as exc:
            raise LocalTranscriptionJobError("UNSAFE_PATH", "SOURCE_OPEN_FAILED") from exc
        return {"bytes": size, "sha256": digest.hexdigest()}

    def _assert_model_ready(self, engine: str = DEFAULT_ENGINE) -> None:
        try:
            ready = bool(self._model_readiness_probe(engine))
        except LocalModelUnavailable as exc:
            raise LocalTranscriptionJobError(exc.code, exc.reason_code) from exc
        except Exception as exc:  # noqa: BLE001 - injected boundary
            raise LocalTranscriptionJobError(
                "RUNTIME_NOT_READY", "MODEL_READINESS_PROBE_FAILED"
            ) from exc
        if not ready:
            raise LocalTranscriptionJobError("MODEL_NOT_READY", "MODEL_NOT_READY")

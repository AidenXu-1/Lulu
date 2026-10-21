"""Owner-only, tamper-evident storage for general job ledger v1."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
from threading import RLock
from typing import Protocol
from uuid import uuid4

from .contracts import (
    ALLOWED_TRANSITIONS,
    JOB_LEDGER_SCHEMA,
    JOB_LEDGER_SCHEMA_VERSION,
    TERMINAL_STATES,
    AttemptV1,
    EventV1,
    JobLedgerError,
    JobState,
    JobV1,
    LeaseToken,
    MigrationReceiptV1,
    OperationReceiptV1,
    ReceiptInheritanceV1,
    conflict,
    integrity_error,
    resource_busy,
)
from ..resource_control.fencing import JobLeaseFencer


MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_POLICY_BYTES = 64 * 1024


class IntegrityAnchorStore(Protocol):
    def hmac_key(self) -> bytes: ...

    def load(self) -> dict[str, object] | None: ...

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None: ...


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise integrity_error("JOB_LEDGER_NONCANONICAL_VALUE") from exc


def _validate_no_follow_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("JOB_LEDGER_SYMLINK_FORBIDDEN")


def _validate_owned_mode(path: Path, *, mode: int, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise integrity_error("JOB_LEDGER_PATH_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise integrity_error("JOB_LEDGER_SYMLINK_FORBIDDEN")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        raise integrity_error("JOB_LEDGER_FILE_TYPE_INVALID")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise integrity_error("JOB_LEDGER_OWNER_INVALID")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise integrity_error("JOB_LEDGER_MODE_INVALID")


def _strict_dict(
    value: object,
    fields: frozenset[str],
    reason_code: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise integrity_error(reason_code)
    return value


def _nonempty(value: object, reason_code: str) -> str:
    if type(value) is not str or not value or len(value) > 16 * 1024:
        raise integrity_error(reason_code)
    return value


def _operation_from_dict(value: object) -> OperationReceiptV1:
    fields = frozenset(OperationReceiptV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_OPERATION_RECEIPT_INVALID")
    for field in fields:
        _nonempty(raw[field], "JOB_OPERATION_RECEIPT_INVALID")
    return OperationReceiptV1(**raw)


def _receipt_inheritance_from_dict(value: object) -> ReceiptInheritanceV1:
    fields = frozenset(ReceiptInheritanceV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_RECEIPT_INHERITANCE_INVALID")
    for field in fields:
        _nonempty(raw[field], "JOB_RECEIPT_INHERITANCE_INVALID")
    return ReceiptInheritanceV1(**raw)


def _operation_receipt_digest(receipt: OperationReceiptV1) -> str:
    return hashlib.sha256(_canonical_bytes(receipt.as_dict())).hexdigest()


def _attempt_from_dict(value: object) -> AttemptV1:
    fields = frozenset(AttemptV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_ATTEMPT_INVALID")
    try:
        state = JobState(raw["state"])
    except (TypeError, ValueError) as exc:
        raise integrity_error("JOB_ATTEMPT_STATE_INVALID") from exc
    if (
        type(raw["attempt_number"]) is not int
        or raw["attempt_number"] < 1
        or type(raw["lease_epoch"]) is not int
        or raw["lease_epoch"] < 0
    ):
        raise integrity_error("JOB_ATTEMPT_REVISION_INVALID")
    for field in (
        "attempt_id",
        "lineage_id",
        "created_at",
        "updated_at",
    ):
        _nonempty(raw[field], "JOB_ATTEMPT_INVALID")
    optional_strings = (
        "worker_instance_id",
        "effect_fingerprint",
        "account_ref",
        "target_ref",
        "uncertain_response_state",
    )
    if any(
        value is not None and (type(value) is not str or not value)
        for value in (raw[field] for field in optional_strings)
    ):
        raise integrity_error("JOB_ATTEMPT_INVALID")
    receipt = raw["operation_receipt"]
    return AttemptV1(
        attempt_id=raw["attempt_id"],
        lineage_id=raw["lineage_id"],
        attempt_number=raw["attempt_number"],
        state=state,
        lease_epoch=raw["lease_epoch"],
        worker_instance_id=raw["worker_instance_id"],
        operation_receipt=(
            None if receipt is None else _operation_from_dict(receipt)
        ),
        effect_fingerprint=raw["effect_fingerprint"],
        account_ref=raw["account_ref"],
        target_ref=raw["target_ref"],
        uncertain_response_state=raw["uncertain_response_state"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
    )


def _validate_partial_failures(
    value: object,
) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        raise integrity_error("JOB_PARTIAL_FAILURE_INVALID")
    normalized: list[dict[str, str]] = []
    required_fields = frozenset({"item_ref", "code"})
    allowed_fields = required_fields | {"retry_hint"}
    for item in value:
        if (
            not isinstance(item, dict)
            or not required_fields.issubset(item)
            or not frozenset(item).issubset(allowed_fields)
        ):
            raise integrity_error("JOB_PARTIAL_FAILURE_INVALID")
        normalized_item: dict[str, str] = {}
        for field, field_value in item.items():
            normalized_item[field] = _nonempty(
                field_value,
                "JOB_PARTIAL_FAILURE_INVALID",
            )
        normalized.append(normalized_item)
    return tuple(normalized)


def _job_from_dict(value: object) -> JobV1:
    fields = frozenset(JobV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_RECORD_INVALID")
    try:
        state = JobState(raw["state"])
    except (TypeError, ValueError) as exc:
        raise integrity_error("JOB_STATE_INVALID") from exc
    if (
        type(raw["job_revision"]) is not int
        or raw["job_revision"] < 1
        or type(raw["event_sequence"]) is not int
        or raw["event_sequence"] < 1
        or not isinstance(raw["frozen_policy"], dict)
        or not isinstance(raw["partial_failures"], list)
        or not isinstance(raw["attempts"], list)
        or not raw["attempts"]
    ):
        raise integrity_error("JOB_RECORD_INVALID")
    for field in (
        "job_id",
        "job_kind",
        "input_ref",
        "created_by",
        "installation_id",
        "canonical_operation_id",
        "idempotency_key",
        "intent_payload_digest",
        "current_attempt_id",
        "lineage_id",
        "created_at",
        "updated_at",
    ):
        _nonempty(raw[field], "JOB_RECORD_INVALID")
    attempts = tuple(_attempt_from_dict(item) for item in raw["attempts"])
    if (
        attempts[-1].attempt_id != raw["current_attempt_id"]
        or attempts[-1].lineage_id != raw["lineage_id"]
        or attempts[-1].state is not state
    ):
        raise integrity_error("JOB_CURRENT_ATTEMPT_INVALID")
    if raw["result_ref"] is not None and (
        type(raw["result_ref"]) is not str or not raw["result_ref"]
    ):
        raise integrity_error("JOB_RESULT_REF_INVALID")
    _validate_policy(raw["frozen_policy"])
    partial_failures = _validate_partial_failures(raw["partial_failures"])
    return JobV1(
        job_id=raw["job_id"],
        job_kind=raw["job_kind"],
        input_ref=raw["input_ref"],
        frozen_policy=deepcopy(raw["frozen_policy"]),
        created_by=raw["created_by"],
        installation_id=raw["installation_id"],
        canonical_operation_id=raw["canonical_operation_id"],
        idempotency_key=raw["idempotency_key"],
        intent_payload_digest=raw["intent_payload_digest"],
        state=state,
        job_revision=raw["job_revision"],
        event_sequence=raw["event_sequence"],
        current_attempt_id=raw["current_attempt_id"],
        lineage_id=raw["lineage_id"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        result_ref=raw["result_ref"],
        partial_failures=deepcopy(partial_failures),
        attempts=attempts,
    )


def _validate_receipt_inheritance(
    job: JobV1,
    inheritance: ReceiptInheritanceV1,
) -> None:
    if len(job.attempts) < 2:
        raise integrity_error("JOB_RECEIPT_INHERITANCE_BINDING_MISMATCH")
    attempt = job.attempts[-1]
    predecessor = job.attempts[-2]
    predecessor_receipt = predecessor.operation_receipt
    comparisons = (
        predecessor_receipt is not None,
        inheritance.predecessor_attempt_id == predecessor.attempt_id,
        inheritance.job_id == job.job_id,
        inheritance.new_attempt_id == attempt.attempt_id,
        inheritance.lineage_id == attempt.lineage_id == job.lineage_id,
        inheritance.installation_id == job.installation_id,
        inheritance.canonical_operation_id == job.canonical_operation_id,
        inheritance.idempotency_key == job.idempotency_key,
        inheritance.intent_payload_digest == job.intent_payload_digest,
        inheritance.effect_fingerprint == attempt.effect_fingerprint,
        inheritance.account_ref == attempt.account_ref,
        inheritance.target_ref == attempt.target_ref,
        inheritance.uncertain_response_state == attempt.uncertain_response_state,
    )
    if not all(comparisons):
        raise integrity_error("JOB_RECEIPT_INHERITANCE_BINDING_MISMATCH")
    assert predecessor_receipt is not None
    if (
        inheritance.predecessor_operation_receipt_id
        != predecessor_receipt.operation_receipt_id
        or inheritance.predecessor_operation_receipt_digest
        != _operation_receipt_digest(predecessor_receipt)
        or inheritance.effect_profile != predecessor_receipt.effect_profile
        or inheritance.job_profile != predecessor_receipt.job_profile
        or inheritance.approval_profile != predecessor_receipt.approval_profile
    ):
        raise integrity_error("JOB_RECEIPT_INHERITANCE_BINDING_MISMATCH")


def _event_from_dict(value: object) -> EventV1:
    fields = frozenset(EventV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_EVENT_INVALID")
    try:
        state = JobState(raw["state"])
    except (TypeError, ValueError) as exc:
        raise integrity_error("JOB_EVENT_STATE_INVALID") from exc
    if (
        type(raw["sequence"]) is not int
        or raw["sequence"] < 1
        or type(raw["job_revision"]) is not int
        or raw["job_revision"] < 1
        or not isinstance(raw["partial_failures"], list)
    ):
        raise integrity_error("JOB_EVENT_INVALID")
    for field in (
        "event_id",
        "job_id",
        "attempt_id",
        "event_type",
        "created_at",
    ):
        _nonempty(raw[field], "JOB_EVENT_INVALID")
    lease_epoch = raw["lease_epoch"]
    worker_instance_id = raw["worker_instance_id"]
    if (lease_epoch is None) != (worker_instance_id is None):
        raise integrity_error("JOB_EVENT_LEASE_INVALID")
    if lease_epoch is not None and (
        type(lease_epoch) is not int
        or lease_epoch < 1
        or type(worker_instance_id) is not str
        or not worker_instance_id
    ):
        raise integrity_error("JOB_EVENT_LEASE_INVALID")
    partial_failures = _validate_partial_failures(raw["partial_failures"])
    return EventV1(
        event_id=raw["event_id"],
        job_id=raw["job_id"],
        attempt_id=raw["attempt_id"],
        sequence=raw["sequence"],
        job_revision=raw["job_revision"],
        event_type=raw["event_type"],
        state=state,
        created_at=raw["created_at"],
        result_ref=raw["result_ref"],
        partial_failures=deepcopy(partial_failures),
        reason_code=raw["reason_code"],
        lease_epoch=lease_epoch,
        worker_instance_id=worker_instance_id,
    )


def _migration_from_dict(value: object) -> MigrationReceiptV1:
    fields = frozenset(MigrationReceiptV1.__dataclass_fields__)
    raw = _strict_dict(value, fields, "JOB_MIGRATION_RECEIPT_INVALID")
    if (
        type(raw["source_schema"]) is not int
        or raw["source_schema"] not in {1, 2}
        or not isinstance(raw["imported_job_ids"], list)
        or any(type(item) is not str or not item for item in raw["imported_job_ids"])
    ):
        raise integrity_error("JOB_MIGRATION_RECEIPT_INVALID")
    for field in (
        "receipt_id",
        "source_path",
        "source_sha256",
        "committed_at",
    ):
        _nonempty(raw[field], "JOB_MIGRATION_RECEIPT_INVALID")
    return MigrationReceiptV1(
        receipt_id=raw["receipt_id"],
        source_path=raw["source_path"],
        source_sha256=raw["source_sha256"],
        source_schema=raw["source_schema"],
        imported_job_ids=tuple(raw["imported_job_ids"]),
        committed_at=raw["committed_at"],
    )


def _validate_policy(value: dict[str, object]) -> None:
    encoded = _canonical_bytes(value)
    if len(encoded) > MAX_POLICY_BYTES:
        raise integrity_error("JOB_FROZEN_POLICY_TOO_LARGE")
    forbidden = ("secret", "token", "api_key", "license_code", "transcript")

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in forbidden):
                    raise integrity_error("JOB_FORBIDDEN_PERSISTED_FIELD")
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


class _LedgerReadProjection:
    """Shared read/validation logic; no constructor, mutation or recovery API."""

    @staticmethod
    def _validate_head(value: object) -> dict[str, object]:
        raw = _strict_dict(
            value,
            frozenset({"epoch", "sequence", "head_hmac"}),
            "JOB_LEDGER_HIGH_WATER_INVALID",
        )
        if (
            type(raw["epoch"]) is not str
            or not raw["epoch"]
            or type(raw["sequence"]) is not int
            or raw["sequence"] < 0
            or type(raw["head_hmac"]) is not str
            or len(raw["head_hmac"]) != 64
        ):
            raise integrity_error("JOB_LEDGER_HIGH_WATER_INVALID")
        return deepcopy(raw)


    def _read_file(self, path: Path, maximum: int) -> bytes:
        _validate_no_follow_ancestors(path)
        _validate_owned_mode(path, mode=0o600, directory=False)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                    raise integrity_error("JOB_LEDGER_FILE_SIZE_INVALID")
                chunks: list[bytes] = []
                remaining = maximum + 1
                while remaining:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_READ_FAILED") from exc
        if len(data) > maximum:
            raise integrity_error("JOB_LEDGER_FILE_SIZE_INVALID")
        return data


    def _read_journal(
        self,
        epoch: object,
        *,
        path: Path | None = None,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        data = self._read_file(
            self.journal_path if path is None else path,
            MAX_SNAPSHOT_BYTES,
        )
        if not data:
            return [], {"epoch": epoch, "sequence": 0, "head_hmac": "0" * 64}
        if not data.endswith(b"\n"):
            raise integrity_error("JOB_LEDGER_JOURNAL_TRUNCATED")
        records: list[dict[str, object]] = []
        previous_hmac = "0" * 64
        for sequence, line in enumerate(data.splitlines(), start=1):
            if len(line) > MAX_RECORD_BYTES:
                raise integrity_error("JOB_LEDGER_RECORD_TOO_LARGE")
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise integrity_error("JOB_LEDGER_JSON_INVALID") from exc
            raw = _strict_dict(
                record,
                frozenset(
                    {
                        "epoch",
                        "sequence",
                        "previous_record_hash",
                        "payload",
                        "record_hmac",
                    }
                ),
                "JOB_LEDGER_RECORD_INVALID",
            )
            if _canonical_bytes(raw) != line:
                raise integrity_error("JOB_LEDGER_RECORD_NONCANONICAL")
            if (
                raw["epoch"] != epoch
                or raw["sequence"] != sequence
                or raw["previous_record_hash"] != previous_hmac
                or type(raw["record_hmac"]) is not str
                or len(raw["record_hmac"]) != 64
            ):
                raise integrity_error("JOB_LEDGER_HASH_CHAIN_INVALID")
            body = {
                "epoch": raw["epoch"],
                "sequence": raw["sequence"],
                "previous_record_hash": raw["previous_record_hash"],
                "payload": raw["payload"],
            }
            expected = hmac.new(
                self._hmac_key,
                _canonical_bytes(body),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, raw["record_hmac"]):
                raise integrity_error("JOB_LEDGER_HMAC_INVALID")
            records.append(raw)
            previous_hmac = raw["record_hmac"]
        return records, {
            "epoch": epoch,
            "sequence": len(records),
            "head_hmac": previous_hmac,
        }


    def _snapshot_payload(self) -> dict[str, object]:
        # A committed event always replaces its frozen JobV1. Unchanged jobs
        # and their event lists need not be serialized again on every commit.
        # The cache is process-local; durable bytes and commit ordering stay
        # identical, and journal replay rebuilds it on every reopen.
        cache = getattr(self, "_snapshot_fragments", None)
        if cache is None:
            cache = self._snapshot_fragments = {}
        jobs, events = [], {}
        for key in sorted(self._jobs):
            job = self._jobs[key]
            cached = cache.get(key)
            if cached is None or cached[0] is not job:
                cached = (job, job.as_dict(), [event.as_dict() for event in self._events[key]])
                cache[key] = cached
            jobs.append(cached[1])
            events[key] = cached[2]
        result = {
            "schema": JOB_LEDGER_SCHEMA,
            "schema_version": JOB_LEDGER_SCHEMA_VERSION,
            "head": deepcopy(self._head),
            "jobs": jobs,
            "events": events,
            "receipt_inheritances": [
                self._receipt_inheritances[key].as_dict()
                for key in sorted(self._receipt_inheritances)
            ],
            "migration_receipt": (
                None
                if self._migration_receipt is None
                else self._migration_receipt.as_dict()
            ),
        }
        if getattr(self, '_legacy_imports', None):
            result['legacy_imports'] = deepcopy(self._legacy_imports)
        return result


    def _validate_snapshot(self) -> None:
        data = self._read_file(self.snapshot_path, MAX_SNAPSHOT_BYTES)
        if not data.endswith(b"\n"):
            raise integrity_error("JOB_LEDGER_SNAPSHOT_TRUNCATED")
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise integrity_error("JOB_LEDGER_SNAPSHOT_INVALID") from exc
        if _canonical_bytes(parsed) + b"\n" != data:
            raise integrity_error("JOB_LEDGER_SNAPSHOT_NONCANONICAL")
        if parsed != self._snapshot_payload():
            raise integrity_error("JOB_LEDGER_SNAPSHOT_MISMATCH")


    def _apply_payload(self, payload: object) -> None:
        if not isinstance(payload, dict) or type(payload.get("event")) is not str:
            raise integrity_error("JOB_LEDGER_PAYLOAD_INVALID")
        if payload['event'] == 'legacy_job_import':
            raw = _strict_dict(payload, frozenset({'event', 'source', 'digest', 'job',
                'job_events', 'inheritances'}), 'JOB_IMPORT_INVALID')
            import re
            if (type(raw['source']) is not str or not re.fullmatch(r'[a-f0-9]{40,64}', raw['source'])
                    or type(raw['digest']) is not str or not re.fullmatch(r'[a-f0-9]{64}', raw['digest'])
                    or type(raw['job_events']) is not list or type(raw['inheritances']) is not list):
                raise integrity_error('JOB_IMPORT_INVALID')
            job = _job_from_dict(raw['job'])
            content = {k: raw[k] for k in ('job', 'job_events', 'inheritances')}
            if hashlib.sha256(_canonical_bytes(content)).hexdigest() != raw['digest']:
                raise integrity_error('JOB_IMPORT_INVALID')
            _validate_policy(job.frozen_policy)
            events = [_event_from_dict(e) for e in raw['job_events']]
            if (job.job_id in self._jobs or job.idempotency_key in self._idempotency
                    or len(events) != job.event_sequence or not events
                    or any(e.job_id != job.job_id or e.sequence != i or e.job_revision != i
                           or e.attempt_id not in {a.attempt_id for a in job.attempts}
                           for i, e in enumerate(events, 1))
                    or events[-1].attempt_id != job.current_attempt_id
                    or events[-1].job_revision != job.job_revision):
                raise integrity_error('JOB_IMPORT_CONFLICT')
            inheritances = [_receipt_inheritance_from_dict(i) for i in raw['inheritances']]
            for inheritance in inheritances:
                attempts = job.attempts
                index = next((i for i, a in enumerate(attempts) if a.attempt_id == inheritance.new_attempt_id), None)
                if index is None or index == 0:
                    raise integrity_error('JOB_IMPORT_INVALID')
                from dataclasses import replace
                _validate_receipt_inheritance(replace(job, attempts=attempts[:index+1],
                    current_attempt_id=attempts[index].attempt_id), inheritance)
            self._jobs[job.job_id] = job
            self._events[job.job_id] = events
            self._idempotency[job.idempotency_key] = (job.job_id, job.intent_payload_digest)
            for inheritance in inheritances:
                self._receipt_inheritances[(job.job_id, inheritance.new_attempt_id)] = inheritance
            if not hasattr(self, '_legacy_imports'): self._legacy_imports = {}
            self._legacy_imports[job.job_id] = {'source': raw['source'], 'digest': raw['digest']}
            return
        if payload["event"] == "migration_commit":
            raw_migration = _strict_dict(
                payload,
                frozenset({"event", "jobs", "job_events", "receipt"}),
                "JOB_MIGRATION_PAYLOAD_INVALID",
            )
            if (
                self._migration_receipt is not None
                or self._jobs
                or not isinstance(raw_migration["jobs"], list)
                or not isinstance(raw_migration["job_events"], list)
                or len(raw_migration["jobs"])
                != len(raw_migration["job_events"])
            ):
                raise integrity_error("JOB_MIGRATION_PAYLOAD_INVALID")
            receipt = _migration_from_dict(raw_migration["receipt"])
            jobs = [_job_from_dict(item) for item in raw_migration["jobs"]]
            events = [
                _event_from_dict(item) for item in raw_migration["job_events"]
            ]
            if (
                tuple(job.job_id for job in jobs) != receipt.imported_job_ids
                or len({job.job_id for job in jobs}) != len(jobs)
                or len({job.idempotency_key for job in jobs}) != len(jobs)
            ):
                raise integrity_error("JOB_MIGRATION_BINDING_INVALID")
            for job, event in zip(jobs, events):
                if (
                    job.job_revision != 1
                    or job.event_sequence != 1
                    or event.job_id != job.job_id
                    or event.attempt_id != job.current_attempt_id
                    or event.job_revision != 1
                    or event.sequence != 1
                ):
                    raise integrity_error("JOB_MIGRATION_BINDING_INVALID")
            for job, event in zip(jobs, events):
                self._jobs[job.job_id] = job
                self._events[job.job_id] = [event]
                self._idempotency[job.idempotency_key] = (
                    job.job_id,
                    job.intent_payload_digest,
                )
            self._migration_receipt = receipt
            return
        if frozenset(payload) == frozenset(
            {"event", "job", "job_event", "receipt_inheritance"}
        ):
            raw = payload
            inheritance = _receipt_inheritance_from_dict(
                raw["receipt_inheritance"]
            )
        else:
            raw = _strict_dict(
                payload,
                frozenset({"event", "job", "job_event"}),
                "JOB_LEDGER_PAYLOAD_INVALID",
            )
            inheritance = None
        if raw["event"] != "job_upsert":
            raise integrity_error("JOB_LEDGER_EVENT_INVALID")
        job = _job_from_dict(raw["job"])
        event = _event_from_dict(raw["job_event"])
        existing = self._jobs.get(job.job_id)
        new_attempt = (
            existing is not None
            and existing.current_attempt_id != job.current_attempt_id
        )
        predecessor_has_receipt = (
            new_attempt and existing.attempts[-1].operation_receipt is not None
        )
        if predecessor_has_receipt != (inheritance is not None):
            raise integrity_error("JOB_RECEIPT_INHERITANCE_REQUIRED")
        if inheritance is not None:
            _validate_receipt_inheritance(job, inheritance)
            inheritance_key = (job.job_id, job.current_attempt_id)
            if inheritance_key in self._receipt_inheritances:
                raise integrity_error("JOB_RECEIPT_INHERITANCE_DUPLICATE")
        expected_revision = 1 if existing is None else existing.job_revision + 1
        expected_sequence = 1 if existing is None else existing.event_sequence + 1
        if (
            event.job_id != job.job_id
            or event.attempt_id != job.current_attempt_id
            or event.job_revision != job.job_revision
            or event.sequence != job.event_sequence
            or job.job_revision != expected_revision
            or job.event_sequence != expected_sequence
        ):
            raise integrity_error("JOB_LEDGER_REVISION_INVALID")
        binding = self._idempotency.get(job.idempotency_key)
        if binding is not None and binding != (
            job.job_id,
            job.intent_payload_digest,
        ):
            raise integrity_error("JOB_LEDGER_IDEMPOTENCY_CORRUPT")
        self._jobs[job.job_id] = job
        self._events.setdefault(job.job_id, []).append(event)
        self._idempotency[job.idempotency_key] = (
            job.job_id,
            job.intent_payload_digest,
        )
        if inheritance is not None:
            self._receipt_inheritances[inheritance_key] = inheritance



class JobLedgerStore(_LedgerReadProjection):
    """Append-only job authority scoped to one explicitly injected data root."""

    def __init__(
        self,
        data_root: Path,
        anchor_store: IntegrityAnchorStore,
        *,
        recovery_at: str | None = None,
    ) -> None:
        if not isinstance(data_root, Path) or not data_root.is_absolute():
            raise integrity_error("JOB_DATA_ROOT_MUST_BE_EXPLICIT_ABSOLUTE")
        if anchor_store is None:
            raise integrity_error("JOB_HIGH_WATER_ANCHOR_REQUIRED")
        self.data_root = data_root
        self.directory = data_root / "job-ledger-v1"
        self.journal_path = self.directory / "journal.jsonl"
        self.pending_path = self.directory / "journal.pending"
        self.lock_path = self.directory / "transaction.lock"
        self.snapshot_path = self.directory / "snapshot.json"
        self._anchor_store = anchor_store
        self._lock = RLock()
        self._jobs: dict[str, JobV1] = {}
        self._events: dict[str, list[EventV1]] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._receipt_inheritances: dict[
            tuple[str, str], ReceiptInheritanceV1
        ] = {}
        self._migration_receipt: MigrationReceiptV1 | None = None
        self._lease_fencing_required = False
        if recovery_at is not None:
            _nonempty(recovery_at, "JOB_RECOVERY_TIME_INVALID")
        key = anchor_store.hmac_key()
        if type(key) is not bytes or len(key) < 32:
            raise integrity_error("JOB_LEDGER_HMAC_KEY_INVALID")
        self._hmac_key = key

        _validate_no_follow_ancestors(data_root)
        if not data_root.exists():
            try:
                data_root.mkdir(mode=0o700, parents=True)
                os.chmod(data_root, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("JOB_DATA_ROOT_CREATE_FAILED") from exc
        _validate_owned_mode(data_root, mode=0o700, directory=True)

        _validate_no_follow_ancestors(self.directory)
        if not self.directory.exists():
            try:
                self.directory.mkdir(mode=0o700)
                os.chmod(self.directory, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("JOB_LEDGER_DIRECTORY_CREATE_FAILED") from exc
        _validate_owned_mode(self.directory, mode=0o700, directory=True)

        created_journal = self._create_file_if_missing(self.journal_path)
        created_snapshot = self._create_file_if_missing(self.snapshot_path)
        self._create_file_if_missing(self.lock_path)
        anchor = anchor_store.load()
        if anchor is None:
            if self.pending_path.exists() or self.pending_path.is_symlink():
                raise integrity_error("JOB_LEDGER_PENDING_WITHOUT_ANCHOR")
            if not created_journal or not created_snapshot:
                raise integrity_error("JOB_LEDGER_HIGH_WATER_MISSING")
            self._head = {
                "epoch": uuid4().hex,
                "sequence": 0,
                "head_hmac": "0" * 64,
            }
            self._write_snapshot()
            try:
                anchor_store.compare_and_set(None, self._head)
            except Exception as exc:  # noqa: BLE001 - injected secure seam
                raise integrity_error("JOB_LEDGER_HIGH_WATER_WRITE_FAILED") from exc
        else:
            with self._file_transaction_lock():
                anchored_head = self._validate_head(
                    self._anchor_store.load()
                )
                if (
                    created_journal
                    and created_snapshot
                    and anchored_head["sequence"] == 0
                    and anchored_head["head_hmac"] == "0" * 64
                ):
                    self._head = anchored_head
                    self._write_snapshot()
                    return
                recovered_pending = self._recover_pending_locked(
                    anchored_head
                )
                records, computed_head = self._read_journal(
                    anchored_head["epoch"]
                )
                if computed_head != anchored_head:
                    raise integrity_error("JOB_LEDGER_HIGH_WATER_ROLLBACK")
                self._head = computed_head
                for record in records:
                    self._apply_payload(record["payload"])
                if recovered_pending:
                    try:
                        self._validate_snapshot()
                    except JobLedgerError as exc:
                        if exc.reason_code not in {
                            "JOB_LEDGER_SNAPSHOT_TRUNCATED",
                            "JOB_LEDGER_SNAPSHOT_INVALID",
                            "JOB_LEDGER_SNAPSHOT_NONCANONICAL",
                            "JOB_LEDGER_SNAPSHOT_MISMATCH",
                        }:
                            raise
                        self._write_snapshot()
                else:
                    self._validate_snapshot()
            if recovery_at is not None:
                self.recover_orphaned_running(changed_at=recovery_at)

    def _create_file_if_missing(self, path: Path) -> bool:
        _validate_no_follow_ancestors(path)
        if path.exists() or path.is_symlink():
            _validate_owned_mode(path, mode=0o600, directory=False)
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
            os.close(descriptor)
            os.chmod(path, 0o600, follow_symlinks=False)
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_FILE_CREATE_FAILED") from exc
        return True


    @property
    def head(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._head)

    @property
    def migration_receipt(self) -> MigrationReceiptV1 | None:
        with self._lock:
            return deepcopy(self._migration_receipt)



    def _fsync_directory(self) -> None:
        directory_descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def _recover_pending_locked(
        self,
        anchored_head: dict[str, object],
    ) -> bool:
        _validate_no_follow_ancestors(self.pending_path)
        if not self.pending_path.exists() and not self.pending_path.is_symlink():
            return False
        _validate_owned_mode(self.pending_path, mode=0o600, directory=False)
        pending_records, pending_head = self._read_journal(
            anchored_head["epoch"],
            path=self.pending_path,
        )
        if pending_head != anchored_head or not pending_records:
            raise integrity_error("JOB_LEDGER_PENDING_HEAD_MISMATCH")
        formal_records, formal_head = self._read_journal(
            anchored_head["epoch"]
        )
        pending_bytes = self._read_file(
            self.pending_path,
            MAX_SNAPSHOT_BYTES,
        )
        formal_bytes = self._read_file(
            self.journal_path,
            MAX_SNAPSHOT_BYTES,
        )
        if formal_head == anchored_head:
            if formal_bytes != pending_bytes:
                raise integrity_error("JOB_LEDGER_PENDING_DIVERGED")
            self.pending_path.unlink()
            self._fsync_directory()
            return False
        if (
            formal_head["sequence"] >= anchored_head["sequence"]
            or not pending_bytes.startswith(formal_bytes)
            or len(pending_records) <= len(formal_records)
        ):
            raise integrity_error("JOB_LEDGER_PENDING_DIVERGED")
        try:
            os.replace(self.pending_path, self.journal_path)
            self._fsync_directory()
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_PENDING_RECOVERY_FAILED") from exc
        return True


    def _write_snapshot(self) -> None:
        encoded = _canonical_bytes(self._snapshot_payload()) + b"\n"
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise integrity_error("JOB_LEDGER_SNAPSHOT_TOO_LARGE")
        _validate_no_follow_ancestors(self.directory)
        _validate_owned_mode(self.directory, mode=0o700, directory=True)
        name = f".snapshot.{secrets.token_hex(16)}.tmp"
        temporary_path = self.directory / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(temporary_path, flags, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("short snapshot write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_path, self.snapshot_path)
            os.chmod(self.snapshot_path, 0o600, follow_symlinks=False)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_SNAPSHOT_WRITE_FAILED") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass



    def commit_migration(
        self,
        *,
        jobs: tuple[JobV1, ...],
        events: tuple[EventV1, ...],
        receipt: MigrationReceiptV1,
    ) -> MigrationReceiptV1:
        """Commit the full legacy import and its receipt in one journal record."""
        if not isinstance(receipt, MigrationReceiptV1):
            raise integrity_error("JOB_MIGRATION_RECEIPT_INVALID")
        if any(not isinstance(job, JobV1) for job in jobs) or any(
            not isinstance(event, EventV1) for event in events
        ):
            raise integrity_error("JOB_MIGRATION_PAYLOAD_INVALID")
        for job in jobs:
            _validate_policy(job.frozen_policy)
        with self._lock:
            if self._migration_receipt is not None:
                if self._migration_receipt == receipt:
                    return deepcopy(self._migration_receipt)
                raise conflict("MIGRATION_ALREADY_COMMITTED")
            if self._jobs:
                raise conflict("MIGRATION_LEDGER_NOT_EMPTY")
            self._append_payload(
                {
                    "event": "migration_commit",
                    "jobs": [job.as_dict() for job in jobs],
                    "job_events": [event.as_dict() for event in events],
                    "receipt": receipt.as_dict(),
                }
            )
            return deepcopy(self._migration_receipt)

    def import_verified_job(self, *, source, job, events, inheritances):
        """Copy an authenticated old job, preserving its complete frozen record.

        Each durable item can be retried after interruption. A later change to
        that job in this ledger is never overwritten by a repeated import.
        Caller must authenticate the source and hold the product write barrier.
        """
        content = {'job': job, 'job_events': events, 'inheritances': inheritances}
        digest = hashlib.sha256(_canonical_bytes(content)).hexdigest()
        payload = dict(content, event='legacy_job_import', source=source, digest=digest)
        with self._lock, self._transaction_guard():
            marker = getattr(self, '_legacy_imports', {}).get(job['job_id'])
            if marker is not None:
                if marker['digest'] != digest:
                    raise integrity_error('JOB_IMPORT_CONFLICT')
                return False
            # Validate before journal/anchor mutation, including all conflicts.
            shadow = _LedgerReadProjection()
            for name in ('_jobs', '_events', '_idempotency', '_receipt_inheritances', '_migration_receipt'):
                setattr(shadow, name, deepcopy(getattr(self, name)))
            shadow._legacy_imports = deepcopy(getattr(self, '_legacy_imports', {}))
            shadow._apply_payload(payload)
            self._append_payload_locked(payload)
            return True

    def _append_payload(self, payload: dict[str, object]) -> None:
        with self._transaction_guard():
            self._append_payload_locked(payload)

    @contextmanager
    def _file_transaction_lock(self):
        _validate_no_follow_ancestors(self.lock_path)
        _validate_owned_mode(self.lock_path, mode=0o600, directory=False)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags)
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_LOCK_UNAVAILABLE") from exc
        acquired = False
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                acquired = True
            except BlockingIOError as exc:
                raise resource_busy("JOB_LEDGER_TRANSACTION_BUSY") from exc
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _transaction_guard(self):
        with self._file_transaction_lock():
            anchored = self._anchor_store.load()
            if anchored is None:
                raise integrity_error("JOB_LEDGER_HIGH_WATER_MISSING")
            anchored_head = self._validate_head(anchored)
            _, journal_head = self._read_journal(anchored_head["epoch"])
            if anchored_head != self._head or journal_head != self._head:
                raise integrity_error("JOB_LEDGER_STALE_INSTANCE")
            if self.pending_path.exists() or self.pending_path.is_symlink():
                raise integrity_error("JOB_LEDGER_PENDING_RECOVERY_REQUIRED")
            yield

    def _append_payload_locked(self, payload: dict[str, object]) -> None:
        old_head = deepcopy(self._head)
        body = {
            "epoch": old_head["epoch"],
            "sequence": old_head["sequence"] + 1,
            "previous_record_hash": old_head["head_hmac"],
            "payload": deepcopy(payload),
        }
        record_hmac = hmac.new(
            self._hmac_key,
            _canonical_bytes(body),
            hashlib.sha256,
        ).hexdigest()
        record = {**body, "record_hmac": record_hmac}
        encoded = _canonical_bytes(record) + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise integrity_error("JOB_LEDGER_RECORD_TOO_LARGE")
        current_journal = self._read_file(
            self.journal_path,
            MAX_SNAPSHOT_BYTES,
        )
        candidate_journal = current_journal + encoded
        if len(candidate_journal) > MAX_SNAPSHOT_BYTES:
            raise integrity_error("JOB_LEDGER_FILE_SIZE_INVALID")
        _validate_no_follow_ancestors(self.directory)
        _validate_owned_mode(self.directory, mode=0o700, directory=True)
        _validate_no_follow_ancestors(self.journal_path)
        _validate_owned_mode(self.journal_path, mode=0o600, directory=False)
        _validate_no_follow_ancestors(self.pending_path)
        if self.pending_path.exists() or self.pending_path.is_symlink():
            raise integrity_error("JOB_LEDGER_PENDING_EXISTS")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        pending_identity = None
        local_anchor = getattr(getattr(self._anchor_store, 'keys', None), 'storage_scheme', None) == 'local-integrity-v1'
        try:
            descriptor = os.open(self.pending_path, flags, 0o600)
            try:
                pending_identity = os.fstat(descriptor)
                offset = 0
                while offset < len(candidate_journal):
                    written = os.write(
                        descriptor,
                        candidate_journal[offset:],
                    )
                    if written <= 0:
                        raise OSError("short pending journal write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if local_anchor and pending_identity is not None:
                # This incomplete file was created by this request, before any
                # anchor write. Never remove an existing or substituted file.
                try:
                    present = self.pending_path.lstat()
                    if ((present.st_dev, present.st_ino) == (pending_identity.st_dev, pending_identity.st_ino)
                            and stat.S_ISREG(present.st_mode) and present.st_nlink == 1):
                        self.pending_path.unlink()
                        self._fsync_directory()
                except OSError:
                    pass
            raise integrity_error("JOB_LEDGER_PENDING_WRITE_FAILED") from exc
        try:
            self._fsync_directory()
        except OSError as exc:
            try:
                self.pending_path.unlink()
            except OSError:
                pass
            raise integrity_error("JOB_LEDGER_PENDING_WRITE_FAILED") from exc
        new_head = {
            "epoch": old_head["epoch"],
            "sequence": body["sequence"],
            "head_hmac": record_hmac,
        }
        try:
            self._anchor_store.compare_and_set(old_head, new_head)
        except Exception as exc:  # noqa: BLE001 - injected secure seam
            if local_anchor:
                try:
                    unchanged = self._anchor_store.load() == old_head
                except Exception:
                    unchanged = False
                if not unchanged:
                    # A failed fsync can follow a successful atomic replacement.
                    # Keep the candidate journal until the confirmed head can
                    # recover it; deleting it here could lose a committed job.
                    raise integrity_error('JOB_LEDGER_HIGH_WATER_WRITE_UNCERTAIN') from exc
            try:
                self.pending_path.unlink()
                self._fsync_directory()
            except OSError as cleanup_exc:
                raise integrity_error(
                    "JOB_LEDGER_PENDING_CLEANUP_FAILED"
                ) from cleanup_exc
            raise integrity_error("JOB_LEDGER_HIGH_WATER_WRITE_FAILED") from exc
        self._head = new_head
        self._apply_payload(payload)
        self._write_snapshot()
        try:
            os.replace(self.pending_path, self.journal_path)
            self._fsync_directory()
        except OSError as exc:
            raise integrity_error("JOB_LEDGER_JOURNAL_COMMIT_FAILED") from exc

    def create_job(
        self,
        *,
        job_id: str,
        attempt_id: str,
        lineage_id: str,
        job_kind: str,
        input_ref: str,
        frozen_policy: dict[str, object],
        created_by: str,
        installation_id: str,
        canonical_operation_id: str,
        idempotency_key: str,
        intent_payload_digest: str,
        created_at: str,
        effect_fingerprint: str | None,
        account_ref: str | None,
        target_ref: str | None,
        uncertain_response_state: str | None,
    ) -> JobV1:
        values = {
            "job_id": job_id,
            "attempt_id": attempt_id,
            "lineage_id": lineage_id,
            "job_kind": job_kind,
            "input_ref": input_ref,
            "created_by": created_by,
            "installation_id": installation_id,
            "canonical_operation_id": canonical_operation_id,
            "idempotency_key": idempotency_key,
            "intent_payload_digest": intent_payload_digest,
            "created_at": created_at,
        }
        for field, value in values.items():
            _nonempty(value, f"JOB_{field.upper()}_INVALID")
        if not isinstance(frozen_policy, dict):
            raise integrity_error("JOB_FROZEN_POLICY_INVALID")
        _validate_policy(frozen_policy)
        with self._lock:
            binding = self._idempotency.get(idempotency_key)
            if binding is not None:
                bound_job_id, bound_digest = binding
                if bound_digest != intent_payload_digest:
                    raise conflict("IDEMPOTENCY_CONFLICT")
                return deepcopy(self._jobs[bound_job_id])
            if job_id in self._jobs:
                raise conflict("JOB_ID_CONFLICT")
            attempt = AttemptV1(
                attempt_id=attempt_id,
                lineage_id=lineage_id,
                attempt_number=1,
                state=JobState.PREPARED,
                lease_epoch=0,
                worker_instance_id=None,
                operation_receipt=None,
                effect_fingerprint=effect_fingerprint,
                account_ref=account_ref,
                target_ref=target_ref,
                uncertain_response_state=uncertain_response_state,
                created_at=created_at,
                updated_at=created_at,
            )
            job = JobV1(
                job_id=job_id,
                job_kind=job_kind,
                input_ref=input_ref,
                frozen_policy=deepcopy(frozen_policy),
                created_by=created_by,
                installation_id=installation_id,
                canonical_operation_id=canonical_operation_id,
                idempotency_key=idempotency_key,
                intent_payload_digest=intent_payload_digest,
                state=JobState.PREPARED,
                job_revision=1,
                event_sequence=1,
                current_attempt_id=attempt_id,
                lineage_id=lineage_id,
                created_at=created_at,
                updated_at=created_at,
                result_ref=None,
                partial_failures=(),
                attempts=(attempt,),
            )
            event = EventV1(
                event_id=f"{job_id}:event:1",
                job_id=job_id,
                attempt_id=attempt_id,
                sequence=1,
                job_revision=1,
                event_type="prepared",
                state=JobState.PREPARED,
                created_at=created_at,
            )
            self._append_payload(
                {
                    "event": "job_upsert",
                    "job": job.as_dict(),
                    "job_event": event.as_dict(),
                }
            )
            return deepcopy(self._jobs[job_id])

    def _require_revision(self, job_id: str, expected_revision: int) -> JobV1:
        job = self._jobs.get(job_id)
        if job is None:
            raise conflict("JOB_NOT_FOUND")
        if type(expected_revision) is not int or job.job_revision != expected_revision:
            raise conflict("JOB_REVISION_CONFLICT")
        return job

    @staticmethod
    def _replace_current_attempt(job: JobV1, attempt: AttemptV1) -> tuple[AttemptV1, ...]:
        return (*job.attempts[:-1], attempt)

    def enable_lease_fencing(self) -> None:
        with self._lock:
            self._lease_fencing_required = True

    @staticmethod
    def _lease_token_for_job(job: JobV1) -> LeaseToken:
        attempt = job.attempts[-1]
        if attempt.lease_epoch < 1 or attempt.worker_instance_id is None:
            raise integrity_error("JOB_HAS_NO_ACTIVE_LEASE")
        return LeaseToken(
            job_id=job.job_id,
            lease_epoch=attempt.lease_epoch,
            worker_instance_id=attempt.worker_instance_id,
        )

    def current_lease_token(self, job_id: str) -> LeaseToken:
        with self._lock:
            return self._lease_token_for_job(self._require_revisionless(job_id))

    def _require_revisionless(self, job_id: str) -> JobV1:
        job = self._jobs.get(job_id)
        if job is None:
            raise conflict("JOB_NOT_FOUND")
        return job

    def verify_worker_lease(
        self,
        job_id: str,
        lease_token: LeaseToken,
        *,
        mutation_kind: str,
    ) -> None:
        with self._lock:
            JobLeaseFencer.verify(
                self._require_revisionless(job_id),
                lease_token,
                mutation_kind=mutation_kind,
            )

    def append_worker_event(
        self,
        job_id: str,
        *,
        expected_revision: int,
        lease_token: LeaseToken,
        event_type: str,
        changed_at: str,
    ) -> JobV1:
        _nonempty(event_type, "JOB_EVENT_TYPE_INVALID")
        _nonempty(changed_at, "JOB_CHANGED_AT_INVALID")
        with self._lock:
            job = self._require_revision(job_id, expected_revision)
            if job.state not in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                raise conflict("JOB_WORKER_EVENT_STATE_INVALID")
            JobLeaseFencer.verify(
                job,
                lease_token,
                mutation_kind="event",
            )
            revision = job.job_revision + 1
            sequence = job.event_sequence + 1
            attempt = replace(job.attempts[-1], updated_at=changed_at)
            updated = replace(
                job,
                job_revision=revision,
                event_sequence=sequence,
                updated_at=changed_at,
                attempts=self._replace_current_attempt(job, attempt),
            )
            event = EventV1(
                event_id=f"{job_id}:event:{sequence}",
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                sequence=sequence,
                job_revision=revision,
                event_type=event_type,
                state=job.state,
                created_at=changed_at,
                lease_epoch=lease_token.lease_epoch,
                worker_instance_id=lease_token.worker_instance_id,
            )
            self._append_payload(
                {
                    "event": "job_upsert",
                    "job": updated.as_dict(),
                    "job_event": event.as_dict(),
                }
            )
            return deepcopy(self._jobs[job_id])

    def freeze_gui_policy(self, job_id, *, expected_revision, policy, changed_at):
        """Bind previously unspecified GUI execution settings before a worker starts.

        Keep the admitted input and its digest intact; the explicit event binds
        the execution snapshot separately. Filled settings cannot be changed.
        """
        from ..task_queue_store import _policy_from_dict, _policy_to_dict, SCHEMA_VERSION
        normalized = _policy_to_dict(_policy_from_dict(policy, schema_version=SCHEMA_VERSION))
        if normalized is None:
            raise integrity_error("GUI_POLICY_INVALID")
        _nonempty(changed_at, "JOB_CHANGED_AT_INVALID")
        with self._lock:
            job = self._require_revision(job_id, expected_revision)
            if job.created_by != "gui" or job.job_kind != "gui_transcription":
                raise conflict("GUI_POLICY_OWNER_INVALID")
            gui = job.frozen_policy.get("gui_task", {})
            previous = job.frozen_policy.get("gui_execution_policy", gui.get("execution_policy"))
            if previous is not None:
                previous = _policy_to_dict(_policy_from_dict(previous, schema_version=SCHEMA_VERSION))
            if previous == normalized:
                return deepcopy(job)
            if previous and previous.get("local_only") and not normalized.get("local_only"):
                raise conflict("GUI_POLICY_IMMUTABLE")
            # Old batches may already be queued with these two settings unset.
            # Bind only empty fields once; every admitted value remains immutable.
            late_binding_fields = {"engine_name", "prefer_subtitles"}
            binding_model = bool(previous and job.state == JobState.QUEUED
                and previous.get("engine_name") is None
                and normalized.get("engine_name") is not None
                and all(previous.get(key) == normalized.get(key)
                        or (key in late_binding_fields and previous.get(key) is None)
                        for key in previous.keys() | normalized.keys()))
            editing_draft = bool(normalized.get("requested_outputs") is not None
                and (previous is None or previous.get("origin") == "single_link")
                and job.state in {JobState.PREPARED, JobState.FAILED, JobState.CANCELLED, JobState.NEEDS_INPUT, JobState.RECOVERY_REQUIRED})
            if not binding_model and job.state not in {JobState.PREPARED, JobState.FAILED, JobState.CANCELLED, JobState.NEEDS_INPUT, JobState.RECOVERY_REQUIRED}:
                raise conflict("GUI_POLICY_STATE_INVALID")
            if normalized["stable_key"] != gui.get("stable_key", ""):
                raise conflict("GUI_POLICY_IDENTITY_INVALID")
            if previous is not None and not (binding_model or editing_draft) and any(
                value != normalized.get(key)
                for key, value in previous.items()
                if not (key in {"engine_name", "prefer_subtitles"} and value is None)
            ):
                raise conflict("GUI_POLICY_IMMUTABLE")
            frozen = deepcopy(job.frozen_policy)
            frozen["gui_execution_policy"] = normalized
            sequence, revision = job.event_sequence + 1, job.job_revision + 1
            updated = replace(job, frozen_policy=frozen, job_revision=revision,
                event_sequence=sequence, updated_at=changed_at)
            event = EventV1(event_id=f"{job_id}:event:{sequence}", job_id=job_id,
                attempt_id=job.attempts[-1].attempt_id, sequence=sequence,
                job_revision=revision, event_type="gui_policy_frozen", state=job.state,
                created_at=changed_at)
            self._append_payload({"event":"job_upsert", "job":updated.as_dict(), "job_event":event.as_dict()})
            return deepcopy(self._jobs[job_id])

    def transition(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_state: JobState,
        changed_at: str,
        result_ref: str | None = None,
        partial_failures: tuple[dict[str, object], ...] = (),
        reason_code: str | None = None,
        lease_token: LeaseToken | None = None,
    ) -> JobV1:
        if not isinstance(new_state, JobState):
            raise integrity_error("JOB_STATE_INVALID")
        _nonempty(changed_at, "JOB_CHANGED_AT_INVALID")
        normalized_partial_failures = _validate_partial_failures(partial_failures)
        with self._lock:
            job = self._require_revision(job_id, expected_revision)
            if job.state in TERMINAL_STATES:
                raise conflict("TERMINAL_STATE_IMMUTABLE")
            # Narrow legacy repair: a workspace preparation already published
            # its durable result but old releases left its job in NEEDS_INPUT.
            # Other waiting jobs retain their existing transition contract.
            completed_handoff = (
                job.job_kind == 'workspace_conversation'
                and job.state is JobState.NEEDS_INPUT and new_state is JobState.SUCCEEDED
                and reason_code == 'WORKSPACE_HANDOFF_PREPARATION_COMPLETED'
                and isinstance(result_ref, str) and result_ref.startswith('workspace-result:sha256:')
                and result_ref == job.result_ref
            )
            if new_state not in ALLOWED_TRANSITIONS[job.state] and not completed_handoff:
                raise conflict("JOB_STATE_TRANSITION_INVALID")
            active_attempt = job.attempts[-1]
            fence_required = (
                self._lease_fencing_required
                and active_attempt.worker_instance_id is not None
                and new_state is not JobState.CANCEL_REQUESTED
            )
            if fence_required and lease_token is None:
                raise integrity_error("JOB_LEASE_TOKEN_REQUIRED")
            if completed_handoff:
                # This is a fenced ledger repair, not a late worker result.
                # Keep the original result reference and current attempt token.
                if lease_token != self._lease_token_for_job(job):
                    raise conflict("JOB_LEASE_FENCE_REJECTED")
            elif lease_token is not None:
                JobLeaseFencer.verify(
                    job,
                    lease_token,
                    mutation_kind=(
                        "result"
                        if new_state in TERMINAL_STATES or result_ref is not None
                        else "event"
                    ),
                )
            attempt = replace(
                job.attempts[-1],
                state=new_state,
                worker_instance_id=(
                    None
                    if new_state is JobState.RECOVERY_REQUIRED
                    else job.attempts[-1].worker_instance_id
                ),
                updated_at=changed_at,
            )
            revision = job.job_revision + 1
            sequence = job.event_sequence + 1
            updated = replace(
                job,
                state=new_state,
                job_revision=revision,
                event_sequence=sequence,
                updated_at=changed_at,
                result_ref=result_ref,
                partial_failures=deepcopy(normalized_partial_failures),
                attempts=self._replace_current_attempt(job, attempt),
            )
            event = EventV1(
                event_id=f"{job_id}:event:{sequence}",
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                sequence=sequence,
                job_revision=revision,
                event_type="state_changed",
                state=new_state,
                created_at=changed_at,
                result_ref=result_ref,
                partial_failures=deepcopy(normalized_partial_failures),
                reason_code=reason_code,
                lease_epoch=(
                    None if lease_token is None else lease_token.lease_epoch
                ),
                worker_instance_id=(
                    None
                    if lease_token is None
                    else lease_token.worker_instance_id
                ),
            )
            self._append_payload(
                {
                    "event": "job_upsert",
                    "job": updated.as_dict(),
                    "job_event": event.as_dict(),
                }
            )
            return deepcopy(self._jobs[job_id])

    def recover_orphaned_running(self, *, changed_at: str) -> tuple[JobV1, ...]:
        """Fence dead workers without replaying their operation receipts."""
        _nonempty(changed_at, "JOB_RECOVERY_TIME_INVALID")
        recovered: list[JobV1] = []
        with self._lock:
            running = [
                self._jobs[key]
                for key in sorted(self._jobs)
                if self._jobs[key].state is JobState.RUNNING
            ]
            for job in running:
                lease_token = self._lease_token_for_job(job)
                recovered.append(
                    self.transition(
                        job.job_id,
                        expected_revision=job.job_revision,
                        new_state=JobState.RECOVERY_REQUIRED,
                        changed_at=changed_at,
                        reason_code="OWNER_LOST",
                        lease_token=lease_token,
                    )
                )
        return tuple(recovered)

    @staticmethod
    def _validate_receipt(job: JobV1, receipt: OperationReceiptV1) -> None:
        attempt = job.attempts[-1]
        if (
            receipt.job_id != job.job_id
            or receipt.attempt_id != attempt.attempt_id
            or receipt.lineage_id != attempt.lineage_id
            or receipt.installation_id != job.installation_id
            or receipt.canonical_operation_id != job.canonical_operation_id
            or receipt.idempotency_key != job.idempotency_key
            or receipt.intent_payload_digest != job.intent_payload_digest
        ):
            raise integrity_error("JOB_OPERATION_RECEIPT_BINDING_MISMATCH")

    @staticmethod
    def _validate_inherited_receipt(
        attempt: AttemptV1,
        receipt: OperationReceiptV1,
        inheritance: ReceiptInheritanceV1 | None,
    ) -> None:
        if inheritance is None:
            return
        if (
            receipt.effect_profile != inheritance.effect_profile
            or receipt.job_profile != inheritance.job_profile
            or receipt.approval_profile != inheritance.approval_profile
            or receipt.installation_id != inheritance.installation_id
            or receipt.canonical_operation_id
            != inheritance.canonical_operation_id
            or receipt.idempotency_key != inheritance.idempotency_key
            or receipt.intent_payload_digest
            != inheritance.intent_payload_digest
            or attempt.effect_fingerprint != inheritance.effect_fingerprint
            or attempt.account_ref != inheritance.account_ref
            or attempt.target_ref != inheritance.target_ref
            or attempt.uncertain_response_state
            != inheritance.uncertain_response_state
        ):
            raise integrity_error("JOB_RECEIPT_INHERITANCE_BINDING_MISMATCH")

    def claim(
        self,
        job_id: str,
        *,
        expected_revision: int,
        worker_instance_id: str,
        operation_receipt: OperationReceiptV1 | None,
        approved_external_claim: object | None = None,
        changed_at: str,
    ) -> JobV1:
        _nonempty(worker_instance_id, "JOB_WORKER_INSTANCE_INVALID")
        _nonempty(changed_at, "JOB_CHANGED_AT_INVALID")
        with self._lock:
            job = self._require_revision(job_id, expected_revision)
            if job.state is not JobState.QUEUED:
                raise conflict("JOB_NOT_CLAIMABLE")
            existing_receipt = job.attempts[-1].operation_receipt
            receipt_inheritance = self._receipt_inheritances.get(
                (job.job_id, job.current_attempt_id)
            )
            if (
                existing_receipt is None
                and receipt_inheritance is None
                and operation_receipt is not None
            ):
                from ..link_transcription_application import (
                    _validate_job_approval_claim,
                )

                if not _validate_job_approval_claim(
                    approved_external_claim,
                    job=job,
                    operation_receipt=operation_receipt,
                ):
                    raise integrity_error(
                        "AUTHORITATIVE_OPERATION_RECEIPT_ISSUANCE_UNAVAILABLE"
                    )
            if (
                existing_receipt is not None
                and operation_receipt is not None
                and operation_receipt != existing_receipt
            ):
                raise integrity_error("JOB_OPERATION_RECEIPT_IMMUTABLE")
            if operation_receipt is not None:
                if not isinstance(operation_receipt, OperationReceiptV1):
                    raise integrity_error("JOB_OPERATION_RECEIPT_INVALID")
                self._validate_receipt(job, operation_receipt)
                self._validate_inherited_receipt(
                    job.attempts[-1],
                    operation_receipt,
                    receipt_inheritance,
                )
            elif (
                existing_receipt is None
                and receipt_inheritance is not None
            ):
                raise integrity_error("JOB_INHERITED_RECEIPT_REQUIRED")
            authoritative_receipt = (
                existing_receipt
                if operation_receipt is None
                else operation_receipt
            )
            attempt = replace(
                job.attempts[-1],
                state=JobState.RUNNING,
                lease_epoch=job.attempts[-1].lease_epoch + 1,
                worker_instance_id=worker_instance_id,
                operation_receipt=authoritative_receipt,
                updated_at=changed_at,
            )
            revision = job.job_revision + 1
            sequence = job.event_sequence + 1
            updated = replace(
                job,
                state=JobState.RUNNING,
                job_revision=revision,
                event_sequence=sequence,
                updated_at=changed_at,
                attempts=self._replace_current_attempt(job, attempt),
            )
            event = EventV1(
                event_id=f"{job_id}:event:{sequence}",
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                sequence=sequence,
                job_revision=revision,
                event_type="claimed",
                state=JobState.RUNNING,
                created_at=changed_at,
                lease_epoch=attempt.lease_epoch,
                worker_instance_id=attempt.worker_instance_id,
            )
            self._append_payload(
                {
                    "event": "job_upsert",
                    "job": updated.as_dict(),
                    "job_event": event.as_dict(),
                }
            )
            return deepcopy(self._jobs[job_id])

    def retry_attempt(
        self,
        job_id: str,
        *,
        expected_revision: int,
        new_attempt_id: str,
        changed_at: str,
        enqueue: bool = True,
    ) -> JobV1:
        """Create a new attempt without mutating the terminal predecessor."""
        _nonempty(new_attempt_id, "JOB_ATTEMPT_ID_INVALID")
        _nonempty(changed_at, "JOB_CHANGED_AT_INVALID")
        with self._lock:
            job = self._require_revision(job_id, expected_revision)
            if job.state not in TERMINAL_STATES:
                raise conflict("JOB_RETRY_REQUIRES_TERMINAL_ATTEMPT")
            if any(
                attempt.attempt_id == new_attempt_id for attempt in job.attempts
            ):
                raise conflict("JOB_ATTEMPT_ID_CONFLICT")
            predecessor = job.attempts[-1]
            predecessor_receipt = predecessor.operation_receipt
            receipt_inheritance = None
            if predecessor_receipt is not None:
                required_values = (
                    predecessor.effect_fingerprint,
                    predecessor.account_ref,
                    predecessor.target_ref,
                    predecessor.uncertain_response_state,
                )
                if any(
                    type(value) is not str or not value
                    for value in required_values
                ):
                    raise integrity_error(
                        "JOB_RECEIPT_INHERITANCE_EVIDENCE_INCOMPLETE"
                    )
                receipt_inheritance = ReceiptInheritanceV1(
                    predecessor_operation_receipt_id=(
                        predecessor_receipt.operation_receipt_id
                    ),
                    predecessor_operation_receipt_digest=(
                        _operation_receipt_digest(predecessor_receipt)
                    ),
                    predecessor_attempt_id=predecessor.attempt_id,
                    job_id=job.job_id,
                    new_attempt_id=new_attempt_id,
                    lineage_id=job.lineage_id,
                    installation_id=job.installation_id,
                    canonical_operation_id=job.canonical_operation_id,
                    effect_profile=predecessor_receipt.effect_profile,
                    job_profile=predecessor_receipt.job_profile,
                    approval_profile=predecessor_receipt.approval_profile,
                    idempotency_key=job.idempotency_key,
                    intent_payload_digest=job.intent_payload_digest,
                    effect_fingerprint=predecessor.effect_fingerprint,
                    account_ref=predecessor.account_ref,
                    target_ref=predecessor.target_ref,
                    uncertain_response_state=(
                        predecessor.uncertain_response_state
                    ),
                )
            next_state = JobState.QUEUED if enqueue else JobState.PREPARED
            attempt = AttemptV1(
                attempt_id=new_attempt_id,
                lineage_id=job.lineage_id,
                attempt_number=predecessor.attempt_number + 1,
                state=next_state,
                lease_epoch=predecessor.lease_epoch,
                worker_instance_id=None,
                operation_receipt=None,
                effect_fingerprint=predecessor.effect_fingerprint,
                account_ref=predecessor.account_ref,
                target_ref=predecessor.target_ref,
                uncertain_response_state=predecessor.uncertain_response_state,
                created_at=changed_at,
                updated_at=changed_at,
            )
            revision = job.job_revision + 1
            sequence = job.event_sequence + 1
            updated = replace(
                job,
                state=next_state,
                job_revision=revision,
                event_sequence=sequence,
                current_attempt_id=new_attempt_id,
                updated_at=changed_at,
                result_ref=None,
                partial_failures=(),
                attempts=(*job.attempts, attempt),
            )
            event = EventV1(
                event_id=f"{job_id}:event:{sequence}",
                job_id=job_id,
                attempt_id=new_attempt_id,
                sequence=sequence,
                job_revision=revision,
                event_type="retried",
                state=next_state,
                created_at=changed_at,
            )
            payload = {
                "event": "job_upsert",
                "job": updated.as_dict(),
                "job_event": event.as_dict(),
            }
            if receipt_inheritance is not None:
                payload["receipt_inheritance"] = receipt_inheritance.as_dict()
            self._append_payload(payload)
            return deepcopy(self._jobs[job_id])

    def receipt_inheritance(
        self,
        job_id: str,
        attempt_id: str,
    ) -> ReceiptInheritanceV1 | None:
        _nonempty(job_id, "JOB_ID_INVALID")
        _nonempty(attempt_id, "JOB_ATTEMPT_ID_INVALID")
        with self._lock:
            return deepcopy(
                self._receipt_inheritances.get((job_id, attempt_id))
            )

    def get_job(self, job_id: str) -> JobV1:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise conflict("JOB_NOT_FOUND")
            return deepcopy(job)

    def list_jobs(self) -> tuple[JobV1, ...]:
        with self._lock:
            return tuple(deepcopy(self._jobs[key]) for key in sorted(self._jobs))

    def events(self, job_id: str, *, after_sequence: int = 0) -> tuple[EventV1, ...]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise integrity_error("JOB_EVENT_CURSOR_INVALID")
        with self._lock:
            if job_id not in self._jobs:
                raise conflict("JOB_NOT_FOUND")
            return tuple(
                deepcopy(event)
                for event in self._events[job_id]
                if event.sequence > after_sequence
            )


def verify_existing_ledger(data_root: Path, anchor_store: IntegrityAnchorStore, *, include_records=False) -> dict[str, object]:
    """Authenticate existing authority without creating, locking, or repairing it.

    Only Core may supply the anchor store or verify these task records.
    A concurrent writer causes a closed failure, never a replay.
    """
    if not isinstance(data_root, Path) or not data_root.is_absolute():
        raise integrity_error("JOB_DATA_ROOT_MUST_BE_EXPLICIT_ABSOLUTE")
    _validate_no_follow_ancestors(data_root)
    _validate_owned_mode(data_root, mode=0o700, directory=True)
    directory = data_root / "job-ledger-v1"
    _validate_no_follow_ancestors(directory)
    _validate_owned_mode(directory, mode=0o700, directory=True)
    pending = directory / "journal.pending"
    if os.path.lexists(pending):
        raise integrity_error("JOB_LEDGER_RECOVERY_REQUIRED")
    _validate_owned_mode(directory / "transaction.lock", mode=0o600, directory=False)
    projection = _LedgerReadProjection()
    projection.journal_path = directory / "journal.jsonl"
    projection.snapshot_path = directory / "snapshot.json"
    projection._hmac_key = anchor_store.hmac_key()
    if type(projection._hmac_key) is not bytes or len(projection._hmac_key) < 32:
        raise integrity_error("JOB_LEDGER_HMAC_KEY_INVALID")
    head = projection._validate_head(anchor_store.load())
    before = {path: projection._read_file(path, MAX_SNAPSHOT_BYTES)
        for path in (projection.journal_path, projection.snapshot_path)}
    records, computed = projection._read_journal(head["epoch"])
    if computed != head:
        raise integrity_error("JOB_LEDGER_HIGH_WATER_ROLLBACK")
    projection._head = computed
    projection._jobs = {}
    projection._events = {}
    projection._idempotency = {}
    projection._receipt_inheritances = {}
    projection._migration_receipt = None
    for record in records:
        projection._apply_payload(record["payload"])
    projection._validate_snapshot()
    if (anchor_store.load() != head or os.path.lexists(pending)
        or any(projection._read_file(path, MAX_SNAPSHOT_BYTES) != raw for path, raw in before.items())):
        raise integrity_error("JOB_LEDGER_READ_CHANGED")
    result = {"schema_id": "lulu.job-ledger-read-verification/v1", "head": head,
        "job_count": len(projection._jobs),
        "installation_ids": sorted({job.installation_id for job in projection._jobs.values()}),
        "journal_sha256": hashlib.sha256(before[projection.journal_path]).hexdigest(),
        "snapshot_sha256": hashlib.sha256(before[projection.snapshot_path]).hexdigest()}
    if include_records:
        result['records'] = deepcopy(projection._snapshot_payload())
    return result

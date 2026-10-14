"""Read-once migration from the legacy pending transcription queue."""

from __future__ import annotations

from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import (
    AttemptV1,
    EventV1,
    JobState,
    JobV1,
    MigrationReceiptV1,
    migration_error,
)
from .store import JobLedgerStore


MAX_PENDING_BYTES = 1024 * 1024
MAX_TASKS = 500


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
        raise migration_error("PENDING_VALUE_NONCANONICAL") from exc


def _strict_dict(
    value: object,
    fields: frozenset[str],
    reason_code: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != fields:
        raise migration_error(reason_code)
    return value


def _text(
    value: object,
    *,
    maximum: int,
    allow_none: bool = False,
) -> str | None:
    if allow_none and value is None:
        return None
    if type(value) is not str or not value or len(value) > maximum:
        raise migration_error("PENDING_TEXT_INVALID")
    return value


def _validate_path(path: Path, *, directory: bool, modes: frozenset[int]) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise migration_error("PENDING_PATH_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise migration_error("PENDING_SYMLINK_FORBIDDEN")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        raise migration_error("PENDING_FILE_TYPE_INVALID")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise migration_error("PENDING_OWNER_INVALID")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode not in modes:
        raise migration_error("PENDING_MODE_INVALID")
    return mode


def _read_pending(path: Path) -> tuple[bytes, dict[str, object]]:
    _validate_path(path.parent, directory=True, modes=frozenset({0o700}))
    _validate_path(path, directory=False, modes=frozenset({0o600, 0o400}))
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_PENDING_BYTES:
                raise migration_error("PENDING_FILE_SIZE_INVALID")
            chunks: list[bytes] = []
            remaining = MAX_PENDING_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise migration_error("PENDING_READ_FAILED") from exc
    if len(encoded) > MAX_PENDING_BYTES:
        raise migration_error("PENDING_FILE_SIZE_INVALID")
    try:
        parsed = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise migration_error("PENDING_JSON_INVALID") from exc
    if not isinstance(parsed, dict):
        raise migration_error("PENDING_SCHEMA_INVALID")
    return encoded, parsed


def _validate_execution_policy(
    value: object,
    *,
    schema: int,
) -> dict[str, object] | None:
    if value is None:
        return None
    fields = {
        "origin",
        "mode",
        "stable_key",
        "media_destination",
        "material_snapshot",
    }
    if schema == 2:
        fields.update({"media_directory", "prefer_subtitles"})
    policy = _strict_dict(
        value,
        frozenset(fields),
        "PENDING_POLICY_INVALID",
    )
    for field in ("origin", "mode", "media_destination"):
        _text(policy[field], maximum=500)
    if type(policy["stable_key"]) is not str or len(policy["stable_key"]) > 500:
        raise migration_error("PENDING_POLICY_INVALID")
    snapshot = _strict_dict(
        policy["material_snapshot"],
        frozenset({"download_mode", "save_media", "save_cover", "version"}),
        "PENDING_POLICY_INVALID",
    )
    _text(snapshot["download_mode"], maximum=500)
    if (
        type(snapshot["save_media"]) is not bool
        or type(snapshot["save_cover"]) is not bool
        or type(snapshot["version"]) is not int
        or snapshot["version"] < 1
    ):
        raise migration_error("PENDING_POLICY_INVALID")
    if schema == 2:
        _text(policy["media_directory"], maximum=16 * 1024, allow_none=True)
        if policy["prefer_subtitles"] is not None and type(
            policy["prefer_subtitles"]
        ) is not bool:
            raise migration_error("PENDING_POLICY_INVALID")
    encoded = _canonical_bytes(policy)
    if len(encoded) > 64 * 1024:
        raise migration_error("PENDING_POLICY_INVALID")
    return deepcopy(policy)


def _validate_pending(parsed: object) -> tuple[int, tuple[dict[str, object], ...]]:
    root = _strict_dict(
        parsed,
        frozenset({"schema", "tasks", "groups"}),
        "PENDING_SCHEMA_INVALID",
    )
    schema = root["schema"]
    if type(schema) is not int or schema not in {1, 2}:
        raise migration_error("PENDING_SCHEMA_UNSUPPORTED")
    if (
        not isinstance(root["tasks"], list)
        or len(root["tasks"]) > MAX_TASKS
        or not isinstance(root["groups"], list)
    ):
        raise migration_error("PENDING_SCHEMA_INVALID")
    tasks: list[dict[str, object]] = []
    seen_ids: set[int] = set()
    for value in root["tasks"]:
        task = _strict_dict(
            value,
            frozenset(
                {
                    "id",
                    "title",
                    "source",
                    "kind",
                    "state",
                    "folder_id",
                    "stable_key",
                    "execution_policy",
                }
            ),
            "PENDING_TASK_INVALID",
        )
        if type(task["id"]) is not int or task["id"] < 1 or task["id"] in seen_ids:
            raise migration_error("PENDING_TASK_ID_INVALID")
        seen_ids.add(task["id"])
        _text(task["title"], maximum=500)
        _text(task["source"], maximum=16 * 1024)
        if task["kind"] not in {"file", "link"}:
            raise migration_error("PENDING_TASK_KIND_INVALID")
        if task["state"] not in {"pending", "queued", "running"}:
            raise migration_error("PENDING_TASK_STATE_INVALID")
        _text(task["folder_id"], maximum=500, allow_none=True)
        if type(task["stable_key"]) is not str or len(task["stable_key"]) > 500:
            raise migration_error("PENDING_STABLE_KEY_INVALID")
        normalized_task = deepcopy(task)
        normalized_task["execution_policy"] = _validate_execution_policy(
            task["execution_policy"],
            schema=schema,
        )
        tasks.append(normalized_task)
    for value in root["groups"]:
        group = _strict_dict(
            value,
            frozenset({"id", "name", "folder_id", "expanded", "members"}),
            "PENDING_GROUP_INVALID",
        )
        if (
            type(group["id"]) is not int
            or group["id"] < 1
            or type(group["expanded"]) is not bool
            or not isinstance(group["members"], list)
        ):
            raise migration_error("PENDING_GROUP_INVALID")
        _text(group["name"], maximum=500)
        _text(group["folder_id"], maximum=500, allow_none=True)
        for member_value in group["members"]:
            member = _strict_dict(
                member_value,
                frozenset({"task_id", "stable_key", "title", "source", "kind"}),
                "PENDING_GROUP_MEMBER_INVALID",
            )
            if member["task_id"] not in seen_ids:
                raise migration_error("PENDING_GROUP_MEMBER_INVALID")
    return schema, tuple(tasks)


def _deterministic_import(
    *,
    tasks: tuple[dict[str, object], ...],
    source_sha256: str,
    committed_at: str,
) -> tuple[tuple[JobV1, ...], tuple[EventV1, ...]]:
    state_map = {
        "pending": JobState.PREPARED,
        "queued": JobState.QUEUED,
        "running": JobState.RECOVERY_REQUIRED,
    }
    jobs: list[JobV1] = []
    events: list[EventV1] = []
    for task in tasks:
        task_id = task["id"]
        job_id = f"legacy-task-{task_id}"
        attempt_id = f"legacy-attempt-{task_id}"
        lineage_id = f"legacy-lineage-{task_id}"
        source_fingerprint = hashlib.sha256(task["source"].encode("utf-8")).hexdigest()
        frozen_policy = {
            "legacy_schema_source_sha256": source_sha256,
            "legacy_task_id": task_id,
            "legacy_kind": task["kind"],
            "legacy_folder_id": task["folder_id"],
            "legacy_stable_key": task["stable_key"],
            "execution_policy": deepcopy(task["execution_policy"]),
        }
        intent_payload_digest = hashlib.sha256(
            b"LULU_PENDING_MIGRATION_INTENT_V1\0"
            + _canonical_bytes(
                {
                    "task_id": task_id,
                    "kind": task["kind"],
                    "source_sha256": source_fingerprint,
                    "frozen_policy": frozen_policy,
                }
            )
        ).hexdigest()
        state = state_map[task["state"]]
        attempt = AttemptV1(
            attempt_id=attempt_id,
            lineage_id=lineage_id,
            attempt_number=1,
            state=state,
            lease_epoch=0,
            worker_instance_id=None,
            operation_receipt=None,
            effect_fingerprint=None,
            account_ref=None,
            target_ref=(
                None
                if task["folder_id"] is None
                else f"legacy-folder:{task['folder_id']}"
            ),
            uncertain_response_state=(
                "legacy_running_owner_lost"
                if state is JobState.RECOVERY_REQUIRED
                else "none"
            ),
            created_at=committed_at,
            updated_at=committed_at,
        )
        job = JobV1(
            job_id=job_id,
            job_kind="legacy_transcription_recovery",
            input_ref=f"legacy-source:sha256:{source_fingerprint}",
            frozen_policy=frozen_policy,
            created_by="pending-migration-v1",
            installation_id="legacy-unbound-installation",
            canonical_operation_id="OP_LEGACY_TRANSCRIPTION_RECOVERY_V1",
            idempotency_key=f"legacy-pending:{source_sha256}:{task_id}",
            intent_payload_digest=intent_payload_digest,
            state=state,
            job_revision=1,
            event_sequence=1,
            current_attempt_id=attempt_id,
            lineage_id=lineage_id,
            created_at=committed_at,
            updated_at=committed_at,
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
            event_type="migrated",
            state=state,
            created_at=committed_at,
            reason_code=(
                "LEGACY_RUNNING_REQUIRES_RECOVERY"
                if state is JobState.RECOVERY_REQUIRED
                else None
            ),
        )
        jobs.append(job)
        events.append(event)
    return tuple(jobs), tuple(events)


class PendingQueueMigrator:
    """Migrate only the explicitly injected data root, never production paths."""

    def __init__(self, data_root: Path, store: JobLedgerStore) -> None:
        if (
            not isinstance(data_root, Path)
            or not data_root.is_absolute()
            or not isinstance(store, JobLedgerStore)
            or store.data_root != data_root
        ):
            raise migration_error("MIGRATION_DATA_ROOT_MISMATCH")
        self.data_root = data_root
        self.store = store
        self.legacy_path = data_root / "task-queue" / "pending.json"
        self.lock_path = store.directory / "migration.lock"

    def _open_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            os.chmod(self.lock_path, 0o600, follow_symlinks=False)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise migration_error("MIGRATION_LOCK_INVALID")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return descriptor
        except OSError as exc:
            raise migration_error("MIGRATION_LOCK_UNAVAILABLE") from exc

    def migrate(
        self,
        *,
        receipt_id: str,
        committed_at: str,
    ) -> MigrationReceiptV1:
        if type(receipt_id) is not str or not receipt_id:
            raise migration_error("MIGRATION_RECEIPT_ID_INVALID")
        if type(committed_at) is not str or not committed_at:
            raise migration_error("MIGRATION_TIME_INVALID")
        descriptor = self._open_lock()
        try:
            existing = self.store.migration_receipt
            if existing is not None:
                if existing.receipt_id != receipt_id:
                    raise migration_error("MIGRATION_ALREADY_COMMITTED")
                return existing
            encoded, parsed = _read_pending(self.legacy_path)
            schema, tasks = _validate_pending(parsed)
            source_sha256 = hashlib.sha256(encoded).hexdigest()
            jobs, events = _deterministic_import(
                tasks=tasks,
                source_sha256=source_sha256,
                committed_at=committed_at,
            )
            receipt = MigrationReceiptV1(
                receipt_id=receipt_id,
                source_path="task-queue/pending.json",
                source_sha256=source_sha256,
                source_schema=schema,
                imported_job_ids=tuple(job.job_id for job in jobs),
                committed_at=committed_at,
            )
            original_mode = stat.S_IMODE(self.legacy_path.lstat().st_mode)
            try:
                os.chmod(self.legacy_path, 0o400, follow_symlinks=False)
                parent_descriptor = os.open(self.legacy_path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_descriptor)
                finally:
                    os.close(parent_descriptor)
                return self.store.commit_migration(
                    jobs=jobs,
                    events=events,
                    receipt=receipt,
                )
            except Exception:
                os.chmod(
                    self.legacy_path,
                    original_mode,
                    follow_symlinks=False,
                )
                raise
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

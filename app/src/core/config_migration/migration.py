"""Deterministic configuration migration under the Phase 6 config lock."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from collections.abc import Mapping

from ..agent_trust.authority import verify_platform_human_presence
from ..agent_trust.canonical import domain_separated_sha256
from ..resource_control.contracts import LockOwner, ResourceKind
from ..resource_control.locks import ResourceLockManager
from .contracts import (
    ConfigFingerprint,
    ConflictDecision,
    MigrationJournalV1,
    MigrationReceiptV1,
    integrity_error,
    migration_conflict,
)
from .paths import ConfigPaths
from .repository import (
    CONFIG_RESOURCE_ID,
    ConfigDocument,
    ConfigRepository,
    atomic_write_owner_file,
)


RECEIPT_SCHEMA = "lulu.config-migration-receipt/v1"
JOURNAL_SCHEMA = "lulu.config-migration-journal/v1"
MIGRATION_COMPATIBILITY_VERSION = 1
CONFLICT_DIGEST_DOMAIN = "LULU_CONFIG_MIGRATION_CONFLICT_V1"
CONFLICT_DIGEST_SCHEMA = "lulu.config-migration-conflict/v1"


def _nonempty(value: object, reason_code: str) -> str:
    if type(value) is not str or not value or len(value) > 16 * 1024:
        raise integrity_error(reason_code)
    return value


def _fingerprint_value(value: ConfigFingerprint) -> dict[str, object]:
    return asdict(value)


def _receipt_bytes(receipt: MigrationReceiptV1) -> bytes:
    payload = {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": receipt.receipt_id,
        "outcome": receipt.outcome,
        "source_fingerprint": _fingerprint_value(receipt.source_fingerprint),
        "target_fingerprint": _fingerprint_value(receipt.target_fingerprint),
        "canonical_revision": (
            asdict(receipt.canonical_revision)
            if receipt.canonical_revision is not None
            else None
        ),
        "journal_path": receipt.journal_path,
        "backup_path": receipt.backup_path,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def journal_bytes(
    journal: MigrationJournalV1,
    *,
    receipt: MigrationReceiptV1 | None,
) -> bytes:
    journal_value = asdict(journal)
    if journal.decision is not None:
        journal_value["decision"] = journal.decision.value
    receipt_value = None
    if receipt is not None:
        receipt_value = {
            "receipt_id": receipt.receipt_id,
            "outcome": receipt.outcome,
            "source_fingerprint": _fingerprint_value(
                receipt.source_fingerprint
            ),
            "target_fingerprint": _fingerprint_value(
                receipt.target_fingerprint
            ),
            "canonical_revision": (
                asdict(receipt.canonical_revision)
                if receipt.canonical_revision is not None
                else None
            ),
            "journal_path": receipt.journal_path,
            "backup_path": receipt.backup_path,
        }
    payload = {
        "schema": JOURNAL_SCHEMA,
        "journal": journal_value,
        "receipt": receipt_value,
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class ConfigMigrationService:
    def __init__(
        self,
        paths: ConfigPaths,
        *,
        repository: ConfigRepository,
        lock_manager: ResourceLockManager,
        fault_injector: object | None = None,
    ) -> None:
        if not isinstance(paths, ConfigPaths):
            raise TypeError("ConfigMigrationService requires ConfigPaths")
        if (
            not isinstance(repository, ConfigRepository)
            or repository.paths != paths
            or repository.lock_manager is not lock_manager
        ):
            raise TypeError("migration service requires shared repository and lock")
        self.paths = paths
        self.repository = repository
        self.lock_manager = lock_manager
        if fault_injector is not None and not callable(
            getattr(fault_injector, "hit", None)
        ):
            raise TypeError("fault injector must expose hit(point)")
        self._fault_injector = fault_injector

    def _hit(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector.hit(point)

    def migrate(
        self,
        *,
        owner: LockOwner,
        migration_id: str,
        started_at: str,
    ) -> MigrationReceiptV1:
        if not isinstance(owner, LockOwner):
            raise integrity_error("CONFIG_MIGRATION_OWNER_INVALID")
        _nonempty(migration_id, "CONFIG_MIGRATION_ID_INVALID")
        _nonempty(started_at, "CONFIG_MIGRATION_TIME_INVALID")
        self.paths.prepare_layout()
        lease = self.lock_manager.claim(
            ResourceKind.CONFIG,
            CONFIG_RESOURCE_ID,
            owner,
        )
        try:
            legacy = self.repository.inspect(self.paths.legacy_config)
            canonical = self.repository.inspect(self.paths.canonical_config)
            self._validate_scenario(legacy, canonical)
            if (
                legacy.fingerprint.exists
                and canonical.fingerprint.exists
                and legacy.fingerprint.semantic_sha256
                != canonical.fingerprint.semantic_sha256
            ):
                raise migration_conflict("CONFIG_VALUES_DIFFER")

            backup_path: Path | None = None
            backup_sha256: str | None = None
            revision = None
            if not legacy.fingerprint.exists and not canonical.fingerprint.exists:
                outcome = "no_config"
            elif canonical.fingerprint.exists:
                revision = self.repository.track_canonical(lease, canonical)
                outcome = (
                    "semantically_equal"
                    if legacy.fingerprint.exists
                    else "canonical_only"
                )
            else:
                content_sha256 = legacy.fingerprint.content_sha256
                if content_sha256 is None:
                    raise integrity_error("LEGACY_CONFIG_FINGERPRINT_INVALID")
                backup_path = self.paths.backup_path(
                    migration_id,
                    content_sha256,
                )
                self._hit("before_backup_write")
                atomic_write_owner_file(backup_path, legacy.raw)
                backup_sha256 = content_sha256
                journal_path = self.paths.journal_path(migration_id)
                prepared = MigrationJournalV1(
                    migration_id=migration_id,
                    state="prepared",
                    source_fingerprint=legacy.fingerprint,
                    target_fingerprint=canonical.fingerprint,
                    decision=None,
                    backup_path=str(backup_path),
                    backup_sha256=backup_sha256,
                    canonical_revision=None,
                    started_at=started_at,
                    compatibility_version=MIGRATION_COMPATIBILITY_VERSION,
                )
                self._hit("before_journal_write")
                atomic_write_owner_file(
                    journal_path,
                    journal_bytes(prepared, receipt=None),
                )
                self._hit("after_journal_prepared")
                revision = self.repository.commit_canonical(
                    lease,
                    expected=canonical.fingerprint,
                    payload=legacy.raw,
                )
                outcome = "migrated_legacy"

            journal_path = self.paths.journal_path(migration_id)
            receipt = MigrationReceiptV1(
                receipt_id=f"{migration_id}:receipt",
                outcome=outcome,
                source_fingerprint=legacy.fingerprint,
                target_fingerprint=canonical.fingerprint,
                canonical_revision=revision,
                journal_path=str(journal_path),
                backup_path=str(backup_path) if backup_path is not None else None,
            )
            completed = MigrationJournalV1(
                migration_id=migration_id,
                state="committed",
                source_fingerprint=legacy.fingerprint,
                target_fingerprint=canonical.fingerprint,
                decision=None,
                backup_path=str(backup_path) if backup_path is not None else None,
                backup_sha256=backup_sha256,
                canonical_revision=revision,
                started_at=started_at,
                compatibility_version=MIGRATION_COMPATIBILITY_VERSION,
            )
            atomic_write_owner_file(
                journal_path,
                journal_bytes(completed, receipt=receipt),
            )
            return receipt
        finally:
            self.lock_manager.release(lease)

    def conflict_intent_digest(
        self,
        *,
        migration_id: str,
        decision: ConflictDecision,
    ) -> str:
        _nonempty(migration_id, "CONFIG_MIGRATION_ID_INVALID")
        if not isinstance(decision, ConflictDecision):
            raise integrity_error("CONFIG_CONFLICT_DECISION_INVALID")
        legacy = self.repository.inspect(self.paths.legacy_config)
        canonical = self.repository.inspect(self.paths.canonical_config)
        self._require_conflict(legacy, canonical)
        return self._conflict_digest(
            migration_id=migration_id,
            decision=decision,
            legacy=legacy,
            canonical=canonical,
        )

    def resolve_conflict(
        self,
        *,
        owner: LockOwner,
        migration_id: str,
        started_at: str,
        decision: ConflictDecision,
        proof_handle_ref: str,
        verifier: object,
        replay_store: object,
        pinned_issuers: Mapping[str, frozenset[str]],
        approval_id: str,
        local_uid: int,
        installation_id: str,
        nonce: str,
        now_wall: float,
        now_monotonic: float,
    ) -> MigrationReceiptV1:
        if not isinstance(owner, LockOwner):
            raise integrity_error("CONFIG_MIGRATION_OWNER_INVALID")
        _nonempty(migration_id, "CONFIG_MIGRATION_ID_INVALID")
        _nonempty(started_at, "CONFIG_MIGRATION_TIME_INVALID")
        if not isinstance(decision, ConflictDecision):
            raise integrity_error("CONFIG_CONFLICT_DECISION_INVALID")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("platform proof verifier must expose verify(handle_ref)")
        if not callable(getattr(replay_store, "consume", None)):
            raise TypeError("platform proof replay store must expose consume(slot)")
        self.paths.prepare_layout()
        lease = self.lock_manager.claim(
            ResourceKind.CONFIG,
            CONFIG_RESOURCE_ID,
            owner,
        )
        try:
            legacy = self.repository.inspect(self.paths.legacy_config)
            canonical = self.repository.inspect(self.paths.canonical_config)
            self._require_conflict(legacy, canonical)
            expected_digest = self._conflict_digest(
                migration_id=migration_id,
                decision=decision,
                legacy=legacy,
                canonical=canonical,
            )
            verified = verify_platform_human_presence(
                handle_ref=proof_handle_ref,
                verifier=verifier,
                pinned_issuers=pinned_issuers,
                expected_approval_id=approval_id,
                expected_intent_payload_digest=expected_digest,
                expected_local_uid=local_uid,
                expected_installation_id=installation_id,
                expected_nonce=nonce,
                now_wall=now_wall,
                now_monotonic=now_monotonic,
            )
            replay_store.consume(verified["replay_slot"])

            revision = None
            backup_path: Path | None = None
            backup_sha256: str | None = None
            journal_path = self.paths.journal_path(migration_id)
            if decision is ConflictDecision.USE_LEGACY:
                backup_path = self._backup_document(
                    migration_id,
                    canonical,
                )
                backup_sha256 = canonical.fingerprint.content_sha256
                self._write_prepared_journal(
                    journal_path=journal_path,
                    migration_id=migration_id,
                    started_at=started_at,
                    decision=decision,
                    legacy=legacy,
                    canonical=canonical,
                    backup_path=backup_path,
                    backup_sha256=backup_sha256,
                )
                revision = self.repository.commit_canonical(
                    lease,
                    expected=canonical.fingerprint,
                    payload=legacy.raw,
                )
                outcome = "conflict_use_legacy"
            elif decision is ConflictDecision.USE_CANONICAL:
                backup_path = self._backup_document(
                    migration_id,
                    legacy,
                )
                backup_sha256 = legacy.fingerprint.content_sha256
                self._write_prepared_journal(
                    journal_path=journal_path,
                    migration_id=migration_id,
                    started_at=started_at,
                    decision=decision,
                    legacy=legacy,
                    canonical=canonical,
                    backup_path=backup_path,
                    backup_sha256=backup_sha256,
                )
                revision = self.repository.track_canonical(lease, canonical)
                outcome = "conflict_use_canonical"
            else:
                backup_path = self._backup_document(
                    migration_id,
                    canonical,
                )
                backup_sha256 = canonical.fingerprint.content_sha256
                self._backup_document(
                    f"{migration_id}:legacy",
                    legacy,
                )
                outcome = "conflict_exported_both_aborted"

            receipt = MigrationReceiptV1(
                receipt_id=f"{migration_id}:receipt",
                outcome=outcome,
                source_fingerprint=legacy.fingerprint,
                target_fingerprint=canonical.fingerprint,
                canonical_revision=revision,
                journal_path=str(journal_path),
                backup_path=str(backup_path) if backup_path is not None else None,
            )
            completed = MigrationJournalV1(
                migration_id=migration_id,
                state="committed",
                source_fingerprint=legacy.fingerprint,
                target_fingerprint=canonical.fingerprint,
                decision=decision,
                backup_path=str(backup_path) if backup_path is not None else None,
                backup_sha256=backup_sha256,
                canonical_revision=revision,
                started_at=started_at,
                compatibility_version=MIGRATION_COMPATIBILITY_VERSION,
            )
            atomic_write_owner_file(
                journal_path,
                journal_bytes(completed, receipt=receipt),
            )
            return receipt
        finally:
            self.lock_manager.release(lease)

    def _backup_document(
        self,
        migration_id: str,
        document: ConfigDocument,
    ) -> Path:
        content_sha256 = document.fingerprint.content_sha256
        if not document.fingerprint.valid or content_sha256 is None:
            raise integrity_error("CONFIG_BACKUP_SOURCE_INVALID")
        backup_path = self.paths.backup_path(migration_id, content_sha256)
        atomic_write_owner_file(backup_path, document.raw)
        return backup_path

    def _write_prepared_journal(
        self,
        *,
        journal_path: Path,
        migration_id: str,
        started_at: str,
        decision: ConflictDecision,
        legacy: ConfigDocument,
        canonical: ConfigDocument,
        backup_path: Path,
        backup_sha256: str | None,
    ) -> None:
        prepared = MigrationJournalV1(
            migration_id=migration_id,
            state="prepared",
            source_fingerprint=legacy.fingerprint,
            target_fingerprint=canonical.fingerprint,
            decision=decision,
            backup_path=str(backup_path),
            backup_sha256=backup_sha256,
            canonical_revision=None,
            started_at=started_at,
            compatibility_version=MIGRATION_COMPATIBILITY_VERSION,
        )
        self._hit("before_journal_write")
        atomic_write_owner_file(
            journal_path,
            journal_bytes(prepared, receipt=None),
        )
        self._hit("after_journal_prepared")

    @staticmethod
    def _conflict_digest(
        *,
        migration_id: str,
        decision: ConflictDecision,
        legacy: ConfigDocument,
        canonical: ConfigDocument,
    ) -> str:
        return domain_separated_sha256(
            CONFLICT_DIGEST_DOMAIN,
            {
                "schema": CONFLICT_DIGEST_SCHEMA,
                "migration_id": migration_id,
                "decision": decision.value,
                "source_fingerprint": _fingerprint_value(
                    legacy.fingerprint
                ),
                "target_fingerprint": _fingerprint_value(
                    canonical.fingerprint
                ),
            },
        )

    @classmethod
    def _require_conflict(
        cls,
        legacy: ConfigDocument,
        canonical: ConfigDocument,
    ) -> None:
        cls._validate_scenario(legacy, canonical)
        if (
            not legacy.fingerprint.exists
            or not canonical.fingerprint.exists
            or legacy.fingerprint.semantic_sha256
            == canonical.fingerprint.semantic_sha256
        ):
            raise migration_conflict("CONFIG_MIGRATION_NOT_CONFLICT")

    @staticmethod
    def _validate_scenario(
        legacy: ConfigDocument,
        canonical: ConfigDocument,
    ) -> None:
        if legacy.fingerprint.exists and not legacy.fingerprint.valid:
            raise integrity_error("LEGACY_CONFIG_INVALID")
        if canonical.fingerprint.exists and not canonical.fingerprint.valid:
            raise integrity_error("CANONICAL_CONFIG_INVALID")

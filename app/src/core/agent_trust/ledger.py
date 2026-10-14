"""Tamper-evident Phase 4 approval ledger with an explicit integrity anchor."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import os
from pathlib import Path
import stat
from threading import RLock
from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

from .approval import ApprovalReceipt
from .authority import AuthorityVerifier, verify_platform_human_presence
from .canonical import CanonicalizationError, canonical_json_bytes, parse_canonical_json
from .contracts import AgentTrustError, integrity_error


_APPROVAL_RECEIPT_FIELDS = frozenset(
    {
        "approval_id",
        "human_presence_proof_handle_ref",
        "human_presence_issuer_id",
        "human_presence_audience",
        "plan_id",
        "capability_id",
        "job_id",
        "attempt_id",
        "lineage_id",
        "canonical_operation_id",
        "intent_payload_digest",
        "trusted_envelope_digest",
        "normalized_arguments_hash",
        "local_uid",
        "installation_id",
        "signed_manifest_sha256",
        "account_or_tenant_ref",
        "target_ref",
        "target_identity",
        "input_fingerprints",
        "record_revisions",
        "nonce",
        "issued_at",
        "expires_at",
        "issuer_channel",
        "state",
    }
)


class IntegrityAnchorStore(Protocol):
    """Secure high-water seam; Phase 4 supplies no production implementation."""

    def hmac_key(self) -> bytes: ...

    def load(self) -> dict[str, object] | None: ...

    def compare_and_set(
        self,
        expected: dict[str, object] | None,
        replacement: dict[str, object],
    ) -> None: ...


def _validate_no_follow_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise integrity_error("LEDGER_SYMLINK_FORBIDDEN")


def _validate_owned_mode(path: Path, *, expected_mode: int, directory: bool) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise integrity_error("LEDGER_PATH_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise integrity_error("LEDGER_SYMLINK_FORBIDDEN")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        raise integrity_error("LEDGER_FILE_TYPE_INVALID")
    if metadata.st_uid != os.getuid():
        raise integrity_error("LEDGER_OWNER_INVALID")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise integrity_error("LEDGER_MODE_INVALID")


class ApprovalLedger:
    """Append-only journal whose durable head is mirrored by a secure anchor."""

    def __init__(self, directory: Path, anchor_store: IntegrityAnchorStore) -> None:
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise integrity_error("LEDGER_DIRECTORY_MUST_BE_EXPLICIT_ABSOLUTE_PATH")
        if anchor_store is None:
            raise integrity_error("LEDGER_ANCHOR_REQUIRED")
        self.directory = directory
        self.journal_path = directory / "approval-ledger.jsonl"
        self._anchor_store = anchor_store
        self._lock = RLock()
        self._records: list[dict[str, object]] = []
        self._approval_states: dict[str, str] = {}
        self._approval_records: dict[str, dict[str, object]] = {}
        self._proof_slots: set[str] = set()
        key = anchor_store.hmac_key()
        if type(key) is not bytes or len(key) < 32:
            raise integrity_error("LEDGER_HMAC_KEY_INVALID")
        self._hmac_key = key

        _validate_no_follow_ancestors(directory)
        created_directory = False
        if not directory.exists():
            try:
                directory.mkdir(mode=0o700)
                os.chmod(directory, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("LEDGER_DIRECTORY_CREATE_FAILED") from exc
            created_directory = True
        _validate_owned_mode(directory, expected_mode=0o700, directory=True)

        created_journal = False
        if not self.journal_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.journal_path, flags, 0o600)
                os.close(descriptor)
                os.chmod(self.journal_path, 0o600, follow_symlinks=False)
            except OSError as exc:
                raise integrity_error("LEDGER_JOURNAL_CREATE_FAILED") from exc
            created_journal = True
        _validate_owned_mode(self.journal_path, expected_mode=0o600, directory=False)

        anchor = anchor_store.load()
        if anchor is None:
            if not created_directory or not created_journal:
                raise integrity_error("LEDGER_HIGH_WATER_MISSING")
            genesis = {
                "epoch": uuid4().hex,
                "sequence": 0,
                "head_hmac": "0" * 64,
            }
            try:
                anchor_store.compare_and_set(None, genesis)
            except Exception as exc:  # noqa: BLE001 - secure-store seam
                raise integrity_error("LEDGER_HIGH_WATER_WRITE_FAILED") from exc
            self._head = genesis
        else:
            anchored_head = self._validate_anchor_shape(anchor)
            records, computed_head = self._read_and_validate_journal(anchored_head["epoch"])
            if computed_head != anchored_head:
                raise integrity_error("LEDGER_HIGH_WATER_ROLLBACK")
            self._head = computed_head
            self._records = records

            def restore_validated_approved(payload: dict[str, object]) -> None:
                approval_id = payload.get("approval_id")
                replay_slot = payload.get("proof_replay_slot")
                receipt = payload.get("receipt")
                if (
                    type(approval_id) is not str
                    or not approval_id
                    or type(replay_slot) is not str
                    or not replay_slot
                    or not isinstance(receipt, dict)
                    or receipt.get("approval_id") != approval_id
                    or receipt.get("state") != "approved"
                    or approval_id in self._approval_states
                    or replay_slot in self._proof_slots
                ):
                    raise integrity_error("LEDGER_APPROVED_EVENT_INVALID")
                self._approval_states[approval_id] = "approved"
                self._approval_records[approval_id] = {
                    "state": "approved",
                    "receipt": deepcopy(receipt),
                    "recovery_required": False,
                }
                self._proof_slots.add(replay_slot)

            for record in records:
                payload = record["payload"]
                if (
                    isinstance(payload, dict)
                    and payload.get("event") == "approved"
                ):
                    restore_validated_approved(payload)
                else:
                    self._apply_payload(payload)

    @staticmethod
    def _validate_anchor_shape(anchor: object) -> dict[str, object]:
        if not isinstance(anchor, dict) or frozenset(anchor) != {
            "epoch",
            "sequence",
            "head_hmac",
        }:
            raise integrity_error("LEDGER_HIGH_WATER_INVALID")
        if (
            type(anchor.get("epoch")) is not str
            or not anchor["epoch"]
            or type(anchor.get("sequence")) is not int
            or anchor["sequence"] < 0
            or type(anchor.get("head_hmac")) is not str
            or len(anchor["head_hmac"]) != 64
        ):
            raise integrity_error("LEDGER_HIGH_WATER_INVALID")
        return deepcopy(anchor)

    @property
    def head(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._head)

    def _read_and_validate_journal(
        self, epoch: object
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        _validate_no_follow_ancestors(self.journal_path)
        _validate_owned_mode(self.journal_path, expected_mode=0o600, directory=False)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.journal_path, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                data = stream.read()
        except OSError as exc:
            raise integrity_error("LEDGER_JOURNAL_READ_FAILED") from exc
        if not data:
            return [], {"epoch": epoch, "sequence": 0, "head_hmac": "0" * 64}
        if not data.endswith(b"\n"):
            raise integrity_error("LEDGER_JOURNAL_TRUNCATED")
        records: list[dict[str, object]] = []
        previous_hmac = "0" * 64
        for expected_sequence, raw_line in enumerate(data.splitlines(), start=1):
            try:
                record = parse_canonical_json(raw_line)
            except CanonicalizationError as exc:
                raise integrity_error("LEDGER_JOURNAL_INVALID_JSON") from exc
            if not isinstance(record, dict) or frozenset(record) != {
                "epoch",
                "sequence",
                "previous_record_hash",
                "payload",
                "record_hmac",
            }:
                raise integrity_error("LEDGER_RECORD_SHAPE_INVALID")
            if (
                record["epoch"] != epoch
                or record["sequence"] != expected_sequence
                or record["previous_record_hash"] != previous_hmac
                or type(record["record_hmac"]) is not str
                or len(record["record_hmac"]) != 64
            ):
                raise integrity_error("LEDGER_HASH_CHAIN_INVALID")
            body = {
                "epoch": record["epoch"],
                "sequence": record["sequence"],
                "previous_record_hash": record["previous_record_hash"],
                "payload": record["payload"],
            }
            expected_hmac = hmac.new(
                self._hmac_key,
                canonical_json_bytes(body),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected_hmac, record["record_hmac"]):
                raise integrity_error("LEDGER_HMAC_INVALID")
            records.append(record)
            previous_hmac = record["record_hmac"]
        return records, {
            "epoch": epoch,
            "sequence": len(records),
            "head_hmac": previous_hmac,
        }

    def _apply_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise integrity_error("LEDGER_EVENT_INVALID")
        event = payload.get("event")
        if event == "approved":
            raise integrity_error(
                "LEDGER_APPROVED_REQUIRES_VERIFIED_TRANSACTION"
            )
        if event == "recovery_required":
            approval_id = payload.get("approval_id")
            reason_code = payload.get("reason_code")
            if (
                type(approval_id) is not str
                or self._approval_states.get(approval_id) != "claimed"
                or type(reason_code) is not str
                or not reason_code
            ):
                raise integrity_error("LEDGER_RECOVERY_EVENT_INVALID")
            self._approval_records[approval_id].update(
                {
                    "state": "claimed",
                    "recovery_required": True,
                    "recovery_reason_code": reason_code,
                }
            )
            return
        if event == "consumed":
            approval_id = payload.get("approval_id")
            outcome = payload.get("outcome")
            if (
                type(approval_id) is not str
                or self._approval_states.get(approval_id) != "claimed"
                or not isinstance(outcome, dict)
                or outcome.get("status") not in {"known_success", "known_failure"}
                or not frozenset(outcome).issubset({"status", "code"})
            ):
                raise integrity_error("LEDGER_CONSUMED_EVENT_INVALID")
            self._approval_states[approval_id] = "consumed"
            self._approval_records[approval_id].update(
                {
                    "state": "consumed",
                    "recovery_required": False,
                    "outcome": deepcopy(outcome),
                }
            )
            return
        if event == "claimed":
            approval_id = payload.get("approval_id")
            operation_receipt = payload.get("operation_receipt")
            if (
                type(approval_id) is not str
                or self._approval_states.get(approval_id) != "approved"
                or not isinstance(operation_receipt, dict)
                or frozenset(operation_receipt)
                != {
                    "operation_receipt_id",
                    "job_id",
                    "attempt_id",
                    "lineage_id",
                    "installation_id",
                    "canonical_operation_id",
                    "effect_profile",
                    "job_profile",
                    "approval_profile",
                    "idempotency_key",
                    "intent_payload_digest",
                    "created_at",
                }
                or type(payload.get("job_revision")) is not int
                or payload["job_revision"] < 1
                or type(payload.get("lease_epoch")) is not int
                or payload["lease_epoch"] < 1
            ):
                raise integrity_error("LEDGER_CLAIM_EVENT_INVALID")
            approved_receipt = self._approval_records[approval_id]["receipt"]
            if (
                operation_receipt["job_id"] != approved_receipt["job_id"]
                or operation_receipt["attempt_id"] != approved_receipt["attempt_id"]
                or operation_receipt["lineage_id"] != approved_receipt["lineage_id"]
                or operation_receipt["installation_id"]
                != approved_receipt["installation_id"]
                or operation_receipt["canonical_operation_id"]
                != approved_receipt["canonical_operation_id"]
                or operation_receipt["intent_payload_digest"]
                != approved_receipt["intent_payload_digest"]
            ):
                raise integrity_error("LEDGER_CLAIM_BINDING_MISMATCH")
            self._approval_states[approval_id] = "claimed"
            self._approval_records[approval_id].update(
                {
                    "state": "claimed",
                    "operation_receipt": deepcopy(operation_receipt),
                    "job_revision": payload["job_revision"],
                    "lease_epoch": payload["lease_epoch"],
                    "recovery_required": False,
                }
            )
            return
        raise integrity_error("LEDGER_EVENT_INVALID")

    def _append_payload(self, payload: dict[str, object]) -> None:
        if isinstance(payload, dict) and payload.get("event") == "approved":
            raise integrity_error(
                "LEDGER_APPROVED_REQUIRES_VERIFIED_TRANSACTION"
            )
        old_head = deepcopy(self._head)
        body = {
            "epoch": old_head["epoch"],
            "sequence": old_head["sequence"] + 1,
            "previous_record_hash": old_head["head_hmac"],
            "payload": deepcopy(payload),
        }
        record_hmac = hmac.new(
            self._hmac_key,
            canonical_json_bytes(body),
            hashlib.sha256,
        ).hexdigest()
        record = {**body, "record_hmac": record_hmac}
        encoded = canonical_json_bytes(record) + b"\n"
        _validate_no_follow_ancestors(self.directory)
        _validate_owned_mode(self.directory, expected_mode=0o700, directory=True)
        _validate_no_follow_ancestors(self.journal_path)
        _validate_owned_mode(self.journal_path, expected_mode=0o600, directory=False)
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.journal_path, flags)
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError("short ledger write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise integrity_error("LEDGER_JOURNAL_APPEND_FAILED") from exc
        new_head = {
            "epoch": old_head["epoch"],
            "sequence": body["sequence"],
            "head_hmac": record_hmac,
        }
        try:
            self._anchor_store.compare_and_set(old_head, new_head)
        except Exception as exc:  # noqa: BLE001 - secure-store seam
            raise integrity_error("LEDGER_HIGH_WATER_WRITE_FAILED") from exc
        self._head = new_head
        self._records.append(record)
        self._apply_payload(payload)

    def verify_and_append_approved(
        self,
        *,
        handle_ref: str,
        verifier: AuthorityVerifier,
        pinned_issuers: Mapping[str, frozenset[str]],
        expected_approval_id: str,
        expected_intent_payload_digest: str,
        expected_local_uid: int,
        expected_installation_id: str,
        expected_nonce: str,
        now_wall: float,
        now_monotonic: float,
        receipt_builder: Callable[[dict[str, object]], ApprovalReceipt],
    ) -> ApprovalReceipt:
        """Verify proof, build receipt, burn replay, and append under one lock."""
        with self._lock:
            verified_proof = verify_platform_human_presence(
                handle_ref=handle_ref,
                verifier=verifier,
                pinned_issuers=pinned_issuers,
                expected_approval_id=expected_approval_id,
                expected_intent_payload_digest=expected_intent_payload_digest,
                expected_local_uid=expected_local_uid,
                expected_installation_id=expected_installation_id,
                expected_nonce=expected_nonce,
                now_wall=now_wall,
                now_monotonic=now_monotonic,
            )
            if (
                verified_proof.get("replay_slot") in self._proof_slots
                or expected_approval_id in self._approval_states
            ):
                raise AgentTrustError(
                    "APPROVAL_CONSUMED",
                    exit_code=21,
                    reason_code="PLATFORM_PROOF_REPLAYED",
                )

            def append_verified_approved(receipt: ApprovalReceipt) -> None:
                """Bind and append only this transaction's verified proof."""
                if not isinstance(receipt, ApprovalReceipt):
                    raise integrity_error("LEDGER_APPROVAL_RECEIPT_INVALID")
                receipt_payload = receipt.as_dict()
                if frozenset(receipt_payload) != _APPROVAL_RECEIPT_FIELDS:
                    raise integrity_error("LEDGER_APPROVAL_RECEIPT_INVALID")
                replay_slot = verified_proof.get("replay_slot")
                approval_id = verified_proof.get("approval_id")
                if (
                    replay_slot in self._proof_slots
                    or approval_id in self._approval_states
                ):
                    raise AgentTrustError(
                        "APPROVAL_CONSUMED",
                        exit_code=21,
                        reason_code="PLATFORM_PROOF_REPLAYED",
                    )
                if (
                    receipt_payload["approval_id"] != approval_id
                    or receipt_payload["human_presence_proof_handle_ref"]
                    != verified_proof.get("proof_handle_ref")
                    or receipt_payload["human_presence_issuer_id"]
                    != verified_proof.get("issuer_id")
                    or receipt_payload["human_presence_audience"]
                    != verified_proof.get("audience")
                    or receipt_payload["intent_payload_digest"]
                    != verified_proof.get("intent_payload_digest")
                    or receipt_payload["local_uid"]
                    != verified_proof.get("local_uid")
                    or receipt_payload["installation_id"]
                    != verified_proof.get("installation_id")
                    or receipt_payload["nonce"] != verified_proof.get("nonce")
                ):
                    raise integrity_error(
                        "LEDGER_APPROVAL_PROOF_BINDING_MISMATCH"
                    )
                approved_payload = {
                    "event": "approved",
                    "approval_id": approval_id,
                    "proof_replay_slot": replay_slot,
                    "proof_handle_digest": verified_proof.get(
                        "proof_handle_digest"
                    ),
                    "receipt": receipt_payload,
                }
                old_head = deepcopy(self._head)
                body = {
                    "epoch": old_head["epoch"],
                    "sequence": old_head["sequence"] + 1,
                    "previous_record_hash": old_head["head_hmac"],
                    "payload": deepcopy(approved_payload),
                }
                record_hmac = hmac.new(
                    self._hmac_key,
                    canonical_json_bytes(body),
                    hashlib.sha256,
                ).hexdigest()
                record = {**body, "record_hmac": record_hmac}
                encoded = canonical_json_bytes(record) + b"\n"
                _validate_no_follow_ancestors(self.directory)
                _validate_owned_mode(
                    self.directory,
                    expected_mode=0o700,
                    directory=True,
                )
                _validate_no_follow_ancestors(self.journal_path)
                _validate_owned_mode(
                    self.journal_path,
                    expected_mode=0o600,
                    directory=False,
                )
                flags = os.O_WRONLY | os.O_APPEND
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(self.journal_path, flags)
                    try:
                        offset = 0
                        while offset < len(encoded):
                            written = os.write(descriptor, encoded[offset:])
                            if written <= 0:
                                raise OSError("short ledger write")
                            offset += written
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError as exc:
                    raise integrity_error(
                        "LEDGER_JOURNAL_APPEND_FAILED"
                    ) from exc
                new_head = {
                    "epoch": old_head["epoch"],
                    "sequence": body["sequence"],
                    "head_hmac": record_hmac,
                }
                try:
                    self._anchor_store.compare_and_set(old_head, new_head)
                except Exception as exc:  # noqa: BLE001 - secure-store seam
                    raise integrity_error(
                        "LEDGER_HIGH_WATER_WRITE_FAILED"
                    ) from exc
                self._head = new_head
                self._records.append(record)
                self._approval_states[approval_id] = "approved"
                self._approval_records[approval_id] = {
                    "state": "approved",
                    "receipt": deepcopy(receipt_payload),
                    "recovery_required": False,
                }
                self._proof_slots.add(replay_slot)

            receipt = receipt_builder(verified_proof)
            append_verified_approved(receipt)
            return receipt

    def approval_state(self, approval_id: str) -> str | None:
        with self._lock:
            return self._approval_states.get(approval_id)

    def approval_record(self, approval_id: str) -> dict[str, object] | None:
        with self._lock:
            record = self._approval_records.get(approval_id)
            return deepcopy(record) if record is not None else None

    def claim(
        self,
        *,
        approval_id: str,
        operation_receipt: "OperationReceipt",
        job_revision: int,
        lease_epoch: int,
    ) -> None:
        """Atomically claim an approved receipt and bind its operation identity."""
        from .approval import OperationReceipt

        with self._lock:
            if self._approval_states.get(approval_id) != "approved":
                raise AgentTrustError(
                    "APPROVAL_CONSUMED",
                    exit_code=21,
                    reason_code="APPROVAL_NOT_CLAIMABLE",
                )
            if not isinstance(operation_receipt, OperationReceipt):
                raise integrity_error("LEDGER_OPERATION_RECEIPT_INVALID")
            if type(job_revision) is not int or job_revision < 1:
                raise integrity_error("LEDGER_JOB_REVISION_INVALID")
            if type(lease_epoch) is not int or lease_epoch < 1:
                raise integrity_error("LEDGER_LEASE_EPOCH_INVALID")
            approved_receipt = self._approval_records[approval_id]["receipt"]
            immutable = operation_receipt.as_dict()
            if (
                immutable["job_id"] != approved_receipt["job_id"]
                or immutable["attempt_id"] != approved_receipt["attempt_id"]
                or immutable["lineage_id"] != approved_receipt["lineage_id"]
                or immutable["installation_id"]
                != approved_receipt["installation_id"]
                or immutable["canonical_operation_id"]
                != approved_receipt["canonical_operation_id"]
                or immutable["intent_payload_digest"]
                != approved_receipt["intent_payload_digest"]
            ):
                raise integrity_error("LEDGER_CLAIM_BINDING_MISMATCH")
            self._append_payload(
                {
                    "event": "claimed",
                    "approval_id": approval_id,
                    "operation_receipt": immutable,
                    "job_revision": job_revision,
                    "lease_epoch": lease_epoch,
                }
            )

    def mark_recovery_required(self, *, approval_id: str, reason_code: str) -> None:
        """Keep an uncertain response claimed and explicitly fence recovery."""
        with self._lock:
            if self._approval_states.get(approval_id) != "claimed":
                raise AgentTrustError(
                    "APPROVAL_INVALID",
                    exit_code=21,
                    reason_code="APPROVAL_NOT_CLAIMED",
                )
            if type(reason_code) is not str or not reason_code:
                raise integrity_error("LEDGER_RECOVERY_REASON_INVALID")
            self._append_payload(
                {
                    "event": "recovery_required",
                    "approval_id": approval_id,
                    "reason_code": reason_code,
                }
            )

    def consume(self, *, approval_id: str, outcome: dict[str, object]) -> None:
        """Consume only after a known success or known failure is available."""
        with self._lock:
            if self._approval_states.get(approval_id) != "claimed":
                raise AgentTrustError(
                    "APPROVAL_CONSUMED",
                    exit_code=21,
                    reason_code="APPROVAL_NOT_CONSUMABLE",
                )
            if (
                not isinstance(outcome, dict)
                or outcome.get("status") not in {"known_success", "known_failure"}
                or not frozenset(outcome).issubset({"status", "code"})
            ):
                raise integrity_error("LEDGER_OUTCOME_NOT_DETERMINED")
            self._append_payload(
                {
                    "event": "consumed",
                    "approval_id": approval_id,
                    "outcome": deepcopy(outcome),
                }
            )

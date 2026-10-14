"""Core-private authoritative approval receipts for trusted-test installation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from threading import RLock

from .contracts import CliRuntimeTrustError, CodeSignatureEvidenceV1


TRUSTED_GUI_HUMAN_ACTION = "trusted_gui_human_action"
CONTROLLED_INSTALLER_HUMAN_ACTION = "controlled_installer_human_action"
_AUTHORITATIVE_ISSUERS = frozenset(
    {TRUSTED_GUI_HUMAN_ACTION, CONTROLLED_INSTALLER_HUMAN_ACTION}
)
_SOURCE_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,200}$")
_MAX_APPROVAL_LIFETIME = timedelta(seconds=120)
_RECEIPT_SEAL = object()


def _approval_error(code: str, reason_code: str) -> CliRuntimeTrustError:
    return CliRuntimeTrustError(code, reason_code)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _approval_error("APPROVAL_INVALID", "INSTALLATION_APPROVAL_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if type(value) is not str or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _signature_binding(signature: CodeSignatureEvidenceV1) -> tuple[str, ...]:
    return (
        signature.signature_kind,
        signature.certificate_sha256,
        signature.cdhash,
        signature.designated_requirement,
        signature.team_id,
    )


def _installation_binding(
    *,
    bundle_id: str,
    source_commit: str,
    source_tree: str,
    canonical_cli_sha256: str,
    installation_id: str,
    signature: CodeSignatureEvidenceV1,
) -> tuple[str, ...]:
    if (
        type(bundle_id) is not str
        or bundle_id != "com.lulu.transcribe.trusted-test"
        or type(source_commit) is not str
        or not _SOURCE_ID.fullmatch(source_commit)
        or type(source_tree) is not str
        or not _SOURCE_ID.fullmatch(source_tree)
        or type(canonical_cli_sha256) is not str
        or not _SHA256.fullmatch(canonical_cli_sha256)
        or type(installation_id) is not str
        or not 8 <= len(installation_id) <= 200
        or not isinstance(signature, CodeSignatureEvidenceV1)
        or signature.signature_kind != "ad_hoc"
        or bool(signature.team_id)
    ):
        raise _approval_error(
            "TRUST_ANCHOR_INVALID", "LOCAL_TRUSTED_TEST_BINDING_INVALID"
        )
    return (
        bundle_id,
        source_commit,
        source_tree,
        canonical_cli_sha256,
        installation_id,
        *_signature_binding(signature),
    )


class _InstallationApprovalReceipt:
    __slots__ = ("__token",)

    def __init__(self, seal: object, token: str) -> None:
        if seal is not _RECEIPT_SEAL:
            raise TypeError("installation approval receipts are Core-issued")
        object.__setattr__(self, "_InstallationApprovalReceipt__token", token)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("installation approval receipts are immutable")

    def __reduce__(self):
        raise TypeError("installation approval receipts are not serializable")


@dataclass(frozen=True, slots=True)
class _ApprovalClaims:
    binding: tuple[str, ...]
    installation_id: str
    issuer_source: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    receipt_sha256: str


@dataclass(slots=True)
class _ApprovalState:
    claims: _ApprovalClaims
    state: str = "pending"
    reservation: object | None = None


@dataclass(frozen=True, slots=True)
class _ApprovalReservation:
    token: str
    marker: object
    claims: _ApprovalClaims


_APPROVALS: dict[str, _ApprovalState] = {}
_ISSUED_NONCES: set[str] = set()
_APPROVAL_LOCK = RLock()


def _issue_authoritative_installation_approval(
    *,
    issuer_source: str,
    bundle_id: str,
    source_commit: str,
    source_tree: str,
    canonical_cli_sha256: str,
    installation_id: str,
    signature: CodeSignatureEvidenceV1,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> object:
    """Core-private seam reserved for a trusted GUI or controlled installer."""

    if issuer_source not in _AUTHORITATIVE_ISSUERS or not _NONCE.fullmatch(nonce):
        raise _approval_error(
            "APPROVAL_INVALID", "INSTALLATION_APPROVAL_ISSUER_INVALID"
        )
    issued = _utc(issued_at)
    expires = _utc(expires_at)
    if expires <= issued or expires - issued > _MAX_APPROVAL_LIFETIME:
        raise _approval_error("APPROVAL_INVALID", "INSTALLATION_APPROVAL_TIME_INVALID")
    binding = _installation_binding(
        bundle_id=bundle_id,
        source_commit=source_commit,
        source_tree=source_tree,
        canonical_cli_sha256=canonical_cli_sha256,
        installation_id=installation_id,
        signature=signature,
    )
    token = secrets.token_urlsafe(32)
    claims = _ApprovalClaims(
        binding=binding,
        installation_id=installation_id,
        issuer_source=issuer_source,
        nonce=nonce,
        issued_at=issued,
        expires_at=expires,
        receipt_sha256=hashlib.sha256(token.encode("ascii")).hexdigest(),
    )
    with _APPROVAL_LOCK:
        if nonce in _ISSUED_NONCES:
            raise _approval_error(
                "APPROVAL_CONSUMED", "INSTALLATION_APPROVAL_NONCE_ALREADY_USED"
            )
        _ISSUED_NONCES.add(nonce)
        _APPROVALS[token] = _ApprovalState(claims=claims)
    return _InstallationApprovalReceipt(_RECEIPT_SEAL, token)


def _issue_from_trusted_gui_verified_human_action(
    **binding: object,
) -> object:
    return _issue_authoritative_installation_approval(
        issuer_source=TRUSTED_GUI_HUMAN_ACTION,
        **binding,
    )


def _issue_from_controlled_installer_verified_human_action(
    **binding: object,
) -> object:
    return _issue_authoritative_installation_approval(
        issuer_source=CONTROLLED_INSTALLER_HUMAN_ACTION,
        **binding,
    )


def _receipt_token(receipt: object) -> str:
    if type(receipt) is not _InstallationApprovalReceipt:
        raise _approval_error(
            "APPROVAL_INVALID", "INSTALLATION_APPROVAL_RECEIPT_INVALID"
        )
    try:
        return object.__getattribute__(
            receipt, "_InstallationApprovalReceipt__token"
        )
    except Exception as exc:  # pragma: no cover - defensive object corruption
        raise _approval_error(
            "APPROVAL_INVALID", "INSTALLATION_APPROVAL_RECEIPT_INVALID"
        ) from exc


def _reserve_authoritative_installation_approval(
    receipt: object,
    *,
    bundle_id: str,
    source_commit: str,
    source_tree: str,
    canonical_cli_sha256: str,
    signature: CodeSignatureEvidenceV1,
    now: datetime,
) -> _ApprovalReservation:
    token = _receipt_token(receipt)
    current = _utc(now)
    with _APPROVAL_LOCK:
        state = _APPROVALS.get(token)
        if state is None:
            raise _approval_error(
                "APPROVAL_INVALID", "INSTALLATION_APPROVAL_RECEIPT_INVALID"
            )
        if state.state != "pending":
            raise _approval_error(
                "APPROVAL_CONSUMED", "INSTALLATION_APPROVAL_ALREADY_CONSUMED"
            )
        claims = state.claims
        expected = _installation_binding(
            bundle_id=bundle_id,
            source_commit=source_commit,
            source_tree=source_tree,
            canonical_cli_sha256=canonical_cli_sha256,
            installation_id=claims.installation_id,
            signature=signature,
        )
        if claims.binding != expected:
            raise _approval_error(
                "APPROVAL_INVALID", "INSTALLATION_APPROVAL_BINDING_MISMATCH"
            )
        if current < claims.issued_at or current > claims.expires_at:
            raise _approval_error(
                "APPROVAL_EXPIRED", "INSTALLATION_APPROVAL_EXPIRED"
            )
        marker = object()
        state.state = "reserved"
        state.reservation = marker
        return _ApprovalReservation(token=token, marker=marker, claims=claims)


def _release_authoritative_installation_approval(
    reservation: _ApprovalReservation,
) -> None:
    with _APPROVAL_LOCK:
        state = _APPROVALS.get(reservation.token)
        if (
            state is not None
            and state.state == "reserved"
            and state.reservation is reservation.marker
        ):
            state.state = "pending"
            state.reservation = None


def _commit_authoritative_installation_approval(
    reservation: _ApprovalReservation,
) -> None:
    with _APPROVAL_LOCK:
        state = _APPROVALS.get(reservation.token)
        if (
            state is None
            or state.state != "reserved"
            or state.reservation is not reservation.marker
        ):
            raise _approval_error(
                "APPROVAL_CONSUMED", "INSTALLATION_APPROVAL_ALREADY_CONSUMED"
            )
        state.state = "consumed"
        state.reservation = None


def _local_ticket_approval_metadata(claims: _ApprovalClaims) -> dict[str, object]:
    return {
        "approval_issuer_source": claims.issuer_source,
        "approval_nonce": claims.nonce,
        "approval_issued_at": _format_utc(claims.issued_at),
        "approval_expires_at": _format_utc(claims.expires_at),
        "approval_receipt_sha256": claims.receipt_sha256,
    }


def _stored_local_ticket_approval_is_valid(ticket: dict[str, object]) -> bool:
    issued = _parse_utc(ticket.get("approval_issued_at"))
    expires = _parse_utc(ticket.get("approval_expires_at"))
    return bool(
        ticket.get("approval_issuer_source") in _AUTHORITATIVE_ISSUERS
        and type(ticket.get("approval_nonce")) is str
        and _NONCE.fullmatch(str(ticket.get("approval_nonce")))
        and type(ticket.get("approval_receipt_sha256")) is str
        and _SHA256.fullmatch(str(ticket.get("approval_receipt_sha256")))
        and issued is not None
        and expires is not None
        and expires > issued
        and expires - issued <= _MAX_APPROVAL_LIFETIME
    )

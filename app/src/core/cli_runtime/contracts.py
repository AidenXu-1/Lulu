"""Closed contracts for a verified installed Agent CLI runtime."""

from __future__ import annotations

from dataclasses import dataclass
import re


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CDHASH = re.compile(r"^[0-9a-f]{40,64}$")


class CliRuntimeTrustError(RuntimeError):
    def __init__(self, code: str, reason_code: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class CodeSignatureEvidenceV1:
    signature_kind: str
    certificate_sha256: str
    cdhash: str
    designated_requirement: str
    team_id: str

    def __post_init__(self) -> None:
        if self.signature_kind not in {"ad_hoc", "developer_id"}:
            raise ValueError("unknown signature kind")
        if self.certificate_sha256 and not _SHA256.fullmatch(
            self.certificate_sha256
        ):
            raise ValueError("invalid certificate fingerprint")
        if not _CDHASH.fullmatch(self.cdhash):
            raise ValueError("invalid CDHash")
        if not self.designated_requirement or len(self.designated_requirement) > 4096:
            raise ValueError("invalid designated requirement")
        if self.signature_kind == "developer_id" and not self.team_id:
            raise ValueError("Developer ID signature requires Team ID")
        if self.signature_kind == "ad_hoc" and self.team_id:
            raise ValueError("ad-hoc signature cannot claim Team ID")

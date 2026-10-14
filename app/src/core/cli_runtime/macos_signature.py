"""macOS codesign boundary used only by the frozen production factory."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .contracts import CliRuntimeTrustError, CodeSignatureEvidenceV1


_FIELD = re.compile(r"^([^=]+)=(.*)$")


class MacOSCodeSignatureVerifier:
    def _run(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/codesign", *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if completed.returncode != 0:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "CODE_SIGNATURE_VERIFICATION_FAILED"
            )
        return f"{completed.stdout}\n{completed.stderr}"

    def verify(
        self,
        *,
        app_bundle: Path,
        cli_path: Path,
    ) -> CodeSignatureEvidenceV1:
        self._run("--verify", "--deep", "--strict", str(app_bundle))
        self._run("--verify", "--strict", str(cli_path))
        details = self._run("--display", "--verbose=4", str(cli_path))
        requirement_output = self._run(
            "--display", "--requirements", "-", str(cli_path)
        )
        fields: dict[str, list[str]] = {}
        for raw_line in details.splitlines():
            match = _FIELD.match(raw_line.strip())
            if match:
                fields.setdefault(match.group(1), []).append(match.group(2).strip())
        designated_requirement = ""
        for raw_line in requirement_output.splitlines():
            line = raw_line.strip()
            if line.startswith("# "):
                line = line.removeprefix("# ")
            if line.startswith("designated =>"):
                designated_requirement = line.removeprefix("designated =>").strip()
                break
        cdhash = (fields.get("CDHash") or [""])[-1].lower()
        team_id = (fields.get("TeamIdentifier") or [""])[-1]
        ad_hoc = (fields.get("Signature") or [""])[-1].lower() == "adhoc"
        authorities = fields.get("Authority") or []
        developer_id = any(
            authority.startswith("Developer ID Application:")
            for authority in authorities
        )
        if ad_hoc:
            signature_kind = "ad_hoc"
            team_id = ""
        elif developer_id and team_id and team_id != "not set":
            signature_kind = "developer_id"
        else:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "CODE_SIGNATURE_IDENTITY_NOT_ALLOWED"
            )
        try:
            return CodeSignatureEvidenceV1(
                signature_kind=signature_kind,
                certificate_sha256="",
                cdhash=cdhash,
                designated_requirement=designated_requirement,
                team_id=team_id,
            )
        except ValueError as exc:
            raise CliRuntimeTrustError(
                "TRUST_ANCHOR_INVALID", "CODE_SIGNATURE_EVIDENCE_INVALID"
            ) from exc

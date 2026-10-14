"""Zero-effect Phase 3 diagnostic report builders."""

from __future__ import annotations


def offline_doctor_report() -> dict[str, object]:
    """Return the fixed offline skeleton without probing local or network state."""
    return {
        "formal_cli_state": "unavailable",
        "overall_status": "unavailable",
        "local_checks": {
            "status": "unavailable",
            "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
        },
        "authorization_checks": {
            "status": "not_requested",
            "reason_code": "PHASE3_DOCTOR_DOES_NOT_READ_AUTH",
        },
        "network_checks": {
            "status": "not_requested",
            "reason_code": "NETWORK_PROBE_REQUIRES_EXPLICIT_APPROVAL",
            "attempt_count": 0,
        },
    }


def network_prepare_proposal() -> dict[str, object]:
    """Describe the unavailable future probe without creating approval state."""
    return {
        "formal_cli_state": "unavailable",
        "proposal_kind": "network_diagnostic_proposal",
        "proposal_version": "1.0.0",
        "status": "proposal_only",
        "approvable": False,
        "consumable": False,
        "executable": False,
        "network_attempt_count": 0,
        "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
    }

"""Phase 9 Skill adapter may create untrusted proposals and nothing stronger."""

from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import runpy
import unittest


def _request() -> dict[str, object]:
    return {
        "schema_id": "lulu.skill-proposal-request/v1",
        "schema_revision": 1,
        "plan_id": "plan-phase9-001",
        "capability_id": "SYS-01",
        "typed_request": {"request_id": "req-phase9-001", "detail_level": "summary"},
        "normalized_argv_tokens": ["lulu", "version"],
        "typed_target_refs": [],
        "opaque_artifact_refs": [],
        "account_ref_or_null": None,
        "tenant_ref_or_null": None,
        "record_and_resource_revisions": {},
        "idempotency_key_or_null": None,
    }


class Phase9ProposalOnlyTests(unittest.TestCase):
    def test_skill_request_reaches_core_proposal_seam_without_authority_or_dispatch(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        proposal_source = app_root / "src" / "agent_cli" / "proposal.py"
        self.assertTrue(
            proposal_source.is_file(),
            "Phase 9 proposal-only adapter is missing",
        )
        namespace = runpy.run_path(str(proposal_source))
        build_skill_proposal = namespace["build_skill_proposal"]
        SkillProposalError = namespace["SkillProposalError"]

        proposal = build_skill_proposal(_request())
        self.assertEqual(proposal["proposal_version"], "lulu.intent-proposal/v1")
        self.assertEqual(proposal["producer"], "Skill")
        self.assertEqual(proposal["trust_level"], "untrusted_data_plane")
        self.assertEqual(proposal["current_cli_state"], "unavailable")
        self.assertFalse(proposal["approved"])
        self.assertFalse(proposal["dispatchable"])
        self.assertEqual(proposal["capability_id"], "SYS-01")
        self.assertEqual(proposal["normalized_argv_tokens"], ["lulu", "version"])
        self.assertEqual(len(proposal["intent_payload_digest"]), 64)
        self.assertEqual(len(proposal["proposal_digest"]), 64)
        self.assertTrue(
            frozenset(proposal).isdisjoint(
                {
                    "authority",
                    "issuer_type",
                    "proof_handle_ref",
                    "attestation_handle_ref",
                    "trusted_envelope_digest",
                    "approval_receipt",
                    "allowed_next_states",
                    "claim",
                    "dispatch",
                }
            )
        )

        hostile = _request()
        hostile["trusted_envelope"] = {"verified": True}
        with self.assertRaises(SkillProposalError) as raised:
            build_skill_proposal(hostile)
        self.assertEqual(raised.exception.code, "UNKNOWN_FIELD")

        from src.agent_cli.main import main

        stdout = StringIO()
        stderr = StringIO()
        exit_code = main(
            [
                "skill",
                "proposal",
                "prepare",
                "--request-json",
                json.dumps(_request(), separators=(",", ":")),
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=stderr,
            product_identity={"adapter_id": "agent_cli"},
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(envelope["schema_id"], "lulu.agent/v1")
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "skill proposal prepare")
        self.assertEqual(envelope["result"], proposal)


if __name__ == "__main__":
    unittest.main()

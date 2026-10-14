"""Phase 9 Skill workflow decisions are closed, non-authoritative projections."""

from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import runpy
import unittest


def _signal(code: str) -> dict[str, object]:
    return {
        "schema_id": "lulu.skill-workflow-signal/v1",
        "schema_revision": 1,
        "code": code,
        "capability_id_or_null": "TR-02",
        "job_id_or_null": "job-001",
        "idempotency_key_or_null": "idem-001",
        "successful_item_refs": ["item-ok"],
        "failed_item_refs": ["item-failed"],
    }


class Phase9WorkflowTests(unittest.TestCase):
    def test_all_workflow_classes_stop_or_wait_without_dispatch_or_blind_replay(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        namespace = runpy.run_path(
            str(app_root / "src" / "agent_cli" / "skill_contract.py")
        )
        self.assertIn(
            "decide_skill_next_step",
            namespace,
            "Phase 9 closed Skill workflow decision seam is missing",
        )
        decide = namespace["decide_skill_next_step"]
        SkillContractError = namespace["SkillContractError"]

        expected_actions = {
            "CAPABILITY_UNAVAILABLE": "stop_unavailable",
            "GUI_ONLY": "trusted_gui_handoff",
            "AUTH_REQUIRED": "trusted_gui_activation",
            "HUMAN_ACTION_REQUIRED": "trusted_human_handoff",
            "RESOURCE_BUSY": "wait_and_reread_status",
            "CONFLICT": "require_explicit_resolution",
            "CANCELLED": "stop_cancelled_attempt",
            "RECOVERY_REQUIRED": "inspect_authoritative_recovery",
            "PARTIAL": "preserve_success_and_repropose_failures",
        }
        for code, action in expected_actions.items():
            with self.subTest(code=code):
                decision = decide(_signal(code))
                self.assertEqual(decision["action"], action)
                self.assertFalse(decision["business_dispatch_allowed"])
                self.assertFalse(decision["replay_allowed"])
                self.assertEqual(decision["idempotency_key_or_null"], "idem-001")
                if code == "PARTIAL":
                    self.assertEqual(decision["preserved_item_refs"], ["item-ok"])
                    self.assertEqual(decision["reproposal_item_refs"], ["item-failed"])
                else:
                    self.assertEqual(decision["preserved_item_refs"], [])
                    self.assertEqual(decision["reproposal_item_refs"], [])

        with self.assertRaises(SkillContractError) as raised:
            decide(_signal("AGENT_SAYS_RETRY_IS_SAFE"))
        self.assertEqual(raised.exception.code, "UNKNOWN_ENUM")

        from src.agent_cli.main import main

        stdout = StringIO()
        stderr = StringIO()
        exit_code = main(
            [
                "skill",
                "jobs",
                "wait-status",
                "--job-id",
                "job-001",
                "--output",
                "json",
            ],
            stdout=stdout,
            stderr=stderr,
            product_identity={"adapter_id": "agent_cli"},
        )
        self.assertEqual(exit_code, 30)
        self.assertEqual(stderr.getvalue(), "")
        envelope = json.loads(stdout.getvalue())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["command"], "skill.jobs.wait-status")
        self.assertEqual(envelope["error"]["code"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(
            envelope["error"]["details"],
            {
                "capability_id": "JOB-02",
                "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            },
        )
        self.assertNotIn("job-001", stdout.getvalue())

        reference_text = "\n".join(
            (
                app_root / "skills" / "lulu-agent" / "references" / reference
            ).read_text(encoding="utf-8")
            for reference in ("workflows.md", "errors.md")
        )
        for token in (
            "AUTH_REQUIRED",
            "HUMAN_ACTION_REQUIRED",
            "RESOURCE_BUSY",
            "CONFLICT",
            "RECOVERY_REQUIRED",
            "PARTIAL",
        ):
            self.assertIn(token, reference_text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from src.core.agent_entry.transaction import (
    ACTIONS,
    STATES,
    AgentEntryTransaction,
    CrashInjected,
    EffectProbe,
    TransactionError,
)


class FakeEffect:
    def __init__(self, name: str, *, before: str = "before") -> None:
        self.name = name
        self.live = before
        self.calls: list[str] = []

    def probe(self) -> EffectProbe:
        return EffectProbe(self.live)

    def apply(self) -> None:
        self.calls.append("apply")
        self.live = "after"

    def rollback(self) -> None:
        self.calls.append("rollback")
        self.live = "before"


class Generation9AgentEntryTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "agent-entry"
        self.effects = [FakeEffect(name) for name in ("core_registration", "skill", "launcher")]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def manager(self) -> AgentEntryTransaction:
        return AgentEntryTransaction(self.root)

    @staticmethod
    def binding() -> dict[str, object]:
        return {
            "release_dmg_sha256": "a" * 64,
            "component_manifest_sha256": "b" * 64,
            "app_component_set_digest": "c" * 64,
            "installation_id_digest": "d" * 64,
            "candidate_generation": 9,
        }

    def execute(
        self,
        action: str,
        effects: list[FakeEffect],
        *,
        transaction_id: str,
        crash_after: str | None = None,
    ):
        return self.manager().execute(
            action,
            effects,
            transaction_id=transaction_id,
            binding=self.binding(),
            final_verifier=lambda: True,
            crash_after=crash_after,
        )

    def test_actions_and_states_are_closed_and_success_has_receipt(self) -> None:
        self.assertEqual(ACTIONS, ("install", "repair", "entry_update", "close", "rollback"))
        self.assertEqual(
            STATES,
            (
                "disabled",
                "installing",
                "ready",
                "update_required",
                "ownership_conflict",
                "rollback_complete",
                "rollback_incomplete",
            ),
        )
        result = self.execute("install", self.effects, transaction_id="tx-install-001")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["phase"], "COMMITTED")
        self.assertEqual(result["effect_count"], 3)
        receipt = self.root / "receipts" / "tx-install-001.json"
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(receipt.read_text())["state"], "ready")
        current = json.loads((self.root / "current.json").read_text())
        self.assertEqual(current["state"], "ready")
        self.assertEqual(current["phase"], "COMMITTED")
        self.assertEqual(current["binding"], self.binding())

        close_effects = [FakeEffect(name, before="after") for name in ("core_registration", "skill", "launcher")]
        closed = self.execute("close", close_effects, transaction_id="tx-close-001")
        self.assertEqual(closed["state"], "disabled")
        self.assertEqual(json.loads((self.root / "current.json").read_text())["state"], "disabled")

    def test_three_effects_each_recover_across_intent_effect_and_applied_windows(self) -> None:
        for effect_name in ("core_registration", "skill", "launcher"):
            for point in ("intent", "effect", "applied"):
                with self.subTest(effect=effect_name, point=point):
                    effects = [FakeEffect(name) for name in ("core_registration", "skill", "launcher")]
                    transaction_id = f"tx-{effect_name}-{point}"
                    with self.assertRaises(CrashInjected):
                        self.execute(
                            "install", effects, transaction_id=transaction_id,
                            crash_after=f"{effect_name}:{point}",
                        )
                    self.assertEqual(
                        json.loads((self.root / "current.json").read_text())["state"],
                        "installing",
                    )
                    recovered = self.manager().recover(transaction_id, effects)
                    self.assertEqual(recovered["state"], "rollback_complete")
                    self.assertTrue(all(effect.live == "before" for effect in effects))

    def test_create_only_and_third_party_or_drift_preserve_unknown_state(self) -> None:
        create_only = [FakeEffect(name, before="absent") for name in ("core_registration", "skill", "launcher")]
        result = self.execute("install", create_only, transaction_id="tx-create-only")
        self.assertEqual(result["state"], "ready")

        conflict = [FakeEffect("core_registration"), FakeEffect("skill", before="third_party"), FakeEffect("launcher")]
        with self.assertRaises(TransactionError) as caught:
            self.execute("repair", conflict, transaction_id="tx-conflict")
        self.assertEqual(caught.exception.state, "ownership_conflict")
        self.assertEqual(caught.exception.effect_count, 0)
        self.assertEqual(conflict[1].live, "third_party")

        drift = [FakeEffect(name) for name in ("core_registration", "skill", "launcher")]
        with self.assertRaises(CrashInjected):
            self.execute(
                "entry_update", drift, transaction_id="tx-drift",
                crash_after="skill:effect",
            )
        drift[1].live = "drift"
        recovered = self.manager().recover("tx-drift", drift)
        self.assertEqual(recovered["state"], "rollback_incomplete")
        self.assertEqual(drift[1].live, "drift")

    def test_final_verification_is_mandatory_before_ready(self) -> None:
        with self.assertRaises(TransactionError) as caught:
            self.manager().execute(
                "install",
                self.effects,
                transaction_id="tx-final-rejected",
                binding=self.binding(),
                final_verifier=lambda: False,
            )
        self.assertEqual(caught.exception.reason_code, "AGENT_ENTRY_FINAL_VERIFICATION_FAILED")
        self.assertEqual(caught.exception.state, "rollback_incomplete")
        current = json.loads((self.root / "current.json").read_text())
        self.assertEqual(current["state"], "rollback_incomplete")
        self.assertFalse((self.root / "receipts/tx-final-rejected.json").exists())


if __name__ == "__main__":
    unittest.main()

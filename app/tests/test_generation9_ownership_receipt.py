from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class FakeKeys:
    def __init__(self):
        self.values = {}

    def get_text(self, key):
        return self.values.get(key)

    def write_new(self, key, value):
        if key in self.values:
            raise RuntimeError("exists")
        self.values[key] = value


class Generation9OwnershipReceiptTests(unittest.TestCase):
    def test_core_only_hmac_receipt_binds_all_generation9_values_and_detects_tamper(self) -> None:
        from src.core.agent_entry.ownership import create_ownership_receipt, verify_ownership_receipt

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "ownership.json"
            keys = FakeKeys()
            created = create_ownership_receipt(
                path,
                key_store=keys,
                release_dmg_sha256="a" * 64,
                component_manifest_sha256="b" * 64,
                app_component_set_digest="c" * 64,
                installation_id="installation-fixture-0001",
                candidate_generation=9,
                managed_effect_digests={
                    "core_registration": "d" * 64,
                    "skill": "e" * 64,
                    "launcher": "f" * 64,
                },
            )
            self.assertEqual(created["schema_id"], "lulu.agent-entry-ownership/v2")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(verify_ownership_receipt(path, key_store=keys), created)
            changed = json.loads(path.read_text())
            changed["candidate_generation"] = 8
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "AGENT_ENTRY_OWNERSHIP_RECEIPT_INVALID"):
                verify_ownership_receipt(path, key_store=keys)

    def test_core_requires_committed_ready_action_receipt_bound_to_ownership(self) -> None:
        from src.core.agent_entry.ownership import verify_ready_action_receipt

        ownership = {
            "release_dmg_sha256": "a" * 64,
            "component_manifest_sha256": "b" * 64,
            "app_component_set_digest": "c" * 64,
            "installation_id_digest": "d" * 64,
            "candidate_generation": 9,
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "current.json"
            payload = {
                "schema_id": "lulu.agent-entry-current-action/v1",
                "transaction_id": "tx-install-001",
                "action": "install",
                "phase": "COMMITTED",
                "state": "ready",
                "effect_count": 3,
                "binding": dict(ownership),
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual(verify_ready_action_receipt(path, ownership=ownership), payload)

            payload["state"] = "installing"
            path.write_text(json.dumps(payload), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "AGENT_ENTRY_ACTION_RECEIPT_NOT_READY"):
                verify_ready_action_receipt(path, ownership=ownership)


if __name__ == "__main__":
    unittest.main()

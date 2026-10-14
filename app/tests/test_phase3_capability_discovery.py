"""Phase 3 bundled capability-discovery contract tests."""

from __future__ import annotations

from collections import Counter
import io
import json
import unittest


class Phase3CapabilityDiscoveryTests(unittest.TestCase):
    def test_bundled_snapshot_matches_fixed_normative_catalog_identity(self):
        from src.agent_cli.catalog import load_capability_catalog

        snapshot = load_capability_catalog()
        source = snapshot["source_catalog"]
        catalog = snapshot["catalog"]
        capabilities = catalog["capabilities"]

        self.assertEqual(
            source,
            {
                "path": "docs/spec/lulu-agent-cli-contract-catalog.md",
                "sha256": "3c7f2becf77765a88cd306121919be335db106c2f6640fc74e5b3f959be3bdea",
                "normative_json_sha256": "c142b0b0c3d6167b4ae7b10ab97aa59b571ee7c9c2d5f53e7a88848decbcdb9a",
            },
        )
        self.assertEqual(snapshot["snapshot_kind"], "lulu_agent_cli_runtime_catalog")
        self.assertEqual(snapshot["snapshot_version"], "1.0.0")
        self.assertEqual(catalog["catalog_kind"], "lulu_agent_cli_capability_contract")
        self.assertEqual(catalog["catalog_version"], "1.0.0")
        self.assertEqual(
            catalog["source_matrix"],
            {
                "path": "docs/spec/lulu-agent-cli-capability-matrix.md",
                "sha256": "5b66d00dc50b568bf8558fa8995ce95a918bcef29c0b8b72e249d9538d5418f9",
                "expected_capability_count": 143,
            },
        )
        ids = [item["capability_id"] for item in capabilities]
        self.assertEqual(len(ids), 143)
        self.assertEqual(len(set(ids)), 143)
        self.assertEqual(
            Counter(item["current_cli_state"] for item in capabilities),
            Counter({"unavailable": 135, "gui_only": 8}),
        )
        self.assertEqual(
            {
                item["capability_id"]
                for item in capabilities
                if item["current_cli_state"] == "gui_only"
            },
            {
                "SYS-08",
                "AUTH-06",
                "REC-05",
                "COL-18",
                "LIB-18",
                "OUT-06",
                "TTS-14",
                "INT-12",
            },
        )
        self.assertEqual(
            next(item for item in capabilities if item["capability_id"] == "TR-02")[
                "target_state"
            ],
            "available",
        )
        self.assertEqual(
            next(item for item in capabilities if item["capability_id"] == "TR-02")[
                "current_cli_state"
            ],
            "unavailable",
        )

    def test_capabilities_projects_all_current_states_without_target_state_escalation(self):
        from src.agent_cli.main import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            ["capabilities", "--output", "json"],
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schema_id"], "lulu.agent/v1")
        self.assertEqual(payload["schema_revision"], 1)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "capabilities")
        result = payload["result"]
        # Current workspace commands extend the immutable legacy matrix.
        from src.core.agent_workspace.contracts import CAPABILITIES
        from src.core.link_assets import CAPABILITIES as EXTRACTION_CAPABILITIES
        current_count = len(CAPABILITIES) + len(EXTRACTION_CAPABILITIES)
        self.assertEqual(result["capability_count"], 139 + current_count)
        self.assertEqual(result["state_counts"], {"unavailable": 132 + current_count, "gui_only": 7})
        self.assertEqual(
            result["source_catalog_sha256"],
            "3c7f2becf77765a88cd306121919be335db106c2f6640fc74e5b3f959be3bdea",
        )
        self.assertEqual(
            result["source_matrix_sha256"],
            "5b66d00dc50b568bf8558fa8995ce95a918bcef29c0b8b72e249d9538d5418f9",
        )
        capabilities = result["capabilities"]
        self.assertEqual(len(capabilities), 139 + current_count)
        self.assertEqual({c['capability_id'] for c in capabilities if c['capability_id'].startswith('WS-')}, CAPABILITIES)
        self.assertEqual(
            Counter(item["state"] for item in capabilities),
            Counter({"unavailable": 132 + current_count, "gui_only": 7}),
        )
        self.assertFalse(any(item["state"] in {"available", "degraded"} for item in capabilities))
        self.assertFalse(any(item["executable"] for item in capabilities))
        self.assertTrue(
            all(
                item["reason_code"] == (
                    "LICENSE_FEATURE_RETIRED" if item["capability_id"].startswith("AUTH-")
                    else "TRUSTED_RUNTIME_REQUIRED" if item["capability_id"].startswith("WS-")
                    else "LINK_EXTRACTION_RUNTIME_UNAVAILABLE" if item['capability_id'] in EXTRACTION_CAPABILITIES
                    else "FORMAL_CLI_NOT_IMPLEMENTED"
                )
                for item in capabilities
                if item["state"] == "unavailable"
            )
        )
        col_05 = next(item for item in capabilities if item["capability_id"] == "COL-05")
        self.assertEqual(col_05["reason_code"], "FORMAL_CLI_NOT_IMPLEMENTED")
        tr_02 = next(item for item in capabilities if item["capability_id"] == "TR-02")
        self.assertEqual(tr_02["state"], "unavailable")
        self.assertEqual(tr_02["display_target_state"], "available")


if __name__ == "__main__":
    unittest.main()

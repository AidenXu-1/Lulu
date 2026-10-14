import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1]
GATE = runpy.run_path(str(APP / "packaging/product_baseline_gate.py"))
CAPABILITY_GATE_PATH = APP / "packaging/product_capability_gate.py"


class ProductBaselineGateTests(unittest.TestCase):
    def _commit(self, repo: Path, message: str, name: str) -> str:
        (repo / name).write_text(message + "\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(repo), "add", name], check=True)
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repo),
                "-c",
                "user.name=Lulu Product Gate",
                "-c",
                "user.email=lulu-product-gate@example.invalid",
                "commit",
                "-qm",
                message,
            ],
            check=True,
        )
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_candidate_must_descend_from_complete_product_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
            old = self._commit(repo, "old incomplete product", "old.txt")
            baseline = self._commit(repo, "accepted complete product", "complete.txt")
            candidate = self._commit(repo, "post-release repairs", "repairs.txt")

            receipt = GATE["verify_product_baseline"](
                repo=repo,
                candidate=candidate,
                baseline=baseline,
            )
            self.assertTrue(receipt["checks"]["candidate_descends_from_baseline"])
            self.assertEqual(receipt["candidate_commit"], candidate)
            self.assertEqual(receipt["baseline_commit"], baseline)

            with self.assertRaisesRegex(SystemExit, "完整产品基线"):
                GATE["verify_product_baseline"](
                    repo=repo,
                    candidate=old,
                    baseline=baseline,
                )

    def test_receipt_is_atomic_and_binds_candidate_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
            baseline = self._commit(repo, "accepted complete product", "complete.txt")
            candidate = self._commit(repo, "repair", "repair.txt")
            output = Path(temp) / "receipt.json"

            receipt = GATE["write_product_baseline_receipt"](
                output,
                GATE["verify_product_baseline"](
                    repo=repo,
                    candidate=candidate,
                    baseline=baseline,
                ),
            )

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), receipt)
            self.assertRegex(receipt["candidate_tree"], r"^[0-9a-f]{40}$")
            self.assertEqual(receipt["schema_version"], 1)

    def test_real_lulu_head_inherits_the_accepted_complete_product(self):
        receipt = GATE["verify_product_baseline"](
            repo=APP.parent,
            candidate="HEAD",
            baseline=os.environ.get("LULU_TEST_PRODUCT_BASELINE") or GATE["COMPLETE_PRODUCT_BASELINE"],
        )
        self.assertTrue(receipt["checks"]["candidate_descends_from_baseline"])

    def test_every_package_profile_invokes_gate_and_preserves_receipt(self):
        build = (APP / "packaging/build_app.sh").read_text(encoding="utf-8")
        self.assertIn("packaging/product_baseline_gate.py", build)
        self.assertIn('--candidate HEAD', build)
        self.assertIn('product-baseline-receipt.json', build)
        self.assertIn('cp "$BASELINE_RECEIPT" "$DIST_DIR/product-baseline-receipt.json"', build)

    def test_capability_gate_names_every_critical_user_surface(self):
        capability_gate = runpy.run_path(str(CAPABILITY_GATE_PATH))
        test_ids = capability_gate["CAPABILITY_TESTS"]
        joined = "\n".join(test_ids)
        for required_surface in (
            "collection_mode_control_persists_and_rolls_back",
            "selected_row_background_is_opaque_and_continuous_across_columns",
            "folder_click_selects_and_clears_all_member_entries",
            "pending_and_interrupted_tasks_restore_without_starting_worker",
            "pipeline_maps_real_engine_progress_into_transcription_stage",
            "douyin_official_fake_ip_without_system_proxy_uses_verified_transparent_tls",
            "homepage_media_fetch_retries_work_page_and_forces_video",
            "origin_referer_survives_interrupted_range_resume",
            "export_to_new_bitable_flow",
            "selected_downloaded_model_delete_entry_remains_clickable",
            "deleting_only_ready_selected_model_is_allowed_with_offline_warning",
            "same_tts_model_in_use_is_blocked_without_blocking_other_models",
            "set_text_strips_transcript_document_metadata_and_aligns_source_snapshot",
            "forced_persistent_worker_exit_does_not_load_model_twice",
            "agnes_length_truncation_retries_only_that_segment_in_smaller_safe_parts",
            "all_user_facing_directions_request_delivery_mode_for_both_providers",
            "proofread_protects_timestamps_numbers_and_links_before_model_call",
            "proofread_requests_plain_body_without_fragile_output_wrapper",
            "test_connection_probe_has_enough_output_budget_for_current_models",
            "worker_reports_real_retained_bytes_and_speed_while_backend_is_quiet",
            "runtime_archive_download_resumes_partial_bytes",
            "runtime_archive_uses_small_verified_ranges_until_complete",
            "ignored_range_response_never_truncates_existing_partial",
            "runtime_archive_keeps_partial_after_bounded_retries_are_exhausted",
            "success_cleanup_removes_update_artifacts_but_never_follows_links",
        ):
            self.assertIn(required_surface, joined)

    def test_capability_gate_test_names_resolve_without_duplicates(self):
        names = runpy.run_path(str(CAPABILITY_GATE_PATH))["CAPABILITY_TESTS"]
        self.assertEqual(len(names), len(set(names)))
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromNames(names)
        self.assertEqual(loader.errors, [], "Packaging must not reference renamed or missing tests")
        self.assertEqual(suite.countTestCases(), len(names))

    def test_capability_gate_runs_real_contracts_and_writes_bound_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "capabilities.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(CAPABILITY_GATE_PATH),
                    "--repo",
                    str(APP.parent),
                    "--candidate",
                    "HEAD",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertGreaterEqual(receipt["passed_test_count"], 9)
            self.assertRegex(receipt["candidate_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(receipt["candidate_tree"], r"^[0-9a-f]{40}$")
            self.assertEqual(receipt["status"], "passed")

    def test_every_package_profile_requires_capability_gate_receipt(self):
        build = (APP / "packaging/build_app.sh").read_text(encoding="utf-8")
        self.assertIn("packaging/product_capability_gate.py", build)
        self.assertIn("product-capabilities-receipt.json", build)
        self.assertIn(
            'cp "$CAPABILITY_RECEIPT" "$DIST_DIR/product-capabilities-receipt.json"',
            build,
        )


if __name__ == "__main__":
    unittest.main()

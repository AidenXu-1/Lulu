"""trusted_test 小红书主页采集四层发布门的离线合同。"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import release_features  # noqa: E402
from src.core.collect import adapters  # noqa: E402
from src.core.collect.application import (  # noqa: E402
    CollectionApplicationService,
    CollectionCommand,
    XHS_RELEASE_DISABLED_MESSAGE,
)


XHS_URL = "https://www.xiaohongshu.com/user/profile/public_brand"
SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "b" * 40
SPEC = Path(__file__).resolve().parents[1] / "packaging/Lulu.spec"
XHS_RUNTIME_MODULES = (
    "src.core.collect.adapters.xiaohongshu",
    "src.core.collect.xhs_browser",
    "src.core.collect.xhs_process",
    "src.core.collect.xhs_process_worker",
)


class CollectionReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write_profile(self, profile: str) -> None:
        (self.root / "build_profile.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "profile": profile,
                    "source_commit": SOURCE_COMMIT,
                    "source_tree": SOURCE_TREE,
                }
            ),
            encoding="utf-8",
        )

    def _write_trusted_features(self) -> None:
        (self.root / "release_features.json").write_text(
            json.dumps(release_features.manifest_payload()),
            encoding="utf-8",
        )

    def _frozen(self):
        return (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.root), create=True),
        )

    def test_only_trusted_test_reads_sealed_false_feature(self):
        for profile, expected in (
            ("internal_test", True),
            ("external_candidate", True),
            ("trusted_test", False),
        ):
            with self.subTest(profile=profile):
                self._write_profile(profile)
                if profile == "trusted_test":
                    self._write_trusted_features()
                else:
                    (self.root / "release_features.json").unlink(missing_ok=True)
                frozen, root = self._frozen()
                with frozen, root:
                    self.assertIs(
                        adapters.homepage_collection_available("xiaohongshu"),
                        expected,
                    )

    def test_trusted_gate_ignores_env_argv_and_fails_closed_on_manifest_drift(self):
        self._write_profile("trusted_test")
        manifest = release_features.manifest_payload()
        manifest["features"]["homepage_collection"]["xiaohongshu"] = True
        (self.root / "release_features.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        frozen, root = self._frozen()
        with (
            frozen,
            root,
            patch.dict(
                os.environ,
                {
                    "LULU_BUILD_PROFILE_FOR_SPEC": "external_candidate",
                    "LULU_XHS_HOMEPAGE_COLLECTION": "1",
                },
            ),
            patch.object(sys, "argv", ["lulu", "--enable-xhs"]),
        ):
            capability = adapters.release_adapter_capability("xiaohongshu")
            self.assertFalse(capability.available)
            self.assertEqual(capability.reason, "release_disabled")
            with self.assertRaises(adapters.AdapterUnavailableError) as caught:
                adapters.resolve_adapter("xiaohongshu")
        self.assertEqual(caught.exception.reason, "release_disabled")

    def test_source_mode_preserves_douyin_xhs_and_bilibili_contracts(self):
        with patch.object(sys, "frozen", False, create=True):
            douyin = adapters.release_adapter_capability("douyin")
            xhs = adapters.release_adapter_capability("xiaohongshu")
            bilibili = adapters.release_adapter_capability("bilibili")
        self.assertTrue(douyin.available)
        self.assertTrue(xhs.available)
        self.assertFalse(bilibili.available)
        self.assertEqual(
            bilibili.reason,
            "official_homepage_contract_unverified",
        )

    def test_trusted_service_never_constructs_xhs_runtime(self):
        self._write_profile("trusted_test")
        self._write_trusted_features()
        touched = []
        frozen, root = self._frozen()
        with (
            frozen,
            root,
            patch(
                "src.core.collect.application._default_xhs_browser_manager",
                side_effect=AssertionError("manager construction is forbidden"),
            ),
            patch(
                "src.core.collect.application._xhs_process_factory_type",
                side_effect=AssertionError("worker construction is forbidden"),
            ),
        ):
            service = CollectionApplicationService(
                adapter_resolver=lambda platform: touched.append(
                    ("adapter", platform)
                ),
                session_factory=lambda *args: touched.append(("session", args)),
            )
            entry = service.inspect(XHS_URL)
            result = service.collect(
                CollectionCommand(XHS_URL, "profile_public", profile_id="default")
            )
            status_codes = {
                service.xhs_browser_status(refresh=True).code,
                service.open_xhs_browser().code,
                service.close_xhs_browser().code,
                service.release_xhs_browser_control().code,
            }
            login = service.run_profile_login("xiaohongshu")

        self.assertEqual(
            (entry.capability.available, entry.capability.reason),
            (False, "release_disabled"),
        )
        self.assertEqual(result.next_action, XHS_RELEASE_DISABLED_MESSAGE)
        self.assertEqual(status_codes, {"release_disabled"})
        self.assertEqual(login, "release_disabled")
        self.assertEqual(touched, [])

    def test_spec_excludes_exactly_four_xhs_modules_only_for_trusted(self):
        source = SPEC.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "TRUSTED_XHS_EXCLUDES"
                for target in node.targets
            )
        )
        self.assertIsInstance(assignment.value, ast.IfExp)
        condition = ast.unparse(assignment.value.test)
        self.assertEqual(condition, "BUILD_PROFILE == 'trusted_test'")
        values = tuple(
            item.value
            for item in assignment.value.body.elts
            if isinstance(item, ast.Constant)
        )
        self.assertEqual(values, XHS_RUNTIME_MODULES)
        self.assertIsInstance(assignment.value.orelse, ast.Tuple)
        self.assertEqual(assignment.value.orelse.elts, [])
        self.assertIn("*TRUSTED_XHS_EXCLUDES", source)
        self.assertIn(
            'PRODUCT_SOURCE_FILE = os.path.join(APP_DIR, "product_source.json")',
            source,
        )
        self.assertIn('datas += [(PRODUCT_SOURCE_FILE, ".")]', source)


if __name__ == "__main__":
    unittest.main()

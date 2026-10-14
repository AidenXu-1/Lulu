from __future__ import annotations

import hashlib
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

from src.core.updater.constants import (
    ALLOWED_GITHUB_RELEASE_PREFIXES,
    ALLOWED_RELEASE_REPOSITORIES,
    CANONICAL_RELEASE_REPOSITORY,
    LEGACY_RELEASE_REPOSITORY,
    MANIFEST_URL,
)


class UpdateReleaseRepositoryMigrationTests(unittest.TestCase):
    def test_release_sha_sidecar_is_portable_from_its_own_directory(self):
        root = Path(__file__).resolve().parents[1]
        helper = root / "packaging/write_sha256_sidecar.sh"
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "release"
            release.mkdir()
            artifact = release / "Lulu-test-arm64.dmg"
            artifact.write_bytes(b"portable-release-fixture")

            subprocess.run(
                ["bash", str(helper), str(artifact)],
                check=True,
                capture_output=True,
                text=True,
            )

            expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
            sidecar = artifact.with_name(artifact.name + ".sha256")
            self.assertEqual(
                sidecar.read_text(encoding="utf-8"),
                f"{expected}  {artifact.name}\n",
            )
            verified = subprocess.run(
                ["shasum", "-a", "256", "-c", sidecar.name],
                cwd=release,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn(f"{artifact.name}: OK", verified.stdout)

    def test_runtime_repository_contract_is_exact(self):
        self.assertEqual(CANONICAL_RELEASE_REPOSITORY, "AidenXu-1/Lulu")
        self.assertEqual(LEGACY_RELEASE_REPOSITORY, "AidenXu-1/lulu-releases")
        self.assertEqual(
            ALLOWED_RELEASE_REPOSITORIES,
            {"AidenXu-1/Lulu", "AidenXu-1/lulu-releases"},
        )
        self.assertEqual(
            set(ALLOWED_GITHUB_RELEASE_PREFIXES),
            {
                "/AidenXu-1/Lulu/releases/",
                "/AidenXu-1/lulu-releases/releases/",
            },
        )
        self.assertEqual(
            MANIFEST_URL,
            "https://github.com/AidenXu-1/Lulu/releases/latest/download/version.json",
        )

    def test_live_probe_can_simulate_current_and_legacy_client_policies(self):
        root = Path(__file__).resolve().parents[1]
        probe = runpy.run_path(str(root / "packaging/probe_update_release.py"))
        self.assertEqual(
            probe["release_urls"]("AidenXu-1/Lulu"),
            (
                "https://github.com/AidenXu-1/Lulu/releases/latest/download/version.json",
                "https://github.com/AidenXu-1/Lulu/releases/latest/download/version.json.sig",
            ),
        )
        self.assertEqual(
            probe["client_prefixes"]("legacy-0.2.13"),
            ("/AidenXu-1/lulu-releases/releases/",),
        )
        self.assertEqual(
            set(probe["client_prefixes"]("current")),
            {
                "/AidenXu-1/Lulu/releases/",
                "/AidenXu-1/lulu-releases/releases/",
            },
        )
        with self.assertRaises(ValueError):
            probe["release_urls"]("other/repository")

    def test_release_builder_requires_explicit_repository(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "packaging/make_update_release.py").read_text(encoding="utf-8")
        script = (root / "packaging/build_release.sh").read_text(encoding="utf-8")
        settings = (root / "src/ui/settings_page.py").read_text(encoding="utf-8")
        self.assertIn('p.add_argument("--repository",choices=ALLOWED_RELEASE_REPOSITORIES,required=True)', source)
        self.assertIn('LULU_UPDATE_REPOSITORY', script)
        self.assertNotIn('default="AidenXu-1/Lulu"', source)
        self.assertIn(
            'REPOSITORY_URL = "https://github.com/AidenXu-1/Lulu"', settings
        )
        self.assertNotIn(
            'REPOSITORY_URL = "https://github.com/AidenXu-1/lulu-releases"',
            settings,
        )


if __name__ == "__main__":
    unittest.main()

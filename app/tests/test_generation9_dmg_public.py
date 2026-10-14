from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "app/packaging/create_generation9_dmg.py"
APP_ROOT = ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def _canonical_app_tree(app: Path) -> str:
    import stat

    records: list[dict[str, object]] = []
    for base, directories, names in os.walk(app, topdown=True, followlinks=False):
        root = Path(base)
        for name in directories:
            path = root / name
            metadata = path.lstat()
            records.append(
                {
                    "path": path.relative_to(app).as_posix(),
                    "type": "directory",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                }
            )
        for name in names:
            path = root / name
            if path.relative_to(app).as_posix() == "Contents/Resources/agent_product_manifest.json":
                continue
            raw = path.read_bytes()
            metadata = path.lstat()
            records.append(
                {
                    "path": path.relative_to(app).as_posix(),
                    "type": "regular",
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    records.sort(key=lambda value: str(value["path"]).encode("utf-8"))
    return hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _fixture_app(root: Path, *, marker: bytes = b"candidate-one\n") -> Path:
    app = root / "Lulu.app"
    executable = app / "Contents/MacOS/Lulu"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(marker)
    executable.chmod(0o755)
    resources = app / "Contents/Resources"
    resources.mkdir()
    manifest = {
        "schema_id": "lulu.agent-component-manifest/v1",
        "product_version": "0.3.2",
        "product_build": "20260904.1",
        "source_revision": "1" * 40,
        "candidate_generation": 9,
        "app_component_set_digest": _canonical_app_tree(app),
        "contains_secret_material": False,
    }
    (resources / "agent_product_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return app


class Generation9DmgPublicTests(unittest.TestCase):
    def _create_payload_dmg(self, root: Path, payload: Path, *, name: str) -> Path:
        output = root / name
        result = subprocess.run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-volname",
                "Lulu security fixture",
                "-srcfolder",
                str(payload),
                "-format",
                "UDZO",
                str(output),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")
        return output

    def _create(self, root: Path, app: Path) -> tuple[Path, dict[str, object]]:
        output = root / "Lulu-0.3.2-20260904.1-arm64.dmg"
        receipt = root / "create-receipt.json"
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "create",
                "--app",
                str(app),
                "--output",
                str(output),
                "--receipt",
                str(receipt),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")
        return output, json.loads(result.stdout)

    def _verify(
        self,
        root: Path,
        dmg: Path,
        *,
        expected_sha256: str,
        expected_app: Path,
        receipt_name: str,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        receipt = root / receipt_name
        result = subprocess.run(
            [
                sys.executable,
                str(TOOL),
                "verify",
                "--dmg",
                str(dmg),
                "--expected-sha256",
                expected_sha256,
                "--expected-app",
                str(expected_app),
                "--receipt",
                str(receipt),
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        return result, receipt

    def test_create_returns_real_readonly_mounted_dmg_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = _fixture_app(root / "candidate")
            output = root / "Lulu-0.3.2-20260904.1-arm64.dmg"
            receipt = root / "receipt.json"

            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOL),
                    "create",
                    "--app",
                    str(app),
                    "--output",
                    str(output),
                    "--receipt",
                    str(receipt),
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"stdout={result.stdout}\nstderr={result.stderr}",
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload, json.loads(receipt.read_text(encoding="utf-8")))
            self.assertEqual(payload["schema_id"], "lulu.generation9-dmg-verification/v1")
            self.assertTrue(payload["verified"])
            self.assertEqual(payload["release_dmg_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(payload["app_component_set_digest"], _canonical_app_tree(app))
            self.assertTrue(payload["mounted_readonly"])
            self.assertTrue(payload["detached"])

    def test_verify_rejects_one_byte_change_without_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = _fixture_app(root / "candidate")
            dmg, created = self._create(root, app)
            with dmg.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                original = stream.read(1)
                stream.seek(-1, os.SEEK_END)
                stream.write(bytes([original[0] ^ 1]))

            result, receipt = self._verify(
                root,
                dmg,
                expected_sha256=str(created["release_dmg_sha256"]),
                expected_app=app,
                receipt_name="one-byte-receipt.json",
            )

            self.assertEqual(result.returncode, 60)
            self.assertEqual(json.loads(result.stdout)["reason_code"], "PACKAGE_HASH_MISMATCH")
            self.assertFalse(receipt.exists())

    def test_verify_rejects_wrong_expected_sha_without_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = _fixture_app(root / "candidate")
            dmg, _created = self._create(root, app)

            result, receipt = self._verify(
                root,
                dmg,
                expected_sha256="0" * 64,
                expected_app=app,
                receipt_name="wrong-sha-receipt.json",
            )

            self.assertEqual(result.returncode, 60)
            self.assertEqual(json.loads(result.stdout)["reason_code"], "PACKAGE_HASH_MISMATCH")
            self.assertFalse(receipt.exists())

    def test_verify_rejects_different_app_payload_without_receipt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            packaged_app = _fixture_app(root / "packaged", marker=b"packaged-app\n")
            expected_app = _fixture_app(root / "expected", marker=b"different-app\n")
            dmg, created = self._create(root, packaged_app)

            result, receipt = self._verify(
                root,
                dmg,
                expected_sha256=str(created["release_dmg_sha256"]),
                expected_app=expected_app,
                receipt_name="wrong-payload-receipt.json",
            )

            self.assertEqual(result.returncode, 60)
            self.assertEqual(json.loads(result.stdout)["reason_code"], "PACKAGE_PAYLOAD_MISMATCH")
            self.assertFalse(receipt.exists())

    def test_packaged_runtime_provisions_private_receipt_from_selected_dmg(self) -> None:
        from src.core.agent_entry.formal_install import provision_verified_dmg_receipt

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = _fixture_app(root / "installed")
            dmg, created = self._create(root, app)
            receipt = root / "state/verified-dmg.json"

            payload = provision_verified_dmg_receipt(
                dmg,
                expected_app=app,
                receipt_path=receipt,
            )

            self.assertEqual(payload, json.loads(receipt.read_text(encoding="utf-8")))
            self.assertEqual(payload["release_dmg_sha256"], created["release_dmg_sha256"])
            self.assertEqual(payload["app_component_set_digest"], _canonical_app_tree(app))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def test_packaged_runtime_receipt_rejects_one_byte_change_before_install(self) -> None:
        from src.core.agent_entry.formal_install import (
            _load_dmg_receipt,
            _verify_dmg_binding,
            provision_verified_dmg_receipt,
        )

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = _fixture_app(root / "installed")
            dmg, _created = self._create(root, app)
            receipt = root / "state/verified-dmg.json"
            provision_verified_dmg_receipt(
                dmg,
                expected_app=app,
                receipt_path=receipt,
            )
            with dmg.open("r+b") as stream:
                stream.seek(-1, os.SEEK_END)
                original = stream.read(1)
                stream.seek(-1, os.SEEK_END)
                stream.write(bytes([original[0] ^ 1]))

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_HASH_MISMATCH"):
                _verify_dmg_binding(dmg, _load_dmg_receipt(receipt))

            self.assertTrue(receipt.exists())

    def test_packaged_runtime_rejects_wrong_app_without_private_receipt(self) -> None:
        from src.core.agent_entry.formal_install import provision_verified_dmg_receipt

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            packaged_app = _fixture_app(root / "packaged", marker=b"packaged-app\n")
            installed_app = _fixture_app(root / "installed", marker=b"different-app\n")
            dmg, _created = self._create(root, packaged_app)
            receipt = root / "state/verified-dmg.json"

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_PAYLOAD_MISMATCH"):
                provision_verified_dmg_receipt(
                    dmg,
                    expected_app=installed_app,
                    receipt_path=receipt,
                )

            self.assertFalse(receipt.exists())

    def test_packaged_runtime_rejects_root_app_absolute_symlink_escape(self) -> None:
        from src.core.agent_entry.formal_install import provision_verified_dmg_receipt

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed_app = _fixture_app(root / "installed")
            payload = root / "absolute-payload"
            payload.mkdir()
            (payload / "Lulu.app").symlink_to(installed_app, target_is_directory=True)
            dmg = self._create_payload_dmg(
                root,
                payload,
                name="absolute-symlink-escape.dmg",
            )
            receipt = root / "absolute-state/verified-dmg.json"

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_PAYLOAD_MISMATCH"):
                provision_verified_dmg_receipt(
                    dmg,
                    expected_app=installed_app,
                    receipt_path=receipt,
                )

            self.assertFalse(receipt.exists())

    def test_packaged_runtime_rejects_root_app_relative_symlink_escape(self) -> None:
        from src.core.agent_entry.formal_install import provision_verified_dmg_receipt

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed_app = _fixture_app(root / "installed")
            payload = root / "relative-payload"
            payload.mkdir()
            relative_escape = Path("..") / root.name / "installed/Lulu.app"
            (payload / "Lulu.app").symlink_to(relative_escape, target_is_directory=True)
            dmg = self._create_payload_dmg(
                root,
                payload,
                name="relative-symlink-escape.dmg",
            )
            receipt = root / "relative-state/verified-dmg.json"

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_PAYLOAD_MISMATCH"):
                provision_verified_dmg_receipt(
                    dmg,
                    expected_app=installed_app,
                    receipt_path=receipt,
                )

            self.assertFalse(receipt.exists())

    def test_packaged_runtime_rejects_nested_app_symlink_escape(self) -> None:
        from src.core.agent_entry.formal_install import provision_verified_dmg_receipt

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed_app = _fixture_app(root / "installed")
            payload = root / "nested-payload"
            mounted_app = payload / "Lulu.app"
            mounted_app.mkdir(parents=True)
            (mounted_app / "Contents").symlink_to(
                installed_app / "Contents",
                target_is_directory=True,
            )
            dmg = self._create_payload_dmg(
                root,
                payload,
                name="nested-symlink-escape.dmg",
            )
            receipt = root / "nested-state/verified-dmg.json"

            with self.assertRaisesRegex(RuntimeError, "PACKAGE_PAYLOAD_MISMATCH"):
                provision_verified_dmg_receipt(
                    dmg,
                    expected_app=installed_app,
                    receipt_path=receipt,
                )

            self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()

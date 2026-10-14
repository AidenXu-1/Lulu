"""2.5 resource migration/import is isolated from old models and the network."""
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.core.artifact_trust import RuntimeArtifact
from src.core.synthesis import runtime_packages as rp


class IndexTTS25InstallTests(unittest.TestCase):
    def test_archive_metadata_is_never_extracted_as_python_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'fixture.tar'
            with tarfile.open(archive, 'w') as bundle:
                for name, content in [('python/lib/module.py', b'# valid'), ('python/lib/._module.py', b'\x00\x05\xa3binary')]:
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
            destination = root / 'extracted'
            destination.mkdir()
            with tarfile.open(archive) as bundle:
                rp._safe_extract_runtime(bundle, destination, lambda: False)
            self.assertTrue((destination / 'python/lib/module.py').is_file())
            self.assertFalse((destination / 'python/lib/._module.py').exists())

    def test_versioned_storage_preserves_old_resources(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(rp, "tts_root", return_value=Path(directory)):
            old = Path(directory) / "local_indextts2" / "keep.txt"
            old.parent.mkdir()
            old.write_text("old voice/model")
            self.assertEqual(rp.model_dir("local_indextts2").name, "local_indextts2_5")
            self.assertEqual(old.read_text(), "old voice/model")
            self.assertEqual(rp.model_dir("local_voxcpm2").name, "local_voxcpm2")

    def test_local_archive_is_verified_and_does_not_download_or_consume_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                entry = tarfile.TarInfo("python/bin/python3.11")
                payload = b"fixture only"
                entry.size, entry.mode = len(payload), 0o755
                bundle.addfile(entry, io.BytesIO(payload))
            artifact = RuntimeArtifact("local_indextts2", "mac-arm64", "fixture", archive.name,
                                       hashlib.sha256(archive.read_bytes()).hexdigest(), archive.stat().st_size)
            with patch.object(rp, "tts_root", return_value=root / "tts"), patch.object(rp.mm, "app_data_dir", return_value=root / "appdata"), patch.object(rp, "current_platform", return_value="mac-arm64"), patch.dict(rp.TTS_RUNTIME_ARTIFACTS, {("local_indextts2", "mac-arm64"): artifact}), patch.object(rp, "_download", side_effect=AssertionError("network")):
                rp.install_runtime("local_indextts2", local_archive=archive)
                self.assertTrue(rp.is_runtime_installed("local_indextts2"))
                self.assertTrue(archive.is_file())

    def test_unpublished_runtime_fails_before_network_or_storage_mutation(self):
        with patch.object(rp, "is_runtime_installed", return_value=False), patch.object(rp, "current_platform", return_value="mac-arm64"), patch.object(rp, "_download", side_effect=AssertionError("network")), patch.object(rp, "model_dir", side_effect=AssertionError("storage")):
            with self.assertRaisesRegex(RuntimeError, "安装资源暂未上线"):
                rp.install_runtime("local_indextts2")

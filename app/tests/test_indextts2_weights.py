"""IndexTTS2 权重处理:主权重 + 锁定辅助快照(不触网)。"""

import tempfile
import unittest
from pathlib import Path

from src.core.synthesis import indextts2_weights as iw


class IndexTTS2WeightsTest(unittest.TestCase):
    def test_main_and_auxiliary_repositories_are_all_pinned(self):
        self.assertEqual(iw.HF_REPO, "IndexTeam/IndexTTS-2.5")
        self.assertEqual(
            set(iw.AUXILIARY_REPOSITORIES),
            {
                "facebook/w2v-bert-2.0",
                "funasr/campplus",
                "nvidia/bigvgan_v2_22khz_80band_256x",
            },
        )
        self.assertTrue(all(len(revision) == 40 for revision in iw.AUXILIARY_REPOSITORIES.values()))
        trust_repos = {item["repo"] for item in iw._weights_trust()["repositories"]}
        self.assertEqual(trust_repos, {iw.HF_REPO, *iw.AUXILIARY_REPOSITORIES})

    def test_checkpoints_dir_is_weights_root(self):
        root = Path("/tmp/it2/checkpoints")
        self.assertEqual(iw.checkpoints_dir(root), root)

    def test_is_installed_requires_all_key_assets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertFalse(iw.is_installed(root))  # 空 → 未就绪
            # 造齐关键文件 + 目录
            for f in iw._REQUIRED_FILES:
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x")
            (root / "qwen0.6bemo4-merge").mkdir(parents=True, exist_ok=True)
            (root / "qwen0.6bemo4-merge" / "model.safetensors").write_bytes(b"x")
            w2v = root / "hf_cache" / "w2v-bert-2.0"
            w2v.mkdir(parents=True, exist_ok=True)
            (w2v / "model.safetensors").write_bytes(b"x")
            self.assertFalse(iw.is_installed(root))
            receipts = root.parent / "receipts"
            iw._write_install_receipt(root, receipt_base=receipts)
            self.assertTrue(iw.is_installed(root, receipt_base=receipts))
            # 缺重头权重 → 不就绪
            (root / "gpt.pth").unlink()
            self.assertFalse(iw.is_installed(root, receipt_base=receipts))

    def test_status_marks_partial_and_lists_missing_heavy_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "gpt.pth").write_bytes(b"x")
            st = iw.status(root)
            self.assertFalse(st.installed)
            self.assertTrue(st.partial)
            self.assertIn("hf_cache/w2v-bert-2.0/model.safetensors", st.missing_files)
            self.assertIn("hf_cache/bigvgan/bigvgan_generator.pt", st.missing_files)

    def test_install_downloads_main_and_all_auxiliary_repositories_to_runtime_layout(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            calls = []
            events = []

            def fake_snapshot(repo, local_dir, **kwargs):
                calls.append((repo, kwargs))
                base = Path(local_dir)
                if repo == iw.HF_REPO:
                    for f in iw._REQUIRED_FILES:
                        if f.startswith("hf_cache/"):
                            continue
                        p = base / f
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(b"x")
                    (base / "qwen0.6bemo4-merge" / "model.safetensors").parent.mkdir(parents=True, exist_ok=True)
                    (base / "qwen0.6bemo4-merge" / "model.safetensors").write_bytes(b"x")
                    return
                if repo == "facebook/w2v-bert-2.0":
                    (base / "model.safetensors").parent.mkdir(parents=True, exist_ok=True)
                    (base / "model.safetensors").write_bytes(b"w2v")
                    return
                for rel in kwargs.get("allow_patterns") or []:
                    p = base / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b"aux")

            receipts = root.parent / "receipts"
            iw.install(root, lambda m, f: events.append(m), lambda: False, _snapshot_download=fake_snapshot, _receipt_dir=receipts)
            self.assertTrue(iw.is_installed(root, receipt_base=receipts))
            self.assertEqual({repo for repo, _kwargs in calls}, {iw.HF_REPO, *iw.AUXILIARY_REPOSITORIES})
            for repo, kwargs in calls:
                expected = iw.HF_REVISION if repo == iw.HF_REPO else iw.AUXILIARY_REPOSITORIES[repo]
                self.assertEqual(kwargs.get("revision"), expected)
            self.assertTrue((root / "codec.pth").is_file())
            self.assertFalse((root / "hf_cache/semantic_codec_model.safetensors").exists())
            self.assertEqual((root / "hf_cache/campplus_cn_common.bin").read_bytes(), b"aux")
            self.assertTrue((root / "hf_cache/w2v-bert-2.0/model.safetensors").is_file())
            self.assertTrue((root / "hf_cache/bigvgan/config.json").is_file())
            self.assertTrue((root / "hf_cache/bigvgan/bigvgan_generator.pt").is_file())
            self.assertTrue(any("辅助模型" in event for event in events))

    def test_install_download_error_keeps_retryable_partial_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "gpt.pth").write_bytes(b"x")

            def boom(*_args, **_kwargs):
                raise OSError("network down")

            with self.assertRaisesRegex(RuntimeError, "已保留当前进度"):
                iw.install(root, lambda *_: None, lambda: False, _snapshot_download=boom, _receipt_dir=root.parent / "receipts")
            st = iw.status(root)
            self.assertTrue(st.partial)
            self.assertFalse(st.installed)

    def test_install_cancel_does_not_mark_ready(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(iw._Cancelled):
                iw.install(root, lambda *_: None, lambda: True, _snapshot_download=lambda *_a, **_k: None, _receipt_dir=root.parent / "receipts")
            self.assertFalse(iw.is_installed(root))

    def test_interrupted_candidate_is_reused_by_next_install_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "weights"
            receipts = Path(d) / "receipts"
            calls = 0

            def snapshot(repo, local_dir, **kwargs):
                nonlocal calls
                candidate = Path(local_dir)
                if repo != iw.HF_REPO:
                    if repo == "facebook/w2v-bert-2.0":
                        (candidate / "model.safetensors").parent.mkdir(parents=True, exist_ok=True)
                        (candidate / "model.safetensors").write_bytes(b"aux")
                    else:
                        for rel in kwargs.get("allow_patterns") or []:
                            path = candidate / rel
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(b"aux")
                    return
                calls += 1
                partial = candidate / "partial.bin"
                if calls == 1:
                    partial.write_bytes(b"resume-me")
                    raise OSError("network down")
                self.assertEqual(partial.read_bytes(), b"resume-me")
                for rel in {item for item in iw._REQUIRED_FILES if not item.startswith("hf_cache/")} | {
                    "qwen0.6bemo4-merge/model.safetensors"
                }:
                    path = candidate / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"x")
                (candidate / "qwen0.6bemo4-merge").mkdir(parents=True, exist_ok=True)

            with self.assertRaisesRegex(RuntimeError, "已保留当前进度"):
                iw.install(
                    root,
                    lambda *_args: None,
                    lambda: False,
                    _snapshot_download=snapshot,
                    _receipt_dir=receipts,
                )
            iw.install(
                root,
                lambda *_args: None,
                lambda: False,
                _snapshot_download=snapshot,
                _receipt_dir=receipts,
            )
            self.assertTrue(iw.is_installed(root, receipt_base=receipts))
            self.assertEqual(calls, 2)

    def test_legacy_main_repo_resume_cache_is_migrated_without_losing_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "weights"
            legacy = root.parent / f".indextts2-download-{iw.HF_REVISION[:16]}" / "candidate"
            legacy.mkdir(parents=True)
            (legacy / "gpt.pth").write_bytes(b"resume-main")

            cache, candidate = iw._resume_candidate(root)

            self.assertEqual((candidate / "gpt.pth").read_bytes(), b"resume-main")
            self.assertFalse(legacy.parent.exists())
            self.assertEqual(cache, iw._resume_cache_dir(root))


if __name__ == "__main__":
    unittest.main()

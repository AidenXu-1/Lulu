"""回归:配音核心 bug 修复。

- stitch_wavs:段间插静音,但最后一段后不再追加(成品末尾不多一截静音)。
- voices.list_voices:voices.json 含多余/未知键时容错,不整库崩。
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from src.core.synthesis import voices
from src.core.synthesis.stitcher import stitch_wavs


class StitcherTrailingGapTests(unittest.TestCase):
    def test_gap_only_between_segments_not_after_last(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        sr = 48000
        gap = 0.16
        # 两段恒定振幅(不会被静音裁剪),长度可预期
        lens = [sr // 2, sr // 3]
        parts = []
        for i, n in enumerate(lens):
            p = root / f"seg{i}.wav"
            sf.write(str(p), np.full(n, 0.5, dtype="float32"), sr)
            parts.append(p)
        dst = root / "merged.wav"
        out, dur = stitch_wavs(parts, dst, gap_seconds=gap)
        data, out_sr = sf.read(str(out), dtype="float32")
        self.assertEqual(out_sr, sr)
        # 期望 = 段1 + 一段 gap + 段2(只插一段 gap,末尾无 gap)
        expected = lens[0] + int(sr * gap) + lens[1]
        self.assertEqual(len(data), expected)
        # 成品最后一个采样应仍是有声内容(末尾不是静音)
        self.assertGreater(abs(float(data[-1])), 0.1)


class VoicesTolerantLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._old_dir, self._old_index = voices.VOICES_DIR, voices.INDEX
        voices.VOICES_DIR = Path(self.tmp.name) / "voices"
        voices.INDEX = voices.VOICES_DIR / "voices.json"
        voices.VOICES_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        voices.VOICES_DIR, voices.INDEX = self._old_dir, self._old_index

    def test_unknown_keys_do_not_crash_library(self):
        records = [
            {"id": "a1", "name": "好音色", "reference_audio_path": "/x/a.wav",
             "legacy_field": "should be ignored", "another": 123},   # 多余键
            {"id": "b2", "name": "另一个", "reference_audio_path": "/x/b.wav"},
            {"name": "缺 id"},                                        # 缺必填 → 丢弃,不崩
        ]
        voices.INDEX.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        loaded = voices.list_voices()
        ids = {v.id for v in loaded}
        self.assertEqual(ids, {"a1", "b2"})
        self.assertIsNotNone(voices.get_voice("a1"))

    def test_relocate_to_empty_directory_copies_without_deleting_old_library(self):
        src_voice = voices.VOICES_DIR / "a1"
        src_voice.mkdir(parents=True, exist_ok=True)
        (src_voice / "reference.wav").write_bytes(b"voice")
        voices.INDEX.write_text('[{"id":"a1","name":"好音色","reference_audio_path":"/x/a.wav"}]', encoding="utf-8")
        old_dir = voices.VOICES_DIR
        target = Path(self.tmp.name) / "new-library"

        new_dir = voices.relocate_voice_library(target)

        self.assertEqual(new_dir, target)
        self.assertTrue((target / "voices.json").exists())
        self.assertTrue((target / "a1" / "reference.wav").exists())
        self.assertTrue((old_dir / "voices.json").exists())
        self.assertTrue((old_dir / "a1" / "reference.wav").exists())

    def test_relocate_rejects_non_lulu_non_empty_directory(self):
        target = Path(self.tmp.name) / "ordinary-folder"
        target.mkdir()
        (target / "keep.txt").write_text("user file", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "空文件夹"):
            voices.relocate_voice_library(target)

        self.assertNotEqual(voices.current_voice_library_dir(), target)


if __name__ == "__main__":
    unittest.main()

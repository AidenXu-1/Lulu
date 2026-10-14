import unittest
from datetime import datetime
from pathlib import Path

from src.core.synthesis.indextts2_results import (
    build_result_summary,
    clean_filename_part,
    default_export_filename,
    export_target_path,
    unique_output_path,
)


class IndexTTS2ResultTests(unittest.TestCase):
    def test_25_result_shows_actual_language_speed_and_mode_not_inactive_template(self):
        summary = build_result_summary(output_path='/tmp/test.wav', voice_label='声音',
            style_meta={'name': '未启用的模板'},
            result_meta={'model_version': '2.5', 'preview': True, 'emotion_control_mode': 'follow',
                         'speech': {'language': 'JA', 'speed': 1.2}},
            duration_seconds=3.0, glossary_applied=False)
        self.assertEqual(summary['model'], 'IndexTTS 2.5')
        self.assertIn('日语', summary['summary'])
        self.assertIn('1.20', summary['summary'])
        self.assertIn('跟随音色', summary['summary'])
        self.assertNotIn('未启用的模板', summary['summary'])
        self.assertIn('试听', summary['title'])
        self.assertIn('IndexTTS2.5', summary['default_filename'])

    def test_filename_parts_are_safe_and_short(self):
        self.assertEqual(clean_filename_part('开心/课程:口播*?"<>|'), "开心_课程_口播")
        self.assertEqual(clean_filename_part("   ...   ", fallback="音色"), "音色")
        self.assertLessEqual(len(clean_filename_part("很长" * 20, max_chars=10)), 10)

    def test_default_export_filename_is_readable(self):
        name = default_export_filename(
            template_name="开心/自定义模板",
            voice_name="兆基 课程口播",
            generated_at=datetime(2026, 6, 27, 0, 5),
        )
        self.assertEqual(name, "Lulu_IndexTTS2_开心_自定义模板_兆基_课程口播_20260627-0005.wav")

    def test_result_summary_contains_delivery_metadata(self):
        summary = build_result_summary(
            output_path=Path("/tmp/out.wav"),
            voice_label="课程音色",
            style_meta={
                "name": "我的开心模板",
                "source": "custom",
                "emotion_preset": "happy",
                "emotion_weight": 0.95,
            },
            result_meta={"emotion_mode": "custom_vector", "emotion_weight": 0.95},
            duration_seconds=6.2,
            glossary_applied=True,
            generated_at=datetime(2026, 6, 27, 0, 8),
        )
        self.assertEqual(summary["model"], "IndexTTS2")
        self.assertEqual(summary["template_kind"], "自定义模板")
        self.assertTrue(summary["advanced"])
        self.assertEqual(summary["glossary_label"], "已应用术语读音")
        self.assertIn("IndexTTS2", summary["summary"])
        self.assertIn("我的开心模板", summary["default_filename"])

    def test_result_summary_includes_text_length_label(self):
        summary = build_result_summary(
            output_path=Path("/tmp/out.wav"),
            voice_label="课程音色",
            style_meta={"name": "开心", "source": "system"},
            result_meta={"text_length": {"level": "risky", "label": "偏长可尝试", "char_count": 140}},
            duration_seconds=2.0,
            glossary_applied=False,
            generated_at=datetime(2026, 6, 27, 1, 20),
        )
        self.assertEqual(summary["text_length_label"], "偏长可尝试")
        self.assertIn("文本:偏长可尝试", summary["summary"])

    def test_result_summary_includes_segment_completion_label(self):
        summary = build_result_summary(
            output_path=Path("/tmp/out.wav"),
            voice_label="课程音色",
            style_meta={"name": "开心", "source": "system"},
            result_meta={
                "text_length": {"level": "segmented", "label": "分段生成", "char_count": 520},
                "segment_generation": True,
                "segment_count": 3,
            },
            duration_seconds=5.0,
            glossary_applied=False,
            generated_at=datetime(2026, 6, 27, 2, 12),
        )
        self.assertTrue(summary["segment_generation"])
        self.assertEqual(summary["segment_count"], 3)
        self.assertEqual(summary["segment_label"], "分段生成 · 3 段完成")
        self.assertIn("分段生成 · 3 段完成", summary["summary"])

    def test_export_target_path_forces_wav_suffix(self):
        self.assertEqual(export_target_path("~/Desktop/demo").suffix, ".wav")
        self.assertEqual(export_target_path("/tmp/demo.mp3"), Path("/tmp/demo.wav"))
        self.assertEqual(export_target_path("/tmp/demo.wav"), Path("/tmp/demo.wav"))

    def test_unique_output_path_avoids_overwrite(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "same.wav"
            target.write_bytes(b"old")
            self.assertEqual(unique_output_path(target), Path(tmp) / "same_2.wav")


if __name__ == "__main__":
    unittest.main()

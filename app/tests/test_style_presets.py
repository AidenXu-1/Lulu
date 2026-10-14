import unittest
from pathlib import Path

from src.core.synthesis import style_presets


class StylePresetTests(unittest.TestCase):
    def test_system_styles_always_exist(self):
        styles = style_presets.list_styles(style_presets.load_store(Path("/tmp/nonexistent-lulu-style.json")))
        names = {style["name"] for style in styles}
        self.assertEqual({"自然", "开心", "坚定", "温柔", "激动", "平静"}, names)
        for style in styles:
            self.assertIn("emotion_vector", style)
            self.assertIn("emotion_weight", style)

    def test_custom_style_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            style_presets.upsert_custom_style("我的课程讲解", "gentle", True, path=path)
            loaded = style_presets.load_store(path)
        self.assertEqual(loaded["custom_styles"][0]["name"], "我的课程讲解")
        self.assertEqual(loaded["custom_styles"][0]["emotion_preset"], "gentle")
        self.assertEqual(loaded["custom_styles"][0]["emotion_intensity"], "standard")

    def test_custom_style_roundtrip_with_intensity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            style_presets.upsert_custom_style("我的活力口播", "happy", True, "vivid", path=path)
            loaded = style_presets.load_store(path)
        self.assertEqual(loaded["custom_styles"][0]["emotion_intensity"], "vivid")

    def test_custom_style_roundtrip_with_advanced_vector(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            vector = [0.2, 0.1, 0.0, 0.0, 0.1, 0.0, 0.2, 0.1]
            style_presets.upsert_custom_style(
                "我的高级模板",
                "happy",
                True,
                "vivid",
                emotion_vector=vector,
                emotion_weight=0.95,
                path=path,
            )
            loaded = style_presets.load_store(path)
            meta = style_presets.style_meta(loaded["custom_styles"][0])
        self.assertEqual(loaded["schema_version"], style_presets.SCHEMA_VERSION)
        self.assertEqual(loaded["custom_styles"][0]["emotion_vector"], vector)
        self.assertEqual(loaded["custom_styles"][0]["emotion_weight"], 0.95)
        self.assertEqual(meta["emotion_vector"], vector)
        self.assertEqual(meta["emotion_weight"], 0.95)

    def test_legacy_style_defaults_to_standard_intensity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            path.write_text(
                '{"schema_version":1,"custom_styles":[{"id":"old","name":"旧风格","emotion_preset":"happy","glossary_apply_enabled":true}]}',
                encoding="utf-8",
            )
            loaded = style_presets.load_store(path)
        self.assertEqual(loaded["schema_version"], style_presets.SCHEMA_VERSION)
        self.assertEqual(loaded["custom_styles"][0]["emotion_intensity"], "standard")

    def test_natural_style_forces_standard_intensity(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            style_presets.upsert_custom_style("自然风格", "natural", True, "vivid", path=path)
            loaded = style_presets.load_store(path)
        self.assertEqual(loaded["custom_styles"][0]["emotion_intensity"], "standard")

    def test_duplicate_and_invalid_validation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            style_presets.upsert_custom_style("我的课程讲解", "gentle", True, path=path)
            with self.assertRaises(ValueError):
                style_presets.upsert_custom_style("我的课程讲解", "happy", True, path=path)
            with self.assertRaises(ValueError):
                style_presets.upsert_custom_style("开心", "happy", True, path=path)
            with self.assertRaises(ValueError):
                style_presets.upsert_custom_style("", "happy", True, path=path)
            with self.assertRaises(ValueError):
                style_presets.upsert_custom_style("未知情感", "missing", True, path=path)

    def test_explicit_overwrite_by_style_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            store = style_presets.upsert_custom_style("我的模板", "gentle", True, "standard", emotion_weight=0.8, path=path)
            style_id = store["last_selected_style_id"]
            style_presets.upsert_custom_style(
                "我的模板",
                "happy",
                False,
                "vivid",
                style_id=style_id,
                emotion_vector=[0.25, 0.0, 0.0, 0.0, 0.1, 0.0, 0.2, 0.1],
                emotion_weight=0.95,
                path=path,
            )
            loaded = style_presets.load_store(path)
        self.assertEqual(len(loaded["custom_styles"]), 1)
        self.assertEqual(loaded["custom_styles"][0]["id"], style_id)
        self.assertEqual(loaded["custom_styles"][0]["emotion_preset"], "happy")
        self.assertFalse(loaded["custom_styles"][0]["glossary_apply_enabled"])
        self.assertEqual(loaded["custom_styles"][0]["emotion_weight"], 0.95)

    def test_find_style_by_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            style_presets.upsert_custom_style("我的模板", "gentle", True, path=path)
            store = style_presets.load_store(path)
        self.assertTrue(style_presets.find_style_by_name("开心", store)["system"])
        self.assertFalse(style_presets.find_style_by_name("我的模板", store)["system"])
        self.assertIsNone(style_presets.find_style_by_name("不存在", store))

    def test_delete_current_falls_back_to_default(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            store = style_presets.upsert_custom_style("我的课程讲解", "gentle", True, path=path)
            style_id = store["last_selected_style_id"]
            loaded = style_presets.delete_custom_style(style_id, path=path)
        self.assertEqual(loaded["last_selected_style_id"], style_presets.DEFAULT_STYLE_ID)
        self.assertEqual(loaded["custom_styles"], [])

    def test_bad_json_degrades_to_system_styles(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            path.write_text("{bad", encoding="utf-8")
            store = style_presets.load_store(path)
            styles = style_presets.list_styles(store)
        self.assertTrue(store["load_error"])
        self.assertGreaterEqual(len(styles), 4)

    def test_invalid_custom_style_is_skipped(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "styles.json"
            path.write_text(
                '{"schema_version":1,"custom_styles":[{"name":"坏风格","emotion_preset":"missing"}]}',
                encoding="utf-8",
            )
            store = style_presets.load_store(path)
        self.assertEqual(store["custom_styles"], [])


if __name__ == "__main__":
    unittest.main()

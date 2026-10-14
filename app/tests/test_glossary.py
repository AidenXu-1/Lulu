import unittest
from pathlib import Path

from src.core.synthesis import glossary


def store(*terms, enabled=True):
    return {"schema_version": 1, "apply_enabled": enabled, "terms": list(terms)}


def term(source, replacement, enabled=True):
    return {"id": source, "source": source, "replacement": replacement, "enabled": enabled}


class GlossaryTests(unittest.TestCase):
    def test_index2_uses_native_pinyin_and_preserves_other_version_terms(self):
        terms = store(term("Lulu", "露露"),
                      {**term("落空", "luo4 kong1"), "kind": "pronunciation"},
                      {**term("hello", "HH AH0 L OW1"), "kind": "pronunciation"},
                      {**term("東京", "トウキョウ"), "kind": "pronunciation"})
        original = "Lulu 不落空，hello 東京。"
        result = glossary.apply_glossary(original, terms, pronunciation=True, model_version="2.0")
        self.assertEqual(result.text, "露露 不LUO4 KONG1，hello 東京。")
        self.assertEqual(result.replacement_count, 2)
        self.assertIn("pronunciation_unavailable", result.warnings)
        self.assertEqual(terms["terms"][1]["replacement"], "luo4 kong1")
        modern = glossary.apply_glossary(original, terms, pronunciation=True)
        self.assertIn("<落空|luo4 kong1>", modern.text)
        self.assertIn("<hello|HH AH0 L OW1>", modern.text)

    def test_failed_save_keeps_previous_file(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            glossary.save_store(store(term("Lulu", "露露")), path)
            previous = path.read_bytes()
            with patch("os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    glossary.save_store(store(term("hello", "你好")), path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_store_cannot_be_silently_overwritten(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            for invalid in ('[]', '{"terms":[{"source":"x","replacement":"y","order":"bad"}]}'):
                path.write_text(invalid)
                loaded = glossary.load_store(path)
                self.assertTrue(loaded.get("load_error"))
                with self.assertRaisesRegex(ValueError, "原文件"):
                    glossary.save_store(loaded, path)
                self.assertEqual(path.read_text(), invalid)

    def test_25_reading_annotations_preserve_legacy_replacements(self):
        terms = store(term("Lulu", "露露"),
                      {**term("行", "XING2"), "kind": "pronunciation"},
                      {**term("hello", "HH AH0 L OW1"), "kind": "pronunciation"})
        result = glossary.apply_glossary("Lulu 说 hello，行走。", terms, pronunciation=True)
        self.assertEqual(result.text, "露露 说 <hello|HH AH0 L OW1>，<行|XING2>走。")
        self.assertEqual(glossary.apply_glossary(result.text, terms, pronunciation=True).text, result.text)
        legacy = glossary.apply_glossary("Lulu 说 hello，行走。", terms)
        self.assertEqual(legacy.text, "露露 说 hello，行走。")
        self.assertIn("pronunciation_unavailable", legacy.warnings)

    def test_pronunciation_kind_survives_save_and_reload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "terms.json"
            glossary.save_store(store({**term("東京", "トウキョウ"), "kind": "pronunciation"}), path)
            self.assertEqual(glossary.load_store(path)["terms"][0]["kind"], "pronunciation")

    def test_chinese_exact_replacement(self):
        result = glossary.apply_glossary("Lulu 是本地配音工具", store(term("Lulu", "露露")))
        self.assertEqual(result.text, "露露 是本地配音工具")
        self.assertEqual(result.replacement_count, 1)

    def test_ascii_boundary_protects_longer_identifier(self):
        result = glossary.apply_glossary("API 和 GraphAPIClient", store(term("API", "A P I")))
        self.assertEqual(result.text, "A P I 和 GraphAPIClient")
        self.assertEqual(result.replacement_count, 1)

    def test_longer_term_first(self):
        result = glossary.apply_glossary(
            "IndexTTS2 做 TTS",
            store(term("TTS", "T T S"), term("IndexTTS2", "Index T T S 二")),
        )
        self.assertEqual(result.text, "Index T T S 二 做 T T S")
        self.assertEqual(result.replacement_count, 2)

    def test_disabled_term_and_global_switch(self):
        self.assertEqual(glossary.apply_glossary("Lulu", store(term("Lulu", "露露", enabled=False))).text, "Lulu")
        self.assertEqual(glossary.apply_glossary("Lulu", store(term("Lulu", "露露"), enabled=False)).text, "Lulu")

    def test_skip_url_email_and_path(self):
        text = "访问 https://Lulu.example.com 发到 Lulu@example.com 路径 /tmp/Lulu/file 以及 Lulu"
        result = glossary.apply_glossary(text, store(term("Lulu", "露露")))
        self.assertTrue(result.text.endswith("以及 露露"))
        self.assertIn("https://Lulu.example.com", result.text)
        self.assertIn("Lulu@example.com", result.text)
        self.assertIn("/tmp/Lulu/file", result.text)
        self.assertEqual(result.replacement_count, 1)

    def test_json_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.json"
            glossary.save_store(store(term("Lulu", "露露")), path)
            loaded = glossary.load_store(path)
        self.assertEqual(loaded["terms"][0]["source"], "Lulu")
        self.assertEqual(loaded["terms"][0]["replacement"], "露露")


if __name__ == "__main__":
    unittest.main()

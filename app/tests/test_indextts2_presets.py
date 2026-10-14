import unittest

from src.core.synthesis.indextts2_presets import (
    DEFAULT_EMOTION_INTENSITY,
    emotion_vector_total,
    emotion_weight_for,
    normalize_emotion_intensity,
    normalize_emotion_vector,
    supports_emotion_intensity,
)


class IndexTTS2PresetTests(unittest.TestCase):
    def test_emotion_intensity_weights(self):
        self.assertEqual(emotion_weight_for("restrained", "happy"), 0.45)
        self.assertEqual(emotion_weight_for("standard", "happy"), 0.8)
        self.assertEqual(emotion_weight_for("vivid", "happy"), 1.1)

    def test_invalid_intensity_falls_back_to_standard(self):
        self.assertEqual(normalize_emotion_intensity("missing", "happy"), DEFAULT_EMOTION_INTENSITY)
        self.assertEqual(emotion_weight_for("missing", "happy"), 0.8)

    def test_natural_does_not_support_intensity(self):
        self.assertFalse(supports_emotion_intensity("natural"))
        self.assertEqual(normalize_emotion_intensity("vivid", "natural"), DEFAULT_EMOTION_INTENSITY)
        self.assertEqual(emotion_weight_for("vivid", "natural"), 0.8)

    def test_non_natural_presets_support_intensity(self):
        self.assertTrue(supports_emotion_intensity("happy"))
        self.assertTrue(supports_emotion_intensity("firm"))

    def test_normalize_emotion_vector_accepts_valid_vector(self):
        vector = normalize_emotion_vector([0.55, 0, 0, 0, 0, 0, 0.15, 0.1])
        self.assertEqual(vector, (0.55, 0.0, 0.0, 0.0, 0.0, 0.0, 0.15, 0.1))
        self.assertEqual(emotion_vector_total(vector), 0.8)

    def test_normalize_emotion_vector_rejects_invalid_shape_or_total(self):
        with self.assertRaises(ValueError):
            normalize_emotion_vector([0.1] * 7)
        with self.assertRaises(ValueError):
            normalize_emotion_vector([0.2] * 8)
        with self.assertRaises(ValueError):
            normalize_emotion_vector([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.1, 0.0])


if __name__ == "__main__":
    unittest.main()

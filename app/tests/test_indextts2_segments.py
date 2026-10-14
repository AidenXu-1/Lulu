import unittest

from src.core.synthesis.indextts2_segments import MAX_SEGMENTS, MAX_TOTAL_CHARS, assess_segment_plan, split_text_for_indextts2


class IndexTTS2SegmentTests(unittest.TestCase):
    def test_annotations_and_english_spaces_survive_long_text_planning(self):
        text = 'Please say <hello|h.ə.l.oʊ> clearly, then continue. ' * 15
        segments = split_text_for_indextts2(text, max_chars=80)
        self.assertEqual(sum(segment.count('<hello|h.ə.l.oʊ>') for segment in segments), 15)
        self.assertTrue(all(segment.count('<') == segment.count('>') for segment in segments))
        self.assertTrue(all('clearly,then' not in segment for segment in segments))

    def test_empty_text_is_blocked(self):
        plan = assess_segment_plan(" \n ")
        self.assertEqual(plan.level, "empty")
        self.assertTrue(plan.is_blocked)

    def test_short_text_uses_single_segment(self):
        plan = assess_segment_plan("这是一句短文本。")
        self.assertEqual(plan.level, "single")
        self.assertFalse(plan.is_segmented)
        self.assertEqual(plan.segments, ("这是一句短文本。",))

    def test_punctuation_text_splits_under_limit(self):
        text = "第一段很短。" * 80
        plan = assess_segment_plan(text)
        self.assertEqual(plan.level, "segmented")
        self.assertTrue(plan.is_segmented)
        self.assertGreater(len(plan.segments), 1)
        self.assertLessEqual(len(plan.segments), MAX_SEGMENTS)

    def test_english_period_is_preferred_split_point(self):
        text = ("This is an English sentence with a period. " * 16).strip()
        segments = split_text_for_indextts2(text, max_chars=120)
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(seg.endswith(".") for seg in segments[:-1]))
        self.assertNotIn("period.This", " ".join(segments))

    def test_english_period_join_keeps_sentence_space_inside_segment(self):
        text = "This is an English sentence with a period. This next sentence should keep the space."
        segments = split_text_for_indextts2(text, max_chars=120)
        self.assertEqual(len(segments), 1)
        self.assertIn("period. This", segments[0])

    def test_hard_split_handles_long_unpunctuated_text(self):
        segments = split_text_for_indextts2("长" * 610)
        self.assertEqual(len(segments), 3)
        self.assertTrue(all(len(seg) <= 280 for seg in segments))

    def test_too_long_text_is_blocked(self):
        plan = assess_segment_plan("长" * (MAX_TOTAL_CHARS + 1))
        self.assertEqual(plan.level, "too_long")
        self.assertTrue(plan.is_blocked)


if __name__ == "__main__":
    unittest.main()

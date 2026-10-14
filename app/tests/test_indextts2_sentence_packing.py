"""Sentence grouping only: no model, network, credentials or user files."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.core.synthesis import indextts2_worker as worker


class SentencePackingTests(unittest.TestCase):
    def pack(self, tokens, limit=120):
        result = worker._sentence_token_segments(tokens, limit)
        self.assertEqual([t for part in result for t in part], tokens)
        self.assertTrue(all(0 < len(part) <= limit for part in result))
        return result

    def test_news_keeps_final_sentence_whole(self):
        tokens = ['字'] * 26 + ['▁.'] + ['字'] * 49 + ['▁.'] + ['字'] * 53 + ['▁.']
        self.assertEqual(list(map(len, self.pack(tokens))), [77, 54])

    def test_short_text_and_empty_unchanged(self):
        self.assertEqual(self.pack([]), [])
        self.assertEqual(self.pack(['好', '▁.']), [['好', '▁.']])

    def test_invalid_budget_is_rejected(self):
        for limit in (0, -1):
            with self.assertRaises(ValueError):
                self.pack(['好'], limit)

    def test_overlong_sentence_uses_clause_boundary(self):
        tokens = ['字'] * 69 + ['▁,'] + ['字'] * 69 + ['▁.']
        self.assertEqual(list(map(len, self.pack(tokens))), [70, 70])

    def test_unpunctuated_text_is_bounded(self):
        self.assertEqual(list(map(len, self.pack(['字'] * 251))), [120, 120, 11])

    def test_decimal_and_version_dot_are_not_sentence_boundaries(self):
        tokens = ['字'] * 65 + ['V', '4', '.', '▁', '一'] + ['字'] * 9 + ['▁,'] + ['字'] * 49 + ['▁.']
        self.assertEqual(list(map(len, self.pack(tokens))), [80, 50])

    def test_closing_quote_and_repeated_punctuation_stay_with_sentence(self):
        tokens = ['字'] * 74 + ['▁?', '!', '▁”'] + ['字'] * 53 + ['▁.']
        parts = self.pack(tokens)
        self.assertEqual(list(map(len, parts)), [77, 54])
        self.assertEqual(parts[0][-3:], ['▁?', '!', '▁”'])

    def test_whole_english_sentences(self):
        tokens = ['▁Hello'] * 69 + ['.'] + ['▁World'] * 69 + ['!']
        self.assertEqual(list(map(len, self.pack(tokens))), [70, 70])

    def test_internal_english_dot_is_not_a_boundary(self):
        tokens = ['▁word'] * 65 + ['▁example', '.', 'com'] + ['▁word'] * 11 + [','] + ['▁word'] * 49 + ['.']
        self.assertEqual(list(map(len, self.pack(tokens))), [80, 50])

    def test_many_lengths_preserve_every_token(self):
        for length in range(0, 700, 7):
            for limit in (1, 24, 120):
                with self.subTest(length=length, limit=limit):
                    self.pack([('▁.' if i % 83 == 0 else '▁,' if i % 17 == 0 else str(i)) for i in range(length)], limit)

    def test_worker_scopes_patch_and_restores_even_on_error(self):
        native = Mock(return_value=[['old']])
        tokenizer = SimpleNamespace(split_segments=native)
        tokens = ['字'] * 76 + ['▁.'] + ['字'] * 53 + ['▁.']
        def infer(**options):
            self.assertEqual(list(map(len, tokenizer.split_segments(tokens))), [77, 54])
            self.assertEqual(options, {'text': '原文'})
            raise RuntimeError('generation failed')
        tts = SimpleNamespace(model_version='2.0', device='cpu', tokenizer=tokenizer, infer=infer)
        with self.assertRaisesRegex(RuntimeError, 'generation failed'):
            worker._complete_infer(tts, text='原文')
        self.assertIs(tokenizer.split_segments, native)
        native.assert_not_called()

    def test_25_mps_and_streaming_keep_native_splitter(self):
        for version, device, streaming in [('2.5', 'cpu', 0), ('2.0', 'mps', 0), ('2.0', 'cpu', 20)]:
            native = Mock(return_value=[['native']])
            tokenizer = SimpleNamespace(split_segments=native)
            tts = SimpleNamespace(model_version=version, device=device, tokenizer=tokenizer,
                infer=lambda **kw: tokenizer.split_segments(['test'], 120, quick_streaming_tokens=streaming))
            self.assertEqual(worker._complete_infer(tts), [['native']])
            self.assertIs(tokenizer.split_segments, native)
            native.assert_called_once()

    def test_sequential_requests_restore_splitter_without_accumulating_wrappers(self):
        native = Mock()
        tokenizer = SimpleNamespace(split_segments=native)
        tts = SimpleNamespace(model_version='2.0', device='cpu', tokenizer=tokenizer,
            infer=lambda **kw: tokenizer.split_segments(kw['tokens']))
        for tokens in (['好', '▁.'], [], ['字'] * 76 + ['▁.'] + ['字'] * 53 + ['▁.']):
            self.assertEqual(worker._complete_infer(tts, tokens=tokens), self.pack(tokens))
            self.assertIs(tokenizer.split_segments, native)
        native.assert_not_called()

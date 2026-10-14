"""Version-specific phonemes, source preservation and matching boundaries."""
import unittest
from src.core.synthesis.voxcpm2_pronunciation import phonemes, validate_terms, apply_pronunciations
from src.core.synthesis.voxcpm2_options import generation_inputs

class PronunciationTests(unittest.TestCase):
    def test_official_notation(self):
        self.assertEqual(phonemes('chong2 qing4'), '{chong2}{qing4}')
        self.assertEqual(phonemes('HH AH0 L OW1'), '{HH AH0 L OW1}')
        for bad in ('chong qing', 'chong9', 'トウキョウ', '{ni3}', 'IGNORE TEXT'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):phonemes(bad)
    def test_matches_once_longest_and_skips_urls_existing_phonemes(self):
        terms=[{'source':'重庆','reading':'chong2 qing4'}, {'source':'重','reading':'zhong4'}, {'source':'hello','reading':'HH AH0 L OW1'}]
        text='重庆重要 hello shelloworld https://a.test/hello {hello}'
        self.assertEqual(apply_pronunciations(text,terms),'{chong2}{qing4}{zhong4}要 {HH AH0 L OW1} shelloworld https://a.test/hello {hello}')
    def test_normalization_only_disabled_for_phonemes(self):
        opts={'mode':'design','instruction':'沉稳男声','normalize':True,'pronunciations':[{'source':'重庆','reading':'chong2 qing4'}]}
        result=generation_inputs('欢迎来到重庆。',None,opts)
        self.assertEqual(result['text'],'(沉稳男声)欢迎来到{chong2}{qing4}。');self.assertFalse(result['normalize'])
        self.assertTrue(generation_inputs('今天是2026年。',None,opts)['normalize'])
        self.assertTrue(opts['normalize'])
    def test_invalid_or_duplicate_entries(self):
        for terms in ([{'source':'','reading':'ni3'}],[{'source':'你','reading':'ni3'}]*2,{},[{'source':'你','reading':False}]):
            with self.assertRaises(ValueError):validate_terms(terms)

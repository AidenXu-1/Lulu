"""Official brace phonemes; keep display text and other engines untouched."""
import re
from .glossary import apply_glossary, index2_pinyin

_CONSONANTS = set('B CH D DH F G HH JH K L M N NG P R S SH T TH V W Y Z ZH'.split())
_VOWELS = set('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())


def phonemes(reading):
    if not isinstance(reading, str):
        raise ValueError('请输入拼音或英文音素。')
    reading = reading.strip()
    chinese = index2_pinyin(reading)
    if chinese:
        return ''.join('{' + s.lower() + '}' for s in chinese.split())
    tokens = reading.upper().split()
    if tokens and all(t in _CONSONANTS or (re.fullmatch(r'[A-Z]+[012]', t) and t[:-1] in _VOWELS) for t in tokens):
        return '{' + ' '.join(tokens) + '}'
    raise ValueError('中文请写带声调的拼音，如 chong2 qing4；英文请写音素，如 HH AH0 L OW1。')


def validate_terms(terms):
    if not isinstance(terms, list) or len(terms) > 100:
        raise ValueError('术语读音最多保存 100 条。')
    clean = []; seen = set()
    for term in terms:
        if not isinstance(term, dict):
            raise ValueError('术语读音格式无效。')
        source, reading = term.get('source'), term.get('reading')
        if not isinstance(source, str) or not source.strip() or len(source) > 80 or any(c in source for c in '{}\n\r'):
            raise ValueError('原词请填写 1～80 字，不包含大括号或换行。')
        source = source.strip()
        if source in seen:
            raise ValueError(f'“{source}”已有读音，请合并为一条。')
        phonemes(reading)
        clean.append({'source': source, 'reading': reading.strip()}); seen.add(source)
    return clean


def apply_pronunciations(text, terms):
    mappings = [{'source': t['source'], 'replacement': phonemes(t['reading'])} for t in validate_terms(terms)]
    # Protect existing brace notation before the shared longest-match replacement.
    pieces = re.split(r'(\{[^{}]*\})', text)
    return ''.join(part if part.startswith('{') and part.endswith('}') else
                   apply_glossary(part, {'apply_enabled': True, 'terms': mappings}).text for part in pieces)

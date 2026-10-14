"""Explicit processing options and strict proofreading validation."""
from difflib import SequenceMatcher
import re

_PROOFREAD_PROTECTED_TOKEN = re.compile(
    r"https?://[^\s<>()\[\]{}\"'，。！？；、]+|"
    r"[+-]?\d+(?:[.:/-]\d+)*(?:%|％)?",
    re.IGNORECASE,
)


def validate_options(value):
    if type(value) is not dict or set(value)-{'language','terms','strict','read_only'}:
        raise ValueError('处理偏好字段无效')
    if not isinstance(value.get('language',''),str) or len(value.get('language',''))>80:
        raise ValueError('翻译语言无效')
    if not isinstance(value.get('terms',''),str) or len(value.get('terms',''))>12000:
        raise ValueError('术语参考过长')
    if type(value.get('strict',False)) is not bool:
        raise ValueError('校对选项无效')
    if type(value.get('read_only',False)) is not bool:
        raise ValueError('讨论选项无效')
    return value


def validate_replacement(original, edit, options):
    if not options.get('strict'):
        return
    result=edit['markdown']
    if edit['action']!='replace' or _PROOFREAD_PROTECTED_TOKEN.findall(original)!=_PROOFREAD_PROTECTED_TOKEN.findall(result):
        raise ValueError('严格校对发现数字或链接变化，建议未进入可应用状态；原始回复已保留')
    if len(original)>50000:
        raise ValueError('严格校对请分段引用，避免整篇较长文稿的相似度检查拖慢工作')
    if not .65<=len(result)/max(1,len(original))<=1.35 or SequenceMatcher(None,original,result,autojunk=False).ratio()<.65:
        raise ValueError('严格校对发现改动幅度过大，原文保持不变；可以关闭此选项后重新生成')

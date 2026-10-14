"""Bind visible fragment labels to immutable ranges before calling a model."""
from __future__ import annotations
import re
from . import documents
from .store import identity


def formatted_source_range(canonical, original, start, end):
    """Map editor-only Markdown spelling changes without rewriting the source.

    Whole-document rendering and block order must agree. Matching inside the
    corresponding block keeps repeated transcript lines attached to their own
    occurrence, rather than searching for the first matching sentence.
    """
    from difflib import SequenceMatcher
    from markdown_it import MarkdownIt
    parser = MarkdownIt('commonmark').enable(['table', 'strikethrough'])
    # This parser compares source only; it never loads or renders media in a UI.
    # Lulu's imported covers are local file references and must remain images
    # on both sides of the comparison, including escaped brackets in captions.
    validate_link = parser.validateLink
    parser.validateLink = lambda url: url.startswith('file:///') or validate_link(url)
    left_env, right_env = {}, {}
    left = parser.parse(canonical, left_env)
    right = parser.parse(original, right_env)
    def comparable(tokens):
        # GFM turns a bare URL into an autolink when serializing. Removing only
        # that automatic wrapper preserves the exact visible URL; named links
        # and changed destinations still differ and are rejected.
        from copy import deepcopy
        result = deepcopy(tokens)
        for token in result:
            children = token.children
            if not children:
                continue
            index = 0
            while index + 2 < len(children):
                a, text, b = children[index:index+3]
                if (a.type == 'link_open' and a.markup == 'autolink' and
                        text.type == 'text' and b.type == 'link_close' and
                        a.attrGet('href') == text.content):
                    children[index:index+3] = [text]
                index += 1
        return parser.renderer.render(result, parser.options, {})
    shape = lambda tokens: [(t.type, t.tag, t.nesting, t.hidden) for t in tokens]
    if (shape(left) != shape(right) or
            comparable(left) != comparable(right)):
        return None

    def offsets(text):
        result = [0]
        for line in text.splitlines(keepends=True):
            result.append(result[-1] + len(line.encode('utf-8')))
        return result

    a_lines, b_lines = offsets(canonical), offsets(original)
    a_bytes, b_bytes = canonical.encode('utf-8'), original.encode('utf-8')
    endpoints = {}
    for a_token, b_token in zip(left, right):
        if a_token.type not in {'inline', 'fence', 'code_block', 'html_block'} or not a_token.map or not b_token.map:
            continue
        a0, a1 = (a_lines[n] for n in a_token.map)
        wanted = [(name, position, is_end) for name, position, is_end in
                  [('start', start, False), ('end', end, True)]
                  if name not in endpoints and a0 <= position <= a1]
        if not wanted:
            continue
        b0, b1 = (b_lines[n] for n in b_token.map)
        a, b = a_bytes[a0:a1], b_bytes[b0:b1]
        # Bound the quadratic matcher to a small changed portion of one block.
        prefix = 0
        while prefix < min(len(a), len(b)) and a[prefix] == b[prefix]:
            prefix += 1
        suffix = 0
        while (suffix < min(len(a), len(b)) - prefix and
               a[len(a)-suffix-1] == b[len(b)-suffix-1]):
            suffix += 1
        ae, be = len(a)-suffix, len(b)-suffix
        if max(ae-prefix, be-prefix) > 16384:
            return None
        opcodes = SequenceMatcher(None, a[prefix:ae], b[prefix:be], autojunk=False).get_opcodes()

        def boundary(position, is_end):
            matches = []
            if position <= prefix:
                matches.append(position)
            if position >= ae:
                matches.append(be + position-ae)
            for tag, x0, x1, y0, y1 in opcodes:
                x0 += prefix; x1 += prefix; y0 += prefix; y1 += prefix
                if tag == 'equal' and x0 <= position <= x1:
                    matches.append(y0 + position-x0)
                elif tag != 'equal':
                    if position == x0: matches.append(y0)
                    if position == x1: matches.append(y1)
            if not matches:
                return None
            return (max if is_end else min)(matches)

        for name, position, is_end in wanted:
            mapped = boundary(position-a0, is_end)
            if mapped is None:
                return None
            endpoints[name] = b0 + mapped
        if len(endpoints) == 2:
            a, b = endpoints['start'], endpoints['end']
            return (a, b) if a < b else None
    return None


def label(number):
    return '片段' + (['零','一','二','三','四','五','六','七','八','九','十'][number] if number <= 10 else str(number))


def bind_instructions(text, references, document):
    if not text.strip():
        raise ValueError('请先填写具体要求')
    matches = list(re.finditer(r'(?m)^\s*(片段[^\s：:]{1,12})\s*[：:]\s*', text))
    if not references:
        if matches:
            raise ValueError('要求中仍有片段编号，但引用已移除；请重新引用或调整要求')
        return text, []
    labels, aliases, ids, spans = {}, {}, set(), []
    body = document['text'].encode('utf-8')
    for ref in references:
        if type(ref) is not dict:
            raise ValueError('引用格式无效')
        identity(ref.get('id'))
        n = ref.get('label_number')
        if type(n) is not int or not 1 <= n <= 10000 or label(n) in labels or ref['id'] in ids:
            raise ValueError('引用编号重复或无效，请重新添加引用')
        a, b = ref.get('start'), ref.get('end')
        if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(body):
            raise ValueError('引用范围无效')
        original = body[a:b].decode('utf-8')
        body[:a].decode('utf-8'); body[b:].decode('utf-8')
        if original != ref.get('markdown') or documents.digest(original) != ref.get('before_hash'):
            raise ValueError('引用与原文不一致，请重新添加引用')
        if any(a < end and start < b for start, end in spans):
            raise ValueError('引用范围重叠，请合并后再发送')
        labels[label(n)] = dict(ref)
        aliases[label(n)] = aliases['片段' + str(n)] = label(n)
        ids.add(ref['id']); spans.append((a, b))
    common = text[:matches[0].start()].strip() if matches else text.strip()
    instructions = {}
    for index, match in enumerate(matches):
        name = aliases.get(match.group(1), match.group(1))
        if name not in labels or name in instructions:
            raise ValueError(f'{name} 未对应唯一引用，请检查编号')
        end = matches[index+1].start() if index+1 < len(matches) else len(text)
        instructions[name] = text[match.end():end].strip()
    active = []
    for name, ref in labels.items():
        own = instructions.get(name, '')
        if common or own:
            ref['instruction'] = '\n'.join(part for part in (common, own) if part)
            active.append(ref)
    if not active:
        raise ValueError('引用已添加，请在片段冒号后填写具体要求')
    return text, active

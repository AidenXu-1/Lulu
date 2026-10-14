"""Visible character counts for library/editor projections; never rewrite Markdown."""
from html.parser import HTMLParser
from functools import lru_cache
import re
import threading

from markdown_it import MarkdownIt

_local = threading.local()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_characters(markdown: str) -> int:
    # Reopening/polling an unchanged document must not render its entire Markdown
    # again. Exact content keys cannot return a previous revision's count. Bound
    # both entry count and input size; large imports do not occupy this cache.
    return _cached_visible_characters(markdown) if len(markdown) <= 262144 else _count(markdown)


@lru_cache(maxsize=8)
def _cached_visible_characters(markdown: str) -> int:
    return _count(markdown)


def _count(markdown: str) -> int:
    parser = getattr(_local, 'parser', None)
    if parser is None:
        parser = _local.parser = MarkdownIt('commonmark').enable(['table', 'strikethrough'])
    body = re.sub(r'^(?:\ufeff)?---\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)(?:\r?\n|$)', '', markdown)
    # Parse locally without fetching links or images. Markup and link targets
    # contribute no characters; code and punctuation remain visible content.
    text = _Text()
    text.feed(parser.render(body))
    return sum(not char.isspace() for part in text.parts for char in part)

"""Text-boundary helpers shared by document consumers.

Lulu's saved transcript document may contain archival metadata around the actual
spoken text.  Consumers such as TTS must not treat that full document as speech.
"""

from __future__ import annotations

import re


_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _unique_markdown_section(markdown: str, title: str) -> str:
    matches = [
        match
        for match in _HEADING_RE.finditer(markdown)
        if match.group(2).strip() == title
    ]
    if len(matches) != 1:
        return ""
    selected = matches[0]
    level = len(selected.group(1))
    section_end = len(markdown)
    for heading in _HEADING_RE.finditer(markdown, selected.end()):
        if len(heading.group(1)) <= level:
            section_end = heading.start()
            break
    return markdown[selected.end():section_end].strip()


def voice_text_from_source(text: str, source_type: str) -> str:
    """Return only text the user intends Lulu to speak.

    Transcript records use Lulu's structured Markdown contract, so a document
    with headings must contain exactly one ``逐字稿`` section.  Plain transcript
    text and every non-transcript source remain user-authored text and are only
    trimmed at their outer boundary.
    """

    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw or str(source_type or "").strip().casefold() != "transcript":
        return raw
    if not _HEADING_RE.search(raw):
        return raw
    return _unique_markdown_section(raw, "逐字稿")

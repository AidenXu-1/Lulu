"""把上传的文案文件读成纯文本。仅用 Python 标准库,不引入新依赖。

支持:txt / md / markdown / text / srt / vtt(直读)、docx(zip+xml 提取)、rtf(正则剥离)。
.doc 旧二进制格式不支持(需重库)。
"""

from __future__ import annotations

import re
from pathlib import Path

SUPPORTED_EXTS = ["txt", "md", "markdown", "text", "docx", "rtf", "srt", "vtt"]
FILE_FILTER = "文案文件 (*.txt *.md *.markdown *.text *.docx *.rtf *.srt *.vtt)"
FILE_LABEL = "支持 txt、md、markdown、docx、rtf、srt、vtt"


def read_text_file(path: str | Path) -> str:
    """读出纯文本;失败抛 RuntimeError(人话)。"""
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    if ext == "docx":
        return _read_docx(p)
    if ext == "rtf":
        return _read_rtf(p)
    text = _read_plain(p)
    if ext in ("srt", "vtt"):
        text = _strip_subtitles(text)
    return text.strip()


def _read_plain(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return p.read_text(encoding="gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"读取文件失败:{exc}") from exc


def _strip_subtitles(text: str) -> str:
    """字幕去掉序号 / 时间轴 / WEBVTT 头,只留文本行。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s or s.upper().startswith("WEBVTT"):
            continue
        out.append(s)
    return "\n".join(out)


def _read_docx(p: Path) -> str:
    import xml.etree.ElementTree as ET
    import zipfile

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"这不是有效的 .docx 文件:{exc}") from exc
    root = ET.fromstring(xml)
    paras = []
    for para in root.iter(w + "p"):
        chunk = "".join(t.text for t in para.iter(w + "t") if t.text)
        paras.append(chunk)
    return "\n".join(x for x in paras).strip()


def _read_rtf(p: Path) -> str:
    raw = p.read_text(encoding="latin-1", errors="ignore")

    def _u(m):
        try:
            return chr(int(m.group(1)) % 65536)
        except ValueError:
            return ""

    raw = re.sub(r"\\u(-?\d+)\s?\??", _u, raw)            # \uN unicode(Word 中文 RTF 走这个)
    raw = re.sub(r"\\par[d]?\b|\\line\b", "\n", raw)      # 段落 / 换行
    raw = re.sub(r"\\'[0-9a-fA-F]{2}", "", raw)           # 码页字节(中文已由 \uN 覆盖,这里多为回退)
    raw = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", raw)         # 其它控制字
    raw = raw.replace("{", "").replace("}", "")
    lines = [ln.strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()

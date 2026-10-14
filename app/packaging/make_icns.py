"""Build a macOS .icns file from a PNG iconset without third-party deps.

`iconutil` is still the preferred path in build_app.sh. This fallback keeps the
packaging icon chain usable on machines where iconutil rejects an otherwise
valid iconset.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHUNKS = (
    ("icp4", "icon_16x16.png", 16),
    ("icp5", "icon_32x32.png", 32),
    ("icp6", "icon_32x32@2x.png", 64),
    ("ic07", "icon_128x128.png", 128),
    ("ic08", "icon_256x256.png", 256),
    ("ic09", "icon_512x512.png", 512),
    ("ic10", "icon_512x512@2x.png", 1024),
)


def _png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG")
    if data[12:16] != b"IHDR":
        raise ValueError("missing PNG IHDR")
    return struct.unpack(">II", data[16:24])


def build(iconset: Path, output: Path) -> None:
    chunks: list[tuple[bytes, bytes]] = []
    for code, name, expected in _CHUNKS:
        path = iconset / name
        try:
            data = path.read_bytes()
            size = _png_size(data)
        except OSError as exc:
            raise SystemExit(f"缺少图标文件:{path}") from exc
        except ValueError as exc:
            raise SystemExit(f"图标文件不可用:{path} ({exc})") from exc
        if size != (expected, expected):
            raise SystemExit(f"图标尺寸不匹配:{path} 是 {size[0]}x{size[1]},应为 {expected}x{expected}")
        chunks.append((code.encode("ascii"), data))

    total = 8 + sum(8 + len(data) for _code, data in chunks)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as fh:
        fh.write(b"icns")
        fh.write(struct.pack(">I", total))
        for code, data in chunks:
            fh.write(code)
            fh.write(struct.pack(">I", 8 + len(data)))
            fh.write(data)
    print("icns ->", output)


def main(argv: list[str]) -> int:
    iconset = Path(argv[1] if len(argv) > 1 else "Lulu.iconset")
    output = Path(argv[2] if len(argv) > 2 else "AppIcon.icns")
    build(iconset, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

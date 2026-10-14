"""Compress verified update bytes without changing paths, modes or App signing."""
from copy import copy
from pathlib import Path
import zipfile
import zlib

from src.core.updater.archive import preflight_zip, _records_from_entries
from src.core.updater.constants import MAX_COMPRESSION_RATIO


def compress_update_zip(source: Path, destination: Path):
    expected = _records_from_entries(preflight_zip(source))
    if destination.exists():
        raise RuntimeError('DELIVERY_OUTPUT_MUST_BE_NEW')
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, 'x') as target:
        for entry in original.infolist():
            info = copy(entry)
            # Very repetitive files must still satisfy old clients' expansion
            # bound. Measure a raw DEFLATE stream, keeping at most one chunk in
            # memory, and store that entry when compression exceeds the bound.
            encoder = zlib.compressobj(wbits=-15)
            size = 0
            with original.open(entry) as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    size += len(encoder.compress(chunk))
            size += len(encoder.flush())
            info.compress_type = (zipfile.ZIP_DEFLATED if not info.is_dir()
                and info.file_size/max(1, size) <= MAX_COMPRESSION_RATIO else zipfile.ZIP_STORED)
            with original.open(entry) as incoming, target.open(info, 'w', force_zip64=True) as outgoing:
                for chunk in iter(lambda: incoming.read(1024*1024), b''):
                    outgoing.write(chunk)
    actual = _records_from_entries(preflight_zip(destination))
    if actual != expected:
        raise RuntimeError('PACKAGE_PAYLOAD_MISMATCH')
    return actual

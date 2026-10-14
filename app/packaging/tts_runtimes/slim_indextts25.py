"""Repack the reviewed Index 2.5 baseline without dependency test suites.

Never edits an installed environment. Output must be a new directory. A successful
repack is a content check, not model synthesis acceptance or publication approval.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

BASELINE_SHA256 = '135a7bfa8c0677de6459d1e1f101159cb6e5aae755661c2797557a04054db612'
SITE = 'python/lib/python3.11/site-packages/'
TEST_PACKAGES = frozenset(('numpy', 'scipy', 'pandas', 'sklearn', 'sympy'))


def omit(name: str) -> bool:
    if not name.startswith(SITE):
        return False
    parts = PurePosixPath(name[len(SITE):]).parts
    # scipy.special._precompute imports a tests helper; keep that entire branch.
    return bool(parts and parts[0] in TEST_PACKAGES and 'tests' in parts[1:]
                and parts[:2] != ('scipy', 'special'))


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(path: Path) -> dict:
    result = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            if member.name in result:
                raise ValueError(f'Duplicate archive path: {member.name}')
            stream = archive.extractfile(member) if member.isfile() else None
            result[member.name] = {
                'type': member.type.decode('ascii'), 'size': member.size,
                'mode': member.mode, 'link': member.linkname,
                'sha256': hashlib.file_digest(stream, 'sha256').hexdigest() if stream else None,
            }
    return result


def repack(source: Path, output: Path, *, expected_sha: str | None = None,
           filename: str = 'LuluTTSRuntime-local_indextts2-mac-arm64-20260920.2.tar.gz',
           omit_member=None) -> dict:
    expected_sha = expected_sha or BASELINE_SHA256
    omit_member = omit_member or omit
    if Path(filename).name != filename or not filename.endswith('.tar.gz'):
        raise ValueError('Output filename must be a plain tar.gz basename')
    if sha(source) != expected_sha:
        raise ValueError('Unreviewed source archive; baseline digest does not match')
    output.mkdir(parents=True, exist_ok=False)
    package = output / filename
    versions = {}
    with tarfile.open(source, 'r:gz') as src, package.open('xb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0, compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode='w|', format=tarfile.PAX_FORMAT) as dst:
                for member in src:
                    if (member.name.startswith(SITE)
                            and len(PurePosixPath(member.name[len(SITE):]).parts) == 2
                            and member.name.endswith('.dist-info/METADATA')):
                        metadata = BytesParser().parsebytes(src.extractfile(member).read())
                        name, version = metadata['Name'], metadata['Version']
                        if name in versions:
                            raise ValueError(f'Duplicate distribution: {name}')
                        versions[name] = version
                    if not omit_member(member.name):
                        dst.addfile(member, src.extractfile(member) if member.isfile() else None)
    before, after = inventory(source), inventory(package)
    removed = {name: record for name, record in before.items() if omit_member(name)}
    expected = {name: record for name, record in before.items() if not omit_member(name)}
    if after != expected:
        raise ValueError('Retained contents differ from the original')
    for name, record in after.items():
        if record['type'] == '1' and record['link'] not in after:
            raise ValueError(f'Broken hard link: {name}')
    report = {
        'status': 'candidate_pending_synthesis', 'source_sha256': expected_sha,
        'file': package.name, 'sha256': sha(package), 'size': package.stat().st_size,
        'source_size': source.stat().st_size, 'retained_members': len(after),
        'removed_members': len(removed), 'removed_bytes': sum(x['size'] for x in removed.values()),
        'unpacked_bytes': sum(x['size'] for x in after.values() if x['type'] == '0'),
        'retained_content_verified': True, 'distribution_count': len(versions),
    }
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'removed.json').write_text(json.dumps(removed, indent=2) + '\n')
    (output / 'retained.json').write_text(json.dumps(after, indent=2) + '\n')
    (output / 'dependencies.txt').write_text(
        '# Observed Python package versions from reviewed mac-arm64 Python 3.11 baseline.\n'
        '# Version snapshot only; not a wheel hash lock or full build reproduction guarantee.\n'
        + ''.join(f'{name}=={versions[name]}\n' for name in sorted(versions, key=str.lower)))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(repack(args.source, args.output), indent=2))

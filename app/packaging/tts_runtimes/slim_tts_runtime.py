"""Reviewed slim candidates for the three published mac-arm64 runtimes."""
import argparse
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

from slim_indextts25 import SITE, omit, repack, sha

PROFILES = {
    'vox': ('local_voxcpm2', '20260920.1', '5ef14551adf9fe65518f1c288df433c398cc1fdf2d8e4fe667010ecf160f2bf3'),
    'qwen': ('local_qwen3_tts', '20260920.1', 'cc920159732e7b25ba80310ac32bf4c143c691156ef7d73c47b924801681717e'),
    'index2': ('local_indextts2', '20260920.3', 'c95f1e0ceb726c8b844a6eabbbdad45dd6576067fab525d5d8bb017bbc3c12ca'),
}


def prune(profile: str, name: str) -> bool:
    web_ffmpeg = SITE + 'gradio/templates/frontend/static/ffmpeg'
    return omit(name) or (profile in ('vox', 'qwen') and
                          (name == web_ffmpeg or name.startswith(web_ffmpeg + '/')))


def source_backed_caches(members) -> set[str]:
    """Only discard bytecode with a regular source file in the same archive."""
    files = {m.name for m in members if m.isfile()}
    result = set()
    for name in files:
        path = PurePosixPath(name)
        match = re.fullmatch(r'(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc', path.name)
        if path.parent.name == '__pycache__' and match:
            source = str(path.parent.parent / (match[1] + '.py'))
            if source in files:
                result.add(name)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=PROFILES)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    model, version, digest = PROFILES[args.profile]
    if sha(args.source) != digest:
        raise ValueError('Unreviewed source archive')
    with tarfile.open(args.source) as archive:
        caches = source_backed_caches(archive.getmembers())
    report = repack(args.source, args.output, expected_sha=digest,
                    filename=f'LuluTTSRuntime-{model}-mac-arm64-{version}.tar.gz',
                    omit_member=lambda name: prune(args.profile, name) or name in caches)
    manifest = {'model_id': model, 'platform': 'mac-arm64', 'python': '3.11',
                'version': version, 'file': report['file'], 'sha256': report['sha256'],
                'size': report['size'], 'unpacked_size': report['unpacked_bytes'],
                'validation_status': 'candidate_pending_synthesis'}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

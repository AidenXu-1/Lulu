"""Build/stage the pinned native ASR tools offline. Never installs an App or weights."""
from pathlib import Path
import argparse
import sys
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import shlex

VERSIONS = {'sensevoice-q8': 'runtime-llamacpp-v0.2.1', 'moss': '0.4.0'}
REVISIONS = {'sensevoice': '6991744856587fa44379e8b5dcc432debffeb1be',
             'llama': '8086439a4cea94c71a5dfb8fe4ad1546aebd640f',
             'moss': '10eefba7e33d6a2fcbed0b6af82c752fe0369b42'}
EXECUTABLES = frozenset({'asr-runner', 'llama-funasr-sensevoice', 'transcribe-cli',
    'libggml-base.0.dylib', 'libggml-cpu.0.dylib', 'libggml-metal.0.dylib',
    'libggml.0.dylib', 'libtranscribe.0.4.dylib'})


def run(*args):
    return subprocess.check_output([str(x) for x in args], text=True, stderr=subprocess.STDOUT)


def verify_sources(root):
    for name, directory in (('sensevoice', 'sensevoice-source'), ('llama', 'llama-source')):
        source = root/directory
        if run('git', '-C', source, 'rev-parse', 'HEAD').strip() != REVISIONS[name]:
            raise RuntimeError(f'Unexpected source revision: {name}')
        if run('git', '-C', source, 'diff', '--name-only', 'HEAD').strip():
            raise RuntimeError(f'Modified upstream source: {name}')
    digest = hashlib.sha256()
    for f in sorted((root/'moss-source').rglob('*')):
        if f.is_symlink() and (f.relative_to(root/'moss-source').as_posix() != 'ggml/.pi/SYSTEM.md'
                               or f.readlink().as_posix() != 'gg/SYSTEM.md'):
            raise RuntimeError('Unexpected source symlink')
        if f.is_file():
            digest.update(f.relative_to(root/'moss-source').as_posix().encode()+b'\0'+hashlib.sha256(f.read_bytes()).digest())
    if digest.hexdigest() != '5b8c2d246ac396f6681056504407e4e84f48d974da188b2f44a5c1a9771400f7':
        raise RuntimeError('MOSS source differs from the retained, tested revision')


def build(root, cmake):
    verify_sources(root)
    if not run(cmake, '--version').startswith('cmake version 4.1.2\n'):
        raise RuntimeError('Use the pinned CMake 4.1.2 build tool')
    common = ['-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_OSX_DEPLOYMENT_TARGET=15.0',
              '-DCMAKE_OSX_ARCHITECTURES=arm64', '-DGGML_NATIVE=OFF',
              *['-DCMAKE_'+language+'_FLAGS='+shlex.quote(f'-ffile-prefix-map={root}=/lulu-asr-build')
                for language in ('C', 'CXX', 'OBJC', 'OBJCXX')]]
    run(cmake, '-S', root/'sensevoice-source/runtime/llama.cpp', '-B', root/'q8-build15',
        *common, '-DFETCHCONTENT_SOURCE_DIR_LLAMA='+str(root/'llama-source'),
        '-DGGML_METAL=OFF', '-DGGML_OPENMP=OFF', '-DGGML_BLAS=OFF')
    run(cmake, '--build', root/'q8-build15', '--target', 'llama-funasr-sensevoice', '-j', '6')
    run(cmake, '-S', root/'moss-source', '-B', root/'moss-build15', *common,
        '-DTRANSCRIBE_BUILD_TESTS=OFF', '-DTRANSCRIBE_BUILD_SHARED=ON',
        '-DTRANSCRIBE_BUILD_EXAMPLES=ON', '-DTRANSCRIBE_USE_OPENMP=OFF',
        '-DGGML_METAL_EMBED_LIBRARY=ON', '-DGGML_METAL_MACOSX_VERSION_MIN=15.0')
    run(cmake, '--build', root/'moss-build15', '--target', 'transcribe-cli', '-j', '6')


def verify_runtime(root):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from unittest.mock import patch
    from src.core.engines import native_process
    with patch.object(native_process, 'runtime_root', return_value=root):
        native_process.checked_tools('sensevoice-q8')
        native_process.checked_tools('moss')
    data = native_process.runtime_data_root(root)
    manifest = json.loads((data/'manifest.json').read_text())
    if (set(manifest['audit']) != EXECUTABLES or manifest['versions'] != VERSIONS
            or manifest['source_revisions'] != REVISIONS or manifest['minimum_macos'] != '15.0'):
        raise RuntimeError('Unexpected native ASR runtime identity')
    files = set(manifest['files']) | {'manifest.json'}
    expected = {root: files} if data == root else {root: EXECUTABLES, data: files - EXECUTABLES}
    for directory, names in expected.items():
        if {p.name for p in directory.iterdir()} != names:
            raise RuntimeError('Unexpected native ASR runtime files')
    for name in manifest['audit']:
        if (root/name).stat().st_mode & 0o777 != 0o755:
            raise RuntimeError('Native ASR runtime execution permissions are invalid')
        if b'/Users/' in (root/name).read_bytes():
            raise RuntimeError('Native ASR runtime contains a builder home path')
        run('/usr/bin/codesign', '--verify', '--strict', root/name)
    return manifest


def copy_into_bundle(runtime, bundle):
    verify_runtime(runtime)
    target = bundle/'Contents/Frameworks/asr-runtime'
    data = bundle/'Contents/Resources/asr-runtime'
    if not (bundle/'Contents/Info.plist').is_file() or target.exists() or data.exists():
        raise RuntimeError('Expected a newly assembled App without ASR resources')
    target.mkdir(parents=True)
    data.mkdir(parents=True)
    # Non-code files in Frameworks receive detached signatures in xattrs, which
    # disappear in the secure updater and ZIP. Resources seals them in the App.
    for source in runtime.iterdir():
        shutil.copy2(source, (target if source.name in EXECUTABLES else data)/source.name)
    verify_runtime(target)


def stage(build_root, output):
    verify_sources(build_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise RuntimeError(f'Output already exists; choose a new directory: {output}')
    with tempfile.TemporaryDirectory(prefix='asr-stage-', dir=output.parent) as tmp:
        target = Path(tmp)
        q8, moss = build_root/'q8-build15', build_root/'moss-build15'
        shutil.copy2(q8/'bin/llama-funasr-sensevoice', target/'llama-funasr-sensevoice')
        shutil.copy2(moss/'bin/transcribe-cli', target/'transcribe-cli')
        # Copy the real bytes under the install-name aliases, never symlinks.
        libraries = {}
        for f in moss.rglob('*.dylib'):
            if f.is_symlink(): continue
            name = run('/usr/bin/otool', '-D', f).splitlines()[1].strip().split('/')[-1]
            if name in libraries: raise RuntimeError(f'Duplicate library: {name}')
            libraries[name] = f
            shutil.copy2(f, target/name)
        run('/usr/bin/clang', '-O2', '-arch', 'arm64', '-mmacosx-version-min=15.0',
            Path(__file__).with_name('asr_runner.c'), '-o', target/'asr-runner')
        audits = {}
        for f in sorted(target.iterdir()):
            commands = run('/usr/bin/otool', '-l', f)
            rpaths = re.findall(r'cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset', commands)
            for rpath in rpaths: run('/usr/bin/install_name_tool', '-delete_rpath', rpath, f)
            if f.name in libraries:
                run('/usr/bin/install_name_tool', '-id', '@loader_path/'+f.name, f)
            for dep in run('/usr/bin/otool', '-L', f).splitlines()[1:]:
                dep = dep.strip().split(' (')[0]
                if dep.startswith(('/usr/lib/', '/System/Library/')): continue
                name = dep.split('/')[-1]
                if name not in libraries: raise RuntimeError(f'External dependency: {dep}')
                run('/usr/bin/install_name_tool', '-change', dep, '@loader_path/'+name, f)
            commands = run('/usr/bin/otool', '-l', f)
            minimum = re.findall(r'\bminos (\d+\.\d+(?:\.\d+)?)', commands)
            if not minimum or any(tuple(map(int, s.split('.'))) > (15, 0, 0) for s in minimum):
                raise RuntimeError(f'Unsupported deployment target: {f.name}: {minimum}')
            arch = run('/usr/bin/lipo', '-archs', f).strip()
            if arch != 'arm64': raise RuntimeError(f'Unexpected architecture: {arch}')
            f.chmod(0o755)
            run('/usr/bin/codesign', '--force', '--sign', '-', f)
            run('/usr/bin/codesign', '--verify', '--strict', f)
            audits[f.name] = {'minimum_macos': minimum, 'architectures': arch}
        for name, source in {
            'LICENSE-SenseVoice.txt': 'sensevoice-source/LICENSE',
            'LICENSE-llama.cpp.txt': 'llama-source/LICENSE',
            'LICENSE-jsonhpp.txt': 'llama-source/licenses/LICENSE-jsonhpp',
            'LICENSE-transcribe.cpp.txt': 'moss-source/LICENSE',
            'LICENSE-transcribe-third-party.txt': 'moss-source/THIRD-PARTY-LICENSES.md',
        }.items():
            shutil.copy2(build_root/source, target/name)
        (target/'NOTICE.txt').write_text(
            'Lulu native ASR runtime, built for macOS 15 / arm64.\n'
            'SenseVoice: https://github.com/QwenAudio/SenseVoice\n'
            'llama.cpp: https://github.com/ggml-org/llama.cpp\n'
            'transcribe.cpp: https://github.com/handy-computer/transcribe.cpp (community runtime); see source_revisions in manifest.json.\n'
            'These tools contain no model weights. Model sources and pinned revisions are in the Lulu model catalog.\n')
        manifest = {'schema': 1, 'versions': VERSIONS, 'source_revisions': REVISIONS,
                    'minimum_macos': '15.0', 'audit': audits,
                    'files': {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(target.iterdir())}}
        (target/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        target.rename(output)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--cmake', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.bundle and args.runtime:
        copy_into_bundle(args.runtime.resolve(), args.bundle.resolve())
    elif args.verify and args.runtime:
        verify_runtime(args.runtime.resolve())
    elif args.build_root and args.output:
        root = args.build_root.resolve()
        if args.cmake: build(root, args.cmake.resolve())
        print(json.dumps(stage(root, args.output.resolve()), indent=2))
    else:
        parser.error('Specify build-root/output, runtime/bundle, or runtime/verify')

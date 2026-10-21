"""Verify the assembled workspace and native WebEngine closure, without launching Lulu."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


class WorkspacePackageError(RuntimeError):
    pass


def _file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size == 0:
        raise WorkspacePackageError(f'工作区运行文件缺失或指向包外：{relative}')
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_assets(resources: Path, source_app: Path) -> dict:
    workspace = 'src/ui/agent_workspace/'
    source_manifest = _file(source_app, workspace + 'assets/build-manifest.json')
    manifest = json.loads(source_manifest.read_text())
    assets = manifest['assets']
    required = {'workspace.js', 'workspace.css', 'style.css', 'columns.css', 'composer.css',
                'local.css', 'THIRD_PARTY_NOTICES.txt'}
    if not required.issubset(assets):
        raise WorkspacePackageError('编辑器资产清单不完整')
    files = [workspace + 'index.html', workspace + 'assets/build-manifest.json',
             'src/ui/assets/obsidian.png', 'src/ui/assets/feishu.png',
             'src/core/agent_workspace/catalog.json']
    model_root = source_app / 'src/ui/model_settings/assets'
    for name in ('index.html', 'settings.js', 'settings.css', 'paper_texture.png'):
        _file(source_app, 'src/ui/model_settings/assets/' + name)
    files += ['src/ui/model_settings/public.json', 'src/ui/model_settings/information.json']
    files += [path.relative_to(source_app).as_posix() for path in sorted(model_root.rglob('*')) if path.is_file()]
    for name, expected in assets.items():
        if Path(name).name != name or name in {'.', '..'}:
            raise WorkspacePackageError('编辑器资产清单包含非法路径')
        relative = workspace + 'assets/' + name
        path = _file(resources, relative)
        if path.stat().st_size != expected['bytes'] or _digest(path) != expected['sha256']:
            raise WorkspacePackageError(f'编辑器资产与构建清单不一致：{name}')
        files.append(relative)
    hashes = {}
    for relative in files:
        source = _file(source_app, relative)
        packaged = _file(resources, relative)
        digest = _digest(packaged)
        if digest != _digest(source):
            raise WorkspacePackageError(f'工作区资产与候选源码不一致：{relative}')
        hashes[relative] = digest
    notices = _file(resources, workspace + 'assets/THIRD_PARTY_NOTICES.txt').read_text()
    for package in manifest['bundledPackages']:
        if f"{package['name']}@{package['version']}" not in notices:
            raise WorkspacePackageError('编辑器第三方依赖许可缺失')
    return hashes


def verify_native(app: Path) -> dict:
    frameworks = app / 'Contents/Frameworks'
    binaries = []
    for module in ('QtWebEngineCore', 'QtWebEngineWidgets', 'QtWebChannel'):
        matches = list((frameworks / 'PySide6').glob(module + '.*.so'))
        if len(matches) != 1:
            raise WorkspacePackageError(f'打包后缺少唯一的 {module} 模块')
        binaries.append(_file(app, matches[0].relative_to(app).as_posix()))
    found = {p.resolve() for p in frameworks.rglob('QtWebEngineProcess') if p.is_file()}
    if len(found) != 1:
        raise WorkspacePackageError('打包后缺少唯一的网页辅助进程')
    helper = _file(app, next(iter(found)).relative_to(app).as_posix())
    if not helper.stat().st_mode & 0o111:
        raise WorkspacePackageError('网页辅助进程不可执行')
    binaries.append(helper)
    candidates = {p.resolve() for p in frameworks.rglob('QtWebEngineCore.framework/Versions/A/Resources')}
    if len(candidates) != 1:
        raise WorkspacePackageError('网页引擎资源目录不完整')
    resource_root = next(iter(candidates))
    resources = {}
    for name in ('icudtl.dat', 'qtwebengine_resources.pak', 'qtwebengine_resources_100p.pak',
                 'qtwebengine_resources_200p.pak', 'qtwebengine_locales/en-US.pak',
                 'qtwebengine_locales/zh-CN.pak'):
        path = _file(app, (resource_root / name).relative_to(app).as_posix())
        resources[path.relative_to(app).as_posix()] = path.stat().st_size
    checked = set()
    # Follow the actual native dependency closure. Python API exclusions must not
    # silently remove C++ dependencies such as QtQuick and QtQml.
    while binaries:
        binary = binaries.pop()
        if binary in checked:
            continue
        checked.add(binary)
        result = subprocess.run(['/usr/bin/otool', '-L', str(binary)], capture_output=True,
                                text=True, check=True, timeout=30)
        for dependency in re.findall(r'^\s+(@rpath/[^\s]+)', result.stdout, re.MULTILINE):
            relative = dependency.removeprefix('@rpath/')
            if not relative.startswith('Qt') or '.framework/' not in relative:
                continue
            possibilities = [frameworks / relative, frameworks / 'PySide6/Qt/lib' / relative]
            target = next((p for p in possibilities if p.is_file()), None)
            if target is None:
                raise WorkspacePackageError(f'网页引擎原生依赖缺失：{relative}')
            binaries.append(_file(app, target.relative_to(app).as_posix()))
    return {'helper': helper.relative_to(app).as_posix(), 'resources': resources,
            'native_dependency_closure': sorted(p.relative_to(app).as_posix() for p in checked)}


def verify(app: Path, source_app: Path) -> dict:
    app = app.resolve()
    return {'schema': 1, 'status': 'passed', 'scope': 'workspace-assets-and-native-dependencies',
            'assets': verify_assets(app / 'Contents/Resources', source_app.resolve()),
            'native': verify_native(app)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--source-app', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    receipt = verify(args.app, args.source_app)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print('工作区资产、许可和网页引擎原生依赖校验通过')

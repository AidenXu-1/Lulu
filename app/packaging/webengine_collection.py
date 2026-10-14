"""Resolve the actual Qt framework version instead of sorting unrelated folders."""
from pathlib import Path


def framework_version(framework: Path) -> str:
    candidates = [path for path in (framework / 'Versions').iterdir()
                  if path.name != 'Current' and (path / 'QtWebEngineCore').is_file()]
    if len(candidates) != 1:
        raise RuntimeError('无法唯一确定 QtWebEngineCore 原生版本，停止打包')
    return candidates[0].name


def collect_webengine(info):
    from PyInstaller import compat
    from PyInstaller.utils.hooks import collect_system_data_files
    framework = Path(info.location['LibrariesPath']) / 'QtWebEngineCore.framework'
    if not compat.is_darwin or not framework.is_dir():
        return info.collect_qtwebengine_files()
    version = framework_version(framework)
    bundled = Path(info.package_location) in framework.parents
    destination = (Path(info.qt_rel_dir) / 'lib' if bundled else Path()) / framework.name / 'Versions' / version
    binaries, datas = [], []
    for directory in ('Helpers', 'Resources'):
        source = framework / 'Versions' / version / directory
        if not source.is_dir():
            source = framework / directory
        if not source.is_dir():
            raise RuntimeError(f'网页引擎缺少 {directory}，停止打包')
        for path, target in collect_system_data_files(str(source), str(destination / directory)):
            if path.endswith('/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess'):
                binaries.append((path, target))
            else:
                datas.append((path, target))
    if len(binaries) != 1:
        raise RuntimeError('网页辅助进程缺失或重复，停止打包')
    return binaries, datas

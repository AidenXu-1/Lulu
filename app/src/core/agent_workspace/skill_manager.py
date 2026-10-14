"""Shared internal Skill lifecycle. Packages are data, never executable installers."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import uuid
import zipfile

import yaml

_LIMIT = 16 * 1024 * 1024
_FILE_LIMIT = 1024 * 1024
_LOCK = threading.RLock()
_LOCAL = threading.local()
_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')


def resources():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).parents[3]
    # PyInstaller's macOS Frameworks data links point into Contents/Resources.
    # Resolve only this trusted bundle path; imported and installed paths reject links.
    root = (base / 'internal-skills').resolve()
    if not root.is_relative_to(base.resolve().parent):
        raise ValueError('随包技能资源不在程序目录内')
    return root


def roots():
    from .skills import skill_root
    active = skill_root()
    return active, active.parent / 'skill-manager'


def safe(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('技能路径不能包含符号链接')
    return path


def name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError('技能名称须为小写英文、数字或短横线，最多 64 字符')
    return value


def read_json(path, default=None):
    safe(path)
    if not path.exists():
        return default
    if path.stat().st_size > _LIMIT:
        raise ValueError('技能记录过大')
    return json.loads(path.read_text(encoding='utf-8'))


def atomic(path, value):
    safe(path)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def files(folder):
    safe(folder)
    if not folder.is_dir():
        raise ValueError('技能文件夹不存在')
    result, total = {}, 0
    paths = []
    for path in folder.rglob('*'):
        paths.append(path)
        if len(paths) > 1024:
            raise ValueError('技能包文件与目录数量过多')
    for path in sorted(paths):
        safe(path)
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError('技能包只能包含普通文件')
        relative = path.relative_to(folder).as_posix()
        size = path.stat().st_size
        total += size
        if size > _FILE_LIMIT or total > _LIMIT or len(result) >= 512:
            raise ValueError('技能包超过大小或文件数量限制')
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def package(folder):
    manifest = files(folder)
    raw = (folder / 'SKILL.md').read_text(encoding='utf-8-sig')
    if len(raw.encode()) > 256 * 1024:
        raise ValueError('SKILL.md 超过 256 KB')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)([\s\S]*)', raw, re.S)
    if not match:
        raise ValueError('SKILL.md 需要名称、简介和正文')
    try:
        meta = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError('SKILL.md 头部格式错误') from exc
    if type(meta) is not dict or not match[2].strip():
        raise ValueError('技能说明格式错误或正文为空')
    identity = name(meta.get('name'))
    description = meta.get('description')
    if not isinstance(description, str) or not 1 <= len(description) <= 1600:
        raise ValueError('技能简介须为 1–1600 字')
    if type(meta.get('disable-model-invocation', False)) is not bool:
        raise ValueError('自动调用选项无效')
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return dict(name=identity, description=description, digest=digest, files=manifest,
                automatic=not meta.get('disable-model-invocation', False))


def bundled():
    values = read_json(resources() / 'catalog.json', [])
    if type(values) is not list:
        raise ValueError('预装技能目录损坏')
    return {name(row['name']): row for row in values}


@contextmanager
def locked():
    active, state = roots()
    safe(active).mkdir(parents=True, exist_ok=True)
    safe(state).mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if getattr(_LOCAL, 'scope', None) is not None:
            yield _LOCAL.scope
            return
        lock_path = safe(state / 'lock')
        with lock_path.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                recover(active, state)
                registry = read_json(state / 'registry.json', {'schema': 1, 'items': {}})
                if type(registry) is not dict or registry.get('schema') != 1 or type(registry.get('items')) is not dict:
                    raise ValueError('技能安装记录损坏，请保留文件并恢复备份')
                _LOCAL.scope = (active, state, registry)
                yield _LOCAL.scope
            finally:
                _LOCAL.scope = None
                fcntl.flock(stream, fcntl.LOCK_UN)


def recover(active, state):
    journal = read_json(state / 'transaction.json')
    if journal is None:
        return
    identity = name(journal['name'])
    token = journal['token']
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('技能恢复记录无效')
    destination = safe(active / identity)
    stage = safe(state / ('stage-' + token))
    backup = safe(state / ('retired-' + token))
    action = journal['action']
    if action == 'install':
        if stage.exists():
            if destination.exists():
                if backup.exists():
                    raise ValueError('技能恢复存在冲突，已保留两份内容')
                destination.rename(backup)
            stage.rename(destination)
        if not destination.is_dir():
            raise ValueError('技能安装恢复文件缺失')
        if package(destination)['digest'] != journal['registry']['items'][identity]['installed_digest']:
            raise ValueError('技能安装恢复校验失败，已保留原文件')
    elif action == 'uninstall':
        if destination.exists():
            if backup.exists():
                raise ValueError('技能卸载恢复存在冲突')
            destination.rename(backup)
    else:
        raise ValueError('未知技能恢复操作')
    sync_directory(active)
    sync_directory(state)
    atomic(state / 'registry.json', journal['registry'])
    # Finish only cleanup already authorized in a previous-version transaction.
    for purge_token in journal.get('purge_tokens', []):
        if not isinstance(purge_token, str) or not re.fullmatch(r'[0-9a-f]{32}', purge_token):
            raise ValueError('技能清理记录无效')
        retired = safe(state / ('retired-' + purge_token))
        if retired.exists():
            shutil.rmtree(retired)
    (state / 'transaction.json').unlink()
    sync_directory(state)


def commit(active, state, registry, identity, action, source=None, previous=None):
    token = uuid.uuid4().hex
    if action == 'uninstall' and (active / identity).is_dir():
        registry['items'][identity]['restore_token'] = token
    if previous is not None:
        registry['items'][identity]['rollback'] = dict(token=token, metadata=previous)
    if source is not None:
        stage = state / ('stage-' + token)
        shutil.copytree(source, stage)
        if package(stage)['digest'] != package(source)['digest']:
            raise ValueError('技能复制校验失败，未启用')
    atomic(state / 'transaction.json', dict(name=identity, token=token, action=action, registry=registry))
    recover(active, state)


def migrate_local_skills(active, state, registry):
    """Reclassify former recommendations without changing any package bytes."""
    titles = {'find-skills': '技能发现', 'skill-creator': '技能制作',
              'human-writing': '活人感写作', 'khazix-writer': '卡兹克公众号写作'}
    migrated = json.loads(json.dumps(registry))
    changed = False
    for identity, saved in list(migrated['items'].items()):
        if saved.get('origin') != 'curated':
            continue
        name(identity)
        changed = True
        has_files = safe(active / identity).exists() or bool(saved.get('restore_token') or saved.get('rollback'))
        record = saved
        while record:
            if record.get('origin') == 'curated':
                record['origin'] = 'user'
                record.setdefault('title', titles.get(identity, identity))
            for token in (record.get('restore_token'), (record.get('rollback') or {}).get('token')):
                if token and re.fullmatch(r'[0-9a-f]{32}', token):
                    has_files = safe(state / ('retired-' + token)).exists() or has_files
            record = (record.get('rollback') or {}).get('metadata')
        if not has_files and saved.get('status') == 'uninstalled':
            # No downloaded content exists; remove only the obsolete catalog stub.
            del migrated['items'][identity]
    if changed:
        atomic(state / 'registry.json', migrated)
        registry.clear()
        registry.update(migrated)


def initialize(active, state, registry):
    migrate_local_skills(active, state, registry)
    for identity, meta in bundled().items():
        if identity in registry['items']:
            continue
        if (active / identity).exists():
            # Existing user files always win. Register the collision, never overwrite.
            registry['items'][identity] = dict(status='installed', enabled=True, origin='user')
            atomic(state / 'registry.json', registry)
            continue
        # Only the first feature bootstrap preinstalls. Future catalog additions are opt-in.
        if registry.get('initialized') or meta['origin'] != 'system':
            continue
        source = resources() / 'packages' / identity
        value = package(source)
        if value['digest'] != meta['digest']:
            raise ValueError('随包技能校验失败：' + identity)
        registry['items'][identity] = dict(status='installed', enabled=True, origin=meta['origin'],
            version=meta['version'], installed_digest=value['digest'], source=meta['source'])
        commit(active, state, registry, identity, 'install', source)
    if not registry.get('initialized'):
        registry['initialized'] = True
        atomic(state / 'registry.json', registry)


def restore_source(state, saved):
    token = saved.get('restore_token', '')
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        return None
    folder = safe(state / ('retired-' + token))
    if not folder.is_dir():
        return None
    value = package(folder)
    if value['digest'] != saved.get('restore_digest'):
        raise ValueError('保留的技能安装包已变化，请重新导入')
    return folder


def listing():
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        catalog = bundled()
        identities = set(catalog) | set(registry['items']) | {p.name for p in active.iterdir() if p.is_dir() and not p.name.startswith('.')}
        entries, issues = [], []
        order = {key: index for index, key in enumerate(catalog)}
        for identity in sorted(identities, key=lambda key: (order.get(key, 1000), key)):
            row = None
            try:
                name(identity)
                installed = safe(active / identity).is_dir()
                saved = registry['items'].get(identity, {})
                official = identity in catalog and saved.get('origin', catalog[identity]['origin']) != 'user'
                meta = catalog[identity] if official else {}
                row = dict(meta, name=identity, title=meta.get('title', saved.get('title', identity)),
                    origin=meta.get('origin', 'user'), installed=installed,
                    enabled=installed and saved.get('enabled', True) and saved.get('status') != 'uninstalled',
                    status='installed' if installed else saved.get('status', 'available'),
                    directory=str(active / identity), recoverable=identity in catalog and official,
                    version=saved.get('version', meta.get('version', '')), source=saved.get('source', meta.get('source', '')),
                    config=saved.get('config', {}), generation=saved.get('generation', ''),
                    can_rollback=bool(saved.get('rollback')),
                    update_available=bool(installed and official and meta.get('origin') == 'system' and saved.get('version') != meta.get('version')))
                if installed:
                    value = package(active / identity)
                    if value['name'] != identity:
                        raise ValueError('技能名称与目录不一致')
                    row.update(description=value['description'], digest=value['digest'], automatic=value['automatic'],
                               modified=bool(saved.get('installed_digest') and saved['installed_digest'] != value['digest']))
                elif saved.get('status') == 'uninstalled' and saved.get('restore_digest'):
                    cached = restore_source(state, saved)
                    if cached is not None:
                        value = package(cached)
                        row.update(recoverable=True, description=value['description'])
                elif saved.get('status') == 'installed':
                    row['status'] = 'missing'
                entries.append(row)
            except (ValueError, OSError, UnicodeError) as exc:
                issues.append(f'{identity}：{exc}')
                if row is not None:
                    row.update(enabled=False, error=str(exc), description='文件需要检查：' + str(exc))
                    row.pop('summary', None)
                    entries.append(row)
        return dict(entries=entries, issues=issues, directory=str(active))


def configure(identity, enabled=None, config=None):
    identity = name(identity)
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        package(active / identity)
        saved = registry['items'].setdefault(identity, dict(status='installed', origin='user'))
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError('启用状态无效')
            saved['enabled'] = enabled
        if config is not None:
            allowed = {'language', 'terms'} if identity == 'lulu-translate' else {'strict'} if identity == 'lulu-proofread' else set()
            if type(config) is not dict or set(config) - allowed:
                raise ValueError('此技能不支持这些偏好')
            from .processing import validate_options
            validate_options(config)
            saved['config'] = config
        saved['generation'] = uuid.uuid4().hex
        atomic(state / 'registry.json', registry)
    return dict(name=identity, state='completed')


def uninstall(identity):
    identity = name(identity)
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        saved = registry['items'].setdefault(identity, dict(origin='user'))
        if (active / identity).is_dir():
            saved.pop('restore_digest', None)
            try:
                saved['restore_digest'] = package(active / identity)['digest']
            except (ValueError, OSError, UnicodeError):
                pass
        saved.update(status='uninstalled', enabled=False)
        commit(active, state, registry, identity, 'uninstall')
    return dict(name=identity, state='completed', message='已卸载；恢复副本保留在技能管理目录')


def details(identity, reference='SKILL.md'):
    identity = name(identity)
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        folder = active / identity
        if not folder.is_dir():
            saved = registry['items'].get(identity, {})
            cached = restore_source(state, saved) if saved.get('restore_digest') else None
            if cached is not None:
                folder = cached
            elif identity in bundled() and saved.get('origin') != 'user':
                folder = resources() / 'packages' / identity
            else:
                raise ValueError('找不到此技能，请重新导入')
        info = package(folder)
        rel = PurePosixPath(reference)
        if rel.is_absolute() or '..' in rel.parts or reference not in info['files']:
            raise ValueError('只能查看技能包内的文件')
        path = safe(folder / reference)
        try:
            text = path.read_bytes()[:256 * 1024].decode('utf-8-sig')
        except UnicodeError:
            text = '此文件为二进制附件，无法显示文字预览。'
        return dict(name=identity, files=list(info['files']), text=text, reference=reference,
                    directory=str(folder), config=registry['items'].get(identity, {}).get('config', {}))


def inspect_source(source):
    """Stage an immutable preview; no installation until a caller chooses a token."""
    if not isinstance(source, str) or not source or len(source) > 4096:
        raise ValueError('请选择本地技能文件夹、ZIP 或 SKILL.md')
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', source.strip()):
        raise ValueError('仅支持本地技能，请自行下载后拖入文件夹或 ZIP')
    active, state = roots()
    safe(state).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspect-', dir=state) as temporary:
        folder = Path(temporary)
        source_path = safe(Path(source).expanduser())
        if source_path.is_file() and source_path.name == 'SKILL.md':
            source_path = source_path.parent
        if source_path.is_file() and source_path.suffix.lower() == '.zip':
            unpack(source_path, folder / 'unpacked')
            source_path = folder / 'unpacked'
        if not source_path.is_dir():
            raise ValueError('请选择技能文件夹或 ZIP 技能包')
        files(source_path)
        candidates = [source_path] if (source_path / 'SKILL.md').is_file() else [p.parent for p in sorted(source_path.rglob('SKILL.md'))]
        if not 1 <= len(candidates) <= 64:
            raise ValueError('未找到 SKILL.md，或技能数量超过 64 项')
        previews = []
        for candidate in candidates:
            meta = package(candidate)
            token = uuid.uuid4().hex
            destination = state / ('preview-' + token)
            shutil.copytree(candidate, destination)
            meta.update(token=token, source=source, conflict=(active / meta['name']).exists(),
                        reserved=meta['name'] in bundled(),
                        warning='仅加载说明和 Markdown 参考；附带脚本不会执行。')
            atomic(state / ('preview-' + token + '.json'), meta)
            previews.append(meta)
        return dict(entries=previews)


def unpack(archive, destination):
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > 512 or sum(i.file_size for i in infos) > _LIMIT:
            raise ValueError('ZIP 超过大小或文件数量限制')
        seen = set()
        for info in infos:
            rel = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (rel.is_absolute() or '..' in rel.parts or '\\' in info.filename or
                    stat.S_ISLNK(mode) or info.filename in seen or info.file_size > _FILE_LIMIT):
                raise ValueError('ZIP 包含不安全或重复路径')
            seen.add(info.filename)
            path = destination.joinpath(*rel.parts)
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as stream:
                    content = stream.read(_FILE_LIMIT + 1)
                if len(content) > _FILE_LIMIT:
                    raise ValueError('ZIP 文件过大')
                path.write_bytes(content)


def install(identity=None, token=None, *, validate=None):
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        if validate is not None:
            validate()
        if (identity is None) == (token is None):
            raise ValueError('请选择一个预装技能或待安装包')
        if identity is not None:
            identity = name(identity)
            previous = registry['items'].get(identity, {})
            cached = restore_source(state, previous) if previous.get('restore_digest') else None
            if cached is not None and not (active / identity).exists():
                source, meta = cached, previous
                value = package(source)
            else:
                meta = bundled().get(identity)
                if meta is None or previous.get('origin') == 'user':
                    raise ValueError('找不到此技能的安装包，请重新导入')
                source = resources() / 'packages' / identity
                value = package(source)
                if value['digest'] != meta['digest']:
                    raise ValueError('随包技能校验失败')
        else:
            if not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{32}', token):
                raise ValueError('安装预览编号无效')
            meta = read_json(state / ('preview-' + token + '.json'))
            if not meta:
                completed = next(((key, value) for key, value in registry['items'].items() if value.get('install_token') == token), None)
                if completed:
                    return dict(name=completed[0], state='completed', message='此安装请求已处理；当前状态：' + completed[1]['status'])
                raise ValueError('安装预览不存在，请重新选择文件')
            source = state / ('preview-' + token)
            value = package(source)
            identity = value['name']
            if value['digest'] != meta['digest']:
                raise ValueError('技能包已变化，请重新预览')
            if identity in bundled():
                raise ValueError('此名称属于预装技能，请在 SKILL.md 中使用其他名称')
            meta = dict(meta, origin='user', version='用户导入')
        if (active / identity).exists():
            if package(active / identity)['digest'] == value['digest']:
                return dict(name=identity, state='completed', message='此版本已安装，保持现有启用状态')
            raise ValueError('已有同名技能，未覆盖；请先导出并卸载旧版，或修改新技能名称')
        previous = registry['items'].get(identity, {})
        registry['items'][identity] = dict(status='installed', enabled=True, origin=meta['origin'],
            version=meta.get('version', ''), source=meta.get('source', ''), title=meta.get('title', identity),
            installed_digest=value['digest'], config=previous.get('config', {}), generation=uuid.uuid4().hex, install_token=token)
        commit(active, state, registry, identity, 'install', source)
        if token:
            shutil.rmtree(source)
            (state / ('preview-' + token + '.json')).unlink()
    return dict(name=identity, state='completed', message='已安装并启用')


def update(identity, rollback=False):
    identity = name(identity)
    if type(rollback) is not bool:
        raise ValueError('恢复版本选项无效')
    with locked() as (active, state, registry):
        initialize(active, state, registry)
        previous = registry['items'].get(identity, {})
        current = package(active / identity)
        if current['digest'] != previous.get('installed_digest'):
            raise ValueError('技能包含你的修改，请先导出保存；未覆盖当前内容')
        if rollback:
            recovery = previous.get('rollback')
            if not recovery or not re.fullmatch(r'[0-9a-f]{32}', recovery['token']):
                raise ValueError('没有可恢复的上一版本')
            source = state / ('retired-' + recovery['token'])
            target = dict(recovery['metadata'])
            if package(source)['digest'] != target['installed_digest']:
                raise ValueError('上一版本校验失败，未恢复')
            target.pop('rollback', None)
        else:
            meta = bundled().get(identity)
            if not meta or previous.get('origin') == 'user':
                raise ValueError('用户技能请导入新包，不自动覆盖')
            if previous.get('version') == meta['version']:
                return dict(name=identity, state='completed', message='已是当前随包版本')
            source = resources() / 'packages' / identity
            if package(source)['digest'] != meta['digest']:
                raise ValueError('随包新版本校验失败')
            target = dict(previous, version=meta['version'], source=meta['source'], installed_digest=meta['digest'])
            target.pop('rollback', None)
        target.update(enabled=previous.get('enabled', True), config=previous.get('config', {}))
        target['generation'] = uuid.uuid4().hex
        registry['items'][identity] = target
        commit(active, state, registry, identity, 'install', source, previous=previous if not rollback else None)
    return dict(name=identity, state='completed', message='已恢复上一版本' if rollback else '已更新，可恢复上一版本')


def export(identity, destination):
    identity = name(identity)
    path = safe(Path(destination).expanduser())
    if not path.is_absolute() or path.suffix.lower() != '.zip' or path.exists():
        raise ValueError('请选择一个尚不存在的 ZIP 文件路径')
    with locked() as (active, state, registry):
        folder = active / identity
        manifest = files(folder)
        with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
            for relative in manifest:
                bundle.write(folder / relative, identity + '/' + relative)
    return dict(state='completed', path=str(path))


def draft(identity, description, instructions):
    identity = name(identity)
    if not isinstance(description, str) or not 1 <= len(description) <= 1600 or not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 100000:
        raise ValueError('请提供技能简介和完整方法')
    _, state = roots()
    safe(state).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='draft-', dir=state) as temp:
        folder = Path(temp)
        (folder / 'SKILL.md').write_text('---\n' + yaml.safe_dump(dict(name=identity, description=description), allow_unicode=True) + '---\n\n' + instructions, encoding='utf-8')
        result = inspect_source(str(folder))
    return dict(result, state='needs_input', action='skills', label='查看并安装技能', message='技能草稿已保存，可在设置中预览和安装。')


def drafts():
    _, state = roots()
    if not state.exists():
        return []
    result = []
    for path in sorted(safe(state).glob('preview-*.json')):
        meta = read_json(path)
        if meta and (state / ('preview-' + meta['token'])).is_dir():
            result.append(meta)
    return result

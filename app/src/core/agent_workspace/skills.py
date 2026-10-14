"""Load enabled Skill instructions on demand; packages never grant tool permissions."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import re
from ..models_manager import app_data_dir

MAX_BYTES = 256 * 1024


def skill_root():
    return app_data_dir() / 'skills'


def _read(path):
    from .skill_manager import safe
    root = skill_root()
    if path != root and root not in path.parents:
        raise ValueError('Skill 文件不在安装目录内')
    with safe(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Skill 文件超过 256 KB，未截断加载')
    return raw.decode('utf-8-sig'), hashlib.sha256(raw).hexdigest()


def catalog():
    from . import skill_manager
    value = skill_manager.listing()
    return dict(directory=value['directory'], issues=value['issues'],
                entries=[row for row in value['entries'] if row['installed'] and row['enabled']])


def validate_loaded(request):
    for name in request.get('loaded_skills', []):
        load(name, request.get('skills', []), explicit_name=request.get('explicit_skill'))


@contextmanager
def guard_loaded(request):
    """Serialize a write with enable/uninstall so a completed disable wins."""
    if not request.get('loaded_skills'):
        yield
        return
    from . import skill_manager
    with skill_manager.locked():
        validate_loaded(request)
        yield


def apply_config(request, name):
    entry = next(e for e in request['skills'] if e['name'] == name)
    if name == 'lulu-proofread':
        request.setdefault('options', {}).setdefault('strict', entry.get('config', {}).get('strict', True))


def explicit(text, entries):
    command = re.match(r'^/([a-z0-9][a-z0-9-]{0,63})(?:\s|$)', text.strip())
    if not command:
        return None
    entry = next((e for e in entries if e['name'] == command[1]), None)
    if entry is None:
        raise ValueError('找不到已启用的 Skill，请在设置中核对安装和启用状态')
    return entry['name']


def load(name, entries, *, explicit_name=None, reference=None, loaded=()):
    from . import skill_manager as manager
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None or (not entry['automatic'] and name != explicit_name):
        raise ValueError('此 Skill 不在本轮可调用范围，请用 /名称 显式选择')
    with manager.locked() as (active, state, registry):
        folder = active / manager.name(name)
        saved = registry['items'].get(name, {})
        if not folder.is_dir() or not saved.get('enabled', True) or saved.get('status') == 'uninstalled':
            raise ValueError('技能已停用或卸载，请重新发送')
        current = manager.package(folder)
        if current['digest'] != entry['digest'] or saved.get('generation', '') != entry.get('generation', ''):
            raise ValueError('Skill 在发送后已有变化，请重新发送以使用新版本')
        rel = Path(reference or 'SKILL.md')
        if reference is not None and (name not in loaded or rel.is_absolute() or '..' in rel.parts or rel.suffix.lower() != '.md'):
            raise ValueError('只能读取已加载 Skill 目录内的 Markdown 参考资料')
        text, digest = _read(folder / rel)
        if reference is None:
            text += '\n\nLulu 运行环境：技能只提供方法，不执行脚本或终端命令。技能安装仅使用本地包与系统随包资源，不支持在线搜索、网址下载或在线更新；需要其他技能时请用户自行下载后在设置中导入。'
        if reference is None and entry.get('config'):
            text += '\n\nLulu 用户技能偏好（本轮要求优先）：\n' + json.dumps(entry['config'], ensure_ascii=False)
        return dict(state='completed', message=f'已读取 Skill：{entry.get("title", name)}' + (f' / {reference}' if reference else ''),
                    label='使用技能 · ' + entry.get('title', name), skill_name=name, skill_content=text, digest=digest)

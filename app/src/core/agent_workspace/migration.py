"""Inventory first; merge legacy views and preserve identities without moving files."""
from __future__ import annotations
import hashlib
import json
from .. import library
from .store import encode, now, workspace_root


def migrate(store, config):
    path=workspace_root()/'migration-v1.json'
    with library._transaction():
        if path.is_symlink():
            raise ValueError('资料升级记录路径无效')
        if path.exists():
            report=json.loads(path.read_text(encoding='utf-8'))
            if (type(report) is not dict or report.get('version') != 1
                    or type(report.get('phase')) is not str
                    or report.get('phase') not in {'inventoried', 'complete'}
                    or type(report.get('entries')) is not list or type(report.get('errors')) is not list
                    or (report['phase'] == 'complete' and type(report.get('imported_templates')) is not int)):
                raise ValueError('资料升级记录格式异常，原文件已保留')
            if report.get('phase')=='complete':
                return report
        entries, errors=[], []
        for file in sorted(library.LIBRARY_DIR.glob('*.json')):
            if file==library.FOLDERS_FILE or file.name.startswith('.'):
                continue
            try:
                if file.is_symlink():
                    raise ValueError('原记录是链接，保留未迁移')
                raw=file.read_bytes();record=json.loads(raw)
                kind=record.get('kind','transcript')
                if kind not in {'transcript','rewrite','voice'}:
                    continue
                meta = record.get('meta') or {}
                text=record.get('text','')
                if kind!='voice':
                    md=library._markdown_abs(record.get('markdown_path'),file.stem)
                    if md.is_file():text=md.read_text(encoding='utf-8')
                entries.append(dict(old_id=file.stem,new_id=file.stem,kind=kind,
                    record_hash=hashlib.sha256(raw).hexdigest(),body_hash=hashlib.sha256(text.encode()).hexdigest(),
                    folder_id=record.get('folder_id'),source_id=record.get('source_id') or meta.get('source_id'),
                    created_at=record.get('created_at'),updated_at=record.get('updated_at')))
            except (OSError,ValueError,TypeError,AttributeError) as exc:
                errors.append(dict(file=file.name,reason=type(exc).__name__))
        report=dict(version=1,phase='inventoried',created_at=now(),entries=entries,errors=errors,
                    old_templates=config.copywriting_user_templates,old_prompts=config.copywriting_prompts,
                    file_policy='retain_original_files_and_ids')
        library._write_atomic_text(path,encode(report))
        presets=store.preference('custom-presets') or []
        ids={p['id'] for p in presets}
        for direction,templates in config.copywriting_user_templates.items():
            for template in templates:
                if type(template) is not dict or not all(isinstance(template.get(k),str) for k in ('id','name','text')):
                    continue
                key='custom-legacy-'+hashlib.sha256((direction+'\0'+template['id']).encode()).hexdigest()[:24]
                if key not in ids:
                    presets.append(dict(id=key,title=template['name'],text=template['text'],icon='doc',
                        hint='原有模板 · '+direction,legacy_id=template['id'],legacy_direction=direction))
                    ids.add(key)
        for direction, prompt in config.copywriting_prompts.items():
            if not isinstance(prompt, dict) or not isinstance(prompt.get('custom_text'), str) or not prompt['custom_text'].strip():
                continue
            key='custom-legacy-prompt-'+hashlib.sha256(direction.encode()).hexdigest()[:20]
            if key not in ids:
                presets.append(dict(id=key,title='原有自定义提示词 · '+direction,text=prompt['custom_text'],
                                    icon='doc',hint='原有处理偏好',legacy_id=key,legacy_direction=direction))
                ids.add(key)
        store.preference('custom-presets',presets)
        report['phase']='complete';report['imported_templates']=sum('legacy_id' in p for p in presets)
        library._write_atomic_text(path,encode(report))
        return report

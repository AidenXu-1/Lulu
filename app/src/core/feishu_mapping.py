"""Explicit column matching for user-owned tables. No network or AI calls."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from .feishu_storage import WorkArchiveRecord, _duration_text


@dataclass(frozen=True)
class SourceField:
    key: str
    name: str
    types: tuple[int, ...]
    aliases: tuple[str, ...] = ()


# User-facing save choices are deliberately narrower than WorkArchiveRecord.
# IDs, account totals and transcription language remain internal metadata.
SOURCES = (
    SourceField('title', '标题', (1,), ('视频名称', '作品标题', '视频标题', '名称')),
    SourceField('author_name', '作者昵称', (1,), ('作者', '博主', '博主昵称')),
    SourceField('published_at_ms', '作品发布时间', (5,), ('发布日期', '发布时间')),
    SourceField('duration_seconds', '时长', (1, 2), ('视频时长',)),
    SourceField('like_count', '点赞', (2,), ('点赞数', '点赞量')),
    SourceField('collect_count', '收藏', (2,), ('收藏数', '收藏量')),
    SourceField('comment_count', '评论', (2,), ('评论数', '评论量')),
    SourceField('share_count', '分享', (2,), ('分享数', '分享量', '转发数')),
    SourceField('work_url', '原作品链接', (15, 1), ('作品链接', '视频链接', '来源链接')),
    SourceField('media_url', '音视频源链接', (15, 1), ('音视频直链', '媒体直链')),
    SourceField('transcript_text', '转录文稿', (1,), ('逐字稿文案', '逐字稿', '文稿', '文案')),
    SourceField('cover_local_path', '封面', (17,), ('作品封面',)),
    SourceField('media_local_path', '音视频附件', (17,), ('媒体附件',)),
    SourceField('captured_at_ms', '采集时间', (5,), ('采集日期',)),
    SourceField('description', '作品简介', (1,), ('作品描述',)),
    SourceField('summary', '摘要', (1,), ()),
    SourceField('author_homepage', '作者主页', (15, 1), ('博主主页',)),
    SourceField('cover_url', '封面链接', (15, 1), ()),
    SourceField('view_count', '播放', (2,), ('播放量',)),
    SourceField('coin_count', '投币', (2,), ('投币数',)),
    SourceField('danmaku_count', '弹幕', (2,), ('弹幕数',)),
    SourceField('platform', '来源平台', (1,), ('平台',)),
    SourceField('content_kind', '作品类型', (1,)),
    SourceField('author_verify', '作者认证', (1,)),
    SourceField('tags', '作品话题', (1,), ('作品标签',)),
    SourceField('original_transcript_text', '原始转录', (1,)),
)
SOURCE_BY_KEY = {s.key: s for s in SOURCES}
PROTECTED = frozenset(('主题', '话题/开头拆解', '我的分析', '标签', '备注', '我的备注', '研究备注', '使用状态', '状态'))
TYPE_NAMES = {1:'文字', 2:'数字', 3:'单选', 4:'多选', 5:'日期', 15:'链接', 17:'附件', 18:'关联', 20:'公式', 21:'关联', 1005:'自动编号'}


def source_value(record: WorkArchiveRecord, key: str):
    """One availability/serialization rule for the form and the actual write."""
    value = getattr(record, key, None)
    if key == 'content_kind':
        return {'video':'视频', 'image':'图文', 'mixed':'混合'}.get(value, '')
    if key == 'platform':
        return {'douyin':'抖音', 'xiaohongshu':'小红书', 'bilibili':'哔哩哔哩',
                'youtube':'YouTube'}.get(value, value)
    if key == 'tags':
        return '、'.join(value or ())
    if key == 'author_fans' and value is not None:
        return str(value)  # Public counts may already be abbreviated (e.g. 1.2万).
    return value


def field_type(field: dict) -> int:
    try:
        return int(field.get('type', 0))
    except (TypeError, ValueError):
        return 0


def compatible(source: SourceField, field: dict) -> bool:
    return (field_type(field) in source.types and not field.get('is_computed')
            and not field.get('is_readonly') and str(field.get('field_name', '')).strip() not in PROTECTED)


def schema_fingerprint(fields: list[dict]) -> str:
    values = [(f.get('field_id'), f.get('field_name'), field_type(f), bool(f.get('is_computed')), bool(f.get('is_readonly'))) for f in fields]
    return hashlib.sha256(json.dumps(sorted(values, key=str), ensure_ascii=False).encode()).hexdigest()


def suggest_mapping(fields: list[dict], saved: dict | None = None) -> tuple[dict, list[str]]:
    """Saved IDs survive rename; missing/retyped IDs never fall back to a same-name column."""
    saved = saved or {}
    result, issues = {}, []
    for source in SOURCES:
        if source.key in saved:
            previous = saved[source.key]
            if previous is None or previous == '':
                result[source.key] = None
                continue
            fid = previous.get('field_id') if isinstance(previous, dict) else previous
            candidates = [f for f in fields if f.get('field_id') == fid and compatible(source, f)]
            if len(candidates) != 1 or (isinstance(previous, dict) and previous.get('type') != field_type(candidates[0])):
                result[source.key] = None
                issues.append(source.key)
                continue
        else:
            exact = [f for f in fields if f.get('field_name', '').strip() == source.name]
            candidates = exact or [f for f in fields if f.get('field_name', '').strip() in source.aliases]
            if len(candidates) != 1 or not compatible(source, candidates[0]):
                result[source.key] = None
                if candidates:
                    issues.append(source.key)
                continue
        f = candidates[0]
        result[source.key] = {'field_id': f['field_id'], 'type': field_type(f)}
    assigned: dict[str, list[str]] = {}
    for key, value in result.items():
        if value:
            assigned.setdefault(value['field_id'], []).append(key)
    for keys in assigned.values():
        if len(keys) > 1:
            for key in keys:
                result[key] = None
                issues.append(key)
    return result, list(dict.fromkeys(issues))


def validate_mapping(fields: list[dict], mapping: dict) -> dict[str, dict]:
    by_id = {f.get('field_id'): f for f in fields}
    selected, used = {}, set()
    for key, value in mapping.items():
        if not value:
            continue
        source = SOURCE_BY_KEY.get(key)
        if source is None or not isinstance(value, dict):
            raise ValueError('列对应无效，请重新检查。')
        f = by_id.get(value.get('field_id'))
        if f is None or not compatible(source, f) or field_type(f) != value.get('type'):
            raise ValueError(f'「{source.name}」对应的列发生变化，请重新确认。')
        if f['field_id'] in used:
            raise ValueError('不同信息不能写入同一列，请调整列对应。')
        used.add(f['field_id'])
        selected[key] = f
    if not selected:
        raise ValueError('请至少对应一项可保存的信息。')
    return selected


def mapped_values(record: WorkArchiveRecord, fields: list[dict], mapping: dict) -> dict:
    selected = validate_mapping(fields, mapping)
    result = {}
    for key, field in selected.items():
        value = source_value(record, key)
        if value is None or value == '' or field_type(field) == 17:
            continue
        if key == 'duration_seconds' and field_type(field) == 1:
            value = _duration_text(record)
        if field_type(field) == 15:
            value = {'link': value, 'text': value}
        result[field['field_name']] = value
    if not result:
        raise ValueError(f'「{record.title or "这条作品"}」没有与目标列对应的有效内容，未创建空记录。')
    return result


# Local snapshot of the latest user-reviewed structures; no source Base dependency.
def template_fields(kind: str) -> list[tuple]:
    from .feishu_templates import template_definition
    return template_definition(kind)['fields']

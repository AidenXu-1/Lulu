"""Built-in structure snapshot, verified against the two user-owned Bases on 2026-09-28.

Creation reads only this local recipe; source Bases are never required at runtime.
Preserves visible field order, field types, numeric/date formatting and grid names.
Does not clone source records, per-user column widths or Feishu AI extensions.
"""
from copy import deepcopy

TEMPLATES = {'topic': {'name': '选题素材库',
           'table_name': '选题素材',
           'view_name': '全部素材',
           'fields': [('标题', 1),
                      ('发布日期', 5, {'date_formatter': 'yyyy-MM-dd'}),
                      ('时长', 1),
                      ('点赞', 2, {'formatter': '1,000'}),
                      ('收藏', 2, {'formatter': '1,000'}),
                      ('评论', 2, {'formatter': '1,000'}),
                      ('分享', 2, {'formatter': '1,000'}),
                      ('原作品链接', 15),
                      ('音视频直链', 15),
                      ('逐字稿文案', 1),
                      ('主题', 1),
                      ('话题/开头拆解', 1),
                      ('封面', 17),
                      ('标签', 1),
                      ('作者昵称', 1),
                      ('采集日期', 5, {'date_formatter': 'yyyy-MM-dd'})]},
 'research': {'name': 'IP调研库',
              'table_name': '博主作品',
              'view_name': '作品总览',
              'fields': [('标题', 1),
                         ('作者昵称', 1),
                         ('发布日期', 5, {'date_formatter': 'yyyy-MM-dd'}),
                         ('时长', 1),
                         ('点赞', 2, {'formatter': '1,000'}),
                         ('收藏', 2, {'formatter': '1,000'}),
                         ('评论', 2, {'formatter': '1,000'}),
                         ('分享', 2, {'formatter': '1,000'}),
                         ('原作品链接', 15),
                         ('音视频直链', 15),
                         ('转录文稿', 1),
                         ('封面', 17),
                         ('音视频附件', 17),
                         ('采集时间', 5, {'date_formatter': 'yyyy-MM-dd'})]}}

def template_definition(kind: str) -> dict:
    if kind not in TEMPLATES:
        raise ValueError('请选择选题素材库或 IP调研库模板。')
    return deepcopy(TEMPLATES[kind])

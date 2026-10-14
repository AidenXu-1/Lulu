"""Batch delivery over existing document, Obsidian and Feishu adapters."""
from dataclasses import asdict
from pathlib import Path
from ...core import library, obsidian
from ...core.agent_workspace.store import identity
from ...core.output.writer import new_export_file


def document_ids(value):
    if type(value) is not list or not 1 <= len(value) <= 500:
        raise ValueError('请选择 1 至 500 篇文稿')
    ids = [identity(item) for item in value]
    if len(set(ids)) != len(ids):
        raise ValueError('所选文稿重复，请重新选择')
    return ids


def deliver(action, *, document_ids, folder=None):
    if action not in {'batch_export', 'batch_obsidian', 'batch_feishu'}:
        raise ValueError('批量操作无效')
    results, records = [], []
    for eid in document_ids:
        title = '文稿'
        try:
            doc = library.document_snapshot(eid)
            transcript = library.load(eid)
            if not doc or not transcript:
                raise FileNotFoundError('文稿不存在或已移至最近删除')
            title = doc['title']
            if action == 'batch_feishu':
                from ...core.feishu_storage import record_from_transcript
                record = record_from_transcript(transcript, title=title,
                    text_override=doc['text'], summary=library.get_summary(eid))
                records.append(asdict(record))
            else:
                name = ''.join(c for c in title[:80] if c not in '/\\:*?"<>|\r\n').strip('. ') or '文稿'
                content = doc['text']
                if action == 'batch_obsidian' and not content.lstrip('\ufeff').startswith(('---\n', '---\r\n')):
                    # The current Markdown is authoritative after user edits.
                    # Preserve it verbatim while adding the existing note metadata.
                    content = obsidian.frontmatter_text(transcript, title) + '\n' + content
                with new_export_file(Path(folder), name + '.md') as stream:
                    stream.write(content.encode('utf-8'))
                    saved = Path(stream.name).name
            results.append(dict(id=eid, title=title, ok=True, **({'filename': saved} if action != 'batch_feishu' else {})))
        except (OSError, ValueError, RuntimeError) as exc:
            results.append(dict(id=eid, title=title, ok=False, message=str(exc)))
    return dict(results=results, records=records)

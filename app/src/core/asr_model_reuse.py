"""Verify legacy ASR content before rebinding a changed mount device number.

Called only by an explicit installation worker. It reads official fixed-revision
file metadata, never downloads weights, imports model runtimes or unpickles SDK
caches. Readiness queries remain read-only and never call this module.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import artifact_trust as at, models_manager as mm, model_transfer as mt
from .receipt_device_repair import plan_repair


def _snapshot_sources(model_id: str, root: Path, trust: dict):
    repositories = trust['repositories']
    if model_id == 'local_sensevoice':
        by_repo = {repo: root / name for name, repo in mm._SENSEVOICE_SUBMODELS.items()}
        return [(entry, 'modelscope', by_repo[entry['repo']]) for entry in repositories]
    if model_id == 'local_qwen3':
        return [(repositories[0], 'modelscope', root),
                (repositories[1], trust['aligner_source'], root / '_aligner')]
    return [(repositories[0], trust['source'], root)]


def _is_download_metadata(relative: str, sources) -> bool:
    """Only known non-model SDK bookkeeping may lack an upstream digest."""
    for _entry, source, prefix in sources:
        prefix = Path(prefix)
        try:
            local = Path(relative).relative_to(prefix)
        except ValueError:
            continue
        if source == 'modelscope' and local.as_posix() == '.msc':
            return True
        if source == 'hf':
            name = local.as_posix()
            if name == '.cache/huggingface/.gitignore':
                return True
            if name.startswith('.cache/huggingface/download/') and name.endswith('.metadata'):
                return True
    return False


def recover_legacy_asr_receipt(session, model_id: str) -> bool:
    """Reuse only a device-only legacy receipt after official content validation.

    An eligible but invalid installation stops here; it never silently starts a
    multi-GB replacement after a failed reuse check. Other installation shapes
    continue through the existing explicit download path.
    """
    root = mm.model_path(model_id)
    receipt_base = mm._receipt_base()
    artifact_id = mm._artifact_id(model_id)
    receipt = at._receipt_path(receipt_base, artifact_id)
    try:
        at._assert_private_entry(receipt, directory=False)
        payload = json.loads(receipt.read_text())
    except (OSError, ValueError, RuntimeError):
        return False
    if not isinstance(payload, dict) or payload.get('content_sha256'):
        return False
    selected = None
    for trust in mm._trust_options(model_id):
        try:
            plan = plan_repair(artifact_id, root, trust, receipt_base)
        except (OSError, RuntimeError, ValueError, TypeError):
            continue
        if plan[3]:
            selected = (trust, plan)
            break
    if selected is None:
        return False
    trust, (receipt, original, repaired, _changed) = selected
    root = Path(repaired['root'])
    session.check()
    session.report('正在校验已有模型，仅核对文件清单，不下载模型…', -1)

    def unchanged():
        session.check()
        _path, now, candidate, _ = plan_repair(artifact_id, root, trust, receipt_base)
        if now != original or candidate != repaired:
            raise RuntimeError('模型或校验记录在检查期间变化，原文件已保留。')
        present = {p.relative_to(root).as_posix() for p in at._walk_private_tree(root)}
        if present != {entry['path'] for entry in repaired['files']}:
            raise RuntimeError('模型文件清单发生变化，原文件已保留，请检查后重试。')

    unchanged()
    sources = _snapshot_sources(model_id, root, trust)
    relative_sources = [(entry, source, source_root.relative_to(root))
                        for entry, source, source_root in sources]
    official = {}
    recorded = {entry['path'] for entry in repaired['files']}
    for entry, source, source_root in sources:
        session.check()
        plan = (mt.hf_plan(entry['repo'], entry['revision'], source_root, read_only=True)
                if source == 'hf' else mt.modelscope_plan(entry['repo'], entry['revision'], source_root))
        if (plan.repo != entry['repo'] or plan.revision != entry['revision']
                or plan.root != source_root or not plan.files):
            raise RuntimeError('官方模型清单身份不符，已保留原文件。')
        for file in plan.files:
            rel = file.path.relative_to(root).as_posix()
            # Historical ModelScope installs omit this Git transfer rule file.
            # It is never read by a model runtime. If present, still verify it.
            if file.path == source_root / '.gitattributes' and rel not in recorded:
                continue
            if rel in official:
                raise RuntimeError('官方模型清单包含重复文件，已保留原文件。')
            official[rel] = file

    if not official.keys() <= recorded:
        raise RuntimeError('已有模型缺少官方清单中的文件，已保留原文件，未重新下载。')
    markers = {'.lulu_done'}
    if model_id == 'local_qwen3':
        markers.add('_aligner/.lulu_done')
    for rel in recorded - official.keys():
        if rel in markers:
            if (root / rel).read_bytes() != b'ok':
                raise RuntimeError('模型完成标记校验失败，原文件已保留。')
        elif not _is_download_metadata(rel, relative_sources):
            raise RuntimeError('已有模型包含官方清单以外的文件，原文件已保留。')

    digests = {}
    for rel in sorted(recorded):
        session.check()
        file = official.get(rel)
        if file is not None and not file.valid(session.check):
            raise RuntimeError('已有模型内容校验未通过，原文件已保留，未重新下载。')
        digests[rel] = (file.digest if file is not None and file.algorithm == 'sha256'
                        else at._sha256_file(root / rel, session.check))
    # Source identity is checked again after all reads and immediately before the
    # small receipt commit. No current model bytes are copied, renamed or deleted.
    unchanged()
    updated = dict(repaired, content_sha256=digests)
    data = (json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    at.restore_install_receipt((receipt, data))
    if not at.install_receipt_valid(artifact_id, root, trust, receipt_base):
        at.restore_install_receipt((receipt, original))
        raise RuntimeError('恢复后校验未通过，原校验记录已恢复。')
    return True

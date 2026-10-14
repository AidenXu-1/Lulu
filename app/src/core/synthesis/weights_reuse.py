"""Explicit, content-verified reuse of installed TTS weights after mount changes.

Readiness never calls this module. No weights, runtimes or model code are fetched
or loaded; only Vox's pinned official file metadata may be requested.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from .. import artifact_trust as at, model_transfer as mt, models_manager as mm
from ..receipt_device_repair import plan_repair


def _inventory(root, links):
    files, link_state, pending = set(), {}, [root]
    while pending:
        parent = pending.pop()
        at._assert_private_entry(parent, directory=True)
        for path in parent.iterdir():
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                target = links.get(path)
                if (target is None or (hasattr(os, 'getuid') and info.st_uid != os.getuid())
                        or path.resolve(strict=True) != target or target.is_symlink()):
                    raise RuntimeError('已有模型包含未确认的链接，原文件保留，未重新下载。')
                at._assert_private_entry(target, directory=False)
                link_state[rel] = (info.st_dev, info.st_ino, info.st_mtime_ns, os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                at._assert_private_entry(path, directory=False)
                files.add(rel)
    if set(link_state) != {p.relative_to(root).as_posix() for p in links}:
        raise RuntimeError('模型快照链接不完整，原文件保留，未重新下载。')
    return files, link_state


def _recover(artifact_id, root, trust, build, check, report):
    root = Path(root)
    receipts = mm.app_data_dir() / '.artifact-trust'
    if at.install_receipt_valid(artifact_id, root, trust, receipts):
        return True
    receipt = at._receipt_path(receipts, artifact_id)
    if not os.path.lexists(receipt):
        return False
    check()
    # Ordinary explicit repairs on the same mount retain their existing
    # selective-download transaction; this recovery owns mount changes only.
    try:
        at._assert_private_entry(receipt, directory=False)
        payload = json.loads(receipt.read_text())
        if payload.get('root_device') == root.lstat().st_dev:
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        receipt, original, repaired, changed = plan_repair(artifact_id, root, trust, receipts)
    except (OSError, ValueError, TypeError, RuntimeError):
        raise RuntimeError('已有模型的安装记录或文件身份不符，原文件保留，未重新下载。') from None
    if not changed:
        raise RuntimeError('已有模型需要进一步核对，原文件保留，未重新下载。')
    report('正在校验已有配音模型，仅核对内容，不下载模型…', -1)
    files, markers, links = build(root, repaired)
    recorded = {item['path'] for item in repaired['files']}
    if recorded != set(files) | set(markers):
        raise RuntimeError('已有模型与固定版本文件清单不一致，原文件保留，未重新下载。')
    initial_inventory = _inventory(root, links)
    if initial_inventory[0] != recorded:
        raise RuntimeError('已有模型包含未确认的文件，原文件保留，未重新下载。')

    def unchanged():
        check()
        _path, current, current_repair, _ = plan_repair(artifact_id, root, trust, receipts)
        if current != original or current_repair != repaired or _inventory(root, links) != initial_inventory:
            raise RuntimeError('校验期间模型或安装记录发生变化，原文件保留，未重新下载。')

    unchanged()
    for rel, expected in markers.items():
        raw = (root / rel).read_bytes()
        try:
            valid = json.loads(raw) == expected if isinstance(expected, dict) else raw == expected
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise RuntimeError('已有模型完成标记不符，原文件保留，未重新下载。')
    digests = {}
    for rel in sorted(recorded):
        check()
        item = files.get(rel)
        if item is not None and not item.valid(check):
            raise RuntimeError('已有配音模型内容校验未通过，原文件保留，未重新下载。')
        digests[rel] = item.digest if item is not None and item.algorithm == 'sha256' else at._sha256_file(root / rel, check)
    unchanged()
    updated = dict(repaired, content_sha256=digests)
    raw = (json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    at.restore_install_receipt((receipt, raw))
    if not at.install_receipt_valid(artifact_id, root, trust, receipts):
        at.restore_install_receipt((receipt, original))
        raise RuntimeError('配音模型恢复后核验未通过，原安装记录已恢复。')
    report('已有配音模型核验通过，直接复用', .95)
    return True


def recover_qwen_weights(root, name, check=lambda: None, report=lambda *_: None):
    from .qwen3_weights import MODELS
    if name not in MODELS:
        raise ValueError('未知千问模型')
    lock = MODELS[name]
    def build(target, _repaired):
        files = {}
        for entry in lock['files']:
            rel = mt._safe_name(entry['path']).as_posix()
            if rel in files:
                raise RuntimeError('千问固定清单包含重复文件')
            files[rel] = mt.File(target / rel, entry['size'], entry['sha256'])
        return files, {'.lulu-qwen-model.json': {'model': name, 'revision': lock['revision']}}, {}
    return _recover('qwen3-weights:' + name, Path(root) / name, lock, build, check, report)


def recover_voxcpm_weights(check=lambda: None, report=lambda *_: None):
    from . import local_models as lm
    root = lm.model_dir(lm.VOXCPM2.id)
    trust = lm._trust(lm.VOXCPM2.id)
    def build(target, _repaired):
        cache = lm.hf_cache_dir(lm.VOXCPM2.id)
        repo, revision = trust['hf_repo'], trust['hf_revision']
        plan = mt.hf_plan(repo, revision, cache, local_dir=False, read_only=True)
        if plan.repo != repo or plan.revision != revision or plan.root != cache or not plan.files:
            raise RuntimeError('VoxCPM2 固定版本清单身份不符，未重新下载。')
        storage = cache / ('models--' + repo.replace('/', '--'))
        snapshot = storage / 'snapshots' / revision
        files, links = {}, {}
        for entry in plan.files:
            if entry.algorithm not in {'sha256', 'git-sha1'}:
                raise RuntimeError('模型摘要类型不符，未重新下载。')
            rel = entry.path.relative_to(snapshot)
            mt._safe_name(rel.as_posix())
            blob = storage / 'blobs' / entry.digest
            key = blob.relative_to(target).as_posix()
            files[key] = mt.File(blob, entry.size, entry.digest, entry.algorithm)
            if entry.path in links:
                raise RuntimeError('模型清单包含重复路径，未重新下载。')
            links[entry.path] = blob
        return files, {'.lulu_done': b'ok'}, links
    return _recover('tts-weights:' + lm.VOXCPM2.id, root, trust, build, check, report)

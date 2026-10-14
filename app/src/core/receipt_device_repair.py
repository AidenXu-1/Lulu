"""Explicit, offline recovery of legacy receipts after a mount device renumber.

Never used by normal readiness checks. The operator must authorize recovery;
all recorded identities except the volatile device number must still match.
This is metadata continuity recovery, not a new content authenticity check.
"""
import json
import os
from pathlib import Path
import tempfile
from . import artifact_trust as at


def plan_repair(artifact_id: str, root: Path, trust: dict, receipt_base: Path):
    receipt = at._receipt_path(receipt_base, artifact_id)
    info = at._assert_private_entry(receipt, directory=False)
    if at._mode(info.st_mode) != 0o600:
        raise RuntimeError('校验记录权限不符')
    original = receipt.read_bytes()
    payload = json.loads(original)
    # Reject symlink components rather than resolving them into a trusted path.
    root = Path(os.path.abspath(root))
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise RuntimeError('模型路径包含符号链接')
    st = at._assert_private_entry(root, directory=True)
    if (payload.get('schema') != at._RECEIPT_SCHEMA
        or payload.get('artifact_id') != artifact_id
        or payload.get('root') != str(root)
        or payload.get('root_inode') != st.st_ino
        or payload.get('trust') != trust):
        raise RuntimeError('目录身份或固定版本不符，不能仅修复磁盘编号')
    old_device = payload.get('root_device')
    files = payload.get('files')
    if not isinstance(old_device, int) or not isinstance(files, list) or not files:
        raise RuntimeError('校验记录不完整')
    seen = set()
    for expected in files:
        rel = expected.get('path') if isinstance(expected, dict) else None
        if not isinstance(rel, str) or not rel or rel.startswith('/') or '..' in Path(rel).parts or rel in seen:
            raise RuntimeError('校验记录路径异常')
        seen.add(rel)
        path = root / rel
        for parent in path.parents:
            if parent == root:
                break
            at._assert_private_entry(parent, directory=True)
        actual = at._file_entry(root, path)
        if expected.get('device') != old_device or actual['device'] != st.st_dev:
            raise RuntimeError('文件跨磁盘，不能统一恢复')
        adjusted = dict(expected, device=st.st_dev)
        if actual != adjusted:
            raise RuntimeError(f'文件身份或元数据变化：{rel}')
    repaired = dict(payload, root_device=st.st_dev,
                    files=[dict(f, device=st.st_dev) for f in files])
    return receipt, original, repaired, old_device != st.st_dev


def apply_repair(artifact_id, root, trust, receipt_base, backup_dir):
    receipt, original, repaired, changed = plan_repair(artifact_id, root, trust, receipt_base)
    if not changed:
        return False
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    at._assert_private_entry(backup_dir, directory=True)
    backup = backup_dir / receipt.name
    with backup.open('xb') as stream:
        os.chmod(backup, 0o600)
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    # Repeat immediately before committing; never seal a moving target.
    _, current, rechecked, _ = plan_repair(artifact_id, root, trust, receipt_base)
    if current != original or rechecked != repaired:
        raise RuntimeError('检查期间文件变化，停止恢复')
    fd, name = tempfile.mkstemp(prefix='.device-repair-', dir=receipt_base)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(repaired, stream, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(receipt)
        if not at.install_receipt_valid(artifact_id, root, trust, receipt_base):
            # Original remains in backup even if the environment changed again.
            raise RuntimeError('恢复后校验失败，旧记录已备份')
    finally:
        temp.unlink(missing_ok=True)
    return True

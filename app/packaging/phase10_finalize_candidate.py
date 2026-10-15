"""App tree digest shared by the product build identity checks."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat
from src.core.agent_entry.canonical_app_tree import CanonicalAppTreeError, canonical_app_tree_identity

class CandidateFinalizeError(RuntimeError):
    """The candidate App tree is unsafe or inconsistent."""

def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def _validate_internal_symlink(
    bundle: Path,
    path: Path,
    relative: str,
    target: str,
) -> None:
    target_path = Path(target)
    if target_path.is_absolute():
        raise CandidateFinalizeError("候选App包含绝对符号链接")
    normalized = Path(
        os.path.normpath(str(Path(relative).parent.joinpath(target_path)))
    )
    if normalized.parts and normalized.parts[0] == "..":
        raise CandidateFinalizeError("候选App符号链接可能逃逸产品包")
    try:
        resolved_bundle = bundle.resolve(strict=True)
        resolved_target = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CandidateFinalizeError("候选App符号链接循环或无法解析") from exc
    try:
        resolved_target.relative_to(resolved_bundle)
    except ValueError as exc:
        raise CandidateFinalizeError("候选App符号链接可能逃逸产品包") from exc

def _legacy_tree_digest(bundle: Path) -> tuple[str, int, int]:
    records: list[bytes] = []
    total_bytes = 0
    file_count = 0
    pending = [bundle]
    while pending:
        parent = pending.pop()
        try:
            entries = sorted(os.scandir(parent), key=lambda item: item.name)
        except OSError as exc:
            raise CandidateFinalizeError("候选App目录无法枚举") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(bundle).as_posix()
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise CandidateFinalizeError("候选App条目无法读取") from exc
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                raise CandidateFinalizeError("候选App包含非owner或可被他人写入的条目")
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                records.append(f"D\0{relative}\0{mode:o}\n".encode("utf-8"))
                pending.append(path)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                _validate_internal_symlink(bundle, path, relative, target)
                records.append(
                    f"L\0{relative}\0{mode:o}\0{target}\n".encode("utf-8")
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CandidateFinalizeError("候选App包含不支持的文件类型")
            raw = path.read_bytes()
            file_count += 1
            total_bytes += len(raw)
            records.append(
                f"F\0{relative}\0{mode:o}\0{len(raw)}\0{_sha256(raw)}\n".encode(
                    "utf-8"
                )
            )
    return _sha256(b"".join(sorted(records))), file_count, total_bytes

def _tree_digest(bundle: Path) -> tuple[str, int, int]:
    if (bundle/"Contents/Resources/component_delivery.v2.json").exists():
        from src.core.agent_entry.delivery_identity import delivery_app_tree_identity
        identity=delivery_app_tree_identity(bundle)
        return str(identity["sha256"]),int(identity["regular_file_count"]),int(identity["regular_bytes"])
    manifest = bundle / "Contents/Resources/agent_product_manifest.json"
    if bundle.name != "Lulu.app" or not manifest.is_file() or manifest.is_symlink():
        return _legacy_tree_digest(bundle)
    try:
        identity = canonical_app_tree_identity(bundle.resolve())
    except CanonicalAppTreeError as exc:
        raise CandidateFinalizeError(str(exc)) from exc
    return (
        str(identity["sha256"]),
        int(identity["regular_file_count"]),
        int(identity["regular_bytes"]),
    )

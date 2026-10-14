"""Owner-only canonical result store under Lulu's shared jobs layout."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from .storage_layout import SharedProductStorageLayout, StorageLayoutError
from .transcription_contracts import LocalFileTranscriptionResultV1
from .transcription_contracts import LocalTranscriptionSegmentV1
from .models import Transcript


_JOB_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_MAX_RESULT_BYTES = 32 * 1024 * 1024


class TranscriptionResultStoreError(RuntimeError):
    pass


def _metadata(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TranscriptionResultStoreError("结果路径不可用") from exc


def _validate_directory(path: Path) -> None:
    metadata = _metadata(path)
    if (
        metadata is None
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise TranscriptionResultStoreError("结果目录必须是owner-only普通目录")


def _ensure_directory(path: Path) -> None:
    metadata = _metadata(path)
    if metadata is None:
        try:
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise TranscriptionResultStoreError("无法创建owner-only结果目录") from exc
    _validate_directory(path)


def _validate_regular_file(path: Path) -> os.stat_result:
    metadata = _metadata(path)
    if (
        metadata is None
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_RESULT_BYTES
    ):
        raise TranscriptionResultStoreError("结果文件必须是owner-only普通文件")
    return metadata


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def inspect_existing_results(directory: Path) -> None:
    """Read-only structure check after the caller validates path ancestors.

    Recognizing result files grants no ledger authority or trust in their text.
    This never invokes the layout-creating or result-reading business methods.
    """
    _validate_directory(directory)
    for path in directory.iterdir():
        if not path.name.endswith(".json") or not _JOB_ID.fullmatch(path.name[:-5]):
            raise TranscriptionResultStoreError("结果目录含未知文件")
        _validate_regular_file(path)


class TranscriptionResultStore:
    def __init__(self, layout: SharedProductStorageLayout) -> None:
        if not isinstance(layout, SharedProductStorageLayout):
            raise TypeError("layout must be SharedProductStorageLayout")
        self._layout = layout
        self.directory = layout.jobs / "results"

    def _ensure_layout(self) -> None:
        # Application Support and Lulu also contain ordinary GUI data. Recheck
        # their owner and ancestors without requiring private result permissions.
        try:
            SharedProductStorageLayout.from_application_support_root(
                self._layout.application_support_root
            )
            if _metadata(self._layout.product_root) is None:
                self._layout.product_root.mkdir(mode=0o700)
            SharedProductStorageLayout.from_application_support_root(
                self._layout.product_root
            )
        except (StorageLayoutError, OSError) as exc:
            raise TranscriptionResultStoreError("结果容器路径无效") from exc
        for directory in (self._layout.jobs, self.directory):
            _ensure_directory(directory)

    @staticmethod
    def _target_name(job_id: str) -> str:
        if type(job_id) is not str or not _JOB_ID.fullmatch(job_id):
            raise TranscriptionResultStoreError("job_id无效")
        return f"{job_id}.json"

    def write(
        self,
        job_id: str,
        result: LocalFileTranscriptionResultV1,
    ) -> dict[str, str]:
        if type(result) is not LocalFileTranscriptionResultV1:
            raise TranscriptionResultStoreError("转录结果类型无效")
        payload = {
            "schema_id": "lulu.transcription-result/v1",
            "schema_revision": 1,
            "job_id": job_id,
            "transcript": {
                "text": result.text,
                "segments": [
                    {
                        "text": segment.text,
                        "start": segment.start,
                        "end": segment.end,
                    }
                    for segment in result.segments
                ],
                "language": result.language,
                "engine": result.engine,
            },
        }
        return self._write_payload(job_id, payload)

    def write_extraction(self, job_id: str, extraction: dict[str, object]) -> dict[str, str]:
        return self._write_payload(job_id, {
            'schema_id': 'lulu.extraction-result/v1', 'schema_revision': 1,
            'job_id': job_id, 'extraction': extraction,
        })

    def write_audio(self, job_id: str, audio: dict[str, object]) -> dict[str, str]:
        return self._write_payload(job_id, {
            "schema_id": "lulu.audio-result/v1", "schema_revision": 1,
            "job_id": job_id, "audio": audio,
        })

    def _write_payload(self, job_id: str, payload: dict[str, object]) -> dict[str, str]:
        self._ensure_layout()
        target = self.directory / self._target_name(job_id)
        if _metadata(target) is not None:
            raise TranscriptionResultStoreError("该job结果已经存在")
        raw = _canonical_bytes(payload)
        if len(raw) > _MAX_RESULT_BYTES:
            raise TranscriptionResultStoreError("转录结果过大")
        temporary = self.directory / f".{job_id}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("short result write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            _validate_regular_file(temporary)
            if _metadata(target) is not None:
                raise TranscriptionResultStoreError("该job结果已经存在")
            os.replace(temporary, target)
            directory_descriptor = os.open(
                self.directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except TranscriptionResultStoreError:
            raise
        except OSError as exc:
            raise TranscriptionResultStoreError("转录结果原子写入失败") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        stored = self._read_bytes(target)
        if stored != raw:
            raise TranscriptionResultStoreError("转录结果哈希回读失败")
        digest = hashlib.sha256(stored).hexdigest()
        return {
            "result_ref": f"lulu-result:{job_id}:sha256:{digest}",
            "content_hash": digest,
        }

    def write_link(self, job_id: str, result: Transcript) -> dict[str, str]:
        """Persist a link pipeline result through the existing canonical store."""
        if not isinstance(result, Transcript) or not result.engine:
            raise TranscriptionResultStoreError("链接转录结果类型无效")
        return self.write(
            job_id,
            LocalFileTranscriptionResultV1(
                text=result.text,
                segments=tuple(
                    LocalTranscriptionSegmentV1(
                        text=segment.text,
                        start=segment.start,
                        end=segment.end,
                    )
                    for segment in result.segments
                ),
                language=result.language,
                engine=result.engine,  # type: ignore[arg-type]
                source_path=result.source,
            ),
        )

    def _read_bytes(self, target: Path) -> bytes:
        _validate_regular_file(target)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_uid != os.getuid()
                    or opened.st_nlink != 1
                    or opened.st_size > _MAX_RESULT_BYTES
                ):
                    raise TranscriptionResultStoreError("结果文件身份无效")
                chunks: list[bytes] = []
                remaining = _MAX_RESULT_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(descriptor)
        except TranscriptionResultStoreError:
            raise
        except OSError as exc:
            raise TranscriptionResultStoreError("转录结果读取失败") from exc
        raw = b"".join(chunks)
        if len(raw) > _MAX_RESULT_BYTES:
            raise TranscriptionResultStoreError("转录结果过大")
        return raw

    def read(self, job_id: str) -> dict[str, object]:
        self._ensure_layout()
        raw = self._read_bytes(self.directory / self._target_name(job_id))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise TranscriptionResultStoreError("转录结果JSON无效") from exc
        if type(payload) is not dict or _canonical_bytes(payload) != raw:
            raise TranscriptionResultStoreError("转录结果不是canonical JSON")
        return payload

    def has_result(self, job_id: str) -> bool:
        """Presence only, for interrupted-task UI; this does not trust content."""
        target = self.directory / self._target_name(job_id)
        return target.exists() or target.is_symlink()

    def pending_receipt(self, job_id: str) -> dict[str, str]:
        """Recover a fully written result after a ledger commit interruption.

        Caller must authenticate the job and explicitly authorize local result
        publication. This method grants no permission to replay inference.
        """
        self._ensure_layout()
        raw = self._read_bytes(self.directory / self._target_name(job_id))
        digest = hashlib.sha256(raw).hexdigest()
        ref = f'lulu-result:{job_id}:sha256:{digest}'
        # Canonical encoding, schema, revision, job ID and exact bytes all agree.
        verified = self.read_verified_bytes(job_id, ref)
        if verified != raw:
            raise TranscriptionResultStoreError('恢复期间转录结果发生变化')
        return dict(result_ref=ref, content_hash=digest)

    def read_verified_bytes(self, job_id: str, result_ref: str, *, schema_id: str = "lulu.transcription-result/v1") -> bytes:
        """Read only a result bound by the caller's authenticated job ledger."""
        self._ensure_layout()
        raw = self._read_bytes(self.directory / self._target_name(job_id))
        expected = f"lulu-result:{job_id}:sha256:{hashlib.sha256(raw).hexdigest()}"
        if result_ref != expected:
            raise TranscriptionResultStoreError("任务结果引用或哈希不一致")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise TranscriptionResultStoreError("转录结果JSON无效") from exc
        if (type(payload) is not dict or payload.get("job_id") != job_id
                or payload.get("schema_id") != schema_id
                or payload.get("schema_revision") != 1 or _canonical_bytes(payload) != raw):
            raise TranscriptionResultStoreError("转录结果身份无效")
        return raw

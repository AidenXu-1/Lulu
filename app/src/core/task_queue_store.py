"""Private, atomic persistence for unfinished transcription tasks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import stat
from typing import Iterable

from .collect.preferences import MaterialPreferenceSnapshot
from .task import (
    Task,
    TaskExecutionPolicy,
    TaskGroup,
    TaskGroupMember,
    TaskState,
)

SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1024 * 1024
MAX_TASKS = 500
MAX_GROUPS = 100
MAX_TITLE = 500
MAX_SOURCE = 16 * 1024
MAX_IDENTIFIER = 500
RECOVERABLE_STATES = frozenset(
    {TaskState.PENDING, TaskState.QUEUED, TaskState.RUNNING, TaskState.FAILED}
)


class TaskQueueStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TaskQueueSnapshot:
    tasks: tuple[Task, ...]
    groups: tuple[TaskGroup, ...]


def _strict_keys(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise TaskQueueStoreError("invalid_schema", f"{label}字段异常")
    return value


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TaskQueueStoreError("invalid_schema", f"{label}不是有效整数")
    return value


def _text(value: object, label: str, maximum: int, *, allow_empty: bool = True) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not allow_empty and not value)
    ):
        raise TaskQueueStoreError("invalid_schema", f"{label}不是有效文本")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label, MAX_IDENTIFIER)


def _policy_to_dict(policy: TaskExecutionPolicy | None) -> dict | None:
    if policy is None:
        return None
    snapshot = policy.material_snapshot
    return {
        "origin": policy.origin,
        "mode": policy.mode,
        "stable_key": policy.stable_key,
        "media_destination": policy.media_destination,
        "media_directory": policy.media_directory,
        "prefer_subtitles": policy.prefer_subtitles,
        "engine_name": policy.engine_name,
        **({"local_only": True} if policy.local_only else {}),
        **({"requested_outputs": list(policy.requested_outputs)} if policy.requested_outputs is not None else {}),
        "material_snapshot": {
            "download_mode": snapshot.download_mode,
            "save_media": snapshot.save_media,
            "save_cover": snapshot.save_cover,
            "version": snapshot.version,
        },
    }


def _policy_from_dict(
    value: object,
    *,
    schema_version: int,
) -> TaskExecutionPolicy | None:
    if value is None:
        return None
    policy_keys = {
        "origin",
        "mode",
        "stable_key",
        "media_destination",
        "material_snapshot",
    }
    if schema_version >= 2:
        policy_keys.update({"media_directory", "prefer_subtitles"})
    if isinstance(value, dict) and "engine_name" in value:
        policy_keys.add("engine_name")
    if isinstance(value, dict) and "local_only" in value:
        policy_keys.add("local_only")
    if isinstance(value, dict) and "requested_outputs" in value:
        policy_keys.add("requested_outputs")
        if (not isinstance(value["requested_outputs"], list)
                or any(type(x) is not str for x in value["requested_outputs"])):
            raise TaskQueueStoreError("invalid_schema", "提取内容异常")
    raw = _strict_keys(value, policy_keys, "执行策略")
    snapshot_raw = _strict_keys(
        raw["material_snapshot"],
        {"download_mode", "save_media", "save_cover", "version"},
        "素材偏好",
    )
    if not isinstance(snapshot_raw["save_media"], bool) or not isinstance(
        snapshot_raw["save_cover"], bool
    ):
        raise TaskQueueStoreError("invalid_schema", "素材偏好布尔值异常")
    try:
        snapshot = MaterialPreferenceSnapshot(
            download_mode=_text(
                snapshot_raw["download_mode"], "下载模式", MAX_IDENTIFIER, allow_empty=False
            ),
            save_media=snapshot_raw["save_media"],
            save_cover=snapshot_raw["save_cover"],
            version=_integer(snapshot_raw["version"], "素材偏好版本"),
        )
        media_directory = (
            None
            if schema_version == LEGACY_SCHEMA_VERSION
            else raw["media_directory"]
        )
        if media_directory is not None:
            media_directory = _text(
                media_directory,
                "冻结保存目录",
                MAX_SOURCE,
                allow_empty=False,
            )
        prefer_subtitles = (
            None
            if schema_version == LEGACY_SCHEMA_VERSION
            else raw["prefer_subtitles"]
        )
        if prefer_subtitles is not None and not isinstance(prefer_subtitles, bool):
            raise TaskQueueStoreError("invalid_schema", "优先字幕快照异常")
        return TaskExecutionPolicy(
            origin=_text(raw["origin"], "任务来源", MAX_IDENTIFIER, allow_empty=False),
            mode=_text(raw["mode"], "任务模式", MAX_IDENTIFIER, allow_empty=False),
            stable_key=_text(raw["stable_key"], "稳定身份", MAX_IDENTIFIER),
            material_snapshot=snapshot,
            media_destination=_text(
                raw["media_destination"], "媒体目标", MAX_IDENTIFIER, allow_empty=False
            ),
            media_directory=media_directory,
            prefer_subtitles=prefer_subtitles,
            engine_name=raw.get("engine_name"),
            local_only=raw.get("local_only", False),
            requested_outputs=tuple(raw["requested_outputs"]) if "requested_outputs" in raw else None,
        )
    except (TypeError, ValueError) as exc:
        raise TaskQueueStoreError("invalid_schema", "执行策略内容异常") from exc


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "source": task.source,
        "kind": task.kind,
        "state": task.state.value,
        "message": task.message,
        "error": task.error,
        "folder_id": task.folder_id,
        "stable_key": task.stable_key,
        "execution_policy": _policy_to_dict(task.execution_policy),
    }


def _task_from_dict(value: object, *, schema_version: int) -> Task:
    raw = _strict_keys(
        value,
        {
            "id",
            "title",
            "source",
            "kind",
            "state",
            "folder_id",
            "stable_key",
            "execution_policy",
        } | ({"message", "error"} if schema_version >= 3 else set()),
        "任务",
    )
    kind = _text(raw["kind"], "任务类型", MAX_IDENTIFIER, allow_empty=False)
    if kind not in {"file", "link"}:
        raise TaskQueueStoreError("invalid_schema", "任务类型异常")
    try:
        state = TaskState(
            _text(raw["state"], "任务状态", MAX_IDENTIFIER, allow_empty=False)
        )
    except ValueError as exc:
        raise TaskQueueStoreError("invalid_schema", "任务状态异常") from exc
    if state not in RECOVERABLE_STATES:
        raise TaskQueueStoreError("invalid_schema", "恢复文件包含终态任务")
    return Task(
        id=_integer(raw["id"], "任务 id"),
        title=_text(raw["title"], "任务标题", MAX_TITLE, allow_empty=False),
        source=_text(raw["source"], "任务来源", MAX_SOURCE, allow_empty=False),
        kind=kind,
        state=state,
        message=_text(raw.get("message", "待转录"), "任务说明", MAX_SOURCE),
        error=None if raw.get("error") is None else _text(raw["error"], "失败原因", MAX_SOURCE),
        folder_id=_optional_text(raw["folder_id"], "文件夹 id"),
        stable_key=_text(raw["stable_key"], "稳定身份", MAX_IDENTIFIER),
        execution_policy=_policy_from_dict(
            raw["execution_policy"],
            schema_version=schema_version,
        ),
    )


def _group_to_dict(group: TaskGroup, eligible_ids: set[int]) -> dict | None:
    members = [
        {
            "task_id": member.task_id,
            "stable_key": member.stable_key,
            "title": member.title or "采集转写",
            "source": member.source,
            "kind": member.kind,
        }
        for member in group.members
        if member.task_id in eligible_ids
    ]
    if not members:
        return None
    return {
        "id": group.id,
        "name": group.name,
        "folder_id": group.folder_id,
        "expanded": group.expanded,
        "members": members,
    }


def _group_from_dict(value: object, task_ids: set[int]) -> TaskGroup:
    raw = _strict_keys(value, {"id", "name", "folder_id", "expanded", "members"}, "任务组")
    if not isinstance(raw["expanded"], bool) or not isinstance(raw["members"], list):
        raise TaskQueueStoreError("invalid_schema", "任务组内容异常")
    members: list[TaskGroupMember] = []
    seen: set[int] = set()
    for item in raw["members"]:
        member = _strict_keys(
            item, {"task_id", "stable_key", "title", "source", "kind"}, "任务组成员"
        )
        task_id = _integer(member["task_id"], "成员任务 id")
        if task_id not in task_ids or task_id in seen:
            raise TaskQueueStoreError("invalid_schema", "任务组成员引用异常")
        seen.add(task_id)
        kind = _text(member["kind"], "成员类型", MAX_IDENTIFIER, allow_empty=False)
        if kind not in {"file", "link"}:
            raise TaskQueueStoreError("invalid_schema", "成员类型异常")
        members.append(
            TaskGroupMember(
                stable_key=_text(member["stable_key"], "成员稳定身份", MAX_IDENTIFIER),
                title=_text(member["title"], "成员标题", MAX_TITLE, allow_empty=False),
                source=_text(member["source"], "成员来源", MAX_SOURCE, allow_empty=False),
                kind=kind,
                task_id=task_id,
                attempt_task_ids=[task_id],
            )
        )
    if not members:
        raise TaskQueueStoreError("invalid_schema", "任务组没有可恢复成员")
    return TaskGroup(
        id=_integer(raw["id"], "任务组 id"),
        name=_text(raw["name"], "任务组名称", MAX_TITLE, allow_empty=False),
        members=members,
        folder_id=_optional_text(raw["folder_id"], "任务组文件夹 id"),
        expanded=raw["expanded"],
    )


class TaskQueueStore:
    """Versioned store that permanently blocks writes after a corrupt load."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.load_failed = False

    @classmethod
    def production(cls) -> "TaskQueueStore":
        from .models_manager import app_data_dir

        return cls(app_data_dir() / "task-queue" / "pending.json")

    def load(self) -> TaskQueueSnapshot:
        if not self.path.exists() and not self.path.is_symlink():
            return TaskQueueSnapshot((), ())
        try:
            data = self._read()
            raw = _strict_keys(data, {"schema", "tasks", "groups"}, "恢复文件")
            schema_version = raw["schema"]
            if schema_version not in {LEGACY_SCHEMA_VERSION, 2, SCHEMA_VERSION}:
                raise TaskQueueStoreError("unsupported_schema", "恢复文件版本不支持")
            if not isinstance(raw["tasks"], list) or len(raw["tasks"]) > MAX_TASKS:
                raise TaskQueueStoreError("limit_exceeded", "恢复任务数量异常")
            if not isinstance(raw["groups"], list) or len(raw["groups"]) > MAX_GROUPS:
                raise TaskQueueStoreError("limit_exceeded", "恢复任务组数量异常")
            tasks = tuple(
                _task_from_dict(item, schema_version=schema_version)
                for item in raw["tasks"]
            )
            task_ids = {task.id for task in tasks}
            if len(task_ids) != len(tasks):
                raise TaskQueueStoreError("invalid_schema", "任务 id 重复")
            groups = tuple(_group_from_dict(item, task_ids) for item in raw["groups"])
            group_ids = {group.id for group in groups}
            if len(group_ids) != len(groups):
                raise TaskQueueStoreError("invalid_schema", "任务组 id 重复")
            grouped_ids = [member.task_id for group in groups for member in group.members]
            if len(grouped_ids) != len(set(grouped_ids)):
                raise TaskQueueStoreError("invalid_schema", "任务被多个任务组引用")
            return TaskQueueSnapshot(tasks, groups)
        except TaskQueueStoreError:
            self.load_failed = True
            raise
        except Exception as exc:
            self.load_failed = True
            raise TaskQueueStoreError("read_failed", "上次任务列表无法读取") from exc

    def save(self, tasks: Iterable[Task], groups: Iterable[TaskGroup]) -> bool:
        if self.load_failed:
            return False
        eligible = [task for task in tasks if task.state in RECOVERABLE_STATES]
        if len(eligible) > MAX_TASKS:
            raise TaskQueueStoreError("limit_exceeded", "待恢复任务超过上限")
        eligible_ids = {task.id for task in eligible}
        if len(eligible_ids) != len(eligible):
            raise TaskQueueStoreError("invalid_schema", "待恢复任务 id 重复")
        group_values = [
            value
            for group in groups
            if (value := _group_to_dict(group, eligible_ids))
        ]
        if len(group_values) > MAX_GROUPS:
            raise TaskQueueStoreError("limit_exceeded", "待恢复任务组超过上限")
        value = {
            "schema": SCHEMA_VERSION,
            "tasks": [_task_to_dict(task) for task in eligible],
            "groups": group_values,
        }
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise TaskQueueStoreError("limit_exceeded", "待恢复任务文件超过上限")
        self._atomic_write(encoded)
        return True

    def _ensure_parent(self) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TaskQueueStoreError("unsafe_path", "任务恢复目录不安全")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise TaskQueueStoreError("unsafe_owner", "任务恢复目录归属异常")
        os.chmod(parent, 0o700)

    def _read(self) -> object:
        info = self.path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TaskQueueStoreError("unsafe_path", "任务恢复文件类型不安全")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise TaskQueueStoreError("unsafe_mode", "任务恢复文件权限异常")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise TaskQueueStoreError("unsafe_owner", "任务恢复文件归属异常")
        fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode) or current.st_size > MAX_FILE_BYTES:
                raise TaskQueueStoreError("unsafe_file", "任务恢复文件异常")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise TaskQueueStoreError("limit_exceeded", "任务恢复文件过大")
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TaskQueueStoreError("invalid_json", "任务恢复文件损坏") from exc
        finally:
            os.close(fd)

    def _atomic_write(self, data: bytes) -> None:
        self._ensure_parent()
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise TaskQueueStoreError("unsafe_path", "任务恢复文件类型不安全")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise TaskQueueStoreError("unsafe_owner", "任务恢复文件归属异常")
        tmp = self.path.parent / f".{self.path.name}.{secrets.token_hex(16)}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

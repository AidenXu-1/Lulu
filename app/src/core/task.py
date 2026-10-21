"""转写任务模型(UI 任务队列用的轻量状态)。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .collect.preferences import MaterialPreferenceSnapshot
from .models import Transcript


class TaskState(str, Enum):
    PENDING = "pending"      # 待转录(已添加,等用户点「开始」)
    QUEUED = "queued"        # 已点开始、排队等转
    RUNNING = "running"      # 下载/转码/转录中
    DONE = "done"            # 完成
    FAILED = "failed"        # 失败
    CANCELLED = "cancelled"  # 已取消


_TASK_ORIGINS = frozenset({
    "single_link",
    "homepage_collect",
    "homepage_history_material",
    "homepage_batch_transcribe",
})
_TASK_MODES = frozenset({"materialize", "transcription_only"})
_MEDIA_DESTINATIONS = frozenset({
    "user_directory",
    "managed_collection",
    "temporary_only",
    "none",
})


def task_display_title(title: str, *, limit: int = 80) -> str:
    """Bound display metadata without changing the collected caption or identity."""
    title = title or "转写任务"
    return title if len(title) <= limit else title[:limit - 1] + "…"


@dataclass(frozen=True)
class TaskExecutionPolicy:
    """用户发起动作时冻结的任务执行策略。"""

    origin: str
    mode: str
    stable_key: str
    material_snapshot: MaterialPreferenceSnapshot
    media_destination: str
    media_directory: str | None = None
    prefer_subtitles: bool | None = None
    engine_name: str | None = None
    requested_outputs: tuple[str, ...] | None = None
    local_only: bool = False

    def __post_init__(self) -> None:
        if type(self.local_only) is not bool:
            raise TypeError("本地转录限制必须是 bool")
        if self.requested_outputs is not None:
            if (not isinstance(self.requested_outputs, tuple)
                    or len(set(self.requested_outputs)) != len(self.requested_outputs)
                    or any(x not in {"text", "video", "cover"} for x in self.requested_outputs)
                    or self.origin != "single_link"):
                raise ValueError("无效的提取内容")
        if self.engine_name is not None and (not isinstance(self.engine_name, str)
                or not self.engine_name or len(self.engine_name) > 128):
            raise ValueError("无效的转录引擎")
        if self.origin not in _TASK_ORIGINS:
            raise ValueError("未知任务来源")
        if self.mode not in _TASK_MODES:
            raise ValueError("未知任务模式")
        if self.media_destination not in _MEDIA_DESTINATIONS:
            raise ValueError("未知媒体目标")
        if not isinstance(self.material_snapshot, MaterialPreferenceSnapshot):
            raise TypeError("material_snapshot 必须是 MaterialPreferenceSnapshot")
        if self.media_directory is not None and (
            not isinstance(self.media_directory, str) or not self.media_directory
        ):
            raise TypeError("media_directory 必须是非空路径或 None")
        if self.prefer_subtitles is not None and not isinstance(
            self.prefer_subtitles, bool
        ):
            raise TypeError("prefer_subtitles 必须是 bool 或 None")
        if self.origin.startswith("homepage_") and not self.stable_key:
            raise ValueError("主页任务必须有稳定作品身份")
        if self.origin == "single_link" and (
            self.mode != "materialize"
            or self.media_destination not in {"user_directory", "none"}
        ):
            raise ValueError("普通入口只能写用户目录或不持久化")
        if self.origin in {"homepage_collect", "homepage_history_material"} and (
            self.mode != "materialize"
            or self.media_destination != "managed_collection"
        ):
            raise ValueError("主页素材任务只能提交到 Lulu 受管库")
        if self.origin == "homepage_batch_transcribe" and (
            self.mode != "transcription_only"
            or self.media_destination not in {"temporary_only", "none"}
        ):
            raise ValueError("主页批量转录只能使用临时素材")
        if self.mode == "transcription_only":
            if self.media_destination not in {"temporary_only", "none"}:
                raise ValueError("纯转录任务不得指向持久媒体目标")
            normalized = MaterialPreferenceSnapshot("audio", False, False)
            if self.material_snapshot != normalized:
                object.__setattr__(
                    self,
                    "material_snapshot",
                    normalized,
                )

    @classmethod
    def transcription_only(
        cls,
        *,
        origin: str,
        stable_key: str,
        snapshot: MaterialPreferenceSnapshot,
    ) -> "TaskExecutionPolicy":
        return cls(
            origin=origin,
            mode="transcription_only",
            stable_key=stable_key,
            material_snapshot=snapshot,
            media_destination="temporary_only",
        )


@dataclass
class Task:
    id: int | str
    title: str
    source: str
    kind: str                       # "file" | "link"
    state: TaskState = TaskState.PENDING
    progress: float = 0.0           # 0~1;<0 表示不确定
    message: str = "待转录"
    transcript: Transcript | None = None
    error: str | None = None
    library_id: str | None = None   # 入文稿库后的条目 id(摘要持久化用)
    folder_id: str | None = None    # 入库目标文件夹(主页采集批量转录自动归类用)
    stable_key: str = ""            # 主页采集作品身份；普通任务保持空
    execution_policy: TaskExecutionPolicy | None = None

    @property
    def is_active(self) -> bool:
        return self.state in (TaskState.QUEUED, TaskState.RUNNING)

    @property
    def is_pending(self) -> bool:
        return self.state == TaskState.PENDING


@dataclass(frozen=True)
class TaskGroupCandidate:
    """采集页交给任务组的运行期候选，不承载任何执行状态。"""

    stable_key: str
    title: str
    source: str = ""
    kind: str = "link"
    skipped_reason: str = ""

    @property
    def executable(self) -> bool:
        return bool(self.source) and not self.skipped_reason


@dataclass
class TaskGroupMember:
    """任务组成员只引用真实 Task id；安全重试优先复用当前 Task。"""

    stable_key: str
    title: str
    source: str = ""
    kind: str = "link"
    task_id: int | str | None = None
    skipped_reason: str = ""
    attempt_task_ids: list[int | str] = field(default_factory=list)

    @property
    def is_skipped(self) -> bool:
        return bool(self.skipped_reason)


@dataclass
class TaskGroup:
    """视图聚合；仅未完成成员随任务恢复队列跨会话保存。"""

    id: int
    name: str
    members: list[TaskGroupMember]
    folder_id: str | None = None
    expanded: bool = False


def task_group_view_identity(group: TaskGroup, *, require_bounded=False):
    """Recognise the same batch across the bounded Core view and full draft.

    Include the batch id so two distinct batches with the same display prefix
    remain separate. Only fields actually stored in the Core view participate.
    """
    name = task_display_title(group.name, limit=500)
    if require_bounded and (name != group.name or any(
            m.task_id is None and m.title != task_display_title(m.title, limit=2000)
            for m in group.members)):
        return None
    members = frozenset(
        ('task', m.task_id) if m.task_id is not None else
        ('skipped', m.stable_key, task_display_title(m.title, limit=2000), m.skipped_reason)
        for m in group.members
    )
    return group.id, name, group.folder_id, members


def merge_task_groups(current: list[TaskGroup], restored: list[TaskGroup]) -> list[TaskGroup]:
    """Keep distinct batches even when separate sessions allocated the same id.

    Current edits win for the same batch. Member identities also recognise a
    restored batch after an earlier merge had to assign it a different id.
    """
    def member_keys(group):
        keys = set()
        for member in group.members:
            if member.task_id is not None:
                keys.add(('task', member.task_id))
                keys.update(('task', tid) for tid in member.attempt_task_ids)
            else:
                keys.add(('skipped', member.stable_key, member.title, member.source, member.kind))
        return frozenset(keys)

    result = list(current)
    used_ids = {group.id for group in result}
    identities = {(group.name, group.folder_id, member_keys(group)) for group in result}
    view_identities = {task_group_view_identity(group) for group in result}
    bounded_identities = {key for group in result
                          if (key := task_group_view_identity(group, require_bounded=True)) is not None}
    represented_tasks = {key for group in result for key in member_keys(group) if key[0] == 'task'}
    next_id = max((group.id for group in (*current, *restored)), default=0) + 1
    for group in restored:
        keys = member_keys(group)
        identity = (group.name, group.folder_id, keys)
        view_identity = task_group_view_identity(group)
        # Compare shortened text only against a bounded view. Two full drafts
        # can share an allocated id and prefix while containing different text.
        same_view = (view_identity in bounded_identities or
                     (task_group_view_identity(group, require_bounded=True) is not None
                      and view_identity in view_identities))
        if (identity in identities or same_view
                or represented_tasks.intersection(key for key in keys if key[0] == 'task')):
            continue
        if group.id in used_ids:
            group = replace(group, id=next_id)
            next_id += 1
        result.append(group)
        used_ids.add(group.id)
        identities.add(identity)
        view_identities.add(task_group_view_identity(group))
        bounded_identity = task_group_view_identity(group, require_bounded=True)
        if bounded_identity is not None:
            bounded_identities.add(bounded_identity)
        represented_tasks.update(key for key in keys if key[0] == 'task')
    return result


@dataclass(frozen=True)
class TaskGroupSnapshot:
    state: str
    completed: int
    total: int
    current_title: str
    succeeded: int
    failed: int
    skipped: int
    cancelled: int
    pending: int
    queued: int
    running: int

    @property
    def can_cancel(self) -> bool:
        return bool(self.pending or self.running)

    @property
    def unstarted(self) -> int:
        """尚未进入串行队列、可由“开始整组”调度的成员数。"""
        return max(0, self.pending - self.queued)

    @property
    def can_retry(self) -> bool:
        return bool(self.failed or self.cancelled)


@dataclass(frozen=True)
class CollectionItemRuntimeSnapshot:
    """从真实转录任务派生的采集表格运行态，不承载持久成功真值。"""

    transcript: str = ""
    media: str = ""


def derive_collection_item_runtime(
    tasks: dict[int | str, Task] | list[Task],
) -> dict[str, CollectionItemRuntimeSnapshot]:
    """按 stable_key 派生运行态；完成态交回文稿库/本地文件真值判断。"""

    source = tasks.values() if isinstance(tasks, dict) else tasks
    snapshots: dict[str, tuple[int, int, CollectionItemRuntimeSnapshot]] = {}
    for sequence, task in enumerate(source):
        key = str(task.stable_key or "")
        if not key:
            continue
        policy = task.execution_policy
        materializes_media = bool(
            policy is None
            or (
                policy.mode == "materialize"
                and policy.material_snapshot.save_media
            )
        )
        if task.state in (TaskState.PENDING, TaskState.QUEUED):
            priority = 2
            snapshot = CollectionItemRuntimeSnapshot(
                transcript="queued",
                media="queued" if materializes_media else "",
            )
        elif task.state == TaskState.RUNNING:
            priority = 3
            message = str(task.message or "")
            downloading = any(
                marker in message
                for marker in ("下载", "获取音频", "获取视频", "保留音视频")
            )
            if getattr(task, "_execution_stage", ""):
                downloading = task._execution_stage in {"waiting_media", "download"}
            snapshot = CollectionItemRuntimeSnapshot(
                transcript="queued" if downloading else "transcribing",
                media=(
                    "downloading"
                    if downloading and materializes_media
                    else ""
                ),
            )
        elif task.state == TaskState.FAILED:
            priority = 1
            snapshot = CollectionItemRuntimeSnapshot(
                transcript="failed",
                media="failed" if materializes_media else "",
            )
        else:
            continue
        previous = snapshots.get(key)
        candidate = (priority, sequence, snapshot)
        if previous is None or candidate[:2] >= previous[:2]:
            snapshots[key] = candidate
    return {key: value[2] for key, value in snapshots.items()}


def derive_task_group(
    group: TaskGroup,
    tasks: dict[int | str, Task] | list[Task],
) -> TaskGroupSnapshot:
    """从真实子任务派生组状态；不缓存计数，不产生第二套调度真相。"""

    task_by_id = (
        tasks
        if isinstance(tasks, dict)
        else {task.id: task for task in tasks}
    )
    succeeded = failed = skipped = cancelled = pending = queued = running = 0
    current_title = ""

    for member in group.members:
        if member.is_skipped:
            skipped += 1
            continue
        task = task_by_id.get(member.task_id)
        if task is None:
            # 没有真实任务身份的可执行成员不能伪装成功；按取消终态呈现。
            cancelled += 1
            continue
        if task.state == TaskState.DONE:
            succeeded += 1
        elif task.state == TaskState.FAILED:
            failed += 1
        elif task.state == TaskState.CANCELLED:
            cancelled += 1
        elif task.state == TaskState.RUNNING:
            running += 1
            current_title = task.title or member.title
        else:
            pending += 1
            if task.state == TaskState.QUEUED:
                queued += 1

    total = len(group.members)
    completed = succeeded + failed + skipped + cancelled
    executable_total = total - skipped
    if running:
        state = "active"
    elif pending:
        state = "pending"
    elif executable_total and succeeded == executable_total and not skipped:
        state = "done"
    elif succeeded:
        state = "partial"
    elif failed:
        state = "failed"
    elif cancelled:
        state = "cancelled"
    else:
        state = "partial" if skipped else "done"
    return TaskGroupSnapshot(
        state=state,
        completed=completed,
        total=total,
        current_title=current_title,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        cancelled=cancelled,
        pending=pending,
        queued=queued,
        running=running,
    )

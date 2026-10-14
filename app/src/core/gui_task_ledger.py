"""GUI task-list adapter over the single measured Lulu Core channel."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import base64
import hashlib
import json
import math
from datetime import datetime

from .models import Segment, Transcript

from .task import Task, TaskExecutionPolicy, TaskGroup, TaskGroupMember, TaskState, task_display_title
from .task_queue_store import SCHEMA_VERSION, TaskQueueSnapshot, _policy_from_dict


CoreDispatcher = Callable[[str, dict[str, object]], dict[str, object]]

_GUI_STATE = {
    "prepared": TaskState.PENDING,
    "queued": TaskState.QUEUED,
    "running": TaskState.RUNNING,
    "needs_input": TaskState.FAILED,
    "cancel_requested": TaskState.RUNNING,
    "recovery_required": TaskState.FAILED,
    "succeeded": TaskState.DONE,
    "partial": TaskState.DONE,
    "failed": TaskState.FAILED,
    "cancelled": TaskState.CANCELLED,
}


def _execution_policy(policy: TaskExecutionPolicy | None) -> dict[str, object] | None:
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
        **({"requested_outputs": list(policy.requested_outputs)} if policy.requested_outputs is not None else {}),
        "material_snapshot": {
            "download_mode": snapshot.download_mode,
            "save_media": snapshot.save_media,
            "save_cover": snapshot.save_cover,
            "version": snapshot.version,
        },
    }


def _task_payload(task: Task) -> dict[str, object]:
    return {
        "title": task_display_title(task.title, limit=500),
        "source": task.source,
        "kind": task.kind,
        "folder_id": task.folder_id,
        "stable_key": task.stable_key,
        "execution_policy": _execution_policy(task.execution_policy),
    }


def _task_from_job(value: object) -> Task:
    if not isinstance(value, dict):
        raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
    expected = {
        "job_id",
        "job_revision",
        "state",
        "job_kind",
        "input_ref",
        "gui_task",
        "result_ref",
        "created_by",
    }
    if set(value) not in (expected, expected | {"failure"}):
        raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
    job_id = value["job_id"]
    revision = value["job_revision"]
    state = value["state"]
    gui = value["gui_task"]
    if (
        not isinstance(job_id, str)
        or not job_id
        or type(revision) is not int
        or revision < 1
        or state not in _GUI_STATE
        or not isinstance(gui, dict)
    ):
        raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
    title = gui.get("title")
    source = gui.get("source")
    kind = gui.get("kind")
    task = Task(
        id=job_id,
        title=title if isinstance(title, str) and title else "转写任务",
        source=source if isinstance(source, str) and source else str(value["input_ref"]),
        kind=kind if kind in {"file", "link"} else "file",
        state=_GUI_STATE[str(state)],
        folder_id=(gui.get("folder_id") if isinstance(gui.get("folder_id"), str) else None),
        stable_key=(gui.get("stable_key") if isinstance(gui.get("stable_key"), str) else ""),
        execution_policy=_policy_from_dict(
            gui.get("execution_policy"),
            schema_version=SCHEMA_VERSION,
        ),
    )
    task.message = {
        TaskState.PENDING: "待转录",
        TaskState.QUEUED: "排队中…",
        TaskState.RUNNING: "处理中…",
        TaskState.DONE: "已完成",
        TaskState.FAILED: "需要处理",
        TaskState.CANCELLED: "已取消",
    }[task.state]
    task._core_policy = gui.get("execution_policy")
    # Optional display metadata: old runtimes/history remain readable.
    try:
        created = datetime.fromisoformat(gui.get("_display_created_at", "").replace("Z", "+00:00"))
        task._display_created_at = created.timestamp() if created.tzinfo else 0
    except (ValueError, TypeError, AttributeError, OverflowError):
        task._display_created_at = 0
    task._core_job_revision = revision
    failure = value.get("failure")
    if task.state is TaskState.FAILED and isinstance(failure, str):
        task.error = failure[:8000]
    task._core_result_ref = value["result_ref"]
    ref = value["result_ref"]
    if isinstance(ref, str) and ref.startswith("lulu-library:"):
        import re
        entry_id = ref.removeprefix("lulu-library:")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", entry_id):
            task.library_id = entry_id
    if isinstance(ref, str) and ref == f"lulu-extraction:{job_id}":
        from .extraction import load_result
        try:
            task.transcript = load_result(job_id)
            task.library_id = (task.transcript.meta or {}).get("extraction_library_id")
            task.error = "；".join((task.transcript.meta or {}).get("extraction_errors", [])) or None
            if task.error and task.state is TaskState.DONE: task.message = "部分完成"
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            task.message = "结果记录无法读取，原素材仍保留"
            task.error = task.message
        task._extraction_result_ref = ref
    task._core_state = state
    task._core_view_only = value["created_by"] != "gui"
    return task


class CoreTaskQueueStore:
    """Task-queue shaped client; authoritative bytes remain Core-only."""

    authoritative = True

    def __init__(self, dispatcher: CoreDispatcher, *, async_write: bool = False) -> None:
        if not callable(dispatcher):
            raise TypeError("dispatcher must be callable")
        self._dispatch = dispatcher
        self.path = None
        self.async_write = async_write
        self._known_jobs: dict[str, Task] = {}
        self._needs_reload = False
        self._view = {"revision": 0, "groups": [], "hidden_ids": []}
        self._visible_ids: set[str] = set()

    @classmethod
    def production(cls) -> "CoreTaskQueueStore":
        from .agent_entry.entrypoints import build_default_gui_core_dispatcher

        return cls(build_default_gui_core_dispatcher(), async_write=True)

    @property
    def hidden_ids(self):
        return frozenset(self._view['hidden_ids'])

    @classmethod
    def unavailable(cls) -> "CoreTaskQueueStore":
        def reject(_command: str, _arguments: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("CORE_TASK_SERVICE_UNAVAILABLE")

        return cls(reject, async_write=True)

    def load(self) -> TaskQueueSnapshot:
        response = self._dispatch("gui.jobs.list", {"include_view": True} if self.async_write else {})
        if set(response) != ({"jobs", "view"} if self.async_write else {"jobs"}) or not isinstance(response["jobs"], list):
            raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
        tasks = tuple(_task_from_job(job) for job in response["jobs"])
        self._known_jobs = {task.id: task for task in tasks}
        self._needs_reload = False
        groups = []
        if self.async_write:
            self._view = response["view"]
            tasks = tuple(task for task in tasks if task.id not in self._view["hidden_ids"])
        # A frozen input title can still be “链接转写”. Completed-card display
        # comes from the saved result, while Core state and inputs remain fixed.
        from . import library
        for task in tasks:
            if task.transcript is not None:
                if task.transcript.title: task.title = task.transcript.title[:80]
                continue
            if task.state is not TaskState.DONE or not task.library_id:
                continue
            try:
                transcript = library.load(task.library_id)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if transcript is not None:
                task.transcript = transcript
                if isinstance(transcript.title, str) and transcript.title.strip():
                    task.title = transcript.title.strip()[:80]
        if self.async_write:
            for raw in self._view["groups"]:
                members = []
                for member in raw["members"]:
                    tid = member.get("task_id")
                    task = self._known_jobs.get(tid)
                    if task is not None and tid not in self._view["hidden_ids"]:
                        members.append(TaskGroupMember(task.stable_key, task.title, task.source, task.kind, tid, attempt_task_ids=[tid]))
                    elif not tid and member.get("skipped_reason"):
                        members.append(TaskGroupMember(member["stable_key"], member["title"], skipped_reason=member["skipped_reason"]))
                if members:
                    groups.append(TaskGroup(raw["id"], raw["name"], members, raw["folder_id"], raw["expanded"]))
        self._visible_ids = {task.id for task in tasks}
        for task in tasks:
            if task.state is TaskState.QUEUED and not getattr(task, "_core_view_only", False):
                task._core_recovered_queue = True
                task.message = "上次排队未完成，点击继续"
        return TaskQueueSnapshot(tasks, tuple(groups))

    def prepare(self, task: Task) -> Task:
        from uuid import uuid4
        task.id = f"gui-job-{uuid4().hex}"
        task._core_pending_create = True
        return task

    @staticmethod
    def _adopt(task: Task, projected: Task) -> None:
        for field in ("_core_job_revision", "_core_result_ref", "_core_state", "_core_view_only", "_core_policy"):
            setattr(task, field, getattr(projected, field, False))
        task._core_pending_create = False
        task._display_created_at = getattr(projected, "_display_created_at", 0)

    def _submit_many(self, operations: list[tuple[Task, dict]]) -> None:
        while operations:
            chunk, size = [], 0
            while operations and len(chunk) < 512:
                candidate = operations[0]
                item_size = len(json.dumps(candidate[1]).encode("utf-8")) + 2
                if chunk and size + item_size > 900_000:
                    break
                chunk.append(operations.pop(0))
                size += item_size
            try:
                response = self._dispatch("gui.jobs.batch", {"operations": [op for _, op in chunk]})
                if (set(response) != {"results", "error"} or not isinstance(response["results"], list)
                        or len(response["results"]) > len(chunk)):
                    raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
                for (task, _), result in zip(chunk, response["results"]):
                    projected = _task_from_job(result)
                    if projected.id != task.id:
                        raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
                    self._known_jobs[task.id] = projected
                    self._adopt(task, projected)
                if response["error"] is not None or len(response["results"]) != len(chunk):
                    from .diagnostics import failure_code
                    # Preserve only a closed cause code, never arbitrary Core
                    # reply text. Otherwise the GUI erases the useful diagnosis.
                    cause = failure_code(RuntimeError(response["error"]))
                    error = RuntimeError("CORE_TASK_BATCH_INCOMPLETE")
                    if len(response["results"]) < len(chunk):
                        error.task_id = chunk[len(response["results"])][0].id
                    raise error from RuntimeError(cause)
            except Exception:
                self._needs_reload = True
                raise

    def _save_batch(self, tasks: list[Task], groups: list[TaskGroup]) -> bool:
        if self._needs_reload:
            self.load()
        for task in tasks:
            known = self._known_jobs.get(task.id)
            if known is not None:
                self._adopt(task, known)
        self._submit_many([
            (task, {"command": "gui.jobs.create", "arguments": {"task": _task_payload(task), "request_id": task.id}})
            for task in tasks if getattr(task, "_core_pending_create", False)
        ])
        self._visible_ids.update(task.id for task in tasks)
        operations = []
        removed_ids = self._visible_ids - {task.id for task in tasks}
        for job_id in removed_ids:
            known = self._known_jobs.get(job_id)
            if (known is not None and not getattr(known, "_core_view_only", False)
                    and known._core_state in {"prepared", "queued", "running", "needs_input", "recovery_required"}):
                operations.append((known, {"command": "gui.jobs.transition", "arguments": {
                    "job_id": job_id, "expected_revision": known._core_job_revision,
                    "state": "cancelled", "result_ref": None,
                }}))
        for task in tasks:
            if getattr(task, "_core_view_only", False):
                continue
            desired = self._desired_state(task)
            if getattr(task, "_core_state", None) == desired and not (
                desired == "prepared" and task.execution_policy is not None
                and task.execution_policy.requested_outputs is not None
                and _execution_policy(task.execution_policy) != getattr(task, "_core_policy", None)
            ):
                continue
            operations.append((task, {"command": "gui.jobs.transition", "arguments": {
                "job_id": task.id, "expected_revision": task._core_job_revision,
                "state": desired, "result_ref": getattr(task, "_extraction_result_ref", None) or (f"lulu-library:{task.library_id}" if task.library_id else None),
                "error_message": (task.error or "")[:8000] if desired == "failed" else None,
                **({"execution_policy": _execution_policy(task.execution_policy)}
                   if desired in {"prepared", "queued", "running"} and task.execution_policy is not None else {}),
            }}))
            if desired == "running":
                import os
                operations[-1][1]["arguments"]["owner_pid"] = os.getpid()
        self._submit_many(operations)
        visible = {task.id for task in tasks}
        hidden = set(self._view["hidden_ids"]) | (self._visible_ids - visible)
        group_values = [{"id": g.id, "name": task_display_title(g.name, limit=500), "folder_id": g.folder_id, "expanded": g.expanded,
            "members": [{"task_id": m.task_id} if m.task_id is not None else
                        {"title": task_display_title(m.title, limit=2000), "stable_key": m.stable_key, "skipped_reason": m.skipped_reason}
                        for m in g.members]} for g in groups]
        if group_values != self._view["groups"] or hidden != set(self._view["hidden_ids"]):
            self._view = self._dispatch("gui.jobs.view", {
                "groups": group_values, "hidden_ids": sorted(hidden), "expected_revision": self._view["revision"],
            })
        self._visible_ids = visible
        return True

    @staticmethod
    def _desired_state(task: Task) -> str:
        if task.state is TaskState.DONE and task.error and getattr(task, "_extraction_result_ref", None):
            return "partial"
        if task.state is TaskState.FAILED and task.transcript is not None and not task.library_id and not getattr(task, "_extraction_result_ref", None):
            if getattr(task, "_core_recovered_result", False) and getattr(task, "_core_state", None) in {None, "prepared"}:
                return "prepared"
            return "needs_input"
        core_state = getattr(task, "_core_state", None)
        if task.state is TaskState.FAILED and core_state in {"needs_input", "recovery_required"}:
            return core_state
        if task.state is TaskState.RUNNING and core_state == "cancel_requested":
            return core_state
        return {
            TaskState.PENDING: "prepared", TaskState.QUEUED: "queued", TaskState.RUNNING: "running",
            TaskState.DONE: "succeeded", TaskState.FAILED: "failed", TaskState.CANCELLED: "cancelled",
        }[task.state]

    def read_transcript(self, job_id: str, result_ref: str, *, is_cancelled=lambda: False) -> Transcript:
        raw = bytearray()
        total = None
        while total is None or len(raw) < total:
            if is_cancelled():
                raise RuntimeError("CANCELLED")
            response = self._dispatch("gui.jobs.result", {"job_id": job_id, "offset": len(raw)})
            if (set(response) != {"job_id", "result_ref", "offset", "total_bytes", "data"}
                    or response["job_id"] != job_id or response["result_ref"] != result_ref
                    or type(response["offset"]) is not int or response["offset"] != len(raw)
                    or type(response["total_bytes"]) is not int
                    or not 0 < response["total_bytes"] <= 32 * 1024 * 1024
                    or (total is not None and response["total_bytes"] != total)
                    or type(response["data"]) is not str):
                raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
            total = response["total_bytes"]
            try:
                chunk = base64.b64decode(response["data"], validate=True)
            except ValueError as exc:
                raise RuntimeError("CORE_TASK_RESPONSE_INVALID") from exc
            if not 0 < len(chunk) <= 32768 or len(raw) + len(chunk) > total:
                raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
            raw.extend(chunk)
        if result_ref != f"lulu-result:{job_id}:sha256:{hashlib.sha256(raw).hexdigest()}":
            raise RuntimeError("CORE_TASK_RESULT_INTEGRITY_ERROR")
        try:
            payload = json.loads(raw)
            if (set(payload) != {"schema_id", "schema_revision", "job_id", "transcript"}
                    or payload["schema_id"] != "lulu.transcription-result/v1"
                    or type(payload["schema_revision"]) is not int or payload["schema_revision"] != 1
                    or payload["job_id"] != job_id):
                raise ValueError()
            value = payload["transcript"]
            if (set(value) != {"text", "segments", "language", "engine"}
                    or type(value["text"]) is not str or type(value["segments"]) is not list
                    or (value["language"] is not None and type(value["language"]) is not str)
                    or type(value["engine"]) is not str):
                raise ValueError()
            segments = []
            for segment in value["segments"]:
                if set(segment) != {"text", "start", "end"} or type(segment["text"]) is not str:
                    raise ValueError()
                for key in ("start", "end"):
                    timestamp = segment[key]
                    if timestamp is not None and (type(timestamp) not in {int, float} or not math.isfinite(timestamp) or timestamp < 0):
                        raise ValueError()
                segments.append(Segment(**segment))
            return Transcript(text=value["text"], segments=segments, language=value["language"],
                              engine=value["engine"], source="", title=None, meta={})
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("CORE_TASK_RESPONSE_INVALID") from exc

    def register(self, task: Task) -> Task:
        if isinstance(task.id, str):
            return task
        created = _task_from_job(
            self._dispatch("gui.jobs.create", {"task": _task_payload(task)})
        )
        task.id = created.id
        task._core_job_revision = created._core_job_revision
        task._core_result_ref = created._core_result_ref
        task._core_state = created._core_state
        task._core_view_only = False
        task._display_created_at = getattr(created, "_display_created_at", 0)
        return task

    def save(self, tasks: Iterable[Task], groups: Iterable[TaskGroup]) -> bool:
        if self.async_write:
            return self._save_batch(list(tasks), list(groups))
        del groups
        for task in tasks:
            if bool(getattr(task, "_core_view_only", False)):
                continue
            if not isinstance(task.id, str):
                raise RuntimeError("CORE_TASK_ID_REQUIRED")
            revision = getattr(task, "_core_job_revision", None)
            if type(revision) is not int:
                raise RuntimeError("CORE_TASK_REVISION_REQUIRED")
            desired_state = self._desired_state(task)
            if getattr(task, "_core_state", None) == desired_state and not (
                desired_state == "prepared" and task.execution_policy is not None
                and task.execution_policy.requested_outputs is not None
                and _execution_policy(task.execution_policy) != getattr(task, "_core_policy", None)
            ):
                continue
            projected = _task_from_job(
                self._dispatch(
                    "gui.jobs.transition",
                    {
                        "job_id": task.id,
                        "expected_revision": revision,
                        "state": desired_state,
                        "result_ref": getattr(task, "_extraction_result_ref", None) or (f"lulu-library:{task.library_id}" if task.library_id else None),
                        "error_message": (task.error or "")[:8000] if desired_state == "failed" else None,
                        **({"execution_policy": _execution_policy(task.execution_policy)}
                           if desired_state in {"prepared", "queued", "running"} and task.execution_policy is not None else {}),
                    },
                )
            )
            task._core_job_revision = projected._core_job_revision
            task._core_result_ref = projected._core_result_ref
            task._core_state = projected._core_state
            task._core_policy = projected._core_policy
        return True

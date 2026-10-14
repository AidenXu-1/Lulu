"""失联退出时的私有输入草稿。它不授权执行，也不替代 Core 状态。"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import time

from .gui_task_ledger import _task_payload, _task_from_job
from .models import Transcript, Segment
from .task import TaskGroup, TaskGroupMember, TaskState, merge_task_groups, task_group_view_identity
from .task_queue_store import TaskQueueSnapshot
from .updater.secure_io import atomic_write, read_nofollow

_MAX_BYTES = 16 * 1024 * 1024


class GuiTaskRecoveryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.hidden_ids = frozenset()

    @staticmethod
    def _hidden_ids(values):
        if (not isinstance(values, list) or len(values) > 20000 or any(
                not isinstance(value, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value) is None
                for value in values)):
            raise ValueError("恢复移除记录格式异常，原文件已保留")
        return set(values)

    @classmethod
    def production(cls):
        from .models_manager import app_data_dir
        return cls(app_data_dir() / "task-recovery" / "unsynced.json")

    def save(self, tasks, groups, *, preserve_existing=True, hidden_ids=()):
        from .extraction import has_unsaved_result
        groups = list(groups)
        hidden = self._hidden_ids(list(hidden_ids))
        values = []
        for task in tasks:
            if getattr(task, "_core_view_only", False) and not has_unsaved_result(task):
                continue
            acknowledged = bool(task.library_id and
                getattr(task, "_core_result_ref", None) == f"lulu-library:{task.library_id}") or bool(
                getattr(task, "_extraction_result_ref", None) and
                getattr(task, "_core_result_ref", None) == task._extraction_result_ref)
            # Keep the body and reserved receipt until Core acknowledges the save.
            # A crash between the library write and that acknowledgement can replay
            # the same receipt without downloading, recognising or saving twice.
            # Recovery retains the original local text; only Core display
            # metadata is bounded, including drafts created by older builds.
            values.append({"id": task.id, "task": dict(_task_payload(task), title=task.title),
                "state": task.state.value, "error": task.error,
                "library_id": task.library_id,
                "pending_library_id": task.library_id or getattr(task, "_pending_library_id", ""),
                "transcript": asdict(task.transcript) if task.transcript and not acknowledged else None})
        group_values = [asdict(group) for group in groups]
        if preserve_existing and self.path.exists():
            previous = json.loads(read_nofollow(self.path, mode=0o600, maximum=_MAX_BYTES))
            hidden.update(self._hidden_ids(previous.get("hidden_ids", [])))
            ids = {item["id"] for item in values}
            values.extend(item for item in previous["tasks"] if item["id"] not in ids)
            previous_groups = [TaskGroup(**dict(item, members=[TaskGroupMember(**member)
                               for member in item["members"]])) for item in previous["groups"]]
            group_values = [asdict(group) for group in merge_task_groups(groups, previous_groups)]
        values = [item for item in values if item["id"] not in hidden]
        for group in group_values:
            group["members"] = [m for m in group["members"] if m["task_id"] not in hidden]
        payload = {"schema": 1, "tasks": values,
                   "groups": [g for g in group_values if g["members"]]}
        if hidden:
            payload["hidden_ids"] = sorted(hidden)
        raw = json.dumps(payload, ensure_ascii=False).encode()
        if len(raw) > _MAX_BYTES:
            raise ValueError("恢复记录超过容量上限")
        atomic_write(self.path, raw)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def merge(self, snapshot, *, authoritative_hidden_ids=()):
        try:
            raw = json.loads(read_nofollow(self.path, mode=0o600, maximum=_MAX_BYTES))
        except FileNotFoundError:
            self.hidden_ids = frozenset()
            return snapshot, False
        if (not isinstance(raw, dict) or set(raw) - {"hidden_ids"} != {"schema", "tasks", "groups"}
                or raw["schema"] != 1 or not isinstance(raw["tasks"], list)
                or not isinstance(raw["groups"], list) or len(raw["tasks"]) > 5000):
            raise ValueError("恢复记录格式异常，原文件已保留")
        local_hidden = self._hidden_ids(raw.get("hidden_ids", []))
        hidden = set(local_hidden)
        # A Core-hidden row is absent from the visible snapshot, not an
        # unsaved new task. Old GUI drafts cannot recreate or retry it.
        hidden.update(self._hidden_ids(list(authoritative_hidden_ids)))
        tasks = {task.id: task for task in snapshot.tasks if task.id not in hidden}
        for item in raw["tasks"]:
            if (not isinstance(item, dict) or set(item) - {"pending_library_id"} != {"id", "task", "state", "error", "library_id", "transcript"}
                    or not isinstance(item["id"], str)
                    or re.fullmatch(r"gui-job-[A-Za-z0-9_-]{1,160}", item["id"]) is None
                    or item["state"] not in {state.value for state in TaskState}):
                raise ValueError("恢复任务格式异常，原文件已保留")
            if item["id"] in hidden:
                continue
            task = tasks.get(item["id"])
            if task is None:
                task = _task_from_job({"job_id":item["id"], "job_revision":1,
                    "state":"cancelled" if item['state'] == TaskState.CANCELLED.value else "prepared",
                    "job_kind":"gui_transcription", "input_ref":"",
                    "gui_task":item["task"], "result_ref":None, "created_by":"gui"})
                task._core_pending_create = True
                del task._core_job_revision
                task._core_state = None
                task.message = "已取消，原输入保留" if task.state is TaskState.CANCELLED else "上次未保存，点击开始"
                tasks[task.id] = task
            elif _task_payload(task)["source"] != item["task"].get("source"):
                raise ValueError("恢复任务身份冲突，原文件已保留")
            # Unacknowledged draft edits may be replayed only while still prepared;
            # queued/running execution choices always come from Core.
            if task.state == TaskState.PENDING and item["state"] == TaskState.PENDING.value:
                from .task_queue_store import _policy_from_dict, SCHEMA_VERSION
                draft_policy = _policy_from_dict(item["task"].get("execution_policy"), schema_version=SCHEMA_VERSION)
                if draft_policy is not None and draft_policy.requested_outputs is not None:
                    task.execution_policy = draft_policy
            if task.state == TaskState.FAILED and isinstance(item["error"], str):
                task.error = item["error"][:8000]
            acknowledged_extraction = (task.transcript is not None
                and getattr(task, "_extraction_result_ref", None)
                and task._extraction_result_ref == getattr(task, "_core_result_ref", None))
            if item["transcript"] is not None and not task.library_id and not acknowledged_extraction:
                pending = item.get("pending_library_id", "")
                if not isinstance(pending, str) or (pending and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", pending) is None):
                    raise ValueError("恢复结果身份异常，原文件已保留")
                task._pending_library_id = pending
                value = dict(item["transcript"])
                value["segments"] = [Segment(**segment) for segment in value["segments"]]
                task.transcript = Transcript(**value)
                task.state = TaskState.FAILED
                task._core_recovered_result = True
                task.message = "结果待保存" if task.execution_policy and task.execution_policy.requested_outputs is not None else "转录内容已恢复，待保存文稿"
        restored_groups = []
        for value in raw["groups"]:
            value = dict(value)
            value["members"] = [TaskGroupMember(**member) for member in value["members"]
                if member["task_id"] is None or member["task_id"] in tasks]
            if value["members"]:
                restored_groups.append(TaskGroup(**value))
        drafts = {task_group_view_identity(group): group for group in restored_groups}
        # Keep full descriptions when the Core projection is the same batch.
        # Its id, membership, folder and current expanded preference stay fixed.
        core_groups = []
        for group in snapshot.groups:
            group = replace(group, members=[m for m in group.members if m.task_id not in hidden])
            if not group.members:
                continue
            draft = drafts.get(task_group_view_identity(group, require_bounded=True))
            core_groups.append(replace(group, name=draft.name, members=draft.members)
                               if draft is not None else group)
        groups = merge_task_groups(core_groups, restored_groups)
        self.hidden_ids = frozenset(local_hidden)
        return TaskQueueSnapshot(tuple(tasks.values()), tuple(groups)), True

    def archive(self):
        # 已同步后保留原草稿，尤其是磁盘曾写入失败的文稿内容，便于人工恢复。
        if self.path.exists():
            self.path.replace(self.path.with_name(f"recovered-{time.time_ns()}.json"))

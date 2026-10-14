"""Core-owned display preferences and GUI worker ownership, never task states."""
from __future__ import annotations
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .updater.secure_io import read_nofollow

VIEW_FILENAME = "gui-task-view-v1.json"


class GuiTaskViewStore:
    def __init__(self, root: Path):
        self.path = root / VIEW_FILENAME
        self.lock = RLock()

    def load(self):
        with self.lock:
            if not os.path.lexists(self.path):
                return {"revision": 0, "groups": [], "hidden_ids": [], "owners": {}}
            data = json.loads(read_nofollow(self.path, mode=0o600, maximum=4 * 1024 * 1024))
            if (type(data) is not dict or set(data) != {"revision", "groups", "hidden_ids", "owners"}
                    or type(data["revision"]) is not int or data["revision"] < 0
                    or type(data["groups"]) is not list or len(data["groups"]) > 2048
                    or type(data["hidden_ids"]) is not list or len(data["hidden_ids"]) > 20000
                    or any(type(value) is not str for value in data["hidden_ids"])
                    or type(data["owners"]) is not dict
                    or any(type(key) is not str or type(pid) is not int or pid <= 0
                           for key, pid in data["owners"].items())):
                raise RuntimeError("GUI_VIEW_INVALID")
            return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".gui-view-{uuid4().hex}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def save_view(self, *, groups, hidden_ids, expected_revision, known_ids):
        if type(groups) is not list or type(hidden_ids) is not list or type(expected_revision) is not int:
            raise RuntimeError("GUI_VIEW_INVALID")
        if len(groups) > 2048 or len(hidden_ids) > 20000 or any(type(i) is not str or i not in known_ids for i in hidden_ids):
            raise RuntimeError("GUI_VIEW_INVALID")
        seen_groups, seen_members = set(), set()
        for group in groups:
            if (type(group) is not dict or set(group) != {"id", "name", "folder_id", "expanded", "members"}
                    or type(group["id"]) is not int or group["id"] in seen_groups
                    or type(group["name"]) is not str or len(group["name"]) > 500
                    or type(group["expanded"]) is not bool or type(group["members"]) is not list):
                raise RuntimeError("GUI_VIEW_INVALID")
            seen_groups.add(group["id"])
            for member in group["members"]:
                if type(member) is not dict:
                    raise RuntimeError("GUI_VIEW_INVALID")
                if set(member) == {"task_id"}:
                    tid = member["task_id"]
                    if type(tid) is not str or tid not in known_ids or tid in seen_members:
                        raise RuntimeError("GUI_VIEW_INVALID")
                    seen_members.add(tid)
                elif set(member) != {"title", "stable_key", "skipped_reason"} or any(type(v) is not str or len(v) > 2000 for v in member.values()):
                    raise RuntimeError("GUI_VIEW_INVALID")
        with self.lock:
            data = self.load()
            if data["revision"] != expected_revision:
                raise RuntimeError("GUI_VIEW_REVISION_CONFLICT")
            data.update(groups=groups, hidden_ids=sorted(set(hidden_ids)), revision=expected_revision + 1)
            self._write(data)
            return {key: data[key] for key in ("revision", "groups", "hidden_ids")}

    def set_owner(self, job_id, pid):
        with self.lock:
            data = self.load()
            if pid is None:
                data["owners"].pop(job_id, None)
            elif type(pid) is int and pid > 0:
                data["owners"][job_id] = pid
            else:
                raise RuntimeError("GUI_WORKER_OWNER_INVALID")
            self._write(data)

    @staticmethod
    def owner_has_exited(pid):
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

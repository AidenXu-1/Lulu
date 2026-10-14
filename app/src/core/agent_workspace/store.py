"""Durable, conversation-scoped drafts and messages; task execution stays in Core."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .. import library


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("资料编号无效")
    return value


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def workspace_root() -> Path:
    root = library._dir() / ".workspace"
    if root.is_symlink():
        raise ValueError("工作区路径不安全，原资料已保留")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


class WorkspaceStore:
    def __init__(self):
        self.path = workspace_root() / "conversations.sqlite3"
        if self.path.is_symlink():
            raise ValueError("会话存储路径不安全")
        existed=self.path.exists()
        with library._transaction(), self.connect(initialize=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("会话来自更新版本，请使用相应版本打开；原记录已保留")
            if version == 0:
                if existed:
                    raise RuntimeError('现有会话数据库缺少版本信息，请查看资料保护与恢复；未重建空库')
                db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
                    title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    draft TEXT NOT NULL DEFAULT '{}', draft_revision INTEGER NOT NULL DEFAULT 0,
                    checkpoint TEXT, connection_id TEXT NOT NULL DEFAULT '',
                    next_reference INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS conversation_document ON conversations(document_id,updated_at);
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,sequence);
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    document_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=1;
                """)
            marker=workspace_root()/'database-created.json'
            if marker.is_symlink():
                raise ValueError('会话标记路径不安全')
            if not marker.exists():
                library._write_atomic_text(marker,encode(dict(version=1,created_at=now())))
            os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, *, initialize=False):
        # The same cross-process lock fences readers/writers against explicit
        # restore. Connections never remain open across model/network work.
        with library._transaction():
            from . import recovery
            if self.path != recovery.database_path():
                raise library.RevisionConflict('资料目录已切换，请重新打开当前工作区')
            recovery.finish_pending_restore()
            if self.path.is_symlink():
                raise ValueError('会话存储路径不安全')
            marker=workspace_root()/'database-created.json'
            if not self.path.exists() and (not initialize or marker.exists()):
                raise RuntimeError('会话数据库不在原位置，请从资料保护中恢复；未创建空库')
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL")
                with db:
                    yield db
                if db.total_changes:
                    recovery.maybe_backup(db)
            except sqlite3.DatabaseError as exc:
                raise RuntimeError('会话数据库暂时无法读取或写入，请查看资料保护与恢复；原文件已保留') from exc
            finally:
                db.close()

    @staticmethod
    def _conversation(row):
        value = dict(row)
        value["draft"] = json.loads(value["draft"])
        value["checkpoint"] = json.loads(value["checkpoint"]) if value["checkpoint"] else None
        return value

    def create(self, document_id: str, request_id: str, title="新对话") -> dict:
        identity(document_id); identity(request_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM conversations WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["document_id"] != document_id:
                    raise library.RevisionConflict("同一会话编号不能绑定另一篇文稿")
                return self._conversation(row)
            stamp = now()
            db.execute("INSERT INTO conversations(id,document_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (request_id, document_id, title[:100], stamp, stamp))
            default=db.execute("SELECT value FROM preferences WHERE key='default-connection'").fetchone()
            if default:
                db.execute('UPDATE conversations SET connection_id=? WHERE id=?', (json.loads(default[0]), request_id))
        self.preference("active:" + document_id, request_id)
        return self.get(request_id)

    def list(self, document_id: str) -> list[dict]:
        identity(document_id)
        with self.connect() as db:
            return [self._conversation(row) for row in db.execute(
                "SELECT * FROM conversations WHERE document_id=? ORDER BY updated_at DESC,id", (document_id,))]

    def delete_conversation(self, conversation_id: str) -> dict:
        """Permanently remove one chat and keep the document's active chat valid."""
        identity(conversation_id)
        with self.connect() as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document_id FROM conversations WHERE id=?',
                             (conversation_id,)).fetchone()
            if row is None:
                raise FileNotFoundError('会话不存在，未删除其他记录')
            document_id = row['document_id']
            active_key = 'active:' + document_id
            active = db.execute('SELECT value FROM preferences WHERE key=?',
                                (active_key,)).fetchone()
            db.execute('DELETE FROM proposals WHERE conversation_id=?', (conversation_id,))
            db.execute('DELETE FROM messages WHERE conversation_id=?', (conversation_id,))
            db.execute('DELETE FROM conversations WHERE id=?', (conversation_id,))
            db.execute('DELETE FROM preferences WHERE key=?', ('archived:' + conversation_id,))
            next_chat = db.execute(
                'SELECT id FROM conversations WHERE document_id=? ORDER BY updated_at DESC,id LIMIT 1',
                (document_id,),
            ).fetchone()
            if active and json.loads(active[0]) == conversation_id:
                if next_chat:
                    db.execute('UPDATE preferences SET value=? WHERE key=?',
                               (encode(next_chat['id']), active_key))
                else:
                    db.execute('DELETE FROM preferences WHERE key=?', (active_key,))
        return dict(document_id=document_id,
                    next_conversation_id=next_chat['id'] if next_chat else None,
                    deleted=True)

    def get(self, conversation_id: str) -> dict:
        identity(conversation_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("会话不存在，未创建替代会话")
            return self._conversation(row)

    def save_draft(self, conversation_id: str, draft: dict, expected_revision: int) -> dict:
        if type(draft) is not dict or set(draft) - {"text", "references", "reference_locations", "next_reference", "scroll", "model", "outbox", "options", "queued_followup"}:
            raise ValueError("草稿字段无效")
        if not isinstance(draft.get("text", ""), str) or type(draft.get("references", [])) is not list:
            raise ValueError("草稿格式无效")
        queued = draft.get("queued_followup")
        if queued is not None:
            if (type(queued) is not dict or set(queued) != {"job_id", "request_id", "text", "references", "reference_locations", "next_reference", "options"}
                    or type(queued.get("text")) is not str or not queued["text"].strip()
                    or len(queued["text"].encode()) > 16 * 1024
                    or type(queued.get("references")) is not list or type(queued.get("options")) is not dict
                    or type(queued.get("reference_locations")) is not dict
                    or len(queued["reference_locations"]) > 100
                    or type(queued.get("next_reference")) is not int
                    or not 1 <= queued["next_reference"] <= 10_001):
                raise ValueError("排队的补充要求格式无效")
            identity(queued["job_id"])
            identity(queued["request_id"])
            if len(queued["references"]) > 100:
                raise ValueError("排队的补充要求引用过多")
            for key, value in queued["reference_locations"].items():
                identity(key)
                if (type(value) is not dict or set(value) != {"kind", "from", "to"}
                        or value["kind"] not in {"source", "prosemirror"}
                        or type(value["from"]) is not int or type(value["to"]) is not int
                        or not 0 <= value["from"] < value["to"] <= 8 * 1024 * 1024):
                    raise ValueError("排队的补充要求引用定位信息无效")
        # Editor coordinates are only navigation hints. Core edit authority stays
        # with the versioned UTF-8 reference; these hints never enter model context.
        locations = draft.get("reference_locations", {})
        if type(locations) is not dict or len(locations) > 100:
            raise ValueError("引用定位信息无效")
        for key, value in locations.items():
            identity(key)
            if (type(value) is not dict or set(value) != {"kind", "from", "to"}
                    or value["kind"] not in {"source", "prosemirror"}
                    or type(value["from"]) is not int or type(value["to"]) is not int
                    or not 0 <= value["from"] < value["to"] <= 8 * 1024 * 1024):
                raise ValueError("引用定位信息无效")
        body = encode(draft)
        if len(body.encode()) > 4 * 1024 * 1024:
            raise ValueError("草稿过大，未截断；请先保存文稿或减少引用")
        identity(conversation_id)
        with self.connect() as db:
            changed = db.execute("UPDATE conversations SET draft=?,draft_revision=draft_revision+1 WHERE id=? AND draft_revision=?",
                                 (body, conversation_id, expected_revision)).rowcount
            if not changed:
                raise library.RevisionConflict("会话草稿已有新修改，当前输入仍保留")
        return self.get(conversation_id)

    def messages(self, conversation_id: str, *, before: int | None = None, limit: int = 100) -> list[dict]:
        self.get(conversation_id)
        if not 1 <= limit <= 500:
            raise ValueError("消息查询范围无效")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE conversation_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                              (conversation_id, before or 2**63-1, limit)).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in reversed(rows)]

    def append(self, conversation_id: str, role: str, content: str, payload=None, message_id=None) -> dict:
        self.get(conversation_id)
        if role not in {"user", "assistant", "tool", "status"} or not isinstance(content, str):
            raise ValueError("消息格式无效")
        mid = identity(message_id or uuid4().hex)
        body = encode(payload or {})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            if old:
                if (old["conversation_id"], old["role"], old["content"], old["payload"]) != (conversation_id, role, content, body):
                    raise library.RevisionConflict("同一消息编号对应不同内容，未重复发送")
                return dict(old, payload=json.loads(old["payload"]))
            stamp = now()
            db.execute("INSERT INTO messages(id,conversation_id,role,content,payload,created_at) VALUES(?,?,?,?,?,?)",
                       (mid, conversation_id, role, content, body, stamp))
            db.execute("UPDATE conversations SET updated_at=?,title=CASE WHEN title='新对话' AND ?='user' THEN ? ELSE title END WHERE id=?",
                       (stamp, role, content.strip().split('\n')[0][:26], conversation_id))
            row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return dict(row, payload=json.loads(row["payload"]))

    def proposal(self, proposal_id: str, value: dict | None = None):
        identity(proposal_id)
        with self.connect() as db:
            if value is not None:
                old = db.execute("SELECT conversation_id,document_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
                if old and tuple(old) != (value["conversation_id"], value["document_id"]):
                    raise library.RevisionConflict("修改建议不能转移到另一份资料或会话")
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (proposal_id, value["conversation_id"], value["document_id"], encode(value), now()))
            row = db.execute("SELECT payload FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("修改建议不存在")
            return json.loads(row[0])

    def proposals(self, conversation_id):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM proposals WHERE conversation_id=? ORDER BY created_at", (identity(conversation_id),))]

    def publish(self, conversation_id, message_id, content, payload, proposals):
        """A reply and all of its proposals appear together, or not at all."""
        self.get(conversation_id)
        identity(message_id)
        stamp = now()
        body = encode(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            if old:
                if (old['conversation_id'], old['content'], old['payload']) != (conversation_id, content, body):
                    raise library.RevisionConflict('已保存的回复与当前结果不同')
                return
            for proposal in proposals:
                if proposal['conversation_id'] != conversation_id:
                    raise ValueError('修改建议与会话不匹配')
                db.execute('INSERT INTO proposals VALUES(?,?,?,?,?)',
                           (proposal['id'], conversation_id, proposal['document_id'], encode(proposal), stamp))
            if proposals:
                fresh = {p['id'] for p in proposals}
                for row in db.execute('SELECT id,payload FROM proposals WHERE conversation_id=?', (conversation_id,)).fetchall():
                    previous = json.loads(row['payload'])
                    if row['id'] not in fresh and previous['state'] in {'pending','partly_applied'}:
                        previous['state'] = 'superseded'
                        for op in previous['operations']:
                            if op['state'] == 'pending':
                                op['state'] = 'superseded'
                        db.execute('UPDATE proposals SET payload=? WHERE id=?', (encode(previous),row['id']))
            db.execute('INSERT INTO messages(id,conversation_id,role,content,payload,created_at) VALUES(?,?,?,?,?,?)',
                       (message_id, conversation_id, 'assistant', content, body, stamp))
            db.execute('UPDATE conversations SET updated_at=? WHERE id=?', (stamp, conversation_id))

    def preference(self, key: str, value=None):
        if not isinstance(key, str) or len(key) > 220:
            raise ValueError("设置名称无效")
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT INTO preferences VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))
            row = db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

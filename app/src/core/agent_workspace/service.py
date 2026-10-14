"""Closed, framework-independent workspace application API for GUI and CLI."""
from __future__ import annotations

import inspect
import json
import sqlite3
from copy import deepcopy
from uuid import uuid4

from .. import library
from . import documents
from .store import WorkspaceStore, encode, identity


OPERATIONS = frozenset({"list", "bootstrap", "library_view", "open", "create_document", "save", "receipt", "recover_draft", "view_state",
                       "conversations", "new_conversation", "open_conversation", "save_draft",
                       "proposal_plan", "proposal_apply", "proposal_discard", "history", "restore",
                       "folders", "move", "rename_folder", "create_folder", "delete_folder", "delete", "trash", "restore_deleted", "purge_deleted", "trash_policy", "cleanup_trash",
                       "reference", "messages", "presets", "save_preset", "delete_preset", "audio", "catalog", "connections",
                       "save_connection", "discover_models", "select_connection", "context", "send", "compact", "run_status", "cancel_run", "runs", "migrate", "templates", "snapshot", "conversation_snapshot", "tool_handoff", "voice_target", "storage_status", "backup", "restore_backup"})
OPERATIONS = OPERATIONS | frozenset({'prepare_media', 'start_media', 'media_status', 'media_tasks', 'cancel_media', 'recover_media'})
OPERATIONS = OPERATIONS | frozenset({'skills', 'rename_conversation', 'edit_document', 'undo_edit', 'archive_conversation', 'tool_catalog'})
OPERATIONS = OPERATIONS | frozenset({'delete_conversation', 'skill_list', 'skill_details', 'skill_inspect', 'skill_install', 'skill_uninstall', 'skill_configure', 'skill_export', 'skill_drafts', 'skill_migrate_presets', 'skill_update'})
OPERATIONS = OPERATIONS | frozenset({'recover_document'})


def _conversation_task_payloads(conversation_id, runner):
    """Return known conversation payload files after checking no task is active."""
    from ..job_ledger.contracts import JobState
    from .media_tasks import KIND as MEDIA_KIND
    from .store import workspace_root
    from .tools import MAX_STEPS

    jobs = runner.control.list()
    active = {JobState.PREPARED, JobState.QUEUED, JobState.RUNNING, JobState.CANCEL_REQUESTED}
    conversations = [job for job in jobs if job.job_kind == 'workspace_conversation'
                     and job.frozen_policy.get('conversation_id') == conversation_id]
    media_jobs = [job for job in jobs if job.job_kind == MEDIA_KIND
                  and job.frozen_policy.get('conversation_id') == conversation_id]
    if any(job.state in active for job in conversations + media_jobs):
        raise ValueError('这段会话仍有任务在处理，请先停止或等它结束，再彻底删除')

    paths = []
    run_root = workspace_root() / 'runs'
    if run_root.is_symlink():
        raise ValueError('会话任务记录路径不安全，未删除')
    if run_root.exists():
        for job in conversations:
            suffixes = ('input', 'partial', 'response', 'result', 'error',
                        *(f'round-{index}' for index in range(MAX_STEPS + 1)))
            paths.extend(runner.path(job.job_id, suffix) for suffix in suffixes)

    media_root = workspace_root() / 'media'
    if media_root.is_symlink():
        raise ValueError('会话转录计划路径不安全，未删除')
    if media_root.exists():
        for path in media_root.glob('media-*.json'):
            if path.is_symlink():
                raise ValueError('会话转录计划路径不安全，未删除')
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError('有一份本地转录计划无法检查，未删除会话') from exc
            if type(value) is not dict:
                raise ValueError('有一份本地转录计划格式异常，未删除会话')
            if value.get('conversation_id') == conversation_id:
                paths.append(path)
    for path in paths:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('会话任务记录路径不安全，未删除')
    return [path for path in paths if path.is_file()]


def _remove_conversation_task_payloads(paths):
    for path in paths:
        path.unlink()


class WorkspaceService:
    def __init__(self, store=None, runtime=None, origin="gui"):
        self._store = store
        self.runtime = runtime
        self.origin = origin

    def skills(self):
        from .skills import catalog
        return catalog()

    def skill_list(self):
        from .skill_manager import listing
        return listing()

    def skill_details(self, name, reference='SKILL.md'):
        from .skill_manager import details
        return details(name, reference)

    def skill_inspect(self, source):
        from .skill_manager import inspect_source
        return inspect_source(source)

    def skill_install(self, name=None, token=None):
        from .skill_manager import install
        return install(name, token)

    def skill_uninstall(self, name):
        from .skill_manager import uninstall
        return uninstall(name)

    def skill_configure(self, name, enabled=None, config=None):
        from .skill_manager import configure
        return configure(name, enabled, config)

    def skill_update(self, name, rollback=False):
        from .skill_manager import update
        return update(name, rollback)

    def skill_export(self, name, destination):
        from .skill_manager import export
        return export(name, destination)

    def skill_drafts(self):
        from .skill_manager import drafts
        return dict(entries=drafts())

    def skill_migrate_presets(self):
        from .skill_manager import draft
        results = []
        for preset in self.presets()['custom']:
            key = 'skill-migration:' + preset['id']
            if self.store.preference(key):
                continue
            result = draft('custom-' + documents.digest(preset['id'])[:16],
                           preset['title'], preset['text'])
            self.store.preference(key, result)
            results.extend(result['entries'])
        return dict(entries=results, message='已转为待安装技能；原预设完整保留')

    def rename_conversation(self, conversation_id, title):
        self.store.get(conversation_id)
        if not isinstance(title,str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError('请输入 1–100 字的会话名称')
        with self.store.connect() as db:
            db.execute('UPDATE conversations SET title=? WHERE id=?',(title.strip(),conversation_id))
        return self.store.get(conversation_id)

    def edit_document(self, document_id, expected_revision, expected_hash, operations, request_id):
        """Atomically apply UTF-8 range edits to one version and return its receipt."""
        identity(request_id)
        if type(operations) is not list or not 1 <= len(operations) <= 100 or any(type(op) is not dict for op in operations):
            raise ValueError('需要 1 到 100 项有效编辑操作')
        intent = documents.digest(encode([document_id, expected_revision, expected_hash, operations]))
        key = 'direct-edit:' + request_id
        with library._transaction():
            previous = self.store.preference(key)
            if previous:
                if previous['intent'] != intent:
                    raise library.RevisionConflict('同一编辑编号对应不同内容，未重复写入')
                return documents.save(**previous['save'])
            doc = documents.snapshot(document_id)
            if (doc['revision'], doc['content_hash']) != (expected_revision, expected_hash):
                raise library.RevisionConflict('正文已有新修改，请读取最新版本后继续')
            updated = documents.apply_operations(doc['text'], operations)
            save = dict(document_id=document_id, markdown=updated, title=doc['title'],
                        expected_revision=expected_revision, expected_hash=expected_hash, request_id=request_id)
            self.store.preference(key, dict(intent=intent, save=save))
            return documents.save(**save)

    def undo_edit(self, request_id, expected_revision, expected_hash, undo_request_id):
        """Undo exactly one saved edit; reject if later edits changed the document."""
        value = documents._read(documents._folder('operations') / (identity(request_id) + '.json'))
        if value['state'] != 'committed':
            raise library.RevisionConflict('这次保存尚未完成恢复，请先读取最新文稿')
        if (expected_revision, expected_hash) != (value['after']['revision'], value['after_hash']):
            raise library.RevisionConflict('后面还有新修改，请从历史中比较恢复，未覆盖当前正文')
        return documents.save(value['document_id'], value['before']['text'], value['before']['title'],
                              expected_revision, expected_hash, undo_request_id)

    def archive_conversation(self, conversation_id, archived=True):
        """Hide a conversation without deleting its messages or draft."""
        if type(archived) is not bool:
            raise ValueError('归档状态无效')
        self.store.get(conversation_id)
        self.store.preference('archived:' + conversation_id, archived)
        return dict(conversation_id=conversation_id, archived=archived)

    def tool_catalog(self):
        """Discover workspace commands, required fields and actual model tools."""
        from .contracts import COMMANDS
        from .context import tool_schemas, PROMPT_VERSION
        commands = []
        for name, (cap, operation, effect) in COMMANDS.items():
            fn = getattr(type(self), operation)
            fields = []
            for key, parameter in inspect.signature(fn).parameters.items():
                if key == 'self':
                    continue
                field = dict(name=key, required=parameter.default is inspect.Parameter.empty)
                if not field['required']:
                    field['default'] = parameter.default
                fields.append(field)
            commands.append(dict(command=name, capability_id=cap, effect=effect,
                                 description=inspect.getdoc(fn) or '', fields=fields))
        return dict(version=1, prompt_version=PROMPT_VERSION, commands=commands, tools=tool_schemas(loaded_skills=('find-skills','skill-creator')),
                    skill_scoped_tools=['search_skills','prepare_skill_install','create_skill_draft','list_skill_drafts','install_skill'])

    @property
    def store(self):
        if self._store is None:
            self._store=WorkspaceStore()
        return self._store

    def storage_status(self):
        from .recovery import status
        return status()

    def backup(self):
        from .recovery import backup
        return backup()

    def restore_backup(self, backup_id, expected_fingerprint, expected_backup_hash):
        from .recovery import restore
        from ..job_ledger.contracts import JobState
        with library._transaction():
            if self.runtime:
                jobs=self.runtime._authorized_job_control('JOB-01').list()
                if any(j.job_kind in {'workspace_conversation', 'workspace_transcription'} and j.state in {
                        JobState.PREPARED,JobState.QUEUED,JobState.RUNNING,JobState.CANCEL_REQUESTED} for j in jobs):
                    raise RuntimeError('请先等待或停止正在运行的文稿会话和转录任务，再恢复备份')
            result=restore(backup_id,expected_fingerprint,expected_backup_hash)
            self._store=None
            return result

    def catalog(self):
        from .providers import catalog
        return catalog()

    def migrate(self):
        from ...config import AppConfig
        from .migration import migrate
        result=migrate(self.store,AppConfig.load())
        return {k:result[k] for k in ('version','phase','errors','imported_templates')}

    def templates(self):
        from .. import prompts
        return [dict(id=direction+':'+t.id,title=t.name,text=t.system_text,hint=direction)
                for direction in prompts.TEMPLATES for t in prompts.templates_for(direction)]

    def connections(self):
        from .providers import available_connections
        return available_connections(self.store)

    def save_connection(self, value):
        from .providers import save_connection
        return save_connection(value, self.store)

    def discover_models(self, connection_id=None, value=None):
        from .providers import discover
        return discover(connection_id, self.store, value)

    def select_connection(self, conversation_id, connection_id):
        from .providers import connection
        connection(connection_id, self.store)
        if self.runtime and any(r['state'] in {'queued','running','cancel_requested'} for r in self._runner().runs(conversation_id)):
            raise RuntimeError('请先等待或停止当前任务，再切换模型')
        with self.store.connect() as db:
            db.execute("UPDATE conversations SET connection_id=? WHERE id=?", (connection_id, conversation_id))
        chat=self.store.get(conversation_id)
        self.store.preference('default-connection', connection_id)
        return chat

    def context(self, conversation_id):
        from .context import projection
        if not self.store.get(conversation_id)["connection_id"]:
            return {"percent": None}
        value = projection(self.store, conversation_id)
        return {"percent": value['percent'], "threshold": round(100*value['compact_at']/value['connection']['window'])}

    def _runner(self):
        if self.runtime is None:
            raise RuntimeError("AI 任务需要通过 Lulu Core 运行，请使用正式程序的工作区入口")
        return self.runtime.workspace_application()

    def send(self, conversation_id, request_id, text, references, options=None):
        return self._runner().start(conversation_id, request_id, text, references, options=options, origin=self.origin)

    def compact(self, conversation_id, request_id):
        return dict(self._runner().start(conversation_id, request_id, mode="compact", origin=self.origin), message="正在整理早期交流…")

    def run_status(self, job_id):
        return self._runner().show(job_id)

    def runs(self, conversation_id):
        return self._runner().runs(conversation_id) if self.runtime else []

    def cancel_run(self, job_id):
        return self._runner().cancel(job_id)

    def _media(self):
        if self.runtime is None:
            raise RuntimeError('后台转录需要通过 Lulu Core 运行，请使用正式程序的工作区入口')
        return self.runtime.workspace_media()

    def prepare_media(self, conversation_id, request_id, source_path, engine):
        return self._media().prepare(conversation_id, request_id, source_path, engine)

    def start_media(self, plan_id, expected_hash):
        return self._media().start(plan_id, expected_hash, origin=self.origin)

    def media_status(self, job_id):
        return self._media().show(job_id)

    def media_tasks(self, conversation_id):
        return self._media().list(conversation_id) if self.runtime else []

    def cancel_media(self, job_id):
        return self._media().cancel(job_id)

    def recover_media(self, job_id):
        return self._media().recover(job_id)

    def tool_handoff(self, conversation_id, message_id, call_id):
        """Only a persisted result in this conversation can drive a GUI handoff."""
        self.store.get(conversation_id)
        identity(message_id)
        if not isinstance(call_id,str) or not 1 <= len(call_id) <= 200:
            raise ValueError('工具调用编号无效')
        with self.store.connect() as db:
            row=db.execute("SELECT payload FROM messages WHERE conversation_id=? AND id=? AND role='tool'",
                           (conversation_id,message_id)).fetchone()
        if row is None:
            raise FileNotFoundError('工具记录不在当前会话中')
        payload=json.loads(row[0])
        for call,result in zip(payload['calls'],payload['results']):
            if call['id']==call_id and (result['state']=='needs_input' or (result['state']=='completed' and result.get('action')=='skills')):
                return result
        raise ValueError('此工具结果无需前往工作台')

    def voice_target(self, document_id, references=None):
        from .references import bind_instructions
        from .tools import target
        doc=documents.snapshot(document_id)
        refs=references if references is not None else []
        if type(refs) is not list or len(refs)>100:
            raise ValueError('引用数量无效')
        for ref in refs:
            if type(ref) is not dict or (ref.get('document_id'),ref.get('revision'),ref.get('content_hash')) != (doc['id'],doc['revision'],doc['content_hash']):
                raise library.RevisionConflict('配音引用对应的正文已变化，请重新选择')
        _,refs=bind_instructions('生成配音',refs,doc)
        return target(dict(document=doc,references=refs))

    def list(self, search="", folder_id=None, sort="updated", offset=0, limit=100, refresh=True, kind="documents"):
        if kind not in {"documents", "voice"}:
            raise ValueError("资料类型无效")
        return library.query_entries(kind=kind, search=search, folder_id=folder_id, sort=sort,
                                     offset=offset, limit=limit, refresh=refresh)

    def library_view(self, kind="documents", search="", folder_id=None, sort="updated", document_id=None):
        """One GUI request for the list and the selected screen, using normal reads."""
        listing = self.list(kind=kind, search=search, folder_id=folder_id, sort=sort)
        warnings = []
        selected = document_id
        if not selected:
            try:
                selected = self.view_state('audio' if kind == 'voice' else 'selected_document')
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                warnings.append('会话设置暂不可用，文稿原文件已保留。')
        content = None
        if selected:
            try:
                content = self.audio(selected) if kind == 'voice' else self.open(selected, include_workspace=True)
            except FileNotFoundError:
                selected = None
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                warnings.append('上次打开的资料暂不可读，其他资料可继续查看。')
                selected = None
        return dict(listing=listing, document_id=selected, content=content, warnings=warnings)

    def bootstrap(self):
        try:
            migration = self.migrate()
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            migration = dict(version=1, phase='unavailable', imported_templates=0,
                             errors=[dict(reason='旧资料登记暂不可用，原文件已保留')])
        try:
            columns = self.view_state('columns')
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            columns = None
        return dict(migration=migration, columns=columns, screen=self.library_view())

    def open(self, document_id, include_workspace=False):
        if type(include_workspace) is not bool:
            raise ValueError('界面读取参数无效')
        doc = self.snapshot(document_id)
        if doc.get('read_only'):
            return self._read_only_document(doc, doc.get('recovery_message', '原资料已保留'))
        try:
            return self._open_document_workspace(doc, include_workspace)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return self._read_only_document(doc, '会话记录暂不可用，正文可以阅读。请在资料保护与恢复中检查会话备份。')

    @staticmethod
    def _read_only_document(doc, message):
        return dict(document=dict(doc, read_only=True), conversation=None, messages=[], proposals=[],
                    recovery_draft=None, workspace=dict(scroll=None, connections=[], context=None, warning=message))

    def _open_document_workspace(self, doc, include_workspace):
        document_id = doc['id']
        chats = self.conversations(document_id)
        active = self.store.preference("active:" + document_id)
        if not chats:
            chat = self.store.create(document_id, uuid4().hex)
        else:
            chat = next((c for c in chats if c["id"] == active), chats[0])
        result = {"document": doc, **self.open_conversation(chat["id"]),
                  "recovery_draft": self.store.preference("document-draft:" + document_id)}
        if include_workspace:
            # One measured request supplies the visible screen. Task recovery
            # remains asynchronous so an old job cannot delay reading a document.
            result['workspace'] = dict(scroll=self.view_state('scroll:' + document_id),
                                       connections=self.connections(), context=self.context(chat['id']))
            self.view_state('selected_document', document_id)
        return result

    def snapshot(self, document_id):
        from ..document_metrics import visible_characters
        try:
            doc = documents.snapshot(document_id)
        except (OSError, ValueError, library.RevisionConflict):
            from ..library_recovery import preview
            doc = preview(document_id)
        return dict(doc, display_char_count=visible_characters(doc['text']))

    def recover_document(self, document_id, expected_hash, request_id):
        from ..library_recovery import recover
        return recover(document_id, expected_hash, request_id)

    def create_document(self, request_id, title="未命名文稿", markdown="", folder_id=None):
        identity(request_id)
        with library._transaction():
            key = "create-document:" + request_id
            previous = self.store.preference(key)
            request = [title, markdown, folder_id]
            if previous:
                if previous["request"] != request:
                    raise library.RevisionConflict("同一创建编号对应不同文稿")
                return previous["result"]
            # Stable explicit entry ID makes retry safe even if the preference write fails.
            eid = "doc_" + request_id
            existing = library._load_raw(eid)
            if existing is None:
                from ..models import Transcript
                library.save(Transcript(text=markdown, segments=[], language=None, engine="manual",
                                        source="手动文稿", meta={}), title, folder_id, entry_id=eid)
            elif existing.get("text") != markdown or existing.get("title") != title:
                raise library.RevisionConflict("创建结果已有后续编辑，请按资料编号查看")
            result = {"document_id": eid}
            self.store.preference(key, {"request": request, "result": result})
            return result

    def save(self, document_id, markdown, title, expected_revision, expected_hash, request_id):
        from ..document_metrics import visible_characters
        result = documents.save(document_id, markdown, title, expected_revision, expected_hash, request_id)
        return dict(result, display_char_count=visible_characters(markdown))

    def receipt(self, request_id):
        return documents.receipt(request_id)

    def recover_draft(self, document_id, value=None):
        identity(document_id)
        if value is not None:
            if type(value) is not dict or set(value) - {"markdown", "title", "base_revision", "base_hash", "cleared"}:
                raise ValueError("恢复草稿格式无效")
            if len(encode(value).encode()) > 8 * 1024 * 1024:
                raise ValueError("恢复草稿超过容量，未截断")
        return self.store.preference("document-draft:" + document_id, value)

    def view_state(self, key, value=None):
        if key not in {"columns", "selected_document", "theme", "library", "audio"} and not key.startswith("scroll:"):
            raise ValueError("界面设置无效")
        if value is not None and len(encode(value)) > 4000:
            raise ValueError("界面状态过大")
        return self.store.preference("view:" + key, value)

    def conversations(self, document_id, archived=False):
        if type(archived) is not bool:
            raise ValueError('归档筛选无效')
        return [c for c in self.store.list(document_id) if bool(self.store.preference('archived:' + c['id'])) == archived]

    def delete_conversation(self, conversation_id):
        """Hard-delete one chat after stopping races and clearing restorable copies."""
        identity(conversation_id)
        if self.runtime is None:
            raise ValueError('请通过 Lulu 主程序彻底删除会话，确保后台记录一并清理')
        runner = self._runner()
        media = self._media()
        with runner.lock, media.lock, library._transaction():
            self.store.get(conversation_id)
            payloads = _conversation_task_payloads(conversation_id, runner)
            from .recovery import forget_conversation
            try:
                removed_backups = forget_conversation(conversation_id)
                _remove_conversation_task_payloads(payloads)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                raise ValueError('会话清理尚未完成，当前会话记录保留；请检查资料保护后重试') from exc
            try:
                result = self.store.delete_conversation(conversation_id)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                raise ValueError('会话备份与任务记录已整理，但当前会话文件写入失败；原会话仍可重试删除') from exc
            result['removed_backup_copies'] = removed_backups
            return result

    def new_conversation(self, document_id, request_id):
        documents.snapshot(document_id)
        return self.store.create(document_id, request_id)

    def open_conversation(self, conversation_id):
        chat = self.store.get(conversation_id)
        self.store.preference("active:" + chat["document_id"], chat["id"])
        return self.conversation_snapshot(conversation_id)

    def conversation_snapshot(self, conversation_id, before=None):
        chat=self.store.get(conversation_id)
        return {"conversation": chat, "messages": self.messages(chat["id"], before=before),
                "proposals": self.store.proposals(chat["id"])}

    def messages(self, conversation_id, before=None, limit=100):
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError('消息位置无效')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('消息数量无效')
        return self.store.messages(conversation_id, before=before, limit=limit)

    def save_draft(self, conversation_id, draft, expected_revision):
        return self.store.save_draft(conversation_id, draft, expected_revision)

    def proposal_plan(self, conversation_id, proposal_id, base_revision, base_hash, operations):
        with library._transaction():
            value = self._proposal_value(conversation_id, proposal_id, base_revision, base_hash, operations)
            try:
                old = self.store.proposal(proposal_id)
            except FileNotFoundError:
                return self.store.proposal(proposal_id, value)
            if old != value:
                raise library.RevisionConflict("该建议编号已有不同内容或已被处理")
            return old

    def _proposal_value(self, conversation_id, proposal_id, base_revision, base_hash, operations):
        """Validate a whole proposal before the caller atomically publishes it."""
        identity(proposal_id)
        chat = self.store.get(conversation_id)
        with library._transaction():
            doc = documents.snapshot(chat["document_id"])
            if doc["revision"] != base_revision or doc["content_hash"] != base_hash:
                raise library.RevisionConflict("文稿已有新修改，建议可保留但不能应用")
            if type(operations) is not list or not 1 <= len(operations) <= 100:
                raise ValueError("修改数量无效")
            ids = set()
            for op in operations:
                if type(op) is not dict or set(op) - {"id", "reference_id", "label_number", "action", "start", "end", "before_hash", "before_markdown", "markdown"}:
                    raise ValueError("修改字段无效")
                oid = identity(op.get("id"))
                if oid in ids:
                    raise ValueError("修改编号重复")
                ids.add(oid)
                if "before_markdown" in op:
                    before = op["before_markdown"]
                    if (not isinstance(before, str)
                            or (op.get("action") == "append" and before != "")
                            or (op.get("action") == "replace" and documents.digest(before) != op.get("before_hash"))):
                        raise ValueError("建议里的原文快照与版本记录不匹配")
            documents.apply_operations(doc["text"], operations)
            value = dict(id=proposal_id, conversation_id=conversation_id, document_id=doc["id"],
                         base_revision=base_revision, base_hash=base_hash, state="pending",
                         operations=[dict(op, state="pending") for op in operations])
            return value

    def proposal_apply(self, proposal_id, operation_ids, request_id):
        identity(request_id)
        with library._transaction():
            p = self.store.proposal(proposal_id)
            identity_key = "proposal-apply:" + request_id
            selected_ids = sorted(operation_ids) if isinstance(operation_ids, list) else []
            if not selected_ids or len(selected_ids) != len(set(selected_ids)):
                raise ValueError("请选择不重复的修改项")
            previous = self.store.preference(identity_key)
            if previous:
                if previous["proposal_id"] != proposal_id or previous["operation_ids"] != selected_ids:
                    raise library.RevisionConflict("应用编号对应不同操作")
                # Replay the frozen document request to finish a possibly interrupted commit.
                result = documents.save(**previous["save"])
                # A late response/retry must not roll back later per-fragment decisions.
                nxt = previous["next_proposal"]
                if p["base_revision"] < nxt["base_revision"] or (
                        p["base_revision"] == nxt["base_revision"] and any(
                            op["id"] in selected_ids and op["state"] == "pending" for op in p["operations"])):
                    p = self.store.proposal(proposal_id, nxt)
                return {"receipt": result, "proposal": p}
            if p["state"] not in {"pending", "partly_applied"}:
                raise library.RevisionConflict("该建议已处理")
            selected = [op for op in p["operations"] if op["id"] in selected_ids and op["state"] == "pending"]
            if len(selected) != len(selected_ids):
                raise ValueError("选中的修改不存在或已处理")
            doc = documents.snapshot(p["document_id"])
            if (doc["revision"], doc["content_hash"]) != (p["base_revision"], p["base_hash"]):
                raise library.RevisionConflict("正文已有新修改，旧建议未覆盖")
            updated = documents.apply_operations(doc["text"], selected)
            nxt = deepcopy(p)
            spans = [(len(doc["text"].encode()) if op['action']=='append' else op['start'],
                      len(doc["text"].encode()) if op['action']=='append' else op['end'],
                      len(op["markdown"].encode()) + (2 if op["action"] == "append" else 0)) for op in selected]
            for op in nxt["operations"]:
                if op["id"] in selected_ids:
                    op["state"] = "applied"
                elif op["state"] == "pending" and op["action"] == "replace":
                    delta = sum(size - (end-start) for start, end, size in spans if end <= op["start"])
                    op["start"] += delta; op["end"] += delta
            nxt["base_revision"] += int(updated != doc["text"])
            nxt["base_hash"] = documents.digest(updated)
            nxt["state"] = "partly_applied" if any(o["state"] == "pending" for o in nxt["operations"]) else "applied"
            save = dict(document_id=doc["id"], markdown=updated, title=doc["title"], expected_revision=doc["revision"],
                        expected_hash=doc["content_hash"], request_id=request_id)
            self.store.preference(identity_key, dict(proposal_id=proposal_id, operation_ids=selected_ids, save=save, next_proposal=nxt))
            result = documents.save(**save)
            self.store.proposal(proposal_id, nxt)
            return {"receipt": result, "proposal": nxt}

    def proposal_discard(self, proposal_id):
        with library._transaction():
            p = self.store.proposal(proposal_id)
            for op in p["operations"]:
                if op["state"] == "pending":
                    op["state"] = "discarded"
            p["state"] = "discarded"
            return self.store.proposal(proposal_id, p)

    def history(self, document_id, name=None):
        if name is not None:
            return {"text": library.read_version(document_id, name)}
        return library.list_versions(document_id)

    def restore(self, document_id, name, expected_revision, expected_hash, request_id):
        text = library.read_version(document_id, name)
        doc = documents.snapshot(document_id)
        return documents.save(document_id, text, doc["title"], expected_revision, expected_hash, request_id)

    def folders(self, kind="documents"):
        if kind == "voice":
            return library.list_folders("voice")
        if kind != "documents":
            raise ValueError("资料类型无效")
        return library.list_folders("transcript") + library.list_folders("rewrite")

    def reference(self, document_id, canonical, start, end, expected_revision, expected_hash):
        doc = documents.snapshot(document_id)
        if (doc["revision"], doc["content_hash"]) != (expected_revision, expected_hash):
            raise library.RevisionConflict("引用时正文已变化，请重新选择")
        a, b = canonical.encode("utf-8"), doc["text"].encode("utf-8")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(a):
            raise ValueError("引用范围无效")
        if len(a) > 8 * 1024 * 1024:
            raise ValueError("引用范围过大")
        a[:start].decode("utf-8"); a[end:].decode("utf-8")
        if a != b:
            # Align corresponding Markdown blocks, then only the two boundary
            # blocks. A whole-transcript diff can take seconds on repeated text.
            from .references import formatted_source_range
            mapped = formatted_source_range(canonical, doc["text"], start, end)
            if mapped is None:
                raise ValueError("这段包含尚未保存的格式变化，请编辑保存后重新引用")
            start, end = mapped
        original = b[start:end].decode("utf-8")
        b[:start].decode("utf-8"); b[end:].decode("utf-8")
        return dict(id=uuid4().hex, document_id=document_id, revision=doc["revision"],
                    content_hash=doc["content_hash"], start=start, end=end,
                    before_hash=documents.digest(original), markdown=original)

    def presets(self):
        from .presets import SYSTEM_PRESETS
        return {"system": SYSTEM_PRESETS, "custom": self.store.preference("custom-presets") or []}

    def save_preset(self, preset_id, title, text):
        identity(preset_id)
        if not preset_id.startswith("custom-") or not isinstance(title, str) or not isinstance(text, str):
            raise ValueError("只能编辑自定义预设")
        legacy=preset_id.startswith('custom-legacy-')
        if not 0 < len(title.strip()) <= (200 if legacy else 30) or not 0 < len(text.strip()) <= (1_000_000 if legacy else 4000):
            raise ValueError("请填写 30 字以内名称与 4,000 字以内提示词")
        with library._transaction():
            presets = self.presets()
            others = [p for p in presets["custom"] if p["id"] != preset_id]
            if (not legacy and sum(not p['id'].startswith('custom-legacy-') for p in others) >= 50) or any(p["title"] == title.strip() for p in presets["system"] + others):
                raise ValueError("预设已达到 50 个，或名称重复")
            old=next((p for p in presets['custom'] if p['id']==preset_id), {})
            others.append(dict(old, id=preset_id, title=title.strip(), text=text.strip(), hint=old.get('hint',"自定义预设"), icon="doc"))
            return self.store.preference("custom-presets", others)

    def delete_preset(self, preset_id):
        if not isinstance(preset_id, str) or not preset_id.startswith("custom-"):
            raise ValueError("系统预设不能删除")
        with library._transaction():
            return self.store.preference("custom-presets", [p for p in self.presets()["custom"] if p["id"] != preset_id])

    def audio(self, document_id):
        value = library.load_voice(identity(document_id))
        if value is None:
            raise FileNotFoundError("音频资料不存在")
        # Paths stay in Core/native Qt. The webpage receives no arbitrary file URL.
        return {k:v for k,v in value.items() if k != "audio_path"}

    def create_folder(self, name, kind="documents"):
        if kind not in {"documents", "voice"}:
            raise ValueError("资料类型无效")
        return {"id": library.create_folder("voice" if kind == "voice" else "transcript", name)}

    def rename_folder(self, folder_id, name):
        return library.rename_folder(folder_id, name)

    def delete_folder(self, folder_id, delete_materials=False):
        identity(folder_id)
        if type(delete_materials) is not bool:
            raise ValueError('请选择有效的删除方式')
        with library._transaction():
            if not delete_materials:
                library.delete_folder(folder_id)
                return {'deleted': folder_id, 'materials_preserved': True}
            folders = library._load_folders()
            if not any(f['id'] == folder_id for f in folders):
                raise FileNotFoundError(folder_id)
            # Read the complete membership before changing anything. A broken
            # record may belong to this folder, so do not silently omit it.
            members = []
            for path in sorted(library._dir().glob('*.json')):
                if path.name.startswith('.') or path.name == library.FOLDERS_FILE.name:
                    continue
                record = library._load_raw(path.stem)
                if record is None:
                    raise ValueError(f'素材记录 {path.stem} 无法读取，文件夹和素材均未删除')
                if record.get('folder_id') == folder_id:
                    members.append(record)
            removed, failed = [], []
            for record in members:
                eid = record['id']
                try:
                    self.delete(eid)
                    removed.append(eid)
                except (OSError, ValueError, RuntimeError) as exc:
                    failed.append({'id': eid, 'title': record.get('title') or eid, 'message': str(exc)})
            folder_deleted = False
            if not failed:
                try:
                    library.delete_folder(folder_id)
                    folder_deleted = True
                except (OSError, ValueError, RuntimeError) as exc:
                    failed.append({'id': folder_id, 'title': '文件夹', 'message': str(exc)})
            return {'deleted': folder_id if folder_deleted else None,
                    'materials_preserved': False, 'removed_ids': removed, 'failed': failed}

    def move(self, document_ids, folder_id):
        if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 500:
            raise ValueError("请选择有效数量的资料")
        results = []
        for eid in document_ids:
            try:
                library.set_entry_folder(identity(eid), folder_id)
                results.append({"id": eid, "ok": True})
            except (OSError, ValueError, RuntimeError) as exc:
                results.append({"id": eid, "ok": False, "message": str(exc)})
        return results

    def delete(self, document_id):
        with library._transaction():
            documents.recover(document_id)
            library.delete(document_id)
        return {"document_id": document_id, "deleted": True}

    def trash(self, token=None):
        return library.deleted_preview(token) if token is not None else library.list_deleted()

    def purge_deleted(self, token, confirmed=False):
        from ..trash_cleanup import purge
        try:
            return purge(token, confirmed=confirmed)
        except OSError as exc:
            raise ValueError("部分文件未能删除，剩余内容已保留，请重试") from exc

    def trash_policy(self, enabled=None):
        from ..trash_cleanup import policy
        return policy(enabled)

    def cleanup_trash(self):
        from ..trash_cleanup import cleanup
        return cleanup()

    def restore_deleted(self, token):
        with library._transaction():
            folders = {f['id'] for f in library._load_folders()}
            eid = library.restore_deleted(token)
            record = library._load_raw(eid)
            if record and record.get('folder_id') and record['folder_id'] not in folders:
                library.set_entry_folder(eid, None)
            return {"document_id": eid}


def validate_request(operation, arguments):
    if operation not in OPERATIONS or type(arguments) is not dict or len(encode(arguments).encode()) > 10 * 1024 * 1024:
        raise ValueError("工作区请求无效")
    inspect.signature(getattr(WorkspaceService, operation)).bind(None, **arguments)


def handle(operation: str, arguments: dict, *, runtime=None, origin="gui") -> object:
    validate_request(operation, arguments)
    return getattr(WorkspaceService(runtime=runtime, origin=origin), operation)(**arguments)

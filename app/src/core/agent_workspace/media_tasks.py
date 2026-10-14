"""Explicit local-media tasks. Plans are immutable; the shared ledger owns state.

Only a human file picker or an explicit CLI request supplies a local path. The
model tool schema has no path/start capability. Existing inference is reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from uuid import uuid4

from .. import library
from ..job_ledger.contracts import JobState
from ..transcription_contracts import LocalFileTranscriptionRequestV1
from ..local_transcription_models import normalize_engine
from ..engines.local_models import LOCAL_MODELS
from . import documents
from .store import WorkspaceStore, encode, identity, workspace_root

KIND = 'workspace_transcription'
ACTIVE = {JobState.PREPARED, JobState.QUEUED, JobState.RUNNING, JobState.CANCEL_REQUESTED}
MAX_BYTES = 2 * 1024 ** 3
EXTENSIONS = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.aiff', '.mp4', '.mov', '.mkv', '.webm'}


class MediaTasks:
    def __init__(self, control, application, now):
        self.control, self.application, self.now = control, application, now
        self.lock = threading.RLock()
        self.workers = {}
        # A Core restart never replays inference or silently imports a result.
        for job in control.list():
            if job.job_kind != KIND or job.state not in ACTIVE:
                continue
            state = JobState.FAILED if job.state in {JobState.PREPARED, JobState.QUEUED} else JobState.RECOVERY_REQUIRED
            control._store.transition(job.job_id, expected_revision=job.job_revision, new_state=state,
                changed_at=now(), lease_token=(control._store.current_lease_token(job.job_id)
                    if job.attempts[-1].worker_instance_id else None),
                reason_code='WORKSPACE_MEDIA_INTERRUPTED')

    @staticmethod
    def path(plan_id):
        identity(plan_id)
        if not plan_id.startswith('media-'):
            raise ValueError('转录计划编号无效')
        root = workspace_root() / 'media'
        if root.is_symlink():
            raise ValueError('任务目录不安全，未读取文件')
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / (plan_id + '.json')
        if path.is_symlink():
            raise ValueError('任务文件不安全')
        return path

    def prepare(self, conversation_id, request_id, source_path, engine):
        self.application.assert_workspace_allowed('WS-24')
        identity(request_id)
        if len(request_id) > 100 or type(source_path) is not str:
            raise ValueError('转录请求无效')
        if normalize_engine(engine) != engine:
            raise ValueError('请选择有效的本地转录模型编号')
        source = Path(source_path)
        if (not source.is_absolute() or '..' in source.parts or source.suffix.lower() not in EXTENSIONS
                or any(p.is_symlink() for p in [source, *source.parents])):
            raise ValueError('请选择普通本地音频或视频文件')
        if not 0 < source.stat().st_size <= MAX_BYTES:
            raise ValueError('会话内转录支持 2 GB 以内的非空音视频文件')
        with self.lock, library._transaction():
            chat = WorkspaceStore().get(conversation_id)
            doc = documents.snapshot(chat['document_id'])
            library_root = str(library._dir())
            path = self.path('media-' + request_id)
            if path.exists():
                old = json.loads(path.read_text())
                if (old['conversation_id'], old['source_path'], old['engine']) != (conversation_id, source_path, engine):
                    raise library.RevisionConflict('此计划编号已有不同输入，未覆盖')
                return self.summary(old)
        # Hashing and readiness checks never hold the library write lock.
        checked = self.application.prepare(source_path, engine=engine)
        if not 0 < checked['source_summary']['bytes'] <= MAX_BYTES:
            raise ValueError('原文件大小已变化，请重新选择')
        plan = dict(plan_id='media-' + request_id, conversation_id=conversation_id, document_id=doc['id'],
                    library_root=library_root, source_path=source_path, filename=source.name,
                    engine=engine, source=checked['source_summary'], title=source.stem,
                    folder_id=doc.get('folder_id'), created_at=self.now())
        with self.lock, library._transaction():
            self._same_library(plan)
            if path.exists():
                old = json.loads(path.read_text())
                if encode(old) != encode(plan):
                    raise library.RevisionConflict('计划已由另一个请求创建，请重新读取')
            else:
                library._write_atomic_text(path, encode(plan))
        return self.summary(plan)

    @staticmethod
    def summary(plan):
        return {k: plan[k] for k in ('plan_id', 'conversation_id', 'filename', 'engine')} | dict(
            plan_hash=documents.digest(encode(plan)), bytes=plan['source']['bytes'], state='ready',
            message=f"使用本地 {LOCAL_MODELS[plan['engine']].display_name} 转录，完成后另存一篇新文稿。")

    @staticmethod
    def _same_library(plan):
        if str(library._dir()) != plan['library_root']:
            raise library.RevisionConflict('资料目录已切换，请回到原资料目录查看任务，未向新目录写入')

    def _plan(self, plan_id, expected_hash=None):
        plan = json.loads(self.path(plan_id).read_text())
        if expected_hash is not None and documents.digest(encode(plan)) != expected_hash:
            raise library.RevisionConflict('转录计划已有变化，请重新选择文件')
        self._same_library(plan)
        WorkspaceStore().get(plan['conversation_id'])
        return plan

    def start(self, plan_id, expected_hash, *, origin='gui'):
        self.application.assert_workspace_allowed('WS-25')
        if origin not in {'gui', 'cli'}:
            raise ValueError('任务来源无效')
        with self.lock, library._transaction():
            plan = self._plan(plan_id, expected_hash)
            jid = 'workspace-' + plan_id
            old = next((j for j in self.control.list() if j.job_id == jid), None)
            if old:
                if old.intent_payload_digest != expected_hash:
                    raise library.RevisionConflict('任务已绑定不同计划')
                return self.show(jid)
            # The plan is immutable, and this exact snapshot becomes the ledger
            # policy. A model never supplies engine/configuration overrides.
            if normalize_engine(plan['engine']) != plan['engine']:
                raise ValueError('转录模型无效，未替换模型')
            if any(j.state in ACTIVE and j.job_kind not in {'workspace_conversation', 'live_recording'} for j in self.control.list()):
                raise RuntimeError('已有本地任务正在处理，请等待或停止后再开始')
            if self.application._transcription_service is None or self.application._result_store is None:
                raise RuntimeError('本地转录服务尚未就绪，请重新打开 Lulu')
            job = self.control._store.create_job(job_id=jid, attempt_id='media-attempt-' + uuid4().hex,
                lineage_id='media-lineage-' + uuid4().hex, job_kind=KIND,
                input_ref='source:sha256:' + plan['source']['sha256'], frozen_policy=plan,
                created_by='agent_cli' if origin == 'cli' else 'gui',
                installation_id=self.application.verified_installation_id(), canonical_operation_id='OP_WORKSPACE_MEDIA_V1',
                idempotency_key=jid, intent_payload_digest=expected_hash, created_at=self.now(),
                effect_fingerprint='effect:local-transcription-document', account_ref='account:none',
                target_ref='target:local-workspace', uncertain_response_state='none')
            try:
                WorkspaceStore().append(plan['conversation_id'], 'status', f"{plan['filename']} · 本地 {LOCAL_MODELS[plan['engine']].display_name} 转录",
                    {'media_job_id': jid}, message_id=jid + '-started')
                queued = self.control._store.transition(jid, expected_revision=job.job_revision, new_state=JobState.QUEUED, changed_at=self.now())
                running, lease = self.control.claim_worker(jid, expected_revision=queued.job_revision,
                    worker_instance_id='core-media:' + jid, operation_receipt=None, changed_at=self.now())
                worker = threading.Thread(target=self.run, args=(jid, lease, plan), daemon=True, name='lulu-workspace-transcription')
                self.workers[jid] = worker
                worker.start()
            except Exception:
                self.workers.pop(jid, None)
                current = self.control.show(jid)
                self.control._store.transition(jid, expected_revision=current.job_revision,
                    lease_token=(self.control._store.current_lease_token(jid) if current.attempts[-1].worker_instance_id else None),
                    new_state=JobState.FAILED, changed_at=self.now(), reason_code='WORKSPACE_MEDIA_START_FAILED')
                raise
            return self.show(jid)

    def _job(self, jid, *, check_library=True):
        identity(jid)
        job = self.control.show(jid)
        if job.job_kind != KIND:
            raise ValueError('此编号不属于文稿转录任务')
        if check_library:
            self._same_library(job.frozen_policy)
        return job

    def show(self, jid, *, check_library=True):
        job = self._job(jid, check_library=check_library)
        events = self.control.wait(jid, after_sequence=0)
        values = [int(e.event_type.rsplit('.', 1)[-1]) for e in events if e.event_type.startswith('media.progress.')]
        return dict(job_id=jid, conversation_id=job.frozen_policy['conversation_id'], state=job.state.value,
                    filename=job.frozen_policy['filename'], engine=job.frozen_policy['engine'],
                    progress=100 if job.state is JobState.SUCCEEDED else max(values, default=0),
                    document_id='transcript_' + jid if job.state is JobState.SUCCEEDED else None,
                    recoverable=job.state is JobState.RECOVERY_REQUIRED and self.application._result_store.has_result(jid))

    def list(self, conversation_id):
        WorkspaceStore().get(conversation_id)
        jobs = sorted(self.control.list(), key=lambda j: (j.created_at, j.job_id))
        return [self.show(j.job_id) for j in jobs if j.job_kind == KIND
                and j.frozen_policy.get('conversation_id') == conversation_id]

    def cancel(self, jid):
        with self.lock:
            # Stopping computation remains possible after a library switch.
            # Cancellation writes no document or conversation in either root.
            job = self._job(jid, check_library=False)
            self.control.cancel(jid, expected_revision=job.job_revision, changed_at=self.now())
            return self.show(jid, check_library=False)

    def _copy_source(self, plan, destination, cancelled):
        path = Path(plan['source_path'])
        if any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError('原文件路径已变化，请重新选择')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != plan['source']['bytes']:
                raise ValueError('原文件已变化，请重新选择')
            digest, size = hashlib.sha256(), 0
            with destination.open('xb') as output:
                os.chmod(destination, 0o600)
                while not cancelled():
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('文件超过会话内转录上限')
                    digest.update(chunk)
                    output.write(chunk)
            if not cancelled() and (size != plan['source']['bytes'] or digest.hexdigest() != plan['source']['sha256']):
                raise ValueError('原文件内容已变化，请重新选择；未启动转录')
        finally:
            os.close(fd)

    def run(self, jid, lease, plan):
        def cancelled():
            return self.control.show(jid).state is not JobState.RUNNING
        bucket = [-1]
        def progress(_message, fraction):
            if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
                return
            value = min(90, int(fraction * 10) * 10)
            with self.lock:
                job = self.control.show(jid)
                if job.state is JobState.RUNNING and value > bucket[0]:
                    self.control.append_worker_event(jid, expected_revision=job.job_revision, lease_token=lease,
                        event_type=f'media.progress.{value}', changed_at=self.now())
                    bucket[0] = value
        try:
            with tempfile.TemporaryDirectory(prefix='lulu-workspace-media-') as folder:
                source = Path(folder) / ('input' + Path(plan['filename']).suffix)
                self._copy_source(plan, source, cancelled)
                if cancelled():
                    raise RuntimeError('已停止')
                result = self.application._transcription_service.transcribe(LocalFileTranscriptionRequestV1(str(source), engine_name=plan['engine']),
                    progress=progress, is_cancelled=cancelled)
                with self.lock:
                    if cancelled():
                        raise RuntimeError('已停止')
                    self.control.verify_worker_lease(jid, lease_token=lease, mutation_kind='result')
                    stored = dict(self.application._result_store.write(jid, result))
                    with library._transaction():
                        self._same_library(plan)
                        job = self._job(jid)
                        self._publish(job, lease, stored)
        except Exception as exc:
            with self.lock:
                job = self.control.show(jid)
                if job.state is JobState.CANCEL_REQUESTED:
                    self.control.settle_cancelled(jid, expected_revision=job.job_revision, lease_token=lease, changed_at=self.now())
                elif job.state is JobState.RUNNING:
                    # Any saved inference result is recoverable without inference.
                    saved = self.application._result_store.has_result(jid)
                    self.control._store.transition(jid, expected_revision=job.job_revision, lease_token=lease,
                        new_state=JobState.RECOVERY_REQUIRED if saved else JobState.FAILED,
                        changed_at=self.now(), reason_code='WORKSPACE_MEDIA_RESULT_PENDING' if saved else 'WORKSPACE_MEDIA_FAILED')
                # Best effort notification; the ledger remains authoritative if
                # a database/directory change prevents appending to the old chat.
                try:
                    self._same_library(plan)
                    if self.control.show(jid).state is JobState.SUCCEEDED:
                        return  # Only the notification failed; successful result stays successful.
                    message = str(exc) if isinstance(exc, ValueError) else '转录未完成，已保存的结果与任务记录保留。'
                    WorkspaceStore().append(plan['conversation_id'], 'status', message,
                        {'media_job_id': jid}, message_id=jid + '-stopped')
                except Exception:
                    pass
        finally:
            with self.lock:
                self.workers.pop(jid, None)

    def _publish(self, job, lease, stored):
        plan, jid = job.frozen_policy, job.job_id
        raw = self.application.read_result_bytes(jid, stored['result_ref'])
        if hashlib.sha256(raw).hexdigest() != stored['content_hash']:
            raise ValueError('转录结果校验失败，未导入文稿')
        value = json.loads(raw)['transcript']
        if (value.get('engine') != plan['engine'] or type(value.get('text')) is not str
                or not value['text'].strip() or type(value.get('segments')) is not list):
            raise ValueError('转录结果内容无效，未导入文稿')
        from ..models import Transcript, Segment
        result = Transcript(text=value['text'], segments=[Segment(**s) for s in value['segments']],
            language=value['language'], engine=value['engine'], source=plan['source_path'],
            meta={'workspace_job_id': jid, 'conversation_id': plan['conversation_id'], 'source_document_id': plan['document_id']})
        eid = 'transcript_' + jid
        library.save(result, plan['title'], plan['folder_id'], entry_id=eid)
        current = self._job(jid)
        self.control.complete_worker(jid, expected_revision=current.job_revision, lease_token=lease,
            new_state=JobState.SUCCEEDED, result_ref=stored['result_ref'], changed_at=self.now())
        self._announce(jid)

    def _announce(self, jid):
        job = self._job(jid)
        if job.state is JobState.SUCCEEDED:
            WorkspaceStore().append(job.frozen_policy['conversation_id'], 'status', '转录完成，已存入文稿库。',
                {'media_job_id': jid, 'document_id': 'transcript_' + jid}, message_id=jid + '-completed')

    def recover(self, jid):
        """Explicitly finish local publication of a verified result; never infer."""
        with self.lock, library._transaction():
            job = self._job(jid)
            if job.state is JobState.SUCCEEDED:
                self._announce(jid)
                return self.show(jid)
            if job.state is not JobState.RECOVERY_REQUIRED:
                raise ValueError('当前任务没有等待恢复的转录结果')
            stored = self.application._result_store.pending_receipt(jid)
            # Verify before changing state; damaged results remain recoverable.
            self.application.read_result_bytes(jid, stored['result_ref'])
            queued = self.control._store.transition(jid, expected_revision=job.job_revision, new_state=JobState.QUEUED, changed_at=self.now())
            running, lease = self.control.claim_worker(jid, expected_revision=queued.job_revision,
                worker_instance_id='core-media-recover:' + jid, operation_receipt=None, changed_at=self.now())
            try:
                self._publish(running, lease, stored)
            except Exception:
                current = self._job(jid)
                if current.state is JobState.RUNNING:
                    self.control._store.transition(jid, expected_revision=current.job_revision, lease_token=lease,
                        new_state=JobState.RECOVERY_REQUIRED, changed_at=self.now(), reason_code='WORKSPACE_MEDIA_RESULT_PENDING')
                raise
            return self.show(jid)

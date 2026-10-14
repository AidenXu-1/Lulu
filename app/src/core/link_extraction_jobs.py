"""Measured-Core public asset jobs. No transcription models or GUI imports."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
from contextlib import nullcontext

from .job_ledger.contracts import JobState
from .job_ledger.service import JobControlService
from .link_assets import OUTPUTS, acquire
from .link_transcription_application import canonicalize_public_url
from .transcription_job_application import LocalTranscriptionJobError


def error(reason, code='INVALID_ARGUMENT'):
    return LocalTranscriptionJobError(code, reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def open_destination(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise error('EXTRACT_DESTINATION_INVALID')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise error('EXTRACT_DESTINATION_INVALID')
        return path, fd, (info.st_dev, info.st_ino)
    except BaseException:
        os.close(fd)
        raise


def _check_destination(target, identity):
    _path, fd, actual = open_destination(str(target))
    os.close(fd)
    if actual != identity:
        raise error('EXTRACT_DESTINATION_CHANGED', 'CONFLICT')


class PublicationUncertain(RuntimeError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__('EXTRACT_PUBLICATION_UNCERTAIN')


def publish(source, root, target, parent_fd, identity, name, *, cancelled=lambda:False, commit_lock=None, verify=lambda:None):
    source = Path(source)
    if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
        raise error('EXTRACT_OUTPUT_INVALID', 'INTEGRITY_ERROR')
    _check_destination(target, identity)
    suffix = source.suffix.lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,8}', suffix):
        raise error('EXTRACT_OUTPUT_INVALID', 'INTEGRITY_ERROR')
    name += suffix
    temporary = '.lulu-extract-' + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    checksum, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as out:
            incoming = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(incoming, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise error('EXTRACT_OUTPUT_INVALID', 'INTEGRITY_ERROR')
                while block := stream.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('提取已取消')
                    out.write(block)
                    checksum.update(block)
                    size += len(block)
            if not size:
                raise error('EXTRACT_OUTPUT_INVALID', 'INTEGRITY_ERROR')
            out.flush()
            os.fsync(out.fileno())
        receipt = dict(path=str(target / name), bytes=size, sha256=checksum.hexdigest())
        with commit_lock or nullcontext():
            if cancelled():
                raise InterruptedError('提取已取消')
            verify()
            _check_destination(target, identity)
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            try:
                os.fsync(parent_fd)
                _check_destination(target, identity)
            except Exception as exc:
                raise PublicationUncertain(receipt) from exc
    finally:
        os.unlink(temporary, dir_fd=parent_fd)
    return receipt


class LinkExtractionJobs:
    def __init__(self, *, application, job_store, result_store, now, id_factory, runner=None):
        self.application, self.store, self.results = application, job_store, result_store
        self.control = JobControlService(job_store, enforce_fencing=True)
        self.now, self.id_factory, self.runner = now, id_factory, runner or acquire
        self.lock = threading.RLock()
        self.workers = {}
        for job in self.control.list():
            if job.job_kind == 'link_extraction' and job.state is JobState.CANCEL_REQUESTED:
                self.control.settle_cancelled(job.job_id, expected_revision=job.job_revision,
                    changed_at=self.now(), lease_token=self.store.current_lease_token(job.job_id))

    def prepare(self, source, outputs, destination):
        installation = self.application.assert_link_extraction_allowed()
        source = canonicalize_public_url(source)
        from urllib.parse import urlsplit
        import ipaddress
        host = urlsplit(source).hostname
        try:
            if not ipaddress.ip_address(host).is_global:
                raise error('URL_INVALID')
        except ValueError:
            if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
                raise error('URL_INVALID')
        chosen = outputs.split(',')
        if not chosen or len(set(chosen)) != len(chosen) or set(chosen) - set(OUTPUTS):
            raise error('EXTRACT_OUTPUTS_INVALID')
        try:
            path, fd, identity = open_destination(destination)
        except OSError as exc:
            raise error('EXTRACT_DESTINATION_INVALID') from exc
        os.close(fd)
        plan = dict(schema_id='lulu.link-extraction-plan/v1', installation_id=installation,
            source_url=source, outputs=[x for x in OUTPUTS if x in chosen], destination=str(path),
            destination_identity=list(identity), effects=dict(network=True, writes_files=True,
                uploads_content=False, may_charge=False, runs_model=False))
        return {**plan, 'plan_digest': digest(plan)}

    def run(self, source, outputs, destination, plan_digest, request_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
            raise error('EXTRACT_REQUEST_ID_INVALID')
        plan = self.prepare(source, outputs, destination)
        if plan_digest != plan['plan_digest']:
            raise error('EXTRACT_PLAN_CHANGED', 'CONFLICT')
        with self.lock:
            for job in self.control.list():
                if job.job_kind == 'link_extraction' and job.frozen_policy.get('request_id') == request_id:
                    if job.frozen_policy.get('plan_digest') != plan_digest:
                        raise error('EXTRACT_REQUEST_CHANGED', 'CONFLICT')
                    return dict(job_id=job.job_id, state=job.state.value, reused=True)
            target, fd, identity = open_destination(destination)
            try:
                if list(identity) != plan['destination_identity']:
                    raise error('EXTRACT_DESTINATION_CHANGED', 'CONFLICT')
                jid = self.id_factory('extract-job')
                created = self.store.create_job(job_id=jid, attempt_id=self.id_factory('extract-attempt'),
                    lineage_id=self.id_factory('extract-lineage'), job_kind='link_extraction',
                    input_ref='url:sha256:' + digest(plan['source_url']), frozen_policy={**plan, 'request_id':request_id},
                    created_by='agent_cli', installation_id=plan['installation_id'],
                    canonical_operation_id='OP_EXTRACT_PUBLIC_LINK_V1', idempotency_key='extract:' + digest(request_id),
                    intent_payload_digest=plan_digest, created_at=self.now(), effect_fingerprint='effect:public-download',
                    account_ref='account:none', target_ref='target:sha256:' + digest(str(target)), uncertain_response_state='none')
                queued = self.store.transition(jid, expected_revision=created.job_revision,
                    new_state=JobState.QUEUED, changed_at=self.now())
                running, lease = self.control.claim_worker(jid, expected_revision=queued.job_revision,
                    worker_instance_id='core-extract:' + jid, changed_at=self.now(), operation_receipt=None)
                worker = threading.Thread(target=self._run, args=(jid, lease, plan, target, fd, identity),
                    name='lulu-link-extraction', daemon=True)
                self.workers[jid] = worker
                try:
                    worker.start()
                except Exception:
                    self.workers.pop(jid, None)
                    self.control.complete_worker(jid, expected_revision=running.job_revision, lease_token=lease,
                        new_state=JobState.FAILED, result_ref=None, changed_at=self.now())
                    raise error('EXTRACT_WORKER_START_FAILED', 'RUNTIME_NOT_READY')
                fd = None
                return dict(job_id=jid, state='running', reused=False)
            finally:
                if fd is not None:
                    os.close(fd)

    def cancel(self, jid):
        with self.lock:
            job = self.control.show(jid)
            return self.control.cancel(jid, expected_revision=job.job_revision, changed_at=self.now())

    def _run(self, jid, lease, plan, target, fd, identity):
        items = {}
        work = None
        def cancelled():
            return self.control.show(jid).state is not JobState.RUNNING
        def progress(kind):
            with self.lock:
                job = self.control.show(jid)
                if job.state is JobState.RUNNING and kind in OUTPUTS:
                    self.control.append_worker_event(jid, expected_revision=job.job_revision, lease_token=lease,
                        event_type='extract.' + kind, changed_at=self.now())
        try:
            with tempfile.TemporaryDirectory(prefix='lulu-extract-') as directory:
                root = Path(directory)
                files, failures = self.runner(plan['source_url'], plan['outputs'], root, progress, cancelled, self.now())
                for kind in plan['outputs']:
                    if cancelled():
                        break
                    if kind in failures:
                        items[kind] = dict(state='failed', **failures[kind])
                        continue
                    try:
                        data = json.loads(Path(files[kind]).read_text(encoding='utf-8')) if kind == 'data' else None
                        receipt = publish(files[kind], root, target, fd, identity, jid + '-' + kind,
                            cancelled=cancelled, commit_lock=self.lock,
                            verify=lambda:self.control.verify_worker_lease(jid, lease_token=lease, mutation_kind='result'))
                        items[kind] = dict(state='succeeded', **receipt)
                        if kind == 'data':
                            work = data
                    except PublicationUncertain as exc:
                        items[kind] = dict(state='unknown', code='EXTRACT_PUBLICATION_UNCERTAIN', **exc.receipt)
                        break
                    except Exception as exc:
                        if cancelled():
                            break
                        items[kind] = dict(state='failed', code=getattr(exc, 'reason_code', 'EXTRACT_SAVE_FAILED'),
                            message='素材未能保存到所选目录，已有文件保持不变。')
        except Exception:
            for kind in plan['outputs']:
                items.setdefault(kind, dict(state='failed', code='EXTRACT_FAILED', message='作品提取未完成，请检查任务状态。'))
        finally:
            try:
                with self.lock:
                    job = self.control.show(jid)
                    if job.state not in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                        return
                    is_cancelled = job.state is JobState.CANCEL_REQUESTED
                    uncertain = any(item['state'] == 'unknown' for item in items.values())
                    for kind in plan['outputs']:
                        items.setdefault(kind, dict(state='cancelled' if is_cancelled else 'not_completed'))
                    count = sum(item['state'] == 'succeeded' for item in items.values())
                    state = (JobState.RECOVERY_REQUIRED if uncertain else JobState.CANCELLED if is_cancelled else JobState.SUCCEEDED if count == len(plan['outputs'])
                             else JobState.PARTIAL if count else JobState.FAILED)
                    stored = self.results.write_extraction(jid, dict(source_url=plan['source_url'],
                        outputs=plan['outputs'], state=state.value, items=items, work_data=work))
                    self.store.transition(jid, expected_revision=job.job_revision, new_state=state,
                        result_ref=stored['result_ref'], changed_at=self.now(), lease_token=lease,
                        partial_failures=tuple(dict(item_ref=kind, code=item.get('code', 'EXTRACT_FAILED'),
                            retry_hint=item.get('message', '提取未完成')) for kind, item in items.items() if item['state'] == 'failed'))
            except Exception:
                # A receipt/ledger write uncertainty must not invite a replay.
                with self.lock:
                    current = self.control.show(jid)
                    if current.state in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                        self.store.transition(jid, expected_revision=current.job_revision, new_state=JobState.RECOVERY_REQUIRED,
                            changed_at=self.now(), lease_token=lease, reason_code='EXTRACT_RESULT_UNCERTAIN')
            finally:
                os.close(fd)
                with self.lock:
                    self.workers.pop(jid, None)

    def receipt(self, jid, result_ref):
        result = json.loads(self.results.read_verified_bytes(jid, result_ref, schema_id='lulu.extraction-result/v1'))['extraction']
        result.pop('work_data', None)  # Full text is served by the bounded reader.
        return result

    def result(self, jid, result_ref, offset):
        raw = self.results.read_verified_bytes(jid, result_ref, schema_id='lulu.extraction-result/v1')
        if type(offset) is not int or not 0 <= offset < len(raw):
            raise error('EXTRACT_OFFSET_INVALID')
        chunk = raw[offset:offset + 32768]
        return dict(job_id=jid, result_ref=result_ref, data=base64.b64encode(chunk).decode('ascii'),
            offset=offset, total_bytes=len(raw))

    def wait_for_worker(self, jid, timeout):
        with self.lock:
            worker = self.workers.get(jid)
        if worker:
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError('link extraction worker has not settled')

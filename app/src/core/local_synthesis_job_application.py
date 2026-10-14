"""Core adapter for the four offline voice choices in Lulu.

The ledger owns task identity and recovery; the existing pipeline owns inference.
No task is replayed after an uncertain response or a lost worker.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import wave

from .synthesis import cli_options

from .job_ledger.contracts import JobState, TERMINAL_STATES
from .job_ledger.service import JobControlService
from .transcription_job_application import LocalTranscriptionJobError
from .transcription_result_store import TranscriptionResultStoreError

ENGINE = 'local_voxcpm2'


def _error(code: str, reason: str):
    return LocalTranscriptionJobError(code, reason)


def _plain_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in [path, *path.parents]):
        raise _error('INVALID_ARGUMENT', 'TTS_PATH_INVALID')
    return path


def _read_input(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > limit:
                raise _error('INVALID_ARGUMENT', 'TTS_INPUT_INVALID')
            raw = stream.read(limit + 1)
            if not raw or len(raw) > limit:
                raise _error('INVALID_ARGUMENT', 'TTS_INPUT_INVALID')
            return raw
    except OSError as exc:
        raise _error('INVALID_ARGUMENT', 'TTS_INPUT_UNAVAILABLE') from exc


def _open_destination(path: Path) -> int:
    """Walk directory descriptors so validation cannot redirect through a symlink."""
    if path.suffix.lower() != '.wav':
        raise _error('INVALID_ARGUMENT', 'TTS_WAV_DESTINATION_REQUIRED')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise _error('INVALID_ARGUMENT', 'TTS_DESTINATION_UNSAFE')
        try:
            os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return fd
        raise _error('CONFLICT', 'TTS_DESTINATION_EXISTS')
    except BaseException:
        os.close(fd)
        raise


def _publish_wav(source: Path, destination: Path, parent_fd: int) -> dict[str, object]:
    raw = _read_input(source, 128 * 1024 * 1024)
    try:
        with wave.open(str(source), 'rb') as reader:
            frames, rate, channels = reader.getnframes(), reader.getframerate(), reader.getnchannels()
            if frames <= 0 or rate <= 0 or channels not in {1, 2} or reader.getcomptype() != 'NONE':
                raise ValueError('invalid audio')
            if len(reader.readframes(frames)) != frames * channels * reader.getsampwidth():
                raise ValueError('truncated audio')
    except (wave.Error, EOFError, ValueError) as exc:
        raise _error('INTEGRITY_ERROR', 'TTS_OUTPUT_WAV_INVALID') from exc
    # A renamed parent must not make the receipt name a different directory.
    current = destination.parent.stat()
    pinned = os.fstat(parent_fd)
    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise _error('CONFLICT', 'TTS_DESTINATION_CHANGED')
    import secrets
    temporary = '.lulu-tts-' + secrets.token_hex(16) + '.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # link is create-new, atomic, and fails even if a dangling symlink exists.
        os.link(temporary, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.unlink(temporary, dir_fd=parent_fd)
    os.fsync(parent_fd)
    return dict(path=str(destination), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
        format='wav', sample_rate=rate, channels=channels, frames=frames, duration_seconds=frames / rate)


class LocalSynthesisJobApplication:
    def __init__(self, *, application, job_store, result_store, now, id_factory,
                 readiness=None, voices=None, pipeline=None, resource_busy=None):
        self.application = application
        self.store = job_store
        self.control = JobControlService(job_store, enforce_fencing=True)
        self.results = result_store
        self.now, self.id_factory = now, id_factory
        self._readiness_override = readiness
        self._voices = voices or self._saved_voices
        self._pipeline = pipeline
        from .synthesis.resource_lock import voxcpm2_busy
        self._resource_busy = resource_busy or voxcpm2_busy
        self._lock = threading.RLock()
        self._workers: dict[str, threading.Thread] = {}
        # Core startup has already fenced RUNNING tasks. A pending cancellation
        # also has no worker after startup and must be settled, never replayed.
        for job in self.control.list():
            if job.job_kind == 'local_synthesis' and job.state is JobState.CANCEL_REQUESTED:
                self.control.settle_cancelled(job.job_id, expected_revision=job.job_revision,
                    changed_at=self.now(), lease_token=self.store.current_lease_token(job.job_id))

    def _runtime_state(self, engine, options):
        return self._readiness_override(engine, options) if self._readiness_override else cli_options.readiness(engine, options)

    @staticmethod
    def _saved_voices():
        from ..config import AppConfig
        from .synthesis.voices import configure_voice_library_dir, list_voices
        configure_voice_library_dir(AppConfig.load().voice_library_dir)
        return list_voices()

    def status(self, *, engine=ENGINE) -> dict[str, object]:
        self.application.assert_local_synthesis_allowed('TTS-01')
        if engine not in cli_options.MODELS:
            raise _error('INVALID_ARGUMENT', 'TTS_ENGINE_INVALID')
        try:
            models = cli_options.catalog()
            for row in models:
                key = 'emotion_mode' if row['model_version'] else 'mode'
                row['mode_readiness'] = {}
                for mode in row['modes']:
                    opts = {key: mode}
                    if mode == 'design': opts['instruction'] = 'probe'
                    if mode == 'emotext': opts['emotion_text'] = 'probe'
                    if mode == 'emoaudio': opts['emotion_audio_path'] = '/probe.wav'
                    if mode == 'vector': opts['emotion_vector'] = [0.] * 8
                    row['mode_readiness'][mode] = self._runtime_state(row['engine'], cli_options.validate(row['engine'], opts))
                row['runtime_state'] = next(iter(row['mode_readiness'].values()))
            selected = next(row for row in models if row['engine'] == engine)
            voices = [dict(voice_id=v.id, name=v.name,
                reference_available=Path(v.reference_audio_path).is_file()) for v in self._voices()]
            return dict(engine=engine, mode='offline', runtime_state=selected['runtime_state'], models=models, voices=voices, resource_busy=self._resource_busy(),
                active_job_ids=[j.job_id for j in self.control.list()
                    if j.job_kind == 'local_synthesis' and j.state not in TERMINAL_STATES])
        except (OSError, ValueError) as exc:
            raise _error('RUNTIME_NOT_READY', 'TTS_STATUS_UNAVAILABLE') from exc

    def synthesize(self, *, text_file: str, destination: str, voice_id: str = '', engine: str = ENGINE, options_file: str | None = None) -> dict[str, object]:
        installation_id = self.application.assert_local_synthesis_allowed('TTS-02')
        try:
            raw_options = json.loads(_read_input(_plain_path(options_file), 64 * 1024)) if options_file else {}
            options = cli_options.validate(engine, raw_options)
        except (ValueError, TypeError, KeyError) as exc:
            raise _error('INVALID_ARGUMENT', 'TTS_ENGINE_OPTIONS_INVALID') from exc
        if self._runtime_state(engine, options) != 'ready':
            raise _error('RUNTIME_NOT_READY', 'TTS_RUNTIME_NOT_READY')
        try:
            raw = _read_input(_plain_path(text_file), 64 * 1024)
            text = raw.decode('utf-8').strip()
            if not text or len(text) > 2000 or '\x00' in text:
                raise _error('INVALID_ARGUMENT', 'TTS_TEXT_INVALID')
            voice = next((v for v in self._voices() if v.id == voice_id), None)
            if voice is None and cli_options.needs_voice(engine, options):
                raise _error('INVALID_ARGUMENT', 'TTS_VOICE_NOT_FOUND')
            if voice and (not isinstance(voice.reference_text, str) or len(voice.reference_text) > 10000):
                raise _error('INVALID_ARGUMENT', 'TTS_VOICE_INVALID')
            reference = _read_input(_plain_path(voice.reference_audio_path), 32 * 1024 * 1024) if voice else b''
            emotion_reference = (_read_input(_plain_path(options['emotion_audio_path']), 32 * 1024 * 1024)
                                 if options.get('emotion_mode') == 'emoaudio' else None)
            if engine == 'local_voxcpm2':
                from .synthesis.voxcpm2_options import generation_inputs
                generation_inputs(text, voice, options)
            elif engine == 'local_qwen3_tts':
                from .synthesis.qwen3_options import generation_inputs
                generation_inputs(text, voice, options)
            target = _plain_path(destination)
            parent_fd = _open_destination(target)
        except (OSError, UnicodeError, ValueError) as exc:
            raise _error('INVALID_ARGUMENT', 'TTS_INPUT_OR_DESTINATION_INVALID') from exc
        try:
            with self._lock:
                if self._resource_busy() or any(j.job_kind != 'live_recording' and j.state in {JobState.RUNNING, JobState.QUEUED, JobState.CANCEL_REQUESTED}
                       for j in self.control.list()):
                    raise _error('RESOURCE_BUSY', 'TTS_COMPUTE_BUSY')
                policy = dict(engine=engine, mode='offline', engine_options=options, voice_id=voice_id, destination=str(target),
                    text_sha256=hashlib.sha256(raw).hexdigest(), reference_sha256=hashlib.sha256(reference).hexdigest(),
                    reference_text_sha256=hashlib.sha256((voice.reference_text if voice else '').encode()).hexdigest(),
                    emotion_reference_sha256=hashlib.sha256(emotion_reference or b'').hexdigest())
                digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
                jid = self.id_factory('tts-job')
                created = self.store.create_job(job_id=jid, attempt_id=self.id_factory('tts-attempt'),
                    lineage_id=self.id_factory('tts-lineage'), job_kind='local_synthesis',
                    input_ref='tts-source:sha256:'+policy['text_sha256'], frozen_policy=policy,
                    created_by='agent_cli', installation_id=installation_id,
                    canonical_operation_id='OP_TTS_LOCAL_V1', idempotency_key='local-synthesis:'+digest,
                    intent_payload_digest=digest, created_at=self.now(), effect_fingerprint='effect:local-audio-result',
                    account_ref='account:none', target_ref='target:sha256:'+hashlib.sha256(str(target).encode()).hexdigest(),
                    uncertain_response_state='none')
                if created.job_id != jid:
                    raise _error('CONFLICT', 'IDEMPOTENT_JOB_ALREADY_EXISTS')
                queued = self.store.transition(jid, expected_revision=created.job_revision,
                    new_state=JobState.QUEUED, changed_at=self.now())
                running, lease = self.control.claim_worker(jid, expected_revision=queued.job_revision,
                    worker_instance_id='core-tts:'+jid, changed_at=self.now(), operation_receipt=None)
                worker = threading.Thread(target=self._run, args=(jid, lease, text, voice, reference, target, parent_fd, engine, options, emotion_reference),
                    name='lulu-local-tts', daemon=True)
                self._workers[jid] = worker
                try:
                    worker.start()
                except Exception:
                    self.control.complete_worker(jid, expected_revision=running.job_revision, lease_token=lease,
                        new_state=JobState.FAILED, result_ref=None, changed_at=self.now())
                    self._workers.pop(jid, None)
                    raise _error('RUNTIME_NOT_READY', 'TTS_WORKER_START_FAILED')
                parent_fd = None  # owned by the worker now
                return dict(job_id=jid, state=running.state.value,
                    receipt_ref=f'lulu-job:{jid}:revision:{running.job_revision}')
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    def cancel(self, jid):
        with self._lock:
            current = self.control.show(jid)
            return self.control.cancel(jid, expected_revision=current.job_revision, changed_at=self.now())

    def _run(self, jid, lease, text, voice, reference, target, parent_fd, engine, options, emotion_reference):
        try:
            with tempfile.TemporaryDirectory(prefix='lulu-core-tts-') as directory:
                root = Path(directory)
                ref = root / ('reference' + (Path(voice.reference_audio_path).suffix if voice else '.wav'))
                ref.write_bytes(reference)
                ref.chmod(0o600)
                options = dict(options)
                if emotion_reference is not None:
                    emotion = root / ('emotion' + Path(options['emotion_audio_path']).suffix)
                    emotion.write_bytes(emotion_reference)
                    emotion.chmod(0o600)
                    options['emotion_audio_path'] = str(emotion)
                output = root / 'result.wav'
                def cancelled():
                    return self.control.show(jid).state is not JobState.RUNNING
                last_bucket = [-1]
                def progress(_message, fraction):
                    # Persist only bounded numeric milestones, never model text.
                    if not isinstance(fraction, (float, int)) or not 0 <= fraction <= 1:
                        return
                    bucket = int(fraction * 10)
                    with self._lock:
                        current = self.control.show(jid)
                        if current.state is JobState.RUNNING and bucket > last_bucket[0]:
                            self.control.append_worker_event(jid, expected_revision=current.job_revision,
                                lease_token=lease, event_type=f'tts.progress.{bucket * 10}', changed_at=self.now())
                            last_bucket[0] = bucket
                if not cancelled():
                    from .synthesis.pipeline import synthesize_text
                    runner = self._pipeline or synthesize_text
                    runner(text=text, voice_id=voice.id if voice else '', voice_profile=replace(voice, reference_audio_path=str(ref)) if voice else None,
                        engine_options=options, output_path=output, engine=cli_options.MODELS[engine][1], output_format='wav',
                        is_cancelled=cancelled, progress=progress)
                with self._lock:
                    current = self.control.show(jid)
                    if current.state is JobState.CANCEL_REQUESTED:
                        self.control.settle_cancelled(jid, expected_revision=current.job_revision,
                            lease_token=lease, changed_at=self.now())
                        return
                    self.control.verify_worker_lease(jid, lease_token=lease, mutation_kind='result')
                    audio = _publish_wav(output, target, parent_fd)
                    audio.update(engine=engine, model_version=cli_options.MODELS[engine][2], voice_id=voice.id if voice else '',
                                 mode=options.get('mode', options.get('emotion_mode')))
                    stored = self.results.write_audio(jid, audio)
                    self.control.complete_worker(jid, expected_revision=current.job_revision, lease_token=lease,
                        new_state=JobState.SUCCEEDED, result_ref=stored['result_ref'], changed_at=self.now())
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Local synthesis worker stopped: %s', type(exc).__name__)
            with self._lock:
                current = self.control.show(jid)
                if current.state is JobState.CANCEL_REQUESTED:
                    self.control.settle_cancelled(jid, expected_revision=current.job_revision,
                        lease_token=lease, changed_at=self.now())
                elif current.state is JobState.RUNNING:
                    self.store.transition(jid, expected_revision=current.job_revision, lease_token=lease,
                        new_state=JobState.FAILED, changed_at=self.now(), reason_code=('TTS_DESTINATION_EXISTS' if isinstance(exc, FileExistsError) else 'TTS_GENERATION_FAILED'))
        finally:
            os.close(parent_fd)
            with self._lock:
                self._workers.pop(jid, None)

    def audio_receipt(self, jid: str, result_ref: str) -> dict[str, object]:
        try:
            raw = self.results.read_verified_bytes(jid, result_ref, schema_id='lulu.audio-result/v1')
            return json.loads(raw)['audio']
        except (TranscriptionResultStoreError, ValueError, KeyError) as exc:
            raise _error('INTEGRITY_ERROR', 'TTS_RESULT_RECEIPT_INVALID') from exc

    def wait_for_worker(self, jid: str, timeout: float):
        """Bounded join for lifecycle verification; never used by CLI dispatch."""
        with self._lock:
            worker = self._workers.get(jid)
        if worker:
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError('TTS worker has not settled')

"""Thin CLI adapter over the Core local-transcription job application."""

from __future__ import annotations

from collections.abc import Callable
import base64
from typing import TYPE_CHECKING

from ..core.job_ledger.contracts import EventV1, JobState, JobV1
from ..core.job_ledger.service import JobControlService
from ..core.transcription_job_application import LocalTranscriptionJobApplication
from ..core.transcription_job_application import LocalTranscriptionJobError
from ..core.link_transcription_application import LinkTranscriptionApplicationService
from ..core.link_transcription_application import LinkTranscriptionError

if TYPE_CHECKING:
    from ..core.cli_runtime.link_host_bridge import LinkApprovalHostBroker


ACTIVATED_CAPABILITY_IDS = frozenset(
    {"SYS-01", "SYS-02", "SYS-03", "SYS-04",
     "TR-01", "TR-02", "TR-03", "TR-11", "TR-12", "JOB-01", "JOB-02", "JOB-03", "JOB-06"}
)


class LocalTranscriptionCliRuntime:
    def __init__(
        self,
        application: LocalTranscriptionJobApplication,
        *,
        job_control: JobControlService | None = None,
        now: Callable[[], str] | None = None,
        link_transcription_service: LinkTranscriptionApplicationService | None = None,
        link_host_broker: LinkApprovalHostBroker | None = None,
        id_factory: Callable[[str], str] | None = None,
        historical_jobs: list[dict[str, object]] | None = None,
        historical_context: dict[str, object] | None = None,
        local_synthesis_service=None,
        link_extraction_service=None,
    ) -> None:
        if not isinstance(application, LocalTranscriptionJobApplication):
            raise TypeError("application must be LocalTranscriptionJobApplication")
        from threading import RLock
        self._workspace_application_lock = RLock()
        self._local_synthesis_service = local_synthesis_service
        self._link_extraction_service = link_extraction_service
        self._application = application
        self._job_control = job_control
        self._now = now
        if link_transcription_service is not None and not isinstance(
            link_transcription_service, LinkTranscriptionApplicationService
        ):
            raise TypeError("link_transcription_service is invalid")
        self._link_transcription_service = link_transcription_service
        if link_host_broker is not None:
            from ..core.cli_runtime.link_host_bridge import LinkApprovalHostBroker

            if not isinstance(link_host_broker, LinkApprovalHostBroker):
                raise TypeError("link_host_broker is invalid")
        self._link_host_broker = link_host_broker
        self._id_factory = id_factory
        from ..core.agent_entry.task_handover import validate_records
        self._history = {row['job_id']: row for row in validate_records(historical_jobs or [])}
        from ..core.agent_entry.task_handover_context import validate_context
        self._historical_context = validate_context(historical_context,
            list(self._history.values())) if historical_context is not None else None

    @property
    def link_transcription_service(self) -> LinkTranscriptionApplicationService | None:
        return self._link_transcription_service

    @property
    def activated_capability_ids(self) -> frozenset[str]:
        if self._application.has_verified_trusted_test_runtime():
            activated = set(ACTIVATED_CAPABILITY_IDS)
            from .library_commands import CAPABILITIES
            activated.update(CAPABILITIES)
            from ..core.agent_workspace.contracts import CAPABILITIES as WORKSPACE_CAPABILITIES
            activated.update(WORKSPACE_CAPABILITIES)
            if self._local_synthesis_service is not None:
                activated.update({"TTS-01", "TTS-02"})
            if self._link_extraction_service is not None:
                from ..core.link_assets import CAPABILITIES
                activated.update(CAPABILITIES)
            if self._link_transcription_service is not None:
                activated.update(
                    self._link_transcription_service.activated_capability_ids
                )
            return frozenset(activated)
        return frozenset()

    def tts_status(self, **options):
        return self._synthesis().status(**options)

    def extraction(self):
        if self._link_extraction_service is None:
            raise LocalTranscriptionJobError('CAPABILITY_UNAVAILABLE', 'LINK_EXTRACTION_RUNTIME_UNAVAILABLE')
        return self._link_extraction_service

    def tts_synthesize(self, **options):
        return self._synthesis().synthesize(**options)

    def _synthesis(self):
        if self._local_synthesis_service is None:
            from ..core.transcription_job_application import LocalTranscriptionJobError
            raise LocalTranscriptionJobError("CAPABILITY_UNAVAILABLE", "FORMAL_CLI_NOT_IMPLEMENTED")
        return self._local_synthesis_service

    def prepare(self, source_path: str, engine: str = 'local_sensevoice_q8') -> dict[str, object]:
        return self._application.prepare(source_path, engine)

    def run(self, source_path: str, engine: str = 'local_sensevoice_q8') -> dict[str, object]:
        return self._application.run(source_path, engine)

    def transcribe_status(self, engine=None):
        return self._application.model_status(engine)

    def jobs_result(self, job_id: str, offset: int):
        # Share the existing bounded, ledger-bound GUI result reader.
        from ..core.transcription_result_store import TranscriptionResultStoreError
        try:
            control = self._authorized_job_control('JOB-06')
            if job_id in self._history and self._history[job_id]['job_kind'] == 'link_extraction':
                row = self._history[job_id]
                if not row.get('result_ref'):
                    raise LocalTranscriptionJobError('CONFLICT', 'EXTRACT_RESULT_NOT_READY')
                return self.extraction().result(job_id, row['result_ref'], offset)
            if job_id not in self._history:
                job = control.show(job_id)
                if job.job_kind == 'link_extraction':
                    if not job.result_ref:
                        raise LocalTranscriptionJobError('CONFLICT', 'EXTRACT_RESULT_NOT_READY')
                    return self.extraction().result(job_id, job.result_ref, offset)
            return self.gui_job_result(job_id, offset)
        except TranscriptionResultStoreError as exc:
            raise LocalTranscriptionJobError('INTEGRITY_ERROR', 'TRANSCRIPTION_RESULT_INVALID') from exc
        except RuntimeError as exc:
            if str(exc) in {'CORE_TASK_RESPONSE_INVALID', 'CORE_TASK_RESULT_UNAVAILABLE'}:
                raise LocalTranscriptionJobError('INVALID_ARGUMENT', str(exc)) from exc
            raise


    def prepare_link(self, source: str, **options: object) -> dict[str, object]:
        if self._link_transcription_service is None:
            raise LinkTranscriptionError(
                "CAPABILITY_UNAVAILABLE", "LINK_TRANSCRIPTION_RUNTIME_UNAVAILABLE"
            )
        options.pop("expected_idempotency_key", None)
        return self._link_transcription_service.prepare(source, **options)

    def run_link(self, source: str, **options: object) -> dict[str, object]:
        service = self._link_transcription_service
        broker = self._link_host_broker
        if service is None:
            raise LinkTranscriptionError(
                "CAPABILITY_UNAVAILABLE", "LINK_TRANSCRIPTION_RUNTIME_UNAVAILABLE"
            )
        if broker is None:
            raise LinkTranscriptionError(
                "HUMAN_ACTION_REQUIRED", "SAME_BUNDLE_HOST_BROKER_UNAVAILABLE"
            )
        expected_idempotency_key = options.pop("expected_idempotency_key", None)
        plan = service.prepare(source, **options)
        grant = broker.request_approval(
            plan=plan,
            expected_idempotency_key=expected_idempotency_key,
        )
        return service.run(
            source,
            approval_receipt=grant.receipt,
            now=grant.approved_at,
            expected_idempotency_key=grant.idempotency_key,
            **options,
        )

    def jobs_list(self) -> dict[str, object]:
        control = self._authorized_job_control("JOB-01")
        jobs = control.list()
        history = self._visible_history(jobs)
        return {"jobs": [self._job_summary(job) for job in jobs] +
                [self._history_summary(row) for row in history]}

    def jobs_show(self, job_id: str) -> dict[str, object]:
        control = self._authorized_job_control("JOB-01")
        if job_id in self._history:
            return self._history_summary(self._history[job_id])
        return self._job_summary(control.show(job_id))

    def jobs_wait(self, job_id: str, after_sequence: int) -> tuple[EventV1, ...]:
        if job_id in self._history:
            self._authorized_job_control("JOB-02")
            raise LocalTranscriptionJobError('CAPABILITY_UNAVAILABLE', 'HISTORY_EVENTS_NOT_IMPORTED')
        return self._authorized_job_control("JOB-02").wait(
            job_id,
            after_sequence=after_sequence,
        )

    def jobs_cancel(self, job_id: str) -> dict[str, object]:
        control = self._authorized_job_control("JOB-03")
        if job_id in self._history:
            raise LocalTranscriptionJobError('CAPABILITY_UNAVAILABLE', 'HISTORY_READ_ONLY')
        if not callable(self._now):
            raise RuntimeError("job cancellation clock is unavailable")
        current = control.show(job_id)
        if current.job_kind == 'live_recording':
            self.live_recording().cancel_job(job_id)
            updated = control.show(job_id)
            return {**self._job_summary(updated), 'cancellation':
                    'requested' if updated.state is JobState.CANCEL_REQUESTED else 'cancelled'}
        if current.job_kind == 'workspace_transcription':
            self.workspace_media().cancel(job_id)
            updated = control.show(job_id)
            return {**self._job_summary(updated), 'cancellation':
                    'requested' if updated.state is JobState.CANCEL_REQUESTED else 'cancelled'}
        updated = (self.extraction().cancel(job_id) if current.job_kind == 'link_extraction' else
            self._synthesis().cancel(job_id) if current.job_kind == "local_synthesis" else control.cancel(
            job_id, expected_revision=current.job_revision, changed_at=self._now(),
        ))
        summary = self._job_summary(updated)
        summary["cancellation"] = (
            "requested"
            if updated.state is JobState.CANCEL_REQUESTED
            else "cancelled"
        )
        return summary

    def jobs_receipt(self, job_id: str) -> dict[str, object]:
        control = self._authorized_job_control("JOB-06")
        if job_id in self._history:
            from copy import deepcopy
            receipt = deepcopy(self._history[job_id])
        else:
            job = control.show(job_id)
            receipt = {**self._job_summary(job), "partial_failures": list(job.partial_failures)}
        if receipt["job_kind"] == "local_synthesis" and receipt["state"] == "succeeded":
            receipt["audio"] = self._synthesis().audio_receipt(job_id, receipt["result_ref"])
        if receipt['job_kind'] == 'link_extraction' and receipt.get('result_ref'):
            receipt['extraction'] = self.extraction().receipt(job_id, receipt['result_ref'])
        return receipt

    def gui_jobs_list(self, include_view: bool = False) -> dict[str, object]:
        control = self._authorized_job_control("JOB-01")
        from ..core.agent_entry.task_handover import gui_history
        jobs = control.list()
        view = None
        if include_view:
            view_store = self._gui_view_store()
            view = view_store.load()
            for job in jobs:
                if job.created_by == "gui" and job.job_kind == "gui_transcription" and job.state is JobState.RUNNING and view_store.owner_has_exited(view["owners"].get(job.job_id)):
                    control.transition_gui_task(job.job_id, expected_revision=job.job_revision,
                        new_state=JobState.NEEDS_INPUT, changed_at=self._now())
                    view_store.set_owner(job.job_id, None)
            jobs = control.list()
        history = self._visible_history(jobs)
        context_tasks = self._historical_context['tasks'] if self._historical_context else {}
        # Workspace jobs have their own conversation-owned controls. Projecting
        # them as legacy file tasks would expose an unusable hash as a file path.
        separate_views = {'local_synthesis', 'link_extraction', 'text_processing', 'workspace_conversation', 'workspace_transcription', 'live_recording'}
        result = {"jobs": [self._gui_job_summary(job) for job in jobs if job.job_kind not in separate_views] +
                  [gui_history(row, context_tasks.get(row['job_id'])) for row in history if row['job_kind'] not in separate_views]}
        if view is not None:
            result["view"] = {key: view[key] for key in ("revision", "groups", "hidden_ids")}
        return result

    def _gui_view_store(self):
        from ..core.gui_task_view import GuiTaskViewStore
        if not hasattr(self, "_gui_view"):
            self._gui_view = GuiTaskViewStore(self._authorized_job_control("JOB-01")._store.data_root)
            if self._historical_context and self._gui_view.load()['revision'] == 0:
                self._gui_view.save_view(**self._historical_context['view'],
                    expected_revision=0, known_ids=set(self._history))
        return self._gui_view

    def gui_jobs_view(self, **arguments):
        control = self._authorized_job_control("JOB-03")
        jobs = control.list()
        hidden = set(arguments.get("hidden_ids", []))
        if any(job.job_id in hidden and job.state in {JobState.RUNNING, JobState.CANCEL_REQUESTED} for job in jobs):
            raise RuntimeError("GUI_VIEW_ACTIVE_TASK_CANNOT_HIDE")
        known_ids = {job.job_id for job in jobs} | {row["job_id"] for row in self._visible_history(jobs)}
        return self._gui_view_store().save_view(**arguments, known_ids=known_ids)

    def gui_job_result(self, job_id: str, offset: int) -> dict[str, object]:
        if type(offset) is not int or not 0 <= offset < 32 * 1024 * 1024:
            raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
        control = self._authorized_job_control("JOB-06")
        if job_id in self._history:
            row = self._history[job_id]
            state, result_ref = row['state'], row['result_ref']
        else:
            job = control.show(job_id)
            state, result_ref = job.state.value, job.result_ref
        if state not in {'succeeded', 'partial'} or not isinstance(result_ref, str):
            raise RuntimeError("CORE_TASK_RESULT_UNAVAILABLE")
        raw = self._application.read_result_bytes(job_id, result_ref)
        if offset >= len(raw):
            raise RuntimeError("CORE_TASK_RESPONSE_INVALID")
        return {"job_id": job_id, "result_ref": result_ref, "offset": offset,
                "total_bytes": len(raw), "data": base64.b64encode(raw[offset:offset + 32768]).decode("ascii")}

    def _visible_history(self, jobs):
        seen = set()
        for job in jobs:
            row = self._job_summary(job)
            jid = row['job_id']
            if jid in self._history:
                # Reinstalling a previously used build can encounter its own
                # unchanged terminal jobs. Accept only identical public records.
                old = self._history[jid]
                if row != self._history_summary(old) or list(job.partial_failures) != old['partial_failures']:
                    raise RuntimeError('PRODUCT_TASK_HANDOVER_CONFLICT')
                seen.add(jid)
        return [row for jid, row in self._history.items() if jid not in seen]

    @staticmethod
    def _history_summary(row):
        return {key: value for key, value in row.items() if key != 'partial_failures'}

    def gui_jobs_create(self, task: dict[str, object], request_id: str | None = None) -> dict[str, object]:
        control = self._authorized_job_control("JOB-01")
        if not callable(self._id_factory) or not callable(self._now):
            raise RuntimeError("authoritative GUI job creation is unavailable")
        if request_id is not None:
            import re
            if not isinstance(request_id, str) or re.fullmatch(r"gui-job-[a-f0-9]{32}", request_id) is None:
                raise RuntimeError("GUI_TASK_REQUEST_ID_INVALID")
        job = control.create_gui_task(
            task=task,
            job_id=request_id or self._id_factory("gui-job"),
            attempt_id=self._id_factory("gui-attempt"),
            lineage_id=self._id_factory("gui-lineage"),
            installation_id=self._application.verified_installation_id(),
            created_at=self._now(),
        )
        return self._gui_job_summary(job)

    def gui_jobs_transition(
        self,
        *,
        job_id: str,
        expected_revision: int,
        state: str,
        result_ref: str | None,
        owner_pid: int | None = None,
        error_message: str | None = None,
        execution_policy: dict | None = None,
    ) -> dict[str, object]:
        control = self._authorized_job_control("JOB-03")
        if not callable(self._now):
            raise RuntimeError("authoritative GUI job clock is unavailable")
        try:
            new_state = JobState(state)
        except ValueError as exc:
            raise RuntimeError("GUI_TASK_STATE_INVALID") from exc
        if new_state is JobState.RUNNING and owner_pid is not None:
            before = control.show(job_id)
            if before.state is not JobState.QUEUED or before.job_revision != expected_revision:
                raise RuntimeError("GUI_WORKER_CLAIM_CONFLICT")
            self._gui_view_store().set_owner(job_id, owner_pid)
        job = control.transition_gui_task(
            job_id,
            expected_revision=expected_revision,
            new_state=new_state,
            changed_at=self._now(),
            result_ref=result_ref,
            error_message=error_message,
            execution_policy=execution_policy,
        )
        return self._gui_job_summary(job)

    def _authorized_job_control(self, capability_id: str) -> JobControlService:
        self._application.assert_job_control_allowed(capability_id)
        if not isinstance(self._job_control, JobControlService):
            raise RuntimeError("authoritative job control is unavailable")
        return self._job_control

    def workspace_application(self):
        control = self._authorized_job_control("JOB-01")
        with self._workspace_application_lock:
            if not hasattr(self, "_workspace_application"):
                from ..core.agent_workspace.application import ConversationApplication
                self._workspace_application = ConversationApplication(control, self._application.verified_installation_id(), self._now,
                    task_reader=lambda cid: self.workspace_media().list(cid))
            return self._workspace_application

    def workspace_media(self):
        control = self._authorized_job_control('JOB-01')
        with self._workspace_application_lock:
            if not hasattr(self, '_workspace_media'):
                from ..core.agent_workspace.media_tasks import MediaTasks
                self._workspace_media = MediaTasks(control, self._application, self._now)
            return self._workspace_media

    def live_recording(self):
        control = self._authorized_job_control('JOB-01')
        with self._workspace_application_lock:
            if not hasattr(self, '_live_recording'):
                from ..core.live_recording.service import LiveService
                from ..core.models_manager import app_data_dir
                self._live_recording = LiveService(app_data_dir(), control,
                    self._application.verified_installation_id(),
                    historical_recordings=(self._historical_context or {}).get('live_recordings', {}))
            return self._live_recording


    @staticmethod
    def _job_summary(job: JobV1) -> dict[str, object]:
        attempt = job.attempts[-1]
        return {
            "job_id": job.job_id,
            "job_kind": job.job_kind,
            "job_revision": job.job_revision,
            "state": job.state.value,
            "lease_epoch": attempt.lease_epoch,
            "last_sequence": job.event_sequence,
            "result_ref": job.result_ref,
            "receipt_ref": f"lulu-job:{job.job_id}:revision:{job.job_revision}",
        }

    @staticmethod
    def _gui_job_summary(job: JobV1) -> dict[str, object]:
        gui_task = job.frozen_policy.get("gui_task")
        if isinstance(gui_task, dict) and "gui_execution_policy" in job.frozen_policy:
            gui_task = {**gui_task, "execution_policy": job.frozen_policy["gui_execution_policy"]}
        return {
            "job_id": job.job_id,
            "job_revision": job.job_revision,
            "state": job.state.value,
            "job_kind": job.job_kind,
            "input_ref": job.input_ref,
            "gui_task": {**(gui_task if isinstance(gui_task, dict) else {}),
                         "_display_created_at": job.created_at},
            "failure": next((item.get("retry_hint") for item in job.partial_failures
                if item.get("code") == "GUI_TRANSCRIPTION_FAILED"), None),
            "result_ref": job.result_ref,
            "created_by": job.created_by,
        }

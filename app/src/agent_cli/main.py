"""Versioned CLI adapter; business commands require the verified Core runtime."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import TextIO

from .catalog import capability_discovery, RETIRED_LICENSE_CAPABILITIES
from .contracts import (
    CLI_PROTOCOL_VERSION,
    SCHEMA_ID,
    SCHEMA_REVISION,
    capture_third_party_output,
    write_cli_error,
    write_json_object,
    write_jsonl_event,
)
from .doctor import network_prepare_proposal, offline_doctor_report
from .grammar import CliGrammarError, parse_cli_argv


from ..version import __version__, __build__, __channel__

_SOURCE_CONTROL_PLANE_IDENTITY = {
    "product_version": __version__,
    "build": __build__,
    "channel": __channel__,
}

_LINK_VALUE_OPTIONS = {
    "--engine": "engine",
    "--format": "format",
    "--subtitle-fallback": "subtitle_fallback",
    "--media-mode": "media_mode",
    "--media-directory": "media_directory",
    "--output-destination": "output_destination",
    "--idempotency-key": "expected_idempotency_key",
}
_LINK_FLAG_OPTIONS = {
    "--prefer-subtitles": "prefer_subtitles",
    "--save-media": "save_media",
    "--save-cover": "save_cover",
}


def _parse_link_options(args: list[str]) -> tuple[str, dict[str, object]]:
    from ..core.link_transcription_application import LinkTranscriptionError

    if (
        len(args) < 5
        or args[-2:] != ["--output", "json"]
        or not args[2].lower().startswith(("http://", "https://"))
    ):
        raise LinkTranscriptionError("INVALID_ARGUMENT", "URL_COMMAND_INVALID")
    values: dict[str, object] = {}
    seen: set[str] = set()
    index = 3
    end = len(args) - 2
    while index < end:
        option = args[index]
        if option in seen:
            raise LinkTranscriptionError("INVALID_ARGUMENT", "DUPLICATE_OPTION")
        seen.add(option)
        flag_field = _LINK_FLAG_OPTIONS.get(option)
        if flag_field is not None:
            values[flag_field] = True
            index += 1
            continue
        value_field = _LINK_VALUE_OPTIONS.get(option)
        if value_field is None:
            raise LinkTranscriptionError("UNKNOWN_FIELD", "OPTION_NOT_REGISTERED")
        if index + 1 >= end or args[index + 1].startswith("--"):
            raise LinkTranscriptionError("INVALID_ARGUMENT", "OPTION_VALUE_REQUIRED")
        values[value_field] = args[index + 1]
        index += 2
    return args[2], values


def _local_business_error(
    exc: Exception,
    *,
    command: str,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    from ..core.job_ledger.contracts import JobLedgerError
    from ..core.resource_control.contracts import ResourceControlError
    from ..core.link_transcription_application import LinkTranscriptionError
    from ..core.transcription_job_application import LocalTranscriptionJobError

    from ..core.transcription_application import LocalFileTranscriptionError

    if isinstance(exc, LocalFileTranscriptionError):
        code = exc.failure.code
        reason_code = getattr(exc, "reason_code", "LOCAL_TRANSCRIPTION_" + code)
    elif isinstance(exc, LocalTranscriptionJobError):
        code = exc.code
        reason_code = exc.reason_code
    elif isinstance(exc, (JobLedgerError, ResourceControlError)):
        code = exc.code
        reason_code = exc.reason_code
    elif isinstance(exc, LinkTranscriptionError):
        code = exc.code
        reason_code = exc.reason_code
    else:
        raise exc
    return write_cli_error(
        code=code,
        command=command,
        details={"reason_code": reason_code},
        stdout=stdout,
        stderr=stderr,
    )


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    product_identity: dict[str, object] | None = None,
    resource_root: Path | None = None,
    local_transcription_runtime: object | None = None,
    link_transcription_service: object | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    if args[:1]==['workspace'] and local_transcription_runtime is not None:
        from .local_transcription import LocalTranscriptionCliRuntime
        from .workspace_commands import execute
        if not isinstance(local_transcription_runtime,LocalTranscriptionCliRuntime):
            raise TypeError('local_transcription_runtime is invalid')
        command='.'.join(args[:3])
        try:
            result, _captured = capture_third_party_output(lambda: execute(args, local_transcription_runtime))
        except Exception as exc:
            return _local_business_error(exc,command=command,stdout=output,stderr=error_output)
        write_json_object({'schema_id':SCHEMA_ID,'schema_revision':SCHEMA_REVISION,
                          'ok':True,'command':command,'result':result},output)
        return 0
    if (args[:2] in (["library", "list"], ["library", "show"], ["copy", "templates"])
            and local_transcription_runtime is not None):
        from .local_transcription import LocalTranscriptionCliRuntime
        from .library_commands import execute
        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError("local_transcription_runtime is invalid")
        command = ".".join(args[:3] if args[:2] == ["copy", "templates"] else args[:2])
        try:
            result, _captured = capture_third_party_output(lambda: execute(args, local_transcription_runtime))
        except Exception as exc:
            return _local_business_error(exc, command=command, stdout=output, stderr=error_output)
        write_json_object({"schema_id": SCHEMA_ID, "schema_revision": SCHEMA_REVISION,
                           "ok": True, "command": command, "result": result}, output)
        return 0
    if link_transcription_service is None and local_transcription_runtime is not None:
        from .local_transcription import LocalTranscriptionCliRuntime

        if isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            link_transcription_service = (
                local_transcription_runtime.link_transcription_service
            )
    if (len(args) >= 3 and args[:2] in (["transcribe", "prepare"], ["transcribe", "run"])
            and args[2].lower().startswith(("http://", "https://"))
            and link_transcription_service is None):
        return write_cli_error(
            code="CAPABILITY_UNAVAILABLE", command=".".join(args[:2]),
            details={"reason_code": "LINK_TRANSCRIPTION_RUNTIME_UNAVAILABLE"},
            stdout=output, stderr=error_output,
        )
    if args[:1] == ['extract'] and local_transcription_runtime is not None:
        from .extraction_commands import execute
        command = '.'.join(args[:2])
        try:
            result, _captured = capture_third_party_output(lambda: execute(args, local_transcription_runtime))
        except Exception as exc:
            return _local_business_error(exc, command=command, stdout=output, stderr=error_output)
        write_json_object({'schema_id': SCHEMA_ID, 'schema_revision': SCHEMA_REVISION,
            'ok': True, 'command': command, 'result': result}, output)
        return 0
    if args[:1] == ["tts"] and local_transcription_runtime is not None:
        from .local_transcription import LocalTranscriptionCliRuntime
        from ..core.transcription_job_application import LocalTranscriptionJobError
        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError("local_transcription_runtime is invalid")
        command = "tts.status" if args[1:2] == ["status"] else "tts.synthesize"
        try:
            if len(args) < 4 or len(args) % 2:
                raise LocalTranscriptionJobError("INVALID_ARGUMENT", "TTS_OPTIONS_INVALID")
            values = {}
            for key, value in zip(args[2::2], args[3::2]):
                if key in values:
                    raise LocalTranscriptionJobError("INVALID_ARGUMENT", "TTS_OPTIONS_INVALID")
                values[key] = value
            if values.get('--output') != 'json':
                raise LocalTranscriptionJobError("INVALID_ARGUMENT", "TTS_OPTIONS_INVALID")
            if args[1] == 'status' and set(values) <= {'--output', '--engine'}:
                result, _captured = capture_third_party_output(
                    lambda: local_transcription_runtime.tts_status(engine=values.get('--engine', 'local_voxcpm2')))
            elif (args[1] == 'synthesize'
                  and {'--engine', '--text-file', '--destination', '--output'} <= set(values)
                  and set(values) <= {'--engine', '--text-file', '--voice-id', '--destination', '--output', '--options-file'}):
                result, _captured = capture_third_party_output(lambda: local_transcription_runtime.tts_synthesize(text_file=values['--text-file'],
                    voice_id=values.get('--voice-id', ''), destination=values['--destination'],
                    engine=values['--engine'], options_file=values.get('--options-file')))
            else:
                raise LocalTranscriptionJobError("INVALID_ARGUMENT", "TTS_OPTIONS_INVALID")
        except Exception as exc:
            return _local_business_error(exc, command=command, stdout=output, stderr=error_output)
        write_json_object({"schema_id": SCHEMA_ID, "schema_revision": SCHEMA_REVISION,
            "ok": True, "command": command, "result": result}, output)
        return 0
    if (
        len(args) >= 5
        and args[:2] == ["transcribe", "prepare"]
        and link_transcription_service is not None
        and args[2].lower().startswith(("http://", "https://"))
    ):
        from ..core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )

        if not isinstance(
            link_transcription_service, LinkTranscriptionApplicationService
        ):
            raise TypeError("link_transcription_service is invalid")
        try:
            source, link_options = _parse_link_options(args)
            link_options.pop("expected_idempotency_key", None)
            from .local_transcription import LocalTranscriptionCliRuntime

            prepare = (
                local_transcription_runtime.prepare_link
                if isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime)
                else link_transcription_service.prepare
            )
            result, _captured = capture_third_party_output(
                lambda: prepare(source, **link_options)
            )
        except Exception as exc:
            return _local_business_error(
                exc,
                command="transcribe.prepare",
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "transcribe.prepare",
                "result": result,
            },
            output,
        )
        return 0
    if (
        len(args) >= 5
        and args[:2] == ["transcribe", "run"]
        and link_transcription_service is not None
        and args[2].lower().startswith(("http://", "https://"))
    ):
        from ..core.link_transcription_application import (
            LinkTranscriptionApplicationService,
            LinkTranscriptionError,
        )

        if not isinstance(
            link_transcription_service, LinkTranscriptionApplicationService
        ):
            raise TypeError("link_transcription_service is invalid")
        try:
            source, link_options = _parse_link_options(args)
            from .local_transcription import LocalTranscriptionCliRuntime

            if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
                raise LinkTranscriptionError(
                    "HUMAN_ACTION_REQUIRED",
                    "SAME_BUNDLE_HOST_BROKER_UNAVAILABLE",
                )
            result, _captured = capture_third_party_output(
                lambda: local_transcription_runtime.run_link(source, **link_options)
            )
        except Exception as exc:
            return _local_business_error(
                exc,
                command="transcribe.run",
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "transcribe.run",
                "result": result,
            },
            output,
        )
        return 0
    if (args[:2] in (["transcribe", "prepare"], ["transcribe", "run"], ["transcribe", "status"])
            and local_transcription_runtime is not None):
        from .local_transcription import LocalTranscriptionCliRuntime
        from ..core.transcription_job_application import LocalTranscriptionJobError
        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError('local_transcription_runtime is invalid')
        command = '.'.join(args[:2])
        try:
            start = 2 if args[1] == 'status' else 3
            if len(args) < start + 2 or args[-2:] != ['--output', 'json']:
                raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'LOCAL_TRANSCRIPTION_OPTIONS_INVALID')
            tail = args[start:-2]
            if tail and (len(tail) != 2 or tail[0] != '--engine'):
                raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'LOCAL_TRANSCRIPTION_OPTIONS_INVALID')
            engine = tail[1] if tail else None
            from ..core.engines.local_models import LOCAL_MODELS
            if engine is not None and engine not in LOCAL_MODELS:
                raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'LOCAL_ENGINE_UNSUPPORTED')
            if args[1] == 'status':
                call = lambda: local_transcription_runtime.transcribe_status(engine)
            else:
                handler = getattr(local_transcription_runtime, args[1])
                call = lambda: handler(args[2], engine=engine or 'local_sensevoice')
            result, _captured = capture_third_party_output(call)
        except Exception as exc:
            return _local_business_error(exc, command=command, stdout=output, stderr=error_output)
        write_json_object(dict(schema_id=SCHEMA_ID, schema_revision=SCHEMA_REVISION,
                               ok=True, command=command, result=result), output)
        return 0
    if args[:2] == ['jobs', 'result'] and local_transcription_runtime is not None:
        from ..core.transcription_job_application import LocalTranscriptionJobError
        try:
            if (len(args) != 8 or args[2] != '--job-id' or args[4] != '--offset'
                    or args[-2:] != ['--output', 'json'] or not args[5].isascii() or not args[5].isdecimal()
                    or len(args[5]) > 8):
                raise LocalTranscriptionJobError('INVALID_ARGUMENT', 'RESULT_OPTIONS_INVALID')
            result, _captured = capture_third_party_output(
                lambda: local_transcription_runtime.jobs_result(args[3], int(args[5])))
        except Exception as exc:
            return _local_business_error(exc, command='jobs.result', stdout=output, stderr=error_output)
        write_json_object(dict(schema_id=SCHEMA_ID, schema_revision=SCHEMA_REVISION,
                               ok=True, command='jobs.result', result=result), output)
        return 0
    if (
        args == ["jobs", "list", "--output", "json"]
        and local_transcription_runtime is not None
    ):
        from .local_transcription import LocalTranscriptionCliRuntime

        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError("local_transcription_runtime is invalid")
        try:
            result, _captured = capture_third_party_output(
                local_transcription_runtime.jobs_list
            )
        except Exception as exc:
            return _local_business_error(
                exc,
                command="jobs.list",
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "jobs.list",
                "result": result,
            },
            output,
        )
        return 0
    if (
        len(args) == 6
        and args[:3] in (
            ["jobs", "show", "--job-id"],
            ["jobs", "cancel", "--job-id"],
            ["jobs", "receipt", "--job-id"],
        )
        and args[-2:] == ["--output", "json"]
        and local_transcription_runtime is not None
    ):
        from .local_transcription import LocalTranscriptionCliRuntime

        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError("local_transcription_runtime is invalid")
        operation = args[1]
        handlers = {
            "show": local_transcription_runtime.jobs_show,
            "cancel": local_transcription_runtime.jobs_cancel,
            "receipt": local_transcription_runtime.jobs_receipt,
        }
        try:
            result, _captured = capture_third_party_output(
                lambda: handlers[operation](args[3])
            )
        except Exception as exc:
            return _local_business_error(
                exc,
                command=f"jobs.{operation}",
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": f"jobs.{operation}",
                "result": result,
            },
            output,
        )
        return 0
    if (
        len(args) == 8
        and args[:3] == ["jobs", "wait", "--job-id"]
        and args[4] == "--after-sequence"
        and args[-2:] == ["--output", "jsonl"]
        and local_transcription_runtime is not None
    ):
        from .local_transcription import LocalTranscriptionCliRuntime

        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError("local_transcription_runtime is invalid")
        try:
            after_sequence = int(args[5])
        except ValueError:
            return write_cli_error(
                code="INVALID_ARGUMENT",
                command="jobs.wait",
                details={"field": "after_sequence", "reason_code": "VALUE_INVALID"},
                stdout=output,
                stderr=error_output,
            )
        try:
            events, _captured = capture_third_party_output(
                lambda: local_transcription_runtime.jobs_wait(
                    args[3], after_sequence
                )
            )
        except Exception as exc:
            return _local_business_error(
                exc,
                command="jobs.wait",
                stdout=output,
                stderr=error_output,
            )
        for event in events:
            write_jsonl_event(
                {
                    "event_schema_id": "lulu.agent-event/v1",
                    "sequence": event.sequence,
                    "event": event.event_type,
                    "payload": event.as_dict(),
                },
                output,
            )
        return 0
    if (
        product_identity is not None
        and len(args) == 7
        and args[:4] == ["skill", "compatibility", "check", "--contract-json"]
        and args[-2:] == ["--output", "json"]
    ):
        from .proposal import SkillProposalError, parse_skill_request_json
        from .skill_contract import SkillContractError, validate_skill_compatibility

        try:
            contract = parse_skill_request_json(args[4])
            expected_fields = {
                "skill_manifest",
                "cli_claim",
                "verified_installation",
            }
            unknown = set(contract) - expected_fields
            missing = expected_fields - set(contract)
            if unknown:
                raise SkillProposalError(
                    "UNKNOWN_FIELD", sorted(unknown)[0], "FIELD_NOT_ALLOWED"
                )
            if missing:
                raise SkillProposalError(
                    "INVALID_ARGUMENT", sorted(missing)[0], "FIELD_REQUIRED"
                )
            compatibility = validate_skill_compatibility(
                skill_manifest=contract["skill_manifest"],
                cli_claim=contract["cli_claim"],
                verified_installation=contract["verified_installation"],
            )
        except SkillProposalError as exc:
            return write_cli_error(
                code=exc.code,
                command="skill.compatibility.check",
                details={"field": exc.field, "reason_code": exc.reason_code},
                stdout=output,
                stderr=error_output,
            )
        except SkillContractError as exc:
            return write_cli_error(
                code="INVALID_ARGUMENT",
                command="skill.compatibility.check",
                details={"field": exc.field, "reason_code": exc.code},
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "skill compatibility check",
                "result": compatibility,
            },
            output,
        )
        return 0
    if (
        product_identity is not None
        and len(args) == 7
        and args[:4] == ["skill", "proposal", "prepare", "--request-json"]
        and args[-2:] == ["--output", "json"]
    ):
        from .proposal import (
            SkillProposalError,
            build_skill_proposal,
            parse_skill_request_json,
        )

        try:
            request = parse_skill_request_json(args[4])
            proposal = build_skill_proposal(request)
        except SkillProposalError as exc:
            return write_cli_error(
                code=exc.code,
                command="skill proposal prepare",
                details={"field": exc.field, "reason_code": exc.reason_code},
                stdout=output,
                stderr=error_output,
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "skill proposal prepare",
                "result": proposal,
            },
            output,
        )
        return 0
    if (
        product_identity is not None
        and len(args) == 7
        and args[:4] == ["skill", "jobs", "wait-status", "--job-id"]
        and args[-2:] == ["--output", "json"]
    ):
        job_id = args[4]
        if (
            not job_id
            or len(job_id) > 160
            or any(not (character.isascii() and (character.isalnum() or character in "_.:-")) for character in job_id)
        ):
            return write_cli_error(
                code="INVALID_ARGUMENT",
                command="skill.jobs.wait-status",
                details={"field": "job_id", "reason_code": "VALUE_INVALID"},
                stdout=output,
                stderr=error_output,
            )
        return write_cli_error(
            code="CAPABILITY_UNAVAILABLE",
            command="skill.jobs.wait-status",
            details={
                "capability_id": "JOB-02",
                "reason_code": "PACKAGE_TRUST_EVIDENCE_UNAVAILABLE",
            },
            stdout=output,
            stderr=error_output,
        )
    active_ids = frozenset()
    if args[:1] in (['help'], ['status'], ['doctor']) and local_transcription_runtime is not None:
        from .local_transcription import LocalTranscriptionCliRuntime
        if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
            raise TypeError('local_transcription_runtime is invalid')
        active_ids = local_transcription_runtime.activated_capability_ids
    if args == ["version", "--output", "json"]:
        if product_identity is None:
            version_identity = dict(_SOURCE_CONTROL_PLANE_IDENTITY)
        else:
            shared_identity = product_identity.get("shared")
            if type(shared_identity) is not dict:
                raise RuntimeError("产品入口缺少已验证的共享身份")
            version_identity = {
                "product_version": shared_identity["product_version"],
                "build": shared_identity["product_build"],
                "channel": shared_identity["channel"],
            }
        result: dict[str, object] = {
            **version_identity,
            "cli_protocol_version": CLI_PROTOCOL_VERSION,
            "formal_cli_state": "unavailable",
        }
        if product_identity is not None:
            result["product_identity"] = product_identity
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "version",
                "result": result,
            },
            output,
        )
        return 0
    if args == ["capabilities", "--output", "json"]:
        activated_capability_ids = frozenset()
        if local_transcription_runtime is not None:
            from .local_transcription import LocalTranscriptionCliRuntime

            if not isinstance(local_transcription_runtime, LocalTranscriptionCliRuntime):
                raise TypeError("local_transcription_runtime is invalid")
            activated_capability_ids = (
                local_transcription_runtime.activated_capability_ids
            )
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "capabilities",
                "result": capability_discovery(
                    resource_root=resource_root,
                    activated_capability_ids=activated_capability_ids,
                ),
            },
            output,
        )
        return 0
    if args == ["status", "--output", "json"]:
        result = {
            "formal_cli_state": "unavailable",
            "authorization": {
                "status": "not_requested",
                "reason_code": "PHASE3_STATUS_DOES_NOT_READ_AUTH",
            },
            "runtime": {
                "status": "unavailable",
                "reason_code": "FORMAL_CLI_NOT_IMPLEMENTED",
            },
            "jobs": {
                "status": "not_requested",
                "reason_code": "PHASE3_STATUS_DOES_NOT_READ_JOBS",
            },
        }
        if active_ids:
            result['formal_cli_state'] = 'available'
            result['runtime'] = {'status': 'ready', 'reason_code': None}
            result['activated_capability_count'] = len(active_ids)
        if product_identity is not None:
            result["product_identity"] = product_identity
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "status",
                "result": result,
            },
            output,
        )
        return 0
    if args == ["help", "--output", "json"]:
        result = dict(formal_cli_state='available' if active_ids else 'unavailable',
            control_commands=['help', 'version', 'capabilities', 'status', 'doctor', 'doctor network prepare'],
            business_dispatch='available' if active_ids else 'unavailable')
        if active_ids:
            result['business_commands'] = [row['argv_template'] for row in capability_discovery(
                resource_root=resource_root, activated_capability_ids=active_ids)['capabilities'] if row['executable']]
            result['transcription_models_command'] = 'lulu transcribe status [--engine <engine>] --output json'
            result['transcription_result_command'] = 'lulu jobs result --job-id <job-id> --offset <byte-offset> --output json'
            result['voice_models_command'] = 'lulu tts status --output json'
            result['workspace_tools_command'] = 'lulu workspace tools catalog --request <absolute JSON file containing {}> --output json'
        write_json_object(dict(schema_id=SCHEMA_ID, schema_revision=SCHEMA_REVISION,
            ok=True, command='help', result=result), output)
        return 0
    if args == ["doctor", "--output", "json"]:
        result = offline_doctor_report()
        if active_ids:
            result.update(formal_cli_state='available', overall_status='available', scope='verified_cli_entry',
                          local_checks={'status':'passed', 'reason_code':None, 'activated_capability_count':len(active_ids)})
        if product_identity is not None:
            result["product_identity"] = product_identity
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "doctor",
                "result": result,
            },
            output,
        )
        return 0
    if args == ["doctor", "network", "prepare", "--output", "json"]:
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": True,
                "command": "doctor network prepare",
                "result": network_prepare_proposal(),
            },
            output,
        )
        return 0
    if (
        len(args) == 7
        and args[:4] == ["doctor", "network", "run", "--approval-receipt"]
        and args[-2:] == ["--output", "json"]
    ):
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": False,
                "command": "doctor network run",
                "error": {
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "正式 CLI 能力尚未实现",
                    "retryable": False,
                    "human_action": "等待后续正式 CLI 阶段",
                    "details": {"reason_code": "FORMAL_CLI_NOT_IMPLEMENTED"},
                },
            },
            output,
        )
        return 30
    try:
        parsed = parse_cli_argv(args)
    except CliGrammarError as exc:
        messages = {
            "UNKNOWN_COMMAND": "命令不在 Phase 3 闭合 grammar 中",
            "UNKNOWN_FIELD": "参数字段不在 Phase 3 闭合 grammar 中",
            "INVALID_ARGUMENT": "参数不符合 Phase 3 闭合 grammar",
        }
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": False,
                "command": "unknown" if exc.code == "UNKNOWN_COMMAND" else "invalid",
                "error": {
                    "code": exc.code,
                    "message": messages[exc.code],
                    "retryable": False,
                    "human_action": "使用 help 或 capabilities 查看当前控制面",
                    "details": exc.details,
                },
            },
            output,
        )
        return exc.exit_code
    if parsed is not None and parsed.current_state == "unavailable":
        retired = parsed.capability_id in RETIRED_LICENSE_CAPABILITIES
        write_json_object(
            {
                "schema_id": SCHEMA_ID,
                "schema_revision": SCHEMA_REVISION,
                "ok": False,
                "command": " ".join(parsed.command_path),
                "error": {
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "授权码功能已退役；新版 Lulu 无需激活。" if retired else "正式 CLI 能力尚未实现",
                    "retryable": False,
                    "human_action": "直接使用新版 Lulu，无需提供授权码" if retired else "等待后续正式 CLI 阶段",
                    "details": {
                        "capability_id": parsed.capability_id,
                        "reason_code": "LICENSE_FEATURE_RETIRED" if retired else "FORMAL_CLI_NOT_IMPLEMENTED",
                    },
                },
            },
            output,
        )
        return 30
    error_output.write("UNKNOWN_COMMAND\n")
    return 2

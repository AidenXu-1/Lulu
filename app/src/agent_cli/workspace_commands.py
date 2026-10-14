"""Closed JSON-file adapter. Every operation uses the same workspace Core service."""
import json
import os
import stat
from pathlib import Path
from ..core.agent_workspace.contracts import COMMANDS
from ..core.agent_workspace.service import handle, validate_request
from ..core.transcription_job_application import LocalTranscriptionJobError
from ..core.library import RevisionConflict


def execute(argv, runtime):
    if len(argv)!=7 or argv[0]!='workspace' or argv[3]!='--request' or argv[-2:]!=['--output','json']:
        raise LocalTranscriptionJobError('INVALID_ARGUMENT','WORKSPACE_COMMAND_INVALID')
    name='.'.join(argv[1:3])
    if name not in COMMANDS:
        raise LocalTranscriptionJobError('CAPABILITY_UNAVAILABLE','WORKSPACE_COMMAND_UNAVAILABLE')
    cap,operation,effect=COMMANDS[name]
    runtime._application.assert_workspace_allowed(cap)
    try:
        path=Path(argv[4]).expanduser()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as file:
            meta=os.fstat(file.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size>8*1024*1024:
                raise ValueError('JSON request must be a regular file under 8 MB')
            raw=file.read(8*1024*1024+1)
        arguments=json.loads(raw)
        validate_request(operation,arguments)
        return handle(operation,arguments,runtime=runtime,origin="cli")
    except LocalTranscriptionJobError:
        raise
    except RevisionConflict as exc:
        raise LocalTranscriptionJobError('CONFLICT','WORKSPACE_REVISION_CONFLICT') from exc
    except FileNotFoundError as exc:
        raise LocalTranscriptionJobError('NOT_FOUND','WORKSPACE_ITEM_NOT_FOUND') from exc
    except (ValueError,TypeError,UnicodeError,KeyError) as exc:
        raise LocalTranscriptionJobError('INVALID_ARGUMENT','WORKSPACE_REQUEST_INVALID') from exc
    except OSError as exc:
        raise LocalTranscriptionJobError('INTERNAL_ERROR','WORKSPACE_STORAGE_UNAVAILABLE') from exc
    except RuntimeError as exc:
        raise LocalTranscriptionJobError('INTERNAL_ERROR','WORKSPACE_REQUEST_FAILED') from exc

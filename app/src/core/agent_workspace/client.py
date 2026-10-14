"""One GUI transport; installed requests execute in the existing authoritative Core."""
import sys
from pathlib import Path

from .service import handle, validate_request


def dispatch(operation: str, **arguments):
    validate_request(operation, arguments)
    if getattr(sys, "frozen", False) and Path(sys.executable).name == "Lulu":
        from ..agent_entry.entrypoints import build_default_gui_core_dispatcher
        reply = build_default_gui_core_dispatcher()("gui.workspace", {"operation": operation, "arguments": arguments})
        if type(reply) is dict and set(reply) == {'error'}:
            error = reply['error']
            if (type(error) is dict and set(error) == {'code', 'message'} and
                    type(error['code']) is str and
                    error['code'] in {'ValueError', 'FileNotFoundError', 'RevisionConflict'} and
                    type(error['message']) is str and len(error['message']) <= 2000):
                from ..library import RevisionConflict
                error_type = {'ValueError': ValueError, 'FileNotFoundError': FileNotFoundError,
                              'RevisionConflict': RevisionConflict}[error['code']]
                raise error_type(error['message'])
        if type(reply) is not dict or set(reply) != {"result"}:
            raise RuntimeError("工作区回复格式无效，请重新打开 Lulu；未重复执行请求")
        return reply["result"]
    return handle(operation, arguments)

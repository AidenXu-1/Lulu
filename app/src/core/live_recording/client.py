"""GUI transport only; source runs must inject an isolated development service."""
from .contracts import LiveError, validate_request

def dispatch(operation, arguments):
    validate_request(operation, arguments)
    from ..agent_entry.entrypoints import build_default_gui_core_dispatcher
    reply = build_default_gui_core_dispatcher()('gui.live', {'operation': operation, 'arguments': arguments})
    if type(reply) is dict and set(reply) == {'error'} and type(reply['error']) is str:
        raise LiveError(reply['error'][:2000])
    if type(reply) is not dict or set(reply) != {'result'} or type(reply['result']) is not dict:
        raise LiveError('直播连接回复无效，请重新打开 Lulu；未重复执行操作')
    return reply['result']

"""Public GUI license actions over the existing measured Core connection."""
from threading import RLock

from .models import LicenseStatus

_ACTIONS = frozenset({'startup_status', 'cached_status_projection', 'activate',
    'refresh_device_summary', 'deactivate_current_device', 'resume_pending_deactivation'})
_PUBLIC_FIELDS = ('allowed', 'code', 'message', 'active_device_count', 'max_devices',
    'request_id', 'deactivation_status', 'replayed', 'low_privilege_actions')


def validate_license_action(arguments):
    if (type(arguments) is not dict or type(arguments.get('action')) is not str
            or arguments['action'] not in _ACTIONS):
        raise ValueError('LICENSE_ACTION_INVALID')
    action = arguments['action']
    if action == 'activate':
        if (set(arguments) != {'action', 'license_code'} or
                type(arguments['license_code']) is not str or len(arguments['license_code']) > 256):
            raise ValueError('LICENSE_ACTION_INVALID')
    elif set(arguments) != {'action'}:
        raise ValueError('LICENSE_ACTION_INVALID')
    return action


def dispatch_license_action(service, arguments):
    action = validate_license_action(arguments)
    status = (service.activate(arguments['license_code']) if action == 'activate'
              else getattr(service, action)())
    if not isinstance(status, LicenseStatus):
        raise ValueError('LICENSE_STATUS_INVALID')
    # No lease token, device hash, activation code or cached keychain material
    # crosses back to the GUI. Core remains the only license storage owner.
    result = {key: getattr(status, key) for key in _PUBLIC_FIELDS}
    result['low_privilege_actions'] = list(status.low_privilege_actions)
    return result


class CoreLicenseService:
    def __init__(self, dispatcher=None):
        self._dispatcher = dispatcher
        self._lock = RLock()

    def _call(self, action, **arguments):
        try:
            with self._lock:
                if self._dispatcher is None:
                    from ..agent_entry.entrypoints import build_default_gui_core_dispatcher
                    self._dispatcher = build_default_gui_core_dispatcher()
                result = self._dispatcher('gui.license', {'action': action, **arguments})
            if (type(result) is not dict or set(result) != set(_PUBLIC_FIELDS)
                    or type(result['allowed']) is not bool or type(result['code']) is not str
                    or type(result['message']) is not str or type(result['replayed']) is not bool
                    or type(result['low_privilege_actions']) is not list
                    or any(type(v) is not str for v in result['low_privilege_actions'])):
                raise ValueError('LICENSE_STATUS_INVALID')
            result = dict(result, low_privilege_actions=tuple(result['low_privilege_actions']))
            return LicenseStatus(**result)
        except Exception:
            # In particular, never replay an activation/deactivation after an
            # uncertain transport result; its existing recovery action owns that.
            return LicenseStatus(False, 'service_unavailable', 'Lulu 正在准备授权服务，请稍后重试。')

    def startup_status(self): return self._call('startup_status')
    def cached_status_projection(self): return self._call('cached_status_projection')
    def activate(self, license_code): return self._call('activate', license_code=license_code)
    def refresh_device_summary(self): return self._call('refresh_device_summary')
    def deactivate_current_device(self): return self._call('deactivate_current_device')
    def resume_pending_deactivation(self): return self._call('resume_pending_deactivation')

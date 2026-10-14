"""Installed App preparation and its deterministic, per-generation service."""
from pathlib import Path
import os
import plistlib

from .standalone_paths import BASE_SERVICE, frozen_app, state_for_app, service_for_app
from .standalone_state import install_state_root, prepare_install_state, verify_install_state
from .installed_materials import _private_json, MATERIALS_DIR
from .ownership import verify_ownership_receipt, verify_ready_action_receipt
from .delivery_identity import MANIFEST_NAME


def registration_bytes(app, *, token):
    import re
    if re.fullmatch(r'[a-f0-9]{64}', token) is None:
        raise RuntimeError('PRODUCT_PATH_UNSAFE')
    label = BASE_SERVICE+'.app-'+token
    return plistlib.dumps({'Label': label,
        'ProgramArguments': [str(Path(app)/'Contents/MacOS/lulu-agent-core')],
        'MachServices': {label: True}, 'ProcessType': 'Interactive'})


def previous_states(product_root, *, target, keys):
    root = Path(product_root)/'AgentEntry'
    candidates = []
    local = getattr(keys, 'storage_scheme', None) == 'local-integrity-v1'
    generations = root/('local-app-generations' if local else 'app-generations')
    if generations.exists():
        if generations.is_symlink():
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        import re
        for path in generations.iterdir():
            if path == target or not re.fullmatch(r'[a-f0-9]{64}', path.name):
                continue
            if path.is_symlink():
                raise RuntimeError('PRODUCT_PATH_UNSAFE')
            owner = verify_ownership_receipt(path/'ownership.json', key_store=keys)
            verify_ready_action_receipt(path/'current.json', ownership=owner)
            from .installed_materials import _private_raw
            import hashlib
            raw = _private_raw(path/MATERIALS_DIR/MANIFEST_NAME)
            if hashlib.sha256(raw).hexdigest() != owner['component_manifest_sha256']:
                raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
            from .delivery_identity import strict_json
            build = strict_json(raw)['product_build']
            date, number = build.split('.')
            candidates.append(((int(date), int(number)), path))
    candidates.sort()
    paths = [path for _, path in candidates]
    if not local and os.path.lexists(root/'ownership.json'):
        owner = verify_ownership_receipt(root/'ownership.json', key_store=keys)
        verify_ready_action_receipt(root/'current.json', ownership=owner)
        paths.insert(0, root)
    return paths


def find_previous_state(product_root, *, target, keys, backup_app=None):
    paths = previous_states(product_root, target=target, keys=keys)
    if backup_app is not None:
        from .component_manifest import verify_component_manifest
        matches = []
        for path in paths:
            try:
                checked = verify_component_manifest(Path(backup_app), path/MATERIALS_DIR/MANIFEST_NAME)
            except RuntimeError:
                continue
            owner = verify_ownership_receipt(path/'ownership.json', key_store=keys)
            if checked['component_manifest_sha256'] == owner['component_manifest_sha256']:
                matches.append(path)
        if len(matches) != 1:
            raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
        return matches[0]
    if len(paths) > 1:
        builds = [_private_json(path/MATERIALS_DIR/MANIFEST_NAME)['product_build'] for path in paths[-2:]]
        if builds[0] == builds[1]:
            raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
    return paths[-1] if paths else None


def prepare_current_app(*, backup_app=None):
    """Only the signed installed Core may touch ownership/task secure storage."""
    app = frozen_app('lulu-agent-core')
    from .standalone_source import uses_local_storage
    if uses_local_storage(app):
        from .local_task_storage import prepare_local_app
        return prepare_local_app(app, Path.home()/'Library/Application Support/Lulu')
    from ..cli_runtime.core_task_keychain import CoreTaskKeyringStore
    from .standalone_history import capture_history, merge_history, previous_core_identity
    keys = CoreTaskKeyringStore()
    product = Path.home()/'Library/Application Support/Lulu'
    target = install_state_root(app, product)
    sources = previous_states(product, target=target, keys=keys)
    previous = find_previous_state(product, target=target, keys=keys, backup_app=backup_app)
    known_cores = {}
    from .task_handover import FILENAME, open_handover
    installation_id = (verify_ownership_receipt(previous/'ownership.json', key_store=keys)['installation_id']
                       if previous is not None else None)
    identity_owner = None
    for source in sources + ([target] if target.exists() else []):
        owner = verify_ownership_receipt(source/'ownership.json', key_store=keys)
        verify_ready_action_receipt(source/'current.json', ownership=owner)
        if installation_id is None:
            installation_id = owner['installation_id']
        if owner['installation_id'] != installation_id:
            raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
        identity_owner = owner
        path = source/MATERIALS_DIR/FILENAME
        if path.exists():
            value = _private_json(path)
            open_handover(value, keys=keys, owner=owner, product_root=product)
            for digest, cdhash in value['body'].get('source_cores', {}).items():
                if digest in known_cores and known_cores[digest] != cdhash:
                    raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
                known_cores[digest] = cdhash
    if identity_owner is not None:
        from .attempted_cores import read_attempted_cores
        for digest, cdhash in read_attempted_cores(product, identity_owner, keys).items():
            if digest in known_cores and known_cores[digest] != cdhash:
                raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
            known_cores[digest] = cdhash
    # Collect authenticated identities before reading any old item. Otherwise
    # an early generation could reject a legitimate later retained generation.
    for source in sources:
        owner = verify_ownership_receipt(source/'ownership.json', key_store=keys)
        previous_core_identity(source, owner,
            backup_app=backup_app if source == previous else None, known_cores=known_cores)
    def handover(state, owner):
        history = ([], None)
        from .effects import CurrentUserLaunchctlService
        # A failed candidate may contain an older copy of A's history. Include
        # every authenticated predecessor, with the actual backup's view last.
        for source in [p for p in sources if p != state] + [state]:
            prior = verify_ownership_receipt(source/'ownership.json', key_store=keys)
            verify_ready_action_receipt(source/'current.json', ownership=prior)
            if prior['installation_id'] != owner['installation_id']:
                raise RuntimeError('PRODUCT_PREVIOUS_INSTALLATION_AMBIGUOUS')
            history = merge_history(history, capture_history(source, prior, product_root=product,
                keys=keys, backup_app=backup_app if source == state else None, known_cores=known_cores))
            label = BASE_SERVICE if source == product/'AgentEntry' else BASE_SERVICE+'.app-'+source.name
            CurrentUserLaunchctlService(label=label).terminate_registered_process()
        return history
    return prepare_install_state(app, product_root=product, keys=keys,
        previous_state=previous, handover_loader=handover, verify_existing=False, source_cores=known_cores)



class StandaloneInstallation:
    def __init__(self, app, keys):
        from .product_install import ProductPaths
        state = state_for_app(app)
        self.paths = ProductPaths(app, state, state/'core.plist',
            Path.home()/'.agents/skills/lulu-agent', Path.home()/'.local/bin/lulu')
        self.keys = keys

    def skill_status(self):
        from .standalone_skill import status
        return status(self)

    status = skill_status

    def install(self, *, transaction_id, crash_after=None):
        from .standalone_skill import install
        return install(self, transaction_id=transaction_id, crash_after=crash_after)

    install_skill = install

    def _all_targets(self):
        return {'app': self.paths.app_bundle, 'materials': self.paths.state_root/MATERIALS_DIR,
            'ownership': self.paths.state_root/'ownership.json', 'current': self.paths.state_root/'current.json',
            'core_registration': self.paths.launch_agent_target}

    def verify_installed(self):
        return verify_install_state(self.paths.app_bundle, state_root=self.paths.state_root, keys=self.keys)


def ensure_service(app):
    from .effects import CurrentUserLaunchctlService
    from .installed_materials import _private_raw
    state = state_for_app(app)
    if _private_raw(state/'core.plist') != registration_bytes(app, token=state.name):
        raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
    service = CurrentUserLaunchctlService(label=service_for_app(app))
    if not service.is_loaded():
        service.start(state/'core.plist')


def backup_from_plan(arguments, app):
    if not arguments:
        return None
    if len(arguments) != 2 or arguments[0] != '--update-plan':
        raise RuntimeError('PRODUCT_UPDATE_PLAN_INVALID')
    from ..updater.install import InstallTransaction
    path = Path(arguments[1])
    root = Path.home()/'Library/Application Support/Lulu/updates'
    # load_plan validates the private plan and all transaction-owned paths.
    if path.resolve() != path or not path.is_relative_to(root):
        raise RuntimeError('PRODUCT_UPDATE_PLAN_INVALID')
    transaction = InstallTransaction(root)
    plan = transaction.load_plan(path)
    if plan['target_app'] != str(app):
        raise RuntimeError('PRODUCT_UPDATE_PLAN_INVALID')
    return Path(plan['backup_app'])


def prepare_entry(arguments):
    from .standalone_paths import frozen_app
    app = frozen_app('lulu-agent-core')
    # A helper must not outlive the GUI whose update can be rolled back.
    import threading
    import time
    parent = os.getppid()
    def monitor_parent():
        while True:
            time.sleep(.2)
            if os.getppid() != parent:
                os._exit(1)
    threading.Thread(target=monitor_parent, daemon=True).start()
    try:
        backup = backup_from_plan(arguments, app)
        # A previous failed launch can leave ready materials behind. Reconcile
        # retained histories even on ordinary drag-install/retry launches.
        prepare_current_app(backup_app=backup)
        ensure_service(app)
        return 0
    except Exception as exc:
        from ..diagnostics import record_failure, failure_code
        from .standalone_bootstrap import _PREPARATION_FAILURES
        record_failure('installation_preparation', exc,
            root=Path.home()/'Library/Application Support/Lulu/AgentEntry')
        # One closed status for the owning GUI; never serialize exception text,
        # a traceback, user paths or secret values into this process channel.
        code = failure_code(exc)
        print('LULU_PREPARATION_FAILURE:'+(code if code in _PREPARATION_FAILURES
              else 'CORE_OPERATION_FAILED'), flush=True)
        return 1

"""One path policy shared by the installed GUI, CLI and Core."""
from pathlib import Path
import os
import sys

from .standalone_source import DESCRIPTOR

BASE_SERVICE = 'com.lulu.transcribe.agent-core'


def installed_app(app):
    app = Path(app)
    allowed = (Path('/Applications/Lulu.app'), Path.home()/'Applications/Lulu.app')
    if app not in allowed or app.resolve() != app:
        raise RuntimeError('INSTALLATION_TARGET_AUTHORITY_REQUIRED')
    if app != allowed[0] and not (app/DESCRIPTOR).is_file():
        raise RuntimeError('INSTALLATION_TARGET_AUTHORITY_REQUIRED')
    return app


def frozen_app(role):
    executable = Path(sys.executable)
    app = installed_app(executable.parent.parent.parent)
    if not getattr(sys, 'frozen', False) or executable != app/'Contents/MacOS'/role:
        raise RuntimeError('FROZEN_INSTALLATION_REQUIRED')
    return app


def state_for_app(app):
    if os.path.lexists(Path(app)/DESCRIPTOR):
        from .standalone_state import install_state_root
        return install_state_root(app)
    return Path.home()/'Library/Application Support/Lulu/AgentEntry'


def service_for_app(app):
    if os.path.lexists(Path(app)/DESCRIPTOR):
        return BASE_SERVICE + '.app-' + state_for_app(app).name
    return BASE_SERVICE

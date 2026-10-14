"""Compatibility setup before the first WebEngine profile is constructed."""
import os
import sys


def prepare_webengine_runtime():
    from PySide6.QtWebEngineCore import qWebEngineVersion

    if sys.platform != 'darwin' or qWebEngineVersion() != '6.11.1':
        return
    # Qt 6.11.1 resolves the locale by probing Chromium's locale directory before
    # installing its own path override. On macOS that probe creates
    # QtWebEngineCore.framework/Versions/Resources and invalidates the signature.
    # An empty initial locale selects Qt's built-in en-US fallback without that
    # early file probe. Lulu's Chinese UI, QLocale and document text are unchanged.
    # Chromium may log its expected ICU fallback. Remove only after an upgraded
    # Qt passes the signed-package runtime immutability gate.
    flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '').rstrip()
    if flags != '--lang=' and not flags.endswith(' --lang='):
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (flags + ' --lang=').lstrip()

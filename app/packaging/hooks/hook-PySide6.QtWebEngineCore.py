"""Keep Qt's standard dependency analysis; collect from its actual binary version."""
from pathlib import Path
import runpy
from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info

if pyside6_library_info.version is not None:
    if pyside6_library_info.version < [6, 2, 2]:
        raise SystemExit('QtWebEngine requires Qt 6.2.2 or newer')
    hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
    collect = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'webengine_collection.py'))['collect_webengine']
    native, resources = collect(pyside6_library_info)
    binaries += native
    datas += resources
    hiddenimports += ['PySide6.QtPrintSupport']

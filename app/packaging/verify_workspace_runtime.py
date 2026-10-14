#!/usr/bin/env python3
"""Start bundled Qt with compiled setup, then verify the signed App stayed intact.

Uses the build Python only as a host. Does not run Lulu's production entrypoint,
open user stores, read credentials, create an installed Core session or use AI.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROBE = r'''
import ctypes, importlib.abc, importlib.util, json, os, sys, time
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
assert sys.dont_write_bytecode
bundle = Path(sys.argv[1]).resolve()
root = bundle / 'Contents/Frameworks'
archive = CArchiveReader(str(bundle/'Contents/MacOS/Lulu')).open_embedded_archive('PYZ.pyz')
class BundledModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if not any(name == p or name.startswith(p+'.') for p in ('src', 'PySide6', 'shiboken6')):
            return None
        if name in archive.toc:
            return importlib.util.spec_from_loader(name, self, is_package=archive.toc[name][0] in (1, 3))
        return None
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        package = archive.toc[module.__name__][0] in (1, 3)
        base = root.joinpath(*module.__name__.split('.'))
        module.__file__ = str(base/'__init__.py' if package else base.with_suffix('.py'))
        if package:
            module.__path__ = [str(base)]
        code = archive.extract(module.__name__)
        if code is not None:
            exec(code, module.__dict__)
sys.meta_path.insert(0, BundledModules())
# Exercise the compiled default key loader with the real frozen layout; injecting
# a public key into UpdateService would miss PyInstaller's Frameworks data links.
host_executable = sys.executable
sys.frozen = True
sys._MEIPASS = str(root)
sys.executable = str(bundle / 'Contents/MacOS/Lulu')
from src.core.updater.service import _default_public_keys
update_keys = _default_public_keys()
if any((bundle / 'Contents/Resources' / name).exists()
       for name in ('update_public_key.pem', 'update_public_keys.json')):
    assert update_keys, 'Bundled default updater public keys cannot be loaded'
import subprocess
for pem in update_keys.values():
    subprocess.run(['/usr/bin/openssl', 'pkey', '-pubin', '-noout'],
                   input=pem.encode(), capture_output=True, check=True, timeout=10)
sys.executable = host_executable
del sys.frozen, sys._MEIPASS
os.environ['QT_PLUGIN_PATH'] = str(root/'PySide6/Qt/plugins')
from src.ui.agent_workspace.runtime import prepare_webengine_runtime
from PySide6.QtCore import QEventLoop, QTimer, QCoreApplication, QEvent, QLocale
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
app = QApplication([])
locale = QLocale().name()
prepare_webengine_runtime()
assert QLocale().name() == locale
class NoNetwork(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        info.block(info.requestUrl().scheme() not in ('data', 'about'))
def tick():
    loop = QEventLoop(); QTimer.singleShot(20, loop.quit); loop.exec()
def until(fn):
    end = time.monotonic()+15
    while time.monotonic() < end:
        if fn(): return
        tick()
    raise RuntimeError('Bundled WebEngine timed out')
def js(page, code):
    result=[]; page.runJavaScript(code, lambda value:result.append(value)); until(lambda:bool(result)); return result[0]
for _ in range(2):
    view = QWebEngineView()
    profile = QWebEngineProfile(view)
    interceptor = NoNetwork(profile); profile.setUrlRequestInterceptor(interceptor)
    assert profile.isOffTheRecord()
    page = QWebEnginePage(profile, view); profile.setParent(page); view.setPage(page)
    loaded=[]; page.loadFinished.connect(loaded.append)
    view.setHtml('<html lang="zh-CN"><meta charset="utf-8"><body><div id="edit" contenteditable="true">中文文稿</div></body></html>')
    until(lambda:bool(loaded)); assert loaded[-1]
    assert js(page, 'document.querySelector("#edit").textContent') == '中文文稿'
    js(page, 'const el=document.getElementById("edit");el.focus();document.execCommand("selectAll");document.execCommand("insertText",false,"中文编辑 😀");')
    assert js(page, 'document.querySelector("#edit").textContent') == '中文编辑 😀'
    view.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete); tick()
lib=ctypes.CDLL(None); lib._dyld_image_count.restype=ctypes.c_uint32
lib._dyld_get_image_name.argtypes=[ctypes.c_uint32]; lib._dyld_get_image_name.restype=ctypes.c_char_p
images=[Path(lib._dyld_get_image_name(i).decode()).resolve() for i in range(lib._dyld_image_count())]
qt=[p for p in images if '.framework/' in str(p) and '/Qt' in str(p)]
assert qt and all(p.is_relative_to(bundle) for p in qt), qt
assert any('QtWebEngineCore.framework' in str(p) for p in qt)
print(json.dumps({'ok':True,'profiles':2,'chinese_edit':True,'native_qt_from_bundle':True,
    'bundled_default_update_keys':list(update_keys),
    'compiled_setup':True,'system_locale_unchanged':True,'scope':'isolated bundled engine, not production entrypoint'}))
'''


def verify(app: Path) -> dict:
    from src.core.agent_entry.delivery_identity import delivery_app_tree_identity, verify_final_signature
    app = app.resolve()
    verify_final_signature(app)
    before = delivery_app_tree_identity(app)
    with tempfile.TemporaryDirectory(prefix='lulu-packaged-webengine-') as directory:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('QT', 'QML', 'DYLD_', 'PYTHON'))}
        env.update(HOME=directory, CFFIXED_USER_HOME=directory, XDG_CACHE_HOME=directory,
                   XDG_CONFIG_HOME=directory, XDG_DATA_HOME=directory,
                   QT_QPA_PLATFORM='offscreen', QTWEBENGINE_DISABLE_SANDBOX='1',
                   QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', PROBE, str(app)],
                                env=env, cwd=directory, capture_output=True, text=True, timeout=60)
    # Always compare even on a failed/crashed probe. Never repair or re-sign a
    # package here: any runtime write invalidates this candidate.
    after = delivery_app_tree_identity(app)
    if before != after:
        raise RuntimeError('网页引擎运行后改变了应用包，停止交付')
    signature = verify_final_signature(app)
    if result.returncode:
        raise RuntimeError('包内网页引擎验证失败：\n' + result.stderr[-3000:])
    return dict(json.loads(result.stdout.strip().splitlines()[-1]),
                immutable_app_tree=after, signature_after_run=signature)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    receipt = verify(args.app)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
    print('WORKSPACE_RUNTIME_OK: 中文编辑、重复打开、包内 Qt、运行后目录与签名均通过')

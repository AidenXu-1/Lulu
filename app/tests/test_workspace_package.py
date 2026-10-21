"""Package regressions: use isolated bundles, never installed data or credentials."""
import ast
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PACKAGING = Path(__file__).resolve().parents[1] / 'packaging'


class WorkspacePackageTests(unittest.TestCase):
    def test_mac_locale_compatibility_is_version_scoped_and_preserves_other_flags(self):
        from PySide6.QtCore import QLocale
        from src.ui.agent_workspace.runtime import prepare_webengine_runtime
        locale = QLocale().name()
        with patch.dict(os.environ, {'QTWEBENGINE_CHROMIUM_FLAGS': '--disable-gpu --lang=zh-CN'}), \
                patch('sys.platform', 'darwin'), \
                patch('PySide6.QtWebEngineCore.qWebEngineVersion', return_value='6.11.1'):
            prepare_webengine_runtime()
            prepare_webengine_runtime()
            self.assertEqual(os.environ['QTWEBENGINE_CHROMIUM_FLAGS'], '--disable-gpu --lang=zh-CN --lang=')
            self.assertEqual(QLocale().name(), locale)
        for platform, version in [('linux', '6.11.1'), ('darwin', '6.11.2')]:
            with self.subTest(platform=platform, version=version), \
                    patch.dict(os.environ, {'QTWEBENGINE_CHROMIUM_FLAGS': '--lang=zh-CN'}), \
                    patch('sys.platform', platform), \
                    patch('PySide6.QtWebEngineCore.qWebEngineVersion', return_value=version):
                prepare_webengine_runtime()
                self.assertEqual(os.environ['QTWEBENGINE_CHROMIUM_FLAGS'], '--lang=zh-CN')

    def test_resource_directory_is_not_a_framework_version(self):
        version = runpy.run_path(str(PACKAGING / 'webengine_collection.py'))['framework_version']
        with tempfile.TemporaryDirectory() as directory:
            framework = Path(directory) / 'QtWebEngineCore.framework'
            (framework / 'Versions/A').mkdir(parents=True)
            (framework / 'Versions/Resources').mkdir()
            (framework / 'Versions/A/QtWebEngineCore').write_bytes(b'binary')
            (framework / 'Versions/Current').symlink_to('A')
            self.assertEqual(version(framework), 'A')
            (framework / 'Versions/B').mkdir()
            (framework / 'Versions/B/QtWebEngineCore').write_bytes(b'another binary')
            with self.assertRaises(RuntimeError):
                version(framework)

    def test_existing_webengine_resources_collect_under_actual_version(self):
        if sys.platform != 'darwin':
            self.skipTest('macOS bundle contract')
        from PyInstaller.utils.hooks.qt import pyside6_library_info
        collect = runpy.run_path(str(PACKAGING / 'webengine_collection.py'))['collect_webengine']
        binaries, datas = collect(pyside6_library_info)
        self.assertEqual(len(binaries), 1)
        self.assertIn('/Versions/A/Helpers/', binaries[0][1])
        self.assertTrue(any(Path(path).name == 'icudtl.dat' for path, _ in datas))
        self.assertTrue(any(Path(path).name == 'zh-CN.pak' for path, _ in datas))
        self.assertTrue(all('/Versions/A/' in destination for _, destination in binaries + datas))

    def test_packaged_assets_require_exact_bytes_and_all_resources(self):
        checker = runpy.run_path(str(PACKAGING / 'verify_workspace_package.py'))
        source = PACKAGING.parent
        verify = checker['verify_assets']
        error = checker['WorkspacePackageError']
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory) / 'Resources'
            for name in ('src/ui/agent_workspace/assets', 'src/ui/assets', 'src/ui/model_settings/assets'):
                shutil.copytree(source / name, resources / name)
            for name in ('src/ui/agent_workspace/index.html', 'src/core/agent_workspace/catalog.json', 'src/ui/model_settings/public.json', 'src/ui/model_settings/information.json'):
                target = resources / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
            self.assertIn('src/ui/model_settings/assets/settings.js', verify(resources, source))
            for relative in ('src/ui/model_settings/assets/settings.js', 'src/ui/model_settings/public.json', 'src/ui/model_settings/information.json',
                             'src/ui/model_settings/assets/logos/bilibili.ico',
                             'src/ui/agent_workspace/assets/workspace.js',
                             'src/ui/agent_workspace/assets/THIRD_PARTY_NOTICES.txt',
                             'src/ui/agent_workspace/index.html', 'src/ui/assets/feishu.png',
                             'src/core/agent_workspace/catalog.json'):
                with self.subTest(relative=relative):
                    path = resources / relative
                    original = path.read_bytes()
                    path.write_bytes(original + b'changed')
                    with self.assertRaises(error):
                        verify(resources, source)
                    path.unlink()
                    with self.assertRaises(error):
                        verify(resources, source)
                    path.symlink_to(source / relative)
                    with self.assertRaises(error):
                        verify(resources, source)
                    path.unlink()
                    path.write_bytes(original)

    def test_webengine_native_dependencies_are_not_pruned(self):
        import PySide6
        framework = Path(PySide6.__file__).parent / 'Qt/lib/QtWebEngineCore.framework/Versions/A/QtWebEngineCore'
        if sys.platform != 'darwin':
            self.skipTest('macOS bundle contract')
        dependencies = subprocess.check_output(['/usr/bin/otool', '-L', str(framework)], text=True)
        spec = ast.parse((PACKAGING / 'Lulu.spec').read_text())
        pruned = next(ast.literal_eval(node.value) for node in spec.body
                      if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == '_QT_UNUSED_FRAMEWORK_MARKERS' for t in node.targets))
        self.assertEqual([name for name in pruned if name in dependencies], [])

    def test_only_exact_webengine_helper_is_an_allowed_executable(self):
        allowed = runpy.run_path(str(PACKAGING / 'profile_gate.py'))['_allowed_nested_macho']
        prefix = 'Contents/Frameworks/PySide6/Qt/lib/QtWebEngineCore.framework/Versions/A/Helpers/'
        helper = prefix + 'QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess'
        self.assertTrue(allowed(helper))
        self.assertFalse(allowed(helper + '-other'))
        self.assertFalse(allowed(helper.replace('QtWebEngineProcess.app', 'Other.app')))
        self.assertFalse(allowed(helper.replace('QtWebEngineCore.framework', 'Other.framework')))
        self.assertFalse(allowed('Contents/MacOS/QtWebEngineProcess'))

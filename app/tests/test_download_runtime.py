"""Anonymous configuration and real spawned-worker recovery without network."""
import io
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from src.core.audio.download_runtime import public_download_options, DownloadFailure, QuietDownloadLogger
from src.core.audio.download_process import download_in_process


def success(url, root, progress, meta, **kwargs):
    path = root/'complete.mp4'; path.write_bytes(b'media')
    meta.update(worker_pid=os.getpid(), mode=kwargs['link_media_mode'])
    progress('download', 1.0)
    return path


def blocked(url, root, progress, meta, **kwargs):
    (root/'pid').write_text(str(os.getpid()))
    time.sleep(30)
    return root/'absent'


def failed(url, root, progress, meta, **kwargs):
    (root/'unfinished.part').write_bytes(b'partial')
    meta['should_not_commit'] = True
    raise RuntimeError('HTTP Error 403: https://media.invalid/?secret=fixture')


def partial(url, root, progress, meta, **kwargs):
    path = root/'unfinished.part'; path.write_bytes(b'partial')
    return path


def blocked_with_child(url, root, progress, meta, **kwargs):
    child = subprocess.Popen([
        sys.executable, "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)",
    ])
    (root / "child-pid").write_text(str(child.pid))
    (root / "pid").write_text(str(os.getpid()))
    time.sleep(30)
    return root / "absent"


def registered_browser(url, root, progress, meta, **kwargs):
    from src.core.collect import macos_browser
    ticket = {"executable": "/fixture/Chrome", "profile": str(root / "browser"),
              "port": 43123, "started": 100}
    macos_browser.launch_observer("browser_launch", ticket)
    (root / "registered").write_text("ready")
    time.sleep(30)


def released_browser(url, root, progress, meta, **kwargs):
    from src.core.collect import macos_browser
    ticket = {"executable": "/fixture/Chrome", "profile": str(root / "browser"),
              "port": 43123, "started": 100}
    macos_browser.launch_observer("browser_launch", ticket)
    macos_browser.launch_observer("browser_released", ticket)
    return success(url, root, progress, meta, **kwargs)


class BrowserOwnershipTests(unittest.TestCase):
    def test_cancel_reaps_os_launched_browser_after_worker_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            'src.core.collect.macos_browser.reap_launch') as reap:
            root = Path(tmp)
            with self.assertRaises(InterruptedError):
                download_in_process('fixture', root, lambda *_: None,
                    is_cancelled=lambda: (root / "registered").exists(),
                    _download=registered_browser, timeout=10)
            reap.assert_called_once()
            self.assertEqual(reap.call_args.args[0]['profile'], str(root.resolve() / 'browser'))

    def test_closed_browser_is_not_reaped_again_after_success(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
            'src.core.collect.macos_browser.reap_launch') as reap:
            result = download_in_process('fixture', tmp, lambda *_: None,
                                         _download=released_browser, timeout=10)
            self.assertTrue(result.is_file())
            reap.assert_not_called()


class DownloadRuntimeTests(unittest.TestCase):
    def test_public_options_do_not_read_cookies_or_fetch_remote_scripts(self):
        with patch('src.core.audio.download_runtime.deno_path', return_value=Path('/bundled/deno')):
            options = public_download_options('https://youtu.be/example')
        self.assertIsNone(options['cookiesfrombrowser'])
        self.assertIsNone(options['cookiefile'])
        self.assertFalse(options['usenetrc'])
        self.assertEqual(options['remote_components'], set())
        self.assertEqual(options['js_runtimes'], {'deno': {'path': '/bundled/deno'}})

    def test_bundled_runtime_missing_never_falls_back_to_machine_node(self):
        from src.core.audio.download_runtime import deno_path
        with tempfile.TemporaryDirectory() as tmp, patch('sys.frozen', True, create=True), patch('sys._MEIPASS', tmp, create=True):
            with self.assertRaises(DownloadFailure) as caught: deno_path()
        self.assertEqual(caught.exception.code, 'CORE_DOWNLOAD_RUNTIME_MISSING')

    def test_provider_messages_are_not_printed_or_carried_in_error(self):
        out = io.StringIO()
        with redirect_stderr(out), redirect_stdout(out):
            logger = QuietDownloadLogger()
            for fn in (logger.error, logger.warning, logger.debug): fn('secret-cookie signed-url')
        self.assertEqual(out.getvalue(), '')
        error = DownloadFailure.from_exception(RuntimeError('403 secret-cookie signed-url'))
        self.assertEqual(error.code, 'CORE_DOWNLOAD_ACCESS_DENIED')
        self.assertNotIn('secret', str(error))


class DownloadProcessTests(unittest.TestCase):
    def assert_dead(self, pid):
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_result_is_committed_only_after_worker_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {'setting': 'kept'}
            result = download_in_process('fixture', Path(tmp), lambda *_: None, meta,
                                         link_media_mode='video', _download=success)
            self.assertEqual(result.read_bytes(), b'media')
            self.assertEqual(meta['setting'], 'kept')
            self.assertEqual(meta['mode'], 'video')
            self.assert_dead(meta['worker_pid'])

    def test_failure_does_not_poison_next_attempt_or_commit_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {}
            with self.assertRaises(DownloadFailure) as caught:
                download_in_process('fixture', Path(tmp), lambda *_: None, meta, _download=failed)
            self.assertEqual(caught.exception.code, 'CORE_DOWNLOAD_ACCESS_DENIED')
            self.assertEqual(meta, {})
            self.assertTrue(download_in_process('fixture', Path(tmp), lambda *_: None, _download=success).is_file())

    def test_cancel_during_blocked_network_reaps_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); started = time.monotonic()
            with self.assertRaises(InterruptedError):
                download_in_process('fixture', root, lambda *_: None,
                                    is_cancelled=lambda: (root/'pid').exists(), _download=blocked)
            self.assertLess(time.monotonic()-started, 3)
            self.assert_dead(int((root/'pid').read_text()))
            self.assertTrue(download_in_process('fixture', root, lambda *_: None, _download=success).exists())

    def test_timeout_reaps_worker_and_next_attempt_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(DownloadFailure) as caught:
                download_in_process('fixture', root, lambda *_: None, timeout=1, _download=blocked)
            self.assertEqual(caught.exception.code, 'CORE_DOWNLOAD_TIMEOUT')
            if (root/'pid').exists(): self.assert_dead(int((root/'pid').read_text()))
            self.assertTrue(download_in_process('fixture', root, lambda *_: None, _download=success).exists())

    def test_partial_file_cannot_be_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DownloadFailure) as caught:
                download_in_process('fixture', Path(tmp), lambda *_: None, _download=partial)
            self.assertEqual(caught.exception.code, 'CORE_DOWNLOAD_INCOMPLETE')

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_cancellation_stops_owned_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(InterruptedError):
                download_in_process(
                    "fixture", root, lambda *_: None,
                    is_cancelled=lambda: (root / "pid").exists(),
                    _download=blocked_with_child,
                )
            self.assert_dead(int((root / "pid").read_text()))
            child_pid = int((root / "child-pid").read_text())
            for _ in range(30):
                status = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(child_pid)],
                    capture_output=True, text=True,
                ).stdout.strip()
                if not status or status.startswith("Z"):
                    break
                time.sleep(0.05)
            self.assertTrue(not status or status.startswith("Z"), status)

    def test_cancellation_exception_keeps_pipeline_identity(self):
        from src.core.pipeline import _Cancelled, was_cancelled
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def check():
                if (root / "pid").exists():
                    raise _Cancelled()
                return False
            with self.assertRaises(_Cancelled) as caught:
                download_in_process("fixture", root, lambda *_: None,
                                    is_cancelled=check, _download=blocked)
            self.assertTrue(was_cancelled(caught.exception))
            self.assert_dead(int((root / "pid").read_text()))

    def test_worker_waits_for_parent_ownership_before_starting_download(self):
        import multiprocessing
        from src.core.audio.download_process import _worker, _stop
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe(duplex=False)
            gate = context.Event()
            process = context.Process(target=_worker, args=(
                child, gate, "fixture", str(root), "audio", {}, blocked_with_child,
            ))
            process.start()
            child.close()
            pid = process.pid
            try:
                self.assertTrue(parent.poll(3))
                self.assertIn(b'"ready"', parent.recv_bytes())
                time.sleep(0.1)
                self.assertFalse((root / "pid").exists())
                self.assertFalse((root / "child-pid").exists())
            finally:
                _stop(process, True)
                process.close()
                parent.close()
            self.assert_dead(pid)


class DownloadPackagingTests(unittest.TestCase):
    def test_qt_filter_removes_relocated_aliases_with_their_framework(self):
        import ast
        spec = Path(__file__).resolve().parents[1] / "packaging/Lulu.spec"
        tree = ast.parse(spec.read_text())
        selected = [node for node in tree.body if
                    isinstance(node, ast.FunctionDef) and node.name == "_drop_toc_entries"
                    or isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "_QT_UNUSED_FRAMEWORK_MARKERS"
                        for target in node.targets)]
        scope = {"os": os}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(spec), "exec"), scope)
        unused = [
            ("QtPdf.framework/Versions/A/QtPdf", "/site/QtPdf.framework/Versions/A/QtPdf", "BINARY"),
            ("QtPdf", "QtPdf.framework/Versions/A/QtPdf", "SYMLINK"),
            ("QtPdf.framework/QtPdf", "Versions/Current/QtPdf", "SYMLINK"),
            ("QtPdf.framework/Versions/Current", "A", "SYMLINK"),
        ]
        needed = [
            # Required by WebEngine even though their Python APIs are excluded.
            ("QtQml.framework/Versions/A/QtQml", "/site/PySide6/Qt/lib/QtQml.framework/Versions/A/QtQml", "BINARY"),
            ("QtQml", "QtQml.framework/Versions/A/QtQml", "SYMLINK"),
            ("QtQml.framework/QtQml", "Versions/Current/QtQml", "SYMLINK"),
            ("QtQml.framework/Versions/Current", "A", "SYMLINK"),
            ("PySide6/Qt/lib/QtQuick.framework/Versions/A/QtQuick", "/site/QtQuick.framework/Versions/A/QtQuick", "BINARY"),
            ("QtCore", "QtCore.framework/Versions/A/QtCore", "SYMLINK"),
            ("QtMultimedia.framework/Versions/A/QtMultimedia", "/site/QtMultimedia", "BINARY"),
            ("download_runtime/deno", "/build/bin/deno", "BINARY"),
        ]
        self.assertEqual(scope["_drop_toc_entries"](unused + needed, scope["_QT_UNUSED_FRAMEWORK_MARKERS"]), needed)

    def test_only_registered_download_runtime_is_allowed(self):
        import runpy
        gate = runpy.run_path(str(Path(__file__).resolve().parents[1] / "packaging/profile_gate.py"))
        allowed = gate["_allowed_nested_macho"]
        self.assertTrue(allowed("Contents/Frameworks/download_runtime/deno"))
        self.assertFalse(allowed("Contents/Frameworks/download_runtime/other"))
        self.assertFalse(allowed("Contents/Frameworks/arbitrary/deno"))

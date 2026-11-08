from __future__ import annotations

import subprocess
import sys
import queue
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.core.recording import RecordingRequest, RecordingSource
from src.core.recording.macos_system_audio import (
    MacOSSystemAudioBackend,
    MacOSSystemAudioSession,
    RecordingBackendError,
)
from src.core.recording import macos_system_audio as audio


class _FakeProcess:
    def __init__(self, returncode: int = 0, timeout: bool = False):
        self.returncode = returncode
        self.timeout = timeout
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.timeout = False

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.timeout = False


class MacOSSystemAudioSessionTests(unittest.TestCase):
    def _session(self, root: Path, process: _FakeProcess):
        output = root / "system.m4a"
        stop_file = root / "system.stop"
        request = RecordingRequest(source=RecordingSource.SYSTEM_AUDIO, output_path=output)
        return MacOSSystemAudioSession(request, process, stop_file, stdout_queue=__import__("queue").Queue())



    def test_unreadable_container_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            session=self._session(Path(tmp),_FakeProcess())
            session.request.output_path.write_bytes(b'broken-container')
            with mock.patch.object(audio,'_decode_to_wav',return_value=False):
                with self.assertRaisesRegex(RecordingBackendError,'无法读取'):session.stop()
            self.assertFalse(session.request.output_path.exists())




class SystemAudioStartupTests(unittest.TestCase):
    def test_capture_clock_survives_messages_before_or_after_start_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); helper = root / 'clock-helper'; output = root / 'system.m4a'
            request = RecordingRequest(source=RecordingSource.SYSTEM_AUDIO, output_path=output)
            for lines in (('capture_started_monotonic=100.25', 'started'),
                          ('started', 'capture_started_monotonic=100.25')):
                helper.write_text('#!' + sys.executable + '\nimport sys,time\nfrom pathlib import Path\n'
                    'Path(sys.argv[1]).write_bytes(b"fixture")\n'
                    + '\n'.join('print(' + repr(line) + ',flush=True)' for line in lines)
                    + '\nwhile not Path(sys.argv[2]).exists():time.sleep(.01)\n')
                helper.chmod(0o700)
                with mock.patch.object(audio, 'ensure_macos_system_audio_helper', return_value=helper):
                    session = MacOSSystemAudioBackend().start(request)
                with mock.patch.object(audio, '_decode_to_wav', return_value=True), mock.patch.object(
                    audio, '_wav_stats', return_value={'duration': 2, 'rms': 1000, 'peak': 3000}):
                    result = session.stop()
                self.assertEqual(result.metadata['capture_started_monotonic'], 100.25)

    def test_installed_component_does_not_require_developer_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=root/'recording_runtime/SystemAudioCapture'
            binary.parent.mkdir();binary.write_bytes(b'fixture');binary.chmod(0o700)
            with mock.patch.object(sys,'frozen',True,create=True), mock.patch.object(sys,'_MEIPASS',str(root),create=True), mock.patch.object(audio.shutil,'which',return_value=None) as search, mock.patch.object(audio.platform,'system',return_value='Darwin'):
                self.assertEqual(audio.ensure_macos_system_audio_helper(),binary)
                self.assertTrue(audio.is_macos_system_audio_available());search.assert_not_called()

    def test_incomplete_installed_app_never_compiles_on_user_machine(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sys,'frozen',True,create=True), mock.patch.object(sys,'_MEIPASS',tmp,create=True), mock.patch.object(audio.subprocess,'run') as run:
            with self.assertRaisesRegex(RecordingBackendError,'重新安装'):
                audio.ensure_macos_system_audio_helper()
            run.assert_not_called()

    def test_actual_failed_child_reports_permission_or_storage_and_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);helper=root/'helper';output=root/'partial.m4a'
            request=RecordingRequest(source=RecordingSource.SYSTEM_AUDIO,output_path=output)
            for domain,code,expected in [('com.apple.ScreenCaptureKit.SCStreamErrorDomain',-3801,'系统拒绝'),('NSPOSIXErrorDomain',28,'空间不足'),('NSCocoaErrorDomain',513,'目录不可写'),('com.apple.ScreenCaptureKit.SCStreamErrorDomain',-3802,'错误码 -3802')]:
                with self.subTest(code=code):
                    line='capture_error: '+json.dumps({'domain':domain,'code':code})
                    helper.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_bytes(b"partial")\nprint('+repr(line)+',file=sys.stderr,flush=True)\nsys.exit(1)\n')
                    helper.chmod(0o700)
                    with mock.patch.object(audio,'ensure_macos_system_audio_helper',return_value=helper):
                        with self.assertRaisesRegex(RecordingBackendError,expected) as error:
                            MacOSSystemAudioBackend().start(request)
                    if code!=-3801:self.assertNotIn('权限',str(error.exception))
                    self.assertFalse(output.exists());self.assertFalse(output.with_suffix('.stop').exists())

    def test_stop_failure_uses_native_reason_and_never_claims_permission_for_disk_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'system.m4a';output.write_bytes(b'partial')
            errors=queue.Queue();errors.put('capture_error: {"domain":"NSPOSIXErrorDomain","code":28}')
            session=MacOSSystemAudioSession(RecordingRequest(source=RecordingSource.SYSTEM_AUDIO,output_path=output),_FakeProcess(1),root/'system.stop',queue.Queue(),errors)
            with self.assertRaisesRegex(RecordingBackendError,'空间不足'):session.stop()
            self.assertFalse(output.exists())

    def test_both_recording_surfaces_preserve_specific_backend_error(self):
        from src.ui.main_window import MainWindow
        from src.ui.voice_page import _VoiceRecordingDialog
        error=RecordingBackendError('磁盘空间不足，系统声音录音未保存。')
        self.assertEqual(MainWindow._friendly_system_recording_error(None,error),str(error))
        self.assertEqual(_VoiceRecordingDialog._friendly_recording_error(None,error),str(error))


if __name__ == "__main__":
    unittest.main()

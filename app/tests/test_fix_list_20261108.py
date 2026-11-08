"""Listed review regressions only; isolated files, fake capture, no accounts/devices."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QObject, QTimer, Signal, QCoreApplication, QEvent
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication, QLabel, QWidget
from src.ui import theme as T
from src.ui.voice_page import _VoiceRecordingDialog
from src.ui.collection_history_dialog import CollectionHistoryDialog
from src.core.collect.history import CollectionHistoryRepository, HistoryError
from tests import test_collection_history as history_fixture
from tests import test_settings_skill_navigation as web_fixture


class FakeMic(QObject):
    tick = Signal(int)
    finished = Signal(str)
    failed = Signal(str)
    def __init__(self):
        super().__init__(); self.started = []; self.active = False
    def has_input(self): return True
    def is_recording(self): return self.active
    def start(self, path): self.started.append(path); self.active = True
    def stop(self): self.active = False


class PaintProbe(QWidget):
    def __init__(self):
        super().__init__(); self.paints = 0
    def paintEvent(self, event):
        self.paints += 1; super().paintEvent(event)


class VoiceStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = PaintProbe(); self.parent._consumer = T.consumer_theme('warm')
        self.parent.resize(800,600); self.parent.show()
        self.dialog = _VoiceRecordingDialog(self.parent, purpose='voice_sample')
        self.dialog._recording_path = lambda prefix: self.root / (prefix+'.m4a')
        self.dialog.show(); self.app.processEvents()
        self.timers = []

    def timer(self, milliseconds, call, single=False):
        timer = QTimer(self.parent); timer.setSingleShot(single)
        timer.timeout.connect(call); timer.start(milliseconds); self.timers.append(timer)
        return timer

    def tearDown(self):
        for timer in self.timers: timer.stop()
        self.dialog._timer.stop()
        self.dialog.close(); self.parent.close()
        self.dialog.deleteLater(); self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

    def _slow_start(self, source, cancel):
        self.dialog._set_source(source); calls=[]; cancelled=[]; events=[]; ticks=[]
        mic=FakeMic(); paths=[]
        def start(request):
            calls.append(time.monotonic()); paths.append(request.output_path)
            request.output_path.write_bytes(b'partial fake audio')
            request.output_path.with_suffix('.stop').write_text('fixture')
            time.sleep(2)
            return SimpleNamespace(is_active=lambda:True,cancel=lambda:cancelled.append(True))
        started=time.monotonic(); paints=self.parent.paints
        self.timer(100,lambda:(ticks.append(time.monotonic()),self.parent.update()))
        def interact():
            events.append((time.monotonic()-started,self.dialog.status.text(),
                           self.dialog.start_btn.isEnabled(),self.dialog.cancel_btn.isEnabled()))
            self.dialog._toggle(); self.dialog._set_source('microphone')
            if cancel:
                (self.dialog.reject if cancel == 'close' else self.dialog._cancel)()
        interaction=self.timer(200,interact,True)
        with patch('src.core.recording.MacOSSystemAudioBackend',return_value=SimpleNamespace(start=start)), \
                patch('src.ui.recorder.MicRecorder',return_value=mic):
            self.dialog._start()
            interaction.stop()
            for timer in self.timers: timer.stop()
        elapsed=time.monotonic()-started
        print(json.dumps({'case':'N9','source':source,'cancel':cancel,'elapsed':round(elapsed,3),
                         'timer_ticks':len(ticks),'paints':self.parent.paints-paints,
                         'interaction_seconds':events[0][0] if events else None,'sessions':len(calls)},ensure_ascii=False),flush=True)
        self.assertTrue(events,'GUI callback was blocked until backend returned')
        self.assertLess(events[0][0],1.5); self.assertGreaterEqual(len(ticks),8)
        self.assertGreater(self.parent.paints,paints)
        self.assertIn('正在准备录音',events[0][1]); self.assertFalse(events[0][2]); self.assertTrue(events[0][3])
        self.assertEqual(len(calls),1); self.assertEqual(self.dialog._source,source)
        if cancel:
            self.assertEqual(cancelled,[True]); self.assertFalse(mic.started)
            self.assertFalse(self.dialog.result_path); self.assertFalse(self.dialog._timer.isActive())
            self.assertEqual(list(self.root.iterdir()),[])
        else:
            self.assertIsNotNone(self.dialog._system_session)
            self.assertTrue(self.dialog.start_btn.isEnabled())
            self.dialog._cancel()

    def test_system_start_keeps_paint_ticks_and_cancel_alive(self): self._slow_start('system_audio',True)
    def test_mixed_start_keeps_cancel_alive_and_never_starts_microphone_after_cancel(self): self._slow_start('mixed',True)
    def test_start_prevents_reentry_and_source_change_then_can_stop(self): self._slow_start('system_audio',False)
    def test_close_while_preparing_waits_for_session_cleanup(self): self._slow_start('system_audio','close')

    def test_real_backend_timeout_with_fake_child_cleans_process_and_files(self):
        from src.core.recording import macos_system_audio as backend
        self.dialog._set_source('system_audio')
        script=self.root/'fake_capture.py'
        script.write_text("import pathlib,sys,time\npathlib.Path(sys.argv[1]).write_bytes(b'partial')\npathlib.Path(sys.argv[2]).write_text('partial')\ntime.sleep(30)\n")
        real_popen=subprocess.Popen; children=[]; ticks=[]
        def spawn(argv,**kwargs):
            child=real_popen([sys.executable,str(script),*argv[1:]],**kwargs); children.append(child); return child
        self.timer(100,lambda:ticks.append(True)); started=time.monotonic()
        try:
            with patch.object(backend,'ensure_macos_system_audio_helper',return_value=script), \
                    patch.object(backend.platform,'system',return_value='Darwin'), \
                    patch.object(backend.subprocess,'Popen',side_effect=spawn):
                self.dialog._start()
        finally:
            for timer in self.timers: timer.stop()
            for child in children:
                if child.poll() is None: child.terminate(); child.wait(timeout=3)
        elapsed=time.monotonic()-started
        print(json.dumps({'case':'N9 timeout','elapsed':round(elapsed,3),'ticks':len(ticks),
                          'remaining_children':sum(c.poll() is None for c in children),
                          'remaining_audio':len(list(self.root.glob('*.m4a')))},ensure_ascii=False),flush=True)
        self.assertGreaterEqual(len(ticks),40); self.assertLess(elapsed,16)
        self.assertEqual(self.dialog.status.text(),'系统声音录制启动超时，已停止本次尝试。请重试或重新打开 Lulu。')
        self.assertFalse(list(self.root.glob('*.m4a'))); self.assertFalse(list(self.root.glob('*.stop')))
        self.assertFalse(self.dialog.result_path); self.assertTrue(self.dialog.start_btn.isEnabled())


class HistoryFontTests(history_fixture.HistoryCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def test_overlay_text_scales_without_clipping_or_accumulation(self):
        T.apply_fonts(self.app,100); self.app.processEvents()
        snapshot=self.add(); parent=QWidget(); parent.resize(1100,800); parent.show()
        dialog=CollectionHistoryDialog(self.repo,snapshot,parent=parent); dialog.open()
        names=['historyDialogTitle','historyRecordName','historyRecordAuthor','historyRecordCount','historyRecordTime']
        measurements={}
        try:
            for percent in [100,130,90,130,100]:
                T.apply_fonts(self.app,percent); self.app.processEvents(); self.app.processEvents()
                labels=[dialog.findChild(QLabel,name) for name in names]+[dialog.time_heading]
                sizes=[QFontInfo(w.font()).pixelSize() for w in labels]
                print(json.dumps({'case':'N1','percent':percent,'pixels':sizes},ensure_ascii=False),flush=True)
                if percent in measurements:self.assertEqual(sizes,measurements[percent])
                measurements[percent]=sizes
                for widget in labels:
                    self.assertGreaterEqual(widget.contentsRect().height(),widget.fontMetrics().height())
                for widget in (labels[0],labels[3],labels[4],labels[5]):
                    self.assertGreaterEqual(widget.contentsRect().width(),widget.fontMetrics().horizontalAdvance(widget.text()),widget.objectName() or widget.text())
                if percent!=100:
                    expected=[round(n*percent/100) for n in measurements[100]]
                    self.assertEqual(sizes,expected)
            self.assertEqual(measurements[100],[19,14,11,12,11,13])
        finally:
            dialog.reject(); dialog.deleteLater(); parent.close(); parent.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            T.apply_fonts(self.app,100); self.app.processEvents()


class HistoryMoveRecoveryTests(history_fixture.HistoryCase):
    def test_receipt_or_post_commit_failure_never_duplicates_move_or_loses_put_back(self):
        from src.core.collect import history as history_module
        for phase in ('receipt','post_commit','pending_cleanup'):
            with self.subTest(phase=phase):
                self.repo=CollectionHistoryRepository(Path(self.temp.name)/phase/'history.json')
                initial=self.add([history_fixture.item(phase)]); key=initial.selected_record_id
                trash=Path(self.temp.name)/(phase+'-trash'); trash.mkdir(); moved=[]
                def move(path): moved.append(path.name); path.rename(trash/path.name)
                if phase=='receipt':
                    original=history_module._replace_file
                    def fail(source,target):
                        if target.suffix=='.sha256':raise PermissionError('injected receipt failure')
                        return original(source,target)
                    injection=patch.object(history_module,'_replace_file',side_effect=fail)
                elif phase=='post_commit':
                    original=self.repo._store
                    def fail(*args,**kwargs):
                        original(*args,**kwargs); raise HistoryError('disk_full')
                    injection=patch.object(self.repo,'_store',side_effect=fail)
                else:
                    original=history_module._unlink
                    def fail(path):
                        if path.suffix=='.pending':raise PermissionError('injected cleanup failure')
                        return original(path)
                    injection=patch.object(history_module,'_unlink',side_effect=fail)
                with injection:
                    with self.assertRaises(HistoryError) as caught:
                        self.repo.move_records_to_system_trash([key],expected_source_hash=initial.source_hash,move=move)
                self.assertEqual(caught.exception.code,'system_trash_pending')
                settled=CollectionHistoryRepository(self.repo.path).load()
                self.assertTrue(settled.writable); self.assertFalse(settled.active_records)
                self.assertEqual(len(moved),1); self.assertEqual(len(list(trash.iterdir())),1)
                self.assertFalse(list(self.repo.returned_packages.glob('*.pending')))
                package=next(trash.iterdir()); package.rename(self.repo.returned_packages/package.name)
                self.assertEqual(self.repo.load().selected_record_id,key)

    def test_successful_system_move_then_failed_index_is_visible_and_resumes_without_duplicate(self):
        target=self.add([history_fixture.item('move')]).selected_record
        initial=self.add([history_fixture.item('keep')]); keep=initial.selected_record
        media=self.path.parent/'original.mp4'; media.write_bytes(b'original media')
        document=self.path.parent/'original.md'; document.write_bytes(b'original document')
        fake_trash=Path(self.temp.name)/'fake-trash'; fake_trash.mkdir(); moved=[]
        def move(path): moved.append(path.name); path.rename(fake_trash/path.name)
        with patch.object(CollectionHistoryRepository,'_store',side_effect=HistoryError('disk_full')):
            with self.assertRaises(HistoryError) as caught:
                self.repo.move_records_to_system_trash([target.record_id],expected_source_hash=initial.source_hash,move=move)
            reopened=CollectionHistoryRepository(self.path,legacy_path=self.legacy_path).load()
        print(json.dumps({'case':'N2 before retry','error':caught.exception.code,'state':reopened.code,
                          'writable':reopened.writable,'packages':len(moved)},ensure_ascii=False),flush=True)
        self.assertEqual(caught.exception.code,'system_trash_pending')
        self.assertEqual(reopened.code,'system_trash_pending'); self.assertFalse(reopened.writable)
        self.assertIn('重试',CollectionHistoryDialog._error_text(reopened.code))
        finished=CollectionHistoryRepository(self.path,legacy_path=self.legacy_path).load()
        self.assertTrue(finished.writable); self.assertEqual(finished.active_records,(keep,))
        self.assertEqual(finished.trashed_records,initial.trashed_records)
        self.assertEqual(len(list(fake_trash.glob('*.lulucollection'))),1); self.assertEqual(len(moved),1)
        self.assertEqual(media.read_bytes(),b'original media'); self.assertEqual(document.read_bytes(),b'original document')
        # Finder Put Back still restores the moved record after the interrupted operation.
        package=next(fake_trash.iterdir()); package.rename(self.repo.returned_packages/package.name)
        restored=self.repo.load(); self.assertEqual({r.record_id for r in restored.active_records},{target.record_id,keep.record_id})
        print(json.dumps({'case':'N2 recovered','packages_created':len(moved),'active_after_settle':len(finished.active_records),
                          'active_after_put_back':len(restored.active_records),'original_media_unchanged':True}),flush=True)


class SettingsNoticeTests(web_fixture.SettingsSkillNavigationTests):
    # Only explicitly named methods below are registered in the build gate.
    def setUp(self):
        from src.config import AppConfig
        from src.ui.approved_settings import ApprovedSettings
        self.page=ApprovedSettings(AppConfig(auto_update_enabled=False,notification_policy='never'),
                                   agent_skill_status_provider=lambda:'not_installed',lazy_categories=True)
        self.page.resize(960,760); self.page.show()
        self.until(lambda:self.page.approved.bridge.is_ready)
    def tearDown(self):
        self.page.shutdown_workers(); self.page.close(); self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete); self.app.processEvents()

    def test_about_trust_notice_is_visible_and_separate_from_privacy(self):
        self.navigate('about')
        self.until(lambda:bool(self.js("document.querySelector('#software-update')!==null")))
        value=self.js("document.querySelector('#distribution-notice')?.textContent || ''")
        self.assertIn('未通过 Apple 公证',value); self.assertIn('未使用 Apple Developer ID',value)
        self.assertNotIn('本地转录',value)
        self.assertTrue(self.js("document.querySelector('#distribution-notice')?.getBoundingClientRect().height>0"))

    def test_experience_skill_status_does_not_claim_cli_disconnected(self):
        self.js("state.meta.experience=true;state.scenes.cli='idle';state.cliReady=false;renderCurrent();void 0")
        self.navigate('cli')
        text=self.js("document.querySelector('#cli-connection').textContent")
        self.assertNotIn('尚未连接',text); self.assertIn('受管 Skill',text)
        self.assertEqual(self.js("document.querySelector('[data-action=check-cli]').textContent.trim()"),'检查受管 Skill')
        self.js("state.meta.experience=false;renderCurrent();void 0")
        self.assertIn('尚未连接',self.js("document.querySelector('#cli-connection').textContent"))
        self.assertEqual(self.js("document.querySelector('[data-action=check-cli]').textContent.trim()"),'连接检查')

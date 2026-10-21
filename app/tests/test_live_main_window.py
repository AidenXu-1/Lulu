"""Real native sidebar/page integration using the existing isolated GUI fixture."""
from pathlib import Path
import os
from types import SimpleNamespace
import unittest

from tests.test_extraction_main_window import ExtractionMainWindowTests
from tests.test_phase5_job_ledger import FakeIntegrityAnchorStore
from src.core.job_ledger.store import JobLedgerStore
from src.core.job_ledger.service import JobControlService
from src.core.live_recording.contracts import Observation
from src.core.live_recording.service import LiveService, uid

class LiveMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ExtractionMainWindowTests.setUpClass()

    def test_completed_live_media_creates_visible_local_text_task(self):
        fixture = ExtractionMainWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        main = fixture.main
        media = fixture.root / 'completed.mp4'
        media.write_bytes(b'isolated handoff fixture')
        main.stack.setCurrentIndex(1)
        main._live_transcribe(str(media))
        self.assertEqual(len(main.tasks), 1)
        self.assertEqual(main.tasks[0].source, str(media))
        self.assertEqual(main.tasks[0].execution_policy.requested_outputs, ('text',))
        self.assertEqual(main.stack.currentIndex(), 0)

    def test_live_ts_task_requires_local_choice_when_global_preference_is_cloud(self):
        fixture = ExtractionMainWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        media = fixture.root / 'completed.ts'
        media.write_bytes(b'isolated handoff fixture')
        fixture.main.cfg.engine_name = 'cloud-fixture'
        self.assertFalse(fixture.main._live_transcribe(str(media)))
        self.assertEqual(fixture.main.tasks, [])
        self.assertEqual(fixture.main.cfg.engine_name, 'cloud-fixture')

    def test_live_task_preparation_failure_does_not_create_a_task(self):
        fixture = ExtractionMainWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        media = fixture.root / 'completed.ts'
        media.write_bytes(b'isolated handoff fixture')
        fixture.main._install_preparation_error = '准备失败，请重新打开'
        self.assertFalse(fixture.main._live_transcribe(str(media)))
        self.assertEqual(fixture.main.tasks, [])

    def test_native_sidebar_page_keeps_paper_and_old_navigation(self):
        fixture = ExtractionMainWindowTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        main = fixture.main
        service = LiveService(fixture.root/'live',JobControlService(JobLedgerStore(fixture.root/'ledger',FakeIntegrityAnchorStore())),
            'fixture',provider=SimpleNamespace(check=lambda *_:Observation('offline')),
            secret_read=lambda:self.fail('credentials accessed'),secret_write=lambda _:None,
            disk_free=lambda _:100*1024**3)
        output = fixture.root/'live-recordings'
        output.mkdir()
        service.index.set_setting('directory',str(output))
        main.live_controller.dispatcher = service.handle
        main.live_controller.start()
        fixture.until(lambda:main.live_controller.connected)
        main._switch_page(9)
        fixture.until(lambda:main.live_recording_page.data)
        self.assertEqual(main.stack.currentIndex(),9)
        self.assertTrue(main.center_wrap.property('paperTextureActive'))
        self.assertTrue(main.titlebar_paper_center.property('paperTextureActive'))
        self.assertIs(main._ensure_page_loaded(8),main.stack.widget(0))
        self.assertEqual(main.stack.count(),10)
        # Actual native window render. Fixtures contain no real user data or credentials.
        proof = os.environ.get('LULU_LIVE_PROOF_PATH')
        if proof:
            main.grab().save(proof)
        main._switch_page(0)
        self.assertIs(main.extraction_workspace,fixture.widget)
        main.live_controller.request_stop()
        fixture.until(lambda:main.live_controller.settled and main.live_controller.worker is None)
        service.shutdown()

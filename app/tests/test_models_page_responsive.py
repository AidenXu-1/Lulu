"""Disk probes must not block navigation, and repeated refreshes are coalesced."""
import threading,time
from PySide6.QtCore import QTimer
from tests.model_settings_fixtures import ControllerCase, m
from src.ui.model_settings.widget import ModelsPage
from types import SimpleNamespace

class ModelPageResponsiveTests(ControllerCase):
    def test_cloud_handoff_preserves_native_tab_content_pair(self):
        calls=[];page=SimpleNamespace(send=lambda *args:calls.append(args))
        for index in (0,1,2):ModelsPage.show_purpose(page,index)
        ModelsPage.show_purpose(page,0,1)
        self.assertEqual(calls,[('purpose',{'purpose':'extract'}),('purpose',{'purpose':'voice'}),('purpose',{'purpose':'assistant'}),('purpose',{'purpose':'extract','localMode':'cloud'})])
        self.service.execute.assert_not_called()

    def test_slow_runtime_probe_is_background_and_shutdown_is_owned(self):
        release=threading.Event();started=threading.Event();threads=[];states=[]
        def slow():threads.append(threading.get_ident());started.set();release.wait(3);return {'read':len(threads)}
        self.c._read_state=slow
        self.c.refresh=lambda:m.ModelController.refresh(self.c)
        self.c.changed.connect(states.append)
        then=time.monotonic();self.c.refresh();self.assertLess(time.monotonic()-then,.3)
        try:
            self.assertTrue(started.wait(1));self.c.refresh();self.c.refresh()
            ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));self.app.processEvents()
            self.assertTrue(ticks);self.assertEqual(len(threads),1);self.assertNotEqual(threads[0],threading.get_ident())
        finally:release.set();self.wait_for(lambda:not self.c.owned_workers())
        self.assertEqual(len(threads),2)
        self.assertEqual([x['read'] for x in states],[2])

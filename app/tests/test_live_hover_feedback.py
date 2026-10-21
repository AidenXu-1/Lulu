"""Pixel-level native pointer checks for the existing live workspace."""
import os
from pathlib import Path
import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from tests import test_live_recording_ui as fixtures
from tests import test_live_data_ui as data_fixtures


class LiveHoverTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.NativeLiveTests.setUpClass.__func__)
    setUp = fixtures.NativeLiveTests.setUp
    tearDown = fixtures.NativeLiveTests.tearDown
    until = fixtures.NativeLiveTests.until
    seed = fixtures.NativeLiveTests.seed
    completed_segment = fixtures.NativeLiveTests.completed_segment
    opened = data_fixtures.NativeDataTests.opened

    def assert_feedback(self, control):
        self.assertTrue(control.isVisible(), control.text() + ' fixture must show the control')
        self.assertTrue(control.isEnabled(), control.text() + ' fixture must enable the control')
        surface = control.window()
        surface.activateWindow()
        QTest.qWait(20)
        def move(widget, point):
            QTest.mouseMove(surface.windowHandle(), widget.mapTo(surface, point))
            QTest.qWait(20)
        control.clearFocus()
        move(surface, QPoint(1, 1))
        self.app.processEvents()
        before = control.grab().toImage()
        move(control, control.rect().center())
        self.app.processEvents()
        self.assertTrue(control.underMouse(), repr((control.text(), control.geometry(), surface.geometry())))
        hovered = control.grab().toImage()
        self.assertNotEqual(before, hovered, control.text() + ' has no hover feedback')
        def light_text_pixels(image):
            # The right side contains the caption, excluding the leading icon.
            return sum(min(image.pixelColor(x, y).getRgb()[:3]) > 235
                       for x in range(image.width() // 3, image.width() - 5)
                       for y in range(5, image.height() - 5))
        if control.property('role') == 'primary':
            self.assertGreater(light_text_pixels(before), 10)
            self.assertGreater(light_text_pixels(hovered), light_text_pixels(before) * .7,
                               'primary hover lost its light, readable caption')
        proof = os.environ.get('LULU_LIVE_FEEDBACK_DIR')
        if proof:
            root = Path(proof)
            root.mkdir(parents=True, exist_ok=True)
            label = control.accessibleName() or control.text()
            label = ''.join(c if c.isalnum() else '_' for c in label)
            before.save(str(root / (label + '-normal.png')))
            hovered.save(str(root / (label + '-hover.png')))
        move(surface, QPoint(1, 1))
        self.app.processEvents()
        self.assertEqual(before, control.grab().toImage(), control.text() + ' does not restore')
        # A held press followed by release outside must give feedback without
        # triggering the actual browser, file action or dialog close.
        control.setFocus()
        move(control, control.rect().center())
        self.app.processEvents()
        focused_hover = control.grab().toImage()
        QTest.mousePress(control, Qt.LeftButton)
        self.app.processEvents()
        pressed = control.grab().toImage()
        self.assertNotEqual(focused_hover, pressed, control.text() + ' has no press feedback')
        if control.property('role') == 'primary':
            self.assertGreater(light_text_pixels(pressed), light_text_pixels(before) * .7)
        popup = self.app.activePopupWidget()
        if popup:
            popup.close()
        move(surface, QPoint(1, 1))
        QTest.mouseRelease(control, Qt.LeftButton, pos=QPoint(-10, -10))
        control.clearFocus()
        control.setEnabled(False)
        move(surface, QPoint(1, 1))
        self.app.processEvents()
        disabled = control.grab().toImage()
        move(control, control.rect().center())
        self.app.processEvents()
        self.assertEqual(disabled, control.grab().toImage(), control.text() + ' responds while disabled')
        control.setEnabled(True)
        move(surface, QPoint(1, 1))

    def test_workspace_button_variants_have_visible_pointer_feedback(self):
        for control in (self.page.storage_button, self.page.record_tab, self.page.host_tab,
                        self.page.comments_toggle, self.page.add_button, self.page.settings_button,
                        self.page.host_filter):
            with self.subTest(control=control.text()):
                self.assert_feedback(control)

    def test_rows_paging_and_segment_actions_have_feedback(self):
        self.seed(hosts=21, recordings=0)
        self.controller.timer.stop()
        self.until(lambda: self.controller.worker is None)
        hid = self.page.data['rows'][0]['id']
        self.service.handle('start', dict(session=self.controller.session, host_id=hid))
        self.page.refresh()
        self.until(lambda: self.page.data['monitoring'] == 1)
        for control in (self.page.pause_all, self.page.next,
                        self.page.table.cellWidget(0, 0).findChild(QPushButton),
                        self.page.table.cellWidget(0, 6).findChild(QPushButton)):
            with self.subTest(control=control.text()):
                self.assert_feedback(control)
        rid, _ = self.completed_segment()
        self.page._tab('recordings')
        self.until(lambda: self.page.data['recordings'] == 1)
        for control in self.page.table.cellWidget(0, 6).findChildren(QPushButton):
            self.assert_feedback(control)
        self.controller.call('detail', recording_id=rid, callback=self.page._detail)
        self.until(lambda: self.page.detail_dialog and self.page.detail_dialog.isVisible())
        dialog = self.page.detail_dialog
        for control in (dialog.export_button, dialog.extract_button, dialog.close_button):
            self.assert_feedback(control)
        dialog.reject()

    def test_data_and_comment_actions_have_feedback(self):
        dialog = self.opened()
        for control in (*dialog.navigation.buttons(), *dialog.buttons.values(), dialog.folder_button):
            with self.subTest(control=control.text()):
                self.assert_feedback(control)
        dialog.navigation.set_value('comments')
        dialog.tabs.setCurrentIndex(1)
        panel = dialog.comments_panel
        self.until(lambda: self.controller.comments_worker is None)
        panel.timer.stop()
        # Populate only the temporary view to cover actionable/selected states.
        from src.core.live_recording.comments import CommentLog
        from tests.test_live_comments import chat
        CommentLog(self.service._recording(dialog.recording_id)).append([chat(i) for i in range(1, 202)])
        panel.refresh()
        self.until(lambda: panel.result.get('total') == 201 and self.controller.comments_worker is None)
        panel.table.selectRow(0)
        self.app.processEvents()
        for control in (panel.export_button, panel.copy_button, panel.older):
            self.assert_feedback(control)
        for control in dialog.findChildren(QPushButton):
            if control.accessibleName() == '关闭直播记录':
                self.assert_feedback(control)
        dialog.reject()

    def test_dialog_primary_quiet_and_close_have_visible_pointer_feedback(self):
        self.page.credentials.show()
        self.app.processEvents()
        for control in (self.page.browser_login, self.page.advanced_toggle,
                        self.page.credentials.close_button):
            with self.subTest(control=control.text()):
                self.assert_feedback(control)
        self.page._set_access_view('clear')
        self.app.processEvents()
        self.assert_feedback(self.page.confirm_clear)

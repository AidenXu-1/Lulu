import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QWidget

from src.ui import theme as T
from src.ui.consumer_select import (
    ConsumerActionPopup,
    ConsumerSelect,
    _settle_popup_below_trigger,
)
from src.ui.control_foundation import PaperTextureFrame


class ConsumerSelectTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def _select(self):
        return ConsumerSelect(
            (("always", "始终提醒"), ("inactive", "仅 Lulu 不在前台时提醒"), ("never", "从不提醒")),
            value="never",
            accessible_name="任务完成提醒",
        )

    def test_is_a_lulu_widget_not_native_combo_and_exposes_current_value(self):
        select = self._select()
        self.assertNotIsInstance(select, QComboBox)
        self.assertEqual(select.currentData(), "never")
        self.assertEqual(select.currentText(), "从不提醒")
        self.assertEqual(select.accessibleName(), "任务完成提醒")
        self.assertIsNone(select.property("consumerThemeScope"))
        self.assertEqual(select.trigger.focusPolicy(), Qt.TabFocus)
        self.assertIsNone(select.popup.property("consumerThemeScope"))
        self.assertIn(T.CARD, select.trigger.styleSheet())

    def test_core_workspace_opt_in_uses_semantic_tokens_and_scopes_popup(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                T.apply_theme(theme_name)
                consumer = T.consumer_theme(theme_name)
                select = ConsumerSelect(
                    (("one", "第一项"), ("two", "第二项")),
                    value="one",
                    accessible_name="核心工作页选择器",
                    consumer_theme_scope="coreWorkspace",
                    consumer_theme_name=theme_name,
                )
                self.assertEqual(select.property("consumerThemeScope"), "coreWorkspace")
                self.assertEqual(select.trigger.property("consumerThemeScope"), "coreWorkspace")
                self.assertEqual(select.popup.property("consumerThemeScope"), "coreWorkspace")
                self.assertIn(consumer.surface_focal, select.trigger.styleSheet())
                self.assertIn(consumer.accent, select.trigger.styleSheet())
                self.assertIn(consumer.surface_floating, select.popup.styleSheet())
                self.assertIn(consumer.accent, select.popup.styleSheet())

        with self.assertRaises(ValueError):
            ConsumerSelect(
                (("one", "第一项"),),
                value="one",
                accessible_name="非法范围",
                consumer_theme_scope="globalShell",
            )

    def test_keyboard_navigation_enter_and_escape(self):
        select = self._select()
        emitted = []
        select.value_changed.connect(emitted.append)
        select.resize(240, 38)
        select.show()
        select.show_popup()
        self.app.processEvents()
        self.assertTrue(select.popup.isVisible())

        QTest.keyClick(select.popup, Qt.Key_Up)
        QTest.keyClick(select.popup, Qt.Key_Return)
        self.assertEqual(select.currentData(), "inactive")
        self.assertEqual(emitted, ["inactive"])

        select.show_popup()
        QTest.keyClick(select.popup, Qt.Key_Escape)
        self.assertFalse(select.popup.isVisible())
        self.assertEqual(select.currentData(), "inactive")

    def test_paper_keep_checked_mark_and_minimum_touch_height(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                select = self._select()
                select.show_popup()
                self.app.processEvents()
                checked = [row for row in select.popup.rows if row.property("selected")]
                self.assertEqual(len(checked), 1)
                self.assertEqual(checked[0].value, "never")
                self.assertGreaterEqual(min(row.height() for row in select.popup.rows), 40)
                self.assertFalse(select.popup.testAttribute(Qt.WA_TranslucentBackground))
                window_color = select.popup.palette().color(QPalette.Window)
                self.assertEqual(window_color, QColor(T.CARD))
                self.assertEqual(window_color.alpha(), 255)
                image = select.popup.grab().toImage()
                self.assertEqual(
                    min(
                        image.pixelColor(x, y).alpha()
                        for x in range(image.width())
                        for y in range(image.height())
                    ),
                    255,
                )
                select.popup.close()

    def test_paper_expose_full_row_hover_and_pointing_cursor(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                select = self._select()
                select.resize(240, 38)
                select.show()
                select.show_popup()
                self.app.processEvents()

                self.assertTrue(
                    all(row.cursor().shape() == Qt.PointingHandCursor for row in select.popup.rows)
                )
                row = select.popup.rows[0]
                baseline = row.grab().toImage().pixelColor(row.width() - 18, row.height() // 2)

                QApplication.sendEvent(row, QEvent(QEvent.Enter))
                self.app.processEvents()
                hovered = row.grab().toImage().pixelColor(row.width() - 18, row.height() // 2)
                self.assertTrue(row.property("hovered"))
                self.assertNotEqual(hovered, baseline)

                QApplication.sendEvent(row, QEvent(QEvent.Leave))
                self.app.processEvents()
                self.assertFalse(row.property("hovered"))

                selected = next(item for item in select.popup.rows if item.property("selected"))
                selected_baseline = selected.grab().toImage().pixelColor(
                    selected.width() - 18, selected.height() // 2
                )
                QApplication.sendEvent(selected, QEvent(QEvent.Enter))
                self.app.processEvents()
                selected_hover = selected.grab().toImage().pixelColor(
                    selected.width() - 18, selected.height() // 2
                )
                self.assertTrue(selected.property("hovered"))
                self.assertNotEqual(selected_hover, selected_baseline)
                select.popup.close()
                self.app.processEvents()
                self.assertFalse(any(row.property("hovered") for row in select.popup.rows))

                select.show_popup()
                self.app.processEvents()
                self.assertFalse(any(row.property("hovered") for row in select.popup.rows))
                QTest.keyClick(select.popup, Qt.Key_Up)
                self.assertEqual(
                    sum(bool(row.property("active")) for row in select.popup.rows),
                    1,
                )
                self.assertFalse(any(row.property("hovered") for row in select.popup.rows))
                select.popup.close()

    def test_paper_popup_is_explicit_and_keeps_default_branch_opaque(self):
        default = self._select()
        self.assertFalse(default.paper_popup)
        self.assertFalse(default.popup.testAttribute(Qt.WA_TranslucentBackground))
        self.assertIsNone(default.popup._paper_card)

        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                consumer = T.consumer_theme(theme)
                select = ConsumerSelect(
                    (("all", "日期不限"), ("7", "近 7 天"), ("custom", "自定义日期")),
                    value="all",
                    accessible_name="发布日期范围",
                    consumer_theme_scope="coreWorkspace",
                    paper_popup=True,
                    clip_popup_corners=True,
                )
                select.resize(180, 38)
                select.show()
                select.show_popup()
                self.app.processEvents()
                paper = select.popup._paper_card
                self.assertIsInstance(paper, PaperTextureFrame)
                self.assertTrue(select.popup.testAttribute(Qt.WA_TranslucentBackground))
                self.assertEqual(
                    paper.property("paperTextureStrength"),
                    consumer.paper_texture_opacity,
                )
                expected_rows_height = 3 * 36 + 2
                self.assertEqual(select.popup.scroll.height(), expected_rows_height)
                self.assertEqual(select.popup._rows_host.height(), expected_rows_height)
                self.assertEqual(select.popup.height(), expected_rows_height + 8)
                self.assertTrue(
                    select.popup.scroll.viewport().testAttribute(
                        Qt.WA_TranslucentBackground
                    )
                )
                self.assertFalse(paper.mask().isEmpty())
                image = select.popup.grab().toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                self.assertGreater(
                    image.pixelColor(image.width() // 2, image.height() // 2).alpha(),
                    0,
                )
                select.popup.close()

    def test_mouse_selection_and_close_without_choice(self):
        select = self._select()
        emitted = []
        select.value_changed.connect(emitted.append)
        select.resize(240, 38)
        select.show()
        select.show_popup()
        self.app.processEvents()
        QTest.mouseClick(select.popup.rows[0], Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(select.currentData(), "always")
        self.assertEqual(emitted, ["always"])

        select.show_popup()
        select.popup.close()
        self.assertEqual(select.currentData(), "always")

    def test_disabled_control_keeps_value_and_never_opens_popup(self):
        select = self._select()
        select.setEnabled(False)
        select.show_popup()
        self.app.processEvents()
        self.assertFalse(select.popup.isVisible())
        self.assertEqual(select.currentData(), "never")

    def test_history_mode_supports_zero_and_dynamic_options(self):
        select = ConsumerSelect(
            (),
            value="",
            accessible_name="采集任务",
            allow_empty=True,
            empty_text="暂无采集任务",
            max_visible_rows=8,
        )
        self.assertEqual(select.currentData(), "")
        self.assertEqual(select.currentText(), "暂无采集任务")
        select.show_popup()
        self.assertFalse(select.popup.isVisible())

        select.set_options((("r1", "第一批"), ("r2", "第二批")), value="r2")
        self.assertEqual(select.currentData(), "r2")
        self.assertEqual(select.currentText(), "第二批")
        self.assertEqual(len(select.popup.rows), 2)
        self.assertEqual(select.trigger.toolTip(), "第二批")

        select.set_options((), value="")
        self.assertEqual(select.currentText(), "暂无采集任务")
        self.assertEqual(select.popup.rows, [])

    def test_history_popup_shows_at_most_eight_rows_and_scrolls_keyboard(self):
        options = tuple((f"r{index}", f"第 {index} 批记录的完整长名称") for index in range(20))
        select = ConsumerSelect(
            options,
            value="r0",
            accessible_name="采集任务",
            max_visible_rows=8,
        )
        select.resize(260, 38)
        select.show()
        select.show_popup()
        self.app.processEvents()
        self.assertTrue(select.popup.isVisible())
        self.assertEqual(select.popup.scroll.height(), 8 * 42 + 7 * 2)
        self.assertTrue(select.popup.scroll.verticalScrollBar().maximum() > 0)
        for _ in range(10):
            QTest.keyClick(select.popup, Qt.Key_Down)
        QTest.keyClick(select.popup, Qt.Key_Return)
        self.assertEqual(select.currentData(), "r10")

        for count in (1, 8, 9, 100):
            with self.subTest(count=count):
                select.set_options(tuple((str(i), str(i)) for i in range(count)), value="0")
                select.show_popup()
                self.app.processEvents()
                expected = min(count, 8) * 42 + max(0, min(count, 8) - 1) * 2
                self.assertEqual(select.popup.scroll.height(), expected)
                select.popup.close()

    def test_default_popup_stays_below_trigger_and_limits_six_rows(self):
        for count in (1, 6, 7, 20, 100):
            with self.subTest(count=count):
                options = tuple((str(index), f"选项 {index}") for index in range(count))
                select = ConsumerSelect(
                    options,
                    value="0",
                    accessible_name="载荷边界",
                    consumer_theme_scope="coreWorkspace",
                    paper_popup=True,
                    clip_popup_corners=True,
                )
                select.resize(188, 38)
                select.move(80, 80)
                select.show()
                select.show_popup()
                self.app.processEvents()
                self.assertTrue(select.popup.isVisible())
                trigger_bottom = select.trigger.mapToGlobal(
                    select.trigger.rect().bottomLeft()
                ).y()
                self.assertGreaterEqual(select.popup.y() - trigger_bottom, 6)
                self.assertLessEqual(select.popup.y() - trigger_bottom, 8)
                self.assertGreaterEqual(select.popup.width(), select.trigger.width())
                visible = min(count, 6)
                expected = visible * 36 + max(0, visible - 1)
                self.assertEqual(select.popup.scroll.height(), expected)
                self.assertEqual(
                    select.popup.scroll.verticalScrollBar().maximum() > 0,
                    count > 6,
                )
                self.assertIn(
                    T.consumer_theme(T.ACTIVE.name).popup_scroll_handle,
                    select.popup.styleSheet(),
                )
                image = select.popup.grab().toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                select.popup.close()

    def test_paper_popup_uses_content_width_and_has_no_blank_vertical_cavity(self):
        select = ConsumerSelect(
            (
                ("date", "发布日期"),
                ("likes", "点赞"),
                ("favorites", "收藏"),
                ("comments", "评论"),
                ("shares", "分享"),
                ("duration", "时长"),
            ),
            value="date",
            accessible_name="排序",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        select.resize(136, 38)
        select.show()
        select.show_popup()
        self.app.processEvents()
        expected_rows_height = 6 * 36 + 5
        self.assertEqual(select.popup.width(), 192)
        self.assertLess(select.popup.width(), 220)
        self.assertEqual(select.popup.scroll.height(), expected_rows_height)
        self.assertEqual(select.popup._rows_host.height(), expected_rows_height)
        self.assertEqual(select.popup.height(), expected_rows_height + 8)
        self.assertEqual([row.height() for row in select.popup.rows], [36] * 6)
        select.popup.close()

    def test_scroll_parent_never_moves_visible_anchor_and_settles_before_clipped_anchor(self):
        class TrackingScrollArea(QScrollArea):
            def __init__(self):
                super().__init__()
                self.ensure_calls = 0

            def ensureWidgetVisible(self, child, x_margin=50, y_margin=50):  # noqa: N802
                self.ensure_calls += 1
                super().ensureWidgetVisible(child, x_margin, y_margin)

        scroll = TrackingScrollArea()
        scroll.resize(640, 400)
        content = QWidget()
        content.resize(620, 760)
        select = ConsumerSelect(
            (("all", "日期不限"), ("7", "近 7 天"), ("custom", "自定义日期")),
            value="all",
            accessible_name="发布日期范围",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
            parent=content,
        )
        select.setGeometry(24, 160, 192, 38)
        scroll.setWidget(content)
        scroll.show()
        self.app.processEvents()

        select.show_popup()
        self.app.processEvents()
        self.assertEqual(scroll.ensure_calls, 0)
        trigger_bottom = select.trigger.mapToGlobal(
            select.trigger.rect().bottomLeft()
        ).y()
        self.assertGreaterEqual(select.popup.y() - trigger_bottom, 6)
        self.assertLessEqual(select.popup.y() - trigger_bottom, 8)
        select.popup.close()

        select.move(24, 640)
        scroll.verticalScrollBar().setValue(0)
        self.app.processEvents()
        select.show_popup()
        self.app.processEvents()
        self.app.processEvents()
        self.assertEqual(scroll.ensure_calls, 1)
        self.assertTrue(select.popup.isVisible())
        trigger_bottom = select.trigger.mapToGlobal(
            select.trigger.rect().bottomLeft()
        ).y()
        self.assertGreaterEqual(select.popup.y() - trigger_bottom, 6)
        self.assertLessEqual(select.popup.y() - trigger_bottom, 8)
        select.popup.close()
        scroll.close()
        select.deleteLater()
        scroll.deleteLater()
        self.app.processEvents()

    def test_native_popup_frame_is_reanchored_below_trigger_after_show(self):
        select = ConsumerSelect(
            (("all", "日期不限"), ("7", "近 7 天")),
            value="all",
            accessible_name="发布日期范围",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        select.resize(192, 38)
        select.move(80, 80)
        select.show()
        select.show_popup()
        self.app.processEvents()

        select.popup.move(select.popup.x(), select.popup.y() - 24)
        _settle_popup_below_trigger(select.popup, select.trigger)
        trigger_bottom = select.trigger.mapToGlobal(
            select.trigger.rect().bottomLeft()
        ).y()
        self.assertGreaterEqual(select.popup.y() - trigger_bottom, 6)
        self.assertLessEqual(select.popup.y() - trigger_bottom, 8)
        select.popup.close()

        actions = ConsumerActionPopup(
            (("collection", "采集数据"), ("covers", "封面")),
            accessible_name="数据位置",
        )
        actions.move(80, 160)
        actions.show()
        actions.show_popup()
        self.app.processEvents()

        actions.popup.move(actions.popup.x(), actions.popup.y() - 24)
        _settle_popup_below_trigger(actions.popup, actions.trigger)
        trigger_bottom = actions.trigger.mapToGlobal(
            actions.trigger.rect().bottomLeft()
        ).y()
        self.assertGreaterEqual(actions.popup.y() - trigger_bottom, 6)
        self.assertLessEqual(actions.popup.y() - trigger_bottom, 8)
        actions.popup.close()

    def test_mouse_open_and_close_does_not_leave_a_focus_ring_on_triggers(self):
        select = ConsumerSelect(
            (("date", "发布日期"), ("likes", "点赞")),
            value="date",
            accessible_name="排序",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
        )
        select.resize(192, 38)
        select.show()
        QTest.mouseClick(select.trigger, Qt.LeftButton)
        self.app.processEvents()
        select.popup.close()
        self.app.processEvents()
        self.assertFalse(select.trigger.hasFocus())
        self.assertEqual(select.trigger.focusPolicy(), Qt.TabFocus)

        actions = ConsumerActionPopup(
            (("collection", "采集数据"), ("covers", "封面")),
            accessible_name="数据位置",
        )
        actions.show()
        QTest.mouseClick(actions.trigger, Qt.LeftButton)
        self.app.processEvents()
        actions.popup.close()
        self.app.processEvents()
        self.assertFalse(actions.trigger.hasFocus())
        self.assertEqual(actions.trigger.focusPolicy(), Qt.TabFocus)

    def test_popup_never_flips_up_when_lower_space_is_insufficient(self):
        select = ConsumerSelect(
            tuple((str(index), str(index)) for index in range(7)),
            value="0",
            accessible_name="下方空间门",
            consumer_theme_scope="coreWorkspace",
            paper_popup=True,
            clip_popup_corners=True,
        )
        select.resize(180, 38)
        select.show()
        fake_screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 240, 40))
        with patch(
            "src.ui.consumer_select.QGuiApplication.screenAt",
            return_value=fake_screen,
        ):
            select.show_popup()
        self.app.processEvents()
        self.assertFalse(select.popup.isVisible())
        self.assertEqual(select.trigger.toolTip(), "窗口下方空间不足")
        self.assertEqual(select.trigger.accessibleDescription(), "窗口下方空间不足")

    def test_action_popup_is_a_fixed_four_row_action_surface_without_selection_chrome(self):
        actions = ConsumerActionPopup(
            (
                ("collection", "采集数据"),
                ("covers", "封面"),
                ("library", "文稿"),
                ("media", "保留音视频"),
            ),
            accessible_name="数据位置",
        )
        self.assertEqual(actions.size().toTuple(), (96, 38))
        self.assertEqual(actions.trigger.text(), "数据位置")
        self.assertEqual(actions.popup.size().toTuple(), (172, 162))
        self.assertEqual(
            [row.open_button.text() for row in actions.popup.rows],
            ["采集数据", "封面", "文稿", "保留音视频"],
        )
        self.assertEqual(actions.popup.findChildren(QScrollArea), [])
        self.assertFalse(
            any("✓" in row.open_button.text() for row in actions.popup.rows)
        )
        for row in actions.popup.rows:
            self.assertEqual(row.height(), 38)
            self.assertEqual(row.folder_button.size().toTuple(), (30, 30))
            self.assertEqual(row.layout().contentsMargins().right(), 6)
        self.assertIn(
            "QFrame#consumerActionRow{background:transparent",
            actions.popup.styleSheet(),
        )
        self.assertIn(
            "border:1px solid transparent",
            actions.popup.styleSheet(),
        )

    def test_action_popup_keeps_left_and_folder_actions_separate_and_keyboard_closable(self):
        actions = ConsumerActionPopup(
            (("collection", "采集数据"), ("library", "文稿")),
            accessible_name="数据位置",
        )
        opened: list[str] = []
        configured: list[str] = []
        actions.row_activated.connect(opened.append)
        actions.button_activated.connect(configured.append)
        actions.set_action_context(
            "collection",
            "/tmp/managed",
            configurable=False,
        )
        actions.set_action_context(
            "library",
            "/tmp/output",
            configurable=True,
        )
        self.assertEqual(
            actions.popup.rows[0].folder_button.accessibleName(),
            "打开采集数据位置",
        )
        self.assertEqual(
            actions.popup.rows[1].folder_button.accessibleName(),
            "设置文稿今后保存位置",
        )
        self.assertIn("/tmp/output", actions.popup.rows[1].folder_button.toolTip())

        actions.move(80, 80)
        actions.show()
        actions.show_popup()
        self.app.processEvents()
        self.assertTrue(actions.popup.isVisible())
        QTest.mouseClick(actions.popup.rows[1].folder_button, Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(configured, ["library"])
        self.assertEqual(opened, [])

        actions.show_popup()
        self.app.processEvents()
        QTest.mouseClick(actions.popup.rows[0].open_button, Qt.LeftButton)
        self.app.processEvents()
        self.assertEqual(opened, ["collection"])
        self.assertEqual(configured, ["library"])

        actions.show_popup()
        self.app.processEvents()
        self.assertFalse(actions.popup.rows[0].open_button.hasFocus())
        actions.popup.close()
        actions.trigger.setFocus()
        QTest.keyClick(actions.trigger, Qt.Key_Down)
        self.app.processEvents()
        self.assertTrue(actions.popup.rows[0].open_button.hasFocus())
        QTest.keyClick(actions.popup.rows[0].open_button, Qt.Key_Escape)
        self.app.processEvents()
        self.assertFalse(actions.popup.isVisible())

    def test_action_popup_uses_all_consumer_themes_and_fails_closed_below_trigger(self):
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                actions = ConsumerActionPopup(
                    (
                        ("collection", "采集数据"),
                        ("covers", "封面"),
                        ("library", "文稿"),
                        ("media", "保留音视频"),
                    ),
                    accessible_name="数据位置",
                )
                actions.move(80, 80)
                actions.show()
                actions.show_popup()
                self.app.processEvents()
                self.assertTrue(actions.popup.isVisible())
                self.assertFalse(actions.popup._paper_card.mask().isEmpty())
                image = actions.popup.grab().toImage()
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                trigger_bottom = actions.trigger.mapToGlobal(
                    actions.trigger.rect().bottomLeft()
                ).y()
                self.assertGreaterEqual(actions.popup.y() - trigger_bottom, 6)
                self.assertLessEqual(actions.popup.y() - trigger_bottom, 8)
                actions.popup.close()

        actions = ConsumerActionPopup(
            (("collection", "采集数据"),),
            accessible_name="数据位置",
        )
        actions.show()
        fake_screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 240, 40))
        with patch(
            "src.ui.consumer_select.QGuiApplication.screenAt",
            return_value=fake_screen,
        ):
            actions.show_popup()
        self.app.processEvents()
        self.assertFalse(actions.popup.isVisible())
        self.assertEqual(actions.trigger.toolTip(), "窗口下方空间不足")


if __name__ == "__main__":
    unittest.main()

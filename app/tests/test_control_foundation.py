import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QFocusEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QFrame,
    QLabel,
    QListWidget,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
)

from src.ui import icons, theme as T
from src.ui.control_foundation import (
    LuluButton,
    LuluCheckBox,
    LuluIconButton,
    LuluSegmentedControl,
    LuluSelectField,
    LuluSurface,
    LuluSwitch,
    LuluTextField,
    PaperTextureFrame,
    adopt_action_surface,
    adopt_button,
    adopt_checkbox,
    adopt_consumer_hover_motion,
    adopt_consumer_theme_scope,
    adopt_bulk_bar,
    adopt_dialog,
    adopt_divider,
    adopt_list_row,
    adopt_list_widget,
    adopt_menu,
    adopt_nav_row,
    adopt_popover,
    adopt_progress,
    adopt_select_field_frame,
    adopt_select_field,
    adopt_slider,
    adopt_spin_box,
    adopt_surface,
    adopt_table,
    adopt_text_area,
    build_consumer_page_header,
    set_control_state,
    set_button_loading,
    set_progress_kind,
    set_status_kind,
    track_status_text_semantics,
)
from src.ui.control_gallery import ControlGallery
from src.ui.widgets import FileDropArea


class ControlFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._original_theme_name = next(
            (name for name, palette in T.THEMES.items() if palette is T.ACTIVE),
            T.DEFAULT_THEME,
        )
        self._original_app_stylesheet = self.app.styleSheet()
        self.addCleanup(self._restore_global_ui_state)
        T.apply_theme("warm")
        self.app.setStyleSheet(T.QSS)

    def _restore_global_ui_state(self):
        T.apply_theme(self._original_theme_name)
        self.app.setStyleSheet(self._original_app_stylesheet)

    def test_consumer_theme_scope_is_an_explicit_supported_opt_in(self):
        shell = QFrame()
        sample = QFrame()
        workspace = QFrame()

        self.assertIs(adopt_consumer_theme_scope(shell, "globalShell"), shell)
        self.assertEqual(shell.property("consumerThemeScope"), "globalShell")
        self.assertIs(
            adopt_consumer_theme_scope(sample, "transcriptionWorkbench"),
            sample,
        )
        self.assertEqual(
            sample.property("consumerThemeScope"),
            "transcriptionWorkbench",
        )
        self.assertIs(
            adopt_consumer_theme_scope(workspace, "coreWorkspace"),
            workspace,
        )
        self.assertEqual(workspace.property("consumerThemeScope"), "coreWorkspace")
        with self.assertRaises(ValueError):
            adopt_consumer_theme_scope(QFrame(), "wholeApplication")

    def test_consumer_page_header_keeps_the_transcription_title_lockup(self):
        trailing = QLabel("本地离线")
        header, title, subtitle = build_consumer_page_header(
            "文案处理",
            "把一篇文案拆开看透",
            trailing=trailing,
        )
        rule = header.findChild(QFrame, "studioTitleRule")

        self.assertIsNotNone(rule)
        self.assertEqual((rule.width(), rule.height()), (3, 46))
        self.assertEqual(title.objectName(), "pageTitle")
        self.assertEqual(title.text(), "文案处理")
        self.assertEqual(subtitle.objectName(), "pageMeta")
        self.assertTrue(subtitle.wordWrap())
        self.assertIs(trailing.parent(), header)
        self.assertEqual(header.layout().indexOf(trailing), 2)


    def test_button_roles_sizes_loading_and_keyboard_are_stable(self):
        clicked = []
        button = LuluButton("开始处理", role="primary", size="md", icon="play")
        button.clicked.connect(lambda: clicked.append(True))
        button.show()
        self.app.processEvents()
        width = button.width()
        self.assertEqual(button.height(), 34)
        self.assertEqual(button.property("role"), "primary")
        self.assertEqual(button.property("controlSize"), "md")
        self.assertEqual(button.accessibleName(), "开始处理")
        button.setFocus()
        QTest.keyClick(button, Qt.Key_Enter)
        self.assertEqual(clicked, [True])
        button.set_loading(True, "正在处理…")
        self.app.processEvents()
        self.assertEqual(button.height(), 34)
        self.assertGreaterEqual(button.width(), width)
        self.assertEqual(button.property("state"), "loading")
        self.assertFalse(button.isEnabled())
        button.set_loading(False)
        self.assertEqual(button.text(), "开始处理")

        self.assertEqual(LuluButton("小", size="sm").height(), 28)
        self.assertEqual(LuluButton("大", size="lg").height(), 40)
        with self.assertRaises(ValueError):
            LuluButton("坏角色", role="unknown")

    def test_adopted_business_button_loading_and_modal_keep_contract(self):
        button = QPushButton("生成配音")
        adopt_button(button, role="primary", size="lg", accessible_name="生成配音")
        button.show()
        self.app.processEvents()
        width = button.width()
        button.setEnabled(False)
        set_button_loading(button, True)
        self.app.processEvents()
        self.assertEqual(button.property("state"), "loading")
        self.assertFalse(button.isEnabled())
        self.assertGreaterEqual(button.width(), width)
        set_button_loading(button, False)
        self.assertEqual(button.text(), "生成配音")
        self.assertFalse(button.isEnabled())

        progress = adopt_progress(QProgressBar(), kind="neutral")
        set_progress_kind(progress, "success")
        self.assertEqual(progress.property("statusKind"), "success")

        dialog = adopt_dialog(QDialog(), accessible_name="生成结果")
        self.assertTrue(dialog.isModal())
        self.assertGreaterEqual(dialog.minimumWidth(), 420)
        self.assertEqual(dialog.accessibleName(), "生成结果")

        status = QLabel("等待生成…")
        set_status_kind(status, "neutral")
        track_status_text_semantics(status)
        status.show()
        status.setText("生成失败：请检查输入")
        self.app.processEvents()
        self.assertEqual(status.property("statusKind"), "error")
        status.setText("完成 · 已保存到素材库")
        self.app.processEvents()
        self.assertEqual(status.property("statusKind"), "success")

    def test_icon_button_requires_tooltip_and_accessible_name(self):
        icon = LuluIconButton("copy", accessible_name="复制", tooltip="复制当前内容")
        self.assertEqual(icon.size().width(), 34)
        self.assertEqual(icon.size().height(), 34)
        self.assertEqual(icon.toolTip(), "复制当前内容")
        self.assertEqual(icon.accessibleName(), "复制")
        with self.assertRaises(ValueError):
            LuluIconButton("copy", accessible_name="", tooltip="复制")

    def test_settings_icon_variant_is_frameless_in_all_interaction_states_and_themes(self):
        selector = 'QPushButton[luluControl="button"][controlVariant="settings"]'
        pane_selector = 'QPushButton[luluControl="button"][controlVariant="pane"]'
        for theme in ("warm",):
            with self.subTest(theme=theme):
                T.apply_theme(theme)
                self.app.setStyleSheet(T.QSS)
                qss = " ".join(T.QSS.split())
                self.assertIn(
                    f'{selector} {{ background: transparent; border: 2px solid transparent; '
                    'border-radius: 7px; padding: 0; }',
                    qss,
                )
                self.assertIn(
                    f'{selector}:checked {{ background: transparent; border-color: transparent; }}',
                    qss,
                )
                self.assertIn(
                    f'{selector}:hover {{ background: {T.RAIL_HOVER}; border-color: transparent; }}',
                    qss,
                )
                self.assertIn(
                    f'{selector}:pressed, {selector}:checked:pressed '
                    f'{{ background: {T.BG_DEEP}; border-color: transparent; }}',
                    qss,
                )
                self.assertIn(
                    f'{selector}:focus, {selector}[demoState="focus"] '
                    f'{{ background: {T.TERRA_WASH}; border: 2px solid transparent; }}',
                    qss,
                )
                self.assertIn(
                    f'{pane_selector} {{ background: transparent; border: 2px solid transparent;',
                    qss,
                )

                button = QPushButton()
                adopt_button(
                    button,
                    role="icon",
                    size="sm",
                    variant="settings",
                    accessible_name="打开设置",
                    tooltip="设置",
                )
                button.setFixedSize(30, 28)
                button.setCheckable(True)
                button.show()
                button.setFocus()
                self.app.processEvents()
                self.assertEqual(button.property("controlVariant"), "settings")
                self.assertEqual(button.focusPolicy(), Qt.StrongFocus)
                self.assertTrue(button.hasFocus())
                self.assertEqual((button.width(), button.height()), (30, 28))
                button.setChecked(True)
                self.assertTrue(button.isChecked())
                QTest.mousePress(button, Qt.LeftButton, pos=button.rect().center())
                self.assertTrue(button.isDown())
                QTest.mouseRelease(button, Qt.LeftButton, pos=button.rect().center())
                button.close()
                button.deleteLater()
                self.app.processEvents()

    def test_input_select_segment_checkbox_and_switch_keyboard_contracts(self):
        field = LuluTextField(accessible_name="作品链接", placeholder="粘贴链接")
        self.assertEqual(field.height(), 38)
        field.set_error("格式不正确")
        self.assertEqual(field.property("state"), "error")
        self.assertEqual(field.accessibleDescription(), "格式不正确")

        select = LuluSelectField(accessible_name="初筛模型")
        select.addItems(("Agnes", "DeepSeek"))
        select.show()
        select.setFocus()
        QTest.keyClick(select, Qt.Key_Down)
        self.assertEqual(select.currentText(), "DeepSeek")
        self.assertFalse(select._chevron.pixmap().isNull())

        segment = LuluSegmentedControl(
            [("audio", "仅音频"), ("video", "音视频")], accessible_name="下载内容"
        )
        changes = []
        segment.selectionChanged.connect(changes.append)
        segment.set_value("video")
        self.assertEqual(segment.value(), "video")
        self.assertEqual(changes, ["video"])
        self.assertEqual(segment.height(), 36)
        self.assertEqual(segment.layout().spacing(), 3)

        check = LuluCheckBox("优先字幕")
        check.show()
        QTest.mouseClick(check, Qt.LeftButton, pos=check.rect().center())
        self.assertTrue(check.isChecked())

        self.assertEqual(check.height(), 28)

        switch = LuluSwitch(accessible_name="自动检查更新")
        switch.show()
        switch.setFocus()
        QTest.keyClick(switch, Qt.Key_Space)
        self.assertTrue(switch.isChecked())
        QTest.keyClick(switch, Qt.Key_Enter)
        self.assertFalse(switch.isChecked())
        self.assertEqual(switch.size().width(), 38)
        self.assertEqual(switch.size().height(), 22)
        self.assertEqual(switch.accessibleDescription(), "已关闭")

    def test_checkbox_scoped_accent_renders_states_without_changing_default(self):
        def indicator_colors(widget):
            widget.resize(widget.sizeHint())
            widget.show()
            self.app.processEvents()
            image = QImage(widget.size(), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            widget.render(image)
            return {
                image.pixelColor(x, y).name().upper()
                for x in range(min(21, image.width()))
                for y in range(min(25, image.height()))
            }

        def contains_antialiased_color(colors, target):
            wanted = QColor(target)
            return any(
                max(
                    abs(QColor(color).red() - wanted.red()),
                    abs(QColor(color).green() - wanted.green()),
                    abs(QColor(color).blue() - wanted.blue()),
                ) <= 64
                for color in colors
            )

        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                T.apply_theme(theme_name)
                self.app.setStyleSheet(T.QSS)

                default = LuluCheckBox("默认")
                default.setChecked(True)
                default_accent, default_soft = default._accent_colors()
                self.assertEqual(default_accent.name().upper(), T.TERRA.upper())
                self.assertEqual(default_soft.name().upper(), T.TERRA_SOFT.upper())
                self.assertIn(T.TERRA.upper(), indicator_colors(default))

                scoped_color = T.consumer_theme(theme_name).accent
                scoped = LuluCheckBox("样板")
                scoped.set_consumer_accent(scoped_color)
                scoped.setChecked(True)
                scoped_accent, scoped_soft = scoped._accent_colors()
                self.assertEqual(scoped_accent.name().upper(), scoped_color.upper())
                self.assertEqual(scoped_soft.name().upper(), scoped_color.upper())
                self.assertIn(scoped_color.upper(), indicator_colors(scoped))
                self.assertNotIn(T.TERRA.upper(), indicator_colors(scoped))

                scoped.setChecked(False)
                scoped.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
                self.assertNotIn(
                    scoped_color.upper(),
                    indicator_colors(scoped),
                )
                scoped.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
                scoped.activateWindow()
                scoped.setFocus(Qt.FocusReason.TabFocusReason)
                self.app.processEvents()
                self.assertTrue(scoped.hasFocus())
                self.assertTrue(scoped._keyboard_focus)
                self.assertTrue(
                    contains_antialiased_color(indicator_colors(scoped), T.INK_FAINT)
                )
                self.assertNotIn(
                    scoped_color.upper(),
                    indicator_colors(scoped),
                )
                default.close()
                scoped.close()

    def test_checkbox_mouse_cycle_and_bare_adapter_restore_unchecked_semantics(self):
        check = LuluCheckBox("下载封面")
        check.resize(check.sizeHint())
        check.show()
        check.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
        self.app.processEvents()
        initial = check.grab().toImage()
        QTest.mouseClick(check, Qt.LeftButton, pos=check.rect().center())
        self.assertTrue(check.isChecked())
        QTest.mouseClick(check, Qt.LeftButton, pos=check.rect().center())
        QApplication.sendEvent(check, QEvent(QEvent.Type.Leave))
        check.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
        self.app.processEvents()
        self.assertFalse(check.isChecked())
        self.assertFalse(check._keyboard_focus)
        self.assertEqual(check.grab().toImage(), initial)

        bare = adopt_checkbox(QCheckBox("兼容复选"), accessible_name="兼容复选")
        self.assertEqual(bare.objectName(), "luluCheckBox")
        self.assertEqual(bare.property("keyboardFocus"), False)
        self.assertIn("keyboardFocus", bare.styleSheet())
        self.assertIn(T.INK_FAINT, bare.styleSheet())
        QApplication.sendEvent(
            bare,
            QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason),
        )
        self.assertEqual(bare.property("keyboardFocus"), True)
        QTest.mouseClick(bare, Qt.LeftButton, pos=bare.rect().center())
        self.assertEqual(bare.property("keyboardFocus"), False)

    def test_all_segments_read_as_independent_pressable_buttons(self):
        selector = 'QPushButton[luluControl="segmentItem"]'
        self.assertIn(selector, T.QSS)
        self.assertIn(f"background: {T.BG_DEEP}; color: {T.INK_SOFT}; border: 1px solid {T.LINE};", T.QSS)
        self.assertIn(f"{selector}:pressed", T.QSS)
        self.assertIn(f"{selector}:checked:pressed", T.QSS)
        self.assertIn(f"background: {T.TERRA_DEEP}; color: #FFFFFF; border: 2px solid {T.TERRA_DEEP};", T.QSS)
        self.assertIn(f"background: {T.TERRA}; color: #FFFFFF; border: 2px solid {T.TERRA_DEEP};", T.QSS)

    def test_surfaces_and_plain_old_button_remain_separate(self):
        for role in ("page", "section", "card", "well", "popover"):
            self.assertEqual(LuluSurface(role).property("surface"), role)
        old = QPushButton("旧页面按钮")
        old.setObjectName("act")
        self.assertIsNone(old.property("role"))
        self.assertNotEqual(old.objectName(), "luluButton")

    def test_adopted_navigation_row_has_keyboard_and_accessible_activation(self):
        activated = []
        row = QFrame()
        row.setObjectName("navItem")
        adopt_nav_row(
            row,
            selected=True,
            accessible_name="导航：文案提取",
            on_activate=lambda: activated.append(True),
        )
        row.show()
        row.setFocus()
        QTest.keyClick(row, Qt.Key_Space)
        QTest.keyClick(row, Qt.Key_Return)
        self.assertEqual(activated, [True, True])
        self.assertEqual(row.objectName(), "luluNavRow")
        self.assertEqual(row.property("legacyObjectName"), "navItem")
        self.assertEqual(row.accessibleName(), "导航：文案提取")

    def test_stage2a_adapters_are_canonical_accessible_and_keyboard_stable(self):
        field = adopt_select_field_frame(QFrame(), accessible_name="初筛模型")
        self.assertEqual(field.height(), 38)
        self.assertEqual(field.focusPolicy(), Qt.StrongFocus)
        self.assertEqual(field.accessibleName(), "初筛模型")

        activated = []
        row = adopt_list_row(
            QFrame(), accessible_name="Agnes", selected=True,
            on_activate=lambda: activated.append(True),
        )
        row.show()
        row.setFocus()
        QTest.keyClick(row, Qt.Key_Return)
        self.assertEqual(activated, [True])
        self.assertTrue(row.property("selected"))

        listing = adopt_list_widget(QListWidget(), accessible_name="候选切片列表")
        spin = adopt_spin_box(QSpinBox(), accessible_name="保留数量")
        menu = adopt_menu(QMenu(), accessible_name="模型列表")
        popover = adopt_popover(QFrame(), accessible_name="模板列表")
        divider = adopt_divider(QFrame())
        editor = adopt_text_area(QPlainTextEdit(), surface="card")
        set_control_state(editor, "error")

        self.assertEqual(listing.objectName(), "luluList")
        self.assertEqual(spin.height(), 38)
        self.assertEqual(menu.property("luluControl"), "selectMenu")
        self.assertEqual(popover.property("surface"), "popover")
        self.assertEqual(divider.property("luluControl"), "divider")
        self.assertEqual(editor.property("state"), "error")

    def test_stage2b2_existing_select_and_action_surface_keep_business_state(self):
        combo = QComboBox()
        combo.setObjectName("legacyVoiceSelect")
        combo.addItem("音色 A", "voice-a")
        combo.addItem("音色 B", "voice-b")
        changes = []
        combo.currentIndexChanged.connect(changes.append)
        adopt_select_field(combo, accessible_name="选择音色")
        combo.show()
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key_Down)
        self.app.processEvents()
        self.assertEqual(combo.currentData(), "voice-b")
        self.assertEqual(changes, [1])
        self.assertEqual(combo.objectName(), "luluSelectField")
        self.assertEqual(combo.property("legacyObjectName"), "legacyVoiceSelect")
        self.assertEqual(combo.height(), 38)
        caret = combo.findChild(QLabel, "luluSelectChevron")
        self.assertIsNotNone(caret)
        self.assertFalse(caret.pixmap().isNull())

        activated = []
        source = QFrame()
        source.setObjectName("legacyPickedSource")
        adopt_action_surface(
            source,
            surface="well",
            variant="selected",
            accessible_name="已选来源",
            on_activate=lambda: activated.append(True),
        )
        source.show()
        source.setFocus()
        QTest.keyClick(source, Qt.Key_Space)
        QTest.keyClick(source, Qt.Key_Return)
        self.assertEqual(activated, [True, True])
        self.assertEqual(source.property("surfaceVariant"), "selected")
        self.assertEqual(source.accessibleName(), "已选来源")

    def test_stage2b3_slider_adapter_preserves_numeric_contract_and_keyboard_steps(self):
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("legacyEmotionSlider")
        slider.setRange(30, 110)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setValue(65)
        adopt_slider(
            slider,
            accessible_name="整体情感强度",
            value_formatter=lambda value: f"{value / 100:.2f}",
        )
        slider.show()
        self.app.processEvents()

        self.assertEqual((slider.minimum(), slider.maximum(), slider.singleStep(), slider.pageStep(), slider.value()), (30, 110, 5, 10, 65))
        self.assertEqual(slider.objectName(), "luluSlider")
        self.assertEqual(slider.property("legacyObjectName"), "legacyEmotionSlider")
        self.assertEqual(slider.property("luluControl"), "slider")
        self.assertEqual(slider.height(), 28)
        self.assertEqual(slider.accessibleName(), "整体情感强度")
        self.assertIn("0.65", slider.accessibleDescription())
        slider.setFocus()
        QTest.keyClick(slider, Qt.Key_Right)
        self.assertEqual(slider.value(), 70)
        QTest.keyClick(slider, Qt.Key_PageUp)
        self.assertEqual(slider.value(), 80)
        self.assertIn("0.80", slider.accessibleDescription())

        editor = adopt_text_area(QPlainTextEdit(), surface="card", accessible_name="待合成文本")
        self.assertEqual(editor.accessibleName(), "待合成文本")
        self.assertEqual(editor.focusPolicy(), Qt.StrongFocus)

    def test_upload_hero_has_consistent_visual_and_keyboard_contract(self):
        picked = []
        dropped = []
        area = FileDropArea(
            lambda: picked.append(True),
            lambda path: dropped.append(path),
            sub="MD · TXT · DOCX",
            title="拖入文案文件",
        )
        adopt_action_surface(
            area,
            surface="well",
            variant="uploadHero",
            accessible_name="上传文案文件",
            on_activate=lambda: picked.append(True),
        )
        area.resize(640, 130)
        area.show()
        self.app.processEvents()

        self.assertEqual(area.property("surfaceVariant"), "uploadHero")
        self.assertEqual(area.accessibleName(), "上传文案文件")
        self.assertEqual(area.focusPolicy(), Qt.StrongFocus)
        self.assertGreaterEqual(area.minimumHeight(), 130)
        self.assertEqual(area.title.text(), "拖入文案文件")
        self.assertEqual(area.action.text(), "或点击选择")
        self.assertEqual(area.sub.text(), "MD · TXT · DOCX")
        self.assertEqual(area.glyph.property("surfaceVariant"), "uploadGlyph")
        self.assertGreater(area.title.geometry().width(), 0)
        self.assertGreater(area.sub.geometry().height(), 0)
        area.setFocus()
        QTest.keyClick(area, Qt.Key_Space)
        self.assertEqual(picked, [True])

    def test_result_canvas_variants_keep_one_lightweight_surface_contract(self):
        canvas = QFrame()
        body = QFrame()
        toolbar = QFrame()
        glyph = QFrame()
        adopt_surface(canvas, "card", variant="resultCanvas")
        adopt_surface(body, "section", variant="resultBody")
        adopt_surface(toolbar, "well", variant="resultToolbar")
        adopt_surface(glyph, "well", variant="resultGlyph")
        editor = adopt_text_area(QPlainTextEdit(), surface="card", accessible_name="生成正文")
        editor.setProperty("surfaceVariant", "resultBody")

        self.assertEqual(canvas.property("surfaceVariant"), "resultCanvas")
        self.assertEqual(body.property("surfaceVariant"), "resultBody")
        self.assertEqual(toolbar.property("surfaceVariant"), "resultToolbar")
        self.assertEqual(glyph.property("surfaceVariant"), "resultGlyph")
        self.assertEqual(editor.property("surfaceVariant"), "resultBody")

    def test_source_picker_variants_keep_light_action_surface_contract(self):
        activated = []
        picker = QFrame()
        glyph = QFrame()
        adopt_action_surface(
            picker,
            surface="card",
            variant="sourcePicker",
            accessible_name="选择一篇文稿",
            on_activate=lambda: activated.append(True),
        )
        adopt_surface(glyph, "well", variant="sourcePickerGlyph")

        self.assertEqual(picker.property("surfaceVariant"), "sourcePicker")
        self.assertEqual(picker.focusPolicy(), Qt.StrongFocus)
        self.assertEqual(picker.accessibleName(), "选择一篇文稿")
        self.assertEqual(glyph.property("surfaceVariant"), "sourcePickerGlyph")
        picker.setFocus()
        QTest.keyClick(picker, Qt.Key_Space)
        self.assertEqual(activated, [True])

    def test_stage3_table_and_bulk_bar_have_stable_consumer_contracts(self):
        table = QTableWidget(2, 3)
        table.setHorizontalHeaderLabels(("选择", "标题", "状态"))
        adopt_table(table, accessible_name="素材结果表格")
        self.assertEqual(table.property("luluControl"), "table")
        self.assertEqual(table.accessibleName(), "素材结果表格")
        self.assertEqual(table.horizontalHeader().height(), 36)
        self.assertEqual(table.verticalHeader().defaultSectionSize(), 40)
        self.assertEqual(table.selectionBehavior(), QAbstractItemView.SelectRows)
        self.assertEqual(table.focusPolicy(), Qt.StrongFocus)

        bulk = adopt_bulk_bar(QFrame(), accessible_name="批量操作")
        self.assertEqual(bulk.property("luluControl"), "bulkBar")
        self.assertEqual(bulk.height(), 44)
        self.assertEqual(bulk.accessibleName(), "批量操作")

    def test_gallery_renders_paper_without_geometry_drift(self):
        for theme in ("warm",):
            T.apply_theme(theme)
            self.app.setStyleSheet(T.QSS)
            gallery = ControlGallery()
            gallery.resize(1200, 760)
            gallery.show()
            self.app.processEvents()
            summary = gallery.state_summary()
            self.assertEqual(summary["button_heights"], [34])
            self.assertTrue(summary["loading_stable"])
            self.assertTrue(summary["error_state"])
            self.assertTrue(summary["icon_accessible"])
            self.assertEqual(summary["select_count"], 3)
            self.assertGreater(gallery.grab().width(), 0)
            gallery.close()
            gallery.deleteLater()
            self.app.processEvents()

    def test_consumer_theme_layer_preserves_global_palette_and_scopes_sample_qss(self):
        global_baseline = {
            "warm": ("#F8F6F2", "#FFFFFF", "#D17B53"),
        }
        expected_consumer = {
            "warm": ("#F5EFE4", "#FBF7EE", "#537D96", "#D8CFBE", 0.14, 0.135, 0.09, 0.085),
        }

        for name, palette in T.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual((palette.BG, palette.CARD, palette.TERRA), global_baseline[name])
                consumer = T.consumer_theme(name)
                self.assertEqual(
                    (
                        consumer.canvas,
                        consumer.surface,
                        consumer.accent,
                        consumer.line,
                        consumer.paper_canvas_opacity,
                        consumer.paper_texture_opacity,
                        consumer.paper_rail_opacity,
                        consumer.paper_queue_opacity,
                    ),
                    expected_consumer[name],
                )
                self.assertEqual(consumer.radius_compact, 6)
                self.assertEqual(consumer.radius_control, 8)
                self.assertEqual(consumer.radius_surface, 12)
                self.assertEqual(consumer.radius_work, 16)
                self.assertEqual(consumer.radius_floating, 12)
                self.assertEqual(consumer.spacing, (4, 8, 12, 16, 24, 32))
                self.assertEqual(
                    (consumer.surface_work, consumer.surface_focal, consumer.surface_floating),
                    (consumer.surface_alt, consumer.surface, consumer.surface),
                )
                self.assertEqual(
                    (consumer.type_page, consumer.type_section, consumer.type_card,
                     consumer.type_body, consumer.type_caption),
                    (24, 16, 14, 12, 11),
                )
                self.assertEqual(
                    (consumer.icon_feature, consumer.icon_control, consumer.icon_compact, consumer.icon_stroke),
                    (20, 16, 13, 1.8),
                )
                self.assertEqual(consumer.control_shadow_alpha, 0)
                self.assertEqual(consumer.popup_max_height, 260)
                qss = T.build_qss(palette)
                self.assertIn('[consumerThemeScope="transcriptionWorkbench"]', qss)
                self.assertIn('[consumerThemeScope="globalShell"]', qss)
                self.assertIn('[consumerThemeScope="coreWorkspace"]', qss)
                self.assertIn(consumer.canvas, qss)
                self.assertIn(consumer.accent, qss)
                self.assertIn("QMenu[consumerThemeScope=", qss)
                workspace_qss = T.build_consumer_core_workspace_qss(consumer)
                self.assertIn(
                    'QWidget#root[consumerThemeScope="coreWorkspace"]',
                    workspace_qss,
                )
                self.assertIn(consumer.surface_focal, workspace_qss)
                self.assertIn(consumer.accent, workspace_qss)
                self.assertNotIn('consumerThemeScope="globalShell"', workspace_qss)
                self.assertNotIn('consumerThemeScope="transcriptionWorkbench"', workspace_qss)
                self.assertIn('surfaceVariant="workbenchIntake"', qss)
                self.assertIn('surfaceVariant="taskCard"', qss)
                self.assertIn("padding-top: 1px", qss)
                self.assertIn("qlineargradient", qss)
                self.assertIn("QFrame#studioTitleRule", qss)
                self.assertIn(consumer.paper_edge, qss)
                self.assertIn(
                    f"border: {consumer.border_width}px dashed {consumer.accent_hairline}",
                    qss,
                )
                self.assertIn(consumer.accent_hairline_active, qss)
                upload_start = qss.index(
                    'QWidget[consumerThemeScope="transcriptionWorkbench"] '
                    'QFrame[luluControl="surface"][surfaceVariant="uploadHero"] {'
                )
                upload_end = qss.index('surfaceVariant="uploadHero"]:hover', upload_start)
                self.assertNotIn("qlineargradient", qss[upload_start:upload_end])
                self.assertIn(f"background: {consumer.secondary}", qss[upload_start:upload_end])
                glyph_start = qss.index(
                    'QWidget[consumerThemeScope="transcriptionWorkbench"] '
                    'QFrame[luluControl="surface"][surfaceVariant="uploadGlyph"] {'
                )
                self.assertIn(
                    f"background: {consumer.accent_wash}",
                    qss[glyph_start:],
                )
                self.assertIn("QFrame#titlebarPaperCenter", qss)
                self.assertIn("QFrame#titlebarPaperLeft", qss)
                self.assertNotIn("QFrame#titlebarPaperRight", qss)
                self.assertIn('[panelAction="paperSecondary"]', qss)
                self.assertNotIn('[sampleAccent="true"]', qss)
                # The current shell has a sidebar and centre; model selection
                # belongs to the workbench. Do not restore retired right-rail CSS.
                for retired in ('[modelWorkTag="true"]', '[paperModelTag="true"]',
                                'QFrame#rightPanelWrap', 'QFrame#titlebarPaperRight'):
                    self.assertNotIn(retired, qss)
                self.assertNotIn("QLabel#brandLogo {", qss)
                self.assertIn('QFrame#sidebar[consumerThemeScope="globalShell"]', qss)
                center_start = qss.index('QFrame#titlebarPaperCenter[consumerThemeScope="globalShell"] {')
                center_end = qss.index('QWidget#brandRow[consumerThemeScope="globalShell"]', center_start)
                center_qss = qss[center_start:center_end]
                self.assertIn(f"background: {palette.BG}", center_qss)
                self.assertNotIn("border-left", center_qss)
                self.assertIn(f"border-right: 1px solid {palette.LINE}", qss)
                self.assertIn(f"border-right: 1px solid {consumer.line}", qss)
                intake_start = qss.index('surfaceVariant="workbenchIntake"] {')
                intake_end = qss.index('surfaceVariant="linkCard"]', intake_start)
                intake_qss = qss[intake_start:intake_end]
                self.assertIn(
                    f"border: {consumer.border_width}px solid {consumer.line}",
                    intake_qss,
                )
                self.assertNotIn("border-bottom:", intake_qss)
                self.assertIn(f"border: {consumer.border_width}px solid {consumer.line}", qss)
                self.assertNotIn("border-top-color:", qss)

        self.assertIs(T.consumer_theme("unknown"), T.CONSUMER_THEMES["warm"])
        self.assertEqual(icons.CONSUMER_ICON_SIZES, {"feature": 20, "control": 16, "compact": 13})
        self.assertEqual(icons.CONSUMER_ICON_STROKE, 1.8)
        compact_icon = icons.consumer_pixmap("link", "#537D96", "compact")
        self.assertEqual(compact_icon.width() / compact_icon.devicePixelRatio(), 13)
        with self.assertRaises(ValueError):
            icons.consumer_pixmap("link", "#537D96", "decorative")

    def test_original_paper_texture_is_seamless_asset_and_strength_is_clamped(self):
        path = PaperTextureFrame.texture_path()
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "paper_texture.png")
        pixmap = PaperTextureFrame.texture_pixmap()
        self.assertFalse(pixmap.isNull())
        self.assertEqual((pixmap.width(), pixmap.height()), (768, 768))
        fabric_path = PaperTextureFrame.fabric_texture_path()
        self.assertTrue(fabric_path.is_file())
        self.assertEqual(fabric_path.name, "model_tag_fabric_texture.png")
        fabric = PaperTextureFrame.fabric_texture_pixmap()
        self.assertFalse(fabric.isNull())
        self.assertEqual((fabric.width(), fabric.height()), (512, 512))

        frame = PaperTextureFrame(strength=0.08, corner_radius=16, layer="work")
        self.assertEqual(frame.property("paperTextureLayer"), "work")
        self.assertEqual(frame.property("paperTextureStrength"), 0.08)
        self.assertEqual(frame.property("paperTextureRadius"), 16.0)
        frame.set_paper_texture_strength(4)
        self.assertEqual(frame.property("paperTextureStrength"), 1.0)
        frame.set_paper_texture_strength(-1)
        self.assertEqual(frame.property("paperTextureStrength"), 0.0)

    def test_paper_texture_frame_can_draw_a_structure_free_fabric_model_tag(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                consumer = T.consumer_theme(theme_name)
                frame = PaperTextureFrame(strength=0.34, corner_radius=10, layer="modelTag")
                frame.setFixedSize(160, 72)
                frame.setStyleSheet(
                    f"background: {consumer.surface_work}; border: 1px solid {consumer.line}; "
                    "border-radius: 10px;"
                )
                frame.setProperty("fabricTag", True)
                frame.setProperty("fabricEdgeColor", consumer.line)
                frame.setProperty("fabricStitchColor", consumer.accent)
                frame.setProperty("fabricEdgeOpacity", 0.38)
                frame.setProperty(
                    "fabricStitchOpacity",
                    0.34,
                )
                frame.show()
                self.app.processEvents()

                image = QImage(frame.size(), QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(Qt.GlobalColor.transparent)
                frame.render(image)
                fabric_body = image.pixelColor(90, 35)
                accent_thread = max(
                    (
                        image.pixelColor(x, y)
                        for y in range(4, 17)
                        for x in range(4, 156)
                    ),
                    key=lambda color: color.blue() - color.red(),
                )
                self.assertGreater(
                    accent_thread.blue() - accent_thread.red(),
                    fabric_body.blue() - fabric_body.red(),
                )
                # The accent remains identifiable, but its dyed-cloth wash must
                # stay close enough to the blanket that it cannot regress into
                # a high-contrast plastic badge.
                thread_distance = sum(
                    abs(a - b)
                    for a, b in zip(
                        (
                            accent_thread.red(),
                            accent_thread.green(),
                            accent_thread.blue(),
                        ),
                        (fabric_body.red(), fabric_body.green(), fabric_body.blue()),
                    )
                )
                self.assertLess(thread_distance, 180)
                fabric_colors = {
                    image.pixelColor(x, y).rgba()
                    for y in range(18, 58, 5)
                    for x in range(34, 145, 7)
                }
                self.assertGreater(
                    len(fabric_colors),
                    8,
                )
                self.assertFalse(bool(frame.property("paperTagLayered")))
                self.assertEqual(len(frame.findChildren(QFrame)), 0)
                frame.close()

    def test_paper_texture_frame_can_draw_a_texture_only_model_tag(self):
        for theme_name in ("warm",):
            with self.subTest(theme=theme_name):
                consumer = T.consumer_theme(theme_name)
                frame = PaperTextureFrame(
                    strength=0.18,
                    corner_radius=10,
                    layer="modelPaperTag",
                )
                frame.setFixedSize(220, 72)
                frame.setStyleSheet(
                    f"background: {consumer.surface}; border: 1px solid {consumer.line}; "
                    "border-radius: 10px;"
                )
                frame.setProperty("paperModelTag", True)
                frame.show()
                self.app.processEvents()

                image = QImage(frame.size(), QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(Qt.GlobalColor.transparent)
                frame.render(image)
                paper_colors = {
                    image.pixelColor(x, y).rgba()
                    for y in range(26, 64, 5)
                    for x in range(22, 145, 7)
                }
                # Dark paper deliberately compresses the fibre palette; four
                # distinct sampled values still prove that it is not a flat fill.
                self.assertGreater(len(paper_colors), 3)
                self.assertFalse(bool(frame.property("paperModelAccentColor")))
                self.assertFalse(bool(frame.property("fabricTag")))
                self.assertEqual(len(frame.findChildren(QFrame)), 0)
                frame.close()

    def test_consumer_hover_feedback_is_color_only_and_keeps_paint_bounds(self):
        surface = QFrame()
        adopt_consumer_hover_motion(surface, color="#2A2622", duration_ms=40)
        self.assertEqual(surface.property("consumerMotion"), "colorOnly")
        self.assertEqual(surface.property("consumerMotionDuration"), 40)
        self.assertEqual(surface.property("consumerMotionColor"), "#2A2622")
        self.assertIsNone(surface.graphicsEffect())


if __name__ == "__main__":
    unittest.main()

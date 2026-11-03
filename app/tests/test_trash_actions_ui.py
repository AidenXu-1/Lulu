"""Isolated real Qt/WebEngine interactions, no user files or model calls."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_agent_workspace_ui as ui
from src.core import library
from src.core.agent_workspace.service import WorkspaceService


class TrashActionsUiTests(unittest.TestCase):
    setUpClass=ui.AgentWorkspaceUiTests.__dict__['setUpClass']
    setUp=ui.AgentWorkspaceUiTests.setUp
    tearDown=ui.AgentWorkspaceUiTests.tearDown
    until=ui.AgentWorkspaceUiTests.until
    tick=ui.AgentWorkspaceUiTests.tick
    js=ui.AgentWorkspaceUiTests.js

    def trash_rows(self, count=4):
        for i in range(count): library.delete(library.save_document('正文',f'文稿{i}'))
        self.widget._set_kind('trash')
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]").length')==count)


    def test_archive_empty_page_is_explicit_and_has_no_auto_delete(self):
        self.widget._set_kind('trash')
        self.until(lambda:self.js('Boolean(document.querySelector(".trash-heading"))'))
        self.assertIn('归档',self.js('document.querySelector(".trash-heading").textContent'))
        self.assertIn('长期保留',self.js('document.querySelector(".trash-heading").textContent'))
        self.assertFalse(self.js('Boolean(document.getElementById("trash-auto"))'))
        self.assertIn('归档文稿或音频后',self.js('document.querySelector(".trash-empty-state").textContent'))

    def test_archive_can_restore_with_no_automatic_cleanup(self):
        self.trash_rows(2)
        self.until(lambda:self.js('!document.getElementById("trash-restore").disabled'))
        self.js('document.getElementById("trash-restore").click()')
        self.until(lambda:len(library.list_deleted())==1)
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]").length')==1)

    def test_archive_delete_names_system_trash_and_failure_keeps_material(self):
        self.trash_rows(2)
        self.until(lambda:self.js('!document.getElementById("trash-purge").disabled'))
        self.js('document.getElementById("trash-purge").click()')
        self.until(lambda:self.js('document.getElementById("dialog").open'))
        self.assertIn('电脑废纸篓',self.js('document.getElementById("dialog-body").textContent'))
        from src.core import trash_cleanup
        with patch.object(trash_cleanup,'move_to_system_trash',side_effect=OSError('模拟系统拒绝')):
            self.js('document.getElementById("purge-confirm").click()')
            self.until(lambda:self.js('document.getElementById("trash-status").textContent.includes("保留") || document.getElementById("trash-status").textContent.includes("仍在")'))
        self.assertEqual(len(library.list_deleted()),2)

    def test_archive_direct_command_shift_selection_and_select_all(self):
        self.trash_rows(6)
        self.until(lambda:self.js('document.querySelectorAll(".trash-item").length')==6)
        self.assertFalse(self.js('Boolean(document.getElementById("trash-select-mode"))'))
        self.js('document.querySelectorAll("[data-trash]")[1].dispatchEvent(new MouseEvent("click",{bubbles:true,metaKey:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),1)
        self.js('document.querySelectorAll("[data-trash]")[4].dispatchEvent(new MouseEvent("click",{bubbles:true,shiftKey:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),4)
        self.assertEqual(self.js('document.getElementById("trash-select-visible").getAttribute("aria-checked")'),'mixed')
        self.assertFalse(self.js('document.getElementById("trash-bulk-bar").hidden'))
        self.js('document.querySelectorAll("[data-trash]")[2].dispatchEvent(new MouseEvent("click",{bubbles:true,metaKey:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),3)
        self.js('document.getElementById("trash-select-visible").click()')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),6)
        self.assertEqual(self.js('document.getElementById("trash-select-visible").getAttribute("aria-checked")'),'true')
        self.js('document.getElementById("utility-page").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),0)
        self.js('document.querySelectorAll("[data-trash]")[1].querySelector(".trash-check").dispatchEvent(new MouseEvent("click",{bubbles:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),1)
        self.js('document.querySelectorAll("[data-trash]")[0].click()')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),0)

    def test_archive_layout_bulk_bar_and_narrow_window(self):
        self.trash_rows(6)
        self.until(lambda:self.js('document.querySelectorAll(".trash-item").length')==6 and self.js('document.getElementById("trash-title").textContent.length>0'))
        self.until(lambda:self.js('document.getAnimations().every(animation=>animation.playState!=="running")'))
        capture=os.environ.get('LULU_PRODUCT_CAPTURE')
        if capture:
            from pathlib import Path
            Path(capture).mkdir(parents=True,exist_ok=True)
            self.widget.grab().save(str(Path(capture)/'archive-default.png'))
        self.assertTrue(self.js('document.querySelector(".trash-shell").getBoundingClientRect().right<=document.getElementById("utility-page").getBoundingClientRect().right+1'))
        self.js('document.querySelectorAll("[data-trash]")[0].dispatchEvent(new MouseEvent("click",{bubbles:true,metaKey:true}));document.querySelectorAll("[data-trash]")[3].dispatchEvent(new MouseEvent("click",{bubbles:true,shiftKey:true}))')
        self.assertEqual(self.js('document.querySelectorAll("[data-trash][data-checked=true]").length'),4)
        self.until(lambda:self.js('document.getAnimations().every(animation=>animation.playState!=="running")'))
        self.assertTrue(self.js('document.getElementById("trash-bulk-bar").getBoundingClientRect().left>=document.querySelector(".trash-shell").getBoundingClientRect().left'))
        if capture:
            from pathlib import Path
            self.widget.grab().save(str(Path(capture)/'archive-selection.png'))
        self.widget.resize(1100,760)
        self.until(lambda:self.js('document.getAnimations().every(animation=>animation.playState!=="running")'))
        self.assertTrue(self.js('document.querySelector(".trash-shell").getBoundingClientRect().right<=document.getElementById("utility-page").getBoundingClientRect().right+1'))
        self.assertTrue(self.js('document.getElementById("trash-bulk-bar").getBoundingClientRect().right<=document.querySelector(".trash-shell").getBoundingClientRect().right+1'))
        if capture:
            from pathlib import Path
            self.widget.grab().save(str(Path(capture)/'archive-narrow.png'))

    def test_archive_real_metadata_filter_sort_and_audio_preview(self):
        import wave
        eid=library.save_document('第一版','真实历史文稿')
        library.save_markdown(eid,'第二版')
        library.delete(eid)
        audio=self.root/'archive.wav'
        with wave.open(str(audio),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000);stream.writeframes(b'\0'*16000)
        voice=library.save_voice(title='归档音频',audio_path=audio,text='生成时正文',engine='test',voice_name='测试音色',config_summary='test',duration_seconds=1)
        library.delete(voice)
        self.widget._set_kind('trash')
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]").length')==2)
        self.js('document.querySelector("[data-trash-filter=voice]").click()')
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]:not([hidden])").length')==1 and self.js('!document.getElementById("trash-audio").hidden'))
        self.until(lambda:self.js('document.getElementById("trash-audio-wave").dataset.state==="ready"'))
        self.js('document.getElementById("trash-audio-play").click()')
        self.until(lambda:self.widget.playing_id==voice)
        self.js('document.getElementById("trash-audio-play").click()')
        self.js('document.querySelector("[data-trash-filter=all]").click()')
        self.until(lambda:self.js('document.querySelectorAll("[data-trash]:not([hidden])").length')==2)
        self.js('document.querySelector("[data-trash][data-kind=document]").click()')
        self.until(lambda:self.js('document.getElementById("trash-title").textContent==="真实历史文稿"'))
        self.js('document.querySelector("[data-trash-view=detail]").click()')
        self.assertIn('1 个完整正文版本',self.js('document.getElementById("trash-details").textContent'))
        self.js('document.getElementById("trash-sort").click()')
        self.until(lambda:self.js('Boolean(document.querySelector(".lulu-menu"))'))
        self.js('Array.from(document.querySelectorAll(".lulu-menu [role=option]")).find(b=>b.textContent.includes("按名称")).click()')
        self.until(lambda:self.js('document.querySelector("[data-trash] .trash-item-copy strong").textContent==="归档音频"'))
        self.assertFalse(self.js('Boolean(document.querySelector(".trash-group>h3"))'))

    def test_document_context_menu_has_separate_archive_and_system_delete(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt")) && !window.lulu.state.loading'))
        self.js('document.querySelector("[data-entry]").dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:100,clientY:300}))')
        self.until(lambda:self.js('Boolean(document.querySelector(".lulu-menu"))'))
        labels=json.loads(self.js('JSON.stringify(Array.from(document.querySelectorAll(".lulu-menu button")).map(b=>b.textContent.trim()))'))
        self.assertIn('归档',labels);self.assertIn('删除',labels)
        self.assertFalse(any('最近删除' in label for label in labels))

    def test_batch_selection_preserves_list_node_and_actions_are_labelled_readable(self):
        self.widget.open_entry(self.eid)
        self.until(lambda:self.js('Boolean(document.querySelector("#prompt")) && !window.lulu.state.loading'))
        self.js('document.getElementById("select-mode").click()')
        self.until(lambda:self.js('window.lulu.state.batch'))
        self.js('window.originalListRow=document.querySelector("[data-entry]");originalListRow.click()')
        self.assertTrue(self.js('originalListRow===document.querySelector("[data-entry]")'))
        self.assertIn('更多',self.js('document.getElementById("batch-more").textContent'))
        self.assertGreaterEqual(self.js('document.getElementById("batch-more").getBoundingClientRect().height'),32)
        self.assertGreaterEqual(self.js('parseFloat(getComputedStyle(document.getElementById("batch-export")).fontSize)'),13)

    def test_audio_context_menu_has_correct_open_archive_and_delete_actions(self):
        import wave
        audio=self.root/'sample.wav'
        with wave.open(str(audio),'wb') as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000);stream.writeframes(b'\0'*1600)
        library.save_voice(title='隔离音频',audio_path=audio,text='正文',engine='test',voice_name='测试',config_summary='test')
        self.widget._set_kind('voice')
        self.until(lambda:self.js('window.lulu.state.kind==="voice" && document.querySelectorAll("[data-entry]").length===1 && !window.lulu.state.loading'))
        self.js('document.querySelector("[data-entry]").dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:100,clientY:250}))')
        self.until(lambda:self.js('Boolean(document.querySelector(".lulu-menu"))'))
        labels=json.loads(self.js('JSON.stringify(Array.from(document.querySelectorAll(".lulu-menu button")).map(b=>b.textContent.trim()))'))
        self.assertIn('打开音频',labels);self.assertIn('归档',labels);self.assertIn('删除',labels)
        self.assertNotIn('导出文稿',labels)

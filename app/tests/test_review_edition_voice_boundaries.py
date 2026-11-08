"""Isolated follow-up review: no real libraries, credentials, network or models."""
import json
import shlex
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from src import edition
from src.core.synthesis import voices
from src.core.updater.models import UpdateSnapshot
from src.ui import cli_info
from src.ui.approved_settings import ApprovedSettings, _update
from src.ui.update_ui import UpdateController, _state_presentation
from tests import test_voice_storage_integrity as voice_fixture


class DevelopmentUiBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_experience_prompt_uses_current_bundle_without_stable_skill_install(self):
        app = Path('/local/用户 Applications/Lulu 开发体验版.app')
        with patch.object(edition, 'is_experience', return_value=True), \
                patch('sys.frozen', True, create=True), patch('sys.executable', str(app/'Contents/MacOS/Lulu')):
            prompt = cli_info.agent_skill_install_prompt(cli_info.AGENT_SKILL_REPOSITORY_URL)
        self.assertIn(str(app/'Contents/MacOS/lulu-agent-cli'), prompt)
        self.assertNotIn('/Applications/Lulu.app', prompt)
        self.assertNotIn('下载并安装 lulu-agent Skill', prompt)
        self.assertIn('capabilities --output json', prompt)
        self.assertIn('停止', prompt)

    def test_settings_cli_metadata_is_bound_to_the_running_experience(self):
        app = Path('/local/Applications/Lulu 开发体验版.app')
        shell = Mock(current_category='cli', update_controller=None)
        with patch.object(edition,'is_experience',return_value=True), \
                patch('sys.frozen',True,create=True),patch('sys.executable',str(app/'Contents/MacOS/Lulu')):
            ApprovedSettings._opened(shell)
        initial = next(c.args[1] for c in shell.approved.bridge.send.call_args_list if c.args[0]=='initial')
        self.assertEqual(initial['meta']['cliPath'],str(app/'Contents/MacOS/lulu-agent-cli'))

    def test_stable_prompt_keeps_public_skill_and_canonical_cli(self):
        with patch.object(edition,'is_experience',return_value=False),patch('sys.frozen',False,create=True):
            prompt=cli_info.agent_skill_install_prompt(cli_info.AGENT_SKILL_REPOSITORY_URL)
        self.assertIn('下载并安装 lulu-agent Skill',prompt)
        self.assertIn('/Applications/Lulu.app/Contents/MacOS/lulu-agent-cli',prompt)

    def test_disabled_update_is_information_without_retry_even_for_persisted_error(self):
        with patch.object(edition,'is_experience',return_value=True):
            for snapshot in (UpdateSnapshot(),UpdateSnapshot(ui_state='check_error',error_code='development_edition')):
                with self.subTest(snapshot=snapshot):
                    shown=_update(snapshot)
                    self.assertEqual(shown['state'],'disabled')
                    self.assertEqual(shown['presentation'][2:4],['',''])
                    self.assertIn('开发体验版',shown['presentation'][0])
                    self.assertNotIn('无法连接',shown['presentation'][1])

    def test_experience_auto_check_never_initializes_service(self):
        with patch.object(edition,'is_experience',return_value=True):
            controller=UpdateController()
            controller._result_timer.stop()
            with patch.object(controller,'_ensure_service') as ensure:
                self.assertFalse(controller.maybe_auto_check(True))
                ensure.assert_not_called()
            self.assertIn('开发体验版',_state_presentation(controller.snapshot)[1])
            controller.deleteLater()

    def test_actual_settings_page_hides_disabled_updates_and_copies_correct_connection(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtGui import QGuiApplication
        from src.config import AppConfig
        from tests.test_settings_skill_navigation import SettingsSkillNavigationTests as WebFixture
        self.until=WebFixture.until.__get__(self)
        self.js=WebFixture.js.__get__(self)
        self.navigate=WebFixture.navigate.__get__(self)
        for development in (True,False):
            with self.subTest(development=development),patch.object(edition,'is_experience',return_value=development):
                controller=UpdateController();controller._result_timer.stop()
                self.page=ApprovedSettings(AppConfig(auto_update_enabled=False,notification_policy='never'),
                    update_controller=controller,agent_skill_status_provider=lambda:'not_installed',lazy_categories=True)
                try:
                    self.page.resize(960,760);self.page.show()
                    self.until(lambda:self.page.approved.bridge.is_ready)
                    self.navigate('about')
                    self.until(lambda:bool(self.js("document.querySelector('#software-update')!==null")))
                    controls=json.loads(self.js("JSON.stringify({auto:!!document.querySelector('#software-update [data-action=toggle]'),check:!!document.querySelector('#software-update [data-action=check-update]'),text:document.querySelector('#software-update').textContent})"))
                    # Check the actual toggle independently of its delegated action name.
                    self.assertEqual(bool(self.js("document.querySelector('#software-update [role=switch]')!==null")),not development)
                    self.assertEqual(controls['check'],not development)
                    if development:self.assertIn('开发体验版单独更新',controls['text'])
                    self.navigate('cli')
                    self.until(lambda:bool(self.js("document.querySelector('[data-action=copy-install]')!==null")))
                    clipboard=Mock()
                    with patch.object(QGuiApplication,'clipboard',return_value=clipboard):
                        self.js("document.querySelector('[data-action=copy-install]').click();void 0")
                        self.until(lambda:clipboard.setText.called)
                    prompt=clipboard.setText.call_args.args[0]
                    self.assertEqual(prompt,cli_info.agent_skill_install_prompt(cli_info.AGENT_SKILL_REPOSITORY_URL))
                    if development:
                        self.assertNotIn('/Applications/Lulu.app',prompt)
                        self.assertIn('复制连接提示词',self.js("document.querySelector('[data-action=copy-install]').textContent"))
                    self.js("document.querySelector('[data-action=cli-reference]').click();void 0")
                    self.until(lambda:bool(self.js("document.querySelector('#modal .code-box')!==null")))
                    example=self.js("document.querySelectorAll('#modal .code-box')[1].textContent")
                    command=shlex.quote(cli_info.agent_cli_path())
                    self.assertEqual(example,f'{command} version --output json\n\n{command} capabilities --output json')
                    if development:self.assertNotIn('先由 Skill',self.js("document.querySelector('#modal').textContent"))
                    clipboard=Mock()
                    with patch.object(QGuiApplication,'clipboard',return_value=clipboard):
                        self.js("document.querySelector('[data-action=copy-cli-path]').click();void 0")
                        self.until(lambda:clipboard.setText.called)
                    self.assertEqual(shlex.split(clipboard.setText.call_args.args[0]),[cli_info.agent_cli_path()])
                finally:
                    self.page.shutdown_workers();self.page.close();self.page.deleteLater()
                    controller.shutdown(1000);controller.deleteLater()
                    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);self.app.processEvents()

    def test_native_update_panels_do_not_offer_retry_or_auto_toggle_for_experience(self):
        from src.ui.update_ui import UpdatePanel,AboutUpdatePanel
        from tests.test_auto_update_ui import Config
        with patch.object(edition,'is_experience',return_value=True):
            controller=UpdateController();controller._result_timer.stop()
            for panel in (UpdatePanel(Config(),controller),AboutUpdatePanel(Config(),controller,
                    repository_url='repo',author_url='author',product_boundary='local',open_url=lambda _:None,show_contact=lambda:None)):
                panel.show();self.app.processEvents()
                self.assertFalse(panel.auto.isVisible())
                self.assertFalse(panel.primary.isVisible())
                self.assertIn('开发体验版',panel.state_title.text())
                panel.close();panel.deleteLater()
            controller.deleteLater()


class VoiceBoundaryTests(unittest.TestCase):
    setUp=voice_fixture.VoiceStorageIntegrityTests.setUp
    tearDown=voice_fixture.VoiceStorageIntegrityTests.tearDown

    def test_external_reference_matching_id_and_filename_never_rebases(self):
        external=self.root/'elsewhere'/self.voice.id/'reference.wav'
        external.parent.mkdir(parents=True);external.write_bytes(b'external voice')
        row=json.loads(self.before)[0];row['reference_audio_path']=str(external)
        voices.INDEX.write_text(json.dumps([row]));before=voices.INDEX.read_bytes()
        self.assertEqual(voices.get_voice(self.voice.id).reference_audio_path,str(external))
        self.assertEqual(voices.INDEX.read_bytes(),before)
        self.assertEqual(external.read_bytes(),b'external voice')

    def test_relocated_owned_voice_survives_edit_and_second_relocation(self):
        first=self.root/'moved';voices.relocate_voice_library(first)
        self.old.rename(self.root/'old-kept')
        voices.update_voice(self.voice.id,name='Renamed')
        second=self.root/'moved-again';voices.relocate_voice_library(second)
        first.rename(self.root/'first-kept')
        voice=voices.get_voice(self.voice.id)
        self.assertEqual(Path(voice.reference_audio_path),second/self.voice.id/'reference.wav')
        self.assertEqual(Path(voice.reference_audio_path).read_bytes(),self.sample.read_bytes())

    def test_partial_delete_reports_failure_keeps_index_and_allows_retry(self):
        folder=self.old/self.voice.id
        remain=folder/'cannot-delete/remaining.txt';remain.parent.mkdir();remain.write_text('remaining')
        def partially_remove(path, **kwargs):
            Path(self.voice.reference_audio_path).unlink()
            if not kwargs.get('ignore_errors'):
                raise PermissionError('injected removal failure')
        with patch.object(voices.shutil,'rmtree',side_effect=partially_remove):
            with self.assertRaisesRegex(voices.VoiceLibraryError,'重试'):
                voices.delete_voice(self.voice.id)
        self.assertEqual(voices.INDEX.read_bytes(),self.before)
        self.assertTrue(remain.exists())
        voices.delete_voice(self.voice.id)
        self.assertFalse(folder.exists())
        self.assertIsNone(voices.get_voice(self.voice.id))
        self.assertTrue(self.sample.exists())

    def test_index_failure_after_file_removal_is_reported_and_retryable(self):
        with patch.object(voices,'_write',side_effect=OSError('injected index failure')):
            with self.assertRaisesRegex(voices.VoiceLibraryError,'列表.*重试'):
                voices.delete_voice(self.voice.id)
        self.assertFalse((self.old/self.voice.id).exists())
        self.assertEqual(voices.INDEX.read_bytes(),self.before)
        voices.delete_voice(self.voice.id)
        self.assertEqual(voices.list_voices(),[])

    def test_delete_never_follows_voice_folder_symlink(self):
        import shutil
        folder=self.old/self.voice.id;shutil.rmtree(folder)
        folder.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises((ValueError,OSError)):
            voices.delete_voice(self.voice.id)
        self.assertEqual(voices.INDEX.read_bytes(),self.before)
        self.assertTrue(self.sample.exists())


if __name__=='__main__':unittest.main()

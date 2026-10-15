import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from uuid import uuid4

from src.core import library, library_gateway


class WorkspaceCoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for name, value in (("LIBRARY_DIR", self.root / "library"),
                            ("FOLDERS_FILE", self.root / "library/folders.json")):
            p = patch.object(library, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_recycle_restore_keeps_latest_markdown_and_audio(self):
        eid = library.save_document("原文", "文稿")
        md = library.markdown_path(eid)
        md.write_text("外部更新")
        library.delete(eid)
        self.assertEqual(library.list_entries(), [])
        self.assertEqual(library.query_entries()["total"], 0)
        receipt = library.list_deleted()[0]
        self.assertEqual(library.restore_deleted(receipt["token"]), eid)
        self.assertEqual(library.load_markdown(eid), "外部更新")
        self.assertEqual(library.list_deleted(), [])
        audio = self.root / "a.wav"
        audio.write_bytes(b"audio")
        voice = library.save_voice(title="音频", audio_path=audio, text="正文", engine="test", voice_name="v", config_summary="test")
        library.delete(voice)
        self.assertEqual(audio.read_bytes(), b"audio")


    def test_restore_never_overwrites_existing_document(self):
        eid = library.save_document("原文", "文稿")
        library.delete(eid)
        receipt = library.list_deleted()[0]
        target = library.LIBRARY_DIR / f"{eid}.json"
        target.write_text("new content")
        with self.assertRaises(library.RevisionConflict):
            library.restore_deleted(receipt["token"])
        self.assertEqual(target.read_text(), "new content")
        self.assertEqual(len(library.list_deleted()), 1)

    def test_queries_read_external_body_without_writing_index(self):
        eid = library.save_document("原文", "文稿")
        raw = library.LIBRARY_DIR / f"{eid}.json"
        before = raw.read_bytes()
        library.markdown_path(eid).write_text("新增搜索关键词")
        result = library.query_entries(search="关键词")
        self.assertEqual(result["total"], 1)
        self.assertNotIn("text", result["entries"][0])
        self.assertEqual(raw.read_bytes(), before)

    def test_library_counts_ignore_search_and_follow_delete_restore(self):
        first = library.save_document('第一篇', '文稿一')
        library.save_document('第二篇', '文稿二')
        audio = self.root / 'voice.wav'
        audio.write_bytes(b'audio')
        library.save_voice(title='配音', audio_path=audio, text='正文', engine='test', voice_name='v', config_summary='test')
        result = library.query_entries(kind='documents', search='找不到')
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['library_counts'], {'documents': 2, 'voice': 1})
        library.delete(first)
        self.assertEqual(library.query_entries(kind='voice')['library_counts'], {'documents': 1, 'voice': 1})
        library.restore_deleted(library.list_deleted()[0]['token'])
        self.assertEqual(library.query_entries()['library_counts'], {'documents': 2, 'voice': 1})

    def test_queries_update_missing_audio_state(self):
        audio = self.root / "a.wav"
        audio.write_bytes(b"audio")
        eid = library.save_voice(title="音频", audio_path=audio, text="正文", engine="test", voice_name="v", config_summary="test")
        self.assertFalse(library.query_entries()["entries"][0]["missing_flags"]["audio_missing"])
        audio.unlink()
        self.assertTrue(library.query_entries()["entries"][0]["missing_flags"]["audio_missing"])
        for invalid_path in ('', str(self.root)):
            raw = library._load_raw(eid)
            raw['audio_path'] = invalid_path
            library._write_raw(raw)
            self.assertTrue(library.load_voice(eid)['missing_flags']['audio_missing'])

    def test_malformed_metadata_is_isolated_without_hiding_healthy_rows(self):
        library.save_document("健康正文", "正常文稿")
        for index, field in enumerate(("kind", "markdown_path", "folder_id")):
            (library.LIBRARY_DIR / f"bad{index}.json").write_text(json.dumps({"id": f"bad{index}", field: {"invalid": 1}}))
        result = library.query_entries()
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["errors"]), 3)


    def test_frozen_gui_uses_measured_core_adapter_for_write_and_read(self):
        import sys
        calls = []
        def core(command, arguments):
            calls.append(command)
            with patch.object(sys, "frozen", False):
                return library_gateway.handle(arguments)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "/fixture/Lulu"), \
                patch("src.core.agent_entry.entrypoints.build_default_gui_core_dispatcher", return_value=core):
            eid = library.save_document("共用正文", "文稿")
            self.assertEqual(library.load_markdown(eid), "共用正文")
        self.assertEqual(calls, ["gui.library", "gui.library"])


    def test_cli_query_has_no_filesystem_write(self):
        from src.agent_cli.library_commands import execute
        eid = library.save_document("可搜索正文", "文稿")
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        runtime = Mock()
        runtime._application = Mock(spec=["assert_library_read_allowed"])
        result = execute(["library", "list", "--search", "搜索", "--output", "json"], runtime)
        self.assertEqual(result["total"], 1)
        shown = execute(["library", "show", "--entry-id", eid, "--output", "json"], runtime)
        self.assertEqual(shown["entry"]["text"], "可搜索正文")
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        runtime._application.assert_library_read_allowed.assert_any_call("LIB-01")

    def test_mutation_receipt_replays_without_duplicate_and_keeps_unknown_io_pending(self):
        eid = library.save_document("原文", "文稿")
        request = {"operation": "append_markdown_section", "args": [eid, "摘要", "结果"], "kwargs": {}, "request_id": uuid4().hex}
        first = library_gateway.handle(request)
        self.assertTrue(first["ok"])
        self.assertEqual(library_gateway.handle(request), first)
        self.assertEqual(library.load_markdown(eid).count("## 摘要"), 1)
        conflict = {**request, "args": [eid, "摘要", "另一内容"]}
        self.assertEqual(library_gateway.handle(conflict)["error"], "RevisionConflict")
        uncertain = {**request, "request_id": uuid4().hex}
        with patch.object(library, "append_markdown_section", side_effect=OSError("disk full")) as write:
            self.assertEqual(library_gateway.handle(uncertain)["error"], "OutcomeUnknown")
            self.assertEqual(library_gateway.handle(uncertain)["error"], "OutcomeUnknown")
            write.assert_called_once()

    def test_large_transcript_round_trips_over_bounded_native_messages(self):
        text = "长逐字稿正文。" * 250000
        request = {"operation": "save_document", "args": [text, "长文"], "kwargs": {}, "request_id": uuid4().hex}
        packed = library_gateway._pack(request)
        self.assertLess(len(json.dumps(packed).encode()), 4 * 1024 * 1024)
        library_gateway.validate_request(packed)
        saved = library_gateway._unpack(library_gateway.handle(packed))
        loaded = library_gateway.handle({"operation": "load_markdown", "args": [saved["value"]], "kwargs": {}})
        self.assertLess(len(json.dumps(loaded).encode()), 4 * 1024 * 1024)
        self.assertEqual(library_gateway._unpack(loaded)["value"], text)
        with patch.object(library_gateway, "_WIRE_LIMIT", 10), self.assertRaises(ValueError):
            library_gateway._unpack(packed)

    def test_history_preserves_external_edit_and_restore_checks_current_version(self):
        eid = library.save_document("最初正文", "文稿")
        library.markdown_path(eid).write_text("外部编辑")
        current = library.document_snapshot(eid)
        version = library.list_versions(eid)[0]
        library.save_markdown(eid, "后来编辑", expected_hash=current["content_hash"])
        with self.assertRaises(library.RevisionConflict):
            library.restore_version(eid, version["name"], current["content_hash"])
        library.restore_version(eid, version["name"], library.document_snapshot(eid)["content_hash"])
        self.assertEqual(library.load_markdown(eid), "最初正文")
        self.assertEqual(len(library.list_versions(eid)), 3)


    def test_cli_main_uses_verified_runtime_and_rejects_unknown_options(self):
        from io import StringIO
        from src.agent_cli.main import main
        from src.agent_cli.local_transcription import LocalTranscriptionCliRuntime
        from src.core.transcription_job_application import LocalTranscriptionJobApplication
        from src.core.auth_guard import UnifiedLicenseAuthGuard
        from tests.test_cli_local_transcription_jobs import _FreshCachedLicense
        application = LocalTranscriptionJobApplication(runtime_verification={
            "profile": "trusted_test", "verified": True, "signature_kind": "developer_id",
            "installation_id": "installation-001"}, authorization_guard=UnifiedLicenseAuthGuard(_FreshCachedLicense()),
            model_readiness_probe=lambda _: True)
        runtime = LocalTranscriptionCliRuntime(application)
        library.save_document("正文", "文稿")
        output = StringIO()
        self.assertEqual(main(["library", "list", "--output", "json"], stdout=output, stderr=StringIO(), local_transcription_runtime=runtime), 0)
        self.assertEqual(json.loads(output.getvalue())["result"]["total"], 1)
        self.assertTrue({"LIB-01", "LIB-02", "COPY-07"} <= runtime.activated_capability_ids)
        output = StringIO()
        self.assertNotEqual(main(["library", "list", "--secret", "value", "--output", "json"], stdout=output, stderr=StringIO(), local_transcription_runtime=runtime), 0)

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest

from src.core import pipeline
from src.core.audio.extractor import _stream_download
from src.core.collect.media import ManagedMediaValidation
from src.core.collect.preferences import MaterialPreferenceSnapshot
from src.core.models import Transcript
from src.core.task import TaskExecutionPolicy


class TranscriptionInputRecoveryTests(unittest.TestCase):
    def test_empty_media_is_rejected_before_decoder_or_output_creation(self):
        from src.core.audio.converter import InputMediaError, to_wav_16k_mono
        with TemporaryDirectory() as tmp:
            source = Path(tmp)/'empty.wav'
            source.write_bytes(b'')
            target = Path(tmp)/'prepared'/'output.wav'
            with patch('src.core.audio.converter._ffmpeg_exe') as decoder:
                with self.assertRaises(InputMediaError) as failure:
                    to_wav_16k_mono(source, target)
            self.assertEqual(failure.exception.reason_code, 'LOCAL_MEDIA_EMPTY')
            decoder.assert_not_called()
            self.assertFalse(target.parent.exists())
            self.assertEqual(source.read_bytes(), b'')

    def test_invalid_media_reason_survives_service_and_cli_without_decoder_log(self):
        from io import StringIO
        import json
        from src.agent_cli.main import _local_business_error
        from src.core.audio.converter import InputMediaError, to_wav_mono
        from src.core.auth_guard import OpenProductAccessGuard
        from src.core.transcription_application import LocalFileTranscriptionApplicationService, LocalFileTranscriptionError
        from src.core.transcription_contracts import LocalFileTranscriptionRequestV1
        for detail,reason in (
                ('private-source-path: Invalid data found when processing input','LOCAL_MEDIA_UNREADABLE'),
                ('private-source-path: Output file does not contain any stream','LOCAL_MEDIA_NO_AUDIO')):
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                source=Path(tmp)/'media.mp4';source.write_bytes(b'unchanged source')
                with patch('src.core.audio.converter.subprocess.run',return_value=Mock(returncode=1,stderr=detail)):
                    with self.assertRaises(InputMediaError) as caught:
                        to_wav_mono(source,Path(tmp)/'prepared.wav')
                self.assertEqual(caught.exception.reason_code,reason)
                self.assertNotIn('private-source-path',str(caught.exception))
                service=LocalFileTranscriptionApplicationService(OpenProductAccessGuard(),
                    model_readiness_probe=lambda _:True,pipeline_runner=Mock(side_effect=caught.exception))
                with self.assertRaises(LocalFileTranscriptionError) as failure:
                    service.transcribe(LocalFileTranscriptionRequestV1(str(source)))
                output=StringIO()
                code=_local_business_error(failure.exception,command='transcribe.run',stdout=output,stderr=StringIO())
                error=json.loads(output.getvalue())['error']
                self.assertEqual(code,2);self.assertEqual(error['details']['reason_code'],reason)
                self.assertIn('原文件保留',error['message'])
                self.assertNotIn('后续正式 CLI',error['human_action'])
                self.assertEqual(source.read_bytes(),b'unchanged source')




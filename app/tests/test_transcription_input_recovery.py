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
    def test_expired_collection_url_refreshes_once_and_offline_never_uses_it(self):
        policy = TaskExecutionPolicy.transcription_only(origin='homepage_batch_transcribe',
            stable_key='douyin:100', snapshot=MaterialPreferenceSnapshot('audio',False,False))
        for online in (True, False):
            with self.subTest(online=online), TemporaryDirectory() as tmp, ExitStack() as stack:
                media = Path(tmp)/'media.wav'; media.write_bytes(b'isolated')
                stack.enter_context(patch.object(pipeline,'resolve_managed_transcription_media',return_value=ManagedMediaValidation(False)))
                collected = stack.enter_context(patch('src.core.collect.transcription_input.download_collected_input',side_effect=OSError('expired')))
                resolve = stack.enter_context(patch.object(pipeline,'resolve_source', return_value=media))
                stack.enter_context(patch.object(pipeline,'to_wav_16k_mono',return_value=media))
                engine = Mock(); engine.transcribe.return_value=Transcript(text='isolated model output')
                stack.enter_context(patch.object(pipeline,'get_engine',return_value=engine))
                pipeline.transcribe_source('https://www.douyin.com/video/100',execution_policy=policy,enable_link_download=online)
                self.assertEqual(collected.call_count,int(online))
                resolve.assert_called_once()
                self.assertEqual(resolve.call_args.args[2],online)
                self.assertEqual(engine.transcribe.call_count,1)

    def test_cancelling_collection_download_never_refreshes_or_runs_asr(self):
        policy = TaskExecutionPolicy.transcription_only(origin='homepage_batch_transcribe',
            stable_key='douyin:100',snapshot=MaterialPreferenceSnapshot('audio',False,False))
        cancelled=[False]
        def download(_key,_tmp,progress,_meta):
            cancelled[0]=True
            progress('cancel checkpoint',0)
        with patch.object(pipeline,'resolve_managed_transcription_media',return_value=ManagedMediaValidation(False)), \
             patch('src.core.collect.transcription_input.download_collected_input',side_effect=download), \
             patch.object(pipeline,'resolve_source') as resolve, patch.object(pipeline,'get_engine') as engine:
            with self.assertRaises(InterruptedError):
                pipeline.transcribe_source('https://www.douyin.com/video/100',execution_policy=policy,is_cancelled=lambda:cancelled[0])
            resolve.assert_not_called()
            engine.assert_not_called()

    def test_cancel_before_download_does_not_open_a_socket_or_retry(self):
        def progress(*_):
            raise InterruptedError('cancelled')
        with patch('urllib.request.urlopen') as request:
            with self.assertRaises(InterruptedError):
                _stream_download('https://example.invalid/video.mp4',Path('/unused'),progress)
            request.assert_not_called()

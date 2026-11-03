"""Temporary media and ledger only; no user files, network or secret storage."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from src.core.live_recording.contracts import LiveError
from src.core.live_recording.media import bundled_ffmpeg, export_recording, fingerprint, probe
from src.core.live_recording.service import uid
from tests.test_live_recording import ServiceTests, until


class FullMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ffmpeg = bundled_ffmpeg()
        self.recording = dict(directory=str(self.root), ended=time.time())
        self.segments = [self.make_part(0, 'red', 440), self.make_part(1, 'blue', 880)]

    def make_part(self, number, color, frequency, size='160x90'):
        path = self.root / f'part-{number:06d}.ts'
        subprocess.run([self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', f'color=c={color}:size={size}:rate=10', '-f', 'lavfi',
            '-i', f'sine=frequency={frequency}', '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-g', '10', '-c:a', 'aac', '-f', 'mpegts', str(path)], check=True, timeout=15)
        return dict(path=str(path), closed=1, valid=1, **probe(path, self.ffmpeg))

    def export(self, **kwargs):
        return export_recording(self.segments, self.recording, self.ffmpeg,
                                target=self.root / 'full.mp4', **kwargs)

    def test_real_merge_duration_audio_order_and_no_overwrite(self):
        before = [fingerprint(s['path']) for s in self.segments]
        progress = []
        result = self.export(progress=lambda phase, value: progress.append(value))
        output = self.root / 'full.mp4'
        info = probe(output, self.ffmpeg)
        self.assertTrue(info['audio'])
        self.assertAlmostEqual(info['duration'], sum(s['duration'] for s in self.segments), delta=.3)
        # Read one pixel from each half. This verifies order in the actual MP4.
        pixels = []
        for offset in (.5, 2.5):
            run = subprocess.run([self.ffmpeg, '-v', 'error', '-ss', str(offset), '-i', str(output),
                '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                check=True, capture_output=True, timeout=15)
            pixels.append(run.stdout)
        self.assertGreater(pixels[0][0], pixels[0][2])
        self.assertGreater(pixels[1][2], pixels[1][0])
        self.assertEqual(result['sha256'], fingerprint(output))
        with self.assertRaises(LiveError):
            self.export()
        self.assertEqual([fingerprint(s['path']) for s in self.segments], before)
        self.assertEqual(progress[-1], 100)
        self.assertFalse(list(self.root.glob('.*partial*')))

    def test_incompatible_picture_is_explicitly_rejected(self):
        self.segments.append(self.make_part(2, 'green', 660, size='320x180'))
        with self.assertRaisesRegex(LiveError, '格式变化'):
            self.export()
        self.assertFalse((self.root / 'full.mp4').exists())

    def test_bad_or_missing_segment_never_silently_skipped(self):
        self.segments[1]['valid'] = 0
        with self.assertRaises(LiveError):
            self.export()
        self.segments[1]['valid'] = 1
        Path(self.segments[1]['path']).unlink()
        with self.assertRaises(LiveError):
            self.export()

    def test_cancel_during_merge_keeps_sources_and_cleans_partial(self):
        cancel = threading.Event()
        def progress(phase, _):
            if phase == 'merging':
                cancel.set()
        with self.assertRaises(LiveError):
            self.export(cancelled=cancel.is_set, progress=progress)
        self.assertTrue(all(Path(s['path']).exists() for s in self.segments))
        self.assertFalse((self.root / 'full.mp4').exists())
        self.assertFalse(list(self.root.glob('.*partial*')))
        self.assertFalse(list(self.root.glob('.lulu-concat*')))

    def test_source_change_after_merge_prevents_publication(self):
        changed = False
        def progress(phase, _):
            nonlocal changed
            if phase == 'verifying' and not changed:
                changed = True
                with Path(self.segments[1]['path']).open('ab') as stream:
                    stream.write(b'changed')
        with self.assertRaisesRegex(LiveError, '已变化'):
            self.export(progress=progress)
        self.assertFalse((self.root / 'full.mp4').exists())


class FullServiceTests(ServiceTests):
    def fixture_recording(self):
        from tests.test_live_recording_delete import register_recording
        media = FullMediaTests()
        media.setUp()
        self.addCleanup(media.doCleanups)
        rid, folder, _ = register_recording(self.service, media.root)
        self.service.index.write('DELETE FROM segments WHERE recording_id=?', (rid,))
        self.service.index.write('UPDATE recordings SET gaps=? WHERE id=?', ('[[1,2]]', rid))
        for index, segment in enumerate(media.segments):
            target = folder / Path(segment['path']).name
            Path(segment['path']).replace(target)
            segment['path'] = str(target)
            self.service.index.write('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)',
                (uid('segment'), rid, segment['path'], index+1, segment['duration'], segment['bytes'], 1, 1, segment['sha256']))
        return media, rid

    def test_service_all_parts_receipt_gap_warning_and_status(self):
        media, rid = self.fixture_recording()
        reply = self.call('export_recording', recording_id=rid, path=str(media.root/'full.mp4'))
        result = until(lambda: (r if (r := self.call('export_status', export_id=reply['export_id']))['state'] != 'running' else None))
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(result['result']['segment_count'], 2)
        self.assertIn('断流', result['message'])
        self.assertEqual(result['progress'], 100)
        self.assertEqual(self.call('export_cancel', export_id=reply['export_id'])['state'], 'succeeded')

    def test_unregistered_raw_part_blocks_whole_export(self):
        media, rid = self.fixture_recording()
        Path(self.service._recording(rid)['directory'], 'part-999999.ts').write_bytes(b'unknown')
        reply = self.call('export_recording', recording_id=rid, path=str(media.root/'full.mp4'))
        result = until(lambda: (r if (r := self.call('export_status', export_id=reply['export_id']))['state'] != 'running' else None))
        self.assertEqual(result['state'], 'failed')
        self.assertIn('清单不一致', result['message'])

    def test_active_recording_rejected(self):
        media, rid = self.fixture_recording()
        self.service.index.write('UPDATE recordings SET ended=NULL WHERE id=?', (rid,))
        with self.assertRaisesRegex(LiveError, '还在录制'):
            self.call('export_recording', recording_id=rid, path=str(media.root/'full.mp4'))

    def test_forged_recording_folder_is_rejected_before_media_read(self):
        media, rid = self.fixture_recording()
        self.service.index.write('UPDATE recordings SET directory=? WHERE id=?', (str(media.root), rid))
        with self.assertRaisesRegex(LiveError, '登记不一致'):
            self.call('export_recording', recording_id=rid, path=str(media.root/'full.mp4'))

    def test_cli_borrows_real_session_without_renewing_or_replacing_it(self):
        old_session, old_deadline = self.service.session, self.service.deadline
        self.service.handle_cli('snapshot', {})
        hid = self.service.handle_cli('add', {'source':'https://live.douyin.com/123'})['host_id']
        until(lambda: not self.service.checks)
        self.service.handle_cli('start', {'host_id':hid})
        self.assertEqual(self.service.session, old_session)
        self.assertEqual(self.service.deadline, old_deadline)
        self.call('stop')
        with self.assertRaisesRegex(LiveError, '先打开 Lulu'):
            self.service.handle_cli('snapshot', {})
        self.assertTrue(self.service.closing)

    def test_completed_export_receipt_survives_new_service_without_reexport(self):
        import sqlite3
        from src.core.live_recording.service import LiveService
        media, rid = self.fixture_recording()
        output = media.root/'persisted.mp4'
        eid = self.call('export_recording', recording_id=rid, path=str(output))['export_id']
        until(lambda: json.loads(self.service.index.setting('export:' + eid))['state'] == 'succeeded')
        original = fingerprint(output)
        restored_root = self.root/'restored'
        (restored_root/'live-recording').mkdir(parents=True)
        with sqlite3.connect(restored_root/'live-recording/index.sqlite3') as db, self.service.index.lock:
            self.service.index.db.backup(db)
        restored = LiveService(restored_root, self.control, 'isolated', provider=self.provider,
            ffmpeg=self.service.ffmpeg, secret_read=lambda: self.fail('unexpected credential read'))
        try:
            session = restored.handle('open', dict(nonce='b'*32))['session']
            receipt = restored.handle('export_status', dict(session=session, export_id=eid))
            self.assertEqual(receipt['state'], 'succeeded')
            self.assertEqual(receipt['result']['sha256'], original)
            self.assertEqual(fingerprint(output), original)
        finally:
            restored.shutdown()

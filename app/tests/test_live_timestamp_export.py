"""Real FFmpeg timestamp jumps, private synthetic media, no network."""
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.core.live_recording.contracts import LiveError, recording_message
from src.core.live_recording.media import export_recording, fingerprint, probe
from tests.test_live_full_export import FullMediaTests


class TimestampExportTests(unittest.TestCase):
    def setUp(self):
        self.media = FullMediaTests()
        self.media.setUp()
        self.addCleanup(self.media.doCleanups)
        self.root, self.ffmpeg = self.media.root, self.media.ffmpeg
        path = self.root / 'part-000002.ts'
        subprocess.run([self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=10:duration=4',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
            '-vf', r'setpts=PTS+12/TB*gte(T\,2)', '-af', r'asetpts=PTS+12/TB*gte(T\,2)',
            '-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-g', '10', '-c:a', 'aac', '-f', 'mpegts', str(path)], check=True, timeout=15)
        self.jump = dict(path=str(path), closed=1, valid=1, **probe(path, self.ffmpeg))

    def test_probe_identifies_source_timestamp_jump_without_inventing_lost_seconds(self):
        self.assertAlmostEqual(self.jump['duration'], 4, delta=.1)
        self.assertTrue(self.jump.get('timestamp_discontinuity'))
        self.assertFalse(self.media.segments[0].get('timestamp_discontinuity'))

    def test_whole_export_keeps_all_frames_and_audio_when_one_part_has_timestamp_jump(self):
        segments = [self.media.segments[0], self.jump, self.media.segments[1]]
        before = [fingerprint(s['path']) for s in segments]
        output = self.root / 'whole-with-jump.mp4'
        result = export_recording(segments, self.media.recording, self.ffmpeg, target=output)
        self.assertAlmostEqual(result['duration'], sum(s['duration'] for s in segments), delta=.3)
        decoded = subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-xerror',
            '-i', str(output), '-map', '0:v', '-fps_mode', 'passthrough', '-f', 'framemd5', '-'],
            check=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(len([line for line in decoded.stdout.splitlines() if not line.startswith('#')]), 80)
        self.assertTrue(probe(output, self.ffmpeg)['audio'])
        self.assertTrue(result['timestamp_warning'])
        self.assertEqual([fingerprint(s['path']) for s in segments], before)
        self.assertFalse(list(self.root.glob('.lulu-normalized-*')))

    def test_cancel_during_timestamp_normalization_removes_only_new_partial_files(self):
        cancel = threading.Event()
        def register(process):
            if '.lulu-normalized-' in str(process.process.args[-1]):
                cancel.set()
        with self.assertRaisesRegex(LiveError, '取消'):
            export_recording([self.jump], self.media.recording, self.ffmpeg,
                target=self.root/'cancelled.mp4', register=register, cancelled=cancel.is_set)
        self.assertEqual(fingerprint(self.jump['path']), self.jump['sha256'])
        self.assertFalse((self.root/'cancelled.mp4').exists())
        self.assertFalse(list(self.root.glob('.lulu-normalized-*')))

    def test_short_disk_space_preserves_sources_and_does_not_start_normalization(self):
        with patch('src.core.live_recording.media.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(LiveError, '空间不足'):
                export_recording([self.jump], self.media.recording, self.ffmpeg, target=self.root/'full.mp4')
        self.assertEqual(fingerprint(self.jump['path']), self.jump['sha256'])
        self.assertFalse((self.root/'full.mp4').exists())
        self.assertFalse(list(self.root.glob('.lulu-normalized-*')))

    def test_completed_media_persists_timestamp_warning_across_index_reopen(self):
        from tests.test_live_recording import ServiceTests
        from src.core.live_recording.index import LiveIndex
        from pathlib import Path
        fixture = ServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.service
        service.index.write('INSERT INTO recordings(id,host_id,name,job_id,directory,started,ended) VALUES(?,?,?,?,?,?,?)',
            ('timestamp-fixture', 'host-fixture', '合成时钟样本', 'job-fixture', str(self.root), 1, 20))
        monitor = dict(job_id='job-fixture', recording_id='timestamp-fixture', directory=str(self.root))
        producer = SimpleNamespace(completed=lambda: [(Path(self.jump['path']), 1)])
        with patch.object(service.control, 'verify_worker_lease'):
            service._save_segments(monitor, producer, 'fixture-lease')
        restored = LiveIndex(service.index.root)
        try:
            self.assertEqual(restored.one('SELECT timestamp_warning FROM recordings')['timestamp_warning'], 1)
            self.assertAlmostEqual(restored.one('SELECT duration FROM segments')['duration'], 4, delta=.1)
        finally:
            restored.close()

    def test_saved_recording_labels_timestamp_warning_separately_from_known_gaps(self):
        self.assertEqual(recording_message(dict(ended=1, reason='paused', gaps='[]',
            timestamp_warning=1)), '已保存，来源时间有跳跃')
        self.assertEqual(recording_message(dict(ended=1, reason='paused', gaps='[[1,2]]',
            timestamp_warning=1)), '录制有缺口')

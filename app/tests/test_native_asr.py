"""Native contracts and resource ownership, with isolated roots and fake inference."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import wave
import unittest
from unittest.mock import patch

from src.core.engines import asr_resource as resource
from src.core.engines.local_native import parse_moss, IncompleteTranscription, NativeASREngine
from src.core.engines import native_process


class NativeASRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.patch = patch('src.core.models_manager.app_data_dir', return_value=self.root)
        self.patch.start()

    def tearDown(self):
        resource.finish_batch('batch')
        self.patch.stop()
        self.tmp.cleanup()

    def row(self):
        return dict(file='/audio.wav', text='你好。 再见。',
            segments=[dict(text='你好。',t0_ms=0,t1_ms=1200,speaker_id=1),
                      dict(text='再见。',t0_ms=1000,t1_ms=2000,speaker_id=2)],
            raw_text='[0.00][S01]你好。[1.20][1.00][S02]再见。[2.00]')

    def parse(self, row):
        return parse_moss(json.dumps(row, ensure_ascii=False), '/audio.wav', 3)

    def test_overlap_and_unknown_speaker_are_preserved(self):
        row = self.row()
        del row['segments'][1]['speaker_id']
        value = self.parse(row)
        self.assertEqual(value.segments[1].start, 1)
        self.assertIsNone(value.segments[1].speaker_id)

    def test_exit_zero_error_keeps_partial(self):
        row = self.row(); row['error'] = 'repetition'
        with self.assertRaises(IncompleteTranscription) as error: self.parse(row)
        self.assertEqual(error.exception.partial_text, row['text'])

    def test_no_fabricated_times_or_lost_text(self):
        for change in ('raw', 'zero', 'missing', 'lost', 'file', 'reverse'):
            row = self.row()
            if change == 'raw': row['raw_text'] = '[S01]你好。[1.20][S02]再见。'
            if change == 'zero': row['segments'][0]['t1_ms'] = 0
            if change == 'missing': del row['segments'][0]['t0_ms']
            if change == 'lost': row['text'] += '遗漏'
            if change == 'file': row['file'] = '/wrong.wav'
            if change == 'reverse': row['segments'][1]['t1_ms'] = 500
            with self.subTest(change=change), self.assertRaises(IncompleteTranscription): self.parse(row)

    def test_duplicate_and_malformed_rows_rejected(self):
        row = json.dumps(self.row())
        for output in (row+'\n'+row, '{}\n'+row, '[]', 'bad',
                       '{"type":"batch_header","load_ms":1}\n'*2+row):
            with self.assertRaises(IncompleteTranscription): parse_moss(output, '/audio.wav', 3)

    def test_unmatched_raw_prefix_tail_and_gap_are_preserved_not_accepted(self):
        for kind in ('prefix', 'tail', 'gap'):
            row = self.row()
            if kind == 'prefix': row['raw_text'] = '遗漏开头' + row['raw_text']
            if kind == 'tail': row['raw_text'] += '[2.00][S03]遗漏尾段'
            if kind == 'gap': row['raw_text'] = row['raw_text'].replace('[1.20]', '[1.20]遗漏中间')
            with self.subTest(kind=kind), self.assertRaises(IncompleteTranscription) as error:
                self.parse(row)
            self.assertEqual(error.exception.raw_text, row['raw_text'])
        row = self.row()
        row['raw_text'] = ' \n' + row['raw_text'].replace('[1.20]', '[1.20] \n') + '\n'
        self.assertEqual(self.parse(row).text, row['text'])

    def test_raw_residual_survives_native_recovery_and_process_failure(self):
        path = self.root/'input.wav'
        with wave.open(str(path), 'wb') as out:
            out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            out.writeframes(b'\1\0' * 48000)
        row = self.row()
        row.update(file=str(path), raw_text=row['raw_text'] + '[2.00][S03]遗漏尾段')
        raw = json.dumps(row, ensure_ascii=False)
        for failed in (False, True):
            with self.subTest(process_failed=failed), \
                 patch('src.core.models_manager.is_downloaded', return_value=True), \
                 patch('src.core.models_manager.model_path', return_value=self.root), \
                 patch('src.core.engines.local_native.moss_preflight'), \
                 patch('src.core.engines.local_native.run_native',
                       side_effect=native_process.NativeProcessError('计算停止', raw) if failed else None,
                       return_value=(raw, 'fixture')):
                with self.assertRaisesRegex(IncompleteTranscription, '已保存在'):
                    NativeASREngine('local_moss_q5').transcribe(path)
        files = list((self.root/'incomplete-transcriptions').glob('*.txt'))
        self.assertEqual(len(files), 2)
        for file in files:
            self.assertIn(row['text'], file.read_text())
            self.assertIn(row['raw_text'], file.read_text())
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_retained_batch_blocks_maintenance_and_other_threads(self):
        with resource.batch_context('batch'):
            with resource.model_use('whisper', retain=True): pass
        with self.assertRaises(resource.ASRResourceBusy):
            with resource.maintenance(): pass
        with self.assertRaises(resource.ASRResourceBusy):
            with resource.model_use('moss'): pass
        with resource.batch_context('batch'):
            with resource.model_use('whisper', retain=True): pass
        self.assertTrue(resource.finish_batch('batch'))
        with resource.maintenance():
            with resource.maintenance(): pass

    def test_resource_excludes_another_process(self):
        code = '''from pathlib import Path
from src.core.engines.asr_resource import model_use, ASRResourceBusy
import sys
try:
 with model_use('other', root=Path(sys.argv[1])): pass
except ASRResourceBusy: sys.exit(7)
'''
        with resource.model_use('first'):
            result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr)
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_task_releases_resource(self):
        with resource.batch_context('batch'):
            with self.assertRaises(ValueError):
                with resource.model_use('whisper', retain=True): raise ValueError()
        with resource.maintenance(): pass

    def test_partial_is_private_and_never_marked_success(self):
        engine = NativeASREngine('local_moss_q5')
        with patch.object(engine, '_transcribe', side_effect=IncompleteTranscription('failed','部分文字')):
            with self.assertRaisesRegex(IncompleteTranscription, '已保存在'): engine.transcribe('/audio.wav')
        files = list((self.root/'incomplete-transcriptions').glob('*.txt'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertIn('部分文字',files[0].read_text())

    def test_native_cancel_reaps_own_process(self):
        runner = Path(__file__).resolve().parents[1]/'build/asr-runtime/asr-runner'
        if not runner.exists(): self.skipTest('build native helper before integration check')
        calls = [0]
        def cancelled():
            calls[0] += 1
            return calls[0] > 3
        with patch.object(native_process, 'checked_tools', return_value=(runner, Path(sys.executable), 'fake')):
            with resource.model_use('fake'), self.assertRaises(InterruptedError):
                native_process.run_native('moss', lambda work: ['-c','import time; time.sleep(30)'],
                    is_cancelled=cancelled, progress=lambda *_:None)
        with resource.maintenance(): pass

    def test_packaged_runtime_binds_exact_inventory_and_rejects_extra_tools(self):
        import hashlib,runpy,shutil
        app_root=Path(__file__).resolve().parents[1]
        runtime=app_root/'build/asr-runtime'
        if not runtime.exists(): self.skipTest('build native runtime first')
        gate=runpy.run_path(str(app_root/'packaging/profile_gate.py'))
        bundle=self.root/'Lulu.app';dest=bundle/'Contents/Frameworks/asr-runtime'
        (bundle/'Contents').mkdir(parents=True)
        (bundle/'Contents/Info.plist').write_bytes(b'fixture')
        runpy.run_path(str(app_root/'packaging/build_asr_runtime.py'))['copy_into_bundle'](runtime,bundle)
        manifest=bundle/'Contents/Resources/asr-runtime/manifest.json'
        analysis=dict(resources=[],
                      native_asr_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
        modules=['src.core.engines.local_native']
        approved=gate['_native_asr_executables'](bundle,analysis,modules)
        self.assertEqual(len(approved),8)
        with self.assertRaises(SystemExit):gate['_native_asr_executables'](bundle,{'resources':[]},modules)
        changed=dict(analysis,native_asr_manifest_sha256='0'*64)
        with self.assertRaises(SystemExit):gate['_native_asr_executables'](bundle,changed,modules)
        foreign=dest/'foreign-tool';foreign.write_bytes(b'\xcf\xfa\xed\xfe');foreign.chmod(0o755)
        approved['Contents/MacOS/Lulu']='main_executable'
        records,findings,_=gate['_bundle_executable_inventory'](bundle,approved,'external_candidate',set(analysis['resources']))
        self.assertTrue(any(f['kind']=='unknown_executable_member' and f['value'].endswith('/foreign-tool') for f in findings))
        foreign.unlink()
        (dest/'transcribe-cli').write_bytes(b'tampered')
        with self.assertRaises(SystemExit):gate['_native_asr_executables'](bundle,analysis,modules)

    def test_frozen_native_runtime_keeps_update_execution_permissions(self):
        import runpy,shutil,stat,zipfile
        from src.core.updater.archive import _entry_mode
        app_root=Path(__file__).resolve().parents[1]
        runtime=app_root/'build/asr-runtime'
        if not runtime.exists():self.skipTest('build native runtime first')
        bundle=self.root/'Lulu.app';dest=bundle/'Contents/Frameworks/asr-runtime'
        builder=runpy.run_path(str(app_root/'packaging/build_asr_runtime.py'))
        (bundle/'Contents').mkdir(parents=True)
        (bundle/'Contents/Info.plist').write_bytes(b'fixture')
        builder['copy_into_bundle'](runtime,bundle)
        with patch.object(sys,'frozen',True,create=True),patch.object(sys,'_MEIPASS',str(dest.parent),create=True):
            self.assertEqual(native_process.runtime_root(),dest)
            native_process.checked_tools('moss')
        for name in builder['EXECUTABLES']:
            info=zipfile.ZipInfo('Lulu.app/'+(dest/name).relative_to(bundle).as_posix())
            info.external_attr=(stat.S_IFREG|0o755)<<16
            self.assertEqual(_entry_mode(info),('file',0o755))
        builder['verify_runtime'](dest)
        (dest/'asr-runner').chmod(0o644)
        with self.assertRaisesRegex(RuntimeError,'permissions'):
            builder['verify_runtime'](dest)

    def test_signed_native_bundle_survives_secure_installer_copy(self):
        import plistlib,runpy,shutil
        from src.core.updater.install import _copy_app_to_dirfd
        app_root=Path(__file__).resolve().parents[1]
        runtime=app_root/'build/asr-runtime'
        if not runtime.exists():self.skipTest('build native runtime first')
        bundle=self.root/'Lulu.app'
        (bundle/'Contents/MacOS').mkdir(parents=True)
        (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(
            CFBundleIdentifier='com.lulu.asr-copy-test',CFBundleExecutable='Lulu',
            CFBundlePackageType='APPL',CFBundleVersion='1')))
        shutil.copy2(runtime/'asr-runner',bundle/'Contents/MacOS/Lulu')
        builder=runpy.run_path(str(app_root/'packaging/build_asr_runtime.py'))
        builder['copy_into_bundle'](runtime,bundle)
        subprocess.run(['/usr/bin/codesign','--force','--deep','--sign','-',str(bundle)],check=True,capture_output=True)
        copied=self.root/'copied';copied.mkdir()
        fd=os.open(copied,os.O_RDONLY|os.O_DIRECTORY)
        try:_copy_app_to_dirfd(bundle,fd,'Lulu.app')
        finally:os.close(fd)
        result=subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(copied/'Lulu.app')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        builder['verify_runtime'](copied/'Lulu.app/Contents/Frameworks/asr-runtime')

    def test_native_failure_preserves_matching_partial_but_never_succeeds(self):
        path = self.root/'input.wav'
        with wave.open(str(path),'wb') as out:
            out.setparams((1,2,16000,0,'NONE','not compressed'));out.writeframes(b'\1\0'*48000)
        row=self.row();row['file']=str(path)
        failure=native_process.NativeProcessError('计算停止',json.dumps(row))
        with patch('src.core.models_manager.is_downloaded',return_value=True), \
             patch('src.core.models_manager.model_path',return_value=self.root), \
             patch('src.core.engines.local_native.moss_preflight'), \
             patch('src.core.engines.local_native.run_native',side_effect=failure):
            with self.assertRaisesRegex(IncompleteTranscription,'已保存在'):
                NativeASREngine('local_moss_q5').transcribe(path)
        files=list((self.root/'incomplete-transcriptions').glob('*.txt'))
        self.assertEqual(len(files),1)
        self.assertIn(row['text'],files[0].read_text())
        from src.agent_cli.contracts import write_cli_error
        from io import StringIO
        output=StringIO()
        self.assertEqual(write_cli_error(code='INCOMPLETE_RESULT',command='transcribe.run',details=None,stdout=output,stderr=StringIO()),71)
        self.assertEqual(json.loads(output.getvalue())['error']['code'],'INCOMPLETE_RESULT')

    def test_zero_silence_rejected_but_quiet_signal_kept(self):
        from src.core.audio.converter import reject_zero_pcm
        path = self.root/'signal.wav'
        for samples, rejected in ((b'\0'*32000, True), (b'\0'*32000+b'\1\0', False)):
            with wave.open(str(path),'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed')); out.writeframes(samples)
            if rejected:
                with self.assertRaisesRegex(ValueError, '完全静音'): reject_zero_pcm(path)
            else: reject_zero_pcm(path)
        with self.assertRaises(InterruptedError): reject_zero_pcm(path,is_cancelled=lambda:True)

    def test_moss_preflight_blocks_context_overflow_and_memory_pressure(self):
        from src.core.engines.local_native import moss_preflight
        from types import SimpleNamespace
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='1')) as query:
            moss_preflight(1200)
            with self.assertRaisesRegex(ValueError, '整段处理范围'): moss_preflight(11000)
            self.assertEqual(query.call_count,1)
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='4')):
            with self.assertRaisesRegex(RuntimeError,'内存压力'): moss_preflight(1200)

    def test_parent_death_reaps_native_child_and_releases_lock(self):
        runner = Path(__file__).resolve().parents[1]/'build/asr-runtime/asr-runner'
        if not runner.exists(): self.skipTest('build native helper before integration check')
        pidfile = self.root/'child.pid'
        code = """import sys,os
from pathlib import Path
from unittest.mock import patch
from src.core.engines import native_process,asr_resource
root,runner,pidfile=map(Path,sys.argv[1:])
with asr_resource.model_use('fake',root=root), patch.object(native_process,'checked_tools',return_value=(runner,Path(sys.executable),'fake')):
 native_process.run_native('moss',lambda work:['-c','import os,time; from pathlib import Path; Path('+repr(str(pidfile))+').write_text(str(os.getpid())); time.sleep(30)'],is_cancelled=lambda:False,progress=lambda *_:None)
"""
        proc = subprocess.Popen([sys.executable,'-B','-c',code,str(self.root),str(runner),str(pidfile)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+8
            while not pidfile.exists() and proc.poll() is None and time.monotonic()<deadline: time.sleep(.05)
            self.assertTrue(pidfile.exists())
            child=int(pidfile.read_text())
            with self.assertRaises(resource.ASRResourceBusy):
                with resource.model_use('other'): pass
            proc.kill(); proc.communicate(timeout=3)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try: os.kill(child,0)
                except ProcessLookupError: break
                time.sleep(.05)
            else: self.fail('Native child survived its owner')
            with resource.maintenance(): pass
        finally:
            if proc.poll() is None: proc.kill()
            proc.communicate(timeout=3)


if __name__ == '__main__': unittest.main()

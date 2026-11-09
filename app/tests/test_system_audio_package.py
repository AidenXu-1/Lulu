"""Current external package native capture admission; no historical license fixtures."""
from pathlib import Path
import runpy,tempfile,unittest


def write(path,content,mode):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(content);path.chmod(mode)


class SystemAudioPackageTests(unittest.TestCase):
    def test_only_exact_native_system_audio_component_is_allowed(self):
        profile=runpy.run_path(str(Path(__file__).resolve().parents[1]/'packaging/profile_gate.py'))
        scan=profile['_bundle_executable_inventory']
        with tempfile.TemporaryDirectory() as tmp:
            app=Path(tmp)/'Lulu.app'
            write(app/'Contents/MacOS/Lulu',b'\xcf\xfa\xed\xfemain',0o700)
            paths=['Contents/Frameworks/recording_runtime/SystemAudioCapture',
                   'Contents/Frameworks/recording_runtime/OtherCapture',
                   'Contents/Frameworks/other/SystemAudioCapture']
            for path in paths:write(app/path,b'\xcf\xfa\xed\xfefixture',0o700)
            records,findings,_=scan(app,approved_executables={'Contents/MacOS/Lulu':'main_executable'},
                                   profile='external_candidate',declared_resources=set())
            allowed={r['path']:r['allowed'] for r in records}
            self.assertTrue(allowed[paths[0]])
            self.assertEqual({f['value'] for f in findings},set(paths[1:]))
            write(app/paths[0],b'#!/bin/sh\nexit 0\n',0o700)
            _,findings,_=scan(app,approved_executables={'Contents/MacOS/Lulu':'main_executable'},
                            profile='external_candidate',declared_resources=set())
            self.assertIn(paths[0],{f['value'] for f in findings})


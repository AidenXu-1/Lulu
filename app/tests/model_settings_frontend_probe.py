"""One isolated Chromium process can serve several capability-gate assertions."""
import json,os,subprocess,sys,tempfile
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def run_probe():
    script=Path(__file__).with_name('model_settings_frontend_child.py')
    with tempfile.TemporaryDirectory(prefix='lulu-settings-frontend-') as home:
        result=subprocess.run([sys.executable,'-B',str(script)],capture_output=True,text=True,timeout=40,env=dict(os.environ,HOME=home,XDG_CACHE_HOME=home+'/cache',XDG_CONFIG_HOME=home+'/config',XDG_DATA_HOME=home+'/data',QT_QPA_PLATFORM='offscreen',QTWEBENGINE_DISABLE_SANDBOX='1'))
    if result.returncode:
        raise AssertionError(result.stderr[-6000:])
    return json.loads(result.stdout.strip().splitlines()[-1])

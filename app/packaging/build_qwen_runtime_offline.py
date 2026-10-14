"""Package the already-tested PBS + venv without resolving or downloading dependencies."""
import argparse, hashlib, json, os, shutil, subprocess, tarfile
from importlib.metadata import distributions
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--python-root',type=Path,required=True);p.add_argument('--venv',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
stage=args.output/'staging'
if stage.exists():raise SystemExit('Staging already exists; choose a fresh output directory')
stage.mkdir();target=stage/'python';shutil.copytree(args.python_root.resolve(),target,symlinks=True)
site=target/'lib/python3.11/site-packages'
shutil.copytree(args.venv/'lib/python3.11/site-packages',site,dirs_exist_ok=True,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
# Keep future offline rebuilds on the package versions verified in both candidates.
# This is a version constraint, not a promise of byte-identical wheel/PBS inputs.
constraints = Path(__file__).parent/'tts_runtimes/local_qwen3_tts.mac-arm64.constraints.txt'
actual = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in distributions(path=[str(site)])}
for line in constraints.read_text().splitlines():
    if not line or line.startswith('#'): continue
    name, expected = line.split('==', 1)
    if actual.get(name.lower().replace('_', '-')) != expected:
        raise RuntimeError('Unverified Qwen build dependency version: ' + name)
# The interpreter must be the real bundled PBS file, not a link to the dev environment.
assert (target/'bin/python3.11').is_file() and not (target/'bin/python3.11').is_symlink()
for f in target.rglob('*'):
    if f.is_symlink() and not f.resolve().is_relative_to(target.resolve()):raise RuntimeError('External runtime symlink: '+str(f))
subprocess.run([str(target/'bin/python3.11'),'-I','-s',str(Path(__file__).parent/'tts_runtimes/local_qwen3_tts.selftest.py')],check=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','HF_HUB_OFFLINE':'1'})
name='LuluTTSRuntime-local_qwen3_tts-mac-arm64-20260917.1.tar.gz';archive=args.output/name
with tarfile.open(archive,'w:gz') as tar:tar.add(target,arcname='python',filter=lambda i: None if '__pycache__' in i.name or i.name.endswith('.pyc') else i)
h=hashlib.sha256()
with archive.open('rb') as f:
    for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
manifest={'model_id':'local_qwen3_tts','platform':'mac-arm64','version':'20260917.1','filename':name,'sha256':h.hexdigest(),'size':archive.stat().st_size}
(args.output/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest),flush=True)

"""Subprocess fixture for cross-process deployment tests; all state under argv root."""
import json,sys,time
from pathlib import Path
from unittest.mock import patch
from src.core.synthesis import qwen3_weights as qw,qwen3_deployment as dp,runtime_packages as rp

def main():
    home=Path(sys.argv[1]);name=sys.argv[2];base=home/'qwen/models'
    locks=json.loads((home/'locks.json').read_text());data=json.loads((home/'data.json').read_text())
    def env(*a,**kw):
        (base.parent/'runtime').mkdir(parents=True,exist_ok=True)
        (base.parent/'runtime/python').write_text('environment')
        with (home/'requests.jsonl').open('a') as f:f.write(json.dumps(['runtime'])+'\n')
    def download(repo,revision,cache,cancel,*,allow_patterns):
        time.sleep(.12)
        with (home/'requests.jsonl').open('a') as f:f.write(json.dumps([repo,allow_patterns])+'\n')
        for rel in allow_patterns:
            dest=cache/('models--'+repo.replace('/','--'))/'snapshots'/revision/rel
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(bytes.fromhex(data[repo.split('/')[-1]][rel]))
    with patch.object(qw,'MODELS',locks),patch.object(qw.mm,'app_data_dir',return_value=home/'private'),patch.object(rp,'model_dir',return_value=base.parent),patch.object(rp,'is_runtime_installed',side_effect=lambda _:(base.parent/'runtime/python').exists()),patch.object(rp,'install_runtime',side_effect=env),patch.object(rp,'_snapshot_download_cancellable',side_effect=download):
        dp.request([name]);dp.run()
        assert qw.is_installed(base,name)
if __name__=='__main__':main()

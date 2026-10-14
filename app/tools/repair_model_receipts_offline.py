"""Operator-only offline recovery. Dry-run by default; no downloads or model writes."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.core import artifact_trust as at, models_manager as mm
from src.core.receipt_device_repair import plan_repair, apply_repair
from src.core.synthesis import runtime_packages as rp, local_models as lm, indextts2_weights as iw, qwen3_weights as qw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--artifact', action='append', help='Only these registered artifact IDs')
    parser.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    receipts = mm.app_data_dir()/'.artifact-trust'
    targets = []
    for mid in mm.LOCAL_MODELS:
        root = mm.managed_dir()/mid
        options = mm._trust_options(mid)
        # Existing receipt selects among the App's pinned alternatives only.
        path = at._receipt_path(receipts, 'asr-model:'+mid)
        old = json.loads(path.read_text()) if path.exists() else {}
        trust = next((t for t in options if t == old.get('trust')), options[0])
        targets.append(('asr-model:'+mid, root, trust))
    for mid, spec in rp.TTS_RUNTIMES.items():
        targets.append((rp._receipt_id('tts-runtime', mid), rp.runtime_dir(mid), rp._trusted_manifest(spec, rp.current_platform())))
        if spec.weights_handler == 'indextts2':
            version = rp.index_weights_options(mid).get('version', '2.5')
            targets.append((iw.weight_profile(version).receipt_id, iw.checkpoints_dir(rp.weights_dir(mid)), iw._weights_trust(version)))
        elif spec.weights_handler == 'qwen3':
            targets.extend(('qwen3-weights:'+n, rp.model_dir(mid)/'models'/n, qw._trust(n)) for n in qw.MODELS)
        else:
            targets.append(('tts-weights:'+mid, lm.model_dir(mid), lm._trust(mid)))
    if args.artifact:
        known = {t[0] for t in targets}
        if set(args.artifact) - known:
            raise SystemExit('Unknown artifact')
        targets = [t for t in targets if t[0] in args.artifact]
    plans = []; report = []
    for aid, root, trust in targets:
        path = at._receipt_path(receipts, aid)
        if not root.exists() or not path.exists():
            report.append({'artifact':aid, 'status':'missing_directory' if not root.exists() else 'missing_receipt', 'root':str(root)})
            continue
        try:
            _, _, payload, changed = plan_repair(aid, root, trust, receipts)
            # Qwen ships per-file SHA256 locks: verify actual content as well.
            if changed and aid.startswith('qwen3-weights:'):
                for entry in trust['files']:
                    if not qw._valid(root/entry['path'], entry, lambda: None):
                        raise RuntimeError('固定文件内容校验失败：'+entry['path'])
            plans.append((aid, root, trust, changed))
            report.append({'artifact':aid, 'status':'device_only' if changed else 'valid', 'files':len(payload['files'])})
        except (OSError, ValueError, RuntimeError) as exc:
            report.append({'artifact':aid, 'status':'blocked', 'reason':str(exc)})
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.apply:
        if any(r['status']=='blocked' for r in report):
            raise SystemExit('检查有异常，未写入任何记录。')
        for aid, root, trust, changed in plans:
            if changed:
                apply_repair(aid, root, trust, receipts, args.backup_dir)
                print('RESTORED '+aid, flush=True)
        print(json.dumps({'asr':{mid:mm.is_downloaded(mid) for mid in mm.LOCAL_MODELS},
                          'tts':{mid:rp.status(mid) for mid in rp.TTS_RUNTIMES}},ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

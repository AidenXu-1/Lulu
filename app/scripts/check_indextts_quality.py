"""Explicit, offline developer check. Human listening remains a separate receipt.

Usage: python app/scripts/check_indextts_quality.py --reference REF.wav --output DIR
       --cases conversation,mixed_numbers --repeats 2
No voice-library/config/App mutation, installation or automatic acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--cases', default='')
    p.add_argument('--repeats', default=2, type=int, choices=range(1, 4))
    args = p.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    from src.core.synthesis.base import VoiceProfile
    from src.core.synthesis.segment_project import SegmentProject
    from src.core.synthesis.indextts2_subprocess import IndexTTS2SubprocessEngine
    cases = json.loads((APP / 'tests/fixtures/indextts_quality_cases.json').read_text())
    wanted = args.cases.split(',') if args.cases else [c['id'] for c in cases]
    if set(wanted) - {c['id'] for c in cases}:
        p.error('Unknown case id')
    selected = [c for c in cases if c['id'] in wanted]
    project = SegmentProject.create(args.output, '\n\n'.join(c['text'] for c in selected),
        VoiceProfile('quality-check', '本地验证参考', str(args.reference.resolve())),
        {'emotion_mode': 'follow', 'language': 'ZH', 'speed': 1.0, 'random_emotion': False})
    report = {'project': str(project.path), 'model_version': '2.5', 'generation_settings': 'official defaults',
              'cases': [{**c, 'status': 'not_run', 'listening': 'pending', 'versions': []} for c in cases],
              'scope': 'File checks and flow checks do not accept naturalness. No fixed best seed.'}
    path = project.root / 'quality-report.json'
    SegmentProject._write(path, report)
    print('PROJECT', project.path, flush=True)
    engine = IndexTTS2SubprocessEngine()
    try:
        for repeat in range(args.repeats):
            for case, part in zip(selected, project.read()['parts']):
                print('START', case['id'], repeat + 1, flush=True)
                version = project.generate(part['id'], engine,
                    progress=lambda message, frac: print(message, flush=True) if frac < 0 else None)
                item = next(c for c in report['cases'] if c['id'] == case['id'])
                item['versions'].append(version)
                item['status'] = 'generated'
                SegmentProject._write(path, report)
                print('SAVED', case['id'], version['duration'], flush=True)
        report['export'] = project.export(project.root / 'exports' / 'quality-first-takes.wav')
        report['flow'] = 'complete; first takes stay selected after later candidates'
    except Exception as exc:
        report['flow'] = 'incomplete; completed takes preserved'
        report['error'] = str(exc)
        raise
    finally:
        SegmentProject._write(path, report)
        print('REPORT', path, flush=True)
        from src.core.synthesis.indextts2_subprocess import shutdown_persistent_worker
        shutdown_persistent_worker()


if __name__ == '__main__':
    main()

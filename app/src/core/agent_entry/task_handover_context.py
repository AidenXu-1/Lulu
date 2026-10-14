"""Display/input hints for terminal history; never authority to resume old jobs."""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess

from ..updater.secure_io import read_nofollow


def validate_context(value, records):
    if (type(value) is not dict or not {'tasks', 'view'} <= set(value)
            or set(value) - {'tasks', 'view', 'live_recordings'}):
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    known = {row['job_id']: row for row in records}
    tasks, view = value['tasks'], value['view']
    if (type(tasks) is not dict or not set(tasks).issubset(known)
            or type(view) is not dict or set(view) != {'groups', 'hidden_ids'}
            or type(view['groups']) is not list or type(view['hidden_ids']) is not list
            or len(view['groups']) > 2048 or len(view['hidden_ids']) > 20000):
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    fields = {'title', 'source', 'kind', 'folder_id', 'stable_key', 'execution_policy'}
    live = value.get('live_recordings', {})
    if type(live) is not dict or len(live) > 10000 or not set(live).issubset(known):
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    for jid, policy in live.items():
        if (known[jid]['job_kind'] != 'live_recording'
                or known[jid]['state'] not in {'succeeded', 'partial', 'failed', 'cancelled'}
                or type(policy) is not dict or set(policy) != {'recording_id', 'host_id', 'directory'}
                or type(policy['recording_id']) is not str
                or re.fullmatch(r'recording-[a-f0-9]{32}', policy['recording_id']) is None
                or type(policy['host_id']) is not str
                or re.fullmatch(r'host-[a-f0-9]{32}', policy['host_id']) is None
                or type(policy['directory']) is not str or not 1 <= len(policy['directory']) <= 16384
                or '\0' in policy['directory'] or not Path(policy['directory']).is_absolute()
                or '..' in Path(policy['directory']).parts
                or Path(policy['directory']).name != policy['recording_id']):
            raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    for task in tasks.values():
        if (type(task) is not dict or set(task) != fields
                or type(task['title']) is not str or not 1 <= len(task['title']) <= 500
                or type(task['source']) is not str or not 1 <= len(task['source']) <= 16384
                or task['kind'] not in {'file', 'link'}
                or type(task['stable_key']) is not str or len(task['stable_key']) > 500
                or (task['folder_id'] is not None and type(task['folder_id']) is not str)
                or (task['execution_policy'] is not None and type(task['execution_policy']) is not dict)):
            raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    if any(type(tid) is not str or tid not in known for tid in view['hidden_ids']):
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    groups, members = set(), set()
    for group in view['groups']:
        if (type(group) is not dict or set(group) != {'id','name','folder_id','expanded','members'}
                or type(group['id']) is not int or group['id'] in groups
                or type(group['name']) is not str or len(group['name']) > 500
                or (group['folder_id'] is not None and type(group['folder_id']) is not str)
                or type(group['expanded']) is not bool or type(group['members']) is not list):
            raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
        groups.add(group['id'])
        for member in group['members']:
            if type(member) is not dict:
                raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
            if set(member) == {'task_id'}:
                tid = member['task_id']
                if type(tid) is not str or tid not in known or tid in members:
                    raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
                members.add(tid)
            elif set(member) != {'title','stable_key','skipped_reason'} or any(type(v) is not str or len(v) > 2000 for v in member.values()):
                raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    if len(json.dumps(value).encode()) > 8 * 1024 * 1024:
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    return deepcopy(value)


def capture_context(root: Path, app: Path, records, previous=None, *, generation=None, task_root=None):
    context = deepcopy(previous or {'tasks': {}, 'view': {'groups': [], 'hidden_ids': []}})
    known = {row['job_id']: row for row in records}
    context['tasks'] = {jid: gui for jid, gui in context['tasks'].items() if jid in known}
    live = {jid: policy for jid, policy in context.get('live_recordings', {}).items() if jid in known}
    if generation is None:
        core = app / 'Contents/MacOS/lulu-agent-core'
        if not core.exists():
            return validate_context(context, records)
        result = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(core)],
            capture_output=True, text=True, timeout=10, check=False)
        match = re.search(r'^CDHash=([a-f0-9]{40})$', result.stderr, re.M)
        if result.returncode or match is None:
            raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
        generation = match[1]
    if re.fullmatch(r'[a-f0-9]{40}', generation) is None:
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    jobs = (Path(task_root) if task_root is not None else root / 'task-generations' / generation) / 'jobs'
    if jobs.resolve(strict=False) != jobs:
        raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
    ledger = jobs / 'job-ledger-v1/snapshot.json'
    if ledger.exists():
        data = json.loads(read_nofollow(ledger, mode=0o600, maximum=128 * 1024 * 1024))
        for job in data['jobs']:
            row = known.get(job['job_id'])
            if (row is None or job.get('created_by') != 'gui'
                    or job.get('job_kind') not in {'gui_transcription', 'live_recording'}):
                continue
            if any(job[key] != row[key] for key in ('job_kind', 'state', 'job_revision', 'result_ref')):
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            if job['job_kind'] == 'live_recording':
                # Keep only the authenticated folder identity needed after the
                # old ledger becomes read-only; never trust the media index path.
                policy = {key: job['frozen_policy'][key] for key in ('recording_id', 'host_id', 'directory')}
                if job['job_id'] in live and live[job['job_id']] != policy:
                    raise RuntimeError('PRODUCT_GUI_HANDOVER_INVALID')
                live[job['job_id']] = policy
            else:
                context['tasks'][job['job_id']] = job['frozen_policy']['gui_task']
    view_path = jobs / 'gui-task-view-v1.json'
    if view_path.exists():
        view = json.loads(read_nofollow(view_path, mode=0o600, maximum=4 * 1024 * 1024))
        # Stale hidden IDs from older generations are only display preferences.
        groups = []
        for group in view['groups']:
            group = dict(group)
            group['members'] = [member for member in group['members']
                if 'task_id' not in member or member['task_id'] in known]
            if group['members']:
                groups.append(group)
        context['view'] = {'groups': groups,
            'hidden_ids': [tid for tid in view['hidden_ids'] if tid in known]}
    if live:
        context['live_recordings'] = live
    return validate_context(context, records)

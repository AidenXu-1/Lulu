"""Authenticated public terminal-task projection across product upgrades.

No old task keys or ledger contents are imported. The original ledger remains
available to the original Core; these records are deliberately read-only.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess

from .ownership import _key_name

FILENAME = 'task-handover-v1.json'
DOMAIN = b'Lulu terminal task handover v1\0'
TERMINAL = frozenset({'succeeded', 'partial', 'failed', 'cancelled'})
FIELDS = frozenset({'job_id', 'job_kind', 'job_revision', 'state', 'lease_epoch',
                    'last_sequence', 'result_ref', 'receipt_ref', 'partial_failures'})


def _error():
    return RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def validate_records(records):
    if type(records) is not list or len(records) > 10000:
        raise _error()
    seen = set()
    for row in records:
        if type(row) is not dict or set(row) != FIELDS:
            raise _error()
        jid = row['job_id']
        if (type(jid) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', jid)
                or jid in seen or type(row['job_kind']) is not str
                or not re.fullmatch(r'[a-z_]{1,64}', row['job_kind'])):
            raise _error()
        seen.add(jid)
        if row['state'] not in TERMINAL:
            raise RuntimeError('PRODUCT_UNFINISHED_TASKS')
        if any(type(row[k]) is not int or row[k] < minimum for k, minimum in
               (('job_revision', 1), ('lease_epoch', 0), ('last_sequence', 1))):
            raise _error()
        if row['receipt_ref'] != f"lulu-job:{jid}:revision:{row['job_revision']}":
            raise _error()
        ref = row['result_ref']
        if ref is not None and (type(ref) is not str or not (
                re.fullmatch(re.escape(f'lulu-result:{jid}:sha256:') + r'[a-f0-9]{64}', ref)
                or re.fullmatch(r'lulu-library:[A-Za-z0-9_-]{1,160}', ref)
                or (ref == f'lulu-extraction:{jid}' and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', jid))
                # Workspace conversations publish a body-digest reference from
                # the same installed Core; carry that authenticated pointer only.
                or (row['job_kind'] == 'workspace_conversation'
                    and re.fullmatch(r'workspace-result:sha256:[a-f0-9]{64}', ref)))):
            raise _error()
        # Completed live jobs keep media in the separately preserved recording
        # index/directory, not a task-result artifact. Carry the exact verified
        # predecessor receipt, including its null reference, without fabricating
        # a result or relaxing authentication for other kinds or partial jobs.
        if (row['state'] in {'succeeded', 'partial'} and ref is None
                and not (row['job_kind'] == 'live_recording' and row['state'] == 'succeeded')):
            raise _error()
        if type(row['partial_failures']) is not list or len(row['partial_failures']) > 1000:
            raise _error()
    if len(_canonical(records)) > 8 * 1024 * 1024:
        raise _error()
    return deepcopy(sorted(records, key=lambda row: row['job_id']))


def _key(keys, owner):
    try:
        key = base64.b64decode(keys.get_text(_key_name(owner['installation_id_digest'])), validate=True)
    except Exception as exc:
        raise _error() from exc
    if len(key) != 32:
        raise _error()
    return key


def seal_handover(records, *, keys, owner, previous_manifest, product_root, gui_context=None, source_cores=None):
    body = {'schema_id': 'lulu.terminal-task-handover/v1',
            'installation_id': owner['installation_id'],
            'target_manifest': owner['component_manifest_sha256'],
            'previous_manifest': previous_manifest,
            'product_root': str(product_root), 'jobs': validate_records(records)}
    if gui_context is not None:
        from .task_handover_context import validate_context
        body['gui_context'] = validate_context(gui_context, body['jobs'])
    if source_cores:
        body['source_cores'] = _validate_source_cores(source_cores)
    return {'body': body, 'hmac_sha256': hmac.new(_key(keys, owner), DOMAIN + _canonical(body), hashlib.sha256).hexdigest()}


def _validate_source_cores(value):
    if (type(value) is not dict or len(value) > 1000 or any(
            type(k) is not str or re.fullmatch(r'[a-f0-9]{64}', k) is None or
            type(v) is not str or re.fullmatch(r'[a-f0-9]{40}', v) is None
            for k, v in value.items())):
        raise _error()
    return dict(value)


def open_handover(value, *, keys, owner, product_root):
    if type(value) is not dict or set(value) != {'body', 'hmac_sha256'}:
        raise _error()
    body = value['body']
    fields = {'schema_id', 'installation_id', 'target_manifest', 'previous_manifest', 'product_root', 'jobs'}
    if (type(body) is not dict or not fields <= set(body) or set(body) - fields - {'gui_context','source_cores'}
            or body['schema_id'] != 'lulu.terminal-task-handover/v1'
            or body['installation_id'] != owner['installation_id']
            or body['target_manifest'] != owner['component_manifest_sha256']
            or body['product_root'] != str(product_root)
            or (body['previous_manifest'] is not None and
                (type(body['previous_manifest']) is not str or not re.fullmatch(r'[a-f0-9]{64}', body['previous_manifest'])))
            or type(value['hmac_sha256']) is not str):
        raise _error()
    expected = hmac.new(_key(keys, owner), DOMAIN + _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, value['hmac_sha256']):
        raise _error()
    records = validate_records(body['jobs'])
    if 'gui_context' in body:
        from .task_handover_context import validate_context
        validate_context(body['gui_context'], records)
    if 'source_cores' in body:
        _validate_source_cores(body['source_cores'])
    return records


def capture_terminal_tasks(run_cli, *, inherited=()):
    """Caller verifies installed code and business-tree stability around calls."""
    # These rows must come from open_handover, never a caller's unverified JSON.
    known = {row['job_id']: row for row in validate_records(list(inherited))}
    response = run_cli(['jobs', 'list', '--output', 'json'])
    if type(response) is not dict or set(response) != {'jobs'} or type(response['jobs']) is not list:
        raise _error()
    rows = []
    for summary in response['jobs']:
        if type(summary) is not dict or set(summary) != FIELDS - {'partial_failures'}:
            raise _error()
        if summary['state'] not in TERMINAL:
            raise RuntimeError('PRODUCT_UNFINISHED_TASKS')
        jid = summary.get('job_id')
        if type(jid) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', jid):
            raise _error()
        previous = known.get(jid)
        if previous is not None:
            if {k: v for k, v in previous.items() if k != 'partial_failures'} != summary:
                raise RuntimeError('PRODUCT_TASKS_CHANGED')
            rows.append(previous)
            continue
        receipt = run_cli(['jobs', 'receipt', '--job-id', jid, '--output', 'json'])
        if type(receipt) is dict and summary['job_kind'] == 'local_synthesis':
            # Audio metadata stays in the hash-bound shared result artifact.
            # Handover carries only the same authenticated ledger projection.
            receipt = {k: v for k, v in receipt.items() if k != 'audio'}
        if type(receipt) is dict and summary['job_kind'] == 'link_extraction':
            receipt = {k: v for k, v in receipt.items() if k != 'extraction'}
        if type(receipt) is not dict or {k: v for k, v in receipt.items() if k != 'partial_failures'} != summary:
            raise RuntimeError('PRODUCT_TASKS_CHANGED')
        rows.append(receipt)
    return validate_records(rows)


def canonical_cli(arguments, *, app=None):
    # This is the packaged public entry, never a source import or alternate CLI.
    executable = Path(app or '/Applications/Lulu.app')/'Contents/MacOS/lulu-agent-cli'
    result = subprocess.run([str(executable), *arguments], capture_output=True, timeout=90)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise RuntimeError('PRODUCT_OLD_TASK_SERVICE_UNAVAILABLE')
    from .delivery_identity import strict_json
    value = strict_json(result.stdout)
    if (value.get('schema_id') != 'lulu.agent/v1' or value.get('schema_revision') != 1
            or value.get('ok') is not True or value.get('command') != '.'.join(arguments[:2])):
        raise _error()
    return value['result']


def gui_history(row, gui_task=None):
    return {'job_id': row['job_id'], 'job_revision': row['job_revision'],
            'state': row['state'], 'job_kind': row['job_kind'],
            'input_ref': row['job_id'], 'gui_task': deepcopy(gui_task) if gui_task else {'title': '历史转录', 'kind': 'file'},
            'failure': next((item.get('retry_hint') for item in row['partial_failures']
                            if item.get('code') == 'GUI_TRANSCRIPTION_FAILED'), None),
            'result_ref': row['result_ref'], 'created_by': 'upgrade-history'}

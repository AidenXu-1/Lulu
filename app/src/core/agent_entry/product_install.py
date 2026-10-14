"""Formal v2 product install and independent optional Skill action."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import os
from pathlib import Path
import secrets
import shutil

from .component_manifest import verify_component_manifest
from .delivery_identity import MANIFEST_NAME, DELIVERY_MARKER, strict_json
from .delivery_package import verify_delivery_dmg
from .effects import object_digest, ManagedPathEffect
from .formal_install import LAUNCHER_BYTES
from .installed_materials import MATERIALS_DIR, component_manifest_path, _private_json
from .ownership import create_ownership_receipt, replace_ownership_receipt, verify_ownership_receipt, verify_ready_action_receipt, delete_ownership_materials
from .product_transaction import ProductTransaction, ProductCrash, snapshot_digest, _durable_copy
from .transaction import OptionalSkillTransaction, CrashInjected, _replace_json, _fsync_directory, validate_installation_binding

# Exact shipped v1 bytes from d3325838; do not normalize or parse shell text.
LEGACY_LAUNCHER = b'#!/bin/sh\nexec /Applications/Lulu.app/Contents/MacOS/lulu-agent-cli "$@"\n'


def _record_install_failure(journal, error, *, operation_stage='UNKNOWN', target_name=None):
    """Diagnostic only: retain a closed error code before recovery rewrites phase."""
    codes = {'PRODUCT_GUI_QUIT_REQUIRED', 'PRODUCT_CLIENTS_STILL_RUNNING',
             'PRODUCT_CORE_STILL_RUNNING', 'PRODUCT_CORE_NOT_REGISTERED',
             'PRODUCT_PATH_DRIFT', 'PRODUCT_PATH_UNSAFE', 'PRODUCT_STAGE_MISSING',
             'PRODUCT_STAGE_MISMATCH', 'PRODUCT_FINAL_VERIFICATION_FAILED',
             'AGENT_ENTRY_TRANSACTION_DRIFT', 'PRODUCT_TASKS_CHANGED',
             'PRODUCT_UNFINISHED_TASKS', 'PRODUCT_TASK_HANDOVER_INVALID',
             'PRODUCT_OLD_TASK_SERVICE_UNAVAILABLE'}
    phases = {'PREPARED', 'FINAL_VERIFIED', 'COMMITTED'} | {
        name + suffix for name in ('APP', 'MATERIALS', 'CORE_REGISTRATION',
                                   'SKILL', 'LAUNCHER', 'OWNERSHIP')
        for suffix in ('_INTENT', '_APPLIED')}
    value = _private_json(journal)
    code = error.args[0] if isinstance(error, RuntimeError) and error.args else None
    kind = type(error).__name__
    allowed_types = {'RuntimeError', 'OSError', 'PermissionError', 'FileNotFoundError',
                     'FileExistsError', 'ValueError', 'TimeoutError', 'CalledProcessError'}
    number = getattr(error, 'errno', None)
    count = value.get('effect_count')
    steps = {'stop_clients', 'verify_clients_stopped', 'query_core_before_stop',
             'stop_core', 'verify_core_stopped', 'verify_target_before',
             'save_target_intent', 'backup_target', 'replace_target',
             'verify_target_after', 'save_target_applied', 'start_core',
             'verify_core_started', 'final_verify', 'save_final_verified',
             'write_current', 'save_committed', 'verify_task_handover'}
    targets = {'app', 'materials', 'core_registration', 'skill', 'launcher', 'ownership'}
    target = journal.parent/'failure.json'
    if target.resolve(strict=False) != target or os.path.lexists(target):
        raise FileExistsError('diagnostic target unavailable')
    _replace_json(target, {
        'schema_id': 'lulu.product-install-failure/v1',
        'phase': value.get('phase') if value.get('phase') in phases else 'UNKNOWN',
        'effect_count': count if type(count) is int and 0 <= count <= 6 else None,
        'error_code': code if isinstance(code, str) and code in codes else 'UNKNOWN',
        'error_type': kind if kind in allowed_types else 'Exception',
        'errno': number if type(number) is int else None,
        'operation_stage': operation_stage if operation_stage in steps else 'UNKNOWN',
        'target': target_name if target_name in targets else None,
    })


@dataclass(frozen=True)
class ProductPaths:
    app_bundle: Path
    state_root: Path
    launch_agent_target: Path
    skill_target: Path
    launcher_target: Path


def _write_private(path,raw):
    path=Path(path)
    if path.resolve(strict=False)!=path:raise RuntimeError('PRODUCT_PATH_UNSAFE')
    missing=[];parent=path.parent
    while not parent.exists():missing.append(parent);parent=parent.parent
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    for directory in reversed(missing):_fsync_directory(directory.parent)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(raw);stream.flush();os.fsync(fd)
    finally:os.close(fd)
    _fsync_directory(path.parent)


def _binding(ownership):
    return {field:ownership[field] for field in ('release_dmg_sha256','component_manifest_sha256',
        'app_component_set_digest','installation_id_digest','candidate_generation')}


class ProductInstaller:
    def __init__(self,paths,*,key_store,service,stop_clients,clients_stopped,legacy_reference_app=None,task_exporter=None):
        self.paths=paths;self.keys=key_store;self.service=service
        self.stop_clients=stop_clients;self.clients_stopped=clients_stopped
        self.legacy_reference_app=legacy_reference_app
        self.task_exporter=task_exporter

    def _task_snapshot(self):
        root=self.paths.state_root.parent
        return {name:snapshot_digest(root/name) for name in
                ('jobs','task-integrity-v1','task-generations')}

    def _prepare_task_handover(self,materials,previous,owner):
        if self.task_exporter is None:return None
        from .task_handover import FILENAME, capture_terminal_tasks, open_handover, seal_handover
        from .task_handover_context import capture_context
        root=self.paths.state_root.parent
        before=self._task_snapshot()
        gui_context=None
        if previous is None:
            if any(value is not None for value in before.values()):
                raise RuntimeError('PRODUCT_TASK_HANDOVER_INVALID')
            rows=[]
        else:
            same_generation = previous['component_manifest_sha256']==owner['component_manifest_sha256']
            if same_generation:
                # Repair from the exact verified delivery. The old App can be
                # damaged; authenticate ownership and the existing handover,
                # preserve its live ledger, and never execute damaged code.
                if self._previous()[0]!=previous:
                    raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
                value=_private_json(self.paths.state_root/MATERIALS_DIR/FILENAME)
                rows=open_handover(value,keys=self.keys,owner=previous,product_root=root)
                gui_context=value['body'].get('gui_context')
            else:
                actual,_=self.verify_installed()
                if actual!=previous:raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
                rows=capture_terminal_tasks(self.task_exporter)
                old_file=self.paths.state_root/MATERIALS_DIR/FILENAME
                previous_context=None
                if old_file.exists():
                    old_value=_private_json(old_file)
                    open_handover(old_value,keys=self.keys,owner=previous,product_root=root)
                    previous_context=old_value['body'].get('gui_context')
                gui_context=capture_context(root,self.paths.app_bundle,rows,previous_context)
            actual = self._previous()[0] if same_generation else self.verify_installed()[0]
            if actual!=previous:
                raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
        if self._task_snapshot()!=before:raise RuntimeError('PRODUCT_TASKS_CHANGED')
        value=seal_handover(rows,keys=self.keys,owner=owner,
            previous_manifest=previous['component_manifest_sha256'] if previous else None,
            product_root=root,gui_context=gui_context)
        _write_private(materials/FILENAME,json.dumps(value,sort_keys=True,separators=(',',':')).encode())
        def quiescent():
            if self._task_snapshot()!=before:raise RuntimeError('PRODUCT_TASKS_CHANGED')
        return quiescent

    def _validate_paths(self):
        for path in (self.paths.app_bundle,self.paths.state_root,self.paths.launch_agent_target,
                     self.paths.skill_target,self.paths.launcher_target):
            if not path.is_absolute() or path.resolve(strict=False)!=path:
                raise RuntimeError('PRODUCT_PATH_UNSAFE')

    @staticmethod
    def _validate_id(transaction_id):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{7,159}',transaction_id):
            raise ValueError('invalid transaction id')

    def _all_targets(self):
        return {'app':self.paths.app_bundle,'materials':self.paths.state_root/MATERIALS_DIR,
            **self._managed_targets(),'ownership':self.paths.state_root/'ownership.json',
            'current':self.paths.state_root/'current.json'}

    def _managed_targets(self):
        return {'core_registration':self.paths.launch_agent_target,
                'skill':self.paths.skill_target,'launcher':self.paths.launcher_target}

    def _previous(self):
        state=self.paths.state_root;targets=self._managed_targets()
        if not os.path.lexists(state/'ownership.json'):
            if (os.path.lexists(state/'current.json') or self.service.is_loaded()
                or any(os.path.lexists(p) for p in targets.values())):
                raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
            return None,False
        owner=verify_ownership_receipt(state/'ownership.json',key_store=self.keys)
        verify_ready_action_receipt(state/'current.json',ownership=owner)
        stored=owner.get('managed_effect_digests')
        if stored is None:
            # v1 did not record per-effect hashes. Require the exact old App as
            # evidence of those objects; no old source DMG is opened or required.
            if self.legacy_reference_app is None:
                candidates=[self.paths.app_bundle,*sorted(self.paths.app_bundle.parent.glob('Lulu.backup-*'))]
                for candidate in candidates:
                    manifest=candidate/'Contents/Resources/agent_product_manifest.json'
                    if candidate.is_symlink() or not manifest.is_file() or manifest.is_symlink():continue
                    if hashlib.sha256(manifest.read_bytes()).hexdigest()==owner['component_manifest_sha256']:
                        self.legacy_reference_app=candidate
                        break
            old=Path(self.legacy_reference_app) if self.legacy_reference_app is not None else self.paths.app_bundle
            verified=verify_component_manifest(old,old/'Contents/Resources/agent_product_manifest.json')
            if any(verified[field]!=owner[field] for field in ('component_manifest_sha256','app_component_set_digest')):
                raise RuntimeError('LEGACY_MANAGED_REFERENCE_REQUIRED')
            for name,source in (
                ('core_registration',old/'Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist'),
                ('skill',old/'Contents/Resources/skills/lulu-agent')):
                if object_digest(targets[name])!=object_digest(source):raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
            launcher=targets['launcher'];meta=launcher.lstat()
            if launcher.is_symlink() or meta.st_uid!=os.getuid() or meta.st_mode&0o777!=0o700 or launcher.read_bytes() not in (LAUNCHER_BYTES,LEGACY_LAUNCHER):
                raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
            optional=True
        else:
            for name,digest in stored.items():
                if name not in targets or object_digest(targets[name])!=digest:raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
            optional='skill' in stored
            if not optional and any(os.path.lexists(targets[name]) for name in ('skill','launcher')):
                raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
        # An authorized product replacement can preserve a mismatched/broken old
        # App verbatim; its old receipt never authorizes the new generation.
        return owner,optional

    def _stage_ownership(self,path,previous,verified,release_sha,effects,*,transaction_id,approved):
        fields={'release_dmg_sha256':release_sha,'component_manifest_sha256':verified['component_manifest_sha256'],
            'app_component_set_digest':verified['app_component_set_digest'],'candidate_generation':9,
            'managed_effect_digests':effects,'product_scope':True}
        if previous is None:
            installation_id='lulu-'+secrets.token_hex(16)
            binding={name:fields[name] for name in ('release_dmg_sha256','component_manifest_sha256','app_component_set_digest','candidate_generation')}
            binding['installation_id_digest']=hashlib.sha256(installation_id.encode()).hexdigest()
            intent={'schema_id':'lulu.product-key-intent/v1','transaction_id':transaction_id,
                'binding':binding,'before':approved,'phase':'KEY_WRITE_INTENT'}
            _write_private(self.paths.state_root/'product-key-intents'/f'{transaction_id}.json',
                (json.dumps(intent,sort_keys=True,separators=(',',':'))+'\n').encode())
            return create_ownership_receipt(path,key_store=self.keys,installation_id=installation_id,**fields)
        _durable_copy(self.paths.state_root/'ownership.json',path)
        return replace_ownership_receipt(path,key_store=self.keys,previous=previous,**fields)

    def install_from_dmg(self,dmg,*,source_app,expected_sha256,stage_root,transaction_id,crash_after=None):
        self._validate_paths();self._validate_id(transaction_id)
        previous,optional=self._previous()
        approved={name:snapshot_digest(path) for name,path in self._all_targets().items()}
        delivery=verify_delivery_dmg(Path(dmg),expected_app=Path(source_app),
            expected_sha256=expected_sha256,stage_root=Path(stage_root))
        self._validate_paths()
        if any(snapshot_digest(path)!=approved[name] for name,path in self._all_targets().items()):
            raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
        if self._previous()!=(previous,optional):raise RuntimeError("AGENT_ENTRY_OWNERSHIP_CONFLICT")
        stage=Path(stage_root);app=delivery.staged_app
        verified=verify_component_manifest(app,stage/MANIFEST_NAME)
        materials=stage/MATERIALS_DIR;materials.mkdir(mode=0o700)
        _write_private(materials/MANIFEST_NAME,delivery.manifest_bytes)
        _replace_json(materials/'verified-dmg.json',delivery.receipt)
        sources={'app':app,'materials':materials,
            'core_registration':app/'Contents/Library/LaunchAgents/com.lulu.transcribe.agent-core.plist'}
        targets={'app':self.paths.app_bundle,'materials':self.paths.state_root/MATERIALS_DIR,
                 'core_registration':self.paths.launch_agent_target}
        if optional:
            launcher=stage/'launcher';_write_private(launcher,LAUNCHER_BYTES);launcher.chmod(0o700)
            sources.update(skill=app/'Contents/Resources/skills/lulu-agent',launcher=launcher)
            targets.update(skill=self.paths.skill_target,launcher=self.paths.launcher_target)
        effect_digests={name:object_digest(source) for name,source in sources.items() if name in self._managed_targets()}
        owner=self._stage_ownership(stage/'ownership.json',previous,verified,expected_sha256,effect_digests,transaction_id=transaction_id,approved=approved)
        validate_quiescent=self._prepare_task_handover(materials,previous,owner)
        sources['ownership']=stage/'ownership.json';targets['ownership']=self.paths.state_root/'ownership.json'
        before={name:snapshot_digest(path) for name,path in targets.items()}
        tx=ProductTransaction(self.paths.state_root,targets,service=self.service)

        def final():
            actual=verify_component_manifest(self.paths.app_bundle,self.paths.state_root/MATERIALS_DIR/MANIFEST_NAME)
            if actual!=verified:return False
            if verify_ownership_receipt(targets['ownership'],key_store=self.keys)!=owner:return False
            if _private_json(self.paths.state_root/MATERIALS_DIR/'verified-dmg.json')!=delivery.receipt:return False
            return all(object_digest(self._managed_targets()[name])==digest for name,digest in effect_digests.items())
        try:
            result=tx.execute(sources,transaction_id=transaction_id,binding=_binding(owner),
                stop_clients=self.stop_clients,clients_stopped=self.clients_stopped,
                final_verifier=final,crash_after=crash_after,
                validate_quiescent=validate_quiescent,
                approved_before={name:approved[name] for name in targets},approved_current=approved["current"])
        except ProductCrash:
            raise
        except Exception as exc:
            journal=tx._work(transaction_id)/'journal.json'
            if journal.exists():
                try:
                    _record_install_failure(journal, exc,
                        operation_stage=tx.failure_stage, target_name=tx.failure_target)
                except Exception:
                    # Diagnostic failure cannot suppress mandatory recovery.
                    pass
                self.recover_product(transaction_id)
            elif previous is None:
                delete_ownership_materials(stage/"ownership.json",key_store=self.keys,expected_binding=_binding(owner))
            raise
        verify_ready_action_receipt(self.paths.state_root/'current.json',ownership=owner)
        return result

    def recover_pending(self):
        self._validate_paths()
        state=self.paths.state_root;results=[]
        barrier=state/'product-installation.json'
        if os.path.lexists(barrier):
            pending=_private_json(barrier)
            if pending.get('phase') not in {'COMMITTED','ROLLED_BACK'}:
                results.append(self.recover_product(pending['transaction_id']))
        current=state/'current.json'
        if os.path.lexists(current):
            pending=_private_json(current)
            if pending.get('action') in {'skill_install','skill_update'} and pending.get('phase')!='COMMITTED':
                results.append(self.recover_skill(transaction_id=pending['transaction_id']))
        intents=state/'product-key-intents'
        if intents.is_symlink():raise RuntimeError('PRODUCT_PATH_UNSAFE')
        if intents.exists():
            for path in sorted(intents.glob('*.json')):
                intent=_private_json(path)
                if intent.get('phase')=='KEY_WRITE_INTENT':
                    tid=intent['transaction_id'];self._validate_id(tid)
                    if path.name!=tid+'.json':raise RuntimeError('PRODUCT_KEY_INTENT_INVALID')
                    if not (state/'product-transactions'/tid/'journal.json').exists():
                        results.append(self.recover_product(tid))
        return results

    def recover_product(self,transaction_id):
        from .product_transaction import _private_read
        self._validate_paths();self._validate_id(transaction_id)
        journal_path=self.paths.state_root/'product-transactions'/transaction_id/'journal.json'
        if not journal_path.exists():
            intent_path=self.paths.state_root/'product-key-intents'/f'{transaction_id}.json'
            intent=_private_read(intent_path)
            if (set(intent)!={'schema_id','transaction_id','binding','before','phase'}
                or intent['schema_id']!='lulu.product-key-intent/v1' or intent['transaction_id']!=transaction_id
                or set(intent['before'])!=set(self._all_targets()) or intent['before']['ownership'] is not None):
                raise RuntimeError('PRODUCT_KEY_INTENT_INVALID')
            binding=validate_installation_binding(intent['binding'])
            if any(snapshot_digest(path)!=intent['before'][name] for name,path in self._all_targets().items()):
                raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
            delete_ownership_materials(self.paths.state_root/'ownership.json',key_store=self.keys,expected_binding=binding)
            intent['phase']='KEY_WRITE_ROLLED_BACK';_replace_json(intent_path,intent)
            return {'state':'restored_original_state','phase':'KEY_WRITE_ROLLED_BACK'}
        journal=_private_read(journal_path)
        names=tuple(journal['objects'])
        targets={'app':self.paths.app_bundle,'materials':self.paths.state_root/MATERIALS_DIR,
                 'core_registration':self.paths.launch_agent_target}
        if 'skill' in names:targets.update(skill=self.paths.skill_target,launcher=self.paths.launcher_target)
        targets['ownership']=self.paths.state_root/'ownership.json'
        def final():
            verified=verify_component_manifest(self.paths.app_bundle,self.paths.state_root/MATERIALS_DIR/MANIFEST_NAME)
            owner=verify_ownership_receipt(targets['ownership'],key_store=self.keys)
            return (_binding(owner)==journal['binding'] and all(owner[field]==verified[field]
                for field in ('component_manifest_sha256','app_component_set_digest','candidate_generation'))
                and all(object_digest(self._managed_targets()[name])==digest
                        for name,digest in owner['managed_effect_digests'].items()))
        def restored():
            if any(snapshot_digest(path)!=journal['objects'][name]['before'] for name,path in targets.items()):return False
            if journal['objects']['ownership']['before'] is None:
                return delete_ownership_materials(targets['ownership'],key_store=self.keys,expected_binding=journal['binding'])
            verify_ownership_receipt(targets['ownership'],key_store=self.keys)
            return True
        return ProductTransaction(self.paths.state_root,targets,service=self.service).recover(transaction_id,
            stop_clients=self.stop_clients,clients_stopped=self.clients_stopped,
            rollback_verifier=restored,final_verifier=final)

    def verify_installed(self):
        manifest=component_manifest_path(self.paths.app_bundle,state_root=self.paths.state_root)
        verified=verify_component_manifest(self.paths.app_bundle,manifest)
        owner=verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)
        verify_ready_action_receipt(self.paths.state_root/'current.json',ownership=owner)
        if owner['schema_id']!='lulu.agent-entry-ownership/v3':raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
        if any(owner[field]!=verified[field] for field in ('component_manifest_sha256','app_component_set_digest','candidate_generation')):
            raise RuntimeError('AGENT_ENTRY_COMPONENT_MISMATCH')
        for name,digest in owner['managed_effect_digests'].items():
            if object_digest(self._managed_targets()[name])!=digest:raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
        return owner,verified

    def skill_status(self):
        try:
            owner,_verified=self.verify_installed()
            if 'skill' not in owner['managed_effect_digests']:
                if os.path.lexists(self.paths.skill_target) or os.path.lexists(self.paths.launcher_target):return 'ownership_conflict'
                return 'not_installed'
            return 'ready'
        except Exception:return 'incompatible'

    def status(self):
        return self.skill_status()

    def install(self,*,transaction_id,crash_after=None):
        return self.install_skill(transaction_id=transaction_id,crash_after=crash_after)

    def _optional_lock(self):
        targets={'app':self.paths.app_bundle,'materials':self.paths.state_root/MATERIALS_DIR,
            'core_registration':self.paths.launch_agent_target,'ownership':self.paths.state_root/'ownership.json'}
        return ProductTransaction(self.paths.state_root,targets,service=self.service)._lock()

    def install_skill(self,*,transaction_id,crash_after=None):
        self._validate_paths();self._validate_id(transaction_id)
        with self._optional_lock():
            return self._install_skill_locked(transaction_id=transaction_id,crash_after=crash_after)

    def _install_skill_locked(self,*,transaction_id,crash_after=None):
        self._validate_paths();self._validate_id(transaction_id)
        owner,verified=self.verify_installed()
        if 'skill' in owner['managed_effect_digests']:
            return dict(_private_json(self.paths.state_root/'current.json'),effect_count=0)
        if os.path.lexists(self.paths.skill_target) or os.path.lexists(self.paths.launcher_target):
            raise RuntimeError('AGENT_ENTRY_OWNERSHIP_CONFLICT')
        work=self.paths.state_root/'optional-skill'/transaction_id;work.mkdir(mode=0o700,parents=True,exist_ok=False)
        _durable_copy(self.paths.state_root/'ownership.json',work/'ownership.before.json')
        _durable_copy(self.paths.state_root/'current.json',work/'current.before.json')
        launcher=work/'launcher';_write_private(launcher,LAUNCHER_BYTES);launcher.chmod(0o700)
        effects=[ManagedPathEffect(name='skill',source=self.paths.app_bundle/'Contents/Resources/skills/lulu-agent',
            target=self.paths.skill_target,work_root=work/'skill'),
            ManagedPathEffect(name='launcher',source=launcher,target=self.paths.launcher_target,work_root=work/'launcher-effect')]
        def final():
            digests={name:object_digest(path) for name,path in self._managed_targets().items()}
            updated=replace_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys,previous=owner,
                release_dmg_sha256=owner['release_dmg_sha256'],component_manifest_sha256=verified['component_manifest_sha256'],
                app_component_set_digest=verified['app_component_set_digest'],candidate_generation=9,
                managed_effect_digests=digests,product_scope=True)
            return verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)==updated
        tx=OptionalSkillTransaction(self.paths.state_root)
        try:
            return tx.execute('skill_install',effects,transaction_id=transaction_id,binding=_binding(owner),
                final_verifier=final,crash_after=crash_after)
        except CrashInjected:
            raise
        except Exception:
            self._recover_skill_locked(transaction_id=transaction_id)
            raise

    def recover_skill(self,*,transaction_id):
        self._validate_paths();self._validate_id(transaction_id)
        with self._optional_lock():
            return self._recover_skill_locked(transaction_id=transaction_id)

    def _recover_skill_locked(self,*,transaction_id):
        self._validate_paths();self._validate_id(transaction_id)
        work=self.paths.state_root/'optional-skill'/transaction_id
        previous=_private_json(work/'ownership.before.json')
        current=_private_json(work/'current.before.json')
        try:live_current=_private_json(self.paths.state_root/'current.json')
        except Exception as exc:raise RuntimeError('PRODUCT_RECOVERY_DRIFT') from exc
        if live_current!=current:
            if (set(live_current)!=set(current) or live_current.get('schema_id')!='lulu.agent-entry-current-action/v2'
                or live_current.get('transaction_id')!=transaction_id or live_current.get('binding')!=_binding(previous)
                or live_current.get('action')!='skill_install' or type(live_current.get('effect_count')) is not int
                or (live_current.get('phase'),live_current.get('state'),live_current.get('effect_count'))
                    not in {('PREPARED','installing',0),('FINAL_REJECTED','rollback_incomplete',2),('COMMITTED','ready',2)}):
                raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
        effects=[ManagedPathEffect(name='skill',source=self.paths.app_bundle/'Contents/Resources/skills/lulu-agent',
            target=self.paths.skill_target,work_root=work/'skill'),
            ManagedPathEffect(name='launcher',source=work/'launcher',target=self.paths.launcher_target,work_root=work/'launcher-effect')]
        def restore(_binding):
            live=verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)
            if live['installation_id']!=previous['installation_id']:return False
            _replace_json(self.paths.state_root/'ownership.json',previous)
            return verify_ownership_receipt(self.paths.state_root/'ownership.json',key_store=self.keys)==previous
        result=OptionalSkillTransaction(self.paths.state_root).recover(transaction_id,effects,rollback_verifier=restore)
        if result.get('state')=='rollback_complete':
            _replace_json(self.paths.state_root/'current.json',current)
        return result


def build_default_product_installer():
    import sys
    from .standalone_source import DESCRIPTOR
    app = Path(sys.executable).parent.parent.parent
    if getattr(sys, "frozen", False) and (app/DESCRIPTOR).exists():
        from .standalone_paths import installed_app
        from .standalone_runtime import StandaloneInstallation
        from ..license.store import StrictKeyringStore
        from .standalone_source import uses_local_storage
        from ..local_integrity import product_local_store
        keys = product_local_store(Path.home()/'Library/Application Support/Lulu') if uses_local_storage(app) else StrictKeyringStore()
        return StandaloneInstallation(installed_app(app), keys)
    from .task_handover import canonical_cli
    from ..license.store import StrictKeyringStore
    from .effects import CurrentUserLaunchctlService
    from .product_lifecycle import stop_clients,clients_stopped,product_service_controller
    home=Path.home();state=home/'Library/Application Support/Lulu/AgentEntry'
    return ProductInstaller(ProductPaths(Path('/Applications/Lulu.app'),state,
        home/'Library/LaunchAgents/com.lulu.transcribe.agent-core.plist',
        home/'.agents/skills/lulu-agent',home/'.local/bin/lulu'),key_store=StrictKeyringStore(),
        service=product_service_controller(),stop_clients=stop_clients,clients_stopped=clients_stopped,
        task_exporter=canonical_cli)

"""Durable product transaction across App and user volumes, with a business barrier.

Recovery preserves backups and unexpected content. A committed journal is the only
point after which recovery may keep the new generation; earlier points restore
exact pre-install objects, without claiming those objects were healthy.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .delivery_identity import strict_json
from .transaction import _replace_json, _fsync_directory, validate_installation_binding

SCHEMA = 'lulu.product-installation/v2'
BARRIER = 'product-installation.json'
BASE = ('app', 'materials', 'core_registration', 'ownership')
OPTIONAL = ('app', 'materials', 'core_registration', 'skill', 'launcher', 'ownership')


class ProductCrash(RuntimeError):
    pass


def _stable_stat(meta):
    return (meta.st_dev,meta.st_ino,meta.st_mode,meta.st_uid,meta.st_size,meta.st_mtime_ns,meta.st_ctime_ns)


def _private_read(path):
    path=Path(path);meta=path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)!=0o600:
        raise RuntimeError('PRODUCT_JOURNAL_INVALID')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        if _stable_stat(os.fstat(fd))!=_stable_stat(meta):raise RuntimeError('PRODUCT_JOURNAL_INVALID')
        with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read()
        if _stable_stat(os.fstat(fd))!=_stable_stat(meta):raise RuntimeError('PRODUCT_JOURNAL_INVALID')
    finally:os.close(fd)
    return strict_json(raw)


def assert_business_available(state_root):
    path=Path(state_root)/BARRIER
    if not os.path.lexists(path):return
    value=_private_read(path)
    if value.get('schema_id')!=SCHEMA or value.get('phase') not in {'COMMITTED','ROLLED_BACK'}:
        raise RuntimeError('PRODUCT_INSTALLATION_IN_PROGRESS')


def snapshot_digest(path):
    """Full portable snapshot of a transaction object, including modes and links.

    This is backup equality, not a substitute for the final signed App verifier.
    It accepts constrained links only and has no signing or manifest exclusions.
    """
    root=Path(path)
    if not os.path.lexists(root):return None
    records=[];pending=[root]
    while pending:
        item=pending.pop();meta=item.lstat();mode=f'{stat.S_IMODE(meta.st_mode):04o}'
        record={'path':'.' if item==root else item.relative_to(root).as_posix(),'mode':mode}
        if stat.S_ISLNK(meta.st_mode):
            raw=os.readlink(item)
            if item==root or Path(raw).is_absolute():raise RuntimeError('PRODUCT_PATH_UNSAFE')
            try:item.resolve(strict=True).relative_to(root.resolve(strict=True))
            except (ValueError,OSError,RuntimeError) as exc:raise RuntimeError('PRODUCT_PATH_UNSAFE') from exc
            record.update(type='symlink',target=raw)
        elif stat.S_ISDIR(meta.st_mode):
            record['type']='directory';pending.extend(item.iterdir())
        elif stat.S_ISREG(meta.st_mode):
            fd=os.open(item,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0));digest=hashlib.sha256()
            try:
                if _stable_stat(os.fstat(fd))!=_stable_stat(meta):raise RuntimeError('PRODUCT_PATH_DRIFT')
                while block:=os.read(fd,1024*1024):digest.update(block)
                if _stable_stat(os.fstat(fd))!=_stable_stat(meta):raise RuntimeError('PRODUCT_PATH_DRIFT')
            finally:os.close(fd)
            record.update(type='regular',size=meta.st_size,sha256=digest.hexdigest())
        else:raise RuntimeError('PRODUCT_PATH_UNSAFE')
        records.append(record)
    raw=json.dumps(sorted(records,key=lambda x:x['path']),sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _durable_copy(source,target):
    if os.path.lexists(target):raise RuntimeError('PRODUCT_STAGE_ALREADY_EXISTS')
    source=Path(source);target=Path(target)
    if source.is_dir():shutil.copytree(source,target,symlinks=True)
    else:shutil.copy2(source,target)
    files=[];dirs=[]
    if target.is_dir():
        for base,children,names in os.walk(target,followlinks=False):
            dirs.append(Path(base))
            files.extend(Path(base)/name for name in names)
    else:files=[target]
    for path in files:
        if path.is_symlink():continue
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:os.fsync(fd)
        finally:os.close(fd)
    for path in reversed(dirs):_fsync_directory(path)
    _fsync_directory(target.parent)


def _move(source,target):
    if os.path.lexists(target):raise RuntimeError('PRODUCT_PATH_CONFLICT')
    os.rename(source,target)
    _fsync_directory(Path(source).parent)
    if Path(source).parent!=Path(target).parent:_fsync_directory(Path(target).parent)


@contextmanager
def product_state_lock(state_root):
    """Same nonblocking mutex for product switching and Core first preparation."""
    root = Path(state_root)
    if not root.is_absolute() or root.resolve(strict=False) != root:
        raise RuntimeError("PRODUCT_PATH_UNSAFE")
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.is_symlink():raise RuntimeError('PRODUCT_PATH_UNSAFE')
    fd=os.open(root/'product-install.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        meta=os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=os.getuid() or stat.S_IMODE(meta.st_mode)!=0o600:
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('PRODUCT_INSTALLATION_BUSY') from exc
        yield
    finally:os.close(fd)


class ProductTransaction:
    def __init__(self,state_root,targets,*,service):
        self.root=Path(state_root)
        self.targets={name:Path(path) for name,path in targets.items()}
        if tuple(self.targets) not in {BASE,OPTIONAL} or len(set(self.targets.values()))!=len(self.targets):
            raise ValueError('product effects must be the exact base or base plus optional set')
        if any(not p.is_absolute() for p in self.targets.values()) or not self.root.is_absolute():
            raise ValueError('absolute product paths required')
        if any(p.resolve(strict=False)!=p for p in (*self.targets.values(),self.root)):
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        if any(a in b.parents for a in self.targets.values() for b in self.targets.values() if a!=b):
            raise RuntimeError('PRODUCT_PATH_UNSAFE')
        self.service=service
        self.failure_stage='UNKNOWN'
        self.failure_target=None

    def _diagnostic_step(self, stage, target=None):
        # In-memory diagnostics only; never changes the strict journal schema.
        self.failure_stage=stage
        self.failure_target=target

    def _lock(self):
        return product_state_lock(self.root)

    def _work(self,tid):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{7,159}',tid):raise ValueError('invalid transaction id')
        return self.root/'product-transactions'/tid

    def _locations(self,tid,name):
        target=self.targets[name]
        return (target.parent/f'.{target.name}.lulu-stage-{tid}',
                target.parent/f'.{target.name}.lulu-backup-{tid}',
                target.parent/f'.{target.name}.lulu-failed-{tid}',
                target.parent/f'.{target.name}.lulu-restore-{tid}')

    def _save(self,journal,phase):
        journal['phase']=phase
        if phase == 'COMMITTED': journal['committed'] = True
        _replace_json(self._work(journal['transaction_id'])/'journal.json',journal)
        _replace_json(self.root/BARRIER,{'schema_id':SCHEMA,'transaction_id':journal['transaction_id'],'phase':phase})

    @staticmethod
    def _cut(crash_after,point):
        if crash_after==point:raise ProductCrash(point)

    def execute(self,sources,*,transaction_id,binding,stop_clients,clients_stopped,final_verifier,crash_after=None,approved_before=None,approved_current=None,validate_quiescent=None):
        exact=validate_installation_binding(binding)
        if tuple(sources)!=tuple(self.targets):raise ValueError('source set mismatch')
        with self._lock():
            assert_business_available(self.root)
            work=self._work(transaction_id);work.mkdir(mode=0o700,parents=True,exist_ok=False)
            current=self.root/'current.json';current_before=snapshot_digest(current)
            if approved_before is not None and current_before!=approved_current:raise RuntimeError('PRODUCT_PATH_DRIFT')
            if current_before is not None:_durable_copy(current,work/'current.before.json')
            objects={}
            for name,target in self.targets.items():
                if target.is_symlink():raise RuntimeError('PRODUCT_PATH_UNSAFE')
                target.parent.mkdir(parents=True,exist_ok=True)
                stage,backup,failed,restore=self._locations(transaction_id,name)
                if any(os.path.lexists(p) for p in (stage,backup,failed,restore)):
                    raise RuntimeError('PRODUCT_PATH_CONFLICT')
                before=snapshot_digest(target);after=snapshot_digest(sources[name])
                if approved_before is not None and (set(approved_before)!=set(self.targets) or before!=approved_before[name]):
                    raise RuntimeError('PRODUCT_PATH_DRIFT')
                if after is None:raise RuntimeError('PRODUCT_STAGE_MISSING')
                _durable_copy(sources[name],stage)
                if snapshot_digest(stage)!=after:raise RuntimeError('PRODUCT_STAGE_MISMATCH')
                objects[name]={'target':str(target),'before':before,'after':after}
            planned={'schema_id':'lulu.agent-entry-current-action/v2','transaction_id':transaction_id,
                'action':'product_update' if objects['app']['before'] is not None else 'product_install',
                'phase':'COMMITTED','state':'ready','effect_count':len(self.targets),'binding':exact}
            _replace_json(work/'current.after.json',planned)
            journal={'schema_id':SCHEMA,'transaction_id':transaction_id,'binding':exact,
                'objects':objects,'current_before':current_before,'service_before':self.service.is_loaded(),
                'phase':'PREPARED','effect_count':0,'committed':False,
                'current_after':snapshot_digest(work/'current.after.json')}
            self._save(journal,'PREPARED');self._cut(crash_after,'prepared')
            self._diagnostic_step('stop_clients')
            stop_clients()
            self._diagnostic_step('verify_clients_stopped')
            if not clients_stopped():raise RuntimeError('PRODUCT_CLIENTS_STILL_RUNNING')
            self._diagnostic_step('query_core_before_stop')
            if self.service.is_loaded():
                self._diagnostic_step('stop_core')
                self.service.stop()
            self._diagnostic_step('verify_core_stopped')
            if self.service.is_loaded():raise RuntimeError('PRODUCT_CORE_STILL_RUNNING')
            if validate_quiescent is not None:
                self._diagnostic_step('verify_task_handover')
                validate_quiescent()
            self._cut(crash_after,'clients_stopped')
            for name,target in self.targets.items():
                stage,backup,_failed,_restore=self._locations(transaction_id,name)
                item=objects[name]
                self._diagnostic_step('verify_target_before',name)
                if snapshot_digest(target)!=item['before']:raise RuntimeError('PRODUCT_PATH_DRIFT')
                self._diagnostic_step('save_target_intent',name)
                self._save(journal,name.upper()+'_INTENT');self._cut(crash_after,name+':intent')
                self._diagnostic_step('backup_target',name)
                if item['before'] is not None:_move(target,backup)
                self._cut(crash_after,name+':backup')
                self._diagnostic_step('replace_target',name)
                _move(stage,target);journal['effect_count']+=1
                self._cut(crash_after,name+':effect')
                self._diagnostic_step('verify_target_after',name)
                if snapshot_digest(target)!=item['after']:raise RuntimeError('PRODUCT_PATH_DRIFT')
                self._diagnostic_step('save_target_applied',name)
                self._save(journal,name.upper()+'_APPLIED');self._cut(crash_after,name+':applied')
            self._diagnostic_step('start_core')
            self.service.start(self.targets['core_registration'])
            self._diagnostic_step('verify_core_started')
            if not self.service.is_loaded():raise RuntimeError('PRODUCT_CORE_NOT_REGISTERED')
            self._cut(crash_after,'service_started')
            self._diagnostic_step('final_verify')
            if final_verifier() is not True:raise RuntimeError('PRODUCT_FINAL_VERIFICATION_FAILED')
            self._diagnostic_step('save_final_verified')
            self._save(journal,'FINAL_VERIFIED');self._cut(crash_after,'final_verified')
            result={'schema_id':'lulu.agent-entry-current-action/v2','transaction_id':transaction_id,
                'action':'product_update' if objects['app']['before'] is not None else 'product_install',
                'phase':'COMMITTED','state':'ready','effect_count':journal['effect_count'],'binding':exact}
            self._diagnostic_step('write_current')
            _replace_json(self.root/'current.json',result)
            self._cut(crash_after,'current_written')
            self._diagnostic_step('save_committed')
            self._save(journal,'COMMITTED');self._cut(crash_after,'committed')
            return result

    def recover(self,transaction_id,*,stop_clients,clients_stopped,rollback_verifier,final_verifier):
        with self._lock():
            work=self._work(transaction_id);journal=_private_read(work/'journal.json')
            if (set(journal)!={'schema_id','transaction_id','binding','objects','current_before','service_before','phase','effect_count','committed','current_after'}
                or journal['schema_id']!=SCHEMA or journal['transaction_id']!=transaction_id
                or set(journal['objects'])!=set(self.targets)):
                raise RuntimeError('PRODUCT_JOURNAL_INVALID')
            validate_installation_binding(journal['binding'])
            valid_hash=lambda value:type(value) is str and re.fullmatch(r'[0-9a-f]{64}',value) is not None
            if (type(journal['service_before']) is not bool or type(journal['effect_count']) is not int
                or not 0<=journal['effect_count']<=len(self.targets) or not valid_hash(journal['current_after'])
                or (journal['current_before'] is not None and not valid_hash(journal['current_before']))):
                raise RuntimeError('PRODUCT_JOURNAL_INVALID')
            for name,target in self.targets.items():
                item=journal['objects'][name]
                if (set(item)!={'target','before','after'} or item['target']!=str(target)
                    or not valid_hash(item['after']) or (item['before'] is not None and not valid_hash(item['before']))):
                    raise RuntimeError('PRODUCT_JOURNAL_INVALID')
            barrier=_private_read(self.root/BARRIER)
            if barrier.get('transaction_id')!=transaction_id:raise RuntimeError('PRODUCT_RECOVERY_SUPERSEDED')
            if type(journal['committed']) is not bool: raise RuntimeError('PRODUCT_JOURNAL_INVALID')
            if journal['committed']:
                if (any(snapshot_digest(target)!=journal['objects'][name]['after'] for name,target in self.targets.items())
                    or not self.service.is_loaded() or final_verifier() is not True):
                    self._save(journal,'COMMITTED_DRIFT');raise RuntimeError('PRODUCT_FINAL_VERIFICATION_FAILED')
                current=_private_read(self.root/'current.json')
                if current.get('binding')!=journal['binding'] or current.get('transaction_id')!=transaction_id:
                    self._save(journal,'COMMITTED_DRIFT');raise RuntimeError('PRODUCT_JOURNAL_INVALID')
                self._save(journal,'COMMITTED')
                return current
            self._save(journal,'RECOVERING')
            stop_clients()
            if not clients_stopped():raise RuntimeError('PRODUCT_CLIENTS_STILL_RUNNING')
            if self.service.is_loaded():self.service.stop()
            if self.service.is_loaded():raise RuntimeError('PRODUCT_CORE_STILL_RUNNING')
            # The current receipt is a managed object too. Preserve a third value
            # in place, before restoring any target or interpreting its JSON.
            if snapshot_digest(self.root/'current.json') not in (journal['current_before'],journal['current_after']):
                raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
            # Preflight every object before moving any object on recovery.
            for name,target in self.targets.items():
                item=journal['objects'][name];live=snapshot_digest(target)
                _stage,backup,failed,_restore=self._locations(transaction_id,name)
                if live not in (None,item['before'],item['after']):raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
                if live!=item['before'] and item['before'] is not None and snapshot_digest(backup)!=item['before']:
                    raise RuntimeError('PRODUCT_BACKUP_REQUIRED')
                if live==item['after'] and live!=item['before'] and os.path.lexists(failed):
                    raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
            for name,target in reversed(tuple(self.targets.items())):
                item=journal['objects'][name];live=snapshot_digest(target)
                _stage,backup,failed,restore=self._locations(transaction_id,name)
                if live==item['before']:continue
                if live is not None:_move(target,failed)
                if item['before'] is not None:
                    if not os.path.lexists(restore):_durable_copy(backup,restore)
                    if snapshot_digest(restore)!=item['before']:raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
                    _move(restore,target)
                if snapshot_digest(target)!=item['before']:raise RuntimeError('PRODUCT_RECOVERY_DRIFT')
            current=self.root/'current.json';saved=work/'current.before.json'
            if journal['current_before'] is not None:
                if snapshot_digest(saved)!=journal['current_before']:raise RuntimeError('PRODUCT_BACKUP_REQUIRED')
                if snapshot_digest(current)!=journal['current_before']:
                    if os.path.lexists(current):_move(current,work/'current.failed.json')
                    _durable_copy(saved,current)
            elif os.path.lexists(current):_move(current,work/'current.failed.json')
            if journal['service_before']:
                self.service.start(self.targets['core_registration'])
                if not self.service.is_loaded():raise RuntimeError('PRODUCT_CORE_NOT_REGISTERED')
            if rollback_verifier() is not True:raise RuntimeError('PRODUCT_ROLLBACK_VERIFICATION_FAILED')
            self._save(journal,'ROLLED_BACK')
            return {'state':'restored_original_state','phase':'ROLLED_BACK','transaction_id':transaction_id}

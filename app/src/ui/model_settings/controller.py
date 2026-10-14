"""Native model lifecycle. No hidden controls or copied model installation logic."""
from __future__ import annotations
import copy
import json
import time
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog
from ...core import models_manager as mm
from ...core.engines.local_models import LOCAL_MODELS, available_local_models
from ...core.synthesis import runtime_packages as rp
from .. import dialogs
from ..worker import TTSRuntimeInstallWorker, QwenFeatureWorker, ModelRemoveWorker, ModelsRelocateWorker
from .workers import ReadWorker, DownloadWorker

ROOT = Path(__file__).resolve().parent
MODEL_IDS = dict(whisper='local_whisper_mlx', sensevoice='local_sensevoice', qwen_asr='local_qwen3',
                 indextts2='local_indextts2_0', indextts25='local_indextts2', voxcpm2='local_voxcpm2', qwen_tts='local_qwen3_tts')
FEATURE_IDS = dict(clone='Base', preset='CustomVoice', design='VoiceDesign')


class ModelController(QObject):
    changed = Signal(dict)
    notice = Signal(str)
    reply = Signal(str, object)

    def __init__(self, cfg, parent=None, *, on_changed=None, storage_busy=None, model_in_use=None,
                 relocation_busy=None, storage_activity_changed=None):
        super().__init__(parent)
        self.cfg, self.host = cfg, parent
        self.on_changed = on_changed or (lambda: None)
        self.storage_busy = storage_busy or (lambda: False)
        self.model_in_use = model_in_use or (lambda mid: '')
        self.relocation_busy = relocation_busy or (lambda: False)
        self.storage_activity_changed = storage_activity_changed or (lambda active: None)
        self.workers = []
        self.active = {}  # one owner for each mutation, not the visible card
        self.jobs = {}
        self.state = {}
        self.closing = False
        self.read_worker = None
        self.read_pending = False
        from ...core.model_connections import Service
        self.cloud = Service(cfg)

    def _read_state(self):
        public = json.loads((ROOT/'public.json').read_text())
        allowed = {m.id for m in available_local_models()}
        local = public['localModels']
        local['extract'] = [m for m in local['extract'] if MODEL_IDS[m['id']] in allowed]
        for kind, rows in local.items():
            for row in rows:
                mid = MODEL_IDS[row['id']]
                if kind == 'extract':
                    installed = mm.is_downloaded(mid)
                    repair = mm.needs_repair(mid)
                    issue = mm.runtime_issue(mid) or ''
                    ready = installed and not issue and LOCAL_MODELS[mid].ready
                    partial = mm.resumable_download_size(mid)
                else:
                    status = rp.status(mid)
                    installed = status == 'ready'
                    repair = status in {'needs_repair', 'runtime_only'}
                    issue = '' if rp.current_platform() else '当前电脑不支持此配音模型。'
                    ready = installed and not issue
                    partial = rp.partial_install_bytes(mid)
                    row['importOnly'] = not rp.TTS_RUNTIMES[mid].runtime_download_available and not rp.is_runtime_installed(mid)
                row.update(modelId=mid, installed=installed, ready=ready, repair=repair, issue=issue,
                           resumable=partial, default=(self.cfg.engine_name == mid if kind=='extract' else getattr(self.cfg,'default_tts_model','local_indextts2')==mid))
        from ...core.synthesis import qwen3_deployment as dp, qwen3_weights as qw
        runtime = rp.is_runtime_installed('local_qwen3_tts')
        deployment = dp.read_state()
        feature_jobs = {}
        for f in public['voiceFeatures']:
            name = FEATURE_IDS[f['id']]
            installed = qw.is_installed(dp.root(), name)
            phase=deployment['features'].get(name,{}).get('state','')
            f.update(installed=installed, ready=runtime and installed, phase=phase)
            if phase in {'queued','installing','paused','cancelling'}:
                owner_active='qwen' in self.active
                paused=phase=='paused' or (not owner_active and phase in {'queued','installing'})
                feature_jobs['feature:'+f['id']]=dict(status='paused' if paused else 'running', paused=paused, progress=-1,
                    message='已暂停，下载进度保留。' if paused else '等待安装…' if phase=='queued' else '正在取消…' if phase=='cancelling' else '正在安装…')
        qwen = next(m for m in local['voice'] if m['id']=='qwen_tts')
        qwen['ready'] = any(f['ready'] for f in public['voiceFeatures'])
        qwen['installed'] = any(f['installed'] for f in public['voiceFeatures'])
        return dict(self.cloud.snapshot(), localModels=local,
                    voiceFeatures=public['voiceFeatures'], featureJobs=feature_jobs, storage=dict(extract=str(mm.storage_dir()),voice=str(rp.tts_root())))

    def publish(self):
        if not self.closing and self.state:
            # A completed settings write wins over an older background snapshot.
            for kind, rows in self.state.get('localModels', {}).items():
                selected = self.cfg.engine_name if kind == 'extract' else self.cfg.default_tts_model
                for row in rows:
                    row['default'] = MODEL_IDS[row['id']] == selected
            jobs=copy.deepcopy(self.jobs)
            jobs.pop('qwen',None)
            # Durable per-feature phase owns pause/resume; no synthetic queue state.
            for key, job in self.state.get('featureJobs',{}).items():
                jobs[key]=dict(job)
                if key in self.jobs and not job['paused']:
                    jobs[key].update(self.jobs[key])
            self.changed.emit(dict(self.state, jobs=jobs))

    def refresh(self):
        if self.closing:
            return
        if self.read_worker:
            self.read_pending = True
            return
        worker = ReadWorker(self._read_state)
        self.read_worker = worker
        def received(value, error):
            if self.closing:
                return
            if error:
                self.notice.emit(error)
            elif not self.read_pending:
                self.state = value
                self.publish()
        worker.result.connect(received)
        def settled():
            self.read_worker = None
            if self.read_pending and not self.closing:
                self.read_pending = False
                self.refresh()
        self._start(worker, finished=settled)

    def _start(self, worker, *, key=None, mid=None, finished=None):
        self.workers.append(worker)
        if key:
            self.active[key] = (worker, mid)
        def settled():
            if key and self.active.get(key, (None,))[0] is worker:
                self.active.pop(key, None)
            if worker in self.workers:
                self.workers.remove(worker)
            if finished:
                finished()
            worker.deleteLater()
        worker.finished.connect(settled)
        worker.start()

    def owned_workers(self):
        return list(self.workers)

    def has_active_work(self):
        return any(w.isRunning() for w, _ in self.active.values())

    def active_model_ids(self):
        return {mid for w, mid in self.active.values() if mid and w.isRunning()}

    def storage_is_busy(self):
        return any(w.isRunning() and (mid or key=='storage') for key, (w, mid) in self.active.items())

    def _gate(self, mid=None, *, destructive=False):
        if self.closing or self.storage_busy() or self.relocation_busy():
            raise RuntimeError('本地资料正在迁移或关闭，请稍后再试。')
        if self.storage_is_busy() or any(k.startswith('cloud-save:') and w.isRunning() for k,(w,_) in self.active.items()):
            raise RuntimeError('有模型操作正在进行，请等它结束或暂停后再试。')
        if mid and (destructive or self.model_in_use(mid)):
            reason = self.model_in_use(mid)
            if reason:
                raise RuntimeError(str(reason))

    def _selection_ready(self, mid):
        if mid in LOCAL_MODELS:
            return mm.is_ready(mid)
        if mid == 'local_qwen3_tts':
            from ...core.synthesis import qwen3_weights as qw, qwen3_deployment as dp
            return rp.is_runtime_installed(mid) and any(qw.is_installed(dp.root(), n) for n in FEATURE_IDS.values())
        return rp.status(mid) == 'ready'

    def selection_request(self, request_id, args):
        if set(args) != {'id', 'action'} or args.get('id') not in MODEL_IDS or args.get('action') != 'default':
            raise ValueError('模型操作无效')
        mid = MODEL_IDS[args['id']]
        # This is a preference, not a model load. Use the background snapshot
        # already displayed by the page; execution retains its full validation.
        ready = any(row.get('id') == args['id'] and row.get('ready')
                    for rows in self.state.get('localModels', {}).values() for row in rows)
        try:
            self._selection(mid, verified=ready)
        except Exception as exc:
            self.reply.emit(request_id, dict(ok=False, message=str(exc)))
        else:
            self.reply.emit(request_id, dict(ok=True, value=None))

    def _selection(self, mid, *, verified=None):
        self._gate(mid)
        if not (self._selection_ready(mid) if verified is None else verified):
            raise RuntimeError('请先准备好模型再选用。')
        field = 'engine_name' if mid in LOCAL_MODELS else 'default_tts_model'
        old = getattr(self.cfg, field)
        setattr(self.cfg, field, mid)
        try:
            if not self.cfg.save():
                raise RuntimeError('无法保存模型选择，已保留原设置。')
        except Exception:
            setattr(self.cfg, field, old)
            raise
        self.on_changed()
        self.publish()

    def _progress(self, key, message, fraction):
        self.jobs[key] = dict(progress=100*fraction if fraction>=0 else -1, status='running', paused=False, message=message)
        if key=='qwen':
            self.refresh()
        self.publish()

    def _operation(self, worker, key, mid, *, qwen=False):
        self.jobs[key] = dict(progress=-1, status='running', paused=False, message='正在准备…')
        outcome = {'error':'', 'paused':False}
        if isinstance(worker, DownloadWorker):
            worker.progress_detail.connect(lambda m, f: self._progress(key,m,f))
        elif hasattr(worker, 'progress'):
            worker.progress.connect(lambda m, f: self._progress(key,m,f))
        if hasattr(worker,'failed'):
            worker.failed.connect(lambda text: outcome.update(error=text))
        if hasattr(worker,'cancelled'):
            worker.cancelled.connect(lambda: outcome.update(paused=True))
        if hasattr(worker,'settled'):
            worker.settled.connect(lambda error: outcome.update(error=error))
        def done():
            error = getattr(worker,'error',None) or outcome['error']
            paused = getattr(worker,'was_cancelled',False) or outcome['paused']
            if qwen:
                for feature in FEATURE_IDS:
                    self.jobs.pop('feature:'+feature,None)
            if error:
                self.jobs.pop(key,None)
                self.notice.emit(error)
            elif paused:
                self.jobs[key] = dict(progress=-1,status='paused',paused=True,message='已暂停，下载进度已保留。')
            else:
                self.jobs.pop(key,None)
            if not self.closing:
                self.on_changed()
                self.refresh()
                self.publish()
        self._start(worker,key=key,mid=mid,finished=done)
        self.publish()

    def local_action(self, id, action):
        if id not in MODEL_IDS or action not in {'install','pause','resume','cancel','remove','default'}:
            raise ValueError('模型操作无效')
        mid = MODEL_IDS[id]
        if action in {'pause','cancel'}:
            active = self.active.get(id)
            if active:
                worker = active[0]
                if not hasattr(worker,'request_cancel'):
                    raise RuntimeError('正在完成文件操作，请稍等。')
                worker.request_cancel()
                self.jobs[id]['message'] = '正在暂停，已下载内容保留…'
                self.publish()
            elif action=='cancel':
                self.jobs.pop(id,None)
                self.publish()
                self.notice.emit('已取消继续安装，已下载文件保留，可从模型菜单清理。')
            return
        if action=='default':
            self._selection(mid)
            return
        self._gate(mid, destructive=action=='remove')
        if action=='remove':
            if id=='qwen_tts':
                raise ValueError('请分别移除不再需要的声音能力，共用文件会按使用情况保留。')
            original=(self.cfg.engine_name,getattr(self.cfg,'default_tts_model',''))
            selected=mid==original[0]
            fallback=next((model for model in available_local_models() if model.id!=mid and mm.is_ready(model.id)),None) if selected else None
            message='移除这个模型的本地文件。文稿、音色和生成结果会保留。'
            if selected:
                message+=(f'将先切换到 {fallback.display_name}。' if fallback else '移除后本地转录暂不可用，重新准备模型即可恢复。')
            elif mid==original[1]:
                message+='移除后请选用其他已就绪的配音模型。'
            if not dialogs.confirm(self.host,'移除模型',message,ok='移除'):
                return
            self._gate(mid,destructive=True)
            if original!=(self.cfg.engine_name,getattr(self.cfg,'default_tts_model','')):
                raise RuntimeError('模型选择刚有变化，移除已取消。请确认当前选择后重试。')
            if fallback:
                if not mm.is_ready(fallback.id):
                    raise RuntimeError('替代模型已不可用，移除已取消。')
                self.cfg.engine_name=fallback.id
                try:
                    if self.cfg.save() is False:
                        raise RuntimeError('无法保存替代模型，移除已取消。')
                except Exception:
                    self.cfg.engine_name=original[0]
                    raise
                self.on_changed()
            self._operation(ModelRemoveWorker(mid,tts=mid not in LOCAL_MODELS), id, mid)
            return
        if id=='qwen_tts':
            raise ValueError('请选择需要安装的声音能力。')
        if mid in LOCAL_MODELS:
            issue=mm.runtime_issue(mid)
            if issue:
                raise RuntimeError(issue)
            worker=DownloadWorker(mid)
        else:
            if rp.current_platform() is None:
                raise RuntimeError('当前电脑不支持此配音模型。')
            archive = None
            if not rp.TTS_RUNTIMES[mid].runtime_download_available and not rp.is_runtime_installed(mid):
                name,_ = QFileDialog.getOpenFileName(self.host,'选择已有的 Lulu 模型运行包','','运行包 (*.tar.gz *.tgz)')
                if not name:
                    return
                archive = Path(name)
            worker=TTSRuntimeInstallWorker(mid,local_archive=archive)
        self._gate(mid)
        self._operation(worker,id,mid)

    def feature_action(self, id, action):
        from ...core.synthesis import qwen3_deployment as dp
        if id not in FEATURE_IDS or action not in {'install','resume','pause','cancel','remove'}:
            raise ValueError('声音能力操作无效')
        name, key, mid = FEATURE_IDS[id], 'feature:'+id, 'local_qwen3_tts'
        active = self.active.get('qwen')
        if action in {'pause','cancel'} and active:
            if self.storage_busy() or self.relocation_busy() or self.closing:
                raise RuntimeError('模型位置正在变化，请稍后再试。')
            phase=dp.read_state()['features'].get(name,{}).get('state')
            if phase not in {'queued','installing','paused'}:
                raise ValueError('此功能当前没有可暂停或取消的安装')
            if action=='cancel':
                if not dialogs.confirm(self.host,'取消这项安装','清理这项未完成的安装，共用文件按其他功能的使用情况保留。',ok='取消安装'):
                    return
                dp.request([name],'cancelling')
                self.jobs.pop(key,None)
                self.refresh()
                return
            dp.request([name],'paused')
            self.jobs[key]=dict(status='paused',paused=True,progress=-1,message='已暂停，下载进度保留。')
            self.refresh()
            self.publish()
            return
        if active and action in {'install','resume'}:
            if self.storage_busy() or self.relocation_busy() or self.closing:
                raise RuntimeError('模型位置正在变化，请稍后再试。')
            dp.request([name])
            self.jobs[key]=dict(status='running',paused=False,progress=-1,message='等待安装…')
            self.publish()
            return
        self._gate(mid,destructive=action in {'remove','cancel'})
        operation='plan' if action in {'install','resume'} else 'removal_plan'
        if action=='pause':
            dp.request([name],'paused')
            self.refresh()
            return
        worker=QwenFeatureWorker([name] if operation=='plan' else name,operation=operation)
        def apply_quote():
            if self.closing:
                return
            if worker.error:
                self.notice.emit(worker.error)
                return
            value=worker.result
            if operation=='plan':
                text=f"本次需下载约 {mm_format(value['download'])}，新增占用约 {mm_format(value['disk'])}。\n已有完整文件会复用，共用组件只保留一份。"
                if not dialogs.confirm(self.host,'安装声音能力',text,ok='开始安装'):
                    return
                self._gate(mid)
                dp.request(value['names'])
                task=QwenFeatureWorker(operation='install')
            else:
                text='移除此项声音能力；个人音色、参考录音和生成结果保留。'
                text+='\n共用组件仍被其他能力使用，会继续保留。' if value['consumers'] else '\n共用组件保留，方便以后继续安装。'
                if not dialogs.confirm(self.host,'移除声音能力',text,ok='移除'):
                    return
                self._gate(mid,destructive=True)
                task=QwenFeatureWorker(name,operation='uninstall',retain_common=True)
            self._operation(task,'qwen',mid,qwen=True)
        def quoted():
            try:
                apply_quote()
            except (ValueError,RuntimeError,OSError) as exc:
                self.notice.emit(str(exc))
        self._start(worker,key='qwen-plan',mid=mid,finished=quoted)

    def storage_open(self,purpose):
        if purpose not in {'extract','voice'}:
            raise ValueError('请选择模型用途')
        path=mm.storage_dir() if purpose=='extract' else rp.tts_root()
        path.mkdir(parents=True,exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            raise RuntimeError('无法打开模型位置，请稍后重试。')

    def storage_change(self,purpose):
        if purpose not in {'extract','voice'}:
            raise ValueError('请选择模型用途')
        self._gate()
        for mid in MODEL_IDS.values():
            if self.model_in_use(mid):
                raise RuntimeError('请先结束模型任务并释放已加载的模型，再移动保存位置。')
        directory=QFileDialog.getExistingDirectory(self.host,'更改模型保存位置（已装模型会移过去）',str(mm.models_root()))
        if not directory or Path(directory)==mm.models_root():
            return
        if not dialogs.confirm(self.host,'移动已有模型',f'将转录和配音模型一起移动到：\n{directory}\n移动期间请保持 Lulu 打开。',ok='移动'):
            return
        self._gate()
        for mid in MODEL_IDS.values():
            if self.model_in_use(mid):
                raise RuntimeError('模型已开始运行，移动已取消。请结束任务后重试。')
        worker=ModelsRelocateWorker(Path(directory),self.cfg)
        self.storage_activity_changed(True)
        self._operation(worker,'storage','')
        worker.finished.connect(lambda:self.storage_activity_changed(False))

    def cloud_request(self, request_id, operation, args):
        if self.closing:
            raise RuntimeError('模型设置正在关闭')
        if operation in {'save_connection','load_saved_configuration','remove_credential'}:
            self._gate()
        def run():
            try:
                return dict(ok=True,value=self.cloud.execute(operation,args))
            except (ValueError, RuntimeError) as exc:
                return dict(ok=False,message=str(exc))
            except Exception:
                return dict(ok=False,message='连接操作未完成，请稍后重试。')
        worker=ReadWorker(run)
        worker.result.connect(lambda value,error:self.reply.emit(request_id,value or dict(ok=False,message=error)))
        self._start(worker,key=('cloud-save:' if operation in {'save_connection','load_saved_configuration','remove_credential'} else 'cloud:')+request_id,finished=lambda:(self.on_changed(),self.refresh()) if not self.closing else None)

    def shutdown_workers(self, timeout_ms=0):
        self.closing=True
        for worker in self.workers:
            if hasattr(worker,'request_cancel'):
                worker.request_cancel()
        deadline=time.monotonic()+max(0,timeout_ms)/1000
        for worker in self.workers:
            if worker.isRunning() and timeout_ms>0:
                worker.wait(max(0,int((deadline-time.monotonic())*1000)))
        return not any(w.isRunning() for w in self.workers)


def mm_format(value):
    from ...core.model_transfer import format_bytes
    return format_bytes(value)

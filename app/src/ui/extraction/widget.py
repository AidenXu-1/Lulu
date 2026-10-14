"""Narrow native bridge: the page cannot supply paths or execute arbitrary commands."""
import json
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFileDialog
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from ...core.task import TaskState, TaskExecutionPolicy
from ...core.collect.preferences import MaterialPreferenceSnapshot
from ...core.extraction import outputs, has_unsaved_result
from ..agent_workspace.runtime import prepare_webengine_runtime

ROOT = Path(__file__).resolve().parent
EDITABLE = {TaskState.PENDING, TaskState.FAILED, TaskState.CANCELLED}

def needs_result_save(task):
    return has_unsaved_result(task) and (task.message=='结果待保存'
                                       or getattr(task,'_core_recovered_result',False))

def has_document(task):
    if task.library_id:
        return True
    if task.state is not TaskState.DONE:
        return False
    reference = getattr(task, '_core_result_ref', None)
    return bool((task.transcript and task.transcript.text.strip()) or
                (isinstance(reference, str) and reference.startswith('lulu-result:')))

class Resources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, request):
        url = request.requestUrl()
        allowed = url.scheme() == 'qrc' and url.path() == '/qtwebchannel/qwebchannel.js'
        if url.scheme() == 'file':
            path = Path(url.toLocalFile()).resolve()
            allowed = path == ROOT/'index.html' or path.is_relative_to(ROOT/'assets')
        request.block(not allowed)

class Page(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, main):
        return main and url == QUrl.fromLocalFile(str(ROOT/'index.html'))
    def createWindow(self, kind): return None

class View(QWebEngineView):
    filesDropped = Signal(list)
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
        else: event.ignore()
    def dragMoveEvent(self, event): self.dragEnterEvent(event)
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls and all(u.isLocalFile() for u in urls):
            self.filesDropped.emit([u.toLocalFile() for u in urls]);event.acceptProposedAction()
        else: event.ignore()

class Bridge(QObject):
    snapshot = Signal(str)
    reply = Signal(str, str)
    notice = Signal(str)
    def __init__(self, widget): super().__init__(widget);self.widget=widget
    @Slot()
    def ready(self): self.widget.ready=True;self.widget.publish(force=True)
    @Slot(str,str,str)
    def request(self, request_id, action, raw):
        if self.widget.closing: return
        try:
            if len(raw)>65536 or len(request_id)>32: raise ValueError('操作内容过大')
            args=json.loads(raw)
            if type(args) is not dict: raise ValueError('操作参数无效')
            value=self.widget.dispatch(action,args)
            self.reply.emit(request_id,json.dumps(dict(ok=True,value=value),ensure_ascii=False))
        except Exception as exc:
            self.reply.emit(request_id,json.dumps(dict(ok=False,message=str(exc) if isinstance(exc,(ValueError,OSError,RuntimeError)) else '操作未完成，请重试'),ensure_ascii=False))
        self.widget.publish(force=True)

class ExtractionWorkspace(QWidget):
    def __init__(self, main):
        super().__init__(main)
        self.main=main;self.ready=False;self.closing=False;self._last='';self._ids={};self._next_id=1
        self._started={};self._elapsed={};self._deleting={};self._models=[]
        self._preferences={}
        self._models_at=0
        prepare_webengine_runtime()
        self.view=View(self);self.profile=QWebEngineProfile(self)
        self.interceptor=Resources(self.profile);self.profile.setUrlRequestInterceptor(self.interceptor)
        # Let the native canvas paint one continuous paper surface under the titlebar.
        self.page=Page(self.profile,self.view);self.page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,False)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,False)
        self.view.setPage(self.page);self.channel=QWebChannel(self.page);self.bridge=Bridge(self)
        self.channel.registerObject('extraction',self.bridge);self.page.setWebChannel(self.channel)
        self.view.filesDropped.connect(self.add_files)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.addWidget(self.view)
        self.timer=QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self.publish);self.timer.start()
        self.view.load(QUrl.fromLocalFile(str(ROOT/'index.html')))

    def shutdown(self):
        # Closing ends the undo window. Only remove settled queue records;
        # never delete their source files, results or a running worker here.
        for tid in tuple(self._deleting):
            task = self.main._task(tid)
            if task and not task.is_active:
                self.main._delete_task(tid)
        self._deleting.clear()
        self.closing=True;self.timer.stop();self.page.setWebChannel(None)

    def initial_policy(self, source, kind):
        cfg=self.main.cfg
        chosen=('text',) if kind!='link' else ('text',)+(('video',) if cfg.keep_media and cfg.link_media_mode=='video' else ())+ (('cover',) if self.main._material_snapshot().save_cover else ())
        return TaskExecutionPolicy(origin='single_link',mode='materialize',stable_key='',
            material_snapshot=MaterialPreferenceSnapshot('video' if 'video' in chosen else 'audio','video' in chosen,'cover' in chosen),
            media_destination='user_directory' if kind=='link' else 'none',media_directory=cfg.media_dir,
            prefer_subtitles=bool(cfg.prefer_subtitles),engine_name=None,requested_outputs=chosen)

    def add_files(self, paths):
        if len(paths)>500: raise ValueError('一次最多加入 500 个文件')
        valid=[Path(path) for path in paths if Path(path).is_file() and Path(path).suffix.lower() in {'.mp3','.wav','.m4a','.aac','.flac','.ogg','.opus','.aiff','.mp4','.mov','.mkv','.webm'}]
        added=[self.main._add_task(str(p),'file',p.name,defer_queue_refresh=True) for p in valid]
        if len(added)>1:
            from ...core.task import TaskGroup, TaskGroupMember
            group=TaskGroup(id=max([g.id for g in self.main.task_groups],default=0)+1,
                name='本地文件',members=[TaskGroupMember(stable_key='',title=t.title,source=t.source,kind=t.kind,task_id=t.id) for t in added])
            self.main.task_groups.append(group)
            self.main._next_group_id=group.id+1
        if added:
            self.main._refresh_badge();self.main._persist_task_queue()
        self.publish(force=True)

    def rows(self):
        result=[];groups={m.task_id:g for g in self.main.task_groups for m in g.members}
        now=time.monotonic()
        for task in self.main.tasks:
            if task.id in self._deleting: continue
            if task.id not in self._ids:self._ids[task.id]=self._next_id;self._next_id+=1
            display_state=task.state
            if (display_state==TaskState.RUNNING and getattr(task,'_core_recovered_queue',False)
                    and getattr(self.main,'current',None) is None
                    and task.id==getattr(self.main,'_core_starting_task_id',None)):
                # No worker has started; show Continue while the Core outcome
                # is unknown, without altering the authoritative running claim.
                display_state=TaskState.QUEUED
            if display_state==TaskState.RUNNING:
                self._started.setdefault(task.id,now)
                self._elapsed[task.id]=int(now-self._started[task.id])
            group=groups.get(task.id);policy=task.execution_policy;tr=task.transcript;meta=(tr.meta or {}) if tr else {}
            selected=outputs(policy)
            if policy and policy.requested_outputs is None:
                selected=('text',)+(('video',) if policy.material_snapshot.save_media and policy.material_snapshot.download_mode=='video' else ())+ (('cover',) if policy.material_snapshot.save_cover else ())
            from ...core.audio.extractor import extract_url
            url=extract_url(task.source) if task.kind=='link' else ''
            from urllib.parse import urlsplit
            host=urlsplit(url).hostname or '' if url else ''
            platform=next((p for domains,p in [(('douyin','iesdouyin'),'douyin'),(('bilibili','b23.tv'),'bilibili'),(('xiaohongshu','xhslink'),'xiaohongshu'),(('youtube','youtu.be'),'youtube'),(('xiaoyuzhou','xyzcdn'),'xiaoyuzhou')] if any(d in host for d in domains)),'other' if task.kind=='link' else 'local')
            duration=meta.get('duration')
            result.append(dict(id=self._ids[task.id],name=task.title,kind=task.kind,platform=platform,
                displayOrder=getattr(task,'_display_created_at',0) if hasattr(task,'_core_job_revision') else 1e15+self._ids[task.id],
                animateNew=bool(getattr(task,'_display_new',False) or not hasattr(task,'_core_job_revision')),
                duration=f'{int(duration)//60:02}:{int(duration)%60:02}' if isinstance(duration,(int,float)) and duration>=0 else '待识别',
                state={TaskState.PENDING:'waiting',TaskState.QUEUED:'queued',TaskState.RUNNING:'running',TaskState.DONE:'done',TaskState.FAILED:'error',TaskState.CANCELLED:'cancelled'}[display_state],
                outputs={k:k in selected for k in ('text','video','cover')},subs=bool(policy.prefer_subtitles) if policy else False,
                folder=(policy.media_directory if policy else None) or self.main.cfg.media_dir,
                group=str(group.id) if group else None,groupName=group.name if group else '',
                skipped=[dict(name=m.title,reason=m.skipped_reason) for m in group.members if m.is_skipped] if group else [],
                progress=task.progress,message=task.message,error=task.error,stage=getattr(task,'_execution_stage',''),
                model=policy.engine_name if policy else self.main.cfg.engine_name,elapsed=self._elapsed.get(task.id,0),
                readOnly=bool(getattr(task,'_core_view_only',False)),editableOutputs=task.kind=='link' and (policy is None or policy.origin=='single_link'),recovered=bool(getattr(task,'_core_recovered_queue',False)),
                savePending=needs_result_save(task),
                documentId=task.library_id,hasDocument=has_document(task),assets=meta.get('extraction_assets',{}),hasResult=bool(has_document(task) or meta.get('extraction_assets'))))
        return result

    def publish(self, force=False):
        if not self.ready or self.closing:return
        for tid in tuple(self._deleting):
            task=self.main._task(tid)
            if time.monotonic() >= self._deleting[tid] and (task is None or task.state not in {TaskState.RUNNING,TaskState.QUEUED}):
                self._deleting.pop(tid,None)
                if task:self.main._delete_task(tid)
        if not self._models or time.monotonic()-self._models_at>15:
            from ...core.engines.providers import PRESETS
            from ...core.models_manager import is_ready
            self._models=[dict(id=p.id,name=p.display_name.removeprefix('本地 · '),ready=is_ready(p.id) if p.kind=='local' else False,
                cloud=p.requires_network,detail='云端转录 · 音频会上传到服务商' if p.requires_network else '本地转录') for p in PRESETS.values()]
            self._models_at=time.monotonic()
        main=self.main
        recorder=getattr(main,'_recorder_obj',None)
        active_record=main._rec_elapsed_timer.isActive() or bool(recorder and recorder.is_recording()) or bool(getattr(main,'_mixed_pending_stop',False))
        task_ids={t.id for t in main.tasks}
        skipped_groups=[dict(id=str(g.id),name=g.name,
            members=[dict(name=m.title,reason=m.skipped_reason) for m in g.members if m.is_skipped])
            for g in main.task_groups if any(m.is_skipped for m in g.members)
            and not any(m.task_id in task_ids for m in g.members)]
        raw=json.dumps(dict(tasks=self.rows(),models=self._models,model=main.cfg.engine_name,
            startupError=getattr(main,'_install_preparation_error',''),
            queueSyncError=bool(getattr(main,'_task_queue_store_failed',False)),
            queueSyncPending=bool(getattr(main,'_task_queue_sync_retry_pending',False)),
            skippedGroups=skipped_groups,
            recording=active_record,expanded=[str(g.id) for g in main.task_groups if g.expanded],
            source={'microphone':'麦克风','system_audio':'系统声音','mixed':'混录'}[main._rec_source],
            recordFolder=main.cfg.recording_dir,recSeconds=main._rec_elapsed),ensure_ascii=False)
        if raw!=self._last or force:self._last=raw;self.bridge.snapshot.emit(raw)

    def selected(self, ids, *, editable=False, allow_readonly=False):
        if type(ids) is not list or len(ids)>5000 or any(type(i) is not int for i in ids):raise ValueError('任务选择无效')
        found=[t for t in self.main.tasks if self._ids.get(t.id) in ids]
        if len(found)!=len(set(ids)):raise ValueError('任务已变化，请重新选择')
        if not allow_readonly and any(getattr(t,'_core_view_only',False) for t in found):raise ValueError('此任务由其他入口管理')
        if editable and any(t.state not in EDITABLE for t in found):raise ValueError('任务已开始，设置保持不变')
        if editable and any(needs_result_save(t) for t in found):raise ValueError('请先保存已经生成的结果，再修改提取选项')
        return found

    def dispatch(self, action, args):
        main=self.main
        schemas={'files':set(),'link':{'text'},'choice':{'ids','key','value'},'folder':{'kind','ids'},
            'sync':set(),
            'start':{'ids'},'cancel':{'ids'},'delete':{'ids'},'expanded':{'id','expanded'},
            'undo_delete':set(),'record_source':{'value'},'record':set(),'record_cancel':set(),'model':{'id'},
            'models':set(),'result':{'id','action'},'clear_skipped':{'id'}}
        if action not in schemas or set(args)!=schemas[action]:raise ValueError('界面操作无效')
        if action=='sync': main._retry_task_queue_sync()
        elif action=='undo_delete': self._deleting.clear()
        elif action=='clear_skipped':
            group=next((g for g in main.task_groups if str(g.id)==args['id']),None)
            if group is None or any(m.task_id==t.id for m in group.members for t in main.tasks):raise ValueError('任务组已变化，请重新选择')
            main._delete_task_group(group.id)
        elif action=='files':main._pick_file()
        elif action=='link':
            from ...core.audio.extractor import extract_url
            if type(args['text']) is not str or len(args['text'])>16384 or not extract_url(args['text']):raise ValueError('请粘贴有效的作品链接')
            main._grab_link(args['text'])
        elif action=='choice':
            key,value=args['key'],args['value']
            if key not in {'text','video','cover','subs'} or type(value) is not bool:raise ValueError('提取选项无效')
            for task in self.selected(args['ids'],editable=True):
                if task.kind!='link' or (task.execution_policy and task.execution_policy.origin!='single_link'):continue
                policy=task.execution_policy or self.initial_policy(task.source,task.kind)
                chosen=set(outputs(policy))
                if key!='subs':chosen.add(key) if value else chosen.discard(key)
                task.execution_policy=replace(policy,requested_outputs=tuple(k for k in ('text','video','cover') if k in chosen),
                    engine_name=None,prefer_subtitles=value if key=='subs' else policy.prefer_subtitles,
                    material_snapshot=MaterialPreferenceSnapshot('video' if 'video' in chosen else 'audio','video' in chosen,'cover' in chosen))
                if task.state in {TaskState.FAILED,TaskState.CANCELLED}:task.state=TaskState.PENDING
            main._persist_task_queue()
        elif action=='folder':
            if args['kind']=='record':main._pick_recording_dir()
            elif args['kind']=='task':
                selected=self.selected(args['ids'],editable=True)
                if not selected:return
                path=QFileDialog.getExistingDirectory(self,'选择视频与封面的保存位置',(selected[0].execution_policy.media_directory if selected[0].execution_policy else None) or main.cfg.media_dir)
                if path:
                    # Recheck after the nested native picker event loop.
                    for task in self.selected(args['ids'],editable=True):task.execution_policy=replace(task.execution_policy or self.initial_policy(task.source,task.kind),media_directory=path)
                    main._persist_task_queue()
            else:raise ValueError('保存位置类型无效')
        elif action=='start':
            if getattr(main,'_install_preparation_error',''):
                raise RuntimeError(main._install_preparation_error)
            for task in self.selected(args['ids']):
                if not outputs(task.execution_policy):continue
                if task.state in {TaskState.FAILED,TaskState.CANCELLED}:
                    if needs_result_save(task):
                        if task.execution_policy and task.execution_policy.requested_outputs is not None:
                            main._commit_extraction_result(task,task.transcript)
                        else:
                            main._retry_result_save(task.id)
                        continue
                    if task.execution_policy and task.execution_policy.requested_outputs is not None:
                        task.execution_policy=replace(task.execution_policy,engine_name=None)
                    task.transcript=None
                    task._extraction_result_ref=None
                    task.state=TaskState.PENDING;task.error=None
                pending_start=(getattr(main,'_task_queue_store_failed',False)
                    and getattr(main,'current',None) is None
                    and task.id==getattr(main,'_core_starting_task_id',None))
                if task.state not in {TaskState.PENDING,TaskState.QUEUED} and not pending_start:continue
                main._paused_task_group_ids.discard(main._task_group_id_for_task(task.id))
                main._start_task(task.id)
        elif action in {'cancel','delete'}:
            selected=self.selected(args['ids'],allow_readonly=action=='delete')
            if any(getattr(t,'_core_view_only',False) and t.state not in {TaskState.DONE,TaskState.FAILED,TaskState.CANCELLED} for t in selected):
                raise ValueError('任务仍在其他入口处理中，结束后可以移除卡片')
            for task in selected:
                if action=='cancel':main._cancel_task(task.id)
                else:
                    self._deleting[task.id]=time.monotonic()+6
                    if task.is_active:main._cancel_task(task.id)
            if action=='delete': return {'undo':True}
        elif action=='expanded':
            task=self.selected([args['id']],allow_readonly=True)[0]
            for group in main.task_groups:
                if any(m.task_id==task.id for m in group.members):group.expanded=bool(args['expanded'])
            main._persist_task_queue()
        elif action=='record_source':
            values={'麦克风':'microphone','系统声音':'system_audio','混录':'mixed'}
            if args['value'] not in values or main._rec_elapsed_timer.isActive() or bool(getattr(main,'_recorder_obj',None) and main._recorder_obj.is_recording()):raise ValueError('录音时不能更改来源')
            main._on_rec_source_selected(values[args['value']])
        elif action=='record':main._toggle_record()
        elif action=='record_cancel':main._cancel_recording()
        elif action=='model':
            item=next((m for m in self._models if m['id']==args['id']),None)
            if item is None:raise ValueError('模型不存在')
            if not item['cloud'] and not item['ready']:
                main._switch_page(1);return
            if item['cloud']:
                from .. import dialogs
                if not dialogs.confirm(main,'使用云端转录','音频会上传到所选服务商。是否使用此模型？'):return
            main.cfg.engine_name=args['id'];main.cfg.save()
        elif action=='models':main._switch_page(1)
        elif action=='result':
            task=self.selected([args['id']],allow_readonly=True)[0];kind=args['action']
            if kind == 'retry_missing':
                if getattr(task,'_core_view_only',False):raise ValueError('历史任务仅供查看，请重新加入任务')
                if task.state is not TaskState.DONE or not task.error: raise ValueError('此任务不需要重试')
                assets=(task.transcript.meta or {}).get('extraction_assets',{}) if task.transcript else {}
                missing=tuple(k for k in outputs(task.execution_policy) if not (task.library_id if k=='text' else assets.get(k)))
                if not missing: return
                policy=replace(task.execution_policy,requested_outputs=missing,engine_name=None,
                    material_snapshot=MaterialPreferenceSnapshot('video' if 'video' in missing else 'audio','video' in missing,'cover' in missing))
                main._add_task(task.source,task.kind,task.title,execution_policy=policy)
            elif kind in {'video','cover'}:
                path=(task.transcript.meta or {}).get('extraction_assets',{}).get(kind) if task.transcript else None
                if not path or not Path(path).is_file():raise FileNotFoundError('素材文件已移动或不存在')
                main._reveal(path)
            else:
                routes={'view':main._view_task,'copy':main._copy_task,'export':main._export_task,
                    'obsidian':main._obsidian_task,'feishu':main._feishu_task,'voice':main._voice_task}
                if kind not in routes or not has_document(task):raise ValueError('文稿暂时不可用')
                if main._ensure_task_transcript(task, routes[kind]):
                    routes[kind](task.id)

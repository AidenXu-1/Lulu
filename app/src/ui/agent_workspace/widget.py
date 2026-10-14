"""Qt shell adapter around the local Markdown workspace and closed Core API."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QWidget, QStackedLayout, QLabel, QFileDialog
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor, QWebEngineSettings
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ...core.agent_workspace import client
from ...core.agent_workspace.service import validate_request
from ...core.agent_workspace.store import identity
from .runtime import prepare_webengine_runtime
from .context_menu import WorkspaceView, open_external
from ..audio_waveform import AudioWaveform

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT.parent / 'assets'


def timing_sample(stage, elapsed_ms):
    # Explicit local diagnosis only. No paths, document identifiers, contents or
    # credentials; no disk writes on the UI thread and no remote collection.
    if os.environ.get('LULU_WORKSPACE_TIMING') == '1' and 0 <= elapsed_ms < 300000:
        print('LULU_WORKSPACE_TIMING ' + json.dumps(dict(stage=stage, ms=round(elapsed_ms, 2))), flush=True)


def audio_file_action(action, *, document_id, path):
    """Native picker paths only; reuse Core's existing index and atomic copy logic."""
    from ...core import library
    if action == 'audio_relink':
        library.relink_voice(document_id, path)
        return {'changed': True, 'document_id': document_id}
    if action != 'audio_export':
        raise ValueError('音频文件操作无效')
    voice = library.load_voice(document_id)
    if not voice:
        raise FileNotFoundError('音频资料不存在')
    destination = Path(path)
    # An existing export remains intact; Core publishes a distinct completed copy.
    saved = library.download_audio(voice['audio_path'], destination.parent, destination.name)
    return {'saved': True, 'filename': saved.name, 'document_id': document_id}


class LocalResources(QWebEngineUrlRequestInterceptor):
    @staticmethod
    def _is_managed_cover(url):
        """Allow only Lulu-verified cover files inside the local editor."""
        if not url.isLocalFile() or url.hasQuery() or url.hasFragment():
            return False
        try:
            from ...core.collect.export import resolve_managed_cover_file
            from ...core.models_manager import app_data_dir

            root = Path(os.path.abspath(app_data_dir())) / 'collection' / 'covers'
            requested = Path(url.toLocalFile())
            relative = requested.relative_to(root)
            if len(relative.parts) != 2:
                return False
            verified = resolve_managed_cover_file(relative.as_posix())
            return verified is not None and verified == requested
        except (OSError, RuntimeError, ValueError):
            return False

    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = url.scheme() == 'qrc' and url.path() == '/qtwebchannel/qwebchannel.js'
        if url.scheme() == 'file':
            path = Path(url.toLocalFile()).resolve()
            allowed = (path == ROOT/'index.html' or path.is_relative_to(ROOT/'assets') or
                       path in {ASSETS/'obsidian.png', ASSETS/'feishu.png', ASSETS/'paper_texture.png'} or
                       self._is_managed_cover(url))
        info.block(not allowed)


class WorkspacePage(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, is_main_frame):
        return is_main_frame and url == QUrl.fromLocalFile(str(ROOT/'index.html'))

    def createWindow(self, kind):
        return None


class Results(QObject):
    done = Signal(str, str)


class Work(QRunnable):
    def __init__(self, request_id, operation, arguments, signals, dispatch):
        super().__init__()
        self.request_id, self.operation, self.arguments, self.signals, self.dispatch = request_id, operation, arguments, signals, dispatch

    def run(self):
        started = time.perf_counter()
        try:
            value = self.dispatch(self.operation, **self.arguments)
            result = dict(ok=True, value=value)
        except Exception as exc:
            result = dict(ok=False, code=type(exc).__name__, message=str(exc) if isinstance(exc,(ValueError,RuntimeError,OSError)) else '操作未完成，原资料已保留')
        timing_sample('rpc:' + self.operation, (time.perf_counter()-started)*1000)
        self.signals.done.emit(self.request_id, json.dumps(result, ensure_ascii=False))


class Bridge(QObject):
    reply = Signal(str, str)
    command = Signal(str, str)

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget

    @Slot(str, float)
    def performanceSample(self, stage, elapsed_ms):
        if stage in {'document_switch', 'selection'}:
            timing_sample(stage, elapsed_ms)

    @Slot(str, str, str)
    def request(self, request_id, operation, payload):
        if self.widget.closing:
            self.respond(request_id, False, code="RuntimeError", message="工作区正在关闭")
            return
        try:
            identity(request_id)
            if len(payload)>10*1024*1024:
                raise ValueError('请求内容过大')
            arguments=json.loads(payload)
            validate_request(operation,arguments)
            if self.widget.preparation_pending:
                self.widget.preparation_requests.append((request_id, operation, payload))
                return
            if self.widget.preparation_error:
                raise RuntimeError(self.widget.preparation_error)
            self.widget.pool.start(Work(request_id,operation,arguments,self.widget.results,self.widget.dispatch))
        except Exception as exc:
            self.respond(request_id,False,message=str(exc),code=type(exc).__name__)

    def respond(self, request_id, ok, value=None, **error):
        self.reply.emit(request_id,json.dumps(dict(ok=ok,value=value,**error),ensure_ascii=False))

    @Slot()
    def ready(self):
        if self.widget.closing:
            return
        self.widget.ready=True
        for action,values in self.widget.waiting:
            self.command.emit(action,json.dumps(values,ensure_ascii=False))
        self.widget.waiting.clear()
        self.widget.loaded.emit()

    @Slot(bool)
    def initialized(self, success):
        self.widget.initialization_succeeded = success

    @Slot(bool)
    def dirtyChanged(self, dirty):
        self.widget.dirty=dirty

    @Slot(int, int)
    def libraryCounts(self, documents, voice):
        if documents >= 0 and voice >= 0:
            self.widget.library_counts_changed.emit(documents, voice)

    @Slot(bool)
    def closeReady(self, saved):
        callback=self.widget.close_callback
        self.widget.close_callback=None
        if saved and callback:
            callback()

    @Slot(str,str,str)
    def nativeAction(self, request_id, action, payload):
        if self.widget.closing:
            self.respond(request_id, False, code="RuntimeError", message="工作区正在关闭")
            return
        try:
            args=json.loads(payload)
            if action not in {'export','obsidian','feishu','batch_export','batch_obsidian','batch_feishu','open_url','voice','import','models','model_purpose','library_page','audio_toggle','audio_seek','audio_stop','audio_status','audio_waveform','audio_relink','audio_export','tool_handoff','choose_media','copy_text','open_skills','skill_settings'} or type(args) is not dict:
                raise ValueError('界面操作无效')
            if action in {'batch_export', 'batch_obsidian', 'batch_feishu'}:
                from .batch import document_ids, deliver
                if set(args) != {'document_ids'}:
                    raise ValueError('请选择要处理的文稿')
                ids = document_ids(args['document_ids'])
                if self.widget.batch_request:
                    raise ValueError('上一项批量操作尚未结束')
                folder = None
                if action == 'batch_export':
                    folder = QFileDialog.getExistingDirectory(self.widget, '批量导出 Markdown 到文件夹',
                        str(getattr(self.widget.cfg, 'last_output_dir', '') or ''))
                elif action == 'batch_obsidian':
                    if not self.widget.main:
                        raise ValueError('请从 Lulu 主窗口存入 Obsidian')
                    folder = self.widget.main._ensure_obsidian_dir()
                elif not self.widget.main:
                    raise ValueError('请从 Lulu 主窗口存入飞书')
                if action != 'batch_feishu' and not folder:
                    self.respond(request_id, True, {'cancelled': True})
                    return
                if action == 'batch_feishu':
                    active = getattr(self.widget.main, '_lulu_feishu_storage_worker', None)
                    if active is not None and active.isRunning():
                        raise ValueError('上一份飞书存储还在进行，请稍候')
                self.widget.batch_request = (request_id, action)
                self.widget.pool.start(Work(request_id, action, dict(document_ids=ids, folder=folder),
                                            self.widget.batch_results, deliver))
                return
            if action in {'audio_relink', 'audio_export'}:
                if set(args) != {'document_id'}:
                    raise ValueError('请选择当前音频')
                eid = identity(args['document_id'])
                if action == 'audio_relink':
                    path, _ = QFileDialog.getOpenFileName(self.widget, '重新定位音频文件', '',
                        '音频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.aiff)')
                else:
                    from ...core import library
                    voice = library.load_voice(eid)
                    if not voice or voice['missing_flags']['audio_missing']:
                        raise FileNotFoundError('音频文件不在原位置，请先重新定位')
                    path, _ = QFileDialog.getSaveFileName(self.widget, '导出音频（同名文件会保留两份）',
                        str(Path(getattr(self.widget.cfg, 'tts_output_dir', '') or Path.home() / 'Downloads').expanduser()
                            / Path(voice.get('audio_filename') or '音频.wav').name), '音频文件 (*.*)',
                        options=QFileDialog.DontConfirmOverwrite)
                if not path:
                    self.respond(request_id, True, None)
                    return
                if action == 'audio_relink' and self.widget.playing_id == eid:
                    self.widget.player.stop()
                    self.widget.player.setSource(QUrl())
                    self.widget.playing_id = None
                self.widget.pool.start(Work(request_id, action,
                    dict(document_id=eid, path=path), self.widget.results, audio_file_action))
                return
            if action == 'choose_media':
                if set(args) != {'conversation_id'}:
                    raise ValueError('请选择当前会话')
                identity(args['conversation_id'])
                path, _ = QFileDialog.getOpenFileName(self.widget, '选择要转录的本地音视频', '',
                    '音视频 (*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.aiff *.mp4 *.mov *.mkv *.webm)')
                if not path:
                    self.respond(request_id, True, None)
                    return
                options = dict(conversation_id=args['conversation_id'], request_id=uuid4().hex,
                               source_path=str(Path(path).resolve()), engine='local_sensevoice')
                # File hashing and readiness checks run off the Qt thread.
                self.widget.pool.start(Work(request_id, 'prepare_media', options, self.widget.results, self.widget.dispatch))
                return
            if action=='tool_handoff':
                validate_request('tool_handoff',args)
                self.widget.pool.start(Work(request_id,'tool_handoff',args,self.widget.handoff_results,self.widget.dispatch))
                return
            value=self.widget.native(action,args)
            self.respond(request_id,True,value)
        except Exception as exc:
            self.respond(request_id,False,message=str(exc),code=type(exc).__name__)


class AgentWorkspace(QWidget):
    loaded=Signal()
    library_counts_changed=Signal(int, int)

    def __init__(self, cfg=None, main=None, *, dispatch=None):
        super().__init__(main)
        self.cfg,self.main,self.dispatch=cfg,main,dispatch or client.dispatch
        self._kind='transcript'
        self.dirty,self.ready=False,False
        self.initialization_succeeded = False
        self.closing=False
        self.waiting=[]
        self.preparation_pending = bool(getattr(main, '_install_preparation_pending', False))
        self.preparation_error = getattr(main, '_install_preparation_error', '')
        self.preparation_requests = []
        self.close_callback=None
        self.pool=QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.results=Results(self)
        self.handoff_results=Results(self)
        self.batch_results=Results(self)
        self.batch_request=None
        self.batch_feishu_worker=None
        self.bridge=Bridge(self)
        self.results.done.connect(self.bridge.reply)
        self.handoff_results.done.connect(self.finish_handoff)
        self.batch_results.done.connect(self.finish_batch)
        prepare_webengine_runtime()
        self.profile=QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.NoCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
        self.interceptor=LocalResources(self.profile)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.view=WorkspaceView(self)
        self.page=WorkspacePage(self.profile,self.view)
        # A profile must outlive its page's WebEngine adapter. QObject destroys
        # page children after the page destructor, including this private profile.
        self.profile.setParent(self.page)
        self.page.setBackgroundColor(QColor('#fbf8f1'))
        self.view.setPage(self.page)
        settings=self.page.settings()
        for flag in (QWebEngineSettings.LocalContentCanAccessRemoteUrls,QWebEngineSettings.LocalContentCanAccessFileUrls,
                     QWebEngineSettings.JavascriptCanOpenWindows,QWebEngineSettings.PluginsEnabled):
            settings.setAttribute(flag,False)
        # Local JS/CSS are served alongside the document; the interceptor closes
        # file access to the built assets and the two trusted brand images only.
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls,True)
        self.channel=QWebChannel(self.page)
        self.channel.registerObject('workspace',self.bridge)
        self.page.setWebChannel(self.channel)
        self.surface = QStackedLayout(self)
        self.surface.setContentsMargins(0, 0, 0, 0)
        self.surface.addWidget(self.view)
        self.loading = QLabel("正在准备素材库…", self)
        self.loading.setAlignment(Qt.AlignCenter)
        self.loading.setAccessibleName("素材库准备状态")
        self.surface.addWidget(self.loading)
        self.surface.setCurrentWidget(self.loading)
        self.loaded.connect(lambda: self.surface.setCurrentWidget(self.view))
        self.player=QMediaPlayer(self);self.output=QAudioOutput(self);self.player.setAudioOutput(self.output)
        self.playing_id=None
        self.waveform_id = None
        self.waveform = AudioWaveform(self)
        self.waveform.changed.connect(self.waveform_state)
        self.player.positionChanged.connect(lambda _:self.audio_state())
        self.player.playbackStateChanged.connect(lambda _:self.audio_state())
        self.player.durationChanged.connect(lambda _:self.audio_state())
        self.player.mediaStatusChanged.connect(lambda _:self.audio_state())
        self.player.errorOccurred.connect(lambda *_:self.audio_state())
        self.view.setUrl(QUrl.fromLocalFile(str(ROOT/'index.html')))

    def call(self,action,*values):
        if self.closing:
            return
        if self.ready:
            self.bridge.command.emit(action,json.dumps(values,ensure_ascii=False))
        else:
            self.waiting.append((action,values))

    def showEvent(self, event):
        super().showEvent(event)
        self.call('visible', True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.call('visible', False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # The same layout scales only when the screen cannot fit the supported
        # compact workspace. Normal/default dimensions stay at 100 percent.
        self.view.setZoomFactor(min(1.0,max(.65,self.width()/1024)))

    def refresh(self,**kwargs):
        self.call('refresh')

    def finish_install_preparation(self, success, message=''):
        self.preparation_pending = False
        self.preparation_error = '' if success else message or '启动准备未完成，请重新打开 Lulu；原资料已保留。'
        requests, self.preparation_requests = self.preparation_requests, []
        for request in requests:
            self.bridge.request(*request)

    def _set_kind(self,kind):
        self._kind = kind if kind in {'voice', 'trash', 'models'} else 'transcript'
        self.call('setKind', self._kind)

    def open_entry(self,entry_id):
        self.call('open',entry_id)

    def flush_pending(self,callback):
        if not self.dirty:
            return True
        if self.close_callback is None:
            self.close_callback=callback
            self.call('prepareClose')
        return False

    def audio_state(self):
        self.bridge.command.emit('audioState',json.dumps(self.audio_state_value(self.playing_id)))

    def waveform_state(self):
        self.bridge.command.emit('waveformState', json.dumps(self.waveform_state_value()))

    def waveform_state_value(self):
        return dict(id=self.waveform_id, state=self.waveform.state,
                    peaks=self.waveform.peaks, description=self.waveform.description)

    def audio_state_value(self, eid):
        current = eid is not None and eid == self.playing_id
        failed = current and self.player.error() != QMediaPlayer.NoError
        return dict(id=eid, position=self.player.position() if current else 0,
                    duration=self.player.duration() if current else 0,
                    playing=current and self.player.playbackState()==QMediaPlayer.PlayingState,
                    loading=current and self.player.mediaStatus()==QMediaPlayer.LoadingMedia,
                    error='无法播放这个音频，请重新定位文件后重试。' if failed else '')

    @Slot(str,str)
    def finish_handoff(self, request_id, payload):
        response=json.loads(payload)
        if not response['ok']:
            self.bridge.reply.emit(request_id,payload)
            return
        try:
            if not self.main:
                raise RuntimeError('此操作需要 Lulu 主窗口；所需选择已保留在工具记录中')
            result=response['value']
            if result['action']=='voice':
                target=result['target']
                self.main.open_voice_studio(target['text'],source={
                    'source_type':'transcript','source_id':target['document_id'],'source_title':target['title'],
                    'source_text_snapshot':target['text'],'source_chain':[dict(
                        entry_id=target['document_id'],revision=target['revision'],
                        content_hash=target['content_hash'],reference_ids=target['reference_ids'])]})
            elif result['action']=='transcription':
                self.main._switch_page(0)
                self.main.link_input.setText(result['url'])
                self.main.link_input.setFocus()
                self.main._toast('已带入作品链接，请确认配置后开始' if result['url'] else '请选择要转录的文件，当前会话已保留')
            elif result['action']=='skills':
                self.main._switch_page(6)
                self.main.settings_page.set_category('skills')
                if result.get('entries'):
                    self.main.settings_page.skills_page.request_pending()
            elif result['action']=='collection':
                self.main._switch_page(5)
                self.main.collection_page.accept_workspace_request(result['url'],result['limit'])
            else:
                raise ValueError('工作台入口无效，未执行其他操作')
            self.bridge.respond(request_id,True,{'state':'needs_input','action':result['action']})
        except Exception as exc:
            self.bridge.respond(request_id,False,message=str(exc),code=type(exc).__name__)

    def finish_batch(self, request_id, payload):
        result = json.loads(payload)
        action = self.batch_request[1] if self.batch_request else ''
        if action != 'batch_feishu' or not result.get('ok'):
            self.batch_request = None
            self.bridge.reply.emit(request_id, payload)
            return
        value = result['value']
        try:
            from ...core.feishu_storage import WorkArchiveRecord
            from ..feishu_dialog import send_records_to_feishu
            if self.closing:
                raise ValueError('工作区正在关闭，未发送文稿')
            records = [WorkArchiveRecord(**r) for r in value.pop('records')]
            if not records:
                self.batch_request = None
                self.bridge.respond(request_id, True, value)
                return
            active = getattr(self.main, '_lulu_feishu_storage_worker', None)
            if active is not None and active.isRunning():
                raise ValueError('上一份飞书存储还在进行，本次文稿尚未发送，请稍后再试')
            worker = send_records_to_feishu(self.main, self.cfg, records,
                title=f'已选 {len(records)} 篇文稿', context='selection', toast=self.main._toast)
            if worker is None:
                self.batch_request = None
                self.bridge.respond(request_id, True, {'cancelled': True})
                return
            self.batch_feishu_worker = worker
            def done(ok, receipt):
                self.batch_request = None
                self.bridge.respond(request_id, True, dict(value,
                    external_complete=bool(ok and getattr(receipt, 'complete', False))))
            worker.done.connect(done)
            worker.finished.connect(lambda: setattr(self, 'batch_feishu_worker', None))
        except Exception as exc:
            self.batch_request = None
            self.bridge.respond(request_id, False, code=type(exc).__name__, message=str(exc))

    def native(self,action,args):
        from ...core import library
        if action == 'skill_settings':
            if args or not self.main:
                raise ValueError('请从 Lulu 主窗口打开技能设置')
            self.main._switch_page(6)
            self.main.settings_page.set_category('skills')
            return
        if action == 'open_skills':
            if args:
                raise ValueError('Skill 目录操作参数无效')
            from ...core.agent_workspace.skills import skill_root
            root = skill_root()
            if any(p.is_symlink() for p in [root,*root.parents]):
                raise ValueError('Skill 目录不能是符号链接')
            root.mkdir(parents=True,exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(root))):
                raise RuntimeError('无法打开 Skill 安装目录，请按显示的路径打开')
            return
        if action == 'open_url':
            if set(args) != {'url'} or type(args['url']) is not str or len(args['url']) > 16384:
                raise ValueError('链接无效')
            open_external(args['url'])
            return
        if action == 'copy_text':
            if set(args) != {'text'} or type(args['text']) is not str or not 0 < len(args['text']) <= 8000:
                raise ValueError('复制内容无效')
            QGuiApplication.clipboard().setText(args['text'])
            return
        if action == 'models':
            if self.main:
                self.main._open_workspace_models()
            else:
                raise RuntimeError('请从 Lulu 主窗口打开模型设置')
            return
        if action == 'model_purpose':
            index = args.get('index')
            if type(index) is not int or index not in (0, 1, 2):
                raise ValueError('模型用途无效')
            if self.main:
                self.main._switch_page(1)
                self.main.models_page.show_purpose(index)
            return
        if action == 'library_page':
            kind = args.get('kind')
            if kind not in {'transcript', 'voice', 'trash'}:
                raise ValueError('素材库页面无效')
            if self.main:
                self.main.open_library(kind)
            else:
                self._set_kind(kind)
            return
        if action=='import':
            names,_=QFileDialog.getOpenFileNames(self,'导入文稿','','文稿 (*.md *.markdown *.txt)')
            created=[]
            for name in names:
                path=Path(name)
                if path.stat().st_size>8*1024*1024:
                    raise ValueError('文件超过 8 MB，未截断导入')
                result=self.dispatch('create_document',request_id=uuid4().hex,title=path.stem,markdown=path.read_text(encoding='utf-8-sig'))
                created.append(result['document_id'])
            self.refresh();return created
        if action == 'audio_waveform' and args == {'document_id': None}:
            self.waveform_id = None
            self.waveform.load_file(None)
            return self.waveform_state_value()
        if action == 'audio_stop':
            ids = args.get('document_ids')
            if not isinstance(ids, list):
                raise ValueError('素材列表无效')
            ids = [identity(value) for value in ids]
            if self.playing_id in ids:
                self.player.stop()
                self.player.setSource(QUrl())
                self.playing_id = None
                self.audio_state()
            return None
        eid=identity(args.get('document_id'))
        if action == 'audio_status':
            return self.audio_state_value(eid)
        voice=library.load_voice(eid)
        if action == 'audio_waveform':
            if set(args) != {'document_id'} or not voice:
                raise ValueError('请选择音频库中的音频')
            self.waveform_id = eid
            self.waveform.load_file(voice.get('audio_path'))
            return self.waveform_state_value()
        if action in {'audio_toggle','audio_seek'}:
            if not voice:
                raise FileNotFoundError('音频资料不存在')
            path=Path(voice['audio_path'])
            if not path.is_file():
                raise FileNotFoundError('音频文件不在原位置，生成时的文字仍保留')
            if self.playing_id!=eid or self.player.error()!=QMediaPlayer.NoError:
                self.player.setSource(QUrl())
                self.playing_id=eid;self.player.setSource(QUrl.fromLocalFile(str(path.resolve())))
            if action=='audio_seek':
                fraction=args.get('fraction')
                if not isinstance(fraction,(float,int)) or not 0<=fraction<=1:
                    raise ValueError('播放进度无效')
                self.player.setPosition(round(self.player.duration()*fraction))
            elif self.player.playbackState()==QMediaPlayer.PlayingState:
                self.player.pause()
            else:
                self.player.play()
            return
        doc=library.document_snapshot(eid)
        transcript=library.load(eid)
        if doc is None or transcript is None:
            raise FileNotFoundError('文稿不存在')
        if action=='export':
            from ..library_page import LibraryViewer
            viewer=LibraryViewer(doc['title'],transcript,self,cfg=self.cfg)
            viewer._export();viewer.deleteLater();return
        if not self.main:
            raise RuntimeError('此操作需要 Lulu 主窗口')
        if action=='voice':
            target=self.dispatch('voice_target',document_id=eid,references=args.get('references',[]))
            self.main.open_voice_studio(target['text'],source={'source_type':'transcript','source_id':eid,
                'source_title':doc['title'],'source_chain':[dict(entry_id=eid,revision=target['revision'],
                    content_hash=target['content_hash'],reference_ids=target['reference_ids'])]});return
        if action=='obsidian':
            folder=self.main._ensure_obsidian_dir()
            if folder:
                from ...core import obsidian
                path=obsidian.save_note(transcript,folder,doc['title'],summary=library.get_summary(eid))
                self.main._toast('已存入 Obsidian：'+path.name)
            return
        if action=='feishu':
            self.main._send_to_feishu(transcript,doc['title'],summary=library.get_summary(eid),text_override=doc['text'])

    def shutdown(self, wait_ms=5000):
        if self.batch_request or (self.batch_feishu_worker is not None and self.batch_feishu_worker.isRunning()):
            return False
        self.closing=True
        self.waiting.clear()
        self.player.stop()
        self.waveform.shutdown()
        return self.pool.waitForDone(wait_ms)

"""Offline settings view; explicit user actions cross a closed native bridge."""
from __future__ import annotations
import json
from pathlib import Path
from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineUrlRequestInterceptor, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from ..agent_workspace.runtime import prepare_webengine_runtime
from .controller import ModelController, MODEL_IDS, FEATURE_IDS, ROOT

ASSETS = ROOT/'assets'


class Resources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url=info.requestUrl()
        allowed=url.scheme()=='qrc' and url.path()=='/qtwebchannel/qwebchannel.js'
        if url.scheme()=='file':
            path=Path(url.toLocalFile()).resolve()
            allowed=path.is_relative_to(ASSETS)
        info.block(not allowed)


class Page(QWebEnginePage):
    def acceptNavigationRequest(self,url,kind,is_main_frame):
        return is_main_frame and url==QUrl.fromLocalFile(str(ASSETS/'index.html'))

    def createWindow(self,kind):
        return None


class Bridge(QObject):
    snapshot=Signal(str)
    reply=Signal(str,str)
    command=Signal(str,str)

    def __init__(self,widget):
        super().__init__(widget)
        self.widget=widget
        self.inflight=set()

    @Slot()
    def ready(self):
        self.widget.ready=True
        self.widget.controller.publish()
        for command,args in self.widget.pending:
            self.command.emit(command,json.dumps(args,ensure_ascii=False))
        self.widget.pending.clear()

    @Slot(str,str,str)
    def request(self,request_id,operation,payload):
        try:
            if not isinstance(request_id,str) or not 0<len(request_id)<=160 or len(payload)>32768:
                raise ValueError('请求无效')
            if request_id in self.inflight or len(self.inflight)>=16:
                raise ValueError('请等待当前操作完成')
            args=json.loads(payload)
            if type(args) is not dict:
                raise ValueError('请求无效')
            c=self.widget.controller
            if c.closing:
                raise RuntimeError('模型设置正在关闭')
            if operation == 'local_action' and args.get('action') == 'default':
                self.inflight.add(request_id)
                c.selection_request(request_id, args)
                return
            if operation in {'test_connection','discover_models','save_connection','load_saved_configuration','remove_credential'}:
                self.inflight.add(request_id)
                c.cloud_request(request_id,operation,args)
                return
            if operation in {'local_action','feature_action'}:
                if set(args)!={'id','action'} or not all(isinstance(v,str) for v in args.values()):
                    raise ValueError('模型操作无效')
                getattr(c,operation)(**args)
            elif operation in {'storage_open','storage_change'}:
                if set(args)!={'purpose'}:
                    raise ValueError('存储操作无效')
                getattr(c,operation)(**args)
            elif operation in {'open_key_page', 'open_pricing_page'}:
                url=c.cloud.key_url(args, kind='billing' if operation=='open_pricing_page' else 'keys')
                if not url.startswith('https://') or not QDesktopServices.openUrl(QUrl(url)):
                    raise RuntimeError('无法打开服务商的密钥页面')
            elif operation=='refresh' and not args:
                c.refresh()
            else:
                raise ValueError('设置操作无效')
            self.respond(request_id,dict(ok=True,value=None))
        except (ValueError,RuntimeError) as exc:
            self.respond(request_id,dict(ok=False,message=str(exc)))
        except Exception:
            self.respond(request_id,dict(ok=False,message='操作未完成，原资料已保留。'))

    def respond(self,request_id,result):
        self.inflight.discard(request_id)
        self.reply.emit(request_id,json.dumps(result,ensure_ascii=False))


class ModelsPage(QWidget):
    def __init__(self,cfg,on_changed=None,*,storage_busy=None,model_in_use=None,relocation_busy=None,
                 storage_activity_changed=None,workspace_models=None):
        super().__init__()
        self.cfg=cfg
        self.ready=False
        self.pending=[]
        self.setObjectName('modelsWorkspaceV2')
        self.setProperty('consumerThemeScope','coreWorkspace')
        self.setProperty('workspacePage','models')
        self.controller=ModelController(cfg,self,on_changed=on_changed,storage_busy=storage_busy,
            model_in_use=model_in_use,relocation_busy=relocation_busy,storage_activity_changed=storage_activity_changed)
        prepare_webengine_runtime()
        self.view=QWebEngineView(self)
        self.profile=QWebEngineProfile(self)
        self.resources=Resources(self)
        self.profile.setUrlRequestInterceptor(self.resources)
        self.page=Page(self.profile,self)
        # Match the manuscript workspace: release the page adapter before its profile.
        self.profile.setParent(self.page)
        # The shared native canvas owns the same paper background as the workbench.
        self.page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        settings=self.page.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls,False)
        settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows,False)
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled,False)
        self.bridge=Bridge(self)
        self.channel=QWebChannel(self.page)
        self.channel.registerObject('models',self.bridge)
        self.page.setWebChannel(self.channel)
        self.view.setPage(self.page)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.view)
        self.controller.changed.connect(lambda state:self.bridge.snapshot.emit(json.dumps(state,ensure_ascii=False)) if self.ready else None)
        self.controller.notice.connect(lambda message:self.send('notice',dict(message=message)))
        self.controller.reply.connect(self.bridge.respond)
        self.view.setUrl(QUrl.fromLocalFile(str(ASSETS/'index.html')))
        self.refresh()

    def send(self,action,args):
        if self.ready:
            self.bridge.command.emit(action,json.dumps(args,ensure_ascii=False))
        else:
            self.pending.append((action,args))

    def show_purpose(self,idx,sub=None):
        if idx not in (0,1,2):
            return
        args=dict(purpose=('extract','voice','assistant')[idx])
        if idx==0 and sub is not None:
            args['localMode']='cloud' if sub==1 else 'local'
        self.send('purpose',args)

    def show_model(self,model_id,feature=None):
        from ...core.engines.local_models import LOCAL_MODELS
        display=next((id for id,mid in MODEL_IDS.items() if mid==model_id),None)
        if not display:
            raise ValueError('模型不存在')
        self.send('purpose',dict(purpose='extract' if model_id in LOCAL_MODELS else 'voice',localMode='local',choice=display))

    def request_feature(self,name):
        id=next((k for k,v in FEATURE_IDS.items() if v==name),name)
        self.show_model('local_qwen3_tts')
        self.controller.feature_action(id,'install')

    def refresh(self):
        self.controller.refresh()

    def showEvent(self,event):
        super().showEvent(event)
        self.refresh()

    def has_active_work(self):
        return self.controller.has_active_work()

    def active_model_ids(self):
        return self.controller.active_model_ids()

    def storage_is_busy(self):
        return self.controller.storage_is_busy()

    def owned_workers(self):
        return self.controller.owned_workers()

    def prepare_storage_relocation(self):
        if self.has_active_work():
            return False
        if self.controller.read_worker:
            self.controller.read_worker.request_cancel()
        return True

    def shutdown_workers(self,timeout_ms=0):
        return self.controller.shutdown_workers(timeout_ms)

    def closeEvent(self,event):
        if not self.shutdown_workers(500):
            event.ignore()
            return
        super().closeEvent(event)

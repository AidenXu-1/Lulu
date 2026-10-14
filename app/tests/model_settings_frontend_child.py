"""Actual Chromium interactions, fake bridge, blocked networking and no real keys."""
import os,json,time,sys
from pathlib import Path
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QTWEBENGINE_DISABLE_SANDBOX']='1'
os.environ['QTWEBENGINE_CHROMIUM_FLAGS']='--disable-gpu --disable-background-networking'
from PySide6.QtCore import QObject,Signal,Slot,QUrl,QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineProfile,QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'app'))
from src.core.agent_workspace import providers as provider_data
fixture=json.loads((root/'app/src/ui/model_settings/public.json').read_text())
policy={p['id']:p for p in provider_data.catalog()['providers']}
for p in fixture['providers']:
 modes={'api':policy[p['id']],**{x['id']:x for x in policy[p['id']]['plans']}}
 for mode in p.get('modes',[]):
  authority=modes.get(mode['id'],{});mode.update(available=authority.get('available',True),unavailableReason=authority.get('unavailable_reason',''))
fixture.update(storage={'extract':'/fixture/models','voice':'/fixture/tts'},jobs={},connections=[],cloudConfigs={'openai':{'configured':True,'credentialId':'openai'},'custom_asr':{'requiresImport':True,'configured':None,'credentialId':'custom_openai'}},defaultConnectionId='',catalogue={'deepseek':[{'id':'fixture-model','label':'Fixture model','window':8192,'max_output':4096}]})
for kind,models in fixture['localModels'].items():
 for model in models:model.update(installed=True,ready=True,default=model['id'] in ('whisper','qwen_tts'))
for f in fixture['voiceFeatures']:f.update(installed=f['id']=='clone',ready=f['id']=='clone')
for provider in fixture['providers']:
 for mode in provider.get('modes',[]):mode.setdefault('protocol','anthropic' if provider['id']=='minimax' else 'openai')
fixture['connections']=[{'id':'legacy-fixture','provider':'deepseek','mode':'api','base':'https://legacy.example.test/v1','protocol':'openai','credential_id':'workspace-key-legacy','label':'既有连接','model':'legacy-model','window':8192,'max_output':4096}]
class BlockNetwork(QWebEngineUrlRequestInterceptor):
 def interceptRequest(self,info):
  if info.requestUrl().scheme() not in ('file','qrc','data'):info.block(True)
class Page(QWebEnginePage):
 def __init__(self,profile):super().__init__(profile);self.errors=[]
 def javaScriptConsoleMessage(self,level,message,line,source):
  if level==QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:self.errors.append((message,line))
class Bridge(QObject):
 snapshot=Signal(str);reply=Signal(str,str);command=Signal(str,str)
 def __init__(self):super().__init__();self.calls=[];self.hold=False;self.held=[];self.hold_discover=False;self.held_discover=[];self.fail_discover=False;self.fail_remove=False;self.hold_remove=False;self.held_remove=[]
 @Slot()
 def ready(self):self.snapshot.emit(json.dumps(fixture))
 @Slot(str,str,str)
 def request(self,id,op,raw):
  args=json.loads(raw);self.calls.append((id,op,args))
  if self.hold and op=='test_connection':self.held.append(id);return
  if self.hold_discover and op=='discover_models':self.held_discover.append(id);return
  if self.fail_discover and op=='discover_models':self.reply.emit(id,json.dumps({'ok':False,'message':'目录暂时不可用'}));return
  val={'message':'已处理'}
  if op=='test_connection':val={'credentialId':'' if args['purpose']=='extract' else 'workspace-key-fixture','status':'partial' if args['purpose']=='extract' else 'success','message':'服务连接正常；尚未测试模型生成。'}
  if op=='discover_models':val=[{'id':f'model-{i}','label':f'Model {i}','window':8192,'max_output':4096} for i in range(7)]
  if op=='load_saved_configuration':val={'credentialId':'custom_openai','cloudConfigs':{'custom_asr':{'provider':'custom_asr','credentialId':'custom_openai','base':'https://legacy-asr.example.test/v1','modelId':'old-asr','configured':True,'requiresImport':False,'active':False}}}
  if op=='save_connection':val={'credentialId':args['provider'] if args['purpose']=='extract' else 'workspace-key-fixture','connections':[],'defaultConnectionId':''}
  if op=='remove_credential':
   if self.fail_remove:self.reply.emit(id,json.dumps({'ok':False,'message':'系统安全存储未能确认删除，原连接保留，请重试'}));return
   if args['purpose']=='extract':fixture['cloudConfigs'][args['provider']]={'configured':False,'credentialId':args['credentialId']}
   else:
    fixture['connections']=[c for c in fixture['connections'] if c['credential_id']!=args['credentialId']]
    fixture['storedConnections']=[c for c in fixture.get('storedConnections',[]) if c['credential_id']!=args['credentialId']]
   val={k:fixture.get(k,[] if k.endswith('Connections') or k=='connections' else '') for k in ('connections','storedConnections','cloudConfigs','defaultConnectionId')}
  if self.hold_remove and op=='remove_credential':self.held_remove.append((id,val));return
  self.reply.emit(id,json.dumps({'ok':True,'value':val}))
app=QApplication([]);profile=QWebEngineProfile();block=BlockNetwork();profile.setUrlRequestInterceptor(block)
view=QWebEngineView();page=Page(profile);view.setPage(page);channel=QWebChannel();bridge=Bridge();channel.registerObject('models',bridge);page.setWebChannel(channel)
view.resize(1448,917);view.show();page.load(QUrl.fromLocalFile(str(root/'app/src/ui/model_settings/assets/index.html')))
def tick(ms=15):
 loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
def until(fn,timeout=10):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  if fn():return
  tick()
 raise AssertionError('timeout')
def js(code):
 before=len(page.errors);values=[];page.runJavaScript(code,lambda v:values.append(v));until(lambda:bool(values))
 if len(page.errors)>before:raise AssertionError((code,page.errors[before:]))
 return values[0]
def click(selector):js('document.querySelector('+json.dumps(selector)+').click()');tick()
def input_value(selector,value):js('(el=>{el.value='+json.dumps(value)+';el.dispatchEvent(new Event("input"));})(document.querySelector('+json.dumps(selector)+'))')
until(lambda:js('Boolean(window.luluModels?.ready && document.querySelector(".local-detail"))'))
assert js('document.querySelector(".detail-title h2").textContent')=='Whisper'
assert js('document.querySelector("main h1")===null')
click('#tab-voice');assert js('[...document.querySelectorAll("[data-local]")].map(x=>x.dataset.local).join(",")')=='indextts2,indextts25,voxcpm2,qwen_tts'
click('[data-local="qwen_tts"]')
click('#tab-assistant');assert js('document.querySelector("#model-list").clientHeight')==180
click('[data-provider="mimo"]');js('document.querySelector("#api-key").focus()')
assert js('parseFloat(getComputedStyle(document.querySelector(".input-shell")).paddingLeft)')>=14
assert js('parseFloat(getComputedStyle(document.querySelector(".input-shell")).gap)')>=12
assert js('getComputedStyle(document.querySelector("#api-key")).outlineStyle')=='none'
click('[data-provider="deepseek"]')
assert js('[...document.querySelectorAll("button[data-provider]")].map(x=>x.dataset.provider).join(",")')=='deepseek,kimi,glm,minimax,mimo,qwen,doubao,openrouter,agnes,custom'
# Built-in connections expose only needed controls and unavailable services stop early.
for provider in fixture['providers']:
 click('[data-provider="'+provider['id']+'"]')
 if provider['id']!='custom':assert js('document.querySelector("#custom-base")===null')
 for i,mode in enumerate(provider.get('modes',[])):
  if len(provider['modes'])>1:click('#service');click('#menu-items button[data-index="'+str(i)+'"]')
  if mode.get('available') is False:assert js('document.querySelector("#api-key")===null && document.querySelector("#save-connection")===null')
 if len(provider.get('modes',[]))>1:click('#service');click('#menu-items button[data-index="0"]')
# Region and service drafts never share an entered key.
click('[data-provider="minimax"]');click('#service');click('#menu-items button[data-index="0"]');input_value('#api-key','china-fixture')
click('#region');click('#menu-items button[data-index="1"]');assert js('document.querySelector("#api-key").value')=='';input_value('#api-key','international-fixture')
click('#service');click('#menu-items button[data-index="1"]');assert js('document.querySelector("#api-key").value')=='';input_value('#api-key','plan-fixture')
click('#service');click('#menu-items button[data-index="0"]');assert js('document.querySelector("#api-key").value')=='china-fixture'
click('#region');click('#menu-items button[data-index="1"]');assert js('document.querySelector("#api-key").value')=='international-fixture'
click('#tab-extract');click('#tab-assistant');assert js('document.querySelector("#api-key").value')=='international-fixture'
click('[data-provider="deepseek"]')
# Model search and an explicit manual addition are both keyboard-accessible.
click('#add-model');input_value('#menu-search','fixture');assert js('document.querySelectorAll("#menu-items button").length')==1
js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape"}))');tick()
click('#add-model');click('#menu-items button[data-index="0"]');input_value('#new-model-id','manual-fixture');js('document.querySelector("#model-form").requestSubmit()');tick()
assert js('document.querySelector("[data-model=manual-fixture]").checked')
click('[data-model-more="manual-fixture"]');click('#menu-items button[data-index="2"]');assert js('document.querySelector("[data-model=manual-fixture]")===null')
input_value('#api-key','fixture-one');click('#key-visibility');assert js('document.querySelector("#api-key").type')=='text';click('#key-visibility');assert js('document.querySelector("#api-key").type')=='password'
assert not js('document.querySelector("#test-connection").disabled')
click('[data-provider="kimi"]');assert js('document.querySelector("#api-key").value')==''
input_value('#api-key','fixture-two');click('[data-provider="deepseek"]');assert js('document.querySelector("#api-key").value')=='fixture-one'
click('#test-connection');until(lambda:js('document.querySelector("#api-key").value')=='');assert '已保存密钥' in js('document.querySelector("#api-key").placeholder')
assert '服务连接正常' in js('document.querySelector("#connection-feedback").textContent')
# A late catalogue belongs to its source provider; failures keep the credential.
bridge.hold_discover=True;click('#fetch-models');until(lambda:bool(bridge.held_discover));click('[data-provider="kimi"]')
bridge.reply.emit(bridge.held_discover.pop(),json.dumps({'ok':True,'value':[{'id':'late-fixture','label':'Late fixture model','window':8192}]}));tick()
assert 'Late fixture model' not in js('document.querySelector("#model-list").textContent')
bridge.hold_discover=False;click('[data-provider="deepseek"]');bridge.fail_discover=True;click('#fetch-models');assert '目录暂时不可用' in js('document.querySelector("#connection-feedback").textContent');assert '已保存密钥' in js('document.querySelector("#api-key").placeholder')
bridge.fail_discover=False
click('#fetch-models');until(lambda:js('document.querySelectorAll(".model-row").length')==7);assert js('document.querySelector("#model-list").scrollHeight')>180
click('[data-model="model-0"]');assert not js('document.querySelector("#save-connection").disabled')
click('#save-connection');assert bridge.calls[-1][2]['key']=='';assert bridge.calls[-1][2]['modelIds']==['model-0']
# A late test must not mark a subsequently edited key as validated.
bridge.hold=True;input_value('#api-key','fixture-three');click('#test-connection');until(lambda:bool(bridge.held));input_value('#api-key','fixture-four')
bridge.reply.emit(bridge.held.pop(),json.dumps({'ok':True,'value':{'credentialId':'workspace-key-old','status':'success','message':'STALE'}}));tick()
assert js('document.querySelector("#api-key").value')=='fixture-four'
assert 'STALE' not in js('document.querySelector("#connection-feedback").textContent')
bridge.hold=False
# Explicit selection preserves a legacy endpoint and opaque key scope.
bridge.snapshot.emit(json.dumps(fixture));tick();click('#saved-connection');click('#menu-items button');
assert js('document.querySelector("#resolved-base").textContent')=='https://legacy.example.test/v1'
assert js('document.querySelector("#api-key").value')==''
click('#test-connection');assert bridge.calls[-1][2]['base']=='https://legacy.example.test/v1'
assert bridge.calls[-1][2]['credentialId']=='workspace-key-legacy';assert bridge.calls[-1][2]['key']==''
# Deferred snapshots keep choices open, then reconcile state after dismissal.
click('[data-model-more]');fixture['storage']['voice']='/fixture/tts-updated';bridge.snapshot.emit(json.dumps(fixture));tick()
assert not js('document.querySelector("#menu").hidden')
js('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape"}))');tick()
assert js('document.querySelector("#menu").hidden')
# Model information is read-only; Escape returns focus and closes the dialog.
count=len(bridge.calls);click('[data-model-more]');click('#menu-items button');assert js('document.querySelector("#modal").open')
js('document.querySelector("#modal-close").click()');tick();assert not js('document.querySelector("#modal").open');assert len(bridge.calls)==count
# File and model commands are explicit, no action on ordinary navigation.
click('#tab-extract');click('#reveal-storage');assert bridge.calls[-1][1:] == ('storage_open',{'purpose':'extract'})
click('[data-local-mode="cloud"]');input_value('#api-key','fixture-asr');click('#test-connection');tick();assert js('document.querySelector("#api-key").value')=='fixture-asr';assert js('document.querySelector("#test-connection").textContent')=='检查完成';click('#save-connection');assert js('document.querySelector("#api-key").value')=='';click('#get-key');assert bridge.calls[-1][1]=='open_key_page'
click('[data-provider="openai"]');assert js('document.querySelector(".detail-top .status").textContent')=='已保存'
count=len(bridge.calls);click('[data-provider="custom_asr"]');assert len(bridge.calls)==count;click('#load-saved-configuration');assert bridge.calls[-1][1:] == ('load_saved_configuration',{'purpose':'extract','provider':'custom_asr'})
assert js('document.querySelector("#custom-base").value')=='https://legacy-asr.example.test/v1';assert js('document.querySelector("#asr-model-id").value')=='old-asr';assert js('document.querySelector("#api-key").value')=='';assert '已保存密钥' in js('document.querySelector("#api-key").placeholder')
# Deletion is scoped to the selected service, explicit, retryable and secret-free.
count=len(bridge.calls);click('#remove-key');assert js('document.querySelector("#modal").open');click('#cancel-remove-key');assert len(bridge.calls)==count
bridge.fail_remove=True;click('#remove-key');click('#confirm-remove-key');tick()
assert js('document.querySelector("#modal").open');assert '原连接保留' in js('document.querySelector("#remove-key-error").textContent')
assert '已保存密钥' in js('document.querySelector("#api-key").placeholder')
bridge.fail_remove=False;click('#confirm-remove-key');tick()
assert not js('document.querySelector("#modal").open');assert js('document.querySelector("#api-key").placeholder')=='输入 API 密钥'
assert js('document.querySelector("#test-connection").disabled && document.querySelector("#save-connection").disabled')
assert bridge.calls[-1][1]=='remove_credential';assert bridge.calls[-1][2]['credentialId']=='custom_openai';assert 'key' not in bridge.calls[-1][2]
assert fixture['cloudConfigs']['openai']['configured']
# Two saved keys stay manageable on the provider page, including an inactive key.
legacy=fixture['connections'][0]
fixture['storedConnections']=[legacy,dict(legacy,id='second-connection',credential_id='workspace-key-second',label='第二把密钥')]
bridge.snapshot.emit(json.dumps(fixture));tick();click('#tab-assistant');click('[data-provider="deepseek"]');click('#saved-connection')
assert js('document.querySelectorAll("#menu-items button").length')==3
click('#menu-items button[data-index="1"]');click('#remove-key');click('#confirm-remove-key');tick()
assert bridge.calls[-1][2]['credentialId']=='workspace-key-second'
assert len(fixture['storedConnections'])==1 and len(fixture['connections'])==1
assert js('document.querySelector("#save-connection").disabled')
click('#saved-connection');assert js('document.querySelectorAll("#menu-items button").length')==2
click('#menu-items button[data-index="0"]');assert '已保存密钥' in js('document.querySelector("#api-key").placeholder')
# A delayed removal must not close a newer dialog or alter another provider's controls.
bridge.hold_remove=True;click('#remove-key');click('#confirm-remove-key');until(lambda:bool(bridge.held_remove))
click('#modal-close');click('[data-provider="kimi"]');click('#add-model');click('#menu-items button[data-index="0"]')
assert js('Boolean(document.querySelector("#model-form"))')
rid,value=bridge.held_remove.pop();bridge.reply.emit(rid,json.dumps({'ok':True,'value':value}));tick()
assert js('document.querySelector("#modal").open && Boolean(document.querySelector("#model-form"))')
assert js('document.querySelector("[data-provider=kimi]").getAttribute("aria-pressed")==="true"')
click('#modal-close');bridge.hold_remove=False
click('#tab-extract');click('[data-provider="custom_asr"]')
bridge.snapshot.emit(json.dumps(fixture));tick();view.resize(968,740);until(lambda:js('innerWidth')==968);tick(120)
assert not js('document.documentElement.hasAttribute("data-theme")')
assert js('document.documentElement.scrollWidth <= innerWidth')
assert js('document.querySelector("#save-connection").getBoundingClientRect().right <= innerWidth')
assert js('document.querySelector("#test-connection").getBoundingClientRect().right <= innerWidth')
assert not page.errors,page.errors
print(json.dumps({'checks':'passed','requests':len(bridge.calls),'errors':page.errors,'model_list_height':180},ensure_ascii=False))
view.close();page.deleteLater();tick();profile.deleteLater();tick()

"""Production Qt bridge and controller in an isolated home, with all network blocked."""
import os,json,time,tempfile
from pathlib import Path
from unittest.mock import patch
os.environ.update(QT_QPA_PLATFORM='offscreen',QTWEBENGINE_DISABLE_SANDBOX='1',QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --disable-background-networking',LULU_TEST_SECRET_STORE='memory')
with tempfile.TemporaryDirectory(prefix='lulu-model-native-') as directory,patch.object(Path,'home',return_value=Path(directory)):
 from src.config import AppConfig
 from src.core import secrets
 from src.ui.model_settings.widget import ModelsPage,Page
 from PySide6.QtCore import QEventLoop,QTimer,QCoreApplication,QEvent
 from PySide6.QtWidgets import QApplication
 errors=[]
 with patch.object(secrets,'get_key',side_effect=AssertionError('passive secret access')),patch.object(secrets,'get_config',side_effect=AssertionError('passive secret access')),patch.object(Page,'javaScriptConsoleMessage',lambda self,level,msg,line,source: errors.append((str(level),msg))):
  app=QApplication([]);cfg=AppConfig();page=ModelsPage(cfg);page.resize(1448,917);page.show()
  def tick(ms=20):
   loop=QEventLoop();QTimer.singleShot(ms,loop.quit);loop.exec()
  def until(fn):
   end=time.monotonic()+15
   while time.monotonic()<end:
    if fn():return
    tick()
   raise AssertionError('native bridge timed out')
  def js(code):
   results=[];page.page.runJavaScript(code,lambda value:results.append(value));until(lambda:len(results));return results[0]
  until(lambda:page.ready and bool(page.controller.state))
  until(lambda:js('Boolean(document.querySelector(".local-detail"))'))
  assert js('document.querySelector("main h1")===null')
  assert js('document.querySelector("#tab-extract").textContent')=='内容提取'
  assert js('document.querySelector("#tab-extract").dataset.purpose')=='extract'
  assert not page.controller.state['localModels']['extract'][0]['ready']
  assert [row['id'] for row in page.controller.state['localModels']['extract']]==['sensevoice','whisper','whisper4','moss']
  page.show_purpose(1);until(lambda:js('document.querySelector("#tab-voice").getAttribute("aria-selected")==="true"'))
  assert js('[...document.querySelectorAll("[data-local]")].map(x=>x.dataset.local).join(",")')=='indextts2,indextts25,voxcpm2,qwen_tts'
  page.show_purpose(2);until(lambda:js('Boolean(document.querySelector("#model-list"))'))
  assert js('document.querySelector("#model-list").clientHeight')==180
  assert js('document.querySelectorAll("button[data-provider]").length')==10
  results=[];page.bridge.reply.connect(lambda rid,raw:results.append((rid,json.loads(raw))))
  page.bridge.request('invalid','unknown','{}');assert results[-1][1]['ok'] is False
  page.bridge.request('refresh','refresh','{}');assert results[-1][1]['ok'] is True
  until(lambda:not page.owned_workers())
  # Real page -> closed bridge -> native worker -> Core deletion, fake secret store.
  from src.core.agent_workspace import providers
  connection=providers.save_connection(dict(provider='deepseek',mode='api',base='https://api.deepseek.com',
   protocol='openai',model='deepseek-flash',window=1048576,max_output=4096,threshold=.8,
   credential_id='workspace-key-native-fixture',label='Native fixture'),page.controller.cloud.store)
  page.controller.state.update(page.controller.cloud.snapshot());page.controller.publish()
  until(lambda:js('Boolean(document.querySelector("#saved-connection"))'))
  js('document.querySelector("#saved-connection").click()')
  until(lambda:js('Boolean(document.querySelector("#menu-items button"))'))
  js('document.querySelector("#menu-items button").click()')
  until(lambda:js('Boolean(document.querySelector("#remove-key"))'))
  js('document.querySelector("#remove-key").click()')
  until(lambda:js('Boolean(document.querySelector("#confirm-remove-key"))'))
  removal_start=len(results)
  with patch.object(secrets,'set_key_verified') as remove_key:
   js('document.querySelector("#confirm-remove-key").click()')
   until(lambda:any(raw['ok'] and raw.get('value',{}).get('storedConnections')==[] for rid,raw in results[removal_start:]))
   until(lambda:not page.owned_workers())
   remove_key.assert_called_once_with('workspace-key-native-fixture','')
  until(lambda:js('!document.querySelector("#modal").open'))
  assert js('document.querySelector("#remove-key")===null')
  assert providers.available_connections(page.controller.cloud.store)==[]
  assert providers.connection(connection['id'],page.controller.cloud.store)==connection
  # Exercise the actual default button and its immediate commit UI through Qt.
  page.show_purpose(0)
  cfg.engine_name='local_sensevoice_q8'
  for row in page.controller.state['localModels']['extract']:
   row.update(ready=True, installed=True)
  page.controller.publish()
  until(lambda:js('Boolean(document.querySelector("[data-local=whisper]"))'))
  js('document.querySelector("[data-local=whisper]").click()')
  until(lambda:js('Boolean(document.querySelector("[data-local-action=default]"))'))
  with patch.object(page.controller, '_selection_ready', side_effect=AssertionError('Default selection must not scan models')):
   started=time.monotonic()
   js('document.querySelector("[data-local-action=default]").click()')
   until(lambda:cfg.engine_name=='local_whisper_mlx')
   until(lambda:js('document.querySelector(".install-zone").textContent.includes("当前默认")'))
   selection_ms=round((time.monotonic()-started)*1000,1)
  until(lambda:not page.owned_workers())
  assert not [item for item in errors if 'ErrorMessageLevel' in item[0]],errors
  capture=os.environ.get('LULU_MODEL_SETTINGS_CAPTURE')
  if capture:
   out=Path(capture);out.mkdir(parents=True,exist_ok=True)
   for idx,name in ((0,'extract'),(1,'voice'),(2,'assistant')):
    page.show_purpose(idx);tick(180);page.view.grab().save(str(out/(name+'.png')))
  assert page.shutdown_workers(3000)
  page.close();page.page.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);page.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
  print(json.dumps(dict(status='passed',defaultSelectionMilliseconds=selection_ms,purposes=3,modelListHeight=180,passiveSecretReads=0,unknownOperationBlocked=True,credentialRemoval=True)))

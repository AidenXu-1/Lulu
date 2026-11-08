// Persistent Core batches; the browser holds only presentation and selection.
let workspaceBatches=[],legacyBatchCount=0,batchError='',nativeExpanded=new Set(),nativeScrolls=new Map(),nativeRequestedPages=new Map();
const pendingIntakes=new Map();
function receiveIntake(raw){const value=JSON.parse(raw);if(value.batch){pendingIntakes.set(value.token,value.batch);filter=value.batch.workspace.group?'batch':'single'}else pendingIntakes.delete(value.token);renderBoard(new Set(value.batch?['native:'+value.batch.batch_id]:[]))}
let nativeUndoToken=null;
function nativeUndoNotice(value){if(value&&value.token!==nativeUndoToken){nativeUndoToken=value.token;toast('已移出列表，原文件和已有结果保留');const button=document.createElement('button');button.textContent='撤销';button.onclick=()=>{act('batch_restore',{token:value.token});hideToast()};$('#toast').append(button);clearTimeout(toastTimer);toastTimer=setTimeout(hideToast,6000)}}
const NX_LABELS={text:'文案',video:'视频',cover:'封面'};
const NX_STATES={staging:'正在加入',draft:'待开始',queued:'等待处理',running:'提取中',settling:'正在结束',paused:'已暂停',succeeded:'已完成',failed:'部分未完成',interrupted:'已中断',cancelled:'已取消',duplicate:'重复作品',recovery_required:'结果待核对'};
const nxBatch=id=>workspaceBatches.find(b=>b.batch_id===id);
const nxItem=(id,index)=>nxBatch(id)?.items.find(i=>i.index===index);
const nxUnknown=i=>Object.values(i.assets).some(a=>a.state==='unknown');
const nxLocked=i=>['queued','running','settling','paused'].includes(i.state)||i.save_pending;
const nxMissing=i=>i.selection.outputs.filter(k=>!['ready','unknown'].includes(i.assets[k].state));
const nxSelected=(b,i)=>b.selectAll?!b.selection.includes(i.index):b.selection.includes(i.index);
const nativeChoicePending=new Set(),nativeChoiceStates=new Map();
const nativeChoiceBusy=(id,index=null)=>index===null?[...nativeChoicePending].some(key=>key.startsWith(id+'|')):nativeChoicePending.has(`${id}|${index}`);
function nativeChoiceItem(id,item){
 const local=nativeChoiceStates.get(`${id}|${item.index}`);
 return local?{...item,selection:{...item.selection,...local.desired}}:item;
}
function nativeChoice(id,index,key,value){
 const token=`${id}|${index}`,item=nxItem(id,index);if(!item||nxLocked(item))return;
 let local=nativeChoiceStates.get(token);
 if(!local){local={desired:{outputs:[...item.selection.outputs],prefer_subtitles:item.selection.prefer_subtitles},sequence:0,running:false};nativeChoiceStates.set(token,local)}
 if(key==='subs')local.desired.prefer_subtitles=value;
 else{const outputs=new Set(local.desired.outputs);value?outputs.add(key):outputs.delete(key);local.desired.outputs=['text','video','cover'].filter(k=>outputs.has(k))}
 local.sequence++;nativeChoicePending.add(token);renderBoard();
 if(local.running)return;local.running=true;
 local.completion=(async()=>{
  try{
   while(nativeChoiceStates.get(token)===local){
    const sequence=local.sequence,desired={outputs:[...local.desired.outputs],prefer_subtitles:local.desired.prefer_subtitles};
    await call('batch_choices',{batch_id:id,index,...desired});
    if(sequence===local.sequence)break;
   }
   return true;
  }catch(error){toast('选择未保存，已恢复实际设置：'+error.message);return false}
  finally{nativeChoiceStates.delete(token);nativeChoicePending.delete(token);renderBoard()}
 })();
}
function nativeSelect(id,index,selected){act('batch_select',{batch_id:id,index,selected})}
function nativeFolder(id,index){act('batch_folder',{batch_id:id,index})}
const nativePending=new Set(),nativeDismissals=new Set();
function nativeVisibleBatches(){return [...workspaceBatches,...pendingIntakes.values()].map(b=>{
 const items=b.items.filter(i=>!nativeDismissals.has(`${b.batch_id}|${i.index}`));
 return items.length===b.items.length?b:{...b,items,visible_total:Math.max(0,b.visible_total-b.items.length+items.length)};
}).filter(b=>b.visible_total>0)}
const nativeCommandKey=(id,action,index=null,selected=false)=>`${id}|${index===null?'all':index}|${action}|${selected?1:0}`;
const nativeIsPending=(id,action,index=null,selected=false)=>{const key=nativeCommandKey(id,action,index,selected);return nativePending.has(key)||!!nxBatch(id)?.pending_controls?.includes(key)};
function nativeCommandAttrs(id,action,index=null,selected=false){
 const key=nativeCommandKey(id,action,index,selected);
 return `data-native-command="${key}" ${nativeIsPending(id,action,index,selected)?'aria-busy="true" disabled':''}`;
}
async function nativeWaitChoices(id,index){
 // A click accepts the user's latest settings without flashing disabled styles.
 // Wait for the entire coalesced write, including clicks made during its first save.
 for(;;){
  const writes=[...nativeChoiceStates].filter(([key])=>index===null?key.startsWith(id+'|'):key===`${id}|${index}`).map(([,local])=>local.completion);
  if(!writes.length)return true;
  if((await Promise.all(writes)).some(ok=>!ok))return false;
 }
}
function nativeControl(id,action,index=null,selected=false){
 const key=nativeCommandKey(id,action,index,selected);if(nativeIsPending(id,action,index,selected))return;
 nativePending.add(key);
 const dismissToken=`${id}|${index}`,item=index===null?null:nxItem(id,index);
 const immediate=action==='dismiss'&&item&&!['running','settling','queued'].includes(item.state);
 if(immediate){nativeDismissals.add(dismissToken);renderBoard()}
 const button=document.querySelector(`[data-native-command="${key}"]`);
 if(button){button.disabled=true;button.setAttribute('aria-busy','true');if(!button.classList.contains('iconbtn'))button.textContent='处理中…'}
 return (async()=>{
  const saved=await nativeWaitChoices(id,index);
  if(!saved&&['start','retry','resume','retry_unknown'].includes(action))return;
  return await call('batch_control',{batch_id:id,action,indices:index===null?null:[index],selected});
 })().catch(e=>toast(e.message)).finally(()=>{
  nativeDismissals.delete(dismissToken);
  nativePending.delete(key);const cached=boardCards.get('native:'+id);if(cached)cached.signature=null;renderBoard()
 });
}
function nativeCardSignature(batch,expanded){
 const volatile=new Set(['message','progress','activity','cloud_attempt','cloud_attempt_history','attempts','attempt_id','attempt_selection','applied_attempt_id','recover_only','version','focus_token','_animateNew']);
 const display={...batch,items:batch.items.map(i=>nativeChoiceItem(batch.batch_id,i))};
 return JSON.stringify(display,(key,value)=>volatile.has(key)?undefined:value)+':'+expanded+':'+[...nativeChoicePending].filter(key=>key.startsWith(batch.batch_id+'|')).join(',');
}
function nativeSyncMessages(batch){
 const card=document.querySelector(`[data-native-batch="${batch.batch_id}"]`);if(!card)return;
 for(const item of batch.items){
  const message=item.error||item.message||'',row=card.querySelector(`[data-native-row="${item.index}"] .nx-row-message`);
  if(row){row.textContent=message;row.title=message}
  if(!batch.workspace.group)nativeSyncProgress(card,batch,item);
 }
}
function nativeResult(id,index,key){call('batch_result',{batch_id:id,index,key}).then(()=>closeModal()).catch(e=>toast(e.message))}
function nativeAction(i){
 if(i.state==='duplicate')return ['locate','查看原任务'];
 if(i.state==='running'||i.state==='settling')return ['cancel',i.state==='settling'?'正在结束':'取消'];
 if(i.state==='queued')return ['pause','暂停'];
 if(i.state==='paused')return ['resume','继续'];
 if(i.save_pending)return ['check','保存结果'];
 if(nxMissing(i).length){const supplement=Object.values(i.assets).some(a=>a.state==='ready'||a.error?.includes('补充提取'));return [i.state==='failed'?'retry':'start',supplement?'补充提取':i.state==='failed'?'重试失败项':'开始提取']}
 if(nxUnknown(i))return ['inspect','核对结果'];
 if(i.assets.text.state==='ready')return ['text','查看文稿'];
 if(Object.values(i.assets).some(a=>a.state==='ready'))return ['inspect','查看结果'];
 return ['none',i.state==='duplicate'?'重复作品':'选择内容'];
}
function nativeAct(id,index){
 const item=nxItem(id,index);if(!item)return;const i=nativeChoiceItem(id,item),[action]=nativeAction(i);
 if(action==='locate')act('batch_locate',{batch_id:id,index});else if(action==='text')nativeResult(id,index,'text');else if(action==='inspect'){nxUnknown(i)||i.save_pending?nativeDetail(id,index):nativeAssets(id,index)}else if(action!=='none')nativeControl(id,action,index);
}
function nativeOutput(b,i,key){
 i=nativeChoiceItem(b.batch_id,i);
 const a=i.assets[key],name=NX_LABELS[key],selected=i.selection.outputs.includes(key),locked=nxLocked(i),id=b.batch_id,glyph=key==='text'?'file':key==='video'?'play':'image';
 if(a.state==='ready')return `<button class="nx-output ready" aria-label="查看已提取${name}：${esc(i.title)}" title="已保存，点击查看" onclick="nativeResult('${id}',${i.index},'${key}')">${icon('check')}<span>${name}</span></button>`;
 if(['running','unknown','pending_save'].includes(a.state))return `<span class="nx-output ${a.state}">${icon(a.state==='unknown'?'info':glyph)}<span>${name}</span><small>${a.state==='unknown'?'待核对':a.state==='pending_save'?'待保存':'提取中'}</small></span>`;
 return `<label class="nx-output ${selected?'selected':''} ${a.state==='failed'?'failed':''}"><input type="checkbox" aria-label="${a.state==='failed'?'素材不可用，重新提取':'提取'}${name}：${esc(i.title)}" ${selected?'checked':''} ${locked||i.local_source&&key!=='text'?'disabled':''} onchange="nativeChoice('${id}',${i.index},'${key}',this.checked)">${icon(glyph)}<span>${name}</span><span class="nx-choice-mark" aria-hidden="true">${icon('check')}</span></label>`;
}
function nativeSubtitle(b,i){i=nativeChoiceItem(b.batch_id,i);return `<button class="subtitle-pill ${i.selection.prefer_subtitles?'on':''}" aria-label="优先字幕：${esc(i.title)}" aria-pressed="${i.selection.prefer_subtitles}" ${nxLocked(i)||!i.selection.outputs.includes('text')||['ready','unknown'].includes(i.assets.text.state)?'disabled':''} onclick="nativeChoice('${b.batch_id}',${i.index},'subs',${!i.selection.prefer_subtitles})"><span class="subtitle-symbol">CC</span>优先字幕</button>`}
function nativeModel(b){return `<button class="nx-model" aria-label="本任务模型：${esc(modelLabel(b.engine))}" aria-haspopup="dialog" aria-expanded="false" ${b.model_locked?'disabled':''} onclick="nativeModels('${b.batch_id}')">${icon(b.model_locked?'shield':modelOptions.find(m=>m.id===b.engine)?.cloud?'cloud':'headphones')}<span>${esc(modelLabel(b.engine))}</span>${b.model_locked?'':`<span class="nx-chevron">${icon('chevron')}</span>`}</button>`}
function nativeStateLabel(b,i){
 const cloud=!!modelOptions.find(m=>m.id===b.engine)?.cloud;
 if(i.state==='queued')return cloud?'等待发送':'等待识别';
 if(i.state==='running')return '提取中';
 if(i.state==='failed')return i.selection.outputs.some(k=>i.assets[k]?.state==='ready')?'部分未完成':'提取失败';
 return NX_STATES[i.state]||i.state;
}
let nativeModelMenu=null;
function nativePositionPopover(pop,trigger,width=320){
 const rect=trigger.getBoundingClientRect(),height=pop.offsetHeight;
 pop.style.left=Math.max(12,Math.min(rect.left,window.innerWidth-width-12))+'px';
 pop.style.top=Math.max(12,rect.bottom+8+height>window.innerHeight-12?rect.top-height-8:rect.bottom+8)+'px';
}
function nativeCloseModels(restoreFocus=false){
 const pop=document.getElementById('nx-model-popover');if(!pop)return;
 const id=pop.dataset.batch,trigger=document.querySelector(`[data-native-batch="${id}"] .nx-model`);pop.remove();nativeModelMenu=null;
 trigger?.setAttribute('aria-expanded','false');if(restoreFocus)trigger?.focus({preventScroll:true});
}
function nativeModelOptions(b,id,tab){return modelOptions.filter(m=>!!m.cloud===(tab==='cloud')).map(m=>`<button class="model-option ${b.engine===m.id?'selected':''}" role="menuitemradio" aria-checked="${b.engine===m.id}" onclick="nativeSetModel('${id}','${m.id}')"><span class="model-option-copy"><strong>${esc(m.name)}</strong><small>${m.cloud?'云端识别':m.ready?'本机模型已就绪':'模型尚未准备，可前往模型设置'}</small></span>${b.engine===m.id?icon('check'):''}</button>`).join('')}
function nativeUpdateModelTab(pop,b,tab){
 nativeModelMenu={id:b.batch_id,tab};pop.classList.toggle('is-cloud',tab==='cloud');
 pop.querySelectorAll('[role=tab]').forEach(button=>{const selected=button.textContent===(tab==='cloud'?'云端 API':'本地');button.setAttribute('aria-selected',selected);button.tabIndex=selected?0:-1});
 pop.querySelector('.nx-model-note').textContent=tab==='cloud'?'并行识别，无需本地排队；音频上传并计费':'逐条识别，按顺序排队；音频不上传';
 const options=pop.querySelector('.nx-model-options');options.setAttribute('aria-label',`${tab==='cloud'?'云端':'本地'}转录模型`);options.innerHTML=nativeModelOptions(b,b.batch_id,tab);
 pop.querySelector('.nx-model-menu-footer>span').textContent=tab==='cloud'?'消耗账户余额，多任务并行处理':'所有本地任务共享识别队列';
}
function nativeModels(id,tab=null){
 const b=nxBatch(id);if(!b||b.model_locked)return;
 const existing=document.getElementById('nx-model-popover');
 if(existing&&existing.dataset.batch===id){
  if(tab===null){nativeCloseModels(true);return}
  nativeUpdateModelTab(existing,b,tab);existing.querySelector('[role=tab][aria-selected=true]')?.focus({preventScroll:true});return;
 }
 const trigger=document.querySelector(`[data-native-batch="${id}"] .nx-model`);if(!trigger)return;
 tab=tab||(modelOptions.find(m=>m.id===b.engine)?.cloud?'cloud':'local');nativeCloseDetails();nativeCloseModels();
 const pop=document.createElement('div');pop.id='nx-model-popover';pop.className='nx-model-popover';pop.dataset.batch=id;pop.dataset.engine=b.engine;pop.setAttribute('role','dialog');pop.setAttribute('aria-label','选择本任务的转录模型');
 pop.innerHTML=`<div class="nx-model-tabs" role="tablist" aria-label="模型运行位置">${[['local','本地'],['cloud','云端 API']].map(([k,n])=>`<button role="tab" aria-selected="${tab===k}" tabindex="${tab===k?0:-1}" onclick="nativeModels('${id}','${k}')">${n}</button>`).join('')}</div><p class="nx-model-note"></p><div class="nx-model-options" role="menu"></div><div class="nx-model-menu-footer"><span></span><button class="textbtn" onclick="act('models');nativeCloseModels()">模型设置${icon('arrow')}</button></div>`;
 nativeUpdateModelTab(pop,b,tab);document.body.append(pop);nativePositionPopover(pop,trigger);
 trigger.setAttribute('aria-expanded','true');pop.querySelector('[role=tab][aria-selected=true]')?.focus({preventScroll:true});
}
function nativeSetModel(id,engine){call('batch_model',{batch_id:id,engine}).then(()=>nativeCloseModels()).catch(e=>toast(e.message))}
function nativeReadyAssets(i){return ['video','cover'].filter(key=>i.assets[key].state==='ready')}
function nativeAssetDirectory(path){const clean=String(path).replace(/\/+$/,'');return clean.slice(0,clean.lastIndexOf('/'))||'/'}
function nativeAssetButton(b,i){
 const choosing=!i.local_source&&!nxLocked(i)&&i.selection.outputs.some(key=>key!=='text'&&i.assets[key].state!=='ready');
 if(choosing)return `<button class="iconbtn nx-asset-folder" aria-label="设置素材保存位置：${esc(i.title)}" title="${esc(i.selection.media_directory?'保存到：'+i.selection.media_directory:'选择视频与封面的保存位置')}" onclick="nativeFolder('${b.batch_id}',${i.index})">${icon('folder')}</button>`;
 const ready=nativeReadyAssets(i),different=ready.length>1&&nativeAssetDirectory(i.assets.video.path)!==nativeAssetDirectory(i.assets.cover.path);
 const label=ready.length?'打开素材所在位置':'视频或封面提取后可打开所在位置';
 return `<button class="iconbtn nx-asset-folder" data-native-assets="${b.batch_id}:${i.index}" aria-label="${label}：${esc(i.title)}" aria-expanded="false" ${different?'aria-haspopup="menu"':''} title="${label}" ${ready.length?'':'disabled'} onclick="nativeAssets('${b.batch_id}',${i.index})">${icon('folder')}</button>`;
}
function nativeAssets(id,index){
 const i=nxItem(id,index);if(!i)return;const ready=nativeReadyAssets(i);if(!ready.length)return;
 nativeCloseModels();nativeCloseDetails();
 if(ready.length===1||nativeAssetDirectory(i.assets.video.path)===nativeAssetDirectory(i.assets.cover.path)){nativeResult(id,index,ready[0]);return}
 const key=`${id}:${index}`,trigger=document.querySelector(`[data-native-assets="${key}"]`);if(!trigger)return;
 const pop=document.createElement('div');pop.id='nx-action-popover';pop.className='nx-action-popover';pop.dataset.key=key;pop.setAttribute('role','menu');pop.setAttribute('aria-label','素材所在位置');
 pop.dataset.batch=id;pop.dataset.index=index;pop.dataset.kind='assets';pop._itemSignature=nativeDetailSignature(i);
 pop.innerHTML=`<div class="nx-action-heading"><strong>素材所在位置</strong><span class="nx-status-pill">视频与封面</span></div><p class="nx-action-message">两项素材保存在不同文件夹，请选择要打开的位置。</p><div class="nx-action-section">${ready.map(key=>`<button role="menuitem" class="nx-action-item" onclick="nativeCloseDetails();nativeResult('${id}',${index},'${key}')">${icon(key==='video'?'play':'image')}<span>${NX_LABELS[key]}所在位置</span><small title="${esc(nativeAssetDirectory(i.assets[key].path))}">${esc(nativeAssetDirectory(i.assets[key].path))}</small>${icon('arrow')}</button>`).join('')}</div>`;
 document.body.append(pop);nativePositionPopover(pop,trigger,380);trigger.setAttribute('aria-expanded','true');pop.querySelector('button')?.focus({preventScroll:true});
}
function nativeCloseDetails(restoreFocus=false){
 const pop=document.getElementById('nx-action-popover');if(!pop)return;
 const trigger=nativePopoverTrigger(pop);pop.remove();
 trigger?.setAttribute('aria-expanded','false');if(restoreFocus)trigger?.focus({preventScroll:true});
}
function nativeDetailTrigger(id,index){return document.querySelector(`[data-native-detail="${id}:${index}"]`)||document.querySelector(`[data-native-batch="${id}"] [data-native-row="${index}"] .nx-action`)||document.querySelector(`[data-native-batch="${id}"] .nx-single-footer .primary`)}
function nativePopoverTrigger(pop){return pop.dataset.kind==='assets'?document.querySelector(`[data-native-assets="${pop.dataset.key}"]`):nativeDetailTrigger(pop.dataset.batch,Number(pop.dataset.index))}
function nativeDetailSignature(i){return JSON.stringify([i.state,!!i.save_pending,i.selection.outputs,i.assets])}
function nativeSyncPopovers(){
 const pop=document.getElementById('nx-model-popover');
 if(pop){
  const b=nxBatch(pop.dataset.batch),trigger=document.querySelector(`[data-native-batch="${pop.dataset.batch}"]:not(.task-exit) .nx-model`);
  if(!b||!trigger||trigger.disabled||b.engine!==pop.dataset.engine)nativeCloseModels();
  else{trigger.setAttribute('aria-expanded','true');nativePositionPopover(pop,trigger)}
 }
 const detail=document.getElementById('nx-action-popover');
 if(detail){
  const i=nxItem(detail.dataset.batch,Number(detail.dataset.index)),trigger=nativePopoverTrigger(detail);
  if(!i||!trigger||trigger.disabled||trigger.closest('.task-exit')||nativeDetailSignature(i)!==detail._itemSignature)nativeCloseDetails();
  else{trigger.setAttribute('aria-expanded','true');nativePositionPopover(detail,trigger,detail.offsetWidth)}
 }
}
function nativeDetail(id,index){
 const i=nxItem(id,index);if(!i)return;
 const key=`${id}:${index}`,existing=document.getElementById('nx-action-popover');
 if(existing&&existing.dataset.key===key){nativeCloseDetails(true);return}
 const trigger=nativeDetailTrigger(id,index);if(!trigger)return;
 nativeCloseModels();nativeCloseDetails();
 const unknown=nxUnknown(i),ready=Object.entries(i.assets).filter(([k,a])=>a.state==='ready');
 const results=ready.map(([k])=>`<button role="menuitem" class="nx-action-item" onclick="nativeCloseDetails();nativeResult('${id}',${index},'${k}')">${icon(k==='text'?'file':k==='video'?'play':'image')}<span>查看${NX_LABELS[k]}</span>${icon('arrow')}</button>`).join('');
 const pop=document.createElement('div');pop.id='nx-action-popover';pop.className='nx-action-popover';pop.dataset.key=key;pop.setAttribute('role','menu');pop.setAttribute('aria-label',`核对任务结果：${i.title}`);
 pop.dataset.batch=id;pop.dataset.index=index;pop.dataset.kind='detail';pop._itemSignature=nativeDetailSignature(i);
 pop.innerHTML=`<div class="nx-action-heading"><strong>${esc(i.title)}</strong><span class="nx-status-pill ${i.state}">${esc(nativeStateLabel(nxBatch(id),i))}</span></div>${i.message||i.error?`<p class="nx-action-message">${esc(i.error||i.message)}</p>`:''}${unknown?'<p class="nx-action-warning">请求可能已在云端处理，普通继续不会重复上传。请先核对服务后台。</p>':''}${results?`<div class="nx-action-section">${results}</div>`:''}<div class="nx-action-section">${i.save_pending?`<button role="menuitem" class="nx-action-item emphasis" onclick="nativeCloseDetails();nativeControl('${id}','check',${index})">${icon('check')}<span>补保存已有结果</span></button>`:''}${unknown?`<button role="menuitem" class="nx-action-item" onclick="nativeCloseDetails();nativeControl('${id}','check',${index})">${icon('search')}<span>核对本机结果</span></button><button role="menuitem" class="nx-action-item" onclick="nativeCloseDetails();nativeControl('${id}','retry_unknown',${index})">${icon('refresh')}<span>已核对，重新上传</span></button>`:''}</div>`;
 document.body.append(pop);nativePositionPopover(pop,trigger,340);trigger.setAttribute('aria-expanded','true');pop.querySelector('button')?.focus({preventScroll:true});
}
function nativeRow(b,i){
 const [action,label]=nativeAction(i),bid=b.batch_id,message=i.error||i.message,showMessage=['running','failed','interrupted','recovery_required'].includes(i.state),pending=nativeIsPending(bid,action,i.index);
 return `<div class="nx-row" data-native-row="${i.index}"><label class="nx-row-select"><input type="checkbox" aria-label="选择：${esc(i.title)}" ${nxSelected(b,i)?'checked':''} onchange="nativeSelect('${bid}',${i.index},this.checked)"></label><div class="nx-item-title"><div class="nx-item-name"><span class="nx-item-number">${String(i.index+1).padStart(2,'0')}</span><strong title="${esc(i.title)}">${esc(i.title)}</strong></div><small><span class="nx-state ${i.state}">${esc(nativeStateLabel(b,i))}</span>${showMessage?`<span class="nx-row-message" title="${esc(message)}">${esc(message)}</span>`:''}</small></div><div class="nx-outputs" role="group" aria-label="${esc(i.title)}的提取内容">${['text','video','cover'].map(k=>nativeOutput(b,i,k)).join('')}${nativeSubtitle(b,i)}</div><div class="nx-actions"><button class="btn secondary nx-action" ${nativeCommandAttrs(bid,action,i.index)} ${action==='none'||i.state==='settling'?'disabled':''} onclick="nativeAct('${bid}',${i.index})">${pending?'处理中…':label}</button>${nativeAssetButton(b,i)}</div></div>`;
}
function nativeBatchCard(b){
 const bid=b.batch_id,open=!nativeExpanded.has(bid),counts=b.counts,ready=counts.succeeded||0,total=b.visible_total;
 const active=(counts.running||0)+(counts.queued||0)+(counts.settling||0),paused=(counts.paused||0)>0;
 const canResume=(b.auto_paused_count||0)>0, resumable=!b.startable_count&&canResume;
 const batchAction=active?'pause':resumable?'resume':'start';
 const canControl=active?!!counts.queued:!!b.startable_count||canResume;
 const text=active?'暂停后续':resumable?'继续本批':'提取待处理',pending=nativeIsPending(bid,batchAction);
 const cloud=!!modelOptions.find(m=>m.id===b.engine)?.cloud,source=b.items.some(i=>i.stable_key)?'主页采集':'批量提取';
 const controls=b.selectedCount?`<span class="nx-selected-count">已选 ${b.selectedCount} 条</span><span class="nx-selection-actions"><button ${nativeCommandAttrs(bid,'start',null,true)} onclick="nativeControl('${bid}','start',null,true)">提取所选</button><button ${nativeCommandAttrs(bid,'pause',null,true)} onclick="nativeControl('${bid}','pause',null,true)">暂停所选</button><button ${nativeCommandAttrs(bid,'resume',null,true)} onclick="nativeControl('${bid}','resume',null,true)">继续所选</button></span>`:`<span>${total} 条作品</span>${total>5?'<span class="nx-scroll-hint">滚动查看全部</span>':''}`;
 return `<article class="task-group nx-batch ${active?'is-active':''}" data-native-batch="${bid}"><header class="nx-head"><div class="nx-batch-heading"><div class="nx-batch-badge">${platformIcon({platform:b.items[0]?.stable_key?'douyin':b.items[0]?.local_source?'local':identifyPlatform(b.items[0]?.source||''),kind:'link'})}</div><div class="nx-heading-main"><div class="nx-eyebrow">${source}<span>·</span>${total} 条作品</div><div class="nx-title"><h3 title="${esc(b.workspace.name)}">${esc(b.workspace.name)}</h3></div><div class="nx-counts"><span>已完成 ${ready} / ${total}</span>${counts.running?`<span>${counts.running} 条处理中</span>`:''}${counts.paused?`<span>${counts.paused} 条已暂停</span>`:''}${(counts.failed||0)+(counts.recovery_required||0)?`<span class="nx-attention">${(counts.failed||0)+(counts.recovery_required||0)} 条需处理</span>`:''}</div></div><div class="nx-head-tools"><span class="nx-mode">${icon(cloud?'cloud':'headphones')}${cloud?'云端并行':'本地逐条'}</span><button class="iconbtn nx-collapse" aria-label="${open?'收起':'展开'}批量任务" aria-expanded="${open}" onclick="nativeToggle('${bid}')"><span>${open?'收起':'展开'}</span>${icon('chevron')}</button><button class="iconbtn nx-remove" ${nativeCommandAttrs(bid,'dismiss')} aria-label="移出本批任务" onclick="nativeControl('${bid}','dismiss')">${icon('x')}</button></div></div><div class="nx-track" role="progressbar" aria-label="已完成作品数" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${ready}"><span style="width:${100*ready/Math.max(1,total)}%"></span></div></header><div class="nx-batch-body ${open?'':'is-collapsed'}" aria-hidden="${!open}" ${open?'':'inert'}><div class="nx-selectbar"><label><input type="checkbox" data-native-select-all aria-label="选择本批全部作品" ${b.selectAll&&b.selectedCount===total?'checked':''} onchange="nativeSelect('${bid}',null,this.checked)">全选</label>${controls}</div><div class="nx-member-viewport" data-native-scroll="${bid}" role="region" aria-label="${esc(b.workspace.name)}作品列表，最多显示5条，可滚动" tabindex="0" style="--nx-total:${Math.min(5,b.total)}"><div class="nx-spacer" style="height:calc(var(--nx-row-height) * ${b.offset||0})"></div>${b.items.filter(i=>!i.hidden).map(i=>nativeRow(b,i)).join('')}<div class="nx-spacer" style="height:calc(var(--nx-row-height) * ${Math.max(0,b.total-(b.offset||0)-b.items.length)})"></div></div></div><footer class="nx-footer"><div class="nx-engine"><span class="nx-engine-label">转录模型</span>${nativeModel(b)}<span class="nx-engine-hint">${cloud?'按服务额度自动并行':'共享本地模型，逐条识别'}</span></div><span class="grow"></span>${!active&&b.startable_count&&canResume?`<button class="btn secondary" ${nativeCommandAttrs(bid,'resume')} onclick="nativeControl('${bid}','resume')">继续本批</button>`:''}<button class="btn primary" ${nativeCommandAttrs(bid,batchAction)} ${canControl?'':'disabled'} onclick="nativeControl('${bid}','${batchAction}')">${pending?'处理中…':active&&!counts.queued?'正在处理当前项':!canControl&&paused?'请逐条继续暂停项':!canControl?'暂无待处理项':text}${pending?'':icon('arrow')}</button></footer>${(b.workspace.skipped||[]).length?`<details class="nx-skipped"><summary>${b.workspace.skipped.length} 条非音视频作品已跳过</summary>${b.workspace.skipped.map(i=>`<p>${esc(i.title)} · ${esc(i.reason)}</p>`).join('')}</details>`:''}</article>`;
}
function nativeSingleCard(b){
 const i=b.items[0];if(!i)return '<article class="job task-card nx-single"><p>正在读取任务…</p></article>';
 const [action,label]=nativeAction(i),bid=b.batch_id,pending=nativeIsPending(bid,action,i.index);
 const problem=['failed','interrupted','recovery_required'].includes(i.state)?i.error||i.message:'';
 const live=['queued','running','settling'].includes(i.state);
 return `<article class="job task-card nx-single state-${i.state}" data-native-batch="${bid}" aria-label="${esc(i.title)}"><header class="nx-single-head"><div class="nx-single-source">${platformIcon({platform:i.local_source?'local':identifyPlatform(i.source),kind:i.local_source?'file':'link'})}<div><span>${i.local_source?'本地文件':platformName(identifyPlatform(i.source))}</span><small>单个任务</small></div></div><div class="nx-card-tools"><span class="nx-status-pill ${i.state}">${esc(nativeStateLabel(b,i))}</span>${nativeAssetButton(b,i)}<button class="iconbtn nx-remove" ${nativeCommandAttrs(bid,'dismiss',i.index)} aria-label="移除任务 ${esc(i.title)}" onclick="nativeControl('${bid}','dismiss',${i.index})">${icon('x')}</button></div></header><h3 class="nx-single-title" title="${esc(i.title)}">${esc(i.title)}</h3><section class="nx-single-setting"><div class="nx-settings-panel" aria-hidden="${live}" ${live?'inert':''}><div class="nx-section-heading"><span>提取内容</span>${!i.local_source?nativeSubtitle(b,i):''}</div><div class="nx-single-outputs">${['text','video','cover'].map(k=>nativeOutput(b,i,k)).join('')}</div></div>${nativeProgressPanel(live)}</section>${problem?`<p class="nx-task-message" role="status">${esc(problem)}</p>`:''}<footer class="jobbody nx-single-footer"><div class="nx-single-model"><span class="nx-engine-label">转录模型</span><div class="nx-engine">${nativeModel(b)}</div></div><button class="btn primary" ${nativeCommandAttrs(bid,action,i.index)} ${action==='none'||i.state==='settling'?'disabled':''} onclick="nativeAct('${bid}',${i.index})">${pending?'处理中…':label}${pending?'':icon('arrow')}</button></footer></article>`;
}

function nativeProgressPanel(live){
 return `<div class="nx-live-panel" aria-hidden="${!live}" ${live?'':'inert'}><div class="nx-live-heading"><div class="nx-live-stage-slot"><span class="nx-live-stage-leaving" aria-hidden="true"></span><strong class="nx-live-stage" role="status" aria-live="polite"></strong></div><span class="nx-live-outputs"></span><div class="nx-live-number"><small>阶段进度</small><strong class="nx-live-value"></strong></div></div><div class="nx-live-detail"><span class="nx-live-message"></span><span class="nx-live-elapsed"></span></div><div class="nx-live-track" role="progressbar" aria-label="当前阶段进度" aria-valuemin="0" aria-valuemax="100"><i></i></div><div class="nx-phase-rail" aria-label="提取步骤"></div></div>`;
}
const nativePhaseNames={prepare:'获取',text:'文案',cover:'封面',save:'保存'};
function nativeProgressLabel(b,i){
 if(i.state==='settling')return '正在结束';
 if(i.state==='queued')return '等待处理';
 const cloud=!!modelOptions.find(m=>m.id===b.engine)?.cloud;
 return ({preparing:'准备提取',waiting_media:'等待素材准备',reuse_check:'检查可用素材',download:'获取音视频',conversion:'处理音频',subtitles:'读取平台字幕',transcription:cloud?'云端识别':'本地识别',cover:'获取封面',result:'整理提取内容',saving:'保存所选内容'})[i.activity?.step]||'正在提取';
}
function nativeSyncProgress(card,b,i){
 const panel=card.querySelector('.nx-live-panel');if(!panel)return;
 const live=['queued','running','settling'].includes(i.state),a=i.activity||{};
 if(!live){
  for(const el of panel.querySelectorAll('.nx-live-stage,.nx-live-stage-leaving'))el.getAnimations().forEach(animation=>animation.cancel());
  return;
 }
 panel.querySelector('.nx-live-outputs').textContent=i.selection.outputs.map(k=>NX_LABELS[k]).join(' · ');
 const label=nativeProgressLabel(b,i),node=panel.querySelector('.nx-live-stage'),leaving=panel.querySelector('.nx-live-stage-leaving');
 const key=i.state==='running'?(a.step||'preparing'):i.state;
 if(node.textContent!==label){
  const old=node.textContent;node.textContent=label;
  if(old&&node._phaseKey!==key&&!motionPreference.matches&&node._phaseAnimation?.playState!=='running'){
   leaving.textContent=old;
   leaving.animate([{opacity:1,transform:'translateY(0) rotateX(0)'},{opacity:0,transform:'translateY(-6px) rotateX(-30deg)'}],{duration:160,easing:'cubic-bezier(.23,1,.32,1)'}).onfinish=()=>{leaving.textContent=''};
   node._phaseAnimation=node.animate([{opacity:0,transform:'translateY(6px) rotateX(30deg)'},{opacity:1,transform:'translateY(0) rotateX(0)'}],{duration:180,easing:'cubic-bezier(.23,1,.32,1)'});
  }
 }
 node._phaseKey=key;
 const known=i.state==='running'&&typeof a.fraction==='number'&&Number.isFinite(a.fraction)&&a.fraction>=0&&a.fraction<=1;
 const track=panel.querySelector('.nx-live-track'),percent=Math.round(100*(known?a.fraction:0));
 panel.querySelector('.nx-live-value').textContent=known?percent+'%':i.state==='queued'?'排队中':'进行中';
 panel.querySelector('.nx-live-number').classList.toggle('is-unknown',!known);
 if(known)track.setAttribute('aria-valuenow',String(percent));else track.removeAttribute('aria-valuenow');
 track.setAttribute('aria-valuetext',known?label+' '+percent+'%':label+'，暂无精确百分比');
 track.classList.toggle('is-unknown',!known);
 track.querySelector('i').style.transform=`scaleX(${known?a.fraction:.18})`;
 const detail=panel.querySelector('.nx-live-message');detail.textContent=i.state==='settling'?'已有内容会保留':i.message||'正在准备所选内容';detail.title=detail.textContent;
 panel._startedAt=a.started_at;
 nativeProgressClock(panel);
 const steps=a.steps||['prepare',...(i.selection.outputs.includes('text')?['text']:[]),...(i.selection.outputs.includes('cover')?['cover']:[]),'save'];
 const rail=panel.querySelector('.nx-phase-rail');
 if(rail._steps!==steps.join(',')){rail.innerHTML=steps.map(step=>`<span class="nx-phase" data-progress-phase="${step}"><i></i><span>${nativePhaseNames[step]}</span></span>`).join('');rail._steps=steps.join(',')}
 const current=steps.indexOf(a.stage||'prepare');
 for(const [index,phase] of [...rail.children].entries()){
  const state=i.assets[steps[index]]?.state,failed=state==='failed',unknown=state==='unknown',passed=index<current&&!failed&&!unknown;
  const active=index===current&&i.state!=='queued';phase.classList.toggle('is-current',active);phase.classList.toggle('is-passed',passed);
  phase.classList.toggle('is-failed',failed);phase.classList.toggle('is-uncertain',unknown);
  phase.setAttribute('aria-label',nativePhaseNames[steps[index]]+(failed?'未完成':unknown?'结果待核对':passed?'已完成':active?'正在处理':'待处理'));
  if(active)phase.setAttribute('aria-current','step');else phase.removeAttribute('aria-current');
  phase.querySelector('i').textContent=failed?'!':unknown?'?':passed?'✓':String(index+1);
 }
}
function nativeProgressClock(panel){
 const clock=panel.querySelector('.nx-live-elapsed'),text=panel._startedAt?'已用时 '+clockText(Math.max(0,Math.floor(Date.now()/1000-panel._startedAt))):'';
 if(clock.textContent!==text)clock.textContent=text;
}
// The elapsed clock must keep working when an engine has no new progress event.
setInterval(()=>{if(!document.hidden)document.querySelectorAll('.nx-live-panel[aria-hidden=false]').forEach(nativeProgressClock)},1000);
function nativeToggle(id){
 const open=nativeExpanded.has(id);open?nativeExpanded.delete(id):nativeExpanded.add(id);
 const card=document.querySelector(`[data-native-batch="${id}"]`),body=card?.querySelector('.nx-batch-body'),button=card?.querySelector('.nx-collapse');if(!body||!button)return;
 nativeCloseDetails();nativeCloseModels();body.classList.toggle('is-collapsed',!open);body.setAttribute('aria-hidden',String(!open));body.inert=!open;
 button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',`${open?'收起':'展开'}批量任务`);button.querySelector('span').textContent=open?'收起':'展开';
}
let nativeLastFocus=null;
function nativePostRender(){
 document.querySelectorAll('[data-native-select-all]').forEach(el=>{const b=nxBatch(el.closest('[data-native-batch]').dataset.nativeBatch);el.indeterminate=!!b&&b.selectedCount>0&&b.selectedCount<b.visible_total});
 document.querySelectorAll('[data-native-scroll]').forEach(el=>{
  const bid=el.dataset.nativeScroll,batch=nxBatch(bid);
  if(batch?.focus_token&&nativeLastFocus!==batch.focus_token){nativeExpanded.delete(bid);nativeLastFocus=batch.focus_token;el.scrollTop=batch.focus_position*(parseFloat(getComputedStyle(el).getPropertyValue('--nx-row-height'))||122);nativeScrolls.set(bid,el.scrollTop);el.closest('article').scrollIntoView({block:'center'});el.focus({preventScroll:true})}
  else if(nativeScrolls.has(bid))el.scrollTop=nativeScrolls.get(bid);
  el.onscroll=()=>{
   nativeScrolls.set(bid,el.scrollTop);
   const height=parseFloat(getComputedStyle(el).getPropertyValue('--nx-row-height'))||122;
   const page=Math.max(0,(Math.floor(el.scrollTop/height/25)-1)*25),b=nxBatch(bid);
   if(b&&page!==(b.offset||0)&&nativeRequestedPages.get(bid)!==page){nativeRequestedPages.set(bid,page);act('batch_page',{batch_id:bid,offset:page});}
  };
 });
}
document.addEventListener('pointerdown',e=>{
 const pop=document.getElementById('nx-model-popover'),detail=document.getElementById('nx-action-popover');
 if(pop&&!pop.contains(e.target)&&!e.target.closest('.nx-model'))nativeCloseModels();
 if(detail&&!detail.contains(e.target)&&!e.target.closest('[data-native-detail],[data-native-assets]'))nativeCloseDetails();
},true);
document.addEventListener('focusin',e=>{
 const pop=document.getElementById('nx-model-popover'),detail=document.getElementById('nx-action-popover');
 if(pop&&!pop.contains(e.target)&&!e.target.closest('.nx-model'))nativeCloseModels();
 if(detail&&!detail.contains(e.target)&&!e.target.closest('[data-native-detail],[data-native-assets]'))nativeCloseDetails();
});
document.addEventListener('keydown',e=>{
 const detail=document.getElementById('nx-action-popover');
 if(detail){
  if(e.key==='Escape'){e.preventDefault();nativeCloseDetails(true);return}
  if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
   const buttons=[...detail.querySelectorAll('button')],index=buttons.indexOf(document.activeElement),next=e.key==='Home'?0:e.key==='End'?buttons.length-1:index<0?0:(index+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;
   e.preventDefault();buttons[next]?.focus();return;
  }
 }
 const pop=document.getElementById('nx-model-popover');if(!pop)return;
 if(e.key==='Escape'){e.preventDefault();nativeCloseModels(true);return}
 if(e.target.matches('[role=tab]')&&['ArrowLeft','ArrowRight'].includes(e.key)){
  e.preventDefault();nativeModels(pop.dataset.batch,nativeModelMenu.tab==='local'?'cloud':'local');return;
 }
 if(!['ArrowDown','ArrowUp','Home','End'].includes(e.key))return;
 const options=[...pop.querySelectorAll('.model-option')];if(!options.length)return;
 e.preventDefault();const index=options.indexOf(document.activeElement),next=e.key==='Home'?0:e.key==='End'?options.length-1:index<0?0:(index+(e.key==='ArrowDown'?1:-1)+options.length)%options.length;
 options[next].focus();
});
window.addEventListener('resize',()=>{nativeCloseModels();nativeCloseDetails()});
document.getElementById('main').addEventListener('scroll',()=>{nativeCloseModels();nativeCloseDetails()},true);

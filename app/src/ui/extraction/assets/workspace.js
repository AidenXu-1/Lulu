const $=s=>document.querySelector(s);
let skippedGroups=[],tasks=[],filter='all',page='extract',urlText='',recording=false,source='麦克风',recordFolder='',recSeconds=0;
let modelOptions=[],chosenModel='',modelMenuOpen=false,modelTab='local',modalTaskTarget=null,folderTarget=null;
let expanded=new Set(),bridge=null,sequence=0,pending=new Map(),toastTimer,previousFocus;
paths.monitor='M3 4h18v12H3z M12 16v5 M8 21h8';paths.mix='M3 8h3l4 8h4l4-8h3 M3 16h3l4-8h4l4 8h3';
paths.image='M3 3h18v18H3z M3 16l5-5 5 5 3-3 5 5 M15 7h.01';paths.info='M12 11v6 M12 7h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0';paths.refresh='M20 5v5h-5 M4 19v-5h5 M5 8a8 8 0 0 1 14-3l1 5 M4 14l1 5a8 8 0 0 0 14-3';
const platformName=p=>({douyin:'抖音',bilibili:'B站',xiaohongshu:'小红书',youtube:'YouTube',xiaoyuzhou:'小宇宙',local:'本地文件',record:'录音',podcast:'播客',other:'作品链接'})[p]||'作品链接';
const isLocal=t=>['file','record'].includes(t.kind)||t.editableOutputs===false,isEditable=t=>!t.readOnly&&!t.savePending&&['waiting','cancelled','error'].includes(t.state);
const isRecoverable=t=>!t.readOnly&&!t.savePending&&t.state==='queued'&&t.recovered;
const isStartable=t=>isEditable(t)||isRecoverable(t);
const isInQueue=t=>!t.recovered&&['queued','running'].includes(t.state);
const validOutput=t=>Object.values(t.outputs).some(Boolean),hasAssets=o=>o.video||o.cover;
const modelLabel=id=>modelOptions.find(m=>m.id===id)?.name||id||'转录模型';
const clockText=n=>`${String(Math.floor((n||0)/60)).padStart(2,'0')}:${String(Math.floor((n||0)%60)).padStart(2,'0')}`;
const recordTime=()=>clockText(recSeconds);
const taskStatusLabel=t=>t.state==='queued'&&t.recovered?'已暂停':t.state==='done'&&t.error?'部分完成':({waiting:'待开始',cancelled:'已取消',error:'待重试',queued:'排队中',running:'处理中',done:'已完成'})[t.state];
const taskStatus=t=>`<span class="task-status ${t.state}" ${t.error?`role="button" tabindex="0" onclick="showError(${t.id})" title="${esc(t.error)}"`:''}><i></i>${taskStatusLabel(t)}</span>`;
function call(action,args={}){return new Promise((resolve,reject)=>{if(!bridge){reject(new Error('界面正在准备'));return}const id=String(++sequence);pending.set(id,{resolve,reject});bridge.request(id,action,JSON.stringify(args))})}
function act(action,args={}){return call(action,args).catch(e=>toast(e.message))}
function targetsFor(target){const [kind,id]=target.split(':'),t=tasks.find(x=>x.id===Number(id));return !t?[]:kind==='g'?tasks.filter(x=>x.group===t.group&&isEditable(x)):isEditable(t)?[t]:[]}
const targetRepresentative=target=>targetsFor(target)[0];
function pickFiles(){act('files')}
function stageLink(text){urlText=text;const input=$('#url');input.value=text;$('#urlError').hidden=true;input.focus()}
function grab(e){e.preventDefault();if(!/https?:\/\/[^\s]+/.test(urlText)){$('#urlError').textContent='请粘贴有效的作品链接';$('#urlError').hidden=false;return}call('link',{text:urlText}).then(()=>{urlText='';$('#url').value='';$('#urlError').hidden=true}).catch(e=>toast(e.message))}
function changeChoice(target,key,value){act('choice',{ids:targetsFor(target).map(t=>t.id),key,value})}
function chooseFolder(kind){act('folder',{kind,ids:kind==='task'?targetsFor(folderTarget).map(t=>t.id):[]})}
function setSource(value){act('record_source',{value})}
function toggleRecord(){act('record')}
function cancelRecord(){act('record_cancel')}
function start(id){act('start',{ids:[id]})}
function startAll(group=null){
 const scope=tasks.filter(t=>!group||t.group===group),resume=scope.some(isRecoverable);
 act('start',{ids:scope.filter(t=>(resume?isRecoverable(t):isStartable(t))&&validOutput(t)).map(t=>t.id)});
}
function cancel(id){act('cancel',{ids:[id]})}
function deleted(ids){call('delete',{ids}).then(()=>{toast('已移除任务');const b=document.createElement('button');b.textContent='撤销';b.onclick=()=>{act('undo_delete');$('#toast').hidden=true};$('#toast').append(b);clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6000)}).catch(e=>toast(e.message))}
function removeTask(id){deleted([id])}
function removeTaskGroup(id){const t=tasks.find(x=>x.id===id);if(t)deleted(tasks.filter(x=>x.group===t.group).map(x=>x.id))}
function toggleGroup(id){const g=tasks.find(t=>t.id===id)?.group;if(expanded.has(g))expanded.delete(g);else expanded.add(g);renderBoard();act('expanded',{id,expanded:expanded.has(g)})}
function toast(text){clearTimeout(toastTimer);$('#toast').textContent=text;$('#toast').hidden=false;toastTimer=setTimeout(()=>$('#toast').hidden=true,4000)}
function openModal(title,content,kind=''){
 if($('#modalback').hidden)previousFocus=document.activeElement;
 modelMenuOpen=false;renderModelPicker();
 $('.modal').dataset.kind=kind;$('#modal-title').textContent=title;$('#modal-content').innerHTML=content;
 $('#modalback').hidden=false;$('#main').inert=true;$('#modalback button').focus();
}
function closeModal(){
 if($('#modalback').hidden)return;
 modalTaskTarget=null;$('#modalback').hidden=true;$('#main').inert=false;
 if(previousFocus?.isConnected)previousFocus.focus({preventScroll:true});
}
function showError(id){const t=tasks.find(x=>x.id===id);if(t)openModal('本次未完成',`<p>${esc(t.error||t.message)}</p>${t.hasResult?`<button class="btn secondary" onclick="showResult(${id})">查看已保存结果</button>`:''}`)}
function resultAction(id,action){
 const content=$('#modal-content').firstElementChild;
 call('result',{id,action}).then(()=>{
  if(['view','voice','retry_missing'].includes(action)&&$('#modal-content').firstElementChild===content)closeModal();
 }).catch(e=>toast(e.message));
}
function showResult(id){
 const t=tasks.find(x=>x.id===id);if(!t)return;
 const command=action=>`resultAction(${id},'${action}')`;
 const media=[['video','视频','play'],['cover','封面','image']].filter(([key])=>t.assets?.[key]);
 const document=t.hasDocument?`<section class="result-document" aria-label="文稿操作">
  <div class="result-document-head"><span class="result-document-icon">${icon('file')}</span><div><h3>文稿</h3><p>${t.documentId?'已存入文稿库':'已完成转录'}</p></div><button class="btn primary" onclick="${command('view')}">查看文稿${icon('arrow')}</button></div>
  <div class="result-document-tools"><button onclick="${command('copy')}">${icon('copy')}复制文案</button><button onclick="${command('export')}">${icon('upload')}导出文案</button></div>
 </section><section class="result-destinations" aria-label="继续使用文稿">
  <button onclick="${command('obsidian')}"><img src="assets/obsidian.png" alt="">存入 Obsidian</button><button onclick="${command('feishu')}"><img src="assets/feishu.png" alt="">存入飞书</button><button onclick="${command('voice')}">${icon('mic')}带入文案配音</button>
 </section>`:'';
 const files=media.length?`<section class="result-files" aria-label="已保存的素材">${media.map(([key,label,glyph])=>`<button class="result-file" onclick="${command(key)}" aria-label="在访达中显示${label}"><span class="result-file-icon">${icon(glyph)}</span><span><strong>${label}</strong><small>在访达中显示</small></span>${icon('folder')}</button>`).join('')}</section>`:'';
 const warning=t.error?`<section class="result-warning" role="status"><div>${icon('info')}<strong>部分内容未完成</strong></div><p>${esc(t.error)}</p>${t.state==='done'&&!t.readOnly?`<button class="textbtn" onclick="${command('retry_missing')}">${icon('refresh')}重试未完成的内容</button>`:''}</section>`:'';
 openModal(t.name,`<div class="result-assets"><div class="result-overview"><span class="result-state ${t.error?'partial':''}">${icon(t.error?'info':'check')}${t.error?'部分完成':'提取完成'}</span><span>${esc(platformName(t.platform))}</span>${t.elapsed?`<span>用时 ${clockText(t.elapsed)}</span>`:''}</div>${document}${files}${warning}${!document&&!files&&!warning?'<p class="result-empty">暂无可查看的结果</p>':''}</div>`,'result');
}
function positionWorkbench(){const w=$('#workbench'),h=$('.pagehead'),error=$('#startup-error'),space=h.offsetHeight+parseFloat(getComputedStyle(h).marginBottom)+(error.hidden?0:error.offsetHeight+parseFloat(getComputedStyle(error).marginBottom));const offset=!tasks.length&&!skippedGroups.length?Math.max(0,($('#main').clientHeight-w.offsetHeight)/2-parseFloat(getComputedStyle($('#workspace')).paddingTop)-space-14):0;w.style.transform=`translateY(${offset}px)`}
function updateLayout(){$('#workspace').classList.toggle('empty-workspace',!tasks.length&&!skippedGroups.length);$('#board').hidden=!tasks.length&&!skippedGroups.length;requestAnimationFrame(positionWorkbench)}
function progressCard(t){const done=t.state==='done',running=t.state==='running',ahead=tasks.slice(0,tasks.indexOf(t)).filter(isInQueue).length;
return `<article class="job task-card process-card ${t.state}" data-job="${t.id}" aria-label="${esc(t.name)}">${deleteButton(t)}<div class="process-source">${platformIcon(t)}<span>${taskDescription(t)}</span><span class="process-status ${t.state}"><i></i>${taskStatusLabel(t)}</span></div><div class="jobtitle" title="${esc(t.name)}">${esc(t.name)}</div><div class="process-outputs">${outputNames(t.outputs).map(n=>`<span>${n}</span>`).join('')}</div>${running?`<div class="live-progress"><div class="live-heading"><div><strong class="live-stage" role="status">${esc(t.message)}</strong><small class="live-detail">${t.outputs.text?'模型 · '+esc(modelLabel(t.model)):'获取所选素材'}</small></div><div class="live-number"><small>当前阶段</small><strong></strong><span></span></div></div><div class="live-track" role="progressbar" aria-label="处理进度"><i></i></div><div class="phase-rail">${['获取',...(t.outputs.text?['转录']:[]),'保存'].map(n=>`<div class="phase"><div class="phase-caption"><i></i><span>${n}</span></div><div class="phase-track"><span></span></div></div>`).join('')}</div></div><div class="process-footer"><span class="elapsed">已用时 <b>${clockText(t.elapsed)}</b></span><button class="textbtn" onclick="cancel(${t.id})">取消</button></div>`:done?`<div class="finished-result"><span class="finish-stamp">${icon('check')}</span><div><strong>${t.error?'已保留完成的内容':t.documentId?'文案已存入文稿库':t.hasDocument?'文案已完成':'素材已保存'}</strong><small>${[t.assets?.video?'视频已保存':'',t.assets?.cover?'封面已保存':''].filter(Boolean).join(' · ')||'可以查看与整理文稿'}</small></div></div><div class="process-footer"><span class="elapsed">${t.elapsed?'用时 <b>'+clockText(t.elapsed)+'</b>':''}</span><button class="btn secondary" onclick="showResult(${t.id})">查看结果${icon('arrow')}</button></div>`:`<div class="queue-body">${t.recovered?'':`<span class="queue-number">${ahead}<small>个任务在前</small></span>`}<span class="queue-hint">${t.recovered?esc(t.message||'上次排队未完成，点击继续'):'轮到后自动开始'}</span></div><div class="process-footer queue-footer">${t.recovered?`<button class="textbtn" onclick="start(${t.id})">继续</button>`:''}<button class="textbtn" onclick="cancel(${t.id})">取消排队</button></div>`}</article>`}
function updateProgressView(t){const el=document.querySelector(`[data-job="${t.id}"]`);if(!el)return;const label=el.querySelector('.live-stage');if(label){label.textContent=t.message;const known=t.progress>=0,n=el.querySelector('.live-number'),track=el.querySelector('.live-track');n.classList.toggle('unknown',!known);n.querySelector('strong').textContent=known?Math.round(t.progress*100):'处理中';n.querySelector('span').textContent=known?'%':'';track.classList.toggle('unknown',!known);if(known)track.setAttribute('aria-valuenow',Math.round(t.progress*100));else track.removeAttribute('aria-valuenow');track.querySelector('i').style.width=(known?t.progress*100:30)+'%';const phase=/transcription|local_service/.test(t.stage)?'转录':/result|saving/.test(t.stage)?'保存':'获取';let passed=true;el.querySelectorAll('.phase').forEach((p,i)=>{const name=p.querySelector('.phase-caption span').textContent,current=name===phase;if(current)passed=false;p.className='phase '+(current?'current':passed?'finished':'');p.querySelector('i').innerHTML=passed?icon('check'):i+1;p.querySelector('.phase-track span').style.width=passed?'100%':current?'30%':'0%'});el.querySelector('.elapsed b').textContent=clockText(t.elapsed)}el.querySelectorAll('.member-state,.member-state-inline').forEach(n=>n.textContent=t.message||taskStatusLabel(t))}
function renderModelPicker(){
 const m=modelOptions.find(x=>x.id===chosenModel),list=modelOptions.filter(x=>Boolean(x.cloud)===(modelTab==='cloud'));
 const focused=document.activeElement,focusId=focused?.id,focusModel=focused?.dataset.modelId,scroll=$('#model-options')?.scrollTop||0;
 $('#model-picker').innerHTML=`<button class="model-switch" id="model-switch" aria-haspopup="dialog" aria-controls="model-menu" aria-expanded="${modelMenuOpen}" onclick="toggleModelMenu()"><span class="model-switch-label">转录模型</span><span class="ready-dot ${m?.ready?'':'unknown'}"></span><strong title="${esc(modelLabel(chosenModel))}">${esc(modelLabel(chosenModel))}</strong><span class="model-chevron"><svg viewBox="0 0 16 16"><path d="m4 6 4 4 4-4"/></svg></span></button>
 <div class="model-menu" id="model-menu" role="dialog" aria-label="选择转录模型" ${modelMenuOpen?'':'hidden'}>
  <div class="model-tabs" role="tablist" aria-label="模型运行位置">${[['local','本地','monitor'],['cloud','云端','cloud']].map(([key,label,glyph])=>`<button id="model-tab-${key}" role="tab" aria-selected="${modelTab===key}" aria-controls="model-panel" tabindex="${modelTab===key?0:-1}" onclick="switchModelTab('${key}')">${icon(glyph)}${label}</button>`).join('')}</div>
  <div class="model-location-note" id="model-location-note">${modelTab==='cloud'?'音频会上传至所选服务商':'音频在这台 Mac 上处理'}</div>
  <div id="model-panel" role="tabpanel" aria-labelledby="model-tab-${modelTab}"><div class="model-options" id="model-options" role="menu" aria-label="${modelTab==='cloud'?'云端':'本地'}转录模型" aria-describedby="model-location-note">${list.map(item=>{
   const clean=item.name.replace(/\s*[（(]云端[）)]/g,''),parts=clean.split(' · '),name=item.cloud?parts[0]:clean,detail=item.cloud?(parts.slice(1).join(' · ')||'云端 API'):(item.checking?'正在检查模型…':item.issue||(item.ready?'已就绪，可直接使用':'准备好模型后即可使用'));
   return `<button class="model-option ${item.id===chosenModel?'selected':''}" data-model-id="${esc(item.id)}" role="menuitemradio" aria-checked="${item.id===chosenModel}" onclick="selectModel('${esc(item.id)}')"><span class="model-option-copy"><strong title="${esc(clean)}">${esc(name)}</strong><small>${esc(detail)}</small></span><span class="model-availability ${item.ready?'ready':''}" ${item.id===chosenModel?'aria-label="当前模型"':''}>${item.id===chosenModel?icon('check'):!item.cloud&&!item.ready?(item.checking?'检查中':item.issue?'待检查':'需准备'):''}</span></button>`;
  }).join('')||'<p class="model-empty">暂无可选模型</p>'}</div></div>
  <div class="model-menu-footer"><span>仅对未开始的任务生效</span><button class="textbtn" onclick="closeModelMenu();act('models')">管理模型${icon('arrow')}</button></div>
 </div>`;
 if(modelMenuOpen){$('#model-options').scrollTop=scroll;const target=focusModel?[...document.querySelectorAll('.model-option')].find(n=>n.dataset.modelId===focusModel):focusId?document.getElementById(focusId):null;target?.focus({preventScroll:true})}
}
function closeModelMenu(restoreFocus=false){modelMenuOpen=false;renderModelPicker();if(restoreFocus)$('#model-switch').focus({preventScroll:true})}
function toggleModelMenu(){modelMenuOpen=!modelMenuOpen;if(modelMenuOpen)modelTab=modelOptions.find(x=>x.id===chosenModel)?.cloud?'cloud':'local';renderModelPicker();if(modelMenuOpen)$(`#model-tab-${modelTab}`).focus()}
function switchModelTab(tab){modelTab=tab;renderModelPicker();$('#model-options').scrollTop=0;$(`#model-tab-${tab}`).focus({preventScroll:true})}
function selectModel(id){call('model',{id}).then(()=>closeModelMenu(true)).catch(e=>toast(e.message))}
let structure='',intakeState='',modelsState='',receivedSnapshot=false;
const seenTaskIds=new Set();
function renderQueueSync(s){
 const notice=$('#queue-sync'),pending=Boolean(s.queueSyncPending),hidden=!s.queueSyncError&&!pending;
 const text=pending?'正在核对保存记录…':'任务记录尚未同步，输入和已生成内容已保留在本机。';
 const changed=notice.hidden!==hidden||notice.querySelector('span').textContent!==text;
 notice.hidden=hidden;notice.querySelector('span').textContent=text;
 const button=notice.querySelector('button');button.disabled=pending;button.textContent=pending?'正在同步…':'重新同步';
 if(changed)requestAnimationFrame(positionWorkbench);
}
function receive(s){renderQueueSync(s);const error=$('#startup-error'),reason=s.startupError||'';if(error.textContent!==reason){error.textContent=reason;error.hidden=!reason;requestAnimationFrame(positionWorkbench)}const added=new Set(receivedSnapshot?s.tasks.filter(t=>t.animateNew!==false&&!seenTaskIds.has(t.id)).map(t=>t.id):[]);s.tasks.forEach(t=>seenTaskIds.add(t.id));receivedSnapshot=true;expanded=new Set(s.expanded||[]);skippedGroups=s.skippedGroups||[];const next=JSON.stringify(skippedGroups)+JSON.stringify(s.tasks.map(t=>[t.id,t.state,t.name,t.outputs,t.subs,t.folder,t.group,t.groupName,t.error,t.documentId,t.assets,t.recovered,t.recovered?t.message:null,t.readOnly,t.editableOutputs,t.savePending,t.skipped,t.displayOrder,(s.expanded||[]).includes(t.group)]));tasks=s.tasks;const rec=JSON.stringify([s.recording,s.source,s.recordFolder]);recording=s.recording;source=s.source;recordFolder=s.recordFolder;recSeconds=s.recSeconds;if(rec!==intakeState){intakeState=rec;renderIntake()}else if($('#recTime'))$('#recTime').textContent='● '+recordTime();const mods=JSON.stringify([s.models,s.model]);modelOptions=s.models;chosenModel=s.model;if(mods!==modelsState){modelsState=mods;renderModelPicker()}if(next!==structure){structure=next;const active=document.activeElement,target=active?.closest('[data-choice-target]')?.dataset.choiceTarget,label=active?.getAttribute('aria-label'),scrolls=[...document.querySelectorAll('[data-group] .members')].map(x=>[x.closest('[data-group]').dataset.group,x.scrollTop]);renderBoard(added);for(const [id,top] of scrolls){const el=document.querySelector(`[data-group="${id}"] .members`);if(el)el.scrollTop=top}if(target&&label)document.querySelector(`[data-choice-target="${target}"] [aria-label="${label}"]`)?.focus({preventScroll:true});if(modalTaskTarget&&!$('#modalback').hidden)renderTaskEditor(modalTaskTarget)}tasks.filter(t=>t.state==='running').forEach(updateProgressView)}
let backdropPress=null;
$('#modalback').addEventListener('pointerdown',e=>{backdropPress=e.target===$('#modalback')});
$('#modalback').addEventListener('click',e=>{if(e.target===$('#modalback')&&backdropPress!==false)closeModal();backdropPress=null});
document.addEventListener('click',e=>{if(modelMenuOpen&&!e.target.closest('#model-picker'))closeModelMenu()},true);
document.addEventListener('focusin',e=>{if(modelMenuOpen&&!e.target.closest('#model-picker'))closeModelMenu()});
document.addEventListener('keydown',e=>{
 if(['Enter',' '].includes(e.key)&&e.target.matches('.task-status[role=button]')){e.preventDefault();e.target.click()}
 if(e.key==='Escape'){if(!$('#modalback').hidden)closeModal();else if(modelMenuOpen)closeModelMenu(true)}
 if(!$('#modalback').hidden&&e.key==='Tab'){
  const items=[...$('#modalback').querySelectorAll('button,input,[tabindex="0"]')].filter(n=>!n.disabled&&n.getClientRects().length),a=items[0],b=items.at(-1);
  if(e.shiftKey&&document.activeElement===a){e.preventDefault();b?.focus()}else if(!e.shiftKey&&document.activeElement===b){e.preventDefault();a?.focus()}
 }
 if(!modelMenuOpen||!e.target.closest('#model-picker'))return;
 if(e.target.matches('[role=tab]')&&['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){
  e.preventDefault();switchModelTab(e.key==='Home'?'local':e.key==='End'?'cloud':modelTab==='local'?'cloud':'local');
 }else if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){
  e.preventDefault();const list=[...document.querySelectorAll('.model-option')],i=list.indexOf(document.activeElement);
  const next=e.key==='Home'?0:e.key==='End'?list.length-1:i<0?(e.key==='ArrowUp'?list.length-1:0):(i+(e.key==='ArrowDown'?1:-1)+list.length)%list.length;
  list[next]?.focus();
 }
});
new ResizeObserver(positionWorkbench).observe($('#main'));new ResizeObserver(positionWorkbench).observe($('#workbench'));
requestAnimationFrame(()=>requestAnimationFrame(()=>$('#workspace').classList.add('motion-ready')));
new QWebChannel(qt.webChannelTransport,channel=>{bridge=channel.objects.extraction;bridge.reply.connect((id,raw)=>{const p=pending.get(id);pending.delete(id);if(!p)return;const r=JSON.parse(raw);r.ok?p.resolve(r.value):p.reject(new Error(r.message))});bridge.snapshot.connect(raw=>receive(JSON.parse(raw)));bridge.notice.connect(toast);bridge.linkInput.connect(stageLink);bridge.ready()});
window.extraction={receive};

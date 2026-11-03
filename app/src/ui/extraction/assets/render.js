// V24 display structures. Native task snapshots are the sole execution state.
const paths={headphones:'M4 14v-3a8 8 0 0 1 16 0v3 M4 12H3v7h4v-7H4 M20 12h1v7h-4v-7h3',grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M9 22h6',file:'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',wave:'M3 10v4 M7 6v12 M12 3v18 M17 7v10 M21 10v4',trash:'M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',box:'m12 3 9 5-9 5-9-5z M3 8v10l9 4 9-4V8 M12 13v9',book:'M3 4h6l3 3 3-3h6v16h-6l-3 2-3-2H3z M12 7v15',cloud:'M7 18H5a4 4 0 0 1 0-8 7 7 0 0 1 13-2 5 5 0 0 1 1 10h-2 M12 12v9 M9 18l3 3 3-3',shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',link:'M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2 M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',upload:'M12 16V3 M7 8l5-5 5 5 M4 15v6h16v-6',settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',folder:'M3 6h7l2 3h9v11H3z',play:'m8 4 12 8-12 8z',expand:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5',search:'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',check:'m5 12 4 4L19 6',x:'m6 6 12 12 M18 6 6 18',arrow:'M5 12h14 M13 6l6 6-6 6',copy:'M8 8h12v13H8z M4 16H2V2h13v3',clock:'M12 7v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',chevron:'m6 9 6 6 6-6'};
const icon=n=>`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[n]||paths.file}"/></svg>`;
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function identifyPlatform(url){
  let host='';try{host=new URL(url).hostname.toLowerCase()}catch{}
  if(host==='b23.tv'||host==='bilibili.com'||host.endsWith('.bilibili.com'))return 'bilibili';
  if(host==='douyin.com'||host.endsWith('.douyin.com')||host==='iesdouyin.com'||host.endsWith('.iesdouyin.com'))return 'douyin';
  if(host==='xiaohongshu.com'||host.endsWith('.xiaohongshu.com')||host==='xhslink.com'||host.endsWith('.xhslink.com'))return 'xiaohongshu';
  if(host==='youtube.com'||host.endsWith('.youtube.com')||host==='youtu.be')return 'youtube';
  if(host==='xiaoyuzhoufm.com'||host.endsWith('.xiaoyuzhoufm.com'))return 'xiaoyuzhou';
  return 'other';
}

function taskDescription(t){return `${platformName(t.platform)}${t.kind==='homepage'?' · 主页采集':''}${t.duration==='待识别'?'':' · '+esc(t.duration)}`}

function renderTaskEditor(target){
  const t=targetRepresentative(target);if(!t)return;
  $('#task-editor').innerHTML=`<div class="editor-title">${platformIcon(t)}<div><strong>${esc(t.name)}</strong><small>${platformName(t.platform)}</small></div></div>${choiceControls(t,target)}<div class="editor-folder">${cardFolder(t,target)}</div><div class="modalfooter"><button class="btn primary" onclick="modalTaskTarget=null;closeModal()">完成</button></div>`;
}

function editTaskOutputs(id){modalTaskTarget='t:'+id;openModal('本条作品的设置','<div id="task-editor"></div>');renderTaskEditor(modalTaskTarget)}

function chooseTaskFolder(target){folderTarget=target;chooseFolder('task')}

function platformIcon(t){
  const src={douyin:'douyin-official.png',bilibili:'bilibili-official.ico',xiaohongshu:'xiaohongshu-official.png',youtube:'youtube-official.png',xiaoyuzhou:'xiaoyuzhou-official.png'}[t.platform];
  return `<div class="platform-icon ${t.platform}">${src?`<img src="assets/${src}" alt="${platformName(t.platform)}">`:icon(t.kind==='record'?'mic':t.kind==='file'?'wave':'link')}</div>`;
}

function deleteButton(t){return t.readOnly&&!['done','error','cancelled'].includes(t.state)?'':`<button class="task-remove" title="移除任务卡片，保留已保存的内容" aria-label="移除任务 ${esc(t.name)}" onclick="removeTask(${t.id})">${icon('x')}</button>`}

function subtitleControl(t,target){
  const people=target.startsWith('g:')?targetsFor(target):[t],hasText=people.some(x=>x.outputs.text);
  const mixed=people.some(x=>x.subs!==t.subs),on=t.subs&&!mixed;
  return `<button class="subtitle-pill ${on?'on':''}" aria-label="优先字幕" aria-pressed="${mixed?'mixed':on}" ${hasText?'':'disabled'} title="${hasText?'有可用字幕时优先使用，否则转录音视频':'选择文案后可设置'}" onclick="changeChoice('${target}','subs',${!on})"><span class="subtitle-symbol" aria-hidden="true">CC</span>优先字幕</button>`;
}

function choiceControls(t,target,includeSubtitle=true){
  const people=target.startsWith('g:')?targetsFor(target):[t],o=t.outputs;
  const mixed=key=>people.some(x=>x.outputs[key]!==o[key]);
  const toggle=(key,label)=>`<label class="task-check ${o[key]&&!mixed(key)?'selected':''} ${mixed(key)?'mixed':''}"><input type="checkbox" aria-label="${label}" ${mixed(key)?'aria-checked="mixed"':''} ${o[key]&&!mixed(key)?'checked':''} onchange="changeChoice('${target}','${key}',this.checked)"><span class="check-mark">${mixed(key)?'−':icon('check')}</span>${label}</label>`;
  return `<div class="task-choices" data-choice-target="${target}" role="group" aria-label="提取内容">${includeSubtitle?`<div class="editor-subtitles">${subtitleControl(t,target)}</div>`:''}<div class="equal-options">${toggle('text','文案')}${toggle('video','视频')}${toggle('cover','封面')}</div></div>`;
}

function cardFolder(t,target){return hasAssets(t.outputs)?`<button class="folder-mini" aria-label="设置本任务保存位置" aria-description="视频和封面的素材目录：${esc(t.folder)}；文案自动进入文稿库" title="视频 / 封面保存到：${esc(t.folder)}；文案进入文稿库" onclick="chooseTaskFolder('${target}')">${icon('folder')}</button>`:''}

function jobCard(t){
  if(['running','queued','done'].includes(t.state))return progressCard(t);
  const edit=isEditable(t),running=t.state==='running',done=t.state==='done',target='t:'+t.id;
  return `<article class="job task-card ${t.state}" data-job="${t.id}" aria-label="${esc(t.name)}">
    ${deleteButton(t)}<div class="card-source">${platformIcon(t)}<span>${taskDescription(t)}</span></div>
    <div class="task-title-line"><div class="jobtitle" title="${esc(t.name)}">${esc(t.name)}</div>${edit&&!isLocal(t)?subtitleControl(t,target):''}</div>
    ${edit&&!isLocal(t)?choiceControls(t,target,false):`<div class="card-output"><span>提取内容</span>${outputNames(t.outputs).map(n=>`<b>${n}</b>`).join('')}</div>`}
    <div class="jobbody">${running?`<div class="grow"><div class="progress-label"><span>${taskStage(t)}</span><span>${t.progress}%</span></div><div class="track"><i style="width:${t.progress}%"></i></div></div><button class="textbtn" onclick="cancel(${t.id})">取消</button>`
    :done?`${taskStatus(t)}<span class="grow"></span><button class="btn secondary" onclick="showResult(${t.id})">查看结果${icon('arrow')}</button>`
    :t.state==='queued'?`${taskStatus(t)}<span class="grow"></span><button class="textbtn" onclick="cancel(${t.id})">取消排队</button>`
    :`${!validOutput(t)?'<span class="choice-warning">请选择内容</span>':taskStatus(t)}<span class="grow"></span>${edit&&!isLocal(t)?cardFolder(t,target):''}<button class="btn primary task-start" ${validOutput(t)&&!t.readOnly?'':'disabled'} onclick="start(${t.id})">${t.readOnly?'历史任务':t.savePending?'保存结果':t.state==='waiting'?'开始处理':'重新开始'}${icon('arrow')}</button>`}</div></article>`;
}

function groupCard(name,members,all=tasks.filter(t=>t.group===name)){
  const first=all[0],key=first.id,target='g:'+key;
  const waiting=filter==='done'?[]:all.filter(isEditable),representative=waiting[0]||first,opened=expanded.has(name),done=all.filter(t=>t.state==='done').length;
  const resumable=filter==='done'?[]:all.filter(isRecoverable),startable=resumable.length?resumable:waiting;
  const editable=waiting.length&&!isLocal(representative),mixed=waiting.some(t=>JSON.stringify(t.outputs)!==JSON.stringify(representative.outputs)||t.folder!==representative.folder);
  return `<section class="group task-group" aria-label="${esc(name)}" data-group="${key}">
  <button class="task-remove" title="删除整组任务" aria-label="删除任务组 ${esc(name)}" onclick="removeTaskGroup(${key})">${icon('x')}</button>
  <div class="grouphead">${platformIcon(first)}<button class="group-toggle" aria-expanded="${opened}" onclick="toggleGroup(${key})"><strong>${esc(first.groupName||name)}</strong><small>${platformName(first.platform)} · ${all.length} 条作品${first.skipped?.length?' · '+first.skipped.length+' 条已跳过':''}${done?' · '+done+' 条完成':''}</small></button>${editable?subtitleControl(representative,target):''}</div>
  <div class="group-setup">${mixed?'<div class="group-setting-caption">部分作品有单独设置</div>':''}
  ${editable?choiceControls(representative,target,false):`<div class="card-output"><span>处理进度</span><b>${done} / ${all.length} 已完成</b></div>`}
  <div class="group-actions"><button class="group-expand" onclick="toggleGroup(${key})" aria-label="${opened?'收起':'展开'}批量任务">${opened?'收起作品':'查看作品'}<span>${opened?'⌃':'⌄'}</span></button><span class="group-status">${all.some(isInQueue)?'处理中':resumable.length?resumable.length+' 条已暂停':waiting.length?waiting.length+' 条待开始':done===all.length?'全部完成':'已停止'}</span>${startable.length?`${editable?cardFolder(representative,target):''}<button class="btn primary" ${startable.some(validOutput)?'':'disabled'} onclick="startAll(tasks.find(x=>x.id===${key}).group)">${resumable.length?'继续这组':'开始这组'}${icon('arrow')}</button>`:''}</div></div>
  ${opened?`<div class="members" tabindex="0" aria-label="批量作品列表，显示五条高度，可滚动">${members.map(t=>`<div class="member" data-job="${t.id}"><span class="member-title" title="${esc(t.name)}">${esc(t.name)}</span><span class="member-summary">${outputNames(t.outputs).join(' · ')} · <span class="member-state-inline">${taskStatusLabel(t)}</span></span><span class="member-state">${taskStatusLabel(t)}</span>${t.savePending?`<button class="textbtn" onclick="start(${t.id})">保存结果</button>`:isRecoverable(t)?`<button class="textbtn" onclick="start(${t.id})">继续</button>`:isEditable(t)?`${!isLocal(t)?`<button class="textbtn" onclick="editTaskOutputs(${t.id})" aria-label="单独设置 ${esc(t.name)}">设置</button>`:''}<button class="textbtn" ${validOutput(t)?'':'disabled'} onclick="start(${t.id})">开始</button>`:t.state==='done'?`<button class="textbtn" onclick="showResult(${t.id})">结果</button>`:`<button class="textbtn" onclick="cancel(${t.id})">取消</button>`}${deleteButton(t)}</div>`).join('')}${(first.skipped||[]).map(m=>`<div class="member skipped-member"><span class="member-title" title="${esc(m.name)}">${esc(m.name)}</span><span class="member-summary" title="${esc(m.reason)}">${esc(m.reason)}</span><span class="member-state">已跳过</span></div>`).join('')}</div>`:''}</section>`;
}

function outputNames(o){return [['text','文案'],['video','视频'],['cover','封面']].filter(([k])=>o[k]).map(([,n])=>n)}

// Keep native task order for execution/queue counts. Only presentation reverses it.
const boardCards=new Map();
const exitingCards=new Map();
let renderedTaskFilter=null;
const motionPreference=matchMedia('(prefers-reduced-motion: reduce)');
let boardInteractionDepth=0,boardRenderPending=false,boardPendingAdded=new Set();
document.addEventListener('pointerdown',event=>{
  if(event.target.closest('.task-feed > *'))boardInteractionDepth++;
},true);
function finishBoardPointer(){
  setTimeout(()=>{
    boardInteractionDepth=Math.max(0,boardInteractionDepth-1);
    if(boardInteractionDepth===0&&boardRenderPending){boardRenderPending=false;const added=new Set(boardPendingAdded);boardPendingAdded.clear();renderBoard(added)}
  },0);
}
document.addEventListener('pointerup',finishBoardPointer,true);
document.addEventListener('pointercancel',finishBoardPointer,true);
function releaseBoardInteraction(){
  boardInteractionDepth=0;
  if(boardRenderPending){boardRenderPending=false;const added=new Set(boardPendingAdded);boardPendingAdded.clear();renderBoard(added)}
}
window.addEventListener('blur',releaseBoardInteraction);
document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseBoardInteraction()});
function taskStructure(t){
  return [t.id,t.state,t.name,t.outputs,t.subs,t.folder,t.group,t.groupName,t.error,
    t.documentId,t.assets,t.recovered,t.recovered?t.message:null,t.readOnly,t.editableOutputs,t.savePending,t.skipped,t.displayOrder];
}
function onCardEntranceEnd(event){if(event.target===event.currentTarget)finishCardEntrance(event.currentTarget)}
function finishCardEntrance(node){node.classList.remove('task-enter');node.removeEventListener('animationend',onCardEntranceEnd)}
function finishCardExit(node){
  clearTimeout(node._exitTimer);
  if(exitingCards.get(node._boardKey)===node)exitingCards.delete(node._boardKey);
  if(!node.isConnected)return;
  node.remove();updateLayout()
}
// Preserve live controls, scroll containers and CSS transitions. Rows use their
// stable service index, so paging never turns one work's control into another's.
function taskNodeKey(node){
  if(node.nodeType!==1)return null;
  if(node.hasAttribute('data-native-row'))return 'row:'+node.dataset.nativeRow;
  if(node.id)return 'id:'+node.id;
  if(node.hasAttribute('data-native-scroll'))return 'scroll:'+node.dataset.nativeScroll;
  const part=['filters','board-title','board-tools','nx-batch-body','nx-single-head','nx-single-setting','nx-settings-panel','nx-live-panel','nx-single-footer','nx-head','nx-footer'].find(name=>node.classList.contains(name));
  return part?'part:'+part:null;
}
function patchTaskNode(node,fresh){
  if(node.nodeType===Node.TEXT_NODE){if(node.data!==fresh.data)node.data=fresh.data;return}
  if(node instanceof HTMLInputElement)node.checked=fresh.checked;
  if(node.isEqualNode(fresh))return;
  for(const attr of [...node.attributes])if(!fresh.hasAttribute(attr.name))node.removeAttribute(attr.name);
  for(const attr of fresh.attributes)if(node.getAttribute(attr.name)!==attr.value)node.setAttribute(attr.name,attr.value);
  // Progress owns its live children; structural refreshes only change visibility.
  if(node.classList.contains('nx-live-panel'))return;
  patchTaskChildren(node,fresh);
}
function patchTaskChildren(node,fresh){
  const keyed=new Map([...node.childNodes].map(child=>[taskNodeKey(child),child]).filter(([key])=>key!==null));
  const retained=new Set();
  [...fresh.childNodes].forEach((wanted,index)=>{
    const key=taskNodeKey(wanted);
    let child=key!==null?keyed.get(key):node.childNodes[index];
    if(!child||retained.has(child)||taskNodeKey(child)!==key||child.nodeType!==wanted.nodeType||child.nodeName!==wanted.nodeName)child=wanted;
    else patchTaskNode(child,wanted);
    if(node.childNodes[index]!==child)node.insertBefore(child,node.childNodes[index]||null);
    retained.add(child);
  });
  for(const child of [...node.childNodes])if(!retained.has(child))child.remove();
}
motionPreference.addEventListener('change',()=>{
  if(motionPreference.matches){
    boardCards.forEach(({node})=>{finishCardEntrance(node);node._reflowAnimation?.cancel()});
    document.querySelectorAll('.task-exit').forEach(finishCardExit)
  }
});
function setTaskFilter(value){
  if(!['single','batch'].includes(value))return;
  filter=value;
  const tabs=document.querySelector('.boardbar .filters');
  if(tabs){tabs.dataset.filter=value;tabs.querySelectorAll('button').forEach(button=>{const selected=button.dataset.filterValue===value;button.classList.toggle('on',selected);button.setAttribute('aria-selected',String(selected))})}
  renderBoard();
}
function renderBoard(added=new Set()){
  const workspaceBatches=nativeVisibleBatches();
  updateLayout();
  if(boardInteractionDepth>0&&boardCards.size){boardRenderPending=true;boardPendingAdded=new Set([...boardPendingAdded,...added]);return}
  const filterChanged=renderedTaskFilter!==null&&renderedTaskFilter!==filter;
  renderedTaskFilter=filter;
  if(filterChanged)document.querySelectorAll('.task-exit').forEach(finishCardExit);
  const board=$('#board');
  if(!tasks.length&&!skippedGroups.length&&!workspaceBatches.length&&!batchError&&!boardCards.size&&!board.querySelector('.task-exit')){board.replaceChildren();return}
  if(!board.querySelector('.boardbar')){
    board.innerHTML='<div class="boardbar"></div><div class="taskgrid task-feed" aria-label="任务，最新添加在前"></div><div class="skipped-groups"></div><div class="empty-filter">暂无这类任务</div>';
  }
  let cloudError=board.querySelector('.nx-service-error');
  if(!cloudError){cloudError=document.createElement('div');cloudError.className='nx-service-error';cloudError.setAttribute('role','status');board.prepend(cloudError)}
  cloudError.hidden=!batchError;
  const errorHTML=batchError?`<span>任务列表暂未更新，已有任务和结果保留。${esc(batchError)}</span><button class="textbtn" onclick="act('batch_refresh')">刷新</button>`:'';
  if(cloudError.innerHTML!==errorHTML)cloudError.innerHTML=errorHTML;
  const bar=board.querySelector('.boardbar');
  const scopeCount=tasks.filter(t=>Boolean(t.group)===(filter==='batch')).length+workspaceBatches.filter(b=>Boolean(b.workspace.group)===(filter==='batch')).reduce((n,b)=>n+b.visible_total,0);
  const clearTitle=pendingIntakes.size?'任务正在保存，完成后即可清空':`仅清空${filter==='batch'?'批量处理':'独立任务'}中的任务`;
  const barHTML=`<div class="board-title"><h2>任务</h2><span class="count">${tasks.length+workspaceBatches.reduce((n,b)=>n+b.visible_total,0)}</span></div><span class="board-caption">本地逐条处理，云端自动并行</span><div class="board-tools">${tasks.length||workspaceBatches.some(b=>b.visible_total)?`<button class="textbtn clear-tasks" title="${clearTitle}" ${pendingIntakes.size||clearingTasks||!scopeCount?'disabled':''} onclick="clearTasks()">${icon('trash')}清空任务</button>`:''}${tasks.some(isStartable)?`<button class="textbtn" onclick="startAll()">${workspaceBatches.length?'处理旧任务':tasks.some(isRecoverable)?'全部继续':'全部开始'}</button>`:''}<div class="filters" role="tablist" aria-label="按任务类型查看" data-filter="${filter}">${[['single','独立任务'],['batch','批量处理']].map(([v,n])=>`<button role="tab" data-filter-value="${v}" class="${filter===v?'on':''}" aria-selected="${filter===v}" onclick="setTaskFilter('${v}')">${n}</button>`).join('')}</div></div>`;
  if(bar._html!==barHTML){
    const template=document.createElement('template');template.innerHTML=barHTML;
    patchTaskChildren(bar,template.content);bar._html=barHTML;
  }
  const shown=t=>filter==='batch'?Boolean(t.group):!t.group;
  const groups=new Map(),items=[],ahead=new Map();let queued=0;
  tasks.forEach((t,index)=>{
    const rank=Number.isFinite(t.displayOrder)?t.displayOrder:index;
    ahead.set(t.id,queued);if(isInQueue(t))queued++;
    if(t.group){
      let group=groups.get(t.group);
      if(!group){group={key:'g:'+t.group,name:t.group,all:[],members:[],index,rank};groups.set(t.group,group)}
      group.rank=Math.min(group.rank,rank);
      group.all.push(t);if(shown(t))group.members.push(t);
    }else if(shown(t))items.push({key:'t:'+t.id,task:t,index,rank});
  });
  items.push(...[...groups.values()].filter(g=>g.members.length));
  items.push(...workspaceBatches.filter(b=>b.visible_total>0&&Boolean(b.workspace.group)===(filter==='batch')).map((b,index)=>({key:'native:'+b.batch_id,native:b,index,rank:2e15+(b.workspace.created_at||index)})));
  items.sort((a,b)=>b.rank-a.rank||b.index-a.index);
  const grid=board.querySelector('.task-feed'),keep=new Set(items.map(item=>item.key)),entrances=[];
  const viewport=$('#main').getBoundingClientRect();
  const previousRects=!motionPreference.matches&&!filterChanged&&(added.size||[...boardCards].some(([key])=>!keep.has(key)))?
    new Map([...boardCards].map(([key,value])=>[key,value.node.getBoundingClientRect()])
      .filter(([,rect])=>rect.bottom>viewport.top&&rect.top<viewport.bottom).slice(0,8)):null;
  if(previousRects)boardCards.forEach(({node})=>node._reflowAnimation?.cancel());
  for(const [key,value] of boardCards){if(!keep.has(key)){
    const node=value.node;
    if(motionPreference.matches||filterChanged)node.remove();
    else{
      const left=node.offsetLeft,top=node.offsetTop,width=node.offsetWidth,height=node.offsetHeight;
      node.style.position='absolute';node.style.left=left+'px';node.style.top=top+'px';node.style.width=width+'px';node.style.height=height+'px';node.style.overflow='hidden';
      finishCardEntrance(node);node.inert=true;node.setAttribute('aria-hidden','true');node.classList.add('task-exit');board.hidden=false;
      node._boardKey=key;exitingCards.set(key,node);
      node._exitAnimation=node.animate([
        {opacity:1,transform:'translateY(0)'},
        {opacity:0,transform:'translateY(-4px)'}
      ],{duration:160,easing:'cubic-bezier(.4,0,.2,1)',fill:'forwards'});
      node._exitAnimation.onfinish=()=>finishCardExit(node);
      node._exitTimer=setTimeout(()=>finishCardExit(node),320)
    }
    boardCards.delete(key)
  }}
  items.forEach((item,index)=>{
    const signature=item.native?nativeCardSignature(item.native,nativeExpanded.has(item.native.batch_id)):JSON.stringify(item.task?[taskStructure(item.task),item.task.state==='queued'?ahead.get(item.task.id):null]:
      [item.all.map(taskStructure),item.members.map(t=>t.id),expanded.has(item.name),filter]);
    let cached=boardCards.get(item.key);
    if(!cached||cached.signature!==signature){
      const template=document.createElement('template');
      template.innerHTML=item.native?(item.native.workspace.group?nativeBatchCard(item.native):nativeSingleCard(item.native)):item.task?jobCard(item.task):groupCard(item.name,item.members,item.all);
      const fresh=template.content.firstElementChild;
      if(item.native?._intake){fresh.setAttribute('inert','');fresh.setAttribute('aria-label','正在保存新任务');fresh.querySelectorAll('button,input').forEach(control=>{control.disabled=true;control.removeAttribute('onclick');control.removeAttribute('onchange')})}
      if(cached){
        const scroller=cached.node.querySelector('[data-native-scroll]');
        if(scroller)nativeScrolls.set(scroller.dataset.nativeScroll,scroller.scrollTop);
        const focused=document.activeElement,hadFocus=cached.node.contains(focused),focusedIndex=focused?.closest('[data-native-row]')?.dataset.nativeRow,focusedLabel=focused?.getAttribute('aria-label');
        // Keep the outer card (and any in-flight entrance) alive on state changes.
        const entering=cached.node.classList.contains('task-enter');
        cached.node.className=fresh.className+(entering?' task-enter':'');
        for(const attr of [...cached.node.attributes])if(attr.name!=='class'&&!fresh.hasAttribute(attr.name))cached.node.removeAttribute(attr.name);
        for(const attr of fresh.attributes)if(attr.name!=='class'&&cached.node.getAttribute(attr.name)!==attr.value)cached.node.setAttribute(attr.name,attr.value);
        patchTaskChildren(cached.node,fresh);
        if(hadFocus&&focusedLabel){const row=focusedIndex!==undefined?cached.node.querySelector(`[data-native-row="${focusedIndex}"]`):cached.node;[...(row?.querySelectorAll('[aria-label]')||[])].find(n=>n.getAttribute('aria-label')===focusedLabel)?.focus({preventScroll:true})}
      }else{
        const exiting=exitingCards.get(item.key);
        if(exiting){exiting._exitAnimation?.cancel();finishCardExit(exiting)}
        cached={node:fresh};boardCards.set(item.key,cached);
        if(item.native){
          if(added.has(item.key))entrances.push(fresh)
        }else if(item.task?added.has(item.task.id):item.all.some(t=>added.has(t.id)))entrances.push(fresh);
      }
      cached.signature=signature;
    }
    if(item.native)nativeSyncMessages(item.native);
    if(grid.children[index]!==cached.node)grid.insertBefore(cached.node,grid.children[index]||null);
  });
  if(previousRects){
    const moves=[];
    for(const [key,{node}] of boardCards){
      const before=previousRects.get(key);if(!before)continue;
      const after=node.getBoundingClientRect(),dx=before.left-after.left,dy=before.top-after.top;
      if(dx||dy)moves.push({node,dx,dy});
    }
    // Read all geometry before starting compositor animations, and bound work
    // to visible cards. Hundreds of offscreen layers make repeated adds stall.
    // Keep layout movement independent of the card's entrance transform so a
    // second quick add can move a card that is still arriving.
    for(const {node,dx,dy} of moves)node._reflowAnimation=node.animate(
      [{translate:`${dx}px ${dy}px`},{translate:'0px 0px'}],
      {duration:220,easing:'cubic-bezier(.22,1,.36,1)'});
  }
  nativePostRender();
  nativeSyncPopovers();
  const skipped=board.querySelector('.skipped-groups');
  const skippedHTML=filter==='batch'?skippedGroups.slice().reverse().map(g=>`<section class="group task-group"><div class="grouphead"><strong>${esc(g.name)}</strong><span class="grow"></span><button class="textbtn" onclick="act('clear_skipped',{id:'${esc(g.id)}'})">移除记录</button></div><div class="members" tabindex="0" aria-label="已跳过的作品">${g.members.map(m=>`<div class="member"><span class="member-title">${esc(m.name)}</span><span class="member-summary" title="${esc(m.reason)}">${esc(m.reason)}</span><span class="member-state">已跳过</span></div>`).join('')}</div></section>`).join(''):'';
  if(skipped._html!==skippedHTML){skipped.innerHTML=skippedHTML;skipped._html=skippedHTML}
  grid.hidden=!items.length&&!grid.querySelector('.task-exit');
  board.querySelector('.empty-filter').hidden=Boolean(items.length||skippedHTML||grid.querySelector('.task-exit'));
  // One batched geometry read after writes; offscreen/bulk cards never animate.
  if(entrances.length&&!motionPreference.matches&&!document.hidden){
    const visible=entrances.slice(0,8).filter(node=>{
      const rect=node.getBoundingClientRect();return rect.bottom>viewport.top&&rect.top<viewport.bottom;
    });
    [...grid.querySelectorAll('.task-enter')].slice(Math.max(0,8-visible.length)).forEach(finishCardEntrance);
    visible.forEach(node=>{
      node.classList.add('task-enter');
      node.addEventListener('animationend',onCardEntranceEnd);
    });
  }
}

function renderIntake(){

  $('#intake').innerHTML=`<div class="canvas"><div class="intake-grid">
  <div class="file-side"><div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="拖入或选择音视频文件" aria-dropeffect="copy" onclick="pickFiles()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pickFiles()}">
    <div class="art-rest" aria-hidden="true"><span class="art-pool"></span><div class="paper-art"><img src="assets/paper-collage-v11.png" alt=""></div></div>
    <h3>拖入音视频文件</h3><span class="choose">选择文件 ${icon('arrow')}</span><span class="formats">MP3 · WAV · M4A · MP4 · MOV · 支持批量</span>
    <div class="hintdrop" aria-hidden="true"><span class="drop-symbol">${icon('upload')}</span><strong>松开即可添加</strong><span>支持一次拖入多个音视频文件</span></div>
  </div></div>
  <div class="link-side"><div class="lead">${icon('link')}<h3>粘贴作品链接</h3></div><form class="url-form" onsubmit="grab(event)"><input id="url" aria-label="作品或播客链接" value="${esc(urlText)}" oninput="urlText=this.value" placeholder="抖音、小红书、B站、YouTube、播客RSS等作品链接"><button class="btn primary" type="submit">加入任务</button></form><div class="error-inline" id="urlError" hidden></div></div>
  <div class="recorder ${recording?'is-recording':''}">
    <div class="recorder-head"><div class="recorder-title"><span class="recorder-symbol">${icon('mic')}</span><h3>录一段声音</h3></div><button class="recorder-folder" aria-label="设置录音保存位置" title="录音保存到：${esc(recordFolder)}" onclick="chooseFolder('record')">${icon('folder')}<span>${esc(recordFolder)}</span>${icon('arrow')}</button></div>
    <div class="recorder-body">${recording?`<div class="live-record"><span class="recording"><i class="recording-dot"></i>正在录制 · ${source}</span><button class="record-cancel" onclick="cancelRecord()">取消</button></div>`:`<div class="record-source" role="group" aria-label="声音来源" data-source="${source}">${[['麦克风','mic'],['系统声音','monitor'],['混录','mix']].map(([n,i])=>`<button class="${source===n?'on':''}" aria-pressed="${source===n}" onclick="setSource('${n}')">${icon(i)}<span>${n}</span></button>`).join('')}</div>`}
    <button class="record-trigger ${recording?'stop':''}" aria-label="${recording?`结束录音，已录制${recordTime()}`:'开始录音'}" title="${recording?'点击结束录音':'开始录音'}" onclick="toggleRecord()"><span class="record-trigger-mark"></span><span>${recording?'录制中':'开始录音'}</span>${recording?`<span class="record-trigger-time" id="recTime">${recordTime()}</span>`:''}</button></div>
  </div></div></div>`;
  syncLinkSubmit();requestAnimationFrame(positionWorkbench);
}

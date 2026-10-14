// V24 display structures. Native task snapshots are the sole execution state.
const paths={headphones:'M4 14v-3a8 8 0 0 1 16 0v3 M4 12H3v7h4v-7H4 M20 12h1v7h-4v-7h3',grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M9 22h6',file:'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',wave:'M3 10v4 M7 6v12 M12 3v18 M17 7v10 M21 10v4',trash:'M3 6h18 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',box:'m12 3 9 5-9 5-9-5z M3 8v10l9 4 9-4V8 M12 13v9',book:'M3 4h6l3 3 3-3h6v16h-6l-3 2-3-2H3z M12 7v15',cloud:'M7 18H5a4 4 0 0 1 0-8 7 7 0 0 1 13-2 5 5 0 0 1 1 10h-2 M12 12v9 M9 18l3 3 3-3',shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',link:'M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2 M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',upload:'M12 16V3 M7 8l5-5 5 5 M4 15v6h16v-6',settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',folder:'M3 6h7l2 3h9v11H3z',play:'m8 4 12 8-12 8z',expand:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5',search:'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',check:'m5 12 4 4L19 6',x:'m6 6 12 12 M18 6 6 18',arrow:'M5 12h14 M13 6l6 6-6 6',copy:'M8 8h12v13H8z M4 16H2V2h13v3',clock:'M12 7v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0'};
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
  const resumable=filter==='done'?[]:all.filter(isRecoverable),startable=[...waiting,...resumable];
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
const motionPreference=matchMedia('(prefers-reduced-motion: reduce)');
function taskStructure(t){
  return [t.id,t.state,t.name,t.outputs,t.subs,t.folder,t.group,t.groupName,t.error,
    t.documentId,t.assets,t.recovered,t.recovered?t.message:null,t.readOnly,t.editableOutputs,t.savePending,t.skipped,t.displayOrder];
}
function onCardEntranceEnd(event){if(event.target===event.currentTarget)finishCardEntrance(event.currentTarget)}
function finishCardEntrance(node){node.classList.remove('task-enter');node.removeEventListener('animationend',onCardEntranceEnd)}
motionPreference.addEventListener('change',()=>{
  if(motionPreference.matches)boardCards.forEach(({node})=>finishCardEntrance(node));
});
function renderBoard(added=new Set()){
  updateLayout();
  const board=$('#board');
  if(!tasks.length&&!skippedGroups.length){board.replaceChildren();boardCards.clear();return}
  if(!board.querySelector('.boardbar')){
    board.innerHTML='<div class="boardbar"></div><div class="taskgrid task-feed" aria-label="任务，最新添加在前"></div><div class="skipped-groups"></div><div class="empty-filter">暂无这类任务</div>';
  }
  const bar=board.querySelector('.boardbar');
  const barHTML=`<h2>任务</h2><span class="count">${tasks.length}</span><div class="filters">${[['all','全部'],['active','待处理'],['done','已完成']].map(([v,n])=>`<button class="${filter===v?'on':''}" onclick="filter='${v}';renderBoard()">${n}</button>`).join('')}</div><span class="grow"></span>${tasks.some(isStartable)?`<button class="textbtn" onclick="startAll()">${tasks.some(isRecoverable)?'全部继续':'全部开始'}</button>`:''}`;
  if(bar._html!==barHTML){bar.innerHTML=barHTML;bar._html=barHTML}
  const shown=t=>filter==='all'||filter==='done'&&t.state==='done'||filter==='active'&&t.state!=='done';
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
  items.sort((a,b)=>b.rank-a.rank||b.index-a.index);
  const grid=board.querySelector('.task-feed'),keep=new Set(items.map(item=>item.key)),entrances=[];
  for(const [key,value] of boardCards){if(!keep.has(key)){value.node.remove();boardCards.delete(key)}}
  items.forEach((item,index)=>{
    const signature=JSON.stringify(item.task?[taskStructure(item.task),item.task.state==='queued'?ahead.get(item.task.id):null]:
      [item.all.map(taskStructure),item.members.map(t=>t.id),expanded.has(item.name),filter]);
    let cached=boardCards.get(item.key);
    if(!cached||cached.signature!==signature){
      const template=document.createElement('template');
      template.innerHTML=item.task?jobCard(item.task):groupCard(item.name,item.members,item.all);
      const fresh=template.content.firstElementChild;
      if(cached){
        // Keep the outer card (and any in-flight entrance) alive on state changes.
        const entering=cached.node.classList.contains('task-enter');
        cached.node.className=fresh.className+(entering?' task-enter':'');
        for(const attr of fresh.attributes)if(attr.name!=='class')cached.node.setAttribute(attr.name,attr.value);
        cached.node.replaceChildren(...fresh.childNodes);
      }else{
        cached={node:fresh};boardCards.set(item.key,cached);
        if(item.task?added.has(item.task.id):item.all.some(t=>added.has(t.id)))entrances.push(fresh);
      }
      cached.signature=signature;
    }
    if(grid.children[index]!==cached.node)grid.insertBefore(cached.node,grid.children[index]||null);
  });
  const skipped=board.querySelector('.skipped-groups');
  const skippedHTML=filter!=='done'?skippedGroups.slice().reverse().map(g=>`<section class="group task-group"><div class="grouphead"><strong>${esc(g.name)}</strong><span class="grow"></span><button class="textbtn" onclick="act('clear_skipped',{id:'${esc(g.id)}'})">移除记录</button></div><div class="members" tabindex="0" aria-label="已跳过的作品">${g.members.map(m=>`<div class="member"><span class="member-title">${esc(m.name)}</span><span class="member-summary" title="${esc(m.reason)}">${esc(m.reason)}</span><span class="member-state">已跳过</span></div>`).join('')}</div></section>`).join(''):'';
  if(skipped._html!==skippedHTML){skipped.innerHTML=skippedHTML;skipped._html=skippedHTML}
  grid.hidden=!items.length;
  board.querySelector('.empty-filter').hidden=Boolean(items.length||skippedHTML);
  // One batched geometry read after writes; offscreen/bulk cards never animate.
  if(entrances.length&&!motionPreference.matches&&!document.hidden){
    const viewport=$('#main').getBoundingClientRect();
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
  <div class="file-side"><div class="dropzone" id="dropzone" role="button" tabindex="0" aria-label="拖入或选择音视频文件" onclick="pickFiles()" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pickFiles()}">
    <div class="art-rest" aria-hidden="true"><span class="art-pool"></span><div class="paper-art"><img src="assets/paper-collage-v11.png" alt=""></div></div>
    <h3>拖入音视频文件</h3><span class="choose">选择文件 ${icon('arrow')}</span><span class="formats">MP3 · WAV · M4A · MP4 · MOV · 支持批量</span>
  </div></div>
  <div class="link-side"><div class="lead">${icon('link')}<h3>粘贴作品链接</h3></div><form class="url-form" onsubmit="grab(event)"><input id="url" aria-label="作品或播客链接" value="${esc(urlText)}" oninput="urlText=this.value" placeholder="抖音、小红书、B站、YouTube、播客RSS等作品链接"><button class="btn primary" type="submit">加入任务</button></form><div class="error-inline" id="urlError" hidden></div></div>
  <div class="recorder ${recording?'is-recording':''}">
    <div class="recorder-head"><div class="recorder-title"><span class="recorder-symbol">${icon('mic')}</span><h3>录一段声音</h3>${recording?`<span class="record-source-label">${source}</span>`:''}</div><button class="recorder-folder" aria-label="设置录音保存位置" title="录音保存到：${esc(recordFolder)}" onclick="chooseFolder('record')">${icon('folder')}<span>${esc(recordFolder)}</span>${icon('arrow')}</button></div>
    <div class="recorder-body">${recording?`<div class="live-record"><span class="recording" id="recTime">● ${recordTime()}</span><button class="record-cancel" onclick="cancelRecord()">取消</button></div>`:`<div class="record-source" role="group" aria-label="声音来源">${[['麦克风','mic'],['系统声音','monitor'],['混录','mix']].map(([n,i])=>`<button class="${source===n?'on':''}" aria-pressed="${source===n}" onclick="setSource('${n}')">${icon(i)}<span>${n}</span></button>`).join('')}</div>`}
    <button class="record-trigger ${recording?'stop':''}" onclick="toggleRecord()"><span class="record-trigger-mark"></span><span>${recording?'结束录音':'开始录音'}</span></button></div>
  </div></div></div>`;
  requestAnimationFrame(positionWorkbench);
}

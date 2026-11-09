'use strict';
const $ = function(s,root){return (root||document).querySelector(s);};
const $$ = function(s,root){return Array.from((root||document).querySelectorAll(s));};
const paths={
link:'M10 13a5 5 0 0 0 7 .2l3-3a5 5 0 0 0-7-7l-2 2 M14 11a5 5 0 0 0-7-.2l-3 3a5 5 0 0 0 7 7l2-2',
grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
headphones:'M4 14v-3a8 8 0 0 1 16 0v3 M4 13H3v7h4v-7z M20 13h1v7h-4v-7z',
mic:'M9 4a3 3 0 0 1 6 0v8a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3',
video:'M3 5h13v14H3z M16 10l5-4v12l-5-4',file:'M5 3h9l5 5v13H5z M14 3v6h5 M8 13h8 M8 17h6',
wave:'M3 10v4 M7 6v12 M11 3v18 M15 7v10 M19 5v14 M23 10v4',archive:'M3 5h18v4H3z M5 9v12h14V9 M9 13h6',
spark:'M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z',book:'M3 4h7a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H3z M13 7a3 3 0 0 1 3-3h5v15h-4a4 4 0 0 0-4 2',
help:'M9 8a3 3 0 0 1 6 0c0 2-3 2-3 5 M12 17h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
plus:'M12 5v14 M5 12h14',sliders:'M4 7h6 M14 7h6 M4 17h10 M18 17h2 M10 4v6 M14 14v6',
image:'M3 4h18v16H3z M3 15l5-5 5 5 3-3 5 5 M16 8h.01',chevron:'M7 10l5 5 5-5',
edit:'M15 4l5 5 M4 20l1-5L17 3l4 4L9 19z',trash:'M4 6h16 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',restore:'M4 10a8 8 0 1 1 1 9 M4 4v6h6',
expand:'M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5',more:'M5 12h.01 M12 12h.01 M19 12h.01',
sort:'M8 4v16 M4 16l4 4 4-4 M16 4h5 M16 9h4 M16 14h3',calendar:'M3 5h18v16H3z M7 3v4 M17 3v4 M3 10h18',
search:'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
check:'M5 12l4 4L19 6',close:'M6 6l12 12 M18 6L6 18',download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
export:'M12 15V3 M7 8l5-5 5 5 M4 13v8h16v-8',folder:'M3 6h7l2 3h9v12H3z',
lock:'M5 10h14v11H5z M8 10V6a4 4 0 0 1 8 0v4',play:'M8 4l12 8-12 8z',
alert:'M12 3l10 18H2z M12 9v5 M12 17h.01',clock:'M12 7v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
loader:'M21 12a9 9 0 1 1-9-9',arrow:'M5 12h14 M14 7l5 5-5 5',pause:'M8 5v14 M16 5v14'
};
function icon(name){return '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="'+(paths[name]||paths.file)+'"/></svg>';}
function paintIcons(root){$$('i[data-icon]',root).forEach(function(el){el.innerHTML=icon(el.dataset.icon);});}
function esc(v){return String(v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function spinner(){return '<i class="spinner">'+icon('loader')+'</i>';}

let records={},recordKey='',mode='partial',loggedIn=false,views=[],toastTimer,batchError='',api;
let drawerKind='',modalGeneration=0, busy={collect:false,batch:false}, managementSelection=new Set(),managementSearch='';
let managementEditing=null,managementPending=null, collectingRecord='';
let linkCheck={code:'empty',message:'请先粘贴抖音公开主页链接。'};
let linkCheckTimer, linkCheckVersion=0, submitAfterCheck=0, loginBusy=false, loginActive=false, loginPending=false;
let loginLabel='未登录', loginBlockReason='', retryBlockReason='', loginSaving=false, canClearLogin=false, collectPending=false, stopPending=false;
let partialRetryLink=null, recordPending=false;
let modePending=false;
const rowNodes=new Map(),emptySelection=new Set();
const reduced=matchMedia('(prefers-reduced-motion: reduce)');
function shouldMove(){return !reduced.matches&&!document.body.classList.contains('keyboard-mode')&&!document.body.classList.contains('reduce-motion');}
function setButton(el,text,busyFlag,iconName){
  const signature=JSON.stringify([text,busyFlag,iconName]);
  if(el.dataset.caption===signature)return;
  el.dataset.caption=signature;
  el.setAttribute('aria-busy',String(!!busyFlag));
  el.innerHTML=(busyFlag?spinner():'<i>'+icon(iconName||'check')+'</i>')+'<span>'+esc(text)+'</span>';
}
function toast(message,action,label,error){
  clearTimeout(toastTimer);const t=$('#toast');$('span',t).textContent=message;t.classList.toggle('error',!!error);$('i',t).innerHTML=icon(error?'alert':'check');
  const b=$('button',t);b.hidden=!action;b.textContent=label||'重试';b.onclick=action||null;t.classList.add('visible');
  toastTimer=setTimeout(function(){t.classList.remove('visible');},action?6500:3500);
}
function closePopovers(){LuluSelect.close({immediate:true});$$('[popover]:popover-open').forEach(function(el){el.hidePopover();});}
function allWorks(){return records[recordKey]?.works||[];}
function currentSelection(){return records[recordKey]?.selection||emptySelection;}
function allSelectedWorks(){const keys=currentSelection();return allWorks().filter(function(w){return keys.has(w.id);});}
const rowAnimations=new Map();
const tableScroller=$('#tableScroller'), workBody=$('#worksBody'), workTable=$('#worksTable');
const rowHeight=parseFloat(getComputedStyle(workTable).getPropertyValue('--workspace-row'))||68;
const overscan=8, smallTableLimit=40;
let renderedViews, renderedStart=-1, renderedEnd=-1, viewportFrame=0, viewportNotice=0;
let workById=new Map();

function spacerRow(){
  const row=document.createElement('tr');
  row.className='virtual-spacer';
  row.setAttribute('aria-hidden','true');
  row.insertCell().colSpan=12;
  return row;
}
const topSpacer=spacerRow(), bottomSpacer=spacerRow();

function renderViewport(){
  const scrolled=Math.max(0,tableScroller.scrollTop);
  const visibleRows=Math.ceil(tableScroller.clientHeight/rowHeight)+1;
  const start=views.length<=smallTableLimit?0:
    Math.max(0,Math.min(Math.floor(scrolled/rowHeight),views.length-visibleRows)-overscan);
  const end=views.length<=smallTableLimit?views.length:
    Math.min(views.length,start+Math.min(80,visibleRows+2*overscan));
  if(renderedViews===views&&renderedStart===start&&renderedEnd===end)return;
  renderedViews=views;renderedStart=start;renderedEnd=end;
  const mounted=views.slice(start,end), keys=new Set(mounted.map(work=>work.id));
  const active=document.activeElement;
  for(const [key,row] of rowNodes){
    if(keys.has(key))continue;
    if(row.contains(active))tableScroller.focus({preventScroll:true});
    rowAnimations.get(row)?.cancel();
    rowAnimations.delete(row);
    row.remove();
    rowNodes.delete(key);
  }
  const ordered=[];
  if(start){
    topSpacer.firstElementChild.style.height=start*rowHeight+'px';
    ordered.push(topSpacer);
  }else topSpacer.remove();
  for(const [offset,work] of mounted.entries()){
    let row=rowNodes.get(work.id);
    if(!row){row=makeRow(work);rowNodes.set(work.id,row);}
    row.setAttribute('aria-rowindex',String(start+offset+2));
    updateRow(work,row);
    ordered.push(row);
  }
  if(end<views.length){
    bottomSpacer.firstElementChild.style.height=(views.length-end)*rowHeight+'px';
    ordered.push(bottomSpacer);
  }else bottomSpacer.remove();
  // Keep overlapping rows attached while scrolling so the keyboard and
  // accessibility tree retain a stable element for the current work.
  let next=workBody.firstElementChild;
  for(const row of ordered){
    if(row===next)next=next.nextElementSibling;
    else workBody.insertBefore(row,next);
  }
  if(active!==document.activeElement&&workBody.contains(active))active.focus({preventScroll:true});
  clearTimeout(viewportNotice);
  viewportNotice=setTimeout(()=>perform('visible_works',{keys:[...keys]}),75);
}

function requestViewport(){
  if(viewportFrame)return;
  viewportFrame=requestAnimationFrame(()=>{viewportFrame=0;renderViewport();});
}
tableScroller.addEventListener('scroll',requestViewport,{passive:true});
new ResizeObserver(requestViewport).observe(tableScroller);
tableScroller.addEventListener('keydown',event=>{
  const row=event.target.closest('tr[data-work]');
  if(!row||!event.target.matches('input[type=checkbox]')||!views.length)return;
  const step={ArrowDown:1,ArrowUp:-1,PageDown:Math.floor(tableScroller.clientHeight/rowHeight),
              PageUp:-Math.floor(tableScroller.clientHeight/rowHeight)};
  const index=Number(row.getAttribute('aria-rowindex'))-2;
  const next=event.key==='Home'?0:event.key==='End'?views.length-1:
    Object.hasOwn(step,event.key)?Math.max(0,Math.min(views.length-1,index+step[event.key])):null;
  if(next===null)return;
  event.preventDefault();
  tableScroller.scrollTop=next*rowHeight;
  renderViewport();
  rowNodes.get(views[next].id)?.querySelector('input').focus({preventScroll:true});
});

function snapshots(){
  const map=new Map();const viewport=$('#tableScroller').getBoundingClientRect();
  for(const row of rowNodes.values()){
    const rect=row.getBoundingClientRect();
    if(rect.bottom>viewport.top&&rect.top<viewport.bottom)map.set(row.dataset.work,rect);
  }
  return map;
}
function animateRows(before,newIds){
  rowAnimations.forEach(animation=>animation.cancel());rowAnimations.clear();
  if(!shouldMove())return;
  const viewport=$('#tableScroller').getBoundingClientRect();
  // Read the visible positions together, then start animations together.
  const measured=[...rowNodes.values()].map(row=>({row,rect:row.getBoundingClientRect()})).filter(({rect})=>rect.bottom>=viewport.top&&rect.top<=viewport.bottom);
  measured.forEach(({row,rect})=>{
    const old=before.get(row.dataset.work);let animation;
    if(old){const dy=old.top-rect.top;if(Math.abs(dy)>1&&Math.abs(dy)<600)animation=row.animate([{transform:'translateY('+dy+'px)'},{transform:'translateY(0)'}],{duration:220,easing:'cubic-bezier(.23,1,.32,1)'});}
    else if(newIds&&newIds.has(row.dataset.work))animation=row.animate([{opacity:0,transform:'translateY(-6px)'},{opacity:1,transform:'translateY(0)'}],{duration:180,easing:'cubic-bezier(.23,1,.32,1)'});
    if(animation){rowAnimations.set(row,animation);animation.onfinish=()=>{if(rowAnimations.get(row)===animation)rowAnimations.delete(row);};}
  });
}
function knownNumber(n){return typeof n==='number'&&Number.isFinite(n);}
function formatNumber(n){return knownNumber(n)?n.toLocaleString('en-US'):'—';}
function duration(w){return w.kind==='image'?'图文':w.durationText||(!knownNumber(w.seconds)?'—':Math.floor(w.seconds/60)+':'+String(w.seconds%60).padStart(2,'0'));}
function assetMarkup(w,key){
  if(key!=='text')return materialMarkup(w,key);
  const val=w.text,label=({ready:'查看',working:'提取中',queued:'待开始',paused:'已暂停',failed:'提取失败',cancelled:'已取消',interrupted:'已中断',recovery_required:'结果待核对'})[val]||'未转录';
  return '<button class="asset-status '+esc(val)+'" data-asset="text" '+(val==='ready'?'':'disabled')+' aria-label="'+esc(label+'文稿：'+w.title)+'">'+(val==='ready'?'<i>'+icon('check')+'</i>':'')+'<span>'+label+'</span></button>';
}
function updateRowSelection(w,row){
  const checked=currentSelection().has(w.id);row.classList.toggle('selected',checked);
  const checkbox=row._checkInput||(row._checkInput=$('input',row));if(checkbox.checked!==checked)checkbox.checked=checked;
}
function paintSelection(){
  for(const [key,row] of rowNodes)updateRowSelection(workById.get(key),row);
}
function updateRow(w,row){
  updateRowSelection(w,row);
  const cells=row._assetCells||(row._assetCells=Object.fromEntries(['text','media','cover'].map(key=>[key,$('[data-asset-cell="'+key+'"]',row)])));
  ['text','media','cover'].forEach(function(key){const cell=cells[key],signature=w[key]+':'+w.kind+':'+(w[key+'Progress']??'unknown')+':'+(w[key+'Reason']??'')+':'+(key==='cover'&&!!w.coverCanRedownload);if(cell.dataset.assetState!==signature){if(key==='text'||!syncMaterialProgress(cell,w,key))cell.innerHTML=assetMarkup(w,key);cell.dataset.assetState=signature;}});
}
function makeRow(w){
  const row=document.createElement('tr');row.dataset.work=w.id;
  row.innerHTML='<td><input type="checkbox" aria-label="选择：'+esc(w.title)+'"></td><td><div class="author-name" title="'+esc(w.author)+'">'+esc(w.author)+'</div></td><td><div class="work-cell"><div class="work-text"><button class="work-title" title="'+esc(w.title)+'">'+esc(w.title)+'</button><div class="work-meta"><span class="kind-tag">'+(w.kind==='image'?'图文':'视频')+'</span></div></div></div></td><td class="date-text">'+(w.date||'—')+'</td><td>'+duration(w)+'</td>'+['like','collect','comment','share'].map(function(k){return '<td class="number">'+formatNumber(w[k])+'</td>';}).join('')+'<td data-asset-cell="text"></td><td data-asset-cell="media"></td><td data-asset-cell="cover"></td>';
  $('input',row).addEventListener('click',function(e){e.stopPropagation();toggleWork(w,e.shiftKey,this.checked);});
  $('.work-title',row).addEventListener('click',function(){showWork(w);});
  row.addEventListener('mousedown',function(e){if(e.shiftKey&&!e.target.closest('button,input'))e.preventDefault();});
  row.addEventListener('click',function(e){if(e.target.closest('button,input,[data-asset-cell]')||(!e.shiftKey&&window.getSelection().toString()))return;if(e.shiftKey)window.getSelection().removeAllRanges();toggleWork(w,e.shiftKey,!currentSelection().has(w.id));});
  row.addEventListener('click',function(e){const b=e.target.closest('[data-asset]');if(!b||b.disabled)return;assetAction(w,b.dataset.asset,b.dataset.action);});
  row.addEventListener('contextmenu',function(e){
    const key=e.target.closest('[data-asset-cell]')?.dataset.assetCell,work=workById.get(w.id)||w;
    if(!['media','cover'].includes(key)||work[key]!=='ready')return;
    e.preventDefault();closePopovers();
    const menu=$('#coverMenu'),refresh=$('#redownloadCover');
    refresh.hidden=key!=='cover'||work.coverCanRedownload!==true;
    refresh.onclick=async()=>{menu.hidePopover();const result=await perform('redownload_cover',{id:work.id});if(result)updateRecordState(result);};
    $('#revealCover').onclick=()=>{menu.hidePopover();perform('reveal',{id:work.id,kind:key});};
    menu.style.width='190px';menu.style.left=Math.max(12,Math.min(e.clientX,innerWidth-202))+'px';
    menu.showPopover();menu.style.top=Math.max(12,Math.min(e.clientY,innerHeight-menu.offsetHeight-12))+'px';
  });
  row.addEventListener('dblclick',function(e){const cell=e.target.closest('[data-asset-cell]'),key=cell?.dataset.assetCell;if(!['media','cover'].includes(key))return;currentSelection().add(w.id);updateRow(w,row);updateSelection();if(w[key]==='ready')perform('reveal',{id:w.id,kind:key});});
  return row;
}
let selectionAnchor=null;
function toggleWork(w,shift,checked){
  const keys=currentSelection(),ids=views.map(function(x){return x.id;});
  if(shift&&selectionAnchor&&ids.includes(selectionAnchor)){let a=ids.indexOf(selectionAnchor),b=ids.indexOf(w.id);ids.slice(Math.min(a,b),Math.max(a,b)+1).forEach(function(id){keys.add(id);});}
  else{checked?keys.add(w.id):keys.delete(w.id);selectionAnchor=w.id;}
  batchError='';paintSelection();updateSelection();

}
function filteredWorks(){return CollectionFilters.apply(allWorks());}
function render(newIds){
  const before=shouldMove()?snapshots():new Map();
  views=filteredWorks();
  workById=new Map(allWorks().map(work=>[work.id,work]));
  for(const key of currentSelection())if(!workById.has(key))currentSelection().delete(key);
  workTable.dataset.record=recordKey;
  workTable.setAttribute('aria-rowcount',String(views.length+1));
  renderViewport();
  $('#emptyState').hidden=views.length>0;$('#recordCount').textContent=(views.length===allWorks().length?allWorks().length:views.length+' / '+allWorks().length)+' 条作品';
  $('#resultFoot').textContent=(views.length===allWorks().length?'共 '+views.length:'显示 '+views.length+' / '+allWorks().length)+' 条作品';
  const filtered=CollectionFilters.active();
  $('h2',$('#emptyState')).textContent=allWorks().length?'暂时没有符合条件的作品':'还没有采集结果';
  $('p',$('#emptyState')).textContent=allWorks().length?'调整筛选，已选作品仍然保留。':'粘贴主页链接开始，或切换到已有的采集记录。';$('#emptyReset').hidden=!filtered;
  $('#filterSummary').hidden=!filtered;$('span',$('#filterSummary')).textContent='筛选出 '+views.length+' 条作品 · 原始资料保持完整';

  updateSelection();animateRows(before,newIds);
}
function updateSelection(){
  const works=allSelectedWorks(),visibleIds=new Set(views.map(function(w){return w.id;})),hidden=works.filter(function(w){return !visibleIds.has(w.id);}).length;
  const video=works.filter(function(w){return w.kind!=='image'&&['douyin','xiaohongshu'].includes(w.platform)&&w.work_url;});
  $('#selectionCount').textContent=works.length?'已选 '+works.length+' 条':'未选择作品';
  $('#selectionDetail').textContent=batchError||busy.batch?'':!works.length?'支持多选与 Shift 连选':hidden?'其中 '+hidden+' 条已选作品在筛选外':works.some(function(w){return w.kind==='image';})?'含图文，转录时会自动跳过':'素材在表格内下载或查看';
  if(batchError){$('#selectionDetail').textContent=batchError;$('#selectionDetail').style.color='var(--red)';}
  else if(busy.batch){$('#selectionDetail').textContent='正在保存本次选择，可继续浏览作品';$('#selectionDetail').style.color='';}
  else $('#selectionDetail').style.color='';
  $('#clearSelection').hidden=works.length===0;
  const all=$('#selectVisible'),n=views.filter(function(w){return currentSelection().has(w.id);}).length;all.checked=views.length>0&&n===views.length;all.indeterminate=n>0&&n<views.length;all.disabled=!views.length;
  $('#feishuAction').disabled=works.length===0;$('#feishuAction').title=works.length?'存入所选 '+works.length+' 条作品':'请先勾选作品';$('#exportAction span').textContent=works.length?'导出所选':'导出列表';$('#exportAction').disabled=!works.length&&!views.length;
  WorkspaceUI.syncFilter();
  $('#transcribeSelected').disabled=works.length===0||busy.batch;$('#transcribeSelected').title='将所选作品加入内容提取，仅提取文稿；确认后开始';
  if(!busy.batch)setButton($('#transcribeSelected'),(batchError?'重试加入':'批量转录')+(video.length?' · '+video.length:''),false,'headphones');
}
function clearFilters(){CollectionFilters.clear();}
function setStatus(text,kind){
  const area=$('.collection-status');area.className='collection-status '+(kind||'');
  area.dataset.active=String(text!=='准备就绪，已有作品会自动保留');
  $('#collectStatus').innerHTML=(kind==='working'?spinner():'<span class="status-dot"></span>')+'<span>'+esc(text)+'</span>';WorkspaceUI.syncOperation();
}
function syncLoginSettings(){
  $('#loginStatus').dataset.connected=String(loggedIn);
  $('#loginStatus span').textContent=loginSaving?'正在更新登录':loginActive?'等待扫码登录':loginBusy?'登录处理中':loginLabel;
  $('#loginButton').hidden=loggedIn&&!loginActive&&!loginSaving;
  $('#loginButton').textContent=loginSaving?'正在更新…':loginActive?'取消登录':'登录';
  $('#loginButton').disabled=loginSaving||loginPending||busy.collect||(!loginActive&&(loginBusy||!!loginBlockReason));
  $('#loginButton').title=loginSaving?'正在保存或清除登录信息，开始处理后无法取消。':loginActive?'取消本次登录':loginBlockReason;
  $('#loginNotice').textContent=loginBlockReason||'部分采集受限或全量采集时，可在专用浏览器中自行登录。已取得的作品会保留。';
  $('#clearLogin').hidden=loginLabel==='未登录'||!canClearLogin||loginBusy;
  $('#clearLogin').disabled=busy.collect||!!loginBlockReason;
  $('#browserChoice').disabled=busy.collect||loginBusy||loginPending;
}
function updateMode(){
  syncLoginSettings();
  const full=mode==='full';$('.segmented').dataset.full=String(full);$$('[data-mode]').forEach(function(b){b.setAttribute('aria-selected',String(b.dataset.mode===mode));});
  $('#modeCopy').textContent=full?(loggedIn?'读取完整公开作品':'需登录专用浏览器'):(loggedIn?'使用已保存登录 · 最新一页':'最新一页 · 约 15～20 条');$('#modeCopy').title=full?'使用专用浏览器读取公开作品，遇到限制时保留已取得结果':CollectionContract._PARTIAL_COLLECTION_SUMMARY;
  const start=$('#collectStart');
  const retry=!full&&partialRetryLink!==null&&partialRetryLink===$('#homepage').value.trim();
  const blocked=retry?retryBlockReason:full&&!loggedIn?loginBlockReason:'';
  start.disabled=linkCheck.code!=='ready'||busy.collect||collectPending||loginBusy||loginPending||!!blocked;
  start.title=loginBusy?'请先完成或取消浏览器登录':blocked||linkCheck.message;
  if(busy.collect||collectPending)setButton(start,busy.collect?'正在采集':'正在启动',true,'loader');
  else setButton(start,linkCheck.code==='blocked'?'本版暂不可用':full&&!loggedIn?'登录后全量采集':full?'开始全量采集':retry?(loggedIn?'用已保存登录重试':'登录后部分采集'):'开始部分采集',false,full&&!loggedIn||retry?'lock':'plus');
}
function checkHomepage(edited=false){
  clearTimeout(linkCheckTimer);
  const version=++linkCheckVersion,link=$('#homepage').value;
  if(edited&&link.trim()!==partialRetryLink)partialRetryLink=null;
  if(edited)setStatus('准备就绪，已有作品会自动保留');
  linkCheck=link.trim()?{code:'checking',message:'正在识别主页链接…'}:{code:'empty',message:'请先粘贴抖音公开主页链接。'};
  $('#homepage').removeAttribute('aria-invalid');
  updateMode();
  if(!api||!link.trim())return;
  linkCheckTimer=setTimeout(async()=>{
    const reply=await perform('inspect_link',{link});
    if(version!==linkCheckVersion)return;
    linkCheck=reply||{code:'invalid',message:'暂时无法识别链接，请重新粘贴后重试。'};
    const invalid=linkCheck.code!=='ready';
    $('#homepage').setAttribute('aria-invalid',String(invalid));
    if(invalid&&edited)setStatus(linkCheck.message,'error');
    updateMode();
    if(!invalid&&submitAfterCheck===version)collect();
  },edited?90:0);
}
function openDrawer(title,eyebrow,kind){
  closePopovers();drawerKind=kind;$('#drawerTitle').textContent=title;$('#drawerEyebrow').textContent=eyebrow;
  if(!$('#drawer').open)$('#drawer').showModal();
}
function showWork(w){
  openDrawer(w.title,w.author+' · '+(w.kind==='image'?'图文':'视频'),'detail');
  $('#drawerBody').innerHTML='<div class="detail-cover">'+icon(w.kind==='image'?'image':'play')+'</div><div class="detail-grid"><div><label>发布日期</label><strong>'+w.date+'</strong></div><div><label>时长</label><strong>'+duration(w)+'</strong></div><div><label>点赞</label><strong>'+formatNumber(w.like)+'</strong></div><div><label>收藏</label><strong>'+formatNumber(w.collect)+'</strong></div></div><div class="dialog-callout">文稿、音视频和封面分别记录状态。提取文稿不会把未下载的素材显示为已保存。</div><div class="detail-actions"><button class="btn primary" id="detailTranscribe">'+icon(w.text==='ready'?'file':'headphones')+(w.text==='ready'?'查看文稿':'提取这条')+'</button></div>';
  $('#detailTranscribe').disabled=w.kind==='image';$('#detailTranscribe').onclick=function(){if(w.text==='ready')showManuscript(w);else stageWorks([w]);};

}
function historyOptions(){
  RecordPicker.render(Object.entries(records),recordKey);
}
function managementFocus(action,key){
  const root=$('#manageList');
  const node=$$('[data-manage-'+action+']',root).find(el=>el.dataset['manage'+action[0].toUpperCase()+action.slice(1)]===key);
  (node||$('#manageSearch')).focus({preventScroll:true});
}
function managementBlocked(keys){return keys.some(key=>!records[key]||(busy.collect&&key===collectingRecord));}
function managementConfirm(keys,origin){
  if(!keys.length||managementBlocked(keys))return;
  managementEditing=null;managementPending={keys:[...keys],origin,status:'confirm',version:updateRecordState.historyVersion};renderManagement();$('#cancelManage').focus({preventScroll:true});
}
function managementCancel(){
  if(managementPending?.status==='working')return;
  const origin=managementPending?.origin||{action:'rename',key:managementEditing?.key};
  managementPending=null;managementEditing=null;renderManagement();
  if(origin.key)managementFocus(origin.action,origin.key);else $('#manageSearch').focus({preventScroll:true});
}
function managementRename(key){
  if(!records[key])return;
  managementEditing={key,draft:records[key].name,error:false,version:updateRecordState.historyVersion};renderManagement();
  const input=$('#manageRenameInput');input.focus({preventScroll:true});input.select();
}
function renderManagement(){
  const list=$('#manageList');if(!list)return;
  if(!$('.manage-content',list)){
    list.innerHTML='<div class="manage-content"><div class="manage-toolbar"><div class="manage-search-wrap">'+icon('search')+'<input id="manageSearch" type="search" aria-label="搜索记录名称或作者" placeholder="搜索记录名称或作者" autocomplete="off"><button id="manageSearchClear" aria-label="清除搜索">'+icon('close')+'</button></div></div><div id="manageRows" role="region" tabindex="0" aria-label="采集记录列表"><table class="manage-table" aria-label="采集记录"><colgroup><col class="manage-check-column"><col><col class="manage-works-column"><col class="manage-time-column"><col class="manage-actions-column"></colgroup><thead><tr><th scope="col" class="manage-check-cell"><label class="manage-select-all"><input type="checkbox" id="manageAll" aria-label="全选当前搜索结果"></label></th><th scope="col" class="manage-name-cell"><span class="manage-name-heading">记录名称</span></th><th scope="col" class="manage-works-cell"><span>作品</span></th><th scope="col" class="manage-time-cell"><span>采集时间</span></th><th scope="col" class="manage-actions-cell"><span>操作</span></th></tr></thead><tbody id="manageRecordsBody"></tbody></table></div><footer class="manage-footer"><div class="manage-summary" role="status" aria-live="polite"><span id="manageCount"></span><small id="manageSelectionHint"></small></div><div class="manage-bulk"><button class="btn quiet compact" id="manageClearSelection">取消选择</button><button class="btn secondary compact danger" id="manageDelete">'+icon('trash')+'移入废纸篓</button><button class="btn primary compact" id="manageDone">完成</button></div></footer></div><div id="manageConfirm"></div>';
    $('#manageSearch').value=managementSearch;
    $('#manageSearch').oninput=function(){if(!this.composing){managementSearch=this.value;managementEditing=null;renderManagement();$('#manageRows').scrollTop=0;}};
    $('#manageSearch').addEventListener('compositionstart',function(){this.composing=true;});
    $('#manageSearch').addEventListener('compositionend',function(){this.composing=false;this.oninput();});
    $('#manageSearchClear').onclick=()=>{managementSearch='';$('#manageSearch').value='';renderManagement();$('#manageSearch').focus();};
    $('#manageClearSelection').onclick=()=>{managementSelection.clear();renderManagement();$('#manageAll').focus();};
    $('#manageDone').onclick=()=>$('#modal').close();
    list.addEventListener('change',e=>{
      const check=e.target.closest('[data-manage-check]');if(!check)return;
      check.checked?managementSelection.add(check.dataset.manageCheck):managementSelection.delete(check.dataset.manageCheck);
      renderManagement();managementFocus('check',check.dataset.manageCheck);
    });
    list.addEventListener('click',e=>{
      const rename=e.target.closest('[data-manage-rename]');if(rename){managementRename(rename.dataset.manageRename);return;}
      const remove=e.target.closest('[data-manage-delete]');if(remove){managementConfirm([remove.dataset.manageDelete],{action:'delete',key:remove.dataset.manageDelete});return;}
      if(e.target.closest('button,input,label,.manage-name-editor'))return;
      const row=e.target.closest('[data-managed]');if(row){const key=row.dataset.managed;managementSelection.has(key)?managementSelection.delete(key):managementSelection.add(key);renderManagement();managementFocus('check',key);}
    });
  }
  if(managementEditing&&$('#manageRenameInput'))managementEditing.draft=$('#manageRenameInput').value;
  const source=Object.entries(records),needle=managementSearch.trim().toLocaleLowerCase();
  const entries=source.filter(([,r])=>!needle||r.name.toLocaleLowerCase().includes(needle)||(r.author||'').toLocaleLowerCase().includes(needle));
  const keys=entries.map(([key])=>key);
  managementSelection.forEach(key=>{if(!records[key])managementSelection.delete(key);});
  const chosen=[...managementSelection],hiddenChosen=chosen.filter(key=>!keys.includes(key)).length;
  $('#manageSearchClear').hidden=!managementSearch;
  $('#manageAll').checked=keys.length>0&&keys.every(key=>managementSelection.has(key));$('#manageAll').indeterminate=keys.some(key=>managementSelection.has(key))&&!$('#manageAll').checked;$('#manageAll').disabled=!keys.length;
  $('#manageAll').onchange=function(){keys.forEach(key=>this.checked?managementSelection.add(key):managementSelection.delete(key));renderManagement();};
  $('#manageCount').textContent=chosen.length?'已选 '+chosen.length+' 份记录':needle?'找到 '+entries.length+' 份记录，共 '+source.length+' 份':'共 '+source.length+' 份记录';
  $('#manageSelectionHint').textContent=chosen.length?(hiddenChosen?hiddenChosen+' 份不在当前搜索结果中':'含 '+chosen.reduce((count,key)=>count+records[key].count,0)+' 条作品'):'';
  $('#manageClearSelection').hidden=!chosen.length;$('#manageDelete').hidden=!chosen.length;
  $('#manageDelete').disabled=managementBlocked(chosen);$('#manageDelete').title=$('#manageDelete').disabled?'采集中的记录暂不能删除':'';
  $('#manageDelete').onclick=()=>managementConfirm(chosen);
  const rows=$('#manageRows'),scroll=rows.scrollTop,body=$('#manageRecordsBody');
  body.innerHTML=entries.length?entries.map(([key,r])=>{
    const editing=managementEditing?.key===key,current=key===recordKey,locked=busy.collect&&key===collectingRecord;
    const name=editing?'<div class="manage-name-editor"><input id="manageRenameInput" placeholder="输入记录名称" aria-label="记录名称" aria-describedby="manageRenameError" aria-invalid="'+managementEditing.error+'" value="'+esc(managementEditing.draft)+'"><small id="manageRenameError" role="alert" '+(managementEditing.error?'':'hidden')+'>请输入记录名称</small></div>':'<div class="manage-name-copy"><strong title="'+esc(r.name)+'">'+esc(r.name)+'</strong>'+(current?'<small class="manage-current">正在查看</small>':locked?'<small class="manage-current">采集中</small>':'')+'<small class="manage-mobile-time">'+esc(r.time)+'</small></div>';
    const actions=editing?'<button class="manage-row-button save-name" id="manageSaveName" aria-label="保存名称">'+icon('check')+'</button><button class="manage-row-button" id="manageCancelName" aria-label="取消重命名">'+icon('close')+'</button>':'<button class="manage-row-button" data-manage-rename="'+esc(key)+'" aria-label="重命名 '+esc(r.name)+'" title="重命名">'+icon('edit')+'</button><button class="manage-row-button danger" data-manage-delete="'+esc(key)+'" aria-label="将 '+esc(r.name)+' 移入废纸篓" title="'+(locked?'采集中的记录暂不能删除':'移入系统废纸篓')+'" '+(locked?'disabled':'')+'>'+icon('trash')+'</button>';
    return '<tr class="manage-record" data-managed="'+esc(key)+'" data-current="'+current+'" data-selected="'+managementSelection.has(key)+'"><td class="manage-check-cell"><label class="manage-row-check"><input type="checkbox" data-manage-check="'+esc(key)+'" aria-label="选择 '+esc(r.name)+'" '+(managementSelection.has(key)?'checked':'')+'></label></td><td class="manage-name-cell"><div class="manage-record-main">'+name+'</div></td><td class="manage-works-cell"><span class="manage-work-count">'+r.count.toLocaleString('zh-CN')+'</span></td><td class="manage-time-cell"><time class="manage-record-time">'+esc(r.time)+'</time></td><td class="manage-actions-cell"><div class="manage-row-actions">'+actions+'</div></td></tr>';
  }).join(''):'<tr><td colspan="5" class="manage-empty-cell"><div class="manage-empty"><span>'+icon(needle?'search':'folder')+'</span><strong>'+(needle?'没有找到相关记录':'还没有采集记录')+'</strong><p>'+(needle?'换个名称或作者试试':'采集一个主页后，记录会出现在这里')+'</p>'+(needle?'<button class="btn quiet compact" id="manageEmptyClear">清除搜索</button>':'')+'</div></td></tr>';
  rows.scrollTop=scroll;
  if($('#manageEmptyClear'))$('#manageEmptyClear').onclick=()=>$('#manageSearchClear').click();
  if(managementEditing&&$('#manageRenameInput')){
    $('#manageRenameInput').oninput=function(){managementEditing.draft=this.value;this.setAttribute('aria-invalid','false');managementEditing.error=false;$('#manageRenameError').hidden=true;};
    $('#manageRenameInput').onkeydown=e=>{if(e.isComposing)return;if(e.key==='Enter'){e.preventDefault();managementSaveName();}if(e.key==='Escape'){e.preventDefault();e.stopPropagation();managementCancel();}};
    $('#manageSaveName').onclick=managementSaveName;$('#manageCancelName').onclick=managementCancel;
  }
  const pending=managementPending,confirm=$('#manageConfirm');
  $('.manage-content',list).inert=!!pending;$('.dialog-head',$('#modal')).inert=!!pending;
  confirm.replaceChildren();
  if(pending){
    const count=pending.keys.length,works=pending.keys.reduce((n,key)=>n+(records[key]?.count||0),0),working=pending.status==='working',error=pending.status==='error';
    confirm.innerHTML='<div class="manage-confirm-shade"><section id="manageConfirmCard" class="manage-confirm-card" data-phase="'+pending.status+'" role="alertdialog" aria-modal="true" aria-busy="'+working+'" tabindex="-1" aria-labelledby="manageConfirmTitle" aria-describedby="manageConfirmCopy"><span class="manage-confirm-icon">'+icon('trash')+'</span><h3 id="manageConfirmTitle">将 '+count+' 份记录移入废纸篓？</h3><p id="manageConfirmCopy">这 '+works+' 条作品信息会随记录一起移动。已保存的素材和文稿不受影响。</p><div class="manage-confirm-names">'+pending.keys.slice(0,3).map(key=>'<span>'+esc(records[key]?.name||'记录已变化')+'</span>').join('')+(count>3?'<small>及另外 '+(count-3)+' 份记录</small>':'')+'</div><p id="manageDeleteStatus" role="alert">'+(error?'未能移入废纸篓，记录已保留。请重试。':'')+'</p><div class="dialog-buttons"><button class="btn secondary compact" id="cancelManage" '+(working?'disabled':'')+'>取消</button><button class="btn primary compact" id="confirmManage" '+(working?'disabled':'')+'>'+(working?spinner()+'正在移入':error?'重试移入':'移入废纸篓')+'</button></div></section></div>';
    $('#cancelManage').onclick=managementCancel;$('#confirmManage').onclick=managementApply;
  }
}

paintIcons();
document.addEventListener('pointerdown',function(){document.body.classList.add('pointer-mode');document.body.classList.remove('keyboard-mode');},true);
document.addEventListener('keydown',function(e){
  document.body.classList.remove('pointer-mode');document.body.classList.add('keyboard-mode');
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='f'&&!$('#modal').open&&!$('#drawer').open){e.preventDefault();($('#historyPopover').matches(':popover-open')?$('#recordSearch'):$('#search')).focus();}
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='a'&&document.activeElement===$('#tableScroller')){e.preventDefault();$('#selectVisible').click();}
});
function positionPopover(trigger,pop,preparing=false){
  if(!preparing&&!pop.matches(':popover-open'))return;
  const anchor=trigger.getBoundingClientRect();if(anchor.bottom<=0||anchor.top>=innerHeight){pop.hidePopover();return;}
  const width=pop.id==='settingsPopover'?320:pop.id==='historyPopover'?320:pop.id==='filterDetails'?336:pop.classList.contains('calendar-popover')?292:190;
  const actualWidth=Math.min(width,innerWidth-24);pop.style.width=actualWidth+'px';pop.style.maxHeight=Math.min(420,innerHeight-24)+'px';
  let needed=pop.offsetHeight;
  if(preparing){const measure=pop.cloneNode(true);measure.removeAttribute('id');measure.removeAttribute('popover');measure.inert=true;measure.setAttribute('aria-hidden','true');Object.assign(measure.style,{position:'fixed',display:'block',visibility:'hidden',transform:'none',left:'-10000px',top:'0'});document.body.append(measure);needed=measure.offsetHeight;measure.remove();}
  const below=innerHeight-anchor.bottom-20,above=anchor.top-20,down=below>=needed||below>=above,maxHeight=Math.max(48,Math.min(420,down?below:above)),height=Math.min(needed,maxHeight);
  pop.style.maxHeight=maxHeight+'px';
  pop.style.left=Math.max(12,Math.min(anchor.left,innerWidth-actualWidth-12))+'px';
  pop.style.top=Math.max(12,Math.min(down?anchor.bottom+8:anchor.top-height-8,innerHeight-height-12))+'px';
  pop.style.setProperty('--origin',down?'top left':'bottom left');
}
$$('[popovertarget]').forEach(function(trigger){const pop=document.getElementById(trigger.getAttribute('popovertarget'));trigger.addEventListener('click',function(){if(!pop.matches(':popover-open'))positionPopover(trigger,pop,true);});pop.addEventListener('toggle',function(event){if(event.newState==='open')positionPopover(trigger,pop);});});
let popupFrame=0;
function positionOpenPopovers(){if(popupFrame)return;popupFrame=requestAnimationFrame(function(){popupFrame=0;$$('[popovertarget]').forEach(function(trigger){positionPopover(trigger,document.getElementById(trigger.getAttribute('popovertarget')));});positionPopover($('[data-select-for=fieldName]'),$('#filterDetails'));});}
window.addEventListener('resize',positionOpenPopovers);
document.addEventListener('scroll',function(event){if(!event.target.closest?.('[popover]'))positionOpenPopovers();},true);
document.addEventListener('click',function(e){
  const opener=e.target.closest('[data-open]');if(opener)showModal(opener.dataset.open);
  if(e.target.closest('[data-close]'))$('#modal').close();
  const record=e.target.closest('[data-record]');if(record)switchRecord(record.dataset.record);
});
$('#modal').addEventListener('close',function(){modalGeneration++;LuluSelect.close({immediate:true});if(this.dataset.surface==='manage'){managementEditing=null;managementPending=null;$('.dialog-head',this).inert=false;$('#recordTrigger').focus({preventScroll:true});}});
$('#modal').addEventListener('cancel',function(event){if(this.dataset.surface==='manage'&&(managementPending||managementEditing)){event.preventDefault();managementCancel();}});
$('#modal').addEventListener('keydown',function(event){
  if(this.dataset.surface!=='manage'||!managementPending)return;
  if(event.key==='Escape'){event.preventDefault();event.stopPropagation();managementCancel();return;}
  if(event.key!=='Tab')return;
  const buttons=$$('.manage-confirm-card button:not(:disabled)',this),first=buttons[0],last=buttons.at(-1);
  if(!first){event.preventDefault();$('#manageConfirmCard').focus();return;}
  if(event.shiftKey&&(document.activeElement===first||!buttons.includes(document.activeElement))){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&(document.activeElement===last||!buttons.includes(document.activeElement))){event.preventDefault();first.focus();}
});
$('#modal').addEventListener('cancel',function(event){if(this.dataset.surface==='clearLogin'&&$('#confirmClearLogin')?.disabled)event.preventDefault();});
$('#closeModal').onclick=function(){const modal=$('#modal');if(modal.dataset.surface==='feishu')perform('feishu_control',{action:'cancel'});else modal.close();};
$('#closeDrawer').onclick=function(){$('#drawer').close();};
[$('#modal'),$('#drawer')].forEach(function(d){d.addEventListener('click',function(e){if(e.target!==d||(d.id==='modal'&&managementPending?.status==='working')||(d.dataset.surface==='clearLogin'&&$('#confirmClearLogin')?.disabled))return;const r=d.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom){if(d.dataset.surface==='feishu')perform('feishu_control',{action:'cancel'});else d.close();}});});
$$('[data-mode]').forEach(function(b,index){
  b.onclick=async function(){
    if(modePending||!api||busy.collect)return;
    const previous=mode;
    mode=b.dataset.mode;modePending=true;updateMode();
    const saved=await perform('mode',{value:mode});
    modePending=false;
    if(!saved){mode=previous;updateMode();}
  };
  b.onkeydown=function(e){if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const buttons=$$('[data-mode]'),next=e.key==='Home'?0:e.key==='End'?1:1-index;buttons[next].click();buttons[next].focus();}};
});
$('#collectStart').onclick=collect;
$('#homepage').oninput=()=>checkHomepage(true);
$('#homepage').onkeydown=function(e){if(e.key==='Enter'&&!e.isComposing)collect();};
$('#loginButton').onclick=()=>browserLogin(loginActive);
$('#selectVisible').onchange=function(event){
  if(event.isTrusted && document.activeElement!==tableScroller)this.focus({preventScroll:true});
  const keys=currentSelection(),checked=this.checked;
  for(const work of views)checked?keys.add(work.id):keys.delete(work.id);
  batchError='';paintSelection();updateSelection();
};
$('#clearSelection').onclick=function(){currentSelection().clear();batchError='';paintSelection();updateSelection();};
$('#clearFilters').onclick=clearFilters;$('#emptyReset').onclick=clearFilters;
$('#transcribeSelected').onclick=function(){stageWorks();};
$('#focusToggle').onclick=()=>WorkspaceUI.toggleFocus();

async function perform(action,args={}) {
  if (!api) return null;
  try { return await api.call(action,args); }
  catch (error) { toast(error.message,null,null,true); return null; }
}

function updateRecordState(value) {
  $('#restoreLegacy').hidden=!value.legacyRemoved;
  const before = recordKey;
  const structureChanged = value.historyVersion !== updateRecordState.historyVersion;
  if (structureChanged) {
    const previous = records;
    records = Object.fromEntries(value.records.map(record => [record.id,{
      ...record, works:previous[record.id]?.works || [], selection:previous[record.id]?.selection || new Set(),
      scroll:previous[record.id]?.scroll || 0,
    }]));
    updateRecordState.historyVersion=value.historyVersion;
  }
  recordKey=value.selected;
  if (!recordKey && (value.works?.length ?? records.current?.works.length)) {
    recordKey='current';
    records.current ||= {name:'当前采集结果',count:value.works.length,works:[],selection:new Set(),scroll:0};
  }
  const current=records[recordKey];
  const changedRecord=before!==recordKey;
  const changedWorks=changedRecord||value.worksVersion===undefined||value.worksVersion!==updateRecordState.worksVersion;
  updateRecordState.worksVersion=value.worksVersion;
  if (changedWorks && current && value.works) {
    const old = new Map(current.works.map(work => [work.id,work]));
    current.works = value.works.map(work => {
      const known = old.get(work.id);
      if (known) { Object.assign(known,work); return known; }
      return work;
    });
    if (changedRecord) {
      current.selection.clear();
      selectionAnchor=null;
      CollectionFilters.searchReset();
    }
  }
  if (changedRecord) {
    rowNodes.clear();
    workBody.replaceChildren();
  }
  $('#recordName').textContent=current?.name || '暂无采集记录';
  if (structureChanged || changedRecord) {
    historyOptions();
    if ($('#modal').open && $('#modal').dataset.surface==='manage') renderManagement();
  }
  busy.collect=value.running;
  collectingRecord=value.running?recordKey:'';
  loggedIn=value.loggedIn;
  loginLabel=value.loginLabel||(loggedIn?'已登录':'未登录');
  loginBlockReason=value.loginBlockReason||'';
  retryBlockReason=value.retryBlockReason||'';
  loginSaving=!!value.loginSaving;
  canClearLogin=value.canClearLogin??loggedIn;
  loginBusy=!!value.loginBusy;
  loginActive=!!value.loginActive;
  partialRetryLink=value.partialRetry?value.link.trim():null;
  if(!modePending)mode=value.mode;
  $('#saveMedia').checked=value.saveMedia;
  $('#saveCover').checked=value.saveCover;
  if (value.linkVersion!==updateRecordState.linkVersion) {
    const initial=updateRecordState.linkVersion===undefined;
    updateRecordState.linkVersion=value.linkVersion;
    if ($('#homepage').value!==value.link && (!initial || !$('#homepage').value)) {
      $('#homepage').value=value.link;
      checkHomepage();
    }
  }
  $('#homepage').disabled=busy.collect;
  $('#collectStop').hidden=!busy.collect;
  $('#collectStop').disabled=!!value.stopping||stopPending;
  $('#collectStop').textContent=value.stopping?'正在停止…':'停止采集';
  $('#recordTrigger').disabled=busy.collect||recordPending;
  $$('[data-mode]').forEach(button => { button.disabled=busy.collect; });
  if (value.browsers && value.browsers.join('|')!==updateRecordState.browsers) {
    $('#browserChoice').replaceChildren(...value.browsers.map(name=>new Option(name,name)));
    updateRecordState.browsers=value.browsers.join('|');
  }
  if (value.browser) {
    $('#browserChoice').value=value.browser;
    updateRecordState.browser=value.browser;
  }
  updateMode();
  setStatus(value.message || '准备就绪，已有作品会自动保留',busy.collect?'working':'');
  LuluSelect.syncAll();
  if (changedWorks) render();
  if (changedRecord) {
    tableScroller.scrollTop=current?.scroll || 0;
    renderViewport();
  }
}

async function collect() {
  if (!api || busy.collect || collectPending || loginBusy || loginPending) return;
  if (linkCheck.code==='checking') {
    submitAfterCheck=linkCheckVersion;
    return;
  }
  if (linkCheck.code!=='ready') {
    setStatus(linkCheck.message,'error');
    $('#homepage').focus();
    return;
  }
  if (mode==='full'&&!loggedIn) return browserLogin(false);
  const link=$('#homepage').value;
  const retry=mode==='partial'&&partialRetryLink===link.trim();
  collectPending=true;
  updateMode();
  const result=await perform(retry?'retry_partial':'collect',retry?{link}:{link,mode});
  collectPending=false;
  if (result) updateRecordState(result);
  else updateMode();
}

async function browserLogin(cancel) {
  if (!api||loginSaving||loginPending||busy.collect||(!cancel&&(loginBusy||loginBlockReason))) return;
  loginPending=true;
  updateMode();
  const result=await perform('login',{cancel});
  loginPending=false;
  if (result) updateRecordState(result);
  else updateMode();
}

async function switchRecord(key) {
  if (!records[key]||recordPending||busy.collect) return;
  closePopovers();
  $('#recordTrigger').focus({preventScroll:true});
  if (key===recordKey) return;
  if (records[recordKey]) records[recordKey].scroll=$('#tableScroller').scrollTop;
  recordPending=true;
  $('#recordTrigger').disabled=true;
  $('#recordTrigger').setAttribute('aria-busy','true');
  $('#tableScroller').inert=true;
  const value=await perform('select_record',{id:key,version:updateRecordState.historyVersion});
  recordPending=false;
  $('#recordTrigger').disabled=busy.collect;
  $('#recordTrigger').setAttribute('aria-busy','false');
  $('#tableScroller').inert=false;
  if (value) updateRecordState(value);
}

async function stageWorks(explicit) {
  const works=explicit || allSelectedWorks();
  if (!works.length || busy.batch) return;
  if (!works.some(work=>work.kind!=='image'&&['douyin','xiaohongshu'].includes(work.platform)&&work.work_url)) {
    return toast(works.every(work=>work.kind==='image')?'选中的都是图文帖（无音频），无法转录':'选中的作品缺少可靠链接或尚不支持转录',null,null,true);
  }
  busy.batch=true;
  batchError='';
  setButton($('#transcribeSelected'),'正在加入',true,'loader');
  updateSelection();
  const result=await perform('transcribe',{id:recordKey,keys:works.map(work=>work.id)});
  busy.batch=false;
  if (!result) batchError='暂未加入，选择已保留';
  if (result?.message) toast(result.message);
  updateSelection();
}

async function assetAction(work,key,action) {
  if (key==='text') return showManuscript(work);
  if (action==='view') {
    const details=await perform('asset_detail',{id:work.id,kind:key});
    if (details) showAsset(work,key,details);
    return;
  }
  const result=await perform('asset',{id:work.id,kind:key,state:''});
  if (result) updateRecordState(result);
}

function showAsset(work,key,details) {
  closePopovers();
  const dialog=$('#modal');
  dialog.dataset.surface='asset';
  $('#modalTitle').textContent='素材详情';
  $('#modalBody').innerHTML=`<p class="dialog-copy">${esc(work.title)}</p>
    <div class="detail-cover">${icon(key==='cover'?'image':'video')}</div><p class="dialog-copy">${esc(details.name)}</p>
    <div class="dialog-buttons"><button class="btn secondary" id="assetOpen">打开素材</button>
    <button class="btn secondary" id="assetReveal">在 Finder 中显示</button>
    <button class="btn primary" data-close>返回列表</button></div>`;
  $('#assetOpen').onclick=()=>perform('asset',{id:work.id,kind:key,state:'ready'});
  $('#assetReveal').onclick=()=>perform('reveal',{id:work.id,kind:key});
  if (!dialog.open) dialog.showModal();
}

async function showManuscript(work) {
  const result = await perform('manuscript',{id:work.id});
  if (!result) return;
  const dialog = $('#modal');
  dialog.dataset.surface = 'manuscript';
  $('#modalTitle').textContent = '查看文稿';
  $('#modalBody').innerHTML = '<p class="dialog-copy">'+esc(work.title)+'</p><div class="dialog-callout manuscript-text">'+esc(result.text)+'</div><div class="dialog-buttons"><button class="btn primary" data-close>返回列表</button></div>';
  if (!dialog.open) dialog.showModal();
}

function showModal(kind) {
  const selected=allSelectedWorks();
  if (kind==='feishu') {
    if (!selected.length) return;
    closePopovers();
    perform('feishu',{id:recordKey,keys:selected.map(work=>work.id)});
    return;
  }
  modalGeneration++;
  const generation=modalGeneration;
  const dialog=$('#modal');
  dialog.dataset.surface=kind;
  $('.dialog-head',dialog).inert=false;
  closePopovers();
  const body=$('#modalBody');
  const title=$('#modalTitle');
  if (kind==='manage') {
    title.textContent='管理采集记录';
    managementEditing=null;managementPending=null;
    body.innerHTML='<div id="manageList"></div>';
    renderManagement();
  } else if (kind==='clearLogin') {
    if (!canClearLogin || loginBusy || loginSaving || loginBlockReason) return;
    title.textContent='清除抖音登录？';
    body.innerHTML='<p class="dialog-copy">清除 Lulu 专用浏览器和直播录制记住的抖音登录，下次使用时需要重新登录。</p><p class="dialog-callout">已采集资料、文稿、录像和日常浏览器的登录都会保留。</p><p class="dialog-status" id="clearLoginError" role="alert"></p><div class="dialog-buttons"><button class="btn secondary" id="cancelClearLogin">取消</button><button class="btn primary" id="confirmClearLogin">清除登录</button></div>';
    $('#cancelClearLogin').onclick=()=>dialog.close();
    $('#confirmClearLogin').onclick=async function(){
      if (this.disabled) return;
      this.disabled=true;
      $('#cancelClearLogin').disabled=true;
      $('#closeModal').disabled=true;
      let value;
      try {value=await perform('clear_login');}
      finally {$('#closeModal').disabled=false;}
      if (generation!==modalGeneration || !dialog.open) return;
      if (value) {updateRecordState(value);dialog.close();}
      else {
        this.disabled=false;$('#cancelClearLogin').disabled=false;
        $('#clearLoginError').textContent=loginBlockReason||'清除尚未开始，请查看提示后重试。';
      }
    };
  } else if (kind==='export') {
    title.textContent='导出采集表格';
    const works=selected.length?selected:views;
    const targetRecord=recordKey;
    body.innerHTML='<p class="dialog-copy">导出'+(selected.length?'已选':'当前可见')+'的 '+works.length+' 条作品。原始资料保持完整。</p><div class="format-options"><label><input type="radio" name="format" value="xlsx" checked>Excel 表格</label><label><input type="radio" name="format" value="csv">CSV 文本</label></div><div id="exportPreview"></div><div class="dialog-buttons"><button class="btn secondary" data-close>返回</button><button class="btn primary" id="exportConfirm">'+icon('export')+'导出表格</button></div>';
    $('#exportConfirm').onclick=async function(){
      this.disabled=true;
      setButton(this,'正在生成',true);
      const format=$('input[name=format]:checked').value;
      const result=await perform('export',{id:targetRecord,keys:works.map(work=>work.id),format});
      if (generation!==modalGeneration || !dialog.open) return;
      this.disabled=false;
      setButton(this,'导出表格',false,'export');
      if (result?.saved) {
        $('#exportPreview').innerHTML='<p class="dialog-status">已导出 '+result.count+' 条作品</p>';
        setButton(this,'再次导出',false,'export');
      }
    };
  } else return;
  LuluSelect.enhance();
  if (!dialog.open) dialog.showModal();
  if (kind==='manage') $('#manageSearch').focus({preventScroll:true});
  if (kind==='clearLogin') $('#cancelClearLogin').focus({preventScroll:true});
}

async function managementApply() {
  const pending=managementPending;
  if (!pending || pending.status==='working') return;
  if (managementBlocked(pending.keys)) {
    pending.status='error';
    renderManagement();
    return;
  }
  pending.status='working';
  renderManagement();
  $('#manageConfirmCard').focus({preventScroll:true});
  const value=await perform('trash_records',{ids:pending.keys,version:pending.version});
  if (managementPending!==pending || !$('#modal').open) return;
  if (!value) {
    pending.status='error';
    renderManagement();
    $('#confirmManage').focus({preventScroll:true});
    return;
  }
  managementSelection.clear();managementPending=null;managementEditing=null;
  updateRecordState(value);
  renderManagement();
  $('#manageSearch').focus({preventScroll:true});
  toast('已将 '+pending.keys.length+' 份记录移入系统废纸篓');
}

async function managementSaveName() {
  const edit=managementEditing;
  if (!edit || edit.saving || !records[edit.key]) return;
  const name=$('#manageRenameInput').value.trim();
  if (!name) {edit.error=true;edit.draft='';renderManagement();$('#manageRenameInput').focus();return;}
  edit.saving=true;
  const input=$('#manageRenameInput');input.disabled=true;
  let value;
  try {value=await perform('rename_record',{id:edit.key,name,version:edit.version});}
  finally {edit.saving=false;if(input.isConnected)input.disabled=false;}
  if (!value || edit!==managementEditing) return;
  managementEditing=null;
  updateRecordState(value);
  if ($('#manageList')) { renderManagement();managementFocus('rename',edit.key); }
}

$('#collectStop').onclick=async()=>{
  if(stopPending||!busy.collect)return;
  stopPending=true;
  $('#collectStop').disabled=true;
  const result=await perform('stop');
  stopPending=false;
  if(result)updateRecordState(result);
  else $('#collectStop').disabled=false;
};
$('#clearLogin').onclick=()=>showModal('clearLogin');
$('#restoreLegacy').onclick=async function(){
  if(this.disabled)return;this.disabled=true;
  try {const value=await perform('restore_legacy',{version:updateRecordState.historyVersion});if(value)updateRecordState(value);}
  finally {this.disabled=false;}
};
$('#saveMedia').onchange=event=>perform('material_pref',{kind:'media',enabled:event.target.checked});
$('#saveCover').onchange=event=>perform('material_pref',{kind:'cover',enabled:event.target.checked});
$('#browserChoice').onchange=async event=>{
  const result=await perform('browser',{name:event.target.value});
  if(result)updateRecordState(result);
  else if(updateRecordState.browser){$('#browserChoice').value=updateRecordState.browser;LuluSelect.syncAll();}
};
CollectionFilters.mount(()=>render());
WorkspaceUI.reset();
updateMode();
render();
LuluSelect.enhance();
connectWorkspace((event,value)=>{
  if (event==='snapshot') updateRecordState(value);
  if (event==='feishu') openFeishu(value);
  if (event==='motion') document.body.classList.toggle('reduce-motion',value);
  if (event==='material') {
    const work=workById.get(value.id);
    if (work) {
      work[value.kind]=value.state;
      work[value.kind+'Progress']=value.progress;
      const row=rowNodes.get(work.id);
      if (row) updateRow(work,row);
    }
  }
  if (event==='material_states') {
    for(const status of value){
      const work=workById.get(status.id);
      if(!work)continue;
      Object.assign(work,status);
      const row=rowNodes.get(status.id);
      if(row)updateRow(work,row);
    }
  }
}).then(bridge=>{
  api=bridge;
  bridge.ready();
  if($('#homepage').value)checkHomepage();
});

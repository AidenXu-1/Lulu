'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const paths = {
  settings:'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  folder:'M3 7V5h6l2 2h10v13H3z M3 9h18',
  skill:'M8 4h8v4h4v8h-4v4H8v-4H4V8h4z M9 9h6v6H9z',
  terminal:'M4 5h16v14H4z M7 9l3 3-3 3 M13 15h4',
  feishu:'M4 5l8 4 8-4-4 9-4 6-4-6z M4 5l8 9 8-9 M8 14h8',
  info:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18 M12 11v6 M12 7v.2',
  search:'M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13 M16 16l5 5',
  chevron:'M9 5l7 7-7 7',
  check:'M5 12l4 4L19 6',
  close:'M6 6l12 12 M18 6L6 18',
  copy:'M8 8h12v13H8z M4 16V3h12',
  arrow:'M5 12h14 M13 6l6 6-6 6',
  external:'M13 4h7v7 M20 4L9 15 M10 5H4v15h15v-6',
  type:'M4 7V4h16v3 M12 4v16 M8 20h8',
  moon:'M20 14a8 8 0 0 1-10-10 9 9 0 1 0 10 10',
  bell:'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5z M10 20h4 M12 2v2',
  shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
  document:'M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6',
  audio:'M4 10v4 M8 6v12 M12 3v18 M16 7v10 M20 10v4',
  download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
  upload:'M12 16V5 M7 10l5-5 5 5 M4 16v5h16v-5',
  play:'M8 4l13 8-13 8z',
  archive:'M4 8h16v13H4z M3 3h18v5H3z M9 12h6',
  eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  refresh:'M20 9a8 8 0 1 0 0 7 M20 3v6h-6',
  link:'M10 8l3-3a4 4 0 0 1 6 6l-3 3 M14 16l-3 3a4 4 0 0 1-6-6l3-3 M8 16l8-8',
  cloud:'M6 18a4 4 0 1 1 0-8 6 6 0 0 1 12-1 4.5 4.5 0 0 1 0 9z',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.document}"></path></svg>`;
const btn = (text, action, options = {}) => `<button class="btn ${options.kind || ''}" data-action="${action}" ${options.id ? `data-id="${esc(options.id)}"` : ''} ${options.disabled ? 'disabled' : ''} ${options.extra || ''}>${options.icon ? icon(options.icon) : ''}${text}</button>`;
const badge = (text, kind = '') => `<span class="badge ${kind}"><span class="pill-dot"></span>${esc(text)}</span>`;
const toggle = (key, value, label, extra = '') => `<button type="button" class="switch" role="switch" aria-checked="${!!value}" aria-label="${esc(label)}" data-pref="${key}" ${extra}></button>`;
const section = (title, content, symbol = '', aside = '') => `<section class="group"><div class="section-heading">${symbol ? icon(symbol) : ''}<h2>${title}</h2>${aside ? `<span class="aside">${aside}</span>` : ''}</div>${content}</section>`;
const row = (id, title, description, control, extra = '') => `<div class="setting-row" id="${id}" ${extra}><div class="row-text"><div class="row-title">${title}</div><div class="row-description">${description}</div></div><div class="row-control">${control}</div></div>`;
const segment = (items, current, action, extra = '') => `<div class="segmented" style="--count:${items.length};--index:${Math.max(0, items.findIndex(([key]) => key === current))}" ${extra}>${items.map(([key,title]) => `<button data-action="${action}" data-value="${esc(key)}" aria-pressed="${key === current}">${title}</button>`).join('')}</div>`;
const disclosure = (title, body, aside = '', id = '') => `<details class="surface" ${id ? `id="${id}"` : ''}><summary class="disclosure">${icon('chevron')}${title}${aside ? `<small>${aside}</small>` : ''}</summary><div class="details-body">${body}</div></details>`;
const CLI_EXAMPLES = [
  {text:'帮我提取该作品链接的视频、封面和逐字稿。',detail:'提供作品链接和素材保存位置。Agent 会先核对当前 Lulu 的下载与转录能力。'},
  {text:'把这段会议录音转成区分发言人的逐字稿。',detail:'提供录音文件，并使用支持区分发言人的可用模型，例如已经就绪的 MOSS。结果通常以匿名编号区分发言人，不会自动知道真实姓名。'},
  {text:'用我的音色给这篇文稿配音。',detail:'提供文稿，并指定已经保存在 Lulu 中的参考音色。有多个音色时，先确认要用哪一个。'},
];
const FONT_SIZE = {min:90,max:130,default:100};
const fontProgress = value => (value-FONT_SIZE.min)/(FONT_SIZE.max-FONT_SIZE.min)*100;
function fontSlider(value) {
  return `<div class="font-slider-control"><div class="font-slider-summary"><output id="font-size-value" for="font-range" aria-hidden="true">${value}%</output><button type="button" class="font-reset" data-action="font-reset" ${value===FONT_SIZE.default?'disabled':''}>恢复默认</button></div><input class="font-range" id="font-range" type="range" min="${FONT_SIZE.min}" max="${FONT_SIZE.max}" step="1" value="${value}" aria-label="界面字号" aria-valuetext="${value}%${value===FONT_SIZE.default?'，标准字号':''}" style="--font-progress:${fontProgress(value)}%"><div class="font-slider-bounds" aria-hidden="true"><span><span class="font-endpoint small">A</span>${FONT_SIZE.min}%</span><span>${FONT_SIZE.max}%<span class="font-endpoint large">A</span></span></div></div>`;
}
function setFontSize(value) {
  const number=Number(value);
  const size=Math.min(FONT_SIZE.max,Math.max(FONT_SIZE.min,Number.isFinite(number)?Math.round(number):FONT_SIZE.default));
  state.prefs.font=size;document.documentElement.style.setProperty('--scale',String(size/100));
  const slider=$('#font-range');
  if(slider){slider.value=String(size);slider.style.setProperty('--font-progress',fontProgress(size)+'%');slider.setAttribute('aria-valuetext',size+'%'+(size===FONT_SIZE.default?'，标准字号':''));$('#font-size-value').textContent=size+'%';$('[data-action="font-reset"]').disabled=size===FONT_SIZE.default;}
}
const pages = {
  general:{title:'通用',icon:'settings',scenes:[['normal','正常状态'],['permission','通知未授权']]},
  storage:{title:'文件与存储',icon:'folder',scenes:[['normal','正常状态'],['failure','迁移失败']]},
  skills:{title:'技能',icon:'skill',scenes:[['normal','正常状态'],['empty','空列表'],['failure','读取失败']]},
  cli:{title:'CLI 与 Agent',icon:'terminal',scenes:[['normal','尚未连接'],['ready','已连接'],['failure','连接异常']]},
  feishu:{title:'飞书知识库',icon:'feishu',scenes:[['normal','首次连接'],['ready','已连接'],['expired','授权失效']]},
  about:{title:'关于 Lulu',icon:'info',scenes:[['normal','当前版本'],['available','发现更新'],['failure','下载失败']]},
};

let state = {page:'general',prefs:{font:100,notification:'never'},paths:[],managed:[],skills:[],
  skillFilter:'all',skillQuery:'',skillStatus:'all',scenes:{},meta:{features:[],unavailable:[],callbacks:[]},
  feishu:{step:0},update:{state:'current',progress:0,presentation:['软件更新','','检查更新','check-update','check']}};
let api, pageAnimation, toastTimer, modalAnimation, previousFocus, modalClosing = false;
let folderChoice, skillDraft, skillLoad = 0, skillsLoading = false, navigationPending = false;
const modal = $('#modal');
const scroll = $('#page-scroll');
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
  || document.documentElement.classList.contains('keyboard-input') || document.body.classList.contains('reduce-motion');
function animateContent(element, distance = 5) {
  if (!element || reduced()) return;
  return element.animate([{opacity:.55,transform:`translateY(${distance}px)`},{opacity:1,transform:'translateY(0)'}],{duration:210,easing:'cubic-bezier(.22,1,.36,1)'});
}
function toast(text) {
  clearTimeout(toastTimer);$('#toast').innerHTML = icon('check') + `<span>${esc(text)}</span>`;$('#toast').classList.add('visible');
  toastTimer = setTimeout(() => $('#toast').classList.remove('visible'), 3200);
}
function renderNavigation() {
  $('#settings-title').innerHTML = icon('settings');
  $('#search-icon').innerHTML = icon('search');
  $('#navigation').innerHTML = `<div id="nav-ink" class="nav-ink"></div>` + [['日常偏好',['general','storage']],['扩展与连接',['skills','cli','feishu']],['LULU',['about']]].map(([name,keys]) => `<div class="nav-group"><div class="nav-heading">${name}</div>${keys.map(key => `<button class="nav-button" data-action="navigate" data-id="${key}" aria-current="${state.page === key ? 'page' : 'false'}">${icon(pages[key].icon)}<span>${pages[key].title.replace('关于 Lulu','关于')}</span></button>`).join('')}</div>`).join('');
}
function moveNavInk(instant = false) {
  const selected = $('.nav-button[aria-current="page"]');const ink = $('#nav-ink');
  if (!selected || !ink) return;
  ink.classList.toggle('instant',instant || reduced());ink.style.transform = `translateY(${selected.offsetTop}px)`;ink.style.height = selected.offsetHeight + 'px';
}
async function navigate(page, target = '', animate = true, notify = true) {
  if (!pages[page]) return;
  if (api && notify) {
    if (navigationPending) return;
    navigationPending=true;
    try {await api.call('category',{page});}
    catch(error) {toast(error.message);return;}
    finally {navigationPending=false;}
  }
  if (page!=='feishu' && state.page==='feishu') {
    state.feishu.secret='';
    state.feishu.step=0;
  }
  $('#global-search').value='';state.page=page;
  $$('.nav-button').forEach(button => button.setAttribute('aria-current',button.dataset.id===page?'page':'false'));
  $('#page-title').textContent=pages[page].title;
  renderCurrent(false);moveNavInk(!animate);
  if(page==='skills' && api) loadSkills();
  pageAnimation?.cancel();if(animate)pageAnimation=animateContent($('#page-content'));
  if(target) requestAnimationFrame(()=>{const item=document.getElementById(target);if(item){item.scrollIntoView({block:'center',behavior:reduced()?'instant':'smooth'});item.classList.add('highlighted');setTimeout(()=>item.classList.remove('highlighted'),1800);}});
}
function renderCurrent(preserve = true) {
  const top = preserve ? scroll.scrollTop : 0;
  LuluSelect.close({immediate:true});
  $('#page-content').innerHTML=({general:renderGeneral,storage:renderStorage,skills:renderSkills,cli:renderCli,feishu:renderFeishu,about:renderAbout})[state.page]();
  bindDisclosures();LuluSelect.enhance();scroll.scrollTop=top;
  if(state.page==='general'){syncNotifications();setFontSize(state.prefs.font);reserveTaskHeight();}
  if(state.page==='feishu' && $('#feishu-secret')) $('#feishu-secret').value=state.feishu.secret||'';
}

function reserveTaskHeight() {
  const task=$('#awake');if(!task)return;
  // Reserve only the space its own text needs at the supported maximum size.
  const measure=task.cloneNode(true);measure.removeAttribute('id');measure.inert=true;measure.setAttribute('aria-hidden','true');
  Object.assign(measure.style,{position:'fixed',left:'-10000px',top:'0',width:task.getBoundingClientRect().width+'px',minHeight:'',fontSize:14*FONT_SIZE.max/100+'px',visibility:'hidden',pointerEvents:'none'});
  measure.style.setProperty('--scale',String(FONT_SIZE.max/100));document.body.append(measure);
  task.style.minHeight=Math.ceil(measure.getBoundingClientRect().height)+'px';measure.remove();
}

function renderGeneral() {
  const p=state.prefs;
  const reading=`<div class="surface">${row('font-size','界面字号','为阅读、改稿和日常操作，选一个舒服的大小。',fontSlider(p.font))}</div>`;
  const running=`<div class="surface">${row('awake','任务期间保持 Mac 唤醒','处理或录制结束后恢复系统原有行为，屏幕仍可自动熄灭。',toggle('awake',p.awake,'任务期间保持 Mac 唤醒'))}</div>`;
  const notifications=`<div class="surface">${row('notification-time','何时通知','你在别处工作时，也能知道 Lulu 的处理结果。',`<select class="control-select" data-pref="notification" aria-label="何时通知"><option value="inactive" data-description="在其他应用工作时提醒" ${p.notification==='inactive'?'selected':''}>仅 Lulu 在后台时</option><option value="always" data-description="无论前台或后台都通知" ${p.notification==='always'?'selected':''}>始终通知</option><option value="never" data-description="仅在 Lulu 任务区保留状态" ${p.notification==='never'?'selected':''}>从不通知</option></select>`)}
    ${row('notification-events','通知哪些情况','批量任务汇总提醒，直播正常分段保持安静。',`<div class="check-group notification-options" role="group" aria-label="通知情况，可多选">${[['completed','完成'],['failed','失败'],['attention','需要你处理']].map(([key,title])=>`<label class="check-label notification-choice"><input type="checkbox" data-pref="${key}" ${p[key]?'checked':''}><span>${title}</span></label>`).join('')}</div>`,'data-notification-dependent')}
    ${row('notification-sound','通知声音','仅在允许发送任务通知时播放。',toggle('sound',p.sound,'通知声音'),'data-notification-dependent')}
    <div class="setting-row"><p class="inline-note" id="notification-note">通知仅显示任务状态，内容详情留在 Lulu 内。</p><div class="row-control">${btn('试看提醒','notification-preview',{kind:'quiet',icon:'bell'})}</div></div></div>`;
  const permission=`<div class="surface">${row('permissions','系统权限','查看通知、麦克风与系统声音录制相关权限。',btn('查看与管理','permissions',{icon:'chevron'}))}</div>`;
  const taskChoices=disclosure('任务结束与退出',`${row('confirm-exit','有任务时，退出前确认','保留你此前的选择，避免误关正在处理的任务。',toggle('confirmExit',p.confirmExit,'有任务时退出前确认'))}${row('completion-action','完成后如何查看结果','批量处理时，保持当前页面可减少操作中断。',`<select class="control-select" data-pref="completionAction" aria-label="完成后如何查看结果"><option value="stay" ${p.completionAction==='stay'?'selected':''}>保持当前页面</option><option value="open_result" ${p.completionAction==='open_result'?'selected':''}>打开对应结果</option></select>`)}`,'保留原有偏好');
  return section('任务运行',running+taskChoices,'moon')+section('阅读与操作',reading,'type')+section('结果提醒',notifications,'bell')+section('系统权限',permission,'shield');
}
function syncNotifications() {
  const disabled=state.prefs.notification==='never';
  $$('[data-notification-dependent]').forEach(row=>{row.dataset.disabled=String(disabled);row.classList.add('dependent');$$('input,button',row).forEach(control=>control.disabled=disabled);});
  const noEvents=!['completed','failed','attention'].some(key=>state.prefs[key]);
  if(noEvents){$('#notification-sound').dataset.disabled='true';$('#notification-sound button').disabled=true;}
  $('#notification-note').textContent=disabled?'通知已关闭，以上偏好将保留。':noEvents?'还未选择提醒情况，声音偏好会保留。':'通知仅显示任务状态，内容详情留在 Lulu 内。';
  $('[data-action="notification-preview"]').disabled=disabled||noEvents;
}
function renderStorage() {
  const renderPath = item => `<div class="path-row" id="path-${item.id}"><div class="path-icon">${icon(item.icon)}</div><div class="path-content"><div class="path-name">${esc(item.title)}</div><div class="path-location"><span>${esc(item.path.split('/').filter(Boolean).slice(-2).join(' › '))}</span><button class="subtle-link" data-action="path-toggle" data-id="${item.id}" aria-expanded="false" aria-controls="full-${item.id}">路径</button></div><div class="path-hint">${esc(item.hint)}</div><div class="path-details" id="full-${item.id}" hidden><code>${esc(item.path)}</code><div>${btn('复制路径','copy-path',{kind:'quiet compact',id:item.id,icon:'copy'})}</div></div></div><div class="path-actions">${btn('更改…','folder-change',{id:item.id})}</div></div>`;
  const groups=[['导出与下载',state.paths.filter(p=>p.kind==='export'),'download'],['音色与模型',state.paths.filter(p=>p.kind!=='export'),'archive']].map(([title,items,symbol])=>section(title,`<div class="surface">${items.map(renderPath).join('')}</div>`,symbol)).join('');
  const managed=state.managed;
  const old=state.inheritance;
  const status = old?.active ? ['正在兼容旧版资料','Lulu 正在检查旧版记录。原资料会保留，新任务可以照常使用。']
    : old?.state==='failed' ? ['旧版资料检查未完成',old.error||'请稍后重试。']
    : old?.state==='partial' ? ['部分旧记录尚未合并','已核验且没有冲突的任务已继承，其他旧原件完整保留。'] : null;
  const recovery=status?section('旧版资料',`<div class="surface">${row('legacy-inheritance',esc(status[0]),esc(status[1]),
    old.state==='failed'&&!old.active?btn('重试资料检查','retry-inheritance'):'')}</div>`,'archive'):'';
  return groups+section('Lulu 管理的资料',disclosure('查看资料位置',`<p class="inline-note">文稿、音频和对话由 Lulu 保存在本机。恢复或删除资料，请在资料库的「归档」中操作。</p>${managed.map(([title,desc,path])=>`<div class="managed-row"><div>${title}<p>${desc}</p></div>${btn('查看位置',path?'managed-path':'live-location',{kind:'quiet',extra:`data-title="${title}" data-path="${esc(path)}"`})}</div>`).join('')}`,'本机资料，按需查看','managed-locations'),'folder')+recovery;
}
function emptyState(title, text, action = '', actionTitle = '') {
  return `<div class="empty-state">${icon('search')}<h3>${title}</h3><p>${text}</p>${action?btn(actionTitle,action):''}</div>`;
}
function renderSkills() {
  const importZone=`<div class="import-zone" id="skill-drop"><div class="import-left"><div class="import-symbol">${icon('upload')}</div><div><div class="import-title">导入你的技能</div><div class="import-meta">拖入完整技能包、ZIP 或 SKILL.md</div></div></div><div class="import-links">${btn('选择本地文件','choose-skill')}<button class="subtle-link" data-action="skill-help">查看导入要求</button></div></div>`;
  const filters=`<div class="filter-bar">${segment([['all','全部'],['system','Lulu 系统'],['user','我安装的']],state.skillFilter,'skill-filter')}<div class="search-wrap">${icon('search')}<input id="skill-search" type="search" placeholder="搜索名称或用途" aria-label="筛选技能名称或用途" value="${esc(state.skillQuery)}"></div><select class="control-select" id="skill-status" aria-label="筛选技能状态"><option value="all">全部状态</option><option value="enabled" ${state.skillStatus==='enabled'?'selected':''}>已启用</option><option value="disabled" ${state.skillStatus==='disabled'?'selected':''}>已停用</option><option value="uninstalled" ${state.skillStatus==='uninstalled'?'selected':''}>未安装</option></select></div>`;
  let list;
  if(state.scenes.skills==='failure')list=`<div class="surface">${emptyState('暂时没读到技能','已保留列表和文件，可以稍后再试。','retry-skills','重新读取')}</div>`;
  else if(state.scenes.skills==='empty')list=`<div class="surface">${emptyState('从一项技能开始','可以从本地导入技能。','choose-skill','选择本地文件')}</div>`;
  else list=skillsList();
  return importZone+filters+`<div id="skill-results">${list}</div>`;
}
function skillsList() {
  const query=state.skillQuery.toLowerCase();
  const rows=state.skills.filter(s=>(state.skillFilter==='all'||s.origin===state.skillFilter)&&(s.title+s.summary+s.name).toLowerCase().includes(query)&&(state.skillStatus==='all'||(state.skillStatus==='enabled'&&s.installed&&s.enabled)||(state.skillStatus==='disabled'&&s.installed&&!s.enabled)||(state.skillStatus==='uninstalled'&&!s.installed)));
  const installed=state.skills.filter(s=>s.installed).length;
  return `<div class="list-meta"><span>${rows.length} 项技能<span aria-hidden="true"> · </span>已安装 ${installed} 项</span></div><div class="surface">${rows.length?rows.map(s=>`<div class="skill-row"><div class="skill-symbol ${s.origin==='user'?'user':''}">${esc(s.title.slice(0,1))}</div><div class="skill-main"><div class="skill-title">${esc(s.title)}</div><div class="skill-desc">${esc(s.summary)}</div><div class="skill-meta"><span>${s.origin==='system'?'Lulu 系统':'用户导入'}</span><span>v${esc(s.version)}</span><span>${s.installed?(s.enabled?(s.automatic?'按需使用':'仅手动指定'):'已停用'):'未安装'}</span></div></div><div class="skill-actions">${s.installed?`<button class="switch" role="switch" aria-label="启用${esc(s.title)}" aria-checked="${s.enabled}" data-action="toggle-skill" data-id="${s.name}"></button>`:btn('重新安装','reinstall-skill',{id:s.name,kind:'compact'})}${btn('详情','skill-detail',{id:s.name,kind:'quiet',icon:'chevron'})}</div></div>`).join(''):emptyState('没有符合条件的技能','试试其他关键词，或调整上方筛选。')}</div>`;
}
function renderCli() {
  const ready=state.cliReady||state.scenes.cli==='ready';const failed=state.scenes.cli==='failure';
  const status=state.meta.experience?badge(failed?'受管 Skill：状态未读到':state.scenes.cli==='update_available'?'受管 Skill：需要更新':ready?'受管 Skill：已安装':'受管 Skill：未安装，可直接连接 CLI',failed?'warn':''):failed?badge('连接异常 · 待检查','error'):state.scenes.cli==='update_available'?badge('已安装 · Skill 需要更新','warn'):ready?badge('已安装 · 可调用','good'):badge('尚未连接 · 待检查');
  const hero=`<div class="connection-hero" id="cli-connection"><div class="connection-symbol">${icon('terminal')}</div><div class="connection-main"><div class="connection-top"><h2>${state.meta.experience?'让你的 Agent 调用开发体验版':'让你的 Agent 调用 Lulu'}</h2>${status}</div><p>${state.meta.experience?'复制连接提示词，按当前开发体验版的完整入口连接；原有稳定版连接保留。':'把安装提示词发给你常用的 Agent。它会检查本机 Lulu，再连接采集、转录、文稿和配音等工具。'}</p><div class="actions">${btn(state.meta.experience?'复制连接提示词':'复制安装提示词','copy-install',{kind:'primary',icon:'copy'})}${state.meta.experience?'':`<a class="btn quiet" href="${esc(state.meta.repository)}" target="_blank" rel="noreferrer noopener">查看 Skill 仓库${icon('external')}</a>`}</div>${failed?`<p class="inline-note">${state.meta.experience?'受管 Skill 状态尚未确认；包内 CLI 请按连接提示词核验。':'连接尚未确认，已有文稿和任务保持原样。'}${btn(state.meta.experience?'重查受管 Skill':'重新检查','check-cli',{kind:'compact quiet'})}</p>`:''}</div></div>`;
  const cards=[['download','素材采集与转录','作品链接、抖音主页和 RSS；批量下载、转录并保存结果。'],['document','文稿与 Obsidian','查找、改稿、版本管理，导出文稿或保存到 Obsidian。'],['audio','配音与音色','使用本地配音模型，管理参考音色并读取生成结果。'],['play','直播与任务管理','监控、录制、导出录像，查询任务与处理状态。']];
  const complete=`${state.meta.features.map(([title,description])=>`<div class="scope-row"><strong>${esc(title)}</strong><p>${esc(description)}</p></div>`).join('')}<p class="mini-note">以下功能尚无 CLI 入口：</p>${state.meta.unavailable.map(([title,description])=>`<div class="scope-row"><strong>${esc(title)}</strong><p>${esc(description)}</p></div>`).join('')}`;
  return hero+section('可以交给 Lulu 的事',`<div class="capability-grid">${cards.map(([symbol,title,text])=>`<div class="capability-card"><h3>${icon(symbol)}${title}</h3><p>${text}</p></div>`).join('')}</div>${disclosure('查看完整能力与限制',complete,'按本机版本核实')}`,'skill')+
    section('可以这样告诉 Agent',`<div class="surface">${CLI_EXAMPLES.map(({text},index)=>`<div class="prompt-example"><span>“${text}”</span>${btn('查看','cli-example',{kind:'quiet compact',id:String(index),icon:'chevron'})}</div>`).join('')}</div><p class="footnote">${icon('info')}<span>${state.meta.experience?'先复制连接提示词完成核验。后续任务请明确指定“用 Lulu 开发体验版”。':'先在你的 Agent 中安装并启用 Lulu Skill。想明确指定时，可以在任务前加“用 Lulu”。'}</span></p>`,'document')+
    section('本机连接与参考',disclosure(state.meta.experience?'受管 Skill 状态与调用入口':'连接检查与调用入口',`<p class="inline-note">CLI 功能随 Lulu 更新，Skill 提供调用指引。实际可用功能需要在连接时查询，更新 Skill 不会自动为旧 App 增加能力。</p><div class="actions" style="margin-top:14px">${btn(state.meta.experience?'检查受管 Skill':'连接检查','check-cli',{icon:'refresh'})}${btn('查看调用入口','cli-reference',{kind:'quiet'})}</div>`,'需要时再展开'),'link');
}
function renderFeishu() {
  const f=state.feishu;const expired=f.expired;const ready=f.connected&&!expired;
  const hero=`<div class="connection-hero"><div class="connection-symbol">${icon('feishu')}</div><div class="connection-main"><div class="connection-top"><h2>${ready?(f.verified?'已连接飞书':'已保存飞书连接'):expired?'需要重新连接':'把内容带到飞书知识库'}</h2>${badge(ready?(f.verified?'已连接':'使用时核验'):expired?'授权失效':!f.loaded?'待检查连接':'尚未连接',ready?'good':expired?'warn':'')}</div><p>${ready?(f.verified?'保存时选择知识库、文档或表格位置。':'本机已保存授权，实际保存时会核验账号与权限。'):expired?'已有保存位置继续保留。重新连接后，在保存时核验账号与权限。':'完成一次应用配置和账号连接，就能在文稿与采集结果中手动存入飞书。'}</p>${ready?`<div class="actions">${btn('重新连接','feishu-reconnect')}${btn('查看应用配置','feishu-config',{kind:'quiet'})}${btn('断开连接','feishu-disconnect',{kind:'quiet'})}</div>`:expired?`<div class="actions">${btn('重新连接','feishu-reconnect',{kind:'primary'})}${btn('查看应用配置','feishu-config',{kind:'quiet'})}</div>`:''}</div></div>`;
  const saved=!f.loaded?`<div class="surface"><div class="setting-row"><div class="row-text"><div class="row-title">已有配置可直接读取，无需重新填写</div><p class="row-description" role="status">${esc(f.notice||'点击后检查本机保存的飞书连接。')}</p></div>${btn(f.busy?'正在读取…':'读取已保存连接','feishu-load',{disabled:!!f.busy})}</div></div>`:'';
  const resume=f.canReturn?'<div class="actions">'+btn('返回存入飞书','feishu-return',{kind:'quiet'})+'</div>':'';
  if(ready&&!f.editing)return hero+resume+section('保存内容时，再选择位置',`<div class="surface"><div class="setting-row"><div class="row-text"><div class="row-title">从文稿或采集结果中「存入飞书」</div><div class="row-description">选择现有文档、知识库或多维表格。目标和权限会在实际保存时核验。</div></div>${badge('手动保存','blue')}</div></div>`,'document')+`<p class="footnote">${icon('shield')}<span>应用凭据保存在系统安全存储中。连接时由 Lulu 核验授权。</span></p>`;
  const steps=['应用凭据','应用权限','安全回调','连接账号'];
  const stepper=`<div class="stepper" aria-label="飞书连接步骤">${steps.map((title,index)=>`<button class="step-button ${index<f.step?'done':''}" data-action="feishu-step" data-value="${index}" aria-current="${index===f.step?'step':'false'}"><span class="step-number">${index<f.step?'✓':index+1}</span>${title}</button>`).join('')}</div>`;
  let body;
  if(f.step===0)body=`<h2>先准备你的飞书应用</h2><p>在飞书后台创建企业自建应用，再填写「凭证与基础信息」中的两项内容。</p><a class="subtle-link" href="https://open.feishu.cn/app">打开开发者后台${icon('external')}</a><label class="field"><span>App ID</span><input id="feishu-app-id" value="${esc(f.appId || '')}" placeholder="cli_…" aria-label="App ID" autocomplete="off"></label><label class="field"><span>App Secret</span><div class="secret-wrap"><input id="feishu-secret" type="password" placeholder="${f.hasSecret?'已保存在本机':'输入 App Secret'}" aria-label="App Secret" autocomplete="off"><button data-action="show-secret" aria-label="显示或隐藏 App Secret">${icon('eye')}</button></div></label><div class="actions">${btn('凭据在哪里','feishu-credentials-help',{kind:'quiet',icon:'document'})}</div>`;
  if(f.step===1)body=`<h2>开通保存内容所需的权限</h2><p>在飞书应用的「权限管理」中开通文档、知识库、表格与文件访问权限。</p><div class="permission-list">${[['文档读写','docx:document'],['知识库访问','wiki:wiki'],['表格与文件','bitable:app · drive:drive'],['保持账号连接','offline_access']].map(([title,value])=>`<div>${title}<span class="badge" style="margin-left:8px">${value}</span></div>`).join('')}</div><div class="actions">${btn('复制权限清单','copy-scopes',{icon:'copy'})}<a class="btn quiet" href="https://open.feishu.cn/app" target="_blank" rel="noreferrer noopener">前往权限管理${icon('external')}</a></div>`;
  if(f.step===2)body=`<h2>添加安全回调地址</h2><p>在应用的「安全设置 → 重定向 URL」中逐条添加以下地址。完成后，在飞书后台发布应用版本。</p>${state.meta.callbacks.map((url,index)=>`<div class="callback-row"><code>${esc(url)}</code>${btn('复制','copy-callback',{id:String(index),kind:'compact',icon:'copy'})}</div>`).join('')}<p class="mini-note">连接时会使用其中一个可用端口。</p>`;
  if(f.step===3)body=`<div class="connect-ready">${icon('link')}<h3>${f.error?'连接暂未完成':'准备好连接账号了'}</h3><p>${f.error?'已有设置已保留。请检查权限和回调地址后重试。':'应用配置完成并发布后，即可连接飞书账号。<br>连接时会在浏览器中完成账号授权。'}</p><div class="actions" style="justify-content:center;margin-top:20px">${btn(f.busy?'取消连接':f.error?'重试连接':'连接飞书',f.busy?'feishu-cancel':'feishu-connect',{kind:'primary',icon:'feishu'})}</div></div>`;
  return hero+saved+resume+stepper+`<div class="surface"><div class="surface-inner step-content">${body}<div class="step-footer">${f.step>0?btn('上一步','feishu-prev',{kind:'quiet'}):'<span class="inline-note">应用凭据保存在本机</span>'}${f.step<3?btn('下一步','feishu-next',{kind:'primary',icon:'arrow'}):btn('返回应用配置','feishu-config',{kind:'quiet'})}</div></div></div>`;
}
function renderAbout() {
  const u=state.update;
  const status=u.presentation;
  const brand=`<div class="about-brand"><img src="../shared/lulu.png" alt="Lulu 图标"><div><div class="about-name"><h2>${state.meta.experience?'Lulu 开发体验版':'Lulu'}</h2><span class="badge blue">${esc(state.meta.version)}</span></div><p>一个专为你省钱的内容助手。<br><span>构建 ${esc(state.meta.build)}</span></p></div></div>`;
  const update=`<div class="surface" id="software-update"><div class="update-summary"><div class="update-symbol ${u.state==='failure'?'error':''}">${icon(status[4])}</div><div class="row-text"><div class="row-title">${esc(status[0])}</div><div class="row-description">${esc(status[1])}</div></div>${status[2]?`<div class="actions">${btn(status[2],status[3]||'noop',{disabled:!status[3],kind:u.state==='current'||u.state==='completed'?'':'primary'})}</div>`:''}</div>${['downloading','paused','ready'].includes(u.state)?`<div class="update-progress"><div class="progress-track"><div id="update-fill" class="progress-fill" style="width:${u.progress ?? 0}%"></div></div><p id="update-progress-label">${u.state==='ready'?'已下载并完成验证':`已下载 ${Math.round(u.progress)}%`}</p></div>`:''}${u.state==='disabled'?'':`<div class="update-auto">${row('auto-update','自动检查更新','定期查看版本信息，安装前仍由你决定。',toggle('autoUpdate',state.prefs.autoUpdate,'自动检查更新'))}</div>`}</div>`;
  const info=`<div class="surface"><div class="about-link"><div>项目仓库<p>查看源码、使用说明与公开版本</p></div><a class="btn quiet" href="https://github.com/AidenXu-1/Lulu" target="_blank" rel="noreferrer noopener">打开仓库${icon('external')}</a></div><div class="about-link"><div>作者 · 兆基<p>AI 运营与内容创作</p></div><div class="actions"><a class="btn quiet" href="https://github.com/AidenXu-1" target="_blank" rel="noreferrer noopener">GitHub${icon('external')}</a>${btn('联系方式','contact',{kind:'quiet'})}</div></div><div class="about-link"><div>版本信息<p>排查问题时可以复制给提供帮助的人</p></div>${btn('复制信息','copy-version',{kind:'quiet',icon:'copy'})}</div></div>`;
  return brand+section('软件更新',update,'refresh')+`${u.notes?`<div class="group">${disclosure('查看本版说明',`<p class="inline-note">${esc(u.notes)}</p>`)}</div>`:''}`+section('产品与作者',info,'info')+section('分发说明','<p id="distribution-notice" class="inline-note">当前为信任用户内测包，未使用 Apple Developer ID 签名，也未通过 Apple 公证。首次打开可能需要你在系统设置中确认。</p>','info')+disclosure('本机处理与联网范围','<p class="inline-note">本地转录与本地配音在本机处理。链接下载、主页采集、直播、模型下载、检查更新，以及主动选择的云端模型和飞书保存会联网。使用云端模型前会告知上传内容与账户计费；Lulu 不会自动上传你的文稿。</p>');
}

function openModal(title, description, content, footer = '', wide = false) {
  LuluSelect.close({immediate:true});
  const presentation = modal.open ? {opacity:getComputedStyle(modal).opacity,transform:getComputedStyle(modal).transform} : null;
  const reversing = modalClosing;modalClosing = false;
  modalAnimation?.cancel();
  if(!modal.open)previousFocus=document.activeElement;
  modal.classList.toggle('wide',wide);
  $('#modal-body').innerHTML=`<div class="modal-header"><div><h2 id="modal-title">${title}</h2>${description?`<p>${description}</p>`:''}</div><button class="btn quiet icon-only" data-action="close-modal" aria-label="关闭弹窗">${icon('close')}</button></div><div class="modal-content">${content}</div>${footer?`<div class="modal-footer">${footer}</div>`:''}`;
  if(!modal.open){modal.showModal();if(!reduced())modalAnimation=modal.animate([{opacity:0,transform:'translateY(7px) scale(.975)'},{opacity:1,transform:'translateY(0) scale(1)'}],{duration:220,easing:'cubic-bezier(.22,1,.36,1)'});}
  else if(reversing&&!reduced())modalAnimation=modal.animate([presentation,{opacity:1,transform:'translateY(0) scale(1)'}],{duration:180,easing:'cubic-bezier(.22,1,.36,1)'});
  LuluSelect.enhance();
  requestAnimationFrame(()=>($('[data-autofocus]',modal)||$('.modal-footer button:last-child',modal)||$('button',modal))?.focus({preventScroll:true}));
}
function closeModal(immediate = false) {
  if (folderChoice?.busy) { stopFolder(); return; }
  LuluSelect.close({immediate:true});
  const presentation={opacity:getComputedStyle(modal).opacity,transform:getComputedStyle(modal).transform};
  modalAnimation?.cancel();modalClosing=true;
  const finish=()=>{modalClosing=false;modal.close();previousFocus?.isConnected&&previousFocus.focus({preventScroll:true});};
  if(!modal.open)return;
  if(immediate||reduced()){finish();return;}
  modalAnimation=modal.animate([presentation,{opacity:0,transform:'translateY(4px) scale(.985)'}],{duration:130,easing:'cubic-bezier(.4,0,1,1)'});modalAnimation.onfinish=finish;
}
function showNotification() {
  openModal('任务提醒预览','批量处理只在这一批结束时汇总提醒。',`<div class="notification-card"><div class="notification-app"><img src="../shared/lulu.png" alt="">LULU<span style="margin-left:auto">现在</span></div><strong>任务已完成</strong><p>打开 Lulu 查看处理结果。</p></div><p class="mini-note">这里展示固定隐私文案，不调用系统通知，也不播放声音。详细数量与失败原因留在任务区。</p>`,btn('了解提醒规则','notification-rules',{kind:'quiet'})+btn('看到了','close-modal',{kind:'primary'}));
}
function searchItems(){return [
  ['general','font-size','界面字号','标准、较大、更大，阅读与排版'],['general','awake','任务期间保持 Mac 唤醒','防休眠、处理任务、录制'],['general','notification-time','何时通知','任务完成提醒、后台通知'],['general','notification-events','通知哪些情况','完成、失败、需要处理'],['general','notification-sound','通知声音','提示音、安静'],['general','permissions','系统权限','麦克风、系统声音、通知授权'],
  ...state.paths.map(p=>['storage',`path-${p.id}`,p.title,`存储 文件位置 路径 ${p.hint}`]),['storage','managed-locations','Lulu 管理的资料','文稿、音频、会话、直播录像、技能目录'],['skills','','技能安装与管理','skill 本地导入、启用、卸载、恢复'],['cli','cli-connection','连接外部 Agent','CLI、Skill、安装提示词、命令、Obsidian、主页采集、RSS'],['feishu','','连接飞书知识库','应用凭据、App ID、安全回调、账号授权'],['about','software-update','软件更新','版本、自动检查、下载、安装'],['about','','作者与联系方式','兆基、微信、GitHub']
];}
function searchSettings(query) {
  if(!query.trim()){navigate(state.page,'',false);return;}
  $('#page-title').textContent='查找设置';
  const terms=query.toLowerCase().trim().split(/\s+/);const found=searchItems().filter(item=>terms.every(term=>item.slice(2).join(' ').toLowerCase().includes(term)));
  $('#page-content').innerHTML=`<div class="list-meta">${found.length} 个结果</div><div class="surface search-results">${found.length?found.map(([page,id,title,text])=>`<button class="result-row" data-action="search-result" data-id="${page}" data-target="${id}"><div><strong>${esc(title)}</strong><span>${pages[page].title} · ${esc(text)}</span></div>${icon('chevron')}</button>`).join(''):emptyState('没找到相关设置','可以换个词，例如“字号”“Obsidian”或“提醒”。')}</div>`;scroll.scrollTop=0;
}

const detailAnimations = new WeakMap();
const pathAnimations = new WeakMap();
function togglePath(button, id) {
  const content=document.getElementById('full-'+id);const opening=button.getAttribute('aria-expanded')!=='true';
  const start=content.hidden?0:content.getBoundingClientRect().height;
  button.setAttribute('aria-expanded',String(opening));button.textContent=opening?'收起':'路径';
  pathAnimations.get(content)?.cancel();content.hidden=false;content.style.height='';
  const end=opening?content.getBoundingClientRect().height:0;
  const finish=()=>{content.hidden=!opening;content.style.height='';content.style.opacity='';};
  if(reduced()){finish();return;}
  content.style.height=start+'px';
  const animation=content.animate([{height:start+'px',opacity:opening ? .3 : 1},{height:end+'px',opacity:opening?1:0}],{duration:opening?230:170,easing:'cubic-bezier(.22,1,.36,1)'});
  pathAnimations.set(content,animation);animation.onfinish=finish;
}
function bindDisclosures() {
  $$('details', $('#page-content')).forEach(details=>{
    $('summary',details).addEventListener('click',event=>{
      event.preventDefault();const summary=$('summary',details),content=$('.details-body',details);
      const wasTarget=details.dataset.targetOpen==='true'||(!details.dataset.targetOpen&&details.open);const opening=!wasTarget;details.dataset.targetOpen=String(opening);
      const start=details.getBoundingClientRect().height;detailAnimations.get(details)?.cancel();details.open=true;
      const end=summary.getBoundingClientRect().height+(opening?content.getBoundingClientRect().height:0)+2;
      const finish=()=>{details.open=opening;details.style.height='';details.style.overflow='';content.style.opacity='';};
      if(reduced()){finish();return;}
      details.style.height=start+'px';details.style.overflow='hidden';
      const animation=details.animate([{height:start+'px'},{height:end+'px'}],{duration:opening?250:190,easing:'cubic-bezier(.22,1,.36,1)'});detailAnimations.set(details,animation);animation.onfinish=finish;
      content.animate([{opacity:opening?0:1,transform:opening?'translateY(-3px)':'translateY(0)'},{opacity:opening?1:0,transform:opening?'translateY(0)':'translateY(-3px)'}],{duration:opening?200:130,fill:'none',easing:'ease-out'});
    });
  });
}

document.addEventListener('keydown',event=>{
  document.documentElement.classList.add('keyboard-input');
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='f'&&!modal.open){event.preventDefault();$('#global-search').focus();$('#global-search').select();}
  if(event.key==='Escape'&&!modal.open&&$('#global-search').value){$('#global-search').value='';navigate(state.page,'',false);$('#global-search').focus();}
  if(event.target.matches('.segmented button')&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();const buttons=$$('button',event.target.parentElement);const next=(buttons.indexOf(event.target)+(event.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;buttons[next].click();buttons[next].focus();}
});
document.addEventListener('pointerdown',()=>document.documentElement.classList.remove('keyboard-input'),{passive:true});
modal.addEventListener('cancel',event=>{event.preventDefault();closeModal();});
modal.addEventListener('click',event=>{if(event.target===modal){const rect=modal.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeModal();}});
window.addEventListener('resize',()=>{moveNavInk(true);reserveTaskHeight();});

async function perform(action, args = {}) {
  if (!api) return null;
  try {
    return await api.call(action, args);
  } catch (error) {
    toast(error.message);
    return null;
  }
}

async function savePref(key, value) {
  const result = await perform('preference',{key,value});
  if (!result) {
    const saved = await perform('preferences');
    if (state.prefs[key] !== value) return;
    if (saved) state.prefs[key] = saved[key];
    renderCurrent();
  }
}

async function showPermissions() {
  openModal('系统权限','在 macOS 中查看和管理权限。',
    [['通知','notifications'],['麦克风','microphone'],['系统声音录制','system']].map(([title,id]) =>
      `<div class="permission-item"><div>${title}</div><span id="permission-${id}" class="badge">尚未检测</span></div>`).join('')
      + '<p class="mini-note">需要使用录音或通知时，再由你完成授权。系统权限的最终状态以 macOS 设置为准。</p>',
    btn('了解设置位置','permission-location',{kind:'quiet'})+btn('完成','close-modal',{kind:'primary'}));
  const states = await perform('permission_status');
  if (states) updatePermission('microphone',states.microphone);
}

function updatePermission(id, state) {
  const badge = $('#permission-'+id);
  if (!badge) return;
  badge.textContent = {authorized:'已允许',denied:'尚未开启',not_determined:'尚未开启'}[state] || '尚未检测';
  badge.className = 'badge '+(state==='authorized'?'good':['denied','not_determined'].includes(state)?'warn':'');
}

async function chooseFolder(id) {
  const item = state.paths.find(path => path.id === id);
  if (!item) return;
  const result = await perform('choose_directory',{id});
  if (!result?.token) return;
  folderChoice = {item,...result};
  const title = item.kind==='models' ? '迁移模型与运行环境' : item.kind==='voices' ? '复制音色并更改位置' : `更改${item.title}位置`;
  const detail = item.kind==='models' ? '完成后会使用新位置。请先结束正在占用模型的任务。'
    : item.kind==='voices' ? '已有音色复制到新位置，原文件保留。之后的新音色使用新位置。'
    : '新的导出或下载从这里开始；已有文件留在原处。';
  openModal(title,detail,`<p class="inline-note">当前位置</p><div class="code-box">${esc(item.path)}</div>
    <p class="inline-note">新位置</p><div class="code-box">${esc(result.path)}</div>`,
    btn('取消','close-modal')+btn(item.kind==='models'?'迁移模型':item.kind==='voices'?'复制音色':'使用此位置','folder-confirm',{kind:'primary'}));
}

async function confirmFolder() {
  if (!folderChoice) return;
  const {token,item} = folderChoice;
  folderChoice.stopping = false;
  const relocating = item.kind==='models' || item.kind==='voices';
  const title = item.kind==='models' ? '正在迁移模型' : '正在复制音色';
  if (relocating) {
    openModal(title,'完成后切换到新位置。',`
      <div class="progress-track"><div class="progress-fill" id="transfer-fill"></div></div>
      <p class="inline-note" id="transfer-label" role="status">准备中…</p>`,
      btn('停止','folder-stop'));
  }
  folderChoice.busy = true;
  try {
    const result = await api.call('commit_directory',{token});
    item.path = result.path;
    folderChoice = null;
    closeModal();
    if (state.page === 'storage') renderCurrent();
    toast('保存位置已更新');
  } catch (error) {
    folderChoice.busy = false;
    openModal('操作未完成',esc(error.message),`<div class="code-box">${esc(item.path)}</div>`,
      btn('返回','close-modal')+btn('重试','folder-confirm',{kind:'primary'}));
  }
}

async function stopFolder() {
  if (!folderChoice?.busy || folderChoice.stopping) return;
  const button = $('[data-action="folder-stop"]');
  if (!button) return;
  folderChoice.stopping = true;
  button.disabled = true;
  $('#transfer-label').textContent = '正在停止，等待当前文件处理完毕…';
  await perform('stop_directory',{token:folderChoice.token});
}

async function loadSkills() {
  // Returning to this page while a read is pending uses that same result.
  if (skillsLoading) return;
  skillsLoading = true;
  const token = ++skillLoad;
  try {
    const {entries} = await api.call('skills');
    if (token !== skillLoad) return;
    state.skills = entries;
    state.scenes.skills = entries.length ? 'normal' : 'empty';
  } catch {
    if (token !== skillLoad) return;
    state.scenes.skills = 'failure';
  } finally {
    skillsLoading = false;
  }
  if (state.page === 'skills' && !$('#global-search').value) renderCurrent();
}

function showSkillTab(tab) {
  const skill = skillDraft;
  if (tab === 'description') return `<div class="skill-document"><h3>什么时候使用</h3>
    <p>${esc(skill.summary)}</p><h3>使用方式</h3><p>在文稿旁的 AI 对话中提出要求，或手动指定这项技能。</p></div>`;
  if (tab === 'files') return `<div class="file-list">${skill.files.map((file,index) =>
    btn(esc(file),'skill-file',{id:String(index),kind:'compact'})).join('')}</div><div class="code-box" id="skill-file-content">${esc(skill.text)}</div>`;
  return `<label class="field"><span>使用方式</span><select id="skill-mode" ${skill.allowAutomatic?'':'disabled'}>
    <option value="auto" data-description="与当前任务匹配时使用" ${skill.automatic?'selected':''}>按需自动使用</option>
    <option value="manual" data-description="仅在明确点名时使用" ${!skill.automatic?'selected':''}>仅手动指定</option>
    </select><small>停用后保留文件与偏好；卸载是单独的操作。</small></label>${skill.name==='lulu-translate'?
    `<label class="field"><span>默认目标语言</span><input id="skill-language" value="${esc(skill.config.language||'英语')}"></label>
      <label class="field"><span>术语参考</span><textarea id="skill-terms">${esc(skill.config.terms||'')}</textarea></label>`:
    skill.name==='lulu-proofread'?`<label class="check-label"><input id="skill-strict" type="checkbox" ${skill.config.strict!==false?'checked':''}>严格保留数字、链接和原文结构</label>`:''}`;
}

function readSkillDraft() {
  if ($('#skill-mode')) skillDraft.automatic = $('#skill-mode').value === 'auto';
  if ($('#skill-language')) skillDraft.config.language = $('#skill-language').value;
  if ($('#skill-terms')) skillDraft.config.terms = $('#skill-terms').value;
  if ($('#skill-strict')) skillDraft.config.strict = $('#skill-strict').checked;
}

async function showSkill(id) {
  const skill = state.skills.find(item => item.name === id);
  if (!skill) return;
  const details = await perform('skill_detail',{id,reference:'SKILL.md'});
  if (!details || state.page !== 'skills') return;
  skillDraft = {...skill,...details};
  openModal(esc(skill.title),skill.installed?'已安装，可调整使用方式':'未安装',
    `<div class="skill-detail-header"><div class="skill-symbol">${esc(skill.title[0])}</div><div class="row-title">${esc(skill.summary)}</div></div>
     ${segment([['description','说明'],['files','文件'],['preferences','偏好']],'description','skill-tab','data-modal-tabs="true"')}
     <div id="skill-tab-body">${showSkillTab('description')}</div>`,
    btn('卸载…','uninstall-skill',{kind:'danger push-left',id,disabled:!skill.installed})
      +btn('取消','close-modal')+btn('保存偏好','save-skill',{kind:'primary',id,disabled:!skill.installed}),true);
}

function showImport(entries) {
  if (!entries?.length) return;
  openModal('导入技能','检查技能包后，选择要安装的技能。',
    entries.map(entry => `<div class="soft-callout">${icon('skill')}<div><strong>${esc(entry.title)}</strong>
      <p>${esc(entry.description)}</p></div>${btn('安装','install-import',{id:entry.token,kind:'primary',disabled:entry.conflict||entry.reserved})}</div>`).join(''),
    btn('取消','close-modal'));
}

function persistFeishuDraft() {
  const appId = $('#feishu-app-id');
  const secret = $('#feishu-secret');
  if (appId) state.feishu.appId = appId.value.trim();
  if (secret?.value) state.feishu.secret = secret.value;
}

function feishuStep(step) {
  persistFeishuDraft();
  if (step > 0 && !state.feishu.appId && !state.feishu.hasSecret) {
    $('#feishu-app-id')?.focus();
    return toast('请先填写应用凭据');
  }
  state.feishu.step = Math.max(0, Math.min(3, step));
  state.feishu.editing = true;
  renderCurrent();
}

function showReference() {
  const cli = state.meta.cliCommand;
  openModal('正式调用入口',state.meta.experience?'使用当前开发体验版的完整入口，查询版本、能力和请求参数。':'先由 Skill 核验安装，再查询版本、能力和请求参数。',
    `<div class="code-box">${esc(state.meta.cliPath)}</div><div class="code-box">${esc(`${cli} version --output json\n\n${cli} capabilities --output json`)}</div>`,
    btn('复制入口','copy-cli-path',{icon:'copy'})+btn('完成','close-modal',{kind:'primary'}),true);
}

document.addEventListener('click',async event=>{
  const external = event.target.closest('a[href]');
  if (external) {
    event.preventDefault();
    return perform('external',{url:external.href});
  }
  const pref = event.target.closest('button[data-pref]');
  if(pref && !pref.disabled && api){
    const key = pref.dataset.pref;
    state.prefs[key] = !state.prefs[key];
    pref.setAttribute('aria-checked',String(state.prefs[key]));
    return savePref(key,state.prefs[key]);
  }
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const {action,id,value} = button.dataset;
  if (action==='navigate' || action==='search-result') return navigate(id,button.dataset.target);
  if (action==='close-modal') return closeModal();
  if (action==='font-reset') {
    setFontSize(100);
    $('#font-range').focus({preventScroll:true});
    return savePref('font',100);
  }
  if (action==='permissions') return showPermissions();
  if (action==='permission-location') return openModal('在 macOS 中管理权限','打开对应的系统设置，核对 Lulu 的权限。',
    [['通知','notifications'],['麦克风','microphone'],['系统声音录制','system']].map(([title,id]) =>
      `<div class="permission-item"><div>${title}</div>${btn('打开系统设置','open-permission',{id,kind:'quiet compact',icon:'external'})}</div>`).join(''),
    btn('返回','permissions',{kind:'quiet'})+btn('完成','close-modal',{kind:'primary'}));
  if (action==='open-permission') return perform('permission',{id});
  if (action==='notification-preview') return showNotification();
  if (action==='notification-rules') return openModal('少打扰，也不漏掉重要的事','提醒随工作状态出现。',
    '<p>单条处理在保存结果后提醒；批量处理在整批结束后汇总。直播正常分段保持安静。</p>',btn('完成','close-modal',{kind:'primary'}));
  if (action==='path-toggle') return togglePath(button,id);
  if (action==='copy-path') return copyText(state.paths.find(path=>path.id===id)?.path);
  if (action==='managed-path') return openModal(esc(button.dataset.title),'保存在本机的资料。',
    `<div class="code-box">${esc(button.dataset.path)}</div>`,
    btn('复制位置','copy-managed',{extra:`data-path="${esc(button.dataset.path)}"`,icon:'copy'})+btn('完成','close-modal',{kind:'primary'}));
  if (action==='live-location') return perform('live_location');
  if (action==='retry-inheritance') return perform('retry_inheritance');
  if (action==='copy-managed') return copyText(button.dataset.path);
  if (action==='folder-change') return chooseFolder(id);
  if (action==='folder-confirm') return confirmFolder();
  if (action==='folder-stop') return stopFolder();
  if (action==='skill-filter') {
    state.skillFilter=value;
    const group=button.parentElement;
    group.style.setProperty('--index',String(['all','system','user'].indexOf(value)));
    $$('button',group).forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    $('#skill-results').innerHTML=skillsList();
    return;
  }
  if (action==='retry-skills') return loadSkills();
  if (action==='skill-detail') return showSkill(id);
  if (action==='skill-tab') {
    readSkillDraft();
    const group=button.parentElement;
    group.style.setProperty('--index',String(['description','files','preferences'].indexOf(value)));
    $$('button',group).forEach(item=>item.setAttribute('aria-pressed',String(item===button)));
    $('#skill-tab-body').innerHTML=showSkillTab(value);
    LuluSelect.enhance();
    animateContent($('#skill-tab-body'),3);
    return;
  }
  if (action==='skill-file') {
    const draft = skillDraft;
    const data = await perform('skill_detail',{id:draft.name,reference:draft.files[Number(id)]});
    if (data && draft===skillDraft && $('#skill-file-content')) $('#skill-file-content').textContent=data.text;
    return;
  }
  if (action==='choose-skill') return showImport(await perform('choose_skill'));
  if (action==='skill-help') return openModal('导入你的技能','选择本机的 ZIP、SKILL.md，或拖入完整技能文件夹。',
    '<p>如果技能依赖参考资料，请保留 SKILL.md 与参考目录的原有结构。安装前会显示名称、用途和冲突检查结果。</p>',
    btn('完成','close-modal',{kind:'primary'}));
  if (action==='toggle-skill') {
    const enabled = button.getAttribute('aria-checked') !== 'true';
    button.disabled = true;
    if (await perform('skill_enable',{id,enabled})) button.setAttribute('aria-checked',String(enabled));
    return loadSkills();
  }
  if (action==='save-skill') {
    readSkillDraft();
    const {config,automatic} = skillDraft;
    if (await perform('skill_preferences',{id,config,automatic})) { closeModal(); loadSkills(); }
    return;
  }
  if (action==='uninstall-skill') return openModal('卸载技能？','停用会保留安装；卸载会移出正在使用的技能。',
    `<p>${esc(skillDraft.title)}</p>`,btn('保留技能','skill-detail',{id})+btn('卸载','confirm-uninstall',{kind:'danger',id}));
  if (['confirm-uninstall','reinstall-skill','install-import'].includes(action)) {
    const operations = {'confirm-uninstall':'skill_uninstall','reinstall-skill':'skill_install','install-import':'skill_import'};
    button.disabled = true;
    if (await perform(operations[action],{id})) { closeModal(); loadSkills(); }
    else button.disabled = false;
    return;
  }
  if (action==='copy-install') return copyText(state.meta.installPrompt);
  if (action==='copy-cli-path') return copyText(state.meta.cliCommand);
  if (action==='cli-reference') return showReference();
  if (action==='check-cli') {
    const result = await perform('check_cli');
    if (result) { state.scenes.cli=result.state; renderCurrent(); }
    return;
  }
  if (action==='cli-example') {
    const example=CLI_EXAMPLES[Number(id)];
    return openModal('可以这样告诉 Agent',esc(example.detail),
      `<div class="code-box">${esc(example.text)}</div>`,btn('复制说法','copy-example',{id,icon:'copy'})+btn('完成','close-modal',{kind:'primary'}));
  }
  if (action==='copy-example') return copyText(CLI_EXAMPLES[Number(id)]?.text);
  if (action==='show-secret') {
    const input=$('#feishu-secret');
    input.type=input.type==='password'?'text':'password';
    return;
  }
  if (action==='feishu-credentials-help') return openModal('凭据在哪里','在飞书开发者后台打开你的企业自建应用。',
    '<p>在「凭证与基础信息」中找到 App ID 和 App Secret，再分别填入对应的输入框。</p>',
    btn('完成','close-modal',{kind:'primary'}));
  if (action==='feishu-load') return perform('feishu_load');
  if (action==='feishu-step') return feishuStep(Number(value));
  if (action==='feishu-next') return feishuStep(state.feishu.step+1);
  if (action==='feishu-prev') return feishuStep(state.feishu.step-1);
  if (action==='feishu-config') return feishuStep(0);
  if (action==='feishu-reconnect') return feishuStep(3);
  if (action==='copy-scopes') return copyText(state.meta.scopes);
  if (action==='copy-callback') return copyText(state.meta.callbacks[Number(id)]);
  if (action==='feishu-return') return perform('feishu_return');
  if (action==='feishu-cancel') return perform('feishu_cancel');
  if (action==='feishu-connect') {
    persistFeishuDraft();
    const {appId,secret} = state.feishu;
    state.feishu.secret = '';
    return perform('feishu_connect',{appId:appId || '',secret:secret || ''});
  }
  if (action==='feishu-disconnect') return openModal('断开飞书连接？','飞书里的内容会保留。',
    '',btn('取消','close-modal')+btn('断开连接','feishu-disconnect-confirm',{kind:'danger'}));
  if (action==='feishu-disconnect-confirm') {
    if (await perform('feishu_disconnect')) closeModal();
    return;
  }
  if (action==='contact') return openModal('联系方式','作者 · 兆基','<img class="contact-image" src="assets/contact.jpg" alt="作者联系方式">',btn('完成','close-modal',{kind:'primary'}));
  if (action==='copy-version') return copyText(`Lulu ${state.meta.version} · ${state.meta.build}`);
  if (action==='install-update') return openModal('安装并重启？','Lulu 会在检查通过后重启并安装更新。',
    '',btn('稍后','close-modal')+btn('安装并重启','install-confirm',{kind:'primary'}));
  if (['check-update','download-update','pause-update','install-confirm','manual-update'].includes(action)) {
    if (action==='install-confirm') closeModal();
    return perform('update',{action});
  }
});

function copyText(text) {
  if (typeof text !== 'string' || !text) return;
  return perform('copy',{text}).then(result => { if (result) toast('已复制'); });
}

document.addEventListener('input',event=>{
  const input = event.target;
  if (input.id==='feishu-app-id') state.feishu.appIdDirty=true;
  if (input.id==='feishu-app-id' || input.id==='feishu-secret') persistFeishuDraft();
  if (input.id==='font-range') {
    setFontSize(input.value);
    perform('font_preview',{percent:state.prefs.font});
  }
  if (input.id==='global-search') searchSettings(input.value);
  if (input.id==='skill-search') {
    state.skillQuery = input.value;
    $('#skill-results').innerHTML=skillsList();
  }
});

document.addEventListener('change',event=>{
  const input = event.target;
  if (input.matches('input[data-pref],select[data-pref]')) {
    const key = input.dataset.pref;
    const value = input.type==='checkbox' ? input.checked : input.value;
    state.prefs[key] = value;
    syncNotifications();
    savePref(key,value);
  }
  if (input.id==='skill-status') {
    state.skillStatus = input.value;
    $('#skill-results').innerHTML=skillsList();
  }
});

renderNavigation();
navigate('general','',false,false);
connectWorkspace((event,value) => {
  if (event==='initial') {
    Object.assign(state,value);
    state.feishu.step=0;
    setFontSize(state.prefs.font);
    navigate(value.page,'',false,false);
  }
  if (event==='navigate') {
    if (value!==state.page) navigate(value,'',true,false);

  }
  if (event==='motion') document.body.classList.toggle('reduce-motion',value);
  if (event==='permission_status') updatePermission('notifications',value);
  if (event==='font_rollback') setFontSize(value);
  if (event==='inheritance') {
    state.inheritance=value;
    if (state.page==='storage' && !$('#global-search').value) renderCurrent();
  }
  if (event==='directory_progress' && folderChoice?.token===value.token && !folderChoice.stopping) {
    const fill = $('#transfer-fill');
    if (fill) {
      fill.style.width = Math.max(0,Math.round(value.fraction*100))+'%';
      $('#transfer-label').textContent=value.message;
    }
  }
  if (event==='update') {
    const previous = state.update;
    state.update=value;
    if (state.page==='about' && !$('#global-search').value) {
      if (previous.state===value.state && $('#update-fill')) {
        $('#update-fill').style.width=value.progress+'%';
        $('#update-progress-label').textContent=`已下载 ${value.progress}%`;
      } else renderCurrent();
    }
  }
  if (event==='feishu') {
    persistFeishuDraft();
    const wasReady=state.feishu.connected&&!state.feishu.expired;
    const changed=['expired','connected','loaded','busy','verified','hasSecret','canReturn','appId','notice'].some(key=>state.feishu[key]!==value[key]);
    const typedAppId=state.feishu.appIdDirty?state.feishu.appId:null;
    state.feishu={...state.feishu,...value,error:/未完成|失败/.test(value.notice||'')};
    if (typedAppId!==null) state.feishu.appId=typedAppId;
    if (changed && value.expired) { state.feishu.step=3; state.feishu.editing=true; }
    if (!wasReady && value.connected && !value.expired) { state.feishu.editing=false; state.feishu.secret=''; }
    if (state.page==='feishu' && !$('#global-search').value && (changed||!$('#feishu-app-id'))) renderCurrent();
    if (value.notice && state.page==='feishu') toast(value.notice);
  }
  if (event==='skill_import' && state.page==='skills') showImport(value);
  if (event==='notice') toast(value);
}).then(bridge => {
  api=bridge;
  bridge.ready();
  const sidebar=$('.sidebar');
  let width=-1;
  new ResizeObserver(() => {
    const next=sidebar.offsetWidth;
    if (next===width) return;
    width=next;
    bridge.call('sidebar_width',{width}).catch(()=>{});
  }).observe(sidebar);
});

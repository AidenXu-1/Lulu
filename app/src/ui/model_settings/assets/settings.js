'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  check:'m5 12 4 4L19 6',chevron:'m6 9 6 6 6-6',external:'M14 3h7v7M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5',
  headphones:'M3 14v-3a9 9 0 0 1 18 0v3M3 13h4v8H5a2 2 0 0 1-2-2zm18 0h-4v8h2a2 2 0 0 0 2-2z',
  grid:'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z',
  mic:'M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0zM5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8',
  wave:'M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4',file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h6',
  trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  cube:'m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5',
  folder:'M3 5h6l2 3h10v12H3z',shield:'m12 2 8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5zm-4 9 3 3 5-5',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',refresh:'M20 7a9 9 0 1 0 1 8M20 2v5h-5',
  eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  eyeoff:'M3 3l18 18M9 5c6-2 11 5 13 7l-3 4M6 6l-4 6s4 7 10 7l4-1',key:'M14 6a5 5 0 1 1-2 8l-7 7H2v-3l7-7a5 5 0 0 1 5-5',
  edit:'m16 3 5 5M4 16 3 21l5-1L21 7a2.12 2.12 0 0 0-3-3z',more:'M5 12h.01M12 12h.01M19 12h.01',close:'m6 6 12 12M6 18 18 6',plus:'M12 5v14M5 12h14',
  moon:'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11',spark:'m12 3 3 6 6 3-6 3-3 6-3-6-6-3 6-3z',
  info:'M12 11v6M12 7h.01M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
  lock:'M5 10h14v11H5zM8 10V6a4 4 0 0 1 8 0v4',cloud:'M7 18a5 5 0 0 1-1-10 7 7 0 0 1 13 1 4.5 4.5 0 0 1 0 9z'
};
function icon(name, extra='') { return `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.info}"></path></svg>`; }

// Public model metadata arrives from Python. Credentials are never in snapshots.
const state = {purpose:'extract',localMode:'local',choice:{extract:null,voice:null},voiceFeature:'clone',provider:null,asr:null};
let snapshot = null, bridge = null, requestSequence = 0, menuAnchor = null, toastTimer, modalReturn, snapshotDeferred = false;
let renderedSnapshotLayout = '', snapshotPointerActive = false, snapshotPointerTimer;
const pending = new Map(), drafts = new Map(), services = new Map(), actionPending = new Set();
const brand = (p, large=false) => `<span class="brand-mark ${large?'large':''} ${p.logo?'has-logo':''}" data-provider="${esc(p.id)}" aria-hidden="true">${p.logo && /^[a-zA-Z0-9_-]+\.(svg|png|ico|webp)$/.test(p.logo)?`<img src="logos/${esc(p.logo)}" alt="" width="46" height="46" draggable="false">`:esc(p.mark || '+')}</span>`;
function notify(message) { clearTimeout(toastTimer);$('toast').textContent=String(message || '');$('toast').hidden=false;toastTimer=setTimeout(()=>{$('toast').hidden=true;},3600); }
function api(operation,args={}) {
  if(!bridge) return Promise.reject(new Error('设置连接尚未就绪，请稍后重试。'));
  const id='models-'+(++requestSequence);
  return new Promise((resolve,reject)=>{pending.set(id,{resolve,reject});bridge.request(id,operation,JSON.stringify(args));});
}
function receiveReply(id,raw) {
  const request=pending.get(id);if(!request)return;pending.delete(id);
  try { const result=typeof raw==='string'?JSON.parse(raw):raw;result.ok?request.resolve(result.value):request.reject(new Error(result.message || '操作未完成，请重试。')); }
  catch(e) { request.reject(e); }
}
function publicRows(kind) { return snapshot?.localModels?.[kind] || []; }
function providerRows() { return (state.purpose==='assistant'?snapshot?.providers:snapshot?.asrProviders) || []; }
function currentProvider() { const rows=providerRows();return rows.find(p=>p.id===(state.purpose==='assistant'?state.provider:state.asr)) || rows[0]; }
function serviceFor(p) {
  const key=state.purpose+':'+p.id;
  if(!services.has(key)) services.set(key,{mode:p.modes?.[0]?.id || 'api',region:0,credentialId:null,connectionId:null});
  const s=services.get(key);if(p.modes?.length&&!p.modes.some(m=>m.id===s.mode))s.mode=p.modes[0].id;
  return s;
}
function modeFor(p=currentProvider()) {return p.modes?.find(m=>m.id===serviceFor(p).mode) || p;}
function scoped(value,p=currentProvider()) {return Array.isArray(value)?value[serviceFor(p).region] || value[0] || '':value || '';}
function officialBase(p=currentProvider()) {return scoped(modeFor(p).base,p);}
function scopeFor(p=currentProvider()) {const s=serviceFor(p);return JSON.stringify([state.purpose,p.id,s.mode,s.region,s.credentialId,s.connectionId]);}
function modelCatalogue(p=currentProvider()) {const mode=modeFor(p);return snapshot?.catalogue?.[mode.catalog] || snapshot?.catalogue?.[p.id+':'+serviceFor(p).mode] || snapshot?.catalogue?.[p.id] || [];}
function hydrate(p) {
  const scope=scopeFor(p),s=serviceFor(p),mode=modeFor(p),base=officialBase(p),asr=state.purpose==='extract';
  if(drafts.has(scope))return drafts.get(scope);
  const saved=(snapshot.storedConnections || snapshot.connections || []).filter(c=>c.provider===p.id&&c.mode===s.mode);
  const matching=saved.filter(c=>!base || String(c.base || '').replace(/\/$/,'')===base.replace(/\/$/,''));
  const latest=s.connectionId?saved.find(c=>c.id===s.connectionId):s.credentialId?matching.find(c=>c.credential_id===s.credentialId):matching.at(-1);
  const storedGroup=latest?saved.filter(c=>c.credential_id===latest.credential_id&&c.base===latest.base&&c.protocol===latest.protocol):[];
  const enabledGroup=storedGroup.filter(c=>(snapshot.connections || []).some(x=>x.id===c.id));
  const group=enabledGroup.length?enabledGroup:storedGroup;
  const config=asr?(snapshot.cloudConfigs?.[p.id] || snapshot.cloudConfigs?.[p.engineId] || {}):{};
  const d={scope,key:'',visible:false,revision:0,credentialId:latest?.credential_id || (config.configured===false?'':config.credentialId) || '',hasCredential:asr?Boolean(config.hasCredential || config.configured):Boolean(latest),
    base:config.base || latest?.base || base,protocol:latest?.protocol || mode.protocol || 'openai',name:latest?.label || '',modelId:config.modelId || config.model || p.modelId || '',
    selected:new Set(group.map(c=>c.model)),defaultModel:group.find(c=>c.id===snapshot.defaultConnectionId)?.model || group[0]?.model || '',
    manual:group.filter(c=>!modelCatalogue(p).some(m=>m.id===c.model)).map(c=>({id:c.model,label:c.label,window:c.window,max_output:c.max_output,saved:true})),
    fetched:null,modelScroll:0,status:'idle',feedback:'',saved:asr?Boolean(config.hasCredential || config.configured):Boolean(latest),busy:null};
  if(asr&&config.configured!==false || latest)d.removalArgs={purpose:state.purpose,provider:p.id,mode:s.mode,region:s.region,base:d.base,protocol:d.protocol,credentialId:d.credentialId};
  drafts.set(scope,d);return d;
}
function currentDraft(){return hydrate(currentProvider());}
function isCustom(p){return p.id.startsWith('custom');}
function baseFor(p,d){return isCustom(p)?d.base:(d.base || officialBase(p));}
function available(p){return modeFor(p).available!==false;}
function hasCredential(d){return Boolean(d.key.trim() || d.credentialId || d.hasCredential);}
function validEndpoint(p,d){if(!isCustom(p))return true;try{const u=new URL(d.base);return u.protocol==='https:'&&u.hostname&&!u.username&&!u.password&&!u.search&&!u.hash;}catch{return false;}}
function modelsFor(p,d){const rows=[...(d.fetched || modelCatalogue(p)),...d.manual];return [...new Map(rows.map(m=>[m.id,m])).values()];}
function cloudArgs(p,d){const s=serviceFor(p);return {purpose:state.purpose,provider:p.id,mode:s.mode,base:baseFor(p,d),protocol:d.protocol,region:s.region,credentialId:d.credentialId,key:d.key,modelIds:[...d.selected],defaultModel:d.defaultModel,label:d.name,modelId:d.modelId};}
function canTest(p,d){return !d.busy&&available(p)&&hasCredential(d)&&validEndpoint(p,d);}
function canSave(p,d){return canTest(p,d)&&(state.purpose==='assistant'?d.selected.size>0:!isCustom(p)||Boolean(d.modelId.trim()));}
function modelState(m){return m.ready?'已安装':m.repair?'需要修复':m.issue?'待处理':m.installed?'待准备':'未安装';}
function jobMessage(job){return job.message || (job.paused?'安装已暂停':'正在准备模型');}
function sectionHeading(cloud=false){
  const extract=state.purpose==='extract';
  return `<div class="section-heading"><div><h2>${extract?'转录模型':state.purpose==='voice'?'配音模型':'云端连接'}</h2><p>${extract?(cloud?'云端转录时，音频会上传到所选服务商。':'在本机转录，音频不上传。'):state.purpose==='voice'?'在本机配音，按需要准备声音能力。':'连接一次，在文稿中随时选用。'}</p></div>${extract?`<div class="segment" aria-label="转录方式"><button data-local-mode="local" aria-pressed="${!cloud}">${icon('lock')}本机离线</button><button data-local-mode="cloud" aria-pressed="${cloud}">${icon('cloud')}云端 API</button></div>`:`<span class="quiet-status">${icon(cloud?'lock':'shield')}${cloud?'密钥保存在本机':'本机离线'}</span>`}</div>`;
}
function progressMarkup(job,label){const value=Number(job.progress);return `<progress ${value>=0?`value="${Math.min(100,value)}"`:''} max="100" aria-label="${esc(label)}"></progress>`;}
function localActions(m){
  const job=snapshot.jobs?.[m.id],busy=actionPending.has('local:'+m.id);
  if(job&&!['complete','idle','ready'].includes(job.status))return `<div class="install-zone"><div class="install-row"><div class="install-progress" data-job="${esc(m.id)}"><div class="progress-heading"><h3>${esc(jobMessage(job))}</h3><span class="progress-value">${Number(job.progress)>=0?Math.round(job.progress)+'%':''}</span></div>${progressMarkup(job,'模型安装进度')}</div><div class="inline-actions"><button class="secondary" data-local-action="${job.paused?'resume':'pause'}" ${busy?'disabled':''}>${job.paused?'继续安装':'暂停'}</button><button class="icon-button" data-local-action="cancel" aria-label="取消模型安装" ${busy?'disabled':''}>${icon('close')}</button></div></div></div>`;
  const qwen=m.id==='qwen_tts',hint=m.issue || (qwen?'各项功能独立安装，共用文件只保存一份。':m.ready?`默认模型用于新的${state.purpose==='extract'?'转录':'配音'}任务。`:m.repair?'已有文件会保留，核验后只补齐缺失的部分。':'准备模型后，即可在本机使用。');
  return `<div class="install-zone"><div class="install-row"><p class="install-hint">${esc(hint)}</p><div class="inline-actions">${m.ready?(m.default?`<span class="installed-label">${icon('check')}当前默认</span>`:`<button class="primary" data-local-action="default" ${busy?'disabled':''}>设为默认</button>`):qwen?'':`<button class="primary" data-local-action="install" ${busy||m.issue?'disabled':''}>${icon('download')}${busy?'正在处理':m.issue?'当前不可用':m.repair?'修复并复用':m.importOnly?'导入安装包':m.resumable?'继续安装':'下载模型'}</button>`}<button id="local-more" class="icon-button" aria-label="模型更多操作" aria-haspopup="menu" ${busy?'disabled':''}>${icon('more')}</button></div></div></div>`;
}
function renderFeatures(){return `<div class="feature-list">${(snapshot.voiceFeatures || []).filter(f=>f.id===state.voiceFeature).map(f=>{const job=snapshot.jobs?.['feature:'+f.id],busy=actionPending.has('feature:'+f.id);return `<div class="feature-row"><span class="feature-icon">${icon(f.icon || 'mic')}</span><div class="feature-description"><h3>${esc(f.name)}</h3><p>${esc(f.description)}${!f.ready&&f.size?' · 约 '+esc(f.size):''}</p></div>${job&&!['complete','idle','ready'].includes(job.status)?`<div class="feature-progress" data-job="feature:${esc(f.id)}" title="${esc(job.message)}">${progressMarkup(job,f.name+'安装进度')}<div class="job-actions"><button data-feature="${esc(f.id)}" data-feature-action="${job.paused?'resume':'pause'}" ${busy?'disabled':''}>${job.paused?'继续安装':'暂停安装'}</button><button class="icon-button" data-feature="${esc(f.id)}" data-feature-action="cancel" aria-label="取消${esc(f.name)}安装" ${busy?'disabled':''}>${icon('close')}</button></div></div>`:f.ready?`<span class="status good">已安装</span><button class="icon-button" data-feature-more="${esc(f.id)}" aria-label="${esc(f.name)}更多操作" aria-haspopup="menu">${icon('more')}</button>`:`<button class="secondary" data-feature="${esc(f.id)}" data-feature-action="install" ${busy?'disabled':''}>${icon('download')}${busy?'处理中':f.installed?'修复环境':'安装'}</button>`}</div>`;}).join('')}</div>`;}
function metric(label,value,unit,note){return `<div class="metric"><span>${esc(label)}</span><strong class="${value===null?'no-data':''}">${value===null?'尚无同条件数据':esc(value)}${value!==null?`<small>${esc(unit)}</small>`:''}</strong><em>${esc(note)}</em></div>`;}
function capabilities(features){return `<div class="capabilities">${features.map(([name,value,enabled])=>`<div class="${enabled?'':'muted'}">${icon(enabled?'check':'info')}<div><strong>${esc(name)}</strong><p>${esc(value)}</p></div></div>`).join('')}</div>`;}
function modelInformation(m){
  const i=m.presentation;if(!i)return `<p class="local-description">${esc(m.description)}</p>${m.id==='qwen_tts'?renderFeatures():''}`;
  const asr=state.purpose==='extract',qwen=m.id==='qwen_tts',f=qwen?(snapshot.voiceFeatures || []).find(f=>f.id===state.voiceFeature):null,v=f?{...i,...f.presentation}:i,special=qwen&&state.voiceFeature!=='clone';
  const metrics=asr?metric('近 20 分钟音频耗时',v.time,'秒','不含首次加载与预热')+metric('实测内存峰值',v.memory,'GB','模型、音频及运行工具')+metric('模型文件下载',v.size,v.sizeUnit,'缺少时才下载'):
    metric(special?'首份样音耗时':'40 字文案首次等待',v.first,'秒',special?'不同文本 / 语种的批次记录':'含加载、参考准备和生成')+metric(special?'其余样音耗时':'紧接着再次生成',v.repeat,'秒','不含模型下载和环境安装')+metric('模型进程内存峰值',v.memory,'GiB','不含 Lulu 和系统');
  const features=special?(state.voiceFeature==='preset'?[['9 种官方音色','无需参考录音',true],['表达描述','先试听确认效果',true],['独立安装','按需要准备',true]]:[['文字设计声音','先生成候选，再试听',true],['保存并复用','继续克隆需声音克隆能力',true],['资源与速度','尚无同条件速度数据',false]]):i.features;
  return `${qwen?`<div class="feature-choices" role="tablist" aria-label="Qwen3-TTS 能力">${(snapshot.voiceFeatures || []).map(f=>`<button data-voice-feature="${esc(f.id)}" role="tab" aria-selected="${f.id===state.voiceFeature}">${esc(f.name)}</button>`).join('')}</div>`:''}<h3 class="model-position">${esc(v.position)}</h3><p class="model-reason">${esc(v.reason)}</p><div class="metric-row">${metrics}</div><div class="measure-note"><span>${asr?'2026-10-16 · M4 Pro / 48 GiB · 同条件独立实测；Lulu 完整等待时间会更长。':esc(i.date)+' · M4 Pro / 48 GiB · 历史实测参考，未在本轮安装版重测。'+(special?'本项与 40 字克隆测试不可直接比较。':'')}</span><button id="model-measure">查看测试与对比 ${icon('external')}</button></div>${capabilities(features)}<p class="boundary">${icon('info')}${esc(i.boundary)}</p>${qwen?renderFeatures():''}`;
}
function renderLocal(){
  const kind=state.purpose,rows=publicRows(kind),m=rows.find(x=>x.id===state.choice[kind]) || rows.find(x=>x.default) || rows[0];
  if(!m)return sectionHeading()+`<p class="loading-state">暂时没有可用的本地模型。</p>`;state.choice[kind]=m.id;
  const i=m.presentation||{},feature=m.id==='qwen_tts'?(snapshot.voiceFeatures || []).find(f=>f.id===state.voiceFeature):null,job=snapshot.jobs?.[feature?'feature:'+feature.id:m.id],label=job&&!['complete','idle','ready'].includes(job.status)?(job.paused?'已暂停':'正在安装'):feature?(feature.ready?'已安装':feature.installed?'待准备':'未安装'):modelState(m);
  return `${sectionHeading()}<div class="workspace local-workspace"><nav class="rail" aria-label="${kind==='extract'?'转录':'配音'}模型"><div class="rail-heading">选择模型</div>${rows.map(x=>`<button class="rail-model" data-local="${esc(x.id)}" aria-pressed="${x.id===m.id}">${brand(x)}<span><span class="rail-name">${esc(x.presentation?.name||x.name+' '+x.version)}</span><span class="rail-subtitle">${x.default?'当前默认':esc(x.presentation?.subtitle||x.subtitle)}</span></span></button>`).join('')}</nav><section class="detail local-detail" aria-label="模型详情"><div class="detail-top">${brand(m,true)}<div class="detail-title"><h2>${esc(i.name||(m.id==='whisper'?m.name:m.name+' '+m.version))}</h2><p>${esc(i.source||m.source)}</p></div><span class="status ${(feature?feature.ready:m.ready)?'good':''}">${esc(label)}</span></div>${modelInformation(m)}${localActions(m)}</section>${renderStorage(kind)}</div>`;
}
function renderStorage(kind){const path=snapshot.storage?.[kind] || '正在读取…';return `<section class="model-storage"><div class="storage-location">${icon('folder')}<span class="storage-path" title="${esc(path)}">保存位置　${esc(path)}</span></div><div class="storage-actions"><button class="link-button" id="reveal-storage">打开文件夹 ${icon('external')}</button><button class="link-button" id="change-storage">更改位置</button></div></section>`;}
function showMeasurements(){
 const asr=state.purpose==='extract',rows=publicRows(state.purpose).filter(m=>m.presentation),table=`<div class="table-wrap"><table class="compare-table"><thead><tr><th>模型</th><th>${asr?'近 20 分钟音频':'40 字首次等待'}</th><th>${asr?'内存峰值':'再次生成'}</th><th>${asr?'短音频字错率':'内存峰值'}</th><th>${asr?'会议字错率':'测试日期'}</th></tr></thead><tbody>${rows.map(m=>{const i=m.presentation;return `<tr><td>${esc(i.name)}${m.id==='qwen_tts'?' 声音克隆':''}</td><td>${esc(asr?i.time:i.first)} 秒</td><td>${esc(asr?i.memory:i.repeat)} ${asr?'GB':'秒'}</td><td>${esc(asr?i.cer[0]:i.memory)}${asr?'':' GiB'}</td><td>${esc(asr?i.cer[1]:i.date)}</td></tr>`;}).join('')}</tbody></table></div>`;
 const details=asr?'2026-10-16，M4 Pro 14 核 CPU / 48 GiB，接电、普通功耗。速度为同一段 19 分 56.98 秒朗读的两轮平均，不含首次加载与预热。峰值包含模型、音频和工具，不含 Lulu 和系统，也不代表电脑最低内存。字错率越低越好；30 段短音频共 4 分 15 秒，3 段会议共 9 分 32 秒。原版 Whisper 的准确率优势限本次短音频样本。测试未单独评估分人、时间戳或多语言精度。':'2026-09-16 / 17 历史参考；M4 Pro / 48 GiB，40 字中文文案、12.85 秒参考音频。Index 两版使用 CPU，其余使用 MPS。首次含加载、参考准备和生成；再次生成的输出时长随模型不同，不能当作绝对速度排名。内存为模型进程 physical footprint，单位 GiB。本轮安装版尚未重测。Qwen 克隆整轮最高 8.65 GiB；官方音色批次约 8.15 GiB，声音设计约 6.16 GiB，输入不同不参与此表速度排名。';
 modal(asr?'转录实测与对比':'配音历史实测',table+`<p class="measurement-conditions">${details}</p>`);$('modal').classList.add('measurement-modal');
}
function serviceControls(p){
  const modes=p.modes || [],m=modeFor(p),s=serviceFor(p),regions=m.regions || p.regions || [];
  const regionLabel=r=>typeof r==='string'?r:(r.name || r.label || '');
  return `<div class="service-line"><div class="compact-field"><span id="service-label">服务类型</span>${modes.length>1?`<button id="service" class="select-button" aria-labelledby="service-label service-text" aria-haspopup="menu"><span id="service-text">${esc(m.label || m.name)}${['个人版','团队版'].includes(m.detail)?' · '+esc(m.detail):''}</span>${icon('chevron')}</button>`:`<span class="static-value">${esc(m.label || (isCustom(p)?'自定义 API':'按量计费 API'))}</span>`}</div>${regions.length?`<div class="compact-field"><span id="region-label">账号地区</span><button id="region" class="select-button region-select" aria-labelledby="region-label region-text" aria-haspopup="menu"><span id="region-text">${esc(regionLabel(regions[s.region] || regions[0]))}</span>${icon('chevron')}</button></div>`:''}</div>`;
}
function savedGroups(p){const s=serviceFor(p);return [...new Map((snapshot.storedConnections || snapshot.connections || []).filter(c=>c.provider===p.id&&c.mode===s.mode).map(c=>[JSON.stringify([c.credential_id,c.base,c.protocol]),c])).values()];}
function savedKeyLabel(c,groups){return c?`密钥 ${groups.indexOf(c)+1} · ${c.label || c.model}`:'添加新密钥';}
function customFields(p,d){const needsImport=state.purpose==='extract'&&snapshot.cloudConfigs?.[p.id]?.requiresImport&&!d.imported;return isCustom(p)?`${needsImport?`<div class="saved-line"><button id="load-saved-configuration" class="link-button" ${d.busy?'disabled':''}>${d.busy==='load'?'正在读取…':'读取已有连接'}</button></div>`:''}<div class="custom-fields"><label>连接名称<input id="custom-name" maxlength="60" value="${esc(d.name)}" placeholder="为这个连接起个名字" autocomplete="off"></label><label>接口协议${state.purpose==='extract'?'<span class="static-value">OpenAI 音频兼容</span>':`<button id="protocol" class="select-button" aria-haspopup="menu">${d.protocol==='anthropic'?'Anthropic 兼容':'OpenAI 兼容'}${icon('chevron')}</button>`}</label><label class="full">服务地址<input id="custom-base" value="${esc(d.base)}" placeholder="https://api.example.com/v1" inputmode="url" autocomplete="off" spellcheck="false"></label></div>`:'';}
function testButton(d){return icon(d.busy==='test'?'refresh':d.status==='success'?'check':['partial','error'].includes(d.status)?'info':'refresh',d.busy==='test'?'spin':'')+`<span>${d.busy==='test'?'测试中':d.status==='success'?'连接正常':d.status==='partial'?'检查完成':d.status==='error'?'重新测试':'测试连接'}</span>`;}
function keyField(p,d){
  const hasLink=Boolean(scoped(modeFor(p).keys,p) || p.keys);
  return `<div class="key-field"><div class="label-row"><label for="api-key">API 密钥</label>${hasLink?`<button id="get-key" class="link-button">前往获取密钥 ${icon('external')}</button>`:''}</div><div class="key-row"><div class="input-shell">${icon('key')}<input id="api-key" type="${d.visible?'text':'password'}" autocomplete="off" spellcheck="false" maxlength="4096" aria-describedby="connection-feedback" placeholder="${d.hasCredential?'已保存密钥；输入可更换':'输入 API 密钥'}" value="${esc(d.key)}"><button id="key-visibility" class="icon-button" aria-label="${d.visible?'隐藏':'显示'}密钥" aria-pressed="${d.visible}">${icon(d.visible?'eyeoff':'eye')}</button></div><button id="test-connection" class="secondary test-button" ${canTest(p,d)?'':'disabled'}>${testButton(d)}</button></div>${d.removalArgs?.credentialId?`<div class="key-actions"><button id="remove-key" class="link-button" ${d.busy?'disabled':''}>${icon('trash')}删除本机密钥</button></div>`:''}</div>`;
}
function feedback(d,asr){return d.feedback || (d.saved?'连接已保存':asr?'保存后，用于下一次云端转录。':`已选 ${d.selected.size} 个模型`);}
function modelRow(m,d){return `<div class="model-row"><label class="model-toggle"><input type="checkbox" data-model="${esc(m.id)}" ${d.selected.has(m.id)?'checked':''}><span><span class="model-title" title="${esc(m.label || m.id)}">${esc(m.label || m.id)}</span><span class="model-id" title="${esc(m.id)}">${esc(m.id)}</span></span></label>${d.defaultModel===m.id?'<span class="model-badge">默认</span>':''}<button class="icon-button" data-model-more="${esc(m.id)}" aria-label="${esc(m.label || m.id)}更多操作" aria-haspopup="menu">${icon('more')}</button></div>`;}
function cloudInformation(p,asr){const m=modeFor(p),billing=Boolean(m.billing||p.billing),account=Boolean(scoped(m.keys,p)||p.keys);return `<div class="cloud-info"><div><span>处理位置</span><strong>服务商云端</strong><p>${asr?'上传待转录音频':'发送所选文本'}</p></div><div><span>使用费用</span><strong>按服务商规则计费</strong>${billing||account?`<button class="link-button" id="service-pricing" data-pricing="${billing?'billing':'account'}">${billing?'查看官方计费':'服务商后台'} ${icon('external')}</button>`:''}</div><div><span>本机资源</span><strong>无需加载本地模型</strong><p>耗时受网络和服务端影响</p></div></div>`;}
function renderCloud(){
  const p=currentProvider();if(!p)return sectionHeading(true)+'<p class="loading-state">正在读取服务商…</p>';
  const d=hydrate(p),asr=state.purpose==='extract',m=modeFor(p),models=modelsFor(p,d),groups=savedGroups(p);
  const blocked=!available(p),reason=m.reason || m.unavailableReason || m.unavailable_reason || '此服务暂未开放接入，请选择其他服务类型。';
  return `${sectionHeading(true)}<div class="workspace cloud-workspace"><nav class="rail" aria-label="服务商"><div class="rail-heading">选择服务商</div>${providerRows().map(x=>`<button data-provider="${esc(x.id)}" aria-pressed="${x.id===p.id}">${brand(x)}<span class="rail-name">${esc(x.name)}</span></button>`).join('')}</nav><section class="detail" aria-label="服务商详情"><div class="detail-top">${brand(p,true)}<div class="detail-title"><h2>${esc(p.name)}</h2><p>${asr?'用于内容提取':'用于文稿助手'}</p></div><span class="status ${d.saved?'good':''}">${d.saved?'已保存':'未保存'}</span></div>${cloudInformation(p,asr)}${!asr&&groups.length>0?`<div class="saved-line"><span>已保存的密钥</span><button id="saved-connection" class="select-button" aria-haspopup="menu">${esc(savedKeyLabel(groups.find(c=>c.credential_id===d.credentialId&&c.base===d.base&&c.protocol===d.protocol),groups))}${icon('chevron')}</button></div>`:''}${customFields(p,d)}${serviceControls(p)}${blocked?`<div class="service-unavailable"><h3>此服务暂未开放接入</h3><p>${esc(reason)}</p></div>`:`${keyField(p,d)}${asr?`<section class="models-block"><h3>转录模型</h3>${isCustom(p)?`<label class="custom-fields">模型 ID<input id="asr-model-id" value="${esc(d.modelId)}" placeholder="服务商提供的转录模型 ID" autocomplete="off" spellcheck="false"></label>`:`<div class="asr-model">${brand({id:p.modelId,logo:p.modelLogo || p.logo})}<div><h3>${esc(p.model)}</h3><p>${esc(p.modelId)}</p></div><span class="status">已自动匹配</span></div>`}</section>`:`<section class="models-block"><div class="block-heading"><h3>启用的模型<span class="model-source">${d.fetched?'已获取目录':''}</span></h3><div class="inline-actions"><button id="fetch-models" class="link-button" ${canTest(p,d)?'':'disabled'}>${icon('refresh',d.busy==='discover'?'spin':'')}${d.busy==='discover'?'获取中':'获取模型'}</button><button id="add-model" class="icon-button" aria-label="添加模型" aria-haspopup="menu">${icon('plus')}</button></div></div><div id="model-list" tabindex="0" role="region" aria-label="可用模型列表" data-scope="${esc(d.scope)}">${models.length?models.map(x=>modelRow(x,d)).join(''):'<div class="model-empty">获取模型列表，或添加服务商提供的模型 ID。</div>'}</div></section>`}<details class="auto-settings"><summary>连接详情</summary><dl><dt>服务地址</dt><dd id="resolved-base">${esc(baseFor(p,d) || '按所选服务自动匹配')}</dd>${!asr?`<dt>模型设置</dt><dd>上下文容量与输出限制随模型规格匹配</dd>`:''}<dt>测试范围</dt><dd>仅检查服务连接，不发送文稿${asr?'或音频':''}。模型实际调用权限需单独验证。</dd></dl></details><div class="detail-footer"><p id="connection-feedback" class="feedback ${d.status==='error'?'error':d.status==='success'?'good':''}" role="status">${esc(feedback(d,asr))}</p><button id="save-connection" class="primary" ${canSave(p,d)?'':'disabled'}>${d.busy==='save'?'正在保存':asr?'保存并选用':'保存连接'}</button></div>`}</section></div>`;
}
function updateView(focusId){
  if(!snapshot)return;snapshotDeferred=false;
  const previousList=$('model-list');if(previousList&&drafts.has(previousList.dataset.scope))drafts.get(previousList.dataset.scope).modelScroll=previousList.scrollTop;
  const active=document.activeElement,focus=focusId || (active?.id&&$('purpose-content').contains(active)?active.id:''),selection=active?.tagName==='INPUT'&&['text','password','search','url'].includes(active.type)?[active.selectionStart,active.selectionEnd]:null;
  closeMenu(false);
  document.querySelectorAll('[data-purpose]').forEach(b=>{const selected=b.dataset.purpose===state.purpose;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});
  $('purpose-content').setAttribute('aria-labelledby','tab-'+state.purpose);
  $('purpose-content').innerHTML=state.purpose==='assistant'||(state.purpose==='extract'&&state.localMode==='cloud')?renderCloud():renderLocal();
  bindPage();const list=$('model-list');if(list)list.scrollTop=drafts.get(list.dataset.scope)?.modelScroll || 0;
  if(focus&&$(focus)&&!$(focus).disabled){$(focus).focus({preventScroll:true});if(selection&&$(focus).setSelectionRange)try{$(focus).setSelectionRange(...selection);}catch{}}
  renderedSnapshotLayout=snapshotLayout(snapshot);
}
function snapshotLayout(value){
  // Byte counts and progress messages do not change the controls on the page.
  return JSON.stringify({...value,
    localModels:Object.fromEntries(Object.entries(value.localModels || {}).map(([kind,rows])=>
      [kind,rows.map(({resumable,...model})=>({...model,resumable:Boolean(resumable)}))])),
    jobs:Object.fromEntries(Object.entries(value.jobs || {}).map(([id,{progress,message,...job}])=>[id,job]))});
}
function syncJobProgress(){
  document.querySelectorAll('[data-job]').forEach(container=>{
    const job=snapshot.jobs?.[container.dataset.job];if(!job)return;
    const progress=container.querySelector('progress'),value=Number(job.progress);
    if(progress){if(value>=0)progress.value=Math.min(100,value);else progress.removeAttribute('value');}
    const heading=container.querySelector('.progress-heading h3'),percent=container.querySelector('.progress-value');
    if(heading)heading.textContent=jobMessage(job);
    if(percent)percent.textContent=value>=0?Math.round(value)+'%':'';
    if(container.classList.contains('feature-progress'))container.title=job.message || '';
  });
}
function refreshSnapshot(){
  if(renderedSnapshotLayout===snapshotLayout(snapshot)){
    snapshotDeferred=false;syncJobProgress();return;
  }
  // Structural transitions must also preserve the target until click is sent.
  if(snapshotPointerActive){snapshotDeferred=true;return;}
  updateView();
}
function receiveSnapshot(raw){
  try {snapshot=typeof raw==='string'?JSON.parse(raw):raw;
    // A state/progress refresh must never dismiss an open choice or edit dialog.
    if(!$('menu').hidden||$('modal').open){snapshotDeferred=true;return;}refreshSnapshot();
  }catch {notify('模型状态读取失败，请重新打开此页。');}
}
function dirty(d,{invalidate=false,credential=false}={}){d.saved=false;d.feedback='';d.revision++;if(invalidate){d.status='idle';d.busy=null;}if(credential){d.credentialId='';d.hasCredential=false;d.fetched=null;}}
function syncCloudButtons(p,d){
  if($('test-connection')){$('test-connection').disabled=!canTest(p,d);$('test-connection').innerHTML=testButton(d);}
  if($('fetch-models')){$('fetch-models').disabled=!canTest(p,d);$('fetch-models').innerHTML=icon('refresh',d.busy==='discover'?'spin':'')+(d.busy==='discover'?'获取中':'获取模型');}
  if($('save-connection')){$('save-connection').disabled=!canSave(p,d);$('save-connection').textContent=d.busy==='save'?'正在保存':state.purpose==='extract'?'保存并选用':'保存连接';}
  if($('remove-key'))$('remove-key').disabled=Boolean(d.busy);
  if($('connection-feedback')){$('connection-feedback').textContent=feedback(d,state.purpose==='extract');$('connection-feedback').className='feedback '+(d.status==='error'?'error':d.status==='success'?'good':'');}
  const badge=document.querySelector('.detail-top>.status');if(badge){badge.className='status '+(d.saved?'good':'');badge.textContent=d.saved?'已保存':'未保存';}
}
async function cloudOperation(kind){
  const p=currentProvider(),d=hydrate(p);if(!(kind==='save'?canSave(p,d):canTest(p,d)))return;
  const args=cloudArgs(p,d),scope=d.scope,revision=d.revision,token=Symbol(kind);d.operationToken=token;d.busy=kind;d.feedback=kind==='test'?'正在测试连接…':kind==='save'?'正在保存连接…':'正在获取模型目录…';syncCloudButtons(p,d);
  const active=()=>currentProvider()&&scopeFor(currentProvider())===scope;
  try {
    const value=await api({test:'test_connection',discover:'discover_models',save:'save_connection'}[kind],args);
    if(d.revision!==revision||d.operationToken!==token)return;
    if(value?.credentialId){d.credentialId=value.credentialId;d.hasCredential=true;d.key='';d.removalArgs={...args,credentialId:value.credentialId};for(const k of ['key','modelIds','defaultModel','label','modelId'])delete d.removalArgs[k];}
    if(kind==='test'){d.status=['success','partial'].includes(value?.status)?value.status:'error';d.feedback=value?.message || '未取得可验证的连接结果。';}
    if(kind==='discover'){const rows=Array.isArray(value)?value:value?.models;if(!Array.isArray(rows))throw new Error('模型目录格式无法识别，原选择已保留。');d.fetched=rows;d.feedback=rows.length?`已获取 ${rows.length} 个模型。`:'目录为空，仍可使用已有型号。';}
    if(kind==='save'){d.saved=true;d.feedback='连接已保存';if(Array.isArray(value?.connections))snapshot.connections=value.connections;if(Array.isArray(value?.storedConnections))snapshot.storedConnections=value.storedConnections;if(value?.cloudConfigs)snapshot.cloudConfigs=value.cloudConfigs;if(value?.defaultConnectionId!==undefined)snapshot.defaultConnectionId=value.defaultConnectionId;}
  }catch(e){if(d.revision===revision&&d.operationToken===token){d.status='error';d.feedback=e.message;}}
  finally{if(d.operationToken===token)d.busy=null;if(active())updateView();}
}
function removeCredential(){
  const p=currentProvider(),d=hydrate(p),s=serviceFor(p),args=d.removalArgs;
  if(d.busy||!args?.credentialId)return;
  modal('删除本机密钥',`<p>从本机移除${esc(p.name)}当前选择的密钥。关联模型将无法继续调用，已有文稿和会话记录保留。</p>${state.purpose==='extract'?'<p>若正在选用此云端转录服务，将切回本地转录。</p>':''}<p class="muted">厂商账户中的密钥仍由你管理。当前未保存的输入会清空。</p><p id="remove-key-error" class="danger" role="alert"></p><div class="modal-actions"><button id="cancel-remove-key" class="secondary">取消</button><button id="confirm-remove-key" class="primary">删除密钥</button></div>`,()=>{
    const confirm=$('confirm-remove-key'),cancel=$('cancel-remove-key'),error=$('remove-key-error');
    cancel.onclick=closeModal;
    confirm.onclick=async()=>{
      if(d.busy)return;
      d.busy='remove';d.revision++;d.operationToken=Symbol('remove');
      confirm.disabled=true;confirm.textContent='正在删除…';cancel.disabled=true;syncCloudButtons(p,d);
      try{
        const value=await api('remove_credential',args);
        for(const field of ['connections','storedConnections','cloudConfigs','defaultConnectionId'])if(value?.[field]!==undefined)snapshot[field]=value[field];
        for(const [scope,draft] of drafts)if(draft.credentialId===args.credentialId||draft.removalArgs?.credentialId===args.credentialId){draft.key='';draft.revision++;draft.operationToken=null;drafts.delete(scope);}
        s.credentialId='new';s.connectionId=null;
        if(confirm.isConnected)closeModal();updateView();notify('本机密钥已删除，文稿与会话记录保留。');
      }catch(e){d.status='error';d.feedback=e.message;if(error.isConnected)error.textContent=e.message;}
      finally{d.busy=null;if(currentProvider()===p&&scopeFor(p)===d.scope)syncCloudButtons(p,d);if(confirm.isConnected){confirm.disabled=false;confirm.textContent='重试删除';cancel.disabled=false;}}
    };
  });
}
async function loadSavedConfiguration(){
  const p=currentProvider(),d=hydrate(p);if(d.busy)return;
  const revision=d.revision,scope=d.scope,token=Symbol('load');d.operationToken=token;d.busy='load';d.feedback='正在读取已有连接…';updateView();
  try{
    const value=await api('load_saved_configuration',{purpose:'extract',provider:p.id});
    if(d.revision!==revision||d.operationToken!==token)return;
    const config=value?.cloudConfigs?.[p.id];if(!config)throw new Error('未取得已有连接信息，请重新输入。');
    snapshot.cloudConfigs={...snapshot.cloudConfigs,...value.cloudConfigs};
    d.base=config.base || '';d.modelId=config.modelId || config.model || '';d.credentialId=config.credentialId || value.credentialId || '';d.hasCredential=Boolean(config.configured || config.hasCredential);
    d.key='';d.imported=true;d.saved=d.hasCredential;d.status='idle';d.feedback=value.message || '已读取已有连接。';
    d.removalArgs={purpose:'extract',provider:p.id,mode:'api',region:0,base:d.base,protocol:d.protocol,credentialId:d.credentialId};
  }catch(e){if(d.revision===revision&&d.operationToken===token){d.status='error';d.feedback=e.message;}}
  finally{if(d.operationToken===token)d.busy=null;if(currentProvider()&&scopeFor(currentProvider())===scope)updateView();}
}
async function runLocal(id,action,feature=false){
  const key=(feature?'feature:':'local:')+id;if(actionPending.has(key))return;
  actionPending.add(key);updateView();
  try{const result=await api(feature?'feature_action':'local_action',{id,action});if(result?.message)notify(result.message);}
  catch(e){notify(e.message);}finally{actionPending.delete(key);updateView();}
}
async function simpleOperation(operation,args={}){try{const value=await api(operation,args);if(value?.message)notify(value.message);}catch(e){notify(e.message);}}
function closeMenu(restore=true){
  const el=$('menu');el.hidden=true;el.innerHTML='';
  if(menuAnchor){menuAnchor.setAttribute('aria-expanded','false');if(restore&&menuAnchor.isConnected)menuAnchor.focus({preventScroll:true});}menuAnchor=null;
  if(snapshotDeferred&&!$('modal').open)queueMicrotask(()=>{if(snapshotDeferred&&$('menu').hidden&&!$('modal').open)refreshSnapshot();});
}
function menu(anchor,items,{search=false}={}){
  closeMenu(false);menuAnchor=anchor;anchor.setAttribute('aria-expanded','true');
  const el=$('menu');el.hidden=false;el.setAttribute('role','menu');el.setAttribute('aria-label',anchor.getAttribute('aria-label') || anchor.textContent.trim());
  el.innerHTML=`${search?'<div class="menu-search"><input id="menu-search" placeholder="搜索模型" aria-label="搜索模型"></div>':''}<div id="menu-items"></div>`;
  const position=()=>{const r=anchor.getBoundingClientRect(),width=Math.min(Math.max(r.width,search?275:190),innerWidth-24);el.style.width=width+'px';el.style.left=Math.max(12,Math.min(r.left,innerWidth-width-12))+'px';el.style.maxHeight=Math.max(100,innerHeight-24)+'px';const h=el.offsetHeight;el.style.top=Math.max(12,r.bottom+h+8<innerHeight?r.bottom+6:r.top-h-6)+'px';};
  function draw(q=''){
    const shown=items.filter(x=>!q||(x.label+' '+(x.description || '')).toLocaleLowerCase().includes(q.toLocaleLowerCase()));
    $('menu-items').innerHTML=shown.length?shown.map((x,i)=>`<button role="${x.checked!==undefined?'menuitemradio':'menuitem'}" ${x.checked!==undefined?`aria-checked="${x.checked}"`:''} data-index="${i}" ${x.disabled?'disabled':''} class="${x.danger?'danger':''}"><span>${esc(x.label)}${x.description?`<small>${esc(x.description)}</small>`:''}</span>${x.checked?icon('check'):''}</button>`).join(''):'<div class="menu-empty">没有匹配的模型</div>';
    $('menu-items').querySelectorAll('button').forEach(b=>b.onclick=()=>{const item=shown[Number(b.dataset.index)];closeMenu();item.action();});position();
  }
  draw();if(search){$('menu-search').oninput=e=>draw(e.target.value);$('menu-search').focus();}else el.querySelector('[aria-checked="true"]:not(:disabled),button:not(:disabled)')?.focus();
}
function modal(title,body,bind){$('modal').classList.remove('measurement-modal');closeMenu(false);modalReturn=document.activeElement;$('modal-title').textContent=title;$('modal-body').innerHTML=body;$('modal').showModal();if(bind)bind();}
function closeModal(){$('modal').close();}
function selectModel(id,checked){const d=currentDraft();if(checked){d.selected.add(id);if(!d.defaultModel)d.defaultModel=id;}else{d.selected.delete(id);if(d.defaultModel===id)d.defaultModel=d.selected.values().next().value || '';}dirty(d);updateView();document.querySelector(`[data-model="${CSS.escape(id)}"]`)?.focus({preventScroll:true});}
function modelMenu(anchor,id){
  const p=currentProvider(),d=hydrate(p),m=modelsFor(p,d).find(x=>x.id===id);if(!m)return;
  menu(anchor,[{label:'模型信息',action:()=>modal(m.label || m.id,`<dl><dt>模型 ID</dt><dd>${esc(m.id)}</dd><dt>上下文容量</dt><dd>${Number.isFinite(m.window)?Number(m.window).toLocaleString('zh-CN'):'待核实'}</dd><dt>单次输出上限</dt><dd>${Number.isFinite(m.max_output)?Number(m.max_output).toLocaleString('zh-CN'):'以服务商为准'}</dd>${m.source?`<dt>规格来源</dt><dd>${esc(m.source)}</dd>`:''}</dl>`)},
    {label:'设为新会话默认',checked:d.defaultModel===id,disabled:!d.selected.has(id),action:()=>{d.defaultModel=id;dirty(d);updateView();}},
    ...(d.manual.some(x=>x.id===id&&!x.saved)?[{label:'移除自定义模型',danger:true,action:()=>{d.manual=d.manual.filter(x=>x.id!==id);d.selected.delete(id);if(d.defaultModel===id)d.defaultModel=d.selected.values().next().value || '';dirty(d);updateView();}}]:[])]);
}
function addModel(anchor){
  const p=currentProvider(),d=hydrate(p),rows=modelsFor(p,d);
  menu(anchor,[{label:'手动添加模型',action:()=>modal('添加模型','<form id="model-form"><label>模型 ID<input id="new-model-id" maxlength="199" placeholder="粘贴服务商提供的模型 ID" autocomplete="off" spellcheck="false" required></label><label>显示名称（可选）<input id="new-model-name" maxlength="80" placeholder="例如：我的写作模型" autocomplete="off"></label><p id="model-form-error" class="danger" role="alert"></p><div class="modal-actions"><button type="button" id="cancel-model" class="secondary">取消</button><button type="submit" class="primary">添加模型</button></div></form>',()=>{
    $('cancel-model').onclick=closeModal;$('model-form').onsubmit=e=>{e.preventDefault();const id=$('new-model-id').value.trim(),label=$('new-model-name').value.trim();if(!id||/\s|[<>"']/.test(id)){$('model-form-error').textContent='请填写准确的模型 ID，不要包含空格。';return;}if(rows.some(m=>m.id===id)){$('model-form-error').textContent='这个模型已在列表中。';return;}d.manual.push({id,label:label || id});d.selected.add(id);if(!d.defaultModel)d.defaultModel=id;dirty(d);closeModal();updateView();};$('new-model-id').focus();
  })},...rows.filter(m=>!d.selected.has(m.id)).map(m=>({label:m.label || m.id,description:m.id,action:()=>selectModel(m.id,true)}))],{search:true});
}
function bindPage(){
  document.querySelectorAll('[data-voice-feature]').forEach(b=>b.onclick=()=>{state.voiceFeature=b.dataset.voiceFeature;updateView();});
  if($('model-measure'))$('model-measure').onclick=showMeasurements;
  document.querySelectorAll('[data-local-mode]').forEach(b=>b.onclick=()=>{state.localMode=b.dataset.localMode;updateView();});
  document.querySelectorAll('[data-local]').forEach(b=>b.onclick=()=>{state.choice[state.purpose]=b.dataset.local;updateView();});
  document.querySelectorAll('button[data-provider]').forEach(b=>b.onclick=()=>{if(state.purpose==='assistant')state.provider=b.dataset.provider;else state.asr=b.dataset.provider;updateView();});
  document.querySelectorAll('[data-local-action]').forEach(b=>b.onclick=()=>runLocal(state.choice[state.purpose],b.dataset.localAction));
  document.querySelectorAll('[data-feature-action]').forEach(b=>b.onclick=()=>runLocal(b.dataset.feature,b.dataset.featureAction,true));
  document.querySelectorAll('[data-feature-more]').forEach(b=>b.onclick=()=>menu(b,[{label:'移除这项功能',danger:true,action:()=>runLocal(b.dataset.featureMore,'remove',true)}]));
  if($('local-more'))$('local-more').onclick=e=>{const m=publicRows(state.purpose).find(x=>x.id===state.choice[state.purpose]);menu(e.currentTarget,[{label:'移除本地模型',danger:true,action:()=>runLocal(m.id,'remove')}]);};
  if($('reveal-storage'))$('reveal-storage').onclick=()=>simpleOperation('storage_open',{purpose:state.purpose});
  if($('change-storage'))$('change-storage').onclick=()=>simpleOperation('storage_change',{purpose:state.purpose});
  if(state.purpose!=='assistant'&&!(state.purpose==='extract'&&state.localMode==='cloud'))return;
  const p=currentProvider();if(!p)return;const d=hydrate(p),s=serviceFor(p),m=modeFor(p);
  if($('service'))$('service').onclick=e=>menu(e.currentTarget,p.modes.map(x=>({label:x.label || x.name,description:x.detail,checked:x.id===s.mode,action:()=>{s.mode=x.id;s.region=0;s.credentialId=null;s.connectionId=null;updateView('service');}})));
  if($('region'))$('region').onclick=e=>menu(e.currentTarget,(m.regions || p.regions || []).map((r,i)=>({label:typeof r==='string'?r:r.name || r.label,checked:i===s.region,action:()=>{s.region=i;s.credentialId=null;s.connectionId=null;updateView('region');}})));
  if($('saved-connection'))$('saved-connection').onclick=e=>{const groups=savedGroups(p);menu(e.currentTarget,[...groups.map(c=>({label:savedKeyLabel(c,groups),description:c.base,checked:c.credential_id===d.credentialId&&c.base===d.base&&c.protocol===d.protocol,action:()=>{s.credentialId=c.credential_id;s.connectionId=c.id;updateView();}})),{label:'添加新密钥',action:()=>{s.credentialId='new';s.connectionId=null;updateView();}}]);};
  if($('load-saved-configuration'))$('load-saved-configuration').onclick=loadSavedConfiguration;
  if($('remove-key'))$('remove-key').onclick=removeCredential;
  if(!available(p))return;
  $('key-visibility').onclick=()=>{d.visible=!d.visible;const input=$('api-key');input.type=d.visible?'text':'password';$('key-visibility').innerHTML=icon(d.visible?'eyeoff':'eye');$('key-visibility').setAttribute('aria-label',d.visible?'隐藏密钥':'显示密钥');$('key-visibility').setAttribute('aria-pressed',String(d.visible));};
  if($('service-pricing'))$('service-pricing').onclick=()=>simpleOperation($('service-pricing').dataset.pricing==='billing'?'open_pricing_page':'open_key_page',{purpose:state.purpose,provider:p.id,mode:s.mode,region:s.region});
  if($('get-key'))$('get-key').onclick=()=>simpleOperation('open_key_page',{purpose:state.purpose,provider:p.id,mode:s.mode,region:s.region});
  $('test-connection').onclick=()=>cloudOperation('test');$('save-connection').onclick=()=>cloudOperation('save');
  if($('fetch-models'))$('fetch-models').onclick=()=>cloudOperation('discover');
  if($('add-model'))$('add-model').onclick=e=>addModel(e.currentTarget);
  document.querySelectorAll('[data-model]').forEach(b=>b.onchange=()=>selectModel(b.dataset.model,b.checked));
  document.querySelectorAll('[data-model-more]').forEach(b=>b.onclick=()=>modelMenu(b,b.dataset.modelMore));
  if($('protocol'))$('protocol').onclick=e=>menu(e.currentTarget,[['openai','OpenAI 兼容'],['anthropic','Anthropic 兼容']].map(([value,label])=>({label,checked:value===d.protocol,action:()=>{d.protocol=value;dirty(d,{invalidate:true,credential:true});updateView('protocol');}})));
  for(const [id,field] of [['api-key','key'],['custom-name','name'],['custom-base','base'],['asr-model-id','modelId']])if($(id))$(id).oninput=e=>{
    d[field]=e.target.value;dirty(d,{invalidate:['base','key','modelId'].includes(field),credential:['base','key'].includes(field)});
    if(field==='base'&&$('resolved-base'))$('resolved-base').textContent=d.base || '待填写';syncCloudButtons(p,d);
  };
}
function receiveCommand(action,raw){
  let value={};try{value=typeof raw==='string'?JSON.parse(raw):raw || {};}catch{return;}
  if(action==='purpose'&&['extract','voice','assistant'].includes(value.purpose)){state.purpose=value.purpose;if(['local','cloud'].includes(value.localMode))state.localMode=value.localMode;if(value.choice)state.choice[value.purpose]=value.choice;if(['clone','preset','design'].includes(value.feature))state.voiceFeature=value.feature;updateView();}
  if(action==='notice')notify(value.message);
}
$('modal-close').innerHTML=icon('close');$('modal-close').onclick=closeModal;
$('modal').addEventListener('close',()=>{if(modalReturn?.isConnected)modalReturn.focus({preventScroll:true});$('modal-body').innerHTML='';if(snapshotDeferred)refreshSnapshot();});
let backdropDown=false;
$('modal').addEventListener('pointerdown',e=>{const r=$('modal').getBoundingClientRect();backdropDown=e.target===$('modal')&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom);});
$('modal').addEventListener('click',e=>{const r=$('modal').getBoundingClientRect();if(backdropDown&&e.target===$('modal')&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom))closeModal();backdropDown=false;});
document.querySelectorAll('[data-purpose]').forEach(b=>b.onclick=()=>{state.purpose=b.dataset.purpose;updateView();});
$('purpose-tabs').addEventListener('keydown',e=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=[...document.querySelectorAll('[data-purpose]')],i=tabs.indexOf(document.activeElement),target=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[target].click();tabs[target].focus();
});
document.addEventListener('keydown',e=>{const feature=e.target.closest?.('[data-voice-feature]');if(!feature||!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const tabs=[...document.querySelectorAll('[data-voice-feature]')],i=tabs.indexOf(feature),n=e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length,id=tabs[n].dataset.voiceFeature;state.voiceFeature=id;updateView();document.querySelector('[data-voice-feature="'+id+'"]').focus();});
document.addEventListener('pointerdown',e=>{if(!$('menu').hidden&&!$('menu').contains(e.target)&&!menuAnchor?.contains(e.target))closeMenu(false);});
document.addEventListener('pointerdown',()=>{clearTimeout(snapshotPointerTimer);snapshotPointerActive=true;},true);
function finishSnapshotPointer(){
  clearTimeout(snapshotPointerTimer);
  snapshotPointerTimer=setTimeout(()=>{
    snapshotPointerActive=false;
    if(snapshotDeferred&&$('menu').hidden&&!$('modal').open)refreshSnapshot();
  },0);
}
document.addEventListener('pointerup',finishSnapshotPointer,true);
document.addEventListener('pointercancel',finishSnapshotPointer,true);
window.addEventListener('blur',finishSnapshotPointer);
document.addEventListener('keydown',e=>{
  if($('menu').hidden)return;if(e.key==='Escape'){e.preventDefault();closeMenu();return;}if(e.key==='Tab'){closeMenu(false);return;}
  const buttons=[...$('menu').querySelectorAll('button:not(:disabled)')],i=buttons.indexOf(document.activeElement);
  if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)&&!(document.activeElement?.tagName==='INPUT'&&['Home','End'].includes(e.key))){e.preventDefault();const target=e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;buttons[target]?.focus();}
});
window.addEventListener('resize',()=>closeMenu(false));window.addEventListener('scroll',e=>{if(!$('menu').contains(e.target))closeMenu(false);},true);
window.luluModels={snapshot:receiveSnapshot,command:receiveCommand,ready:false};
function connectNative(){
  if(typeof QWebChannel!=='function'||typeof qt==='undefined'){notify('模型设置尚未连接到 Lulu。');return;}
  new QWebChannel(qt.webChannelTransport,channel=>{
    bridge=channel.objects.models;if(!bridge){notify('模型设置连接不可用。');return;}
    bridge.reply.connect(receiveReply);bridge.snapshot.connect(receiveSnapshot);bridge.command.connect(receiveCommand);window.luluModels.ready=true;bridge.ready();
  });
}
connectNative();

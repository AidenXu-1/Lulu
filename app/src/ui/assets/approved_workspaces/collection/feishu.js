'use strict';

let feishuState, feishuClosing = false;

function openFeishu(state) {
  feishuState = state;
  const modal = $('#modal');
  if (!state) {
    if (modal.open && modal.dataset.surface==='feishu') {
      feishuClosing=true;
      modal.close();
    }
    return;
  }
  if (!modal.open || modal.dataset.surface!=='feishu') {
    closePopovers();
    modal.dataset.surface='feishu';
    $('#modalTitle').textContent='存入飞书多维表格';
    $('#modalBody').innerHTML=`
      <div class="feishu-account"><span id="feishuAccount"></span><button class="btn quiet compact" id="feishuConnect">连接设置</button></div>
      <p class="dialog-copy">来源：<span id="feishuSource"></span><br>已选 <span id="feishuCount"></span> 条作品</p>
      <div id="feishuForm">
        <div class="feishu-section"><h3>保存为</h3><div class="format-options"><label><input type="radio" name="feishuFormat" checked>多维表格</label><label><input type="radio" name="feishuFormat" disabled>飞书文档</label></div></div>
        <div class="feishu-section"><h3>存到哪里</h3><div class="format-options"><label><input type="radio" name="feishuMode" value="0">已有表格</label><label><input type="radio" name="feishuMode" value="1">新建表格</label></div>
          <div id="feishuExisting">
            <label class="field-label" for="feishuTarget">目标表格</label><select id="feishuTarget"></select>
            <div id="feishuLinkRow"><label class="field-label" for="feishuUrl">表格链接</label><div class="feishu-link-line"><input class="dialog-input" id="feishuUrl" placeholder="粘贴飞书多维表格链接"><button class="btn secondary" id="feishuRead">读取</button></div></div>
            <label class="field-label" for="feishuTable">数据表</label><select id="feishuTable"></select>
            <details id="feishuMapping" hidden><summary>列对应 · 查看与调整</summary><div class="mapping-grid" id="feishuMappingFields"></div></details>
          </div>
          <div id="feishuNew" hidden>
            <label class="field-label" for="feishuTemplate">使用模板</label><select id="feishuTemplate"></select>
            <label class="field-label" for="feishuName">名称</label><input class="dialog-input" id="feishuName">
            <label class="field-label" for="feishuLocation">存放位置</label><select id="feishuLocation"></select>
            <input class="dialog-input" id="feishuLocationUrl" aria-label="位置链接" placeholder="粘贴文件夹或知识库链接" hidden>
          </div>
        </div>
        <div class="feishu-section"><h3>再次保存时</h3><div class="format-options"><label><input type="radio" name="repeat" value="0">更新原内容</label><label><input type="radio" name="repeat" value="1">另存一份</label></div></div>
      </div>
      <p class="dialog-status" id="feishuStatus" role="status"></p>
      <p class="dialog-copy" id="feishuDetails" hidden></p>
      <div class="dialog-buttons"><button class="btn quiet" id="feishuOpen" hidden>打开飞书</button><button class="btn secondary" id="feishuAdjust" hidden>调整选项</button><button class="btn secondary" id="feishuCancel">取消</button><button class="btn primary" id="feishuConfirm" disabled>存入</button></div>`;
    for(const [id,action] of Object.entries({
      feishuConnect:'connect',feishuRead:'read',feishuConfirm:'submit',
      feishuCancel:'cancel',feishuOpen:'open',feishuAdjust:'adjust',
    })) $('#'+id).onclick=()=>perform('feishu_control',{action});
    for(const [id,action] of Object.entries({feishuTarget:'target',feishuTable:'table',feishuTemplate:'template',feishuLocation:'location'})) {
      $('#'+id).onchange=event=>perform('feishu_control',{action,value:Number(event.target.value)});
    }
    for(const [id,action] of Object.entries({feishuUrl:'url',feishuName:'name',feishuLocationUrl:'location_url'})) {
      const input=$('#'+id);
      const update=()=>perform('feishu_control',{action,value:input.value});
      input.oninput=event=>{if(!event.isComposing)update();};
      input.oncompositionend=update;
      input.onchange=update;
    }
    for(const [name,action] of [['feishuMode','mode'],['repeat','repeat']]) {
      for(const radio of $$('[name='+name+']')) radio.onchange=()=>perform('feishu_control',{action,value:Number(radio.value)});
    }
    if (!modal.open) modal.showModal();
  }
  if (state.complete && $('#feishuForm')) {
    $('#modalBody').innerHTML=`
      <div class="dialog-callout"><strong>存入完成</strong><p id="feishuStatus" role="status">${esc(state.status)}</p>
        <p>封面与音视频分别记录保存结果。</p>${state.details?`<p>${esc(state.details)}</p>`:''}</div>
      <div class="dialog-buttons">${state.canOpen?'<button class="btn quiet" id="feishuOpen">打开飞书</button>':''}
        <button class="btn primary" id="feishuConfirm">完成</button></div>`;
    $('#feishuConfirm').onclick=()=>perform('feishu_control',{action:'submit'});
    if ($('#feishuOpen')) $('#feishuOpen').onclick=()=>perform('feishu_control',{action:'open'});
    $('#feishuConfirm').focus({preventScroll:true});
    return;
  }
  if (state.complete) return;
  $('#feishuCount').textContent=state.count;
  $('#feishuSource').textContent=state.source || '已选作品';
  $('#feishuAccount').textContent=state.account;
  $('#feishuStatus').textContent=state.status;
  $('#feishuForm').inert=state.busy||state.result;
  $('#feishuForm').hidden=state.complete;
  $('#feishuConnect').disabled=state.busy||state.result;
  $('#feishuExisting').hidden=state.mode!=='existing';
  $('#feishuNew').hidden=state.mode!=='new';
  $('#feishuLocationUrl').hidden=!state.custom;
  $('#feishuLinkRow').hidden=state.target.index>0;
  $('#feishuRead').disabled=!state.connected||state.busy;
  for(const [name,value] of [['feishuMode',state.mode==='new'?1:0],['repeat',state.repeat==='copy'?1:0]]) {
    const radio=$('[name='+name+'][value="'+value+'"]');
    radio.checked=true;
  }
  for(const [id,value] of Object.entries({feishuUrl:state.url,feishuName:state.name,feishuLocationUrl:state.locationUrl})) {
    const input=$('#'+id);
    if (document.activeElement!==input) input.value=value;
  }
  for(const [id,value] of Object.entries({feishuTarget:state.target,feishuTable:state.table,feishuTemplate:state.template,feishuLocation:state.location})) {
    syncFeishuSelect($('#'+id),value);
  }
  $('#feishuTable').disabled=state.table.labels.length<=1;
  const grid=$('#feishuMappingFields');
  const fields=state.mapping.map(row=>row.key).join('|');
  if(grid.dataset.fields!==fields){
    grid.dataset.fields=fields;
    grid.innerHTML=state.mapping.map(row=>`<label>${esc(row.title)}<select data-field="${esc(row.key)}" aria-label="${esc(row.title)}对应列"></select></label>`).join('');
    for(const select of $$('select',grid)) select.onchange=()=>perform('feishu_control',{action:'mapping',key:select.dataset.field,value:Number(select.value)});
  }
  for(const row of state.mapping) syncFeishuSelect($$('select',grid).find(select=>select.dataset.field===row.key),row);
  $('#feishuMapping').hidden=!state.mapping.length;
  $('#feishuConfirm').disabled=!state.canSubmit||state.busy;
  $('#feishuConfirm').textContent=state.complete?'完成':state.result?state.submitLabel:'存入';
  $('#feishuCancel').textContent=state.busy?'停止':state.result?'关闭':'取消';
  $('#feishuCancel').hidden=state.complete;
  $('#feishuOpen').hidden=!state.canOpen;
  $('#feishuAdjust').hidden=!state.result||state.complete;
  $('#feishuDetails').hidden=!state.details;
  $('#feishuDetails').textContent=state.details;
  LuluSelect.enhance(modal);
  LuluSelect.syncAll();
}

function syncFeishuSelect(select,state) {
  if (!select) return;
  const labels=JSON.stringify(state.labels);
  if(select.dataset.labels!==labels){
    select.dataset.labels=labels;
    select.replaceChildren(...state.labels.map((label,index)=>new Option(label,String(index))));
  }
  select.value=String(state.index);
}

$('#modal').addEventListener('close',event=>{
  if(event.target.dataset.surface!=='feishu')return;
  if(feishuClosing){feishuClosing=false;return;}
  if(feishuState)perform('feishu_control',{action:'cancel'});
});

$('#modal').addEventListener('cancel',event=>{
  if(event.target.dataset.surface!=='feishu')return;
  event.preventDefault();
  perform('feishu_control',{action:'cancel'});
});

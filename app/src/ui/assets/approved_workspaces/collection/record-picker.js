/* Record selection UI only; the existing collection workflow owns switching. */
'use strict';
window.RecordPicker=(()=>{
  const q=s=>document.querySelector(s),pop=q('#historyPopover'),list=q('#historyOptions');
  const input=q('#recordSearch'),clear=q('#clearRecordSearch'),trigger=q('#recordTrigger');
  const escape=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const normalize=value=>String(value).normalize('NFKC').toLocaleLowerCase().trim();
  let entries=[],current='',active='',visible=[];

  function mark(key,{scroll=false}={}){
    active=key;
    const rows=[...list.querySelectorAll('[data-record]')];
    rows.forEach(row=>row.classList.toggle('is-active',row.dataset.record===key));
    const row=rows.find(row=>row.dataset.record===key);
    if(row){
      input.setAttribute('aria-activedescendant',row.id);
      if(scroll){
        if(row.offsetTop<list.scrollTop)list.scrollTop=row.offsetTop;
        else if(row.offsetTop+row.offsetHeight>list.scrollTop+list.clientHeight)list.scrollTop=row.offsetTop+row.offsetHeight-list.clientHeight;
      }
    }else input.removeAttribute('aria-activedescendant');
  }

  function paint(){
    const query=normalize(input.value);
    visible=entries.filter(([,record])=>normalize(record.name).includes(query));
    clear.hidden=!input.value;
    q('#recordListCaption').textContent=query?'搜索结果':'全部记录';
    q('#recordMatches').textContent=visible.length+' 条';
    list.innerHTML=visible.length?visible.map(([key,record],index)=>{
      const chosen=key===current,metadata=record.count+' 条作品 · '+record.time;
      return '<button class="history-option" role="option" aria-selected="'+chosen+'" tabindex="-1" id="record-option-'+index+'" data-record="'+escape(key)+'" title="'+escape(record.name)+'"><span class="history-option-copy"><strong>'+escape(record.name)+'</strong><small>'+escape(metadata)+'</small></span><span class="history-option-check" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg></span></button>';
    }).join(''):'<div class="record-search-empty"><strong>'+(query?'没有找到相关记录':'还没有采集记录')+'</strong><span>'+(query?'试试其他名称':'采集主页后，会显示在这里')+'</span></div>';
    list.scrollTop=0;
    mark(visible.some(([key])=>key===active)?active:visible.some(([key])=>key===current)?current:visible[0]?.[0]||'');
    pop.style.setProperty('--record-list-height',Math.max(visible.length?64:86,Math.min(visible.length,5)*56+8)+'px');
    if(pop.matches(':popover-open'))positionPopover(trigger,pop);
  }

  function render(next,selected){entries=next;current=selected;paint();}
  function resetSearch(){input.value='';active=current;paint();}
  input.addEventListener('input',()=>{if(!input.composing){active='';paint();}});
  input.addEventListener('compositionstart',()=>{input.composing=true;});
  input.addEventListener('compositionend',()=>{input.composing=false;active='';paint();});
  clear.addEventListener('click',()=>{resetSearch();input.focus({preventScroll:true});});
  list.addEventListener('pointermove',event=>{const row=event.target.closest('[data-record]');if(row&&event.pointerType!=='touch')mark(row.dataset.record);});
  list.addEventListener('pointerdown',event=>{if(event.target.closest('[data-record]'))event.preventDefault();});
  pop.addEventListener('beforetoggle',event=>{
    if(event.newState==='open'){
      resetSearch();
    }
  });
  pop.addEventListener('toggle',event=>{
    const open=event.newState==='open';
    trigger.setAttribute('aria-expanded',String(open));input.setAttribute('aria-expanded',String(open));
    if(open){input.focus({preventScroll:true});mark(active,{scroll:true});}
  });
  pop.addEventListener('keydown',event=>{
    if(event.isComposing||input.composing)return;
    if(event.key==='Escape'){
      event.preventDefault();event.stopPropagation();
      if(input.value){resetSearch();input.focus({preventScroll:true});}
      else{pop.hidePopover();trigger.focus({preventScroll:true});}
      return;
    }
    if(event.target!==input&&!list.contains(event.target))return;
    if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
      event.preventDefault();event.stopPropagation();
      if(!visible.length)return;
      const index=visible.findIndex(([key])=>key===active),step=event.key==='ArrowUp'?-1:1;
      const next=event.key==='Home'?0:event.key==='End'?visible.length-1:Math.max(0,Math.min(visible.length-1,index+step));
      mark(visible[next][0],{scroll:true});input.focus({preventScroll:true});
    }else if(event.key==='Enter'&&event.target===input){
      event.preventDefault();list.querySelector('.is-active')?.click();
    }
  });
  trigger.addEventListener('keydown',event=>{
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      event.preventDefault();if(!pop.matches(':popover-open'))pop.showPopover();
      mark(event.key==='ArrowUp'?visible.at(-1)?.[0]:visible[0]?.[0],{scroll:true});
    }
  });
  return {render};
})();

/* Shared select controls for this isolated preview. Native selects only store values. */
'use strict';
window.LuluSelect = (() => {
  const instances = new Set();
  const bySelect = new WeakMap();
  let active = null;
  let sequence = 0;
  let typeBuffer = '';
  let typeTime = 0;
  let typeOwner = null;
  let scrollFrame = 0;
  const reduce = () => matchMedia('(prefers-reduced-motion: reduce)').matches || document.documentElement.classList.contains('keyboard-input');
  const svg = path => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"></path></svg>`;
  const caret = svg('M6 9l6 6 6-6');
  const check = svg('M5 12l4 4L19 6');

  function destroyMenu(context, restore = false) {
    context.animation?.cancel();
    if (typeof context.menu.hidePopover === 'function') {
      try { context.menu.hidePopover(); } catch (_) { /* It can already be hidden. */ }
    }
    context.menu.remove();
    context.instance.button.setAttribute('aria-expanded', 'false');
    context.instance.button.removeAttribute('aria-controls');
    context.instance.button.removeAttribute('aria-activedescendant');
    if (active === context) active = null;
    if (restore && context.instance.button.isConnected && !context.instance.button.disabled) context.instance.button.focus({preventScroll:true});
  }

  function close({restore = false, immediate = false} = {}) {
    if (!active) return;
    const context = active;
    const view = getComputedStyle(context.menu);
    const from = {opacity:view.opacity, transform:view.transform};
    context.animation?.cancel();
    context.closing = true;
    context.instance.button.setAttribute('aria-expanded', 'false');
    context.instance.button.removeAttribute('aria-activedescendant');
    if (restore && context.instance.button.isConnected) context.instance.button.focus({preventScroll:true});
    if (immediate || reduce()) return destroyMenu(context);
    context.animation = context.menu.animate([from, {opacity:0, transform:`translateY(${context.direction * -2}px) scale(.985)`}], {duration:110, easing:'cubic-bezier(.4,0,1,1)'});
    context.animation.onfinish = () => destroyMenu(context);
  }

  function visibleOption(context, index) {
    const item = context.items[index];
    if (!item) return;
    const top = item.offsetTop;
    const bottom = top + item.offsetHeight;
    if (top < context.menu.scrollTop) context.menu.scrollTop = top;
    else if (bottom > context.menu.scrollTop + context.menu.clientHeight) context.menu.scrollTop = bottom - context.menu.clientHeight;
  }

  function markActive(context, index) {
    if (!context.options[index] || context.options[index].disabled) return;
    context.index = index;
    context.items.forEach((item, n) => item.classList.toggle('is-active', n === index));
    context.instance.button.setAttribute('aria-activedescendant', context.items[index].id);
    visibleOption(context, index);
  }

  function place(context) {
    const anchor = context.instance.wrapper.getBoundingClientRect();
    const menu = context.menu;
    menu.style.minWidth = Math.min(Math.max(anchor.width, 166), innerWidth - 24) + 'px';
    menu.style.maxWidth = Math.max(0, innerWidth - 24) + 'px';
    menu.style.maxHeight = '320px';
    const below = innerHeight - anchor.bottom - 20;
    const above = anchor.top - 20;
    const needed = Math.min(menu.scrollHeight + 2, 320);
    context.direction = below >= Math.min(needed, 180) || below >= above ? 1 : -1;
    menu.style.maxHeight = Math.max(48, Math.min(320, context.direction === 1 ? below : above)) + 'px';
    const rect = menu.getBoundingClientRect();
    const left = Math.max(12, Math.min(anchor.left, innerWidth - rect.width - 12));
    const top = context.direction === 1 ? anchor.bottom + 8 : anchor.top - rect.height - 8;
    menu.style.left = left + 'px';
    menu.style.top = Math.max(12, Math.min(top, innerHeight - rect.height - 12)) + 'px';
    menu.style.transformOrigin = `${Math.max(12, Math.min(rect.width - 12, anchor.left + anchor.width / 2 - left))}px ${context.direction === 1 ? 'top' : 'bottom'}`;
    menu.dataset.side = context.direction === 1 ? 'bottom' : 'top';
  }

  function open(instance, mode = '') {
    if (instance.select.disabled || instance.wrapper.hidden) return;
    if (active?.instance === instance && active.closing) {
      const context = active;
      const view = getComputedStyle(context.menu);
      const from = {opacity:view.opacity, transform:view.transform};
      context.animation?.cancel();context.closing = false;
      instance.button.setAttribute('aria-expanded','true');
      markActive(context, context.index);
      if (!reduce()) context.animation = context.menu.animate([from,{opacity:1,transform:'none'}],{duration:170,easing:'cubic-bezier(.22,1,.36,1)'});
      return;
    }
    if (active?.instance === instance) return close({restore:true});
    close({immediate:true});
    const options = [...instance.select.options];
    if (!options.some(option => !option.disabled)) return;
    const menu = document.createElement('div');
    menu.className = 'select-menu';menu.id = `select-menu-${++sequence}`;
    menu.setAttribute('role','listbox');menu.setAttribute('aria-label',instance.button.getAttribute('aria-label'));
    if (typeof menu.showPopover === 'function') menu.setAttribute('popover','manual');
    const items = options.map((option,index) => {
      const item = document.createElement('div');
      item.className = 'select-option';item.id = `${menu.id}-option-${index}`;
      item.setAttribute('role','option');item.setAttribute('aria-label',option.label);item.setAttribute('aria-selected',String(option.selected));
      if (option.disabled) item.setAttribute('aria-disabled','true');
      const text = document.createElement('span');text.className = 'select-option-text';
      const title = document.createElement('span');title.className = 'select-option-title';title.textContent = option.label;
      text.append(title);
      if (option.dataset.description) { const description=document.createElement('span');description.className='select-option-description';description.id=item.id+'-description';description.textContent=option.dataset.description;item.setAttribute('aria-describedby',description.id);text.append(description); }
      const selected = document.createElement('span');selected.className = 'select-option-check';selected.innerHTML = check;
      item.append(text,selected);menu.append(item);
      item.addEventListener('pointerdown',event=>event.preventDefault());
      item.addEventListener('pointermove',()=>{if(active?.menu===menu&&!active.closing&&!option.disabled)markActive(active,index);});
      item.addEventListener('click',()=>{if(active?.menu===menu&&!option.disabled)commit(active,index);});
      return item;
    });
    const context = active = {instance,menu,options,items,index:0,direction:1,closing:false};
    (instance.button.closest('dialog[open]') || document.body).append(menu);
    if (typeof menu.showPopover === 'function') menu.showPopover();
    instance.button.setAttribute('aria-expanded','true');instance.button.setAttribute('aria-controls',menu.id);
    const enabled = options.map((option,index)=>!option.disabled?index:-1).filter(index=>index>=0);
    const selected = options.findIndex(option=>option.selected&&!option.disabled);
    place(context);markActive(context,mode==='first'?enabled[0]:mode==='last'?enabled.at(-1):selected>=0?selected:enabled[0]);
    instance.button.focus({preventScroll:true});
    if (!reduce()) context.animation = menu.animate([{opacity:0,transform:`translateY(${context.direction * -3}px) scale(.975)`},{opacity:1,transform:'none'}],{duration:180,easing:'cubic-bezier(.22,1,.36,1)'});
  }

  function commit(context, index, restore = true) {
    const option = context.options[index];
    if (!option || option.disabled) return;
    const changed = context.instance.select.value !== option.value;
    context.instance.select.value = option.value;
    context.items.forEach((item,n)=>item.setAttribute('aria-selected',String(n===index)));
    sync(context.instance);
    close({restore,immediate:reduce()});
    if (changed) context.instance.select.dispatchEvent(new Event('change',{bubbles:true}));
  }

  function typeSelect(instance, character) {
    const now = performance.now();
    typeBuffer = now-typeTime > 800 || typeOwner !== instance ? character : typeBuffer + character;typeTime=now;typeOwner=instance;
    if (!active || active.instance !== instance || active.closing) open(instance);
    if (!active || active.instance !== instance) return;
    const needle = [...typeBuffer].every(char=>char===typeBuffer[0]) ? typeBuffer[0] : typeBuffer;
    const start = needle.length===1 ? active.index+1 : 0;
    for (let offset=0;offset<active.options.length;offset++) {
      const index=(start+offset)%active.options.length,option=active.options[index];
      if (!option.disabled&&option.label.toLocaleLowerCase().startsWith(needle)) return markActive(active,index);
    }
  }

  function keydown(instance, event) {
    if (event.isComposing || event.metaKey || event.ctrlKey) return;
    document.documentElement.classList.add('keyboard-input');
    const expanded = active?.instance===instance&&!active.closing;
    const handled=['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(event.key) || expanded&&['Escape','Tab'].includes(event.key);
    if (handled) { event.preventDefault();event.stopPropagation(); }
    if (event.key==='Escape'&&expanded) return close({restore:true,immediate:true});
    if (['Enter',' '].includes(event.key)) return expanded?commit(active,active.index):open(instance);
    if (event.key==='Tab'&&expanded) {
      commit(active,active.index,false);close({immediate:true});
      const scope=instance.button.closest('dialog[open]')||document.body;
      const focusable=[...scope.querySelectorAll('button,input,select,textarea,a[href],[tabindex="0"]')].filter(element=>!element.disabled&&!element.closest('.select-menu')&&element.getClientRects().length>0);
      const next=focusable[focusable.indexOf(instance.button)+(event.shiftKey?-1:1)];
      next?.focus({preventScroll:true});return;
    }
    if (event.key==='Home'||event.key==='End') {
      if (!expanded) return open(instance,event.key==='Home'?'first':'last');
      const enabled=active.options.map((option,index)=>!option.disabled?index:-1).filter(index=>index>=0);
      return markActive(active,event.key==='Home'?enabled[0]:enabled.at(-1));
    }
    if (event.key==='ArrowDown'||event.key==='ArrowUp') {
      if (!expanded) return open(instance);
      const direction=event.key==='ArrowDown'?1:-1;
      for(let index=active.index+direction;index>=0&&index<active.options.length;index+=direction)if(!active.options[index].disabled)return markActive(active,index);
      return;
    }
    if (event.key.length===1&&!event.altKey) {event.preventDefault();event.stopPropagation();typeSelect(instance,event.key.toLocaleLowerCase());}
  }

  function sync(instance) {
    const select=instance.select;
    instance.value.textContent=select.selectedOptions[0]?.label||'请选择';
    instance.button.disabled=select.disabled;
    instance.button.setAttribute('aria-label',select.getAttribute('aria-label')||select.closest('label')?.querySelector('span')?.textContent||'选择选项');
    instance.wrapper.hidden=select.hidden;
    const scene=select.closest('.scene-control');if(scene)scene.hidden=select.hidden;
    if(active?.instance===instance&&(select.disabled||select.hidden||!select.isConnected))close({immediate:true});
  }

  function syncAll() { for(const instance of instances)if(instance.select.isConnected)sync(instance); }
  function enhance(root = document) {
    for(const instance of instances)if(!instance.select.isConnected){if(active?.instance===instance)close({immediate:true});instance.observer.disconnect();instances.delete(instance);}
    for(const select of root.querySelectorAll('select')) {
      if(bySelect.has(select)){sync(bySelect.get(select));continue;}
      const wrapper=document.createElement('div');wrapper.className='select-field';wrapper.dataset.selectFor=select.id||select.dataset.pref||'select';
      select.before(wrapper);wrapper.append(select);select.classList.add('select-native');select.tabIndex=-1;select.setAttribute('aria-hidden','true');
      const button=document.createElement('button');button.type='button';button.className='select-trigger';button.setAttribute('role','combobox');button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');
      const value=document.createElement('span');value.className='select-value';
      const arrow=document.createElement('span');arrow.className='select-chevron';arrow.innerHTML=caret;
      button.append(value,arrow);wrapper.append(button);
      const instance={select,wrapper,button,value};
      button.addEventListener('click',()=>open(instance));button.addEventListener('keydown',event=>keydown(instance,event));
      select.addEventListener('change',()=>sync(instance));
      instance.observer=new MutationObserver(()=>sync(instance));instance.observer.observe(select,{attributes:true,childList:true,subtree:true,attributeFilter:['disabled','hidden','aria-label','selected','value']});
      bySelect.set(select,instance);instances.add(instance);sync(instance);
    }
  }

  document.addEventListener('pointerdown',event=>{if(active&&!active.menu.contains(event.target)&&!active.instance.wrapper.contains(event.target))close();},true);
  document.addEventListener('click',()=>queueMicrotask(syncAll));
  document.addEventListener('scroll',event=>{
    if(!active||active.closing||active.menu.contains(event.target)||scrollFrame)return;
    scrollFrame=requestAnimationFrame(()=>{
      scrollFrame=0;if(!active||active.closing)return;
      const anchor=active.instance.wrapper.getBoundingClientRect();
      const owner=active.instance.wrapper.closest('.page-scroll,dialog[open]');
      const boundary=owner?owner.getBoundingClientRect():{top:0,bottom:innerHeight,left:0,right:innerWidth};
      if(anchor.bottom<=Math.max(0,boundary.top)||anchor.top>=Math.min(innerHeight,boundary.bottom)||anchor.right<=boundary.left||anchor.left>=boundary.right)return close({immediate:true});
      place(active);visibleOption(active,active.index);
    });
  },true);
  window.addEventListener('resize',()=>close({immediate:true}));
  return {enhance,syncAll,close};
})();

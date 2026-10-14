import { icon } from './icons.js';

let active = null;
export function closeMenu(restoreFocus = false) {
  if (!active) return;
  const { anchor, menu } = active;
  active = null;
  anchor.setAttribute('aria-expanded', 'false');
  menu.remove();
  if (restoreFocus && anchor.isConnected) anchor.focus({ preventScroll: true });
}

// One anchored surface for actions and choices, including selects inside dialogs.
// No modal scrim, no layout movement; the original button remains the anchor.
export function openMenu(anchor, items, { label = '', choice = false, searchable = false, align = 'end', point = null, onError = () => {} } = {}) {
  if (active?.anchor === anchor) { closeMenu(true); return; }
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'lulu-menu';
  menu.setAttribute('role', searchable ? 'dialog' : choice ? 'listbox' : 'menu');
  menu.setAttribute('aria-label', label);
  const rect = point ? {left:point.x, right:point.x, top:point.y, bottom:point.y, width:0} : anchor.getBoundingClientRect();
  menu.style.minWidth = Math.min(Math.max(choice ? rect.width : 226, 180), innerWidth - 24) + 'px';
  let search, list = menu;
  if (searchable) {
    search = document.createElement('input');
    search.type = 'search'; search.className = 'menu-search'; search.placeholder = '搜索模型';
    search.setAttribute('aria-label', '搜索模型');
    list = document.createElement('div'); list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', label);
    menu.append(search, list);
  }
  for (const item of items) {
    if (item.separator) { const line = document.createElement('div'); line.className = 'menu-separator'; line.setAttribute('role', 'separator'); menu.append(line); continue; }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'lulu-menu-item' + (item.danger ? ' danger' : '');
    button.setAttribute('role', choice ? 'option' : 'menuitem');
    if (choice) button.setAttribute('aria-selected', String(Boolean(item.selected)));
    if (item.id) button.id = item.id;
    button.disabled = Boolean(item.disabled);
    button.tabIndex = -1;
    const glyph = document.createElement('span');
    glyph.className = 'menu-glyph';
    glyph.innerHTML = choice ? (item.selected ? icon('check') : '') : icon(item.icon || 'doc');
    const text = document.createElement('span');
    text.className = 'menu-label';
    text.textContent = item.label;
    button.append(glyph, text);
    button.onclick = () => {
      closeMenu(true);
      Promise.resolve().then(item.run).catch(onError);
    };
    list.append(button);
  }
  if (search) {
    const empty = document.createElement('div'); empty.className = 'menu-empty'; empty.textContent = '没有匹配的模型'; empty.hidden = true;
    menu.append(empty);
    search.oninput = () => {
      const query = search.value.trim().toLocaleLowerCase();
      for (const row of list.children) row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
      empty.hidden = [...list.children].some(row => !row.hidden);
    };
  }
  (anchor.closest('dialog') || document.body).append(menu);
  active = { anchor, menu };
  anchor.setAttribute('aria-expanded', 'true');
  anchor.setAttribute('aria-haspopup', searchable ? 'dialog' : choice ? 'listbox' : 'menu');
  const gap = 6, edge = 12;
  const below = innerHeight - rect.bottom - gap - edge, above = rect.top - gap - edge;
  const upward = below < Math.min(menu.scrollHeight, 320) && above > below;
  menu.style.maxHeight = Math.max(80, Math.min(420, upward ? above : below)) + 'px';
  const width = menu.getBoundingClientRect().width, height = menu.getBoundingClientRect().height;
  menu.style.left = Math.max(edge, Math.min(align === 'start' ? rect.left : rect.right - width, innerWidth - width - edge)) + 'px';
  menu.style.top = Math.max(edge, upward ? rect.top - height - gap : rect.bottom + gap) + 'px';
  const buttons = () => [...menu.querySelectorAll('button:not(:disabled):not([hidden])')];
  (search || menu.querySelector('[aria-selected="true"]') || buttons()[0])?.focus({ preventScroll: true });
  let typed = '', typedAt = 0;
  menu.onkeydown = event => {
    const all = buttons(), i = all.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = all[(i + 1) % all.length];
    if (event.key === 'ArrowUp') next = i < 0 ? all.at(-1) : all[(i - 1 + all.length) % all.length];
    if (event.target !== search && event.key === 'Home') next = all[0];
    if (event.target !== search && event.key === 'End') next = all.at(-1);
    if (event.key === 'Escape') { event.stopPropagation(); event.preventDefault(); closeMenu(true); return; }
    if (event.key === 'Tab') { closeMenu(true); return; }
    if (event.target !== search && event.key.length === 1 && !event.ctrlKey && !event.metaKey && event.key !== ' ') {
      typed = (performance.now() - typedAt < 700 ? typed : '') + event.key.toLowerCase(); typedAt = performance.now();
      next = all.find(b => b.textContent.trim().toLowerCase().startsWith(typed));
    }
    if (next) { event.preventDefault(); next.focus({ preventScroll: true }); next.scrollIntoView({ block: 'nearest' }); }
  };
  return menu;
}

document.addEventListener('pointerdown', event => {
  if (active && !active.menu.contains(event.target) && !active.anchor.contains(event.target)) closeMenu();
}, true);
document.addEventListener('scroll', event => {
  if (active && !active.menu.contains(event.target)) closeMenu();
}, true);
window.addEventListener('resize', () => closeMenu());
document.addEventListener('close', () => closeMenu(), true);

const controls = new WeakMap();
export function syncSelect(select) {
  if (!select.isConnected || select.multiple) return;
  let trigger = controls.get(select);
  if (!trigger) {
    trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'lulu-select';
    trigger.id = select.id + '-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-label', select.getAttribute('aria-label') || document.querySelector(`label[for="${select.id}"]`)?.textContent || select.closest('label')?.firstChild?.textContent?.trim() || '选择选项');
    select.classList.add('lulu-select-source');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select.after(trigger);
    document.querySelectorAll(`label[for="${select.id}"]`).forEach(label => label.htmlFor = trigger.id);
    controls.set(select, trigger);
    trigger.onclick = () => openMenu(trigger, [...select.options].filter(o => !o.hidden).map(option => ({
      label: option.textContent, selected: option.selected, disabled: option.disabled,
      run: () => { if (select.value !== option.value) { select.value = option.value; select.dispatchEvent(new Event('change', { bubbles: true })); } syncSelect(select); }
    })), { label: trigger.getAttribute('aria-label'), choice: true, searchable:select.dataset.searchable === 'true', align: 'start' });
    trigger.onkeydown = event => {
      if (['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); trigger.click(); }
    };
    select.addEventListener('change', () => syncSelect(select));
  }
  trigger.disabled = select.disabled;
  const label = select.selectedOptions[0]?.textContent || '请选择';
  if (trigger.dataset.label !== label) {
    trigger.dataset.label = label;
    trigger.replaceChildren();
    const text = document.createElement('span'); text.textContent = label;
    trigger.append(text); trigger.insertAdjacentHTML('beforeend', icon('chevron'));
  }
}
export function enhanceSelects(root = document) {
  root.querySelectorAll('select').forEach(syncSelect);
}
// Visit only added subtrees and changed option lists, not the whole manuscript
// for every keystroke. Native select values remain the single source of truth.
export function observeSelects() {
  enhanceSelects();
  new MutationObserver(records => {
    const changed = new Set();
    for (const record of records) {
      const select = record.target instanceof Element ? record.target.closest('select') : record.target.parentElement?.closest('select');
      if (select) changed.add(select);
      for (const node of record.addedNodes) if (node instanceof Element) {
        if (node.matches('select')) changed.add(node);
        node.querySelectorAll('select').forEach(s => changed.add(s));
      }
    }
    changed.forEach(syncSelect);
    if (active && !active.anchor.isConnected) closeMenu();
  }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled', 'selected'] });
}

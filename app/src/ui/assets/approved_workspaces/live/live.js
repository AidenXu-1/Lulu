(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const paths = {
    audio: '<path d="M4 14v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="12" width="4" height="8" rx="2"/><rect x="17" y="12" width="4" height="8" rx="2"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    mic: '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
    video: '<rect x="3" y="6" width="12" height="13" rx="2"/><path d="m15 10 6-3v12l-6-3"/>',
    file: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5Z"/><path d="M14 3v5h5M8 12h8M8 16h6"/>',
    wave: '<path d="M3 10v4M7 6v12M12 3v18M17 7v10M21 10v4"/>',
    archive: '<path d="M3 4h18v5H3zM5 9v11h14V9M10 13h4"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    cube: '<path d="m12 2 9 5v10l-9 5-9-5V7l9-5Zm-9 5 9 5 9-5M12 12v10M7.5 4.5l9 5"/>',
    gem: '<path d="m10 2 7 3 4 10-8 7-9-5-1-8 7-7Zm0 0 3 10m0 10V12l8 3M3 9l10 3 4-7"/>',
    feather: '<path d="m4 20 8-8M4 20l1-7L17 3a3 3 0 0 1 4 4L11 19l-7 1ZM9 9l6 6"/>',
    folder: '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11H3V7Z"/>',
    chevron: '<path d="m7 10 5 5 5-5"/>',
    settings: '<path d="m10 3-.5 3-2 .9L5 5.7 3 9l2.3 1.8v2.4L3 15l2 3.3 2.5-1.2 2 .9.5 3h4l.5-3 2-.9 2.5 1.2 2-3.3-2.3-1.8v-2.4L21 9l-2-3.3-2.5 1.2-2-.9-.5-3h-4Z"/><circle cx="12" cy="12" r="3"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    pause: '<path d="M8 5v14M16 5v14"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    left: '<path d="m14 6-6 6 6 6"/>',
    right: '<path d="m10 6 6 6-6 6"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
    external: '<path d="M7 5H4v15h15v-3M12 4h8v8M20 4 10 14"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    browser: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M2 9h20M6 6.5h.1M9 6.5h.1"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    more: '<circle cx="5" cy="12" r=".8"/><circle cx="12" cy="12" r=".8"/><circle cx="19" cy="12" r=".8"/>'
  };
  const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.info}</svg>`;
  const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  document.querySelectorAll('[data-icon]').forEach((el) => el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon)));


  const tabStrip = document.querySelector('.tabs');
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const reduced = () => reduceMotion.matches || document.body.classList.contains('reduce-motion');
  const hostNodes = new Map(), recordNodes = new Map(), pending = new Map();
  const views = {hosts:{query:'',filter:'all',page:1,scroll:0,left:0}, recordings:{query:'',filter:'all',page:1,scroll:0,left:0}};
  const shown = {hosts:[],recordings:[]}, totals = {hosts:0,recordings:0};
  let tab = 'hosts', page = 1, pages = 1, listTransition, restoreScroll, loading = true;
  let snapshot = {hosts:0,recordings:0,monitoring:0,recording:0};
  let api, queryTimer, commentsPending = false, toastTimer, lastFocus, popoverOwner;
  let openRecord = null, openSegmentRecord = null, detailGeneration = 0, detailOpening = 0;
  const hostFilters = [['all', '全部状态'], ['live', '直播中'], ['offline', '未直播'], ['unknown', '无法确认']];
  const dateFilters = [['all', '全部日期'], ['today', '今天'], ['week', '最近 7 天'], ['month', '最近 30 天']];
  const statusText = { live: '直播中', offline: '未直播', unknown: '无法确认' };
  const pad = value => String(value).padStart(2, '0');
  function stamp(value) {
    if (!value) return '—';
    const date = new Date(value);
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  function duration(seconds) {
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60].map(pad).join(':');
  }
  function toast(message, error = false) {
    clearTimeout(toastTimer);
    $('toast').classList.toggle('error', error);
    $('toast').innerHTML = icon(error ? 'info' : 'check') + `<span>${esc(message)}</span>`;
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3400);
  }
  function secondsFor(record) {
    if (!record) return 0;
    const elapsed = record.runningSince === null ? 0 : Math.min(5, Math.floor((performance.now() - record.runningSince) / 1000));
    return record.seconds + elapsed;
  }

  function filesFor(record) {
    const bytes = record.bytes;
    const size = bytes >= 1024 ** 3 ? (bytes / 1024 ** 3).toFixed(2) + ' GB' : (bytes / 1024 ** 2).toFixed(1) + ' MB';
    return record.segmentCount + ' 段 · ' + size;
  }
  function makeHostRow(host) {
    const row = document.createElement('tr');
    row.dataset.hostId = host.id;
    row.innerHTML = `<td><button class="remove-host" title="移除主播并保留已有录像" aria-label="移除 ${esc(host.name)}">×</button></td>
      <td><div class="host-name"></div><div class="host-source"></div></td>
      <td><span class="status"><i></i><span></span></span></td><td><span class="checked"></span></td>
      <td><div class="progress"><strong></strong><span></span><button class="access-action" type="button" aria-haspopup="dialog" hidden>抖音连接</button></div></td>
      <td><button class="open-room">打开 ${icon('external')}</button></td>
      <td><button class="button row-action"></button></td>`;
    row.querySelector('.host-name').textContent = host.name;
    row.querySelector('.host-name').title = host.name;
    row.querySelector('.host-source').textContent = host.url;
    row.querySelector('.host-source').title = host.url;
    row.querySelector('.open-room').onclick = () => perform('open_room', {id:host.id});
    row.querySelector('.access-action').onclick = showConnection;
    return row;
  }
  function updateHostRow(host, row) {
    row.querySelector('.host-name').textContent = host.name;
    row.querySelector('.host-name').title = host.name;
    row.querySelector('.host-source').textContent = host.url;
    row.querySelector('.host-source').title = host.url;
    row.querySelector('.remove-host').onclick = () => confirmRemove(host);
    row.querySelector('.row-action').onclick = () => toggleHost(host);
    const state = row.querySelector('.status');
    state.className = 'status ' + host.status;
    state.lastElementChild.textContent = statusText[host.status];
    row.querySelector('.checked').textContent = stamp(host.checked);
    const progress = row.querySelector('.progress');
    progress.className = 'progress' + (host.error ? ' error' : host.record ? ' recording' : '');
    progress.firstElementChild.hidden = host.needsLogin;
    progress.firstElementChild.textContent = host.record ? duration(secondsFor(host.record)) : '—';
    progress.children[1].textContent = host.error || host.progress || '—';
    progress.title = host.error || host.progress || '';
    const access = progress.lastElementChild;
    access.hidden = !host.needsLogin;
    access.textContent = snapshot.connected ? '管理连接' : '抖音连接';
    const action = row.querySelector('.row-action');
    action.className = 'button row-action' + (host.pending ? ' pending' : host.active ? ' monitoring' : '');
    action.disabled = host.pending;
    action.setAttribute('aria-busy', String(host.pending));
    const label = host.pending ? (host.active ? '暂停中…' : '启动中…') : (host.active ? '暂停监控' : host.error ? '重试监控' : '开始监控');
    if (action.dataset.label !== label) {
      action.innerHTML = (host.pending ? '<span class="spinner" aria-hidden="true"></span>' : icon(host.active ? 'pause' : 'play')) + `<span>${label}</span>`;
      action.dataset.label = label;
    }
    const room = row.querySelector('.open-room');
    room.disabled = !host.room;
    room.innerHTML = host.room ? '打开 ' + icon('external') : '待识别';
    row.querySelector('.remove-host').disabled = host.pending;
  }
  function makeRecordRow(record) {
    const row = document.createElement('tr');
    row.dataset.recordId = record.id;
    row.innerHTML = `<td><div class="host-name">${esc(record.name)}</div></td>
      <td class="checked">${stamp(record.started)}</td><td class="record-duration"></td><td class="record-files checked"></td>
      <td><span class="record-status"></span></td><td><button class="record-view" aria-haspopup="dialog" aria-label="查看 ${esc(record.name)} ${stamp(record.started)} 的直播数据">查看</button></td>
      <td><div class="record-actions"><button class="button segments">查看分段</button><button class="record-delete" aria-label="将 ${esc(record.name)} 的本场录像移入废纸篓">删除</button></div></td>`;
    return row;
  }
  function updateRecordRow(record, row) {
    row.querySelector('.record-view').onclick = () => openRecordData(record);
    row.querySelector('.segments').onclick = () => openSegments(record);
    row.querySelector('.record-delete').onclick = () => confirmDeleteRecord(record);
    const active = record.runningSince !== null;
    row.querySelector('.record-duration').textContent = duration(secondsFor(record));
    row.querySelector('.record-files').textContent = filesFor(record);
    const status = row.querySelector('.record-status');
    status.className = 'record-status' + (active ? ' live-record' : record.interrupted ? ' interrupted' : '');
    status.textContent = record.status;
    const remove = row.querySelector('.record-delete');
    remove.disabled = active || Boolean(record.blocked) || pending.has(record.id);
    remove.title = record.blocked || (active ? '录制结束后可移入废纸篓' : '移入系统废纸篓');
  }
  function renderRows(items, body, nodes, create, update) {
    const visible = new Set(items.map(item => item.id));
    for (const [id, node] of nodes) {
      if (!visible.has(id)) { node.remove(); nodes.delete(id); }
    }
    items.forEach((item, index) => {
      let node = nodes.get(item.id);
      if (!node) { node = create(item); nodes.set(item.id, node); }
      if (body.children[index] !== node) body.insertBefore(node, body.children[index] || null);
      update(item, node);
    });
  }
  function alignTabIndicator() {
    const selected = $('tab-' + tab);
    tabStrip.style.setProperty('--tab-offset', selected.offsetLeft + 'px');
    tabStrip.style.setProperty('--tab-width', selected.offsetWidth / 80);
  }
  function render() {
    const isHosts = tab === 'hosts';
    for (const view of ['hosts', 'recordings']) {
      const button = $('tab-' + view);
      const selected = view === tab;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    $('live-panel').setAttribute('aria-labelledby', 'tab-' + tab);
    $('table-scroll').setAttribute('aria-label', isHosts ? '主播监控列表' : '录像列表');
    $('hosts-badge').textContent = snapshot.hosts;
    $('records-badge').textContent = snapshot.recordings;
    $('monitor-count').innerHTML = snapshot.monitoring + '<small>位</small>';
    $('recording-count').innerHTML = snapshot.recording + '<small>场</small>';
    $('recording-count').parentElement.dataset.recording = String(snapshot.recording > 0);
    $('disk-space').innerHTML = snapshot.free == null ? '—' : snapshot.free.toFixed(1) + '<small>GB</small>';
    $('storage-warning').hidden = !snapshot.diskWarning;
    if ($('filter').dataset.kind !== tab) {
      $('filter').replaceChildren(...(isHosts ? hostFilters : dateFilters).map(([value, label]) => new Option(label, value)));
      $('filter').value = views[tab].filter;
      $('filter').dataset.kind = tab;
      $('filter').setAttribute('aria-label', isHosts ? '筛选直播状态' : '筛选录像日期');
    }
    LuluSelect.syncAll();
    $('search').placeholder = isHosts ? '搜索主播名称或链接' : '搜索录像中的主播';
    $('search').setAttribute('aria-label', $('search').placeholder);
    $('pause-all').hidden = !isHosts || (!pausableCount() && !pending.has('pause_all'));
    $('pause-all').disabled = pending.has('pause_all');
    $('add-host').hidden = !isHosts;
    const text = $('search').value.trim().toLocaleLowerCase();
    const filter = $('filter').value;
    const visible = shown[tab];
    const total = totals[tab];
    $('host-table').hidden = !isHosts;
    $('record-table').hidden = isHosts;
    if (isHosts) renderRows(visible, $('host-rows'), hostNodes, makeHostRow, updateHostRow);
    else renderRows(visible, $('record-rows'), recordNodes, makeRecordRow, updateRecordRow);
    const empty = $('empty-state');
    empty.hidden = visible.length > 0 || loading;
    const filtered = Boolean(text || filter !== 'all');
    empty.querySelector('h2').textContent = filtered ? '没有找到匹配内容' : isHosts ? '还没有添加主播' : '还没有录像';
    empty.querySelector('p').textContent = filtered ? '试试其他关键词，或清除当前筛选。'
      : isHosts ? '添加抖音主页或直播间链接后，可开启自动监控。' : '开启主播监控后，开播的录像会显示在这里。';
    $('empty-add').hidden = !isHosts || filtered;
    $('clear-filters').hidden = !filtered;
    const countLabel = `${filtered ? '找到' : '共'} ${total} ${isHosts ? '位主播' : '场录像'}`;
    if ($('list-count').textContent !== countLabel) $('list-count').textContent = countLabel;
    $('pager').hidden = pages === 1;
    $('footer-hint').hidden = pages > 1;
    if (document.activeElement !== $('page-jump')) $('page-jump').value = page;
    $('page-number').textContent = '/ ' + pages;
    $('prev-page').disabled = page === 1;
    $('next-page').disabled = page === pages;
    alignTabIndicator();
    renderComments();
  }
  function renderComments() {
    const button = $('comments-toggle');
    button.setAttribute('aria-checked', String(commentsPending ? !snapshot.comments : Boolean(snapshot.comments)));
    button.setAttribute('aria-busy', String(commentsPending));
    button.setAttribute('aria-disabled', String(commentsPending));
    button.disabled = !api || commentsPending;
    button.title = commentsPending ? '正在保存设置…' : '从下一场录制起生效';
  }
  function resetScroll() {
    $('table-scroll').scrollTo(0, 0);
  }
  function setTab(value) {
    if (value === tab) return;
    const scroll = $('table-scroll');
    views[tab] = { query: $('search').value, filter: $('filter').value, page, scroll: scroll.scrollTop, left: scroll.scrollLeft };
    tab = value;
    const view = views[tab];
    page = view.page;
    pages = totals[tab] ? Math.ceil(totals[tab] / 20) : 1;
    $('search').value = view.query;
    closePopover();
    LuluSelect.close({ immediate: true });
    render();
    scroll.scrollTo(view.left, view.scroll);
    restoreScroll = view;
    requestView();
    listTransition?.cancel();
    if (!reduced()) {
      listTransition = $('list-area').animate(
        [{ opacity: .68, transform: 'translateY(3px)' }, { opacity: 1, transform: 'none' }],
        { duration: 190, easing: 'cubic-bezier(.22, 1, .36, 1)' }
      );
    }
  }

  function closePopover(restoreFocus = false) {
    if (popoverOwner) popoverOwner.setAttribute('aria-expanded', 'false');
    $('popover').hidden = true;
    if (restoreFocus) popoverOwner?.focus();
    popoverOwner = null;
  }
  function openPopover(owner, html, role = 'menu') {
    const same = popoverOwner === owner && !$('popover').hidden;
    closePopover(); if (same) return;
    popoverOwner = owner; owner.setAttribute('aria-expanded', 'true');
    const popover = $('popover'); popover.innerHTML = html; popover.setAttribute('role', role);
    popover.setAttribute('aria-label', owner === $('storage-button') ? '录像保存位置' : '更多设置');
    popover.hidden = false;
    const box = owner.getBoundingClientRect();
    popover.style.left = `${Math.max(12, Math.min(window.innerWidth - popover.offsetWidth - 12, box.right - popover.offsetWidth))}px`;
    popover.style.top = `${Math.max(8, Math.min(box.bottom + 8, innerHeight - popover.offsetHeight - 8))}px`;
    popover.querySelector('button')?.focus();
  }
  function openModal(title, eyebrow, html, variant = '') {
    LuluSelect.close({ immediate: true });
    const origin = $('modal-layer').hidden ? document.activeElement : lastFocus;
    if (!$('modal-layer').hidden) closeModal(false);
    lastFocus = origin;
    $('modal-layer').firstElementChild.classList.toggle('live-data', variant === 'live-data');
    $('modal-title').textContent = title; $('modal-eyebrow').textContent = eyebrow;
    $('modal-content').innerHTML = html; $('modal-layer').hidden = false;
    document.querySelector('.app-window').inert = true;
    $('modal-close').focus();
  }
  function closeModal(restoreFocus = true) {
    if ($('modal-layer').hidden) return;
    LuluSelect.close({ immediate: true });
    openRecord = null;
    openSegmentRecord = null;
    detailGeneration++;
    $('modal-layer').hidden = true;
    document.querySelector('.app-window').inert = false;
    $('modal-content').replaceChildren();
    if (restoreFocus && lastFocus?.isConnected) lastFocus.focus();
  }
  function showRecord(record) {
    const active = record.runningSince !== null;
    const status = record.status;
    const samples = record.samples;
    const count = samples.length;
    openModal('直播数据', record.name, `
      <div class="data-context">
        <span>${stamp(record.started)} 开始</span>
        <span class="data-state ${active ? 'running' : record.interrupted ? 'interrupted' : ''}">${status}</span>
      </div>
      <div class="data-tabs" role="tablist" aria-label="直播数据分类">
        <button id="show-trend" role="tab" aria-selected="true" aria-controls="trend-panel">数据走势</button>
        <button id="show-comments" role="tab" aria-selected="false" aria-controls="comments-panel" tabindex="-1">弹幕记录</button>
      </div>
      <div class="data-body">
        <section class="data-panel" id="trend-panel" role="tabpanel" aria-labelledby="show-trend">
          <dl class="data-summary">
            <div><dt>已录时长</dt><dd id="data-duration">${duration(secondsFor(record))}</dd></div>
            <div><dt>采样最高人数</dt><dd id="sample-peak">${formatNumber(record.peak)}</dd></div>
            <div><dt>最近在线人数</dt><dd id="sample-latest">${formatNumber(record.latest)}</dd></div>
            <div><dt>采集记录</dt><dd><span id="sample-count">${record.sampleCount}</span><small>次</small></dd></div>
          </dl>
          <div class="data-card">
            <div class="data-card-head"><h3 id="data-metric-title"></h3>
              <div class="metric-picks" role="group" aria-label="走势指标">
                ${Object.entries(liveMetrics).map(([key, [label]]) => `<button type="button" data-metric="${key}" aria-pressed="${key === 'viewers'}">${label}</button>`).join('')}
              </div>
            </div>
            <p class="data-description" id="data-definition"></p>
            <div class="data-graph">
              <div class="data-axis" aria-hidden="true"></div>
              <div class="sample-plot">
                <svg class="sample-chart" viewBox="0 0 500 142" preserveAspectRatio="none" tabindex="0" role="slider" aria-valuemin="1" aria-valuemax="${count}">
                  <path d="M0 7H500M0 49H500M0 91H500M0 133H500" stroke="#e9e0d0" vector-effect="non-scaling-stroke"/>
                  <path id="data-area" fill="#e6eef0" stroke="none"/>
                  <path id="data-line" fill="none" stroke="currentColor"/>
                  <line id="data-crosshair" y1="7" y2="133" stroke="#8ba5b0" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>
                  <circle id="data-marker" r="4" fill="currentColor" stroke="#fffcf6" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
                </svg>
                <div id="sample-value" class="sample-value" aria-hidden="true" hidden><time></time><strong></strong></div>
              </div>
              <div class="data-x-axis" aria-hidden="true"></div>
            </div>
            <div class="data-chart-note"><span>每 5 分钟采样</span><span id="data-point-label"></span></div>
          </div>
        </section>
        <section class="data-panel" id="comments-panel" role="tabpanel" aria-labelledby="show-comments" hidden>
          <div class="data-card" id="comment-content" hidden>
            <div class="data-card-head"><h3>本场弹幕</h3><span class="comment-hint" id="comment-count"></span></div>
            <div class="comment-tools">
              <label class="search-box">${icon('search')}<input id="comment-search" type="search" placeholder="搜索昵称或弹幕内容" aria-label="搜索昵称或弹幕内容" maxlength="300"></label>
              <div class="comment-actions">
                <button class="button" id="copy-comments" disabled>复制选中</button>
                <select id="comment-export" aria-label="导出弹幕"><option value="" disabled selected>导出弹幕</option><option value="txt">文字文件 · TXT</option><option value="csv">表格文件 · CSV</option></select>
              </div>
            </div>
            <table class="comment-table" hidden>
              <thead><tr><th scope="col" class="comment-choice" aria-label="选择"></th><th scope="col" class="comment-time">时间</th><th scope="col" class="comment-name">昵称</th><th scope="col">内容</th></tr></thead>
              <tbody id="sample-comments"></tbody>
            </table>
            <p class="comment-hint" id="comment-results" role="status" hidden>没有匹配的弹幕，试试其他关键词。</p>
            <nav id="comment-pages" class="comment-pages" aria-label="弹幕分页" hidden>
              <button class="icon-button" id="comment-prev" aria-label="上一页">${icon('left')}</button>
              <span id="comment-page"></span>
              <button class="icon-button" id="comment-next" aria-label="下一页">${icon('right')}</button>
            </nav>
          </div>
          <div class="data-card comment-empty" id="comment-empty" role="status">${icon('wave')}
            <h3>正在读取弹幕</h3><p></p></div>
        </section>
      </div>
      <footer class="data-footer">
        <small id="data-feedback" role="status">${esc(record.dataError || '')}</small>
        <div class="data-footer-actions">
          <button class="button" id="data-folder">${icon('folder')} 打开数据文件夹</button>
          <button class="button primary" id="record-done">完成</button>
        </div>
      </footer>`, 'live-data');
    openRecord = record;

    const tabs = [$('show-trend'), $('show-comments')];
    const body = document.querySelector('.data-body');
    const panels = [$('trend-panel'), $('comments-panel')];
    const scrollPositions = [0, 0];
    let current = 0;
    let panelMotion;
    const show = (index) => {
      if (index === current) return;
      scrollPositions[current] = body.scrollTop;
      current = index;
      tabs.forEach((button, i) => {
        button.setAttribute('aria-selected', String(i === index));
        button.tabIndex = i === index ? 0 : -1;
        panels[i].hidden = i !== index;
      });
      $('data-folder').hidden = index === 1;
      $('data-feedback').textContent = index === 0 ? record.dataError : '';
      if (index === 1) refreshComments();
      body.scrollTop = scrollPositions[index];
      panelMotion?.cancel();
      if (!reduced()) panelMotion = panels[index].animate(
        [{ opacity: .5, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }],
        { duration: 180, easing: 'cubic-bezier(.22, 1, .36, 1)' }
      );
    };
    tabs.forEach((button, index) => {
      button.onclick = () => show(index);
      button.onkeydown = (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index;
        show(next);
        tabs[next].focus();
      };
    });
    const updateChart = setupLiveChart(samples, record.started);
    const refreshComments = setupCommentTools(record);
    let refreshing = false, refreshedAt = 0;
    record.refresh = async row => {
      const now = performance.now();
      if (refreshing || (!record.needsFinalData && !row.clockRunning)) return;
      if (row.clockRunning && now-refreshedAt<5000) return;
      refreshing = true;
      refreshedAt = now;
      try {
        const data = await api.call('data',{id:record.id});
        if (openRecord!==record) return;
        record.needsFinalData = data.clockRunning;
        record.dataError = data.dataError;
        $('sample-peak').textContent = formatNumber(data.peak);
        $('sample-latest').textContent = formatNumber(data.latest);
        $('sample-count').textContent = data.sampleCount;
        $('data-folder').disabled = !data.hasFolder;
        if (current === 0) $('data-feedback').textContent = data.dataError || '';
        if (current === 1) refreshComments();
        updateChart(data.samples);
      } catch (error) {
        if (openRecord===record) $('data-feedback').textContent = error.message;
      } finally {
        refreshing = false;
      }
    };
    record.needsFinalData = record.clockRunning;
    $('data-folder').disabled = !record.hasFolder;
    $('data-folder').onclick = () => perform('open_data_folder', {id:record.id});
    $('record-done').onclick = closeModal;
  }

  function setupLiveChart(initialSamples, started) {
    const chart = document.querySelector('.sample-chart');
    const callout = $('sample-value');
    const timeLabel = callout.querySelector('time');
    const valueLabel = callout.querySelector('strong');
    const buttons = [...document.querySelectorAll('[data-metric]')];
    let samples = [], count = 0, metric = 'viewers';
    const sampleTime = index => duration(Math.max(0, Math.round(samples[Math.round(index)].at - started / 1000)));
    let points, values, metricName, unit, activePoint = -1;
    const selectPoint = index => {
      if (!count) return;
      activePoint = Math.max(0, Math.min(count - 1, index));
      const [x, y] = points[activePoint];
      $('data-marker').setAttribute('cx', x);
      $('data-marker').setAttribute('cy', y);
      $('data-marker').hidden = values[activePoint] == null;
      $('data-crosshair').setAttribute('x1', x);
      $('data-crosshair').setAttribute('x2', x);
      const time = sampleTime(activePoint);
      const value = values[activePoint] == null ? '未获取' : formatNumber(values[activePoint]) + ' ' + unit;
      const label = time + ' · ' + value;
      timeLabel.textContent = time;
      valueLabel.textContent = metricName + ' ' + value;
      callout.style.setProperty('--sample-x', x / 5 + '%');
      callout.style.setProperty('--sample-y', y / 142 * 100 + '%');
      callout.toggleAttribute('data-below', y < 59);
      $('data-point-label').textContent = label;
      chart.setAttribute('aria-valuenow', activePoint + 1);
      chart.setAttribute('aria-valuetext', label);
    };
    const showMetric = key => {
      metric = key;
      const [name, description, field, metricUnit] = liveMetrics[key];
      metricName = name;
      unit = metricUnit;
      values = samples.map(sample => sample[field]);
      const step = Math.max(1, Math.ceil(values.reduce((max,value) => Math.max(max,value || 0),0) / 300) * 100);
      const maximum = step * 3;
      points = values.map((value, index) => [5 + index * 490 / Math.max(1, count - 1), 133 - value * 126 / maximum]);
      const commands = points.map((point,index) => values[index] == null ? '' :
        (index === 0 || values[index - 1] == null ? 'M' : 'L') + point.join(',')).filter(Boolean);
      $('data-line').setAttribute('d', commands.join(' '));
      $('data-area').setAttribute('d', count > 1 && values.every(value => value != null)
        ? `M5,133 L${points.map(point => point.join(',')).join(' ')} L495,133 Z` : '');
      $('data-metric-title').textContent = name + '走势';
      $('data-definition').textContent = description;
      document.querySelector('.data-axis').innerHTML = [maximum, step * 2, step, 0].map(value => `<span>${formatNumber(value)}</span>`).join('');
      chart.setAttribute('aria-label', name + '采样点，可用左右方向键查看');
      buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.metric === key)));
      selectPoint(activePoint);
    };
    function updateSamples(next) {
      if (samples === next || (count && JSON.stringify(samples) === JSON.stringify(next))) return;
      const atEnd = activePoint === count - 1;
      const selectedAt = samples[activePoint]?.at;
      samples = next;
      count = samples.length;
      chart.closest('.data-graph').hidden = !count;
      buttons.forEach(button => { button.disabled = !count; });
      if (!count) {
        $('data-definition').textContent = '这场没有可用的采样。';
        return;
      }
      const samePoint = samples.findIndex(sample => sample.at === selectedAt);
      activePoint = atEnd || samePoint < 0 ? count - 1 : samePoint;
      chart.setAttribute('aria-valuemax',count);
      document.querySelector('.data-x-axis').innerHTML = [...new Set([0, (count - 1) / 2, count - 1])].map(index => `<span>${sampleTime(index)}</span>`).join('');
      showMetric(metric);
    }
    buttons.forEach(button => { button.onclick = () => showMetric(button.dataset.metric); });
    const pointAt = event => {
      const bounds = chart.getBoundingClientRect();
      const index = Math.round(((event.clientX - bounds.left) / bounds.width * 500 - 5) / 490 * (count - 1));
      if (index !== activePoint) selectPoint(index);
      callout.hidden = false;
    };
    chart.addEventListener('pointermove', event => { if (event.pointerType === 'mouse') pointAt(event); });
    chart.addEventListener('pointerleave', () => { if (!chart.matches(':focus-visible')) callout.hidden = true; });
    chart.addEventListener('click', pointAt);
    chart.addEventListener('focus', () => { if (chart.matches(':focus-visible')) callout.hidden = false; });
    chart.addEventListener('blur', () => { callout.hidden = true; });
    chart.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      callout.hidden = false;
      if (event.key === 'Home') selectPoint(0);
      else if (event.key === 'End') selectPoint(count - 1);
      else selectPoint(activePoint + (['ArrowRight', 'ArrowUp'].includes(event.key) ? 1 : -1));
    });
    updateSamples(initialSamples);
    return updateSamples;
  }

  $('tab-hosts').onclick = () => setTab('hosts'); $('tab-recordings').onclick = () => setTab('recordings');
  tabStrip.addEventListener('keydown', (event) => {
    if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
      event.preventDefault(); setTab(event.key === 'Home' ? 'hosts' : event.key === 'End' ? 'recordings' : tab === 'hosts' ? 'recordings' : 'hosts');
      $(tab === 'hosts' ? 'tab-hosts' : 'tab-recordings').focus();
    }
  });
  $('search').oninput = event => { if (!event.isComposing) resetPage(220); };
  $('search').oncompositionend = () => resetPage(220);
  $('filter').onchange = () => resetPage();
  $('clear-filters').onclick = () => { resetFilters(); $('search').focus(); };
  $('pager').onsubmit = event => {
    event.preventDefault();
    const input = $('page-jump');
    const requested = /^\d+$/.test(input.value.trim()) ? Number(input.value) : 0;
    if (requested < 1 || requested > pages) {
      toast('请输入 1 到 ' + pages + ' 之间的页码', true);
      input.select();
      return;
    }
    setPage(requested);
    input.blur();
  };
  $('prev-page').onclick = () => setPage(page - 1);
  $('next-page').onclick = () => setPage(page + 1);
  $('comments-toggle').onclick = changeComments;
  $('storage-button').onclick = openStorage;
  $('add-host').onclick = showAdd; $('empty-add').onclick = showAdd;
  $('pause-all').onclick = confirmPauseAll;
  $('modal-close').onclick = () => closeModal();
  $('modal-layer').onclick = (event) => { if (event.target === $('modal-layer')) closeModal(); };
  document.addEventListener('click', (event) => { if (popoverOwner && !$('popover').contains(event.target) && !popoverOwner.contains(event.target)) closePopover(); });
  document.addEventListener('keydown', (event) => {
    if (event.defaultPrevented) return;
    if (event.key === 'Escape') { if (!$('modal-layer').hidden) closeModal(); else closePopover(true); }
    if (!$('popover').hidden && ['ArrowUp','ArrowDown'].includes(event.key)) {
      event.preventDefault(); const items = [...$('popover').querySelectorAll('button')];
      const index = items.indexOf(document.activeElement); items[(index + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus();
    }
    if (event.key === 'Tab' && !$('modal-layer').hidden) {
      const focusable = [...$('modal-layer').querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),[tabindex="0"]')].filter((node) => node.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  window.addEventListener('resize', () => closePopover());
  document.addEventListener('pointerdown', () => document.body.classList.remove('keyboard-mode'), true);

  const liveMetrics = {
    viewers: ['在线人数', '平台返回的当前在线人数，按实际采样时间展示。', 'viewer_count', '人'],
    likes: ['点赞', '平台返回的本场累计点赞次数。', 'like_count', '次'],
    cumulative: ['累计观看', '平台返回的累计观看次数。', 'cumulative_view_count', '次'],
  };

  function formatNumber(value) {
    return value == null ? '—' : value.toLocaleString('zh-CN');
  }

  function updateView(state) {
    snapshot = state;
    loading = false;
    if (popoverOwner===$('storage-button')) {
      const path=$('popover').querySelector('.path');
      if (path) path.textContent=state.folder || '正在读取';
    }
    if (state.tab === tab) {
      const now = performance.now();
      shown[tab] = state.rows.map(row => {
        const running = row.clockRunning ? now : null;
        return tab === 'hosts' ? {...row, pending:pending.has(row.id) || pending.has('pause_all'),
          record:running === null ? null : {seconds:row.seconds,runningSince:running}}
          : {...row,runningSince:running};
      });
      totals[tab] = state.total;
      page = state.page;
      pages = state.pages;
    }
    render();
    if (restoreScroll && state.tab === tab) {
      $('table-scroll').scrollTo(restoreScroll.left, restoreScroll.scroll);
      restoreScroll = null;
    }
    if (openRecord) {
      const row = state.tab === 'recordings' && state.rows.find(item => item.id === openRecord.id);
      if (row) {
        openRecord.seconds = row.seconds;
        openRecord.runningSince = row.clockRunning ? performance.now() : null;
        const state = document.querySelector('.data-state');
        state.textContent = row.status;
        state.classList.toggle('running', row.clockRunning);
        state.classList.toggle('interrupted', row.interrupted);
        openRecord.refresh(row);
      }
    }
    if (openSegmentRecord && state.tab==='recordings') {
      const row=state.rows.find(item=>item.id===openSegmentRecord.id);
      if (row) openSegmentRecord.refresh(row);
    }
  }

  function requestView(delay = 0) {
    clearTimeout(queryTimer);
    if (!api) return;
    const query = $('search').value;
    const filter = $('filter').value;
    queryTimer = setTimeout(() => {
      api.call('view', {tab, query, filter, page}).catch(error => toast(error.message,true));
    }, delay);
  }

  function setPage(next, delay = 0) {
    page = next;
    render();
    resetScroll();
    requestView(delay);
  }

  function resetPage(delay = 0) {
    setPage(1, delay);
  }

  function resetFilters() {
    $('search').value = '';
    $('filter').value = 'all';
    resetPage();
  }

  async function perform(action, args = {}) {
    if (!api) return null;
    try {
      return await api.call(action, args);
    } catch (error) {
      toast(error.message, true);
      return null;
    }
  }

  function renderPending() {
    if (tab === 'hosts') {
      shown.hosts.forEach(host => { host.pending = pending.has(host.id) || pending.has('pause_all'); });
    }
    render();
  }

  function pausableCount() {
    const waiting=[...pending].filter(([id,action])=>
      action==='start' && !shown.hosts.some(host=>host.id===id&&host.active)).length;
    return snapshot.monitoring + waiting;
  }

  async function runMutation(action, id, args = {}) {
    if (!api || pending.has(id)) return false;
    pending.set(id,action);
    renderPending();
    try {
      const result = await api.call(action, args);
      if (result?.message) toast(result.message);
      requestView();
      return true;
    } catch (error) {
      if (action!=='start' || !pending.has('pause_all')) toast(error.message, true);
      return false;
    } finally {
      pending.delete(id);
      renderPending();
    }
  }

  function toggleHost(host) {
    if (host.pending) return;
    return runMutation(host.active ? 'pause' : 'start', host.id, {id:host.id});
  }

  async function changeComments() {
    if (!api || commentsPending) return;
    commentsPending = true;
    renderComments();
    try {
      const result = await api.call('comments_setting', {enabled:!snapshot.comments});
      snapshot.comments = result.enabled;
      toast(`已${result.enabled ? '开启' : '关闭'}弹幕保存，从下一场录制起生效`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      commentsPending = false;
      renderComments();
    }
  }

  function openStorage() {
    openPopover($('storage-button'), `<h3>录像保存位置</h3><div class="path">${esc(snapshot.folder || '正在读取')}</div><button class="menu-item" id="open-folder">${icon('folder')}在访达中打开</button><button class="menu-item" id="change-folder">${icon('settings')}更改保存位置</button>`, 'dialog');
    if ($('popover').hidden) return;
    $('open-folder').onclick = () => { closePopover(true); perform('open_folder'); };
    $('change-folder').onclick = () => {
      closePopover(true);
      openModal('保存位置', '录像留在本机', `<p class="modal-description">新录像会保存在你选择的文件夹。已有录像保留在原来的位置。</p><label class="field-label" for="folder-preview">保存到</label><input class="input" id="folder-preview" readonly value="${esc(snapshot.folder || '')}"><div class="modal-actions"><button class="button" id="folder-cancel">取消</button><button class="button primary" id="folder-select">选择文件夹</button></div>`);
      $('folder-cancel').onclick = () => closeModal();
      $('folder-select').onclick = async () => {
        const token = detailGeneration;
        const result = await perform('directory');
        if (result?.changed && token === detailGeneration) closeModal();
        requestView();
      };
    };
  }

  function showConnection() {
    const busy = snapshot.monitoring || pending.size;
    openModal('抖音连接', '访问受限时使用', `
      <div class="connection-state${snapshot.connected ? ' connected' : ''}">
        <div class="connection-symbol">${icon('shield')}</div>
        <div><h3>${snapshot.connected ? esc(snapshot.loginLabel) : '游客访问'}</h3><p>遇到登录要求时，可以在浏览器中完成登录。</p></div>
      </div>
      ${busy ? '<p class="field-note">更改连接前，请先暂停全部监控。</p>' : ''}
      <div class="modal-actions">
        ${snapshot.connected ? `<button class="button" id="disconnect" ${busy ? 'disabled' : ''}>移除登录信息</button>` : ''}
        <button class="button" id="connection-done">完成</button>
        <button class="button primary" id="browser-login" ${busy ? 'disabled' : ''}>${icon('browser')}浏览器登录</button>
      </div>`);
    $('browser-login').onclick = () => { closeModal(); perform('login'); };
    if (snapshot.connected) $('disconnect').onclick = () => { closeModal(); perform('clear_login'); };
    $('connection-done').onclick = () => closeModal();
  }

  function showAdd() {
    openModal('添加主播', '主播监控', `
      <p class="modal-description">粘贴抖音主页、直播间链接或分享文字。</p>
      <form id="add-form">
        <label class="field-label" for="host-link">抖音链接</label>
        <input class="input" id="host-link" placeholder="https://v.douyin.com/…" autocomplete="off" maxlength="4096" aria-describedby="add-error">
        <p class="error-text" id="add-error" role="alert"></p>
        <div class="modal-actions"><button type="button" class="button" id="add-cancel">取消</button><button type="submit" class="button primary">添加主播</button></div>
      </form>`);
    const form = $('add-form');
    const input = $('host-link');
    input.focus();
    $('add-cancel').onclick = () => closeModal();
    form.onsubmit = async event => {
      event.preventDefault();
      const submit = form.querySelector('[type=submit]');
      if (!api || submit.disabled) return;
      submit.disabled = true;
      try {
        const result = await api.call('add', {source:input.value});
        if (!form.isConnected) return;
        if (result?.alreadyPresent) {
          $('add-error').textContent = '这位主播已在列表里。';
          return;
        }
        closeModal();
        resetFilters();
        toast('已添加主播，可以开启监控');
      } catch (error) {
        if (form.isConnected) {
          $('add-error').textContent = error.message;
          input.focus();
        }
      } finally {
        submit.disabled = false;
      }
    };
  }

  function confirm(title, eyebrow, description, label, action) {
    openModal(title, eyebrow, `<p class="modal-description">${esc(description)}</p>
      <div class="modal-actions"><button class="button" id="action-cancel">取消</button><button class="button primary" id="action-confirm">${esc(label)}</button></div>`);
    $('action-cancel').onclick = () => closeModal();
    $('action-confirm').onclick = () => { closeModal(); action(); };
    $('action-cancel').focus();
  }

  function confirmRemove(host) {
    if (host.pending) return;
    confirm('移除这位主播？', '主播监控', host.name + ' 将从监控列表中移除。本场录制会停止，已保存的录像保留。',
      '移除主播', () => runMutation('remove',host.id,{id:host.id}));
  }

  function confirmPauseAll() {
    const count=pausableCount();
    if (!count || pending.has('pause_all')) return;
    confirm('暂停全部监控？', '主播监控', `将暂停 ${count} 位主播的监控。正在录制的场次会保存已录内容。`,
      '暂停全部', () => runMutation('pause_all','pause_all'));
  }

  function confirmDeleteRecord(record) {
    if (record.blocked || pending.has(record.id)) return;
    confirm('移入废纸篓？', '删除本场录像',
      `${record.name} 在 ${stamp(record.started)} 的录像、直播数据和弹幕记录将一同移入系统废纸篓。`,
      '移入废纸篓', () => runMutation('delete',record.id,{id:record.id}));
  }

  async function openRecordData(record) {
    const token = ++detailOpening;
    const data = await perform('data', {id:record.id});
    if (data && token === detailOpening && tab === 'recordings') {
      showRecord({...record,...data,runningSince:data.clockRunning ? performance.now() : null});
    }
  }

  async function openSegments(record) {
    const token = ++detailOpening;
    const data = await perform('detail', {id:record.id});
    if (data && token === detailOpening && tab === 'recordings') showSegments({...record,...data});
  }

  function showSegments(record, selected = null) {
    openModal('录像分段', record.name, `
      <p class="modal-description" id="segment-summary"></p>
      <div class="segment-list" role="group" aria-label="选择已保存的分段"></div>
      <div class="modal-actions">
        <button class="button" id="segment-folder">${icon('folder')}打开文件夹</button>
        <button class="button" id="full-export">导出整场 MP4</button>
        <button class="button" id="segment-export">导出分段</button>
        <button class="button primary" id="segment-extract">提取文案</button>
      </div>`);
    openSegmentRecord = record;
    const list=document.querySelector('.segment-list');
    const selection = () => Number(document.querySelector('[name=segment]:checked')?.value ?? -1);
    let lastSegments='', refreshing=false, needsFinal=record.runningSince!==null, refreshedAt=0;
    function paint() {
      const segments=record.segments;
      const previous=selection();
      const preferred=previous>=0?previous:selected;
      const chosen=segments[preferred]?.ready?preferred:segments.findIndex(segment=>segment.ready);
      const encoded=JSON.stringify(segments);
      if (encoded!==lastSegments) {
        const focused=list.contains(document.activeElement) ? document.activeElement.value : null;
        lastSegments=encoded;
        list.innerHTML=segments.map((segment,index)=>`<label class="segment-item">
          <input type="radio" name="segment" value="${index}" ${segment.ready?'':'disabled'} ${index===chosen?'checked':''}>
          <strong>分段 ${index+1} <small>${segment.ready?'已保存':esc(segment.state)}</small></strong>
          <span>${duration(segment.seconds)}</span></label>`).join('');
        if(focused!==null) list.querySelector(`[value="${focused}"]`)?.focus({preventScroll:true});
      }
      $('segment-summary').textContent=`${stamp(record.started)} · ${duration(secondsFor(record))} · ${segments.length} 段`;
      const active=record.runningSince!==null;
      $('full-export').disabled=active||chosen<0;
      $('full-export').title=active?'录制结束后可导出整场':'';
      $('segment-export').disabled=chosen<0;
      $('segment-extract').disabled=chosen<0;
    }
    record.refresh=async row=>{
      record.seconds=row.seconds;
      record.runningSince=row.clockRunning?performance.now():null;
      paint();
      const now=performance.now();
      if (refreshing||(!row.clockRunning&&!needsFinal)||(row.clockRunning&&now-refreshedAt<5000)) return;
      refreshing=true;
      refreshedAt=now;
      try {
        const data=await api.call('detail',{id:record.id});
        if(openSegmentRecord!==record) return;
        record.segments=data.segments;
        needsFinal=row.clockRunning;
        paint();
      } catch(error) {
        if(openSegmentRecord===record) toast(error.message,true);
      } finally {
        refreshing=false;
      }
    };
    paint();
    $('segment-folder').onclick = () => perform('open_record_folder',{id:record.id});
    $('full-export').onclick = () => showExport(record, null);
    $('segment-export').onclick = () => showExport(record, selection());
    $('segment-extract').onclick = async () => {
      const token = detailGeneration;
      if (await perform('transcribe',{id:record.id,segment:selection()}) && token===detailGeneration) closeModal();
    };
  }

  function showExport(record, segment) {
    const full = segment === null;
    const label = full ? '导出整场 MP4' : '导出分段';
    openModal(label, record.name, `<p class="modal-description">${full ? '将本场已保存的分段合并为一个 MP4 文件。' : '选择分段 ' + (segment + 1) + ' 的导出格式。'}</p>
      ${full ? '' : '<div class="export-options" role="group" aria-label="导出格式"><label><input type="radio" name="format" value="mp4" checked> MP4 视频</label><label><input type="radio" name="format" value="mp3"> MP3 音频</label></div>'}
      <div class="modal-actions"><button class="button" id="export-back">返回分段</button><button class="button primary" id="export-start">选择保存位置</button></div>`);
    $('export-back').onclick = () => showSegments(record, segment);
    $('export-start').onclick = async () => {
      const trigger = $('export-start');
      trigger.disabled = true;
      const format = full ? 'mp4' : document.querySelector('[name=format]:checked').value;
      const result = await perform('export_recording',{id:record.id,segment,format});
      if (trigger.isConnected && result?.started) closeModal();
      trigger.disabled = false;
    };
  }

  function setupCommentTools(record) {
    const list = $('sample-comments');
    const table = list.closest('table');
    const search = $('comment-search');
    const copy = $('copy-comments');
    const exportSelect = $('comment-export');
    const selection = new Map();
    let commentPage = 1, request = 0, timer, busy = false, rerun = false;
    const generation = detailGeneration;
    LuluSelect.enhance($('comments-panel'));

    function updateCount() {
      copy.disabled = !selection.size;
      copy.textContent = selection.size ? `复制选中 ${selection.size} 条` : '复制选中';
    }

    async function refresh() {
      if (!list.isConnected || detailGeneration !== generation) return;
      if (busy) { rerun = true; return; }
      const expected = request;
      const query = search.value.trim();
      busy = true;
      try {
        const result = await api.call('comments',{id:record.id,query,page:commentPage});
        if (!list.isConnected || expected !== request) return;
        const {messages,total,matching,page,pages} = result;
        for (const message of messages) {
          if (selection.has(message.key)) selection.set(message.key,message.copy);
        }
        commentPage = page;
        const empty = $('comment-empty');
        const showTools = Boolean(total) || Boolean(query) || selection.size>0;
        $('comment-content').hidden = !showTools;
        $('comment-results').hidden = Boolean(messages.length) || Boolean(result.readError);
        table.hidden = !messages.length;
        empty.hidden = showTools;
        empty.querySelector('h3').textContent = result.readError ? '暂时无法读取弹幕'
          : '本场暂无弹幕';
        empty.querySelector('p').textContent = result.readError ? '请检查保存位置后重试。'
          : result.state === 'disabled' ? '录制开始前开启「保存弹幕」，即可在这里查看。'
          : '弹幕保存后会出现在这里。';
        $('comment-count').textContent = total == null ? '' : query
          ? `匹配 ${matching} 条 · 全场 ${total} 条` : `已保存 ${total} 条`;
        const focusedKey = list.contains(document.activeElement) ? document.activeElement.dataset.key : null;
        list.innerHTML = messages.map((message,index) => `<tr class="comment-row">
          <td class="comment-choice"><input type="checkbox" data-key="${esc(message.key)}" aria-label="选中第 ${index + 1} 条弹幕" ${selection.has(message.key) ? 'checked' : ''}></td>
          <td class="comment-time">${esc(message.time)}</td><td>${esc(message.nickname)}</td><td>${esc(message.text)}</td>
        </tr>`).join('');
        for (const [index,input] of [...list.querySelectorAll('input')].entries()) {
          input.onchange = () => {
            if (input.checked) selection.set(messages[index].key, messages[index].copy);
            else selection.delete(messages[index].key);
            updateCount();
          };
          if (focusedKey && input.dataset.key === focusedKey) input.focus({preventScroll:true});
        }
        $('comment-pages').hidden = pages < 2;
        $('comment-page').textContent = page + ' / ' + pages;
        $('comment-prev').disabled = page <= 1;
        $('comment-next').disabled = page >= pages;
        exportSelect.disabled = !total || result.readError;
        LuluSelect.syncAll();
      } catch (error) {
        if (list.isConnected) $('data-feedback').textContent = error.message;
      } finally {
        busy = false;
        if (rerun) { rerun = false; refresh(); }
      }
    }

    function searchChanged() {
      request++;
      commentPage = 1;
      clearTimeout(timer);
      timer = setTimeout(refresh, 200);
    }
    search.oninput = event => { if (!event.isComposing) searchChanged(); };
    search.oncompositionend = searchChanged;
    $('comment-prev').onclick = () => { request++; commentPage--; refresh(); };
    $('comment-next').onclick = () => { request++; commentPage++; refresh(); };
    copy.onclick = async () => {
      const result = await perform('copy',{text:[...selection.values()].join('\n')});
      if (result && list.isConnected) $('data-feedback').textContent = `已复制 ${selection.size} 条弹幕`;
    };
    exportSelect.onchange = async () => {
      const format = exportSelect.value;
      exportSelect.value = '';
      exportSelect.disabled = true;
      LuluSelect.syncAll();
      const result = await perform('comments_export',{id:record.id,format});
      if (list.isConnected) {
        if (result?.saved) $('data-feedback').textContent = '已导出本场弹幕';
        exportSelect.disabled = false;
        LuluSelect.syncAll();
      }
    };
    return refresh;
  }

  LuluSelect.enhance();
  render();
  $('cancel-exports').onclick = () => perform('cancel_exports');
  const tabResize = new ResizeObserver(alignTabIndicator);
  tabResize.observe($('tab-hosts'));
  tabResize.observe($('tab-recordings'));
  requestAnimationFrame(() => { tabStrip.dataset.ready = ''; });
  connectWorkspace((event, value) => {
    if (event === 'snapshot') updateView(value);
    if (event === 'notice') toast(value.message, value.error);
    if (event === 'directory_pending' && $('folder-select')) closeModal();
    if (event === 'storage' && popoverOwner!==$('storage-button')) openStorage();
    if (event === 'motion') document.body.classList.toggle('reduce-motion', value);
    if (event === 'exports') {
      $('export-status').hidden = !value.active;
      $('export-progress').textContent = value.message;
    }
  }).then(bridge => {
    api = bridge;
    bridge.ready();
    requestView();
  });

  setInterval(() => {
    if (openRecord) $('data-duration').textContent = duration(secondsFor(openRecord));
    for (const host of shown.hosts) {
      if (host.record) {
        const clock = hostNodes.get(host.id)?.querySelector('.progress strong');
        if (clock) clock.textContent = duration(secondsFor(host.record));
      }
    }
    for (const record of shown.recordings) {
      const row = recordNodes.get(record.id);
      if (row && record.runningSince !== null) row.querySelector('.record-duration').textContent = duration(secondsFor(record));
    }
  }, 1000);
})();

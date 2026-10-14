import { openMenu, closeMenu, observeSelects } from "./menus.js";
import { createEditor, renderMarkdownPreview } from "./editor.js";
import { icon } from "./icons.js";
import { showMarkdownGuide } from "./markdown_guide.js";
const $ = (id) => document.getElementById(id), esc = (x) => String(x ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const uid = () => crypto.randomUUID().replaceAll("-", "");
const state = { page: "library", kind: "documents", rows: [], folders: [], total: 0, next: null, doc: null, chat: null, messages: [], proposals: [], refs: [], queuedFollowup: null, nextRef: 1, batch: false, selected: /* @__PURE__ */ new Set(), dirty: false, draftDirty: false, ready: false, loading: false, selection: null };
let bridge, editor, saveTimer, draftTimer, toastTimer, searchTimer, querySerial = 0, chooseSerial = 0, saving = null, editingSequence = 0, draftSequence = 0, draftSaving = null;
const pending = /* @__PURE__ */ new Map(), watching = /* @__PURE__ */ new Map(), submitting = /* @__PURE__ */ new Set();
const mediaWatching = new Map(), mediaStates = new Map();
const audioOperations = new Set();
let skillMenuSerial = 0, skillMenuSource = null, skillEntries = null, skillRequest = null;
let choiceGeneration = 0, selectionTimer, selectionAnchor = null, batchBusy = false;
let selectionQuery = null;
let transition = Promise.resolve(), commands = Promise.resolve(), referenceGeneration = 0, addingReferences = false;
function serializeTransition(fn) {
  const task = transition.then(fn);
  transition = task.catch(() => {
  });
  return task;
}
function rpc(operation, arguments_ = {}) {
  return new Promise((resolve, reject) => {
    const id = uid();
    pending.set(id, { resolve, reject });
    bridge.request(id, operation, JSON.stringify(arguments_));
  });
}
function bind(id, fn) {
  if ($(id)) $(id).onclick = () => Promise.resolve().then(fn).catch(error);
}
function error(e) {
  const message = e?.message || String(e);
  toast(message === 'COMPONENT_EVIDENCE_REQUIRED'
    ? '暂时无法连接文稿服务，请重新打开 Lulu 后再试。当前内容已保留。' : message);
}
function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => $("toast").hidden = true, 5500);
}
function dialog(title, body) {
  closeMenu();
  $("dialog-title").textContent = title;
  $("dialog-body").innerHTML = body;
  if (!$("dialog").open) $("dialog").showModal();
}
function dirtyStatus() {
  bridge.dirtyChanged(state.dirty || state.draftDirty);
}
function number(v) {
  if (v == null || v === "") return "";
  const n = Number(v);
  if (!Number.isFinite(n)) return "";
  return n >= 1e4 ? (n / 1e4).toFixed(1) + "万" : n >= 1e3 ? (n / 1e3).toFixed(1) + "千" : n.toLocaleString("zh-CN");
}
function date(v) {
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? "" : `${d.getFullYear() === (/* @__PURE__ */ new Date()).getFullYear() ? "" : d.getFullYear() + "/"}${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function versionDate(value) {
  return new Date(value).toLocaleString("zh-CN", {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
}
function group(v) {
  const d = new Date(v), today = /* @__PURE__ */ new Date();
  today.setHours(0, 0, 0, 0);
  return d >= today ? "今天" : d >= new Date(+today - 864e5) ? "昨天" : "更早";
}
function folderName(id) {
  return state.folders.find((f) => f.id === id)?.name || "未分类";
}
function author(row) {
  const a = row.author || row.uploader || row.meta?.author || row.meta?.uploader;
  return typeof a === "object" ? a.nickname || a.name || a.nick_name || "" : a || "";
}
function authorFans(row) {
  const a = row.author || row.meta?.author || {};
  return a.follower_count ?? a.followers ?? a.fans ?? row.follower_count ?? row.meta?.follower_count;
}
function metrics(row) {
  const s = row.stats || row.meta?.stats || {};
  return [["heart", s.like ?? s.likes ?? s.digg_count, "点赞"], ["star", s.favorite ?? s.collect_count, "收藏"], ["comment", s.comment ?? s.comment_count, "评论"]].filter((v) => v[1] != null).map(([i, v, label]) => `<span class="metric" title="${label}">${icon(i)} ${esc(number(v))}</span>`).join("");
}
function voiceModel(record) {
  const engine = record.engine, version = record.model_version ?? record.meta?.model_version;
  if (engine === "local_indextts2") return ["2.0", "2.5"].includes(String(version)) ? `IndexTTS ${version}` : "IndexTTS";
  return {local_voxcpm2:"VoxCPM2", local_qwen3_tts:"Qwen3-TTS"}[engine] || engine || "未记录模型";
}
function audioFormat(filename) {
  const suffix = (filename || "").split(".").pop().toUpperCase();
  return suffix && suffix.length <= 5 ? suffix : "音频";
}
function documentOrigin(record) {
  const platform = record.platform || record.meta?.platform;
  const platformName = {douyin:"抖音", xiaohongshu:"小红书", bilibili:"B站", youtube:"YouTube"}[platform] || platform;
  if (record.source_label && !/^(local_|[\/]|https?:)/.test(record.source_label)) return record.source_label;
  if (platformName) return platformName + " · 文案提取";
  if (/^(\/|[A-Za-z]:[\\/])/.test(record.source || "")) return "本地导入 · 文案提取";
  if (/^local_/.test(record.engine || "")) return "本地转录 · 文案提取";
  if (record.source && !/[\\/]|^local_/.test(record.source)) return record.source;
  return record.kind === "rewrite" ? "创作文稿" : "文稿";
}
function renderRows() {
  let last = "";
  $("list").innerHTML = state.rows.map((row) => {
    const sort = $("sort").value, g = sort === "title" ? "" : group(sort === "updated" ? row.updated_at || row.created_at : row.created_at), heading = g !== last ? `<div class="group-label">${g}</div>` : "";
    last = g;
    const voice = state.kind === "voice", name = author(row), fans = number(authorFans(row)), m = voice ? "" : metrics(row);
    const details = voice ? `<span class="voice-description" title="${esc([row.voice_name, voiceModel(row)].filter(Boolean).join(" · "))}">${esc([row.voice_name, voiceModel(row)].filter(Boolean).join(" · ") || "音频")}</span>` : name ? `<span class="avatar" aria-hidden="true">${esc([...name][0])}</span><span class="author" title="${esc(name)}">${esc(name)}</span>${fans ? `<span class="author-fans">${esc(fans)} 粉丝</span>` : ""}` : `<span class="author">${esc(documentOrigin(row))}</span>`;
    return heading + `<div class="entry ${voice ? "audio-entry" : "manuscript-entry"} ${voice || m ? "" : "no-metrics"} ${(state.batch ? state.selected.has(row.id) : state.doc?.id === row.id) ? "selected" : ""}" role="option" tabindex="0" aria-selected="${state.batch ? state.selected.has(row.id) : state.doc?.id === row.id}" data-entry="${esc(row.id)}"><div class="entry-top"><div class="entry-title">${state.batch ? `<input type="checkbox" tabindex="-1" ${state.selected.has(row.id) ? "checked" : ""} aria-label="选择">` : voice ? icon("audio") : ""}<strong title="${esc(row.title)}">${esc(row.title || "未命名")}</strong></div><div class="row-right"><span class="row-folder" title="${esc(folderName(row.folder_id))}">${esc(folderName(row.folder_id))}</span><span class="row-words">${esc(voice ? (row.duration_seconds == null ? "音频" : audioClock(row.duration_seconds * 1000)) : (row.display_char_count ?? row.char_count ?? 0) + " 字")}</span></div></div>${voice ? `<div class="audio-summary">${details}</div><div class="entry-data">${icon("clock")}<time class="created-time" title="创建时间 ${esc(row.created_at)}">${esc(date(row.created_at))}</time><span class="grow"></span><span>${esc(audioFormat(row.audio_filename))} · 本地文件</span></div>` : `<div class="entry-data">${details}</div><div class="entry-stats"><span class="entry-metrics">${m}</span><time class="created-time" title="创建时间 ${esc(row.created_at)}">${esc(date(row.created_at))}</time></div>`}</div>`;
  }).join("") + (state.next != null ? '<button id="list-more" class="list-more">加载更多</button>' : "");
  if (!state.rows.length) $("list").innerHTML = '<div class="workspace-error">' + ($("search").value ? "没有找到匹配的资料" : "这里还没有资料，可新建文稿或从文案提取开始。") + "</div>";
  $("count").textContent = state.total + (state.kind === "voice" ? " 个" : " 篇");
  $("list-info").textContent = state.total + (state.kind === "voice" ? " 个音频" : " 篇文稿");
  $("list").querySelectorAll("[data-entry]").forEach((node) => {
    const activate = (event = {}) => {
      const id = node.dataset.entry, additive = event.metaKey || event.ctrlKey;
      if (batchBusy) return;
      if (event.shiftKey || additive) {
        if (!state.batch) {
          state.selected = new Set(state.rows.some(r => r.id === (selectionAnchor || state.doc?.id)) ? [selectionAnchor || state.doc.id] : []);
          selectionAnchor ||= state.doc?.id;
          state.batch = true;
        }
        if (event.shiftKey) {
          const first = state.rows.findIndex(r => r.id === selectionAnchor), last = state.rows.findIndex(r => r.id === id);
          if (!additive) state.selected.clear();
          for (const row of state.rows.slice(Math.min(first < 0 ? last : first, last), Math.max(first < 0 ? last : first, last) + 1)) state.selected.add(row.id);
        } else {
          state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
          selectionAnchor = id;
        }
      } else if (state.batch) {
        state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
        selectionAnchor = id;
      } else {
        selectionAnchor = id;
        if (state.loading || id !== state.doc?.id) choose(id).catch(error);
        return;
      }
      renderRows(); renderBatch();
      document.querySelector(`[data-entry="${id}"]`)?.focus({preventScroll:true});
    };
    node.onmousedown = e => { if (e.shiftKey || e.metaKey || e.ctrlKey) e.preventDefault(); };
    node.onclick = activate;
    node.oncontextmenu = event => {
      if (state.kind !== 'documents') return;
      event.preventDefault();
      const id = node.dataset.entry;
      const ids = state.batch && state.selected.has(id) ? [...state.selected] : [id];
      openMenu(node, [
        {label:'打开文稿', icon:'doc', run:() => choose(id)},
        {separator:true},
        {label:`存入 Obsidian${ids.length > 1 ? `（${ids.length} 篇）` : ''}`, icon:'obsidian', disabled:batchBusy, run:() => deliverBatch('batch_obsidian', ids)},
        {label:`存入飞书${ids.length > 1 ? `（${ids.length} 篇）` : ''}`, icon:'feishu', disabled:batchBusy, run:() => deliverBatch('batch_feishu', ids)},
        {label:ids.length > 1 ? '批量导出' : '导出文稿', icon:'export', disabled:batchBusy, run:() => deliverBatch('batch_export', ids)},
        {label:'移动到文件夹', icon:'folder', disabled:batchBusy, run:() => moveDialog(ids, node)},
        {separator:true},
        {label:'移至最近删除', icon:'trash', danger:true, disabled:batchBusy, run:() => deleteDialog(ids)}
      ], {label:'文稿操作', point:{x:event.clientX,y:event.clientY}, align:'start', onError:error});
    };
    node.onkeydown = (e) => {
      if (["Enter", " "].includes(e.key)) {
        e.preventDefault();
        activate(e);
      }
    };
  });
  bind("list-more", () => refresh(true, false));
}
async function refresh(more = false, force = true) {
  const serial = ++querySerial;
  const response = await rpc("list", { kind: state.kind, search: $("search").value, folder_id: $("folder").value || null, sort: $("sort").value, offset: more ? state.next || 0 : 0, limit: 100, refresh: force });
  if (serial !== querySerial) return;
  applyListing(response, more);
}
function applyListing(response, more = false) {
  const query = JSON.stringify([state.kind, $("search").value, $("folder").value, $("sort").value]);
  if (query !== selectionQuery) { state.selected.clear(); state.batch = false; selectionAnchor = null; selectionQuery = query; }
  state.rows = more ? [...state.rows, ...response.entries] : response.entries;
  if (!more) state.selected = new Set([...state.selected].filter(id => state.rows.some(row => row.id === id)));
  state.total = response.total;
  state.next = response.next_offset;
  state.folders = response.folders;
  if (response.library_counts) bridge.libraryCounts(response.library_counts.documents, response.library_counts.voice);
  const current = $("folder").value;
  $("folder").innerHTML = '<option value="">全部文件夹</option>' + state.folders.map((f) => `<option value="${esc(f.id)}">${esc(f.name)}</option>`).join("");
  $("folder").value = current;
  renderRows();
  renderBatch();
  if (response.errors.length) toast("部分资料暂时无法读取，其余资料已显示。");
}
function renderBatch() {
  const docs = state.kind === 'documents', disabled = !state.selected.size || batchBusy;
  $("list").setAttribute('aria-multiselectable', String(state.batch));
  $("batch").hidden = !state.batch;
  $("batch").classList.toggle('manuscript-batch', docs);
  $("batch").innerHTML = `<span class="batch-count">${batchBusy ? '正在处理' : '已选'} ${state.selected.size} ${docs ? '篇' : '个'}</span>${docs ? `<div class="batch-destinations" role="group" aria-label="存入知识库"><button id="batch-obsidian" class="text-button knowledge-save">${icon('obsidian')}<span>存入 Obsidian</span></button><button id="batch-feishu" class="text-button knowledge-save">${icon('feishu')}<span>存入飞书</span></button></div><div class="batch-tools"><button id="batch-export" class="text-button">${icon('export')}<span>导出</span></button><button id="batch-more" class="text-button" aria-label="更多批量操作" title="更多批量操作">${icon('more')}</button></div>` : '<span class="grow"></span><button id="batch-move">移动</button><button id="batch-delete">移至最近删除</button>'}`;
  $("batch").querySelectorAll('button').forEach(b => b.disabled = disabled);
  bind("batch-move", () => moveDialog([...state.selected]));
  bind("batch-delete", () => deleteDialog([...state.selected]));
  bind("batch-export", () => deliverBatch('batch_export'));
  bind("batch-obsidian", () => deliverBatch('batch_obsidian'));
  bind("batch-feishu", () => deliverBatch('batch_feishu'));
  bind("batch-more", () => openMenu($("batch-more"), [
    {label:'移动到文件夹', icon:'folder', run:() => moveDialog([...state.selected], $("batch-more"))},
    {label:'移至最近删除', icon:'trash', danger:true, run:() => deleteDialog([...state.selected])}
  ], {label:'更多批量操作', onError:error}));
  $("select-mode").textContent = state.batch ? "完成" : "选择";
  $("select-mode").disabled = batchBusy;
  $("select-all").disabled = batchBusy || !state.total;
}
async function selectAllResults() {
  if (batchBusy) return;
  const query = JSON.stringify([state.kind, $("search").value, $("folder").value, $("sort").value]);
  const params = {kind:state.kind, search:$("search").value, folder_id:$("folder").value || null, sort:$("sort").value, limit:100, refresh:false};
  const serial = ++querySerial;
  batchBusy = true; renderBatch();
  try {
    const rows = []; let offset = 0, response;
    do {
      response = await rpc("list", {...params, offset});
      if (serial !== querySerial || query !== JSON.stringify([state.kind, $("search").value, $("folder").value, $("sort").value])) return;
      rows.push(...response.entries);
      offset = response.next_offset;
    } while (offset != null);
    state.rows = rows; state.total = response.total; state.next = null;
    state.selected = new Set(rows.map(row => row.id)); state.batch = true;
    selectionQuery = query; selectionAnchor = rows[0]?.id || null;
    renderRows();
  } finally { batchBusy = false; renderBatch(); }
}

async function deliverBatch(action, ids = [...state.selected]) {
  if (batchBusy || !ids.length) return;
  batchBusy = true; renderBatch();
  try {
    await flush();
    const result = await native(action, {document_ids:ids});
    if (!result || result.cancelled) return;
    const failures = (result.results || []).filter(r => !r.ok);
    if (action === 'batch_feishu') {
      if (failures.length) dialog('部分文稿未能读取', failures.map(r => `<p>${esc(r.title)}：${esc(r.message)}</p>`).join(''));
      return;
    }
    const success = result.results.length - failures.length;
    dialog(action === 'batch_obsidian' ? '存入 Obsidian 结果' : '批量导出结果',
      `<p>已完成 ${success} 篇${failures.length ? `，${failures.length} 篇未完成` : ''}。每篇文稿独立保存为 Markdown 文件，同名文件已保留。</p>` +
      result.results.map(r => `<p>${esc(r.title)}：${r.ok ? '已保存 · ' + esc(r.filename) : esc(r.message)}</p>`).join(''));
  } finally { batchBusy = false; renderBatch(); }
}

function choose(id) {
  closeMenu();
  const generation = ++choiceGeneration;
  document.querySelectorAll('[data-entry]').forEach(node => node.classList.toggle('pending-choice', node.dataset.entry === id));
  return serializeTransition(() => generation === choiceGeneration ? chooseNow(id) : undefined);
}
async function chooseNow(id, prepared = undefined) {
  const started = performance.now();
  clearTimeout(selectionTimer);
  state.loading = true;
  try {
    await flush();
    const serial = ++chooseSerial;
    // Leave the previous screen intact if reading the next document fails.
    // While the save/read handoff is pending, do not accept new input into a
    // document whose draft has just been flushed.
    $("detail").inert = $("agent").inert = true;
    const data = prepared !== undefined ? prepared : id ? await rpc(state.kind === "voice" ? "audio" : "open", { document_id: id, ...(state.kind === "voice" ? {} : {include_workspace: true}) }) : null;
    if (serial !== chooseSerial) return;
    state.selection = null;
    $("selection-menu").hidden = true;
    native("audio_waveform", { document_id: null }).catch(error);
    const oldEditor = editor;
    state.doc = null;
    state.chat = null;
    state.refs = [];
    state.refLocations = {};
    referenceGeneration++;
    state.messages = [];
    state.proposals = [];
    if (id) {
      if (state.kind === "voice") {
        state.doc = { ...data, id };
        if (oldEditor) { await oldEditor.destroy(); editor = null; }
        renderAudio();
      } else {
        state.doc = data.document;
        const row = state.rows.find((item) => item.id === id);
        if (row) {
          for (const key of ["title", "folder_id", "char_count", "display_char_count", "updated_at"])
            row[key] = state.doc[key];
        }
        loadChat(data);
        await renderDocument(data.workspace?.scroll);
        renderChat(data.workspace);
        if (data.recovery_draft && !data.recovery_draft.cleared) {
          const draft = data.recovery_draft;
          if (draft.markdown !== state.doc.text || draft.title !== state.doc.title) recoveryDialog(draft);
        }
      }
    } else {
      if (oldEditor) { await oldEditor.destroy(); editor = null; }
      renderEmpty();
    }
    syncRowSelection();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (state.doc?.id === id) bridge.performanceSample('document_switch', performance.now() - started);
    }));
    if (prepared === undefined && (state.kind === "voice" || !id))
      await rpc("view_state", { key: state.kind === "voice" ? "audio" : "selected_document", value: id || "" });
  } finally {
    state.loading = false;
    $("detail").inert = $("agent").inert = false;
    syncRowSelection();
  }
}
function syncRowSelection() {
  for (const node of $("list").querySelectorAll('[data-entry]')) {
    const selected = state.batch ? state.selected.has(node.dataset.entry) : node.dataset.entry === state.doc?.id;
    node.classList.toggle('selected', selected);
    node.classList.remove('pending-choice');
    node.setAttribute('aria-selected', String(selected));
    if (node.dataset.entry === state.doc?.id && state.kind === 'documents') {
      const title = node.querySelector('.entry-title strong');
      title.textContent = title.title = state.doc.title;
      const folder = node.querySelector('.row-folder');
      folder.textContent = folder.title = folderName(state.doc.folder_id);
      node.querySelector('.row-words').textContent = (state.doc.display_char_count ?? state.doc.char_count ?? 0) + ' 字';
    }
  }
}
function renderEmpty() {
  $("detail").innerHTML = `<div class="empty-detail">${state.kind === "voice" ? "选择一段音频，查看详情或试听" : "选择一篇文稿，开始阅读和创作"}</div>`;
  $("agent").innerHTML = '<div class="agent-empty">选择文稿后，在这里与 Lulu 交流</div>';
}
function loadChat(data) {
  state.chat = data.conversation || null;
  state.messages = data.messages || [];
  state.hasOlderMessages = state.messages.length === 100;
  state.proposals = data.proposals || [];
  const d = state.chat?.draft || {};
  state.queuedFollowup = d.queued_followup || null;
  state.refs = d.references || [];
  state.refLocations = d.reference_locations || {};
  referenceGeneration++;
  state.outbox = d.outbox || null;
  state.options = d.options || {};
  state.nextRef = d.next_reference || Math.max(0, ...state.refs.map((r) => r.label_number)) + 1;
  state.chatScroll = d.scroll ?? null;
  state.draftDirty = false;
  dirtyStatus();
}
async function renderDocument(savedScroll = undefined) {
  const d = state.doc;
  if (d.read_only) {
    if (editor) { await editor.destroy(); editor = null; }
    $("detail").innerHTML = `<header class="detail-head"><span>原资料保留 · 只读查看</span><span class="grow"></span>${d.recovery_required ? '<button id="recover-document" class="secondary">恢复为新文稿</button>' : ''}</header><div id="document-scroll" class="document-scroll"><article class="document"><h1 id="doc-title" class="doc-title">${esc(d.title)}</h1><p role="status">${esc(d.recovery_message || '会话暂不可用，正文仍可阅读和复制。')}</p><div id="read-only-body" class="source-text"></div></article></div>`;
    renderMarkdownPreview($("read-only-body"), d.text);
    const recoveryRequest = uid();
    bind('recover-document', async () => {
      const button = $('recover-document');
      if (button.disabled) return;
      button.disabled = true;
      try {
        const result = await rpc('recover_document', {document_id:d.id, expected_hash:d.content_hash, request_id:recoveryRequest});
        await refresh();
        await choose(result.document_id);
        toast('已恢复为新文稿，原文件完整保留。');
      } finally { button.disabled = false; }
    });
    return;
  }
  const aliases = [d.title, (d.source || "").split(/[\\/]/).pop()];
  // Retain the editor DOM and its installed plugins, reset document state below.
  // The old editor stays visible until authoritative content has arrived.
  const retainedRoot = editor?.loadDocument ? $("editor") : null;
  if (retainedRoot) retainedRoot.remove();
  else if (editor) { await editor.destroy(); editor = null; }
  $("detail").innerHTML = `<header class="detail-head"><span class="detail-folder">${icon("folder")} ${esc(folderName(d.folder_id))}</span><span class="grow"></span><button id="save-obsidian" class="text-button knowledge-save">${icon("obsidian")}<span>存入 Obsidian</span></button><button id="save-feishu" class="text-button knowledge-save">${icon("feishu")}<span>存入飞书</span></button><span class="toolbar-separator"></span><button id="history" class="icon-button" title="修改记录" aria-label="修改记录">${icon("clock")}</button><button id="export" class="text-button">${icon("export")} 导出</button><button id="doc-more" class="icon-button" aria-haspopup="menu" aria-expanded="false" aria-label="文稿操作">${icon("more")}</button></header><div id="document-scroll" class="document-scroll"><article class="document"><div class="document-kicker">${esc(documentOrigin(d))}</div><h1 id="doc-title" class="doc-title" contenteditable="plaintext-only" role="textbox" aria-label="文稿标题">${esc(d.title)}</h1><div class="doc-meta"><span>${esc(date(d.created_at))}</span><span id="words">${d.display_char_count ?? d.char_count ?? 0} 字</span><button id="source-info" class="text-button">${icon("link")} 来源与数据</button><span id="save-state" role="status">已保存</span></div><div id="editor" class="editor"></div></article></div>`;
  if (retainedRoot) $("editor").replaceWith(retainedRoot);
  const reused = editor?.loadDocument?.(d.text, aliases);
  if (!reused && editor) { await editor.destroy(); editor = null; }
  if (!reused) editor = await createEditor($("editor"), d.text, (text) => {
    state.doc = { ...state.doc, text };
    editingSequence++;
    state.dirty = true;
    renderReferences();
    dirtyStatus();
    $("save-state").textContent = "正在保存…";
    const count = editor?.characterCount();
    if (count != null) $("words").textContent = count + " 字";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => flushDocument().catch(error), 500);
  }, selectionChanged, aliases);
  $("doc-title").oninput = () => {
    state.doc.title = $("doc-title").textContent;
    const titled = editor?.setTitle?.(state.doc.title);
    if (titled != null) state.doc.text = titled;
    editingSequence++;
    state.dirty = true;
    renderReferences();
    dirtyStatus();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => flushDocument().catch(error), 500);
  };
  for (const target of ["obsidian", "feishu"]) bind("save-" + target, async () => {
    await flushDocument();
    await native(target, { document_id: d.id });
  });
  bind("history", showHistory);
  bind("export", async () => {
    await flushDocument();
    await native("export", { document_id: d.id });
  });
  bind("doc-more", documentMenu);
  bind("source-info", () => dialog("来源与数据", `<div class="source-text">${esc(JSON.stringify({ 来源: d.source, 作者: author(d), 数据: d.meta?.stats, 创建时间: d.created_at }, null, 2))}</div>`));
  const scroll = savedScroll !== undefined ? savedScroll : await rpc("view_state", { key: "scroll:" + d.id });
  if (scroll && state.doc?.id === d.id) $("document-scroll").scrollTop = scroll.top || 0;
  let timer;
  $("document-scroll").onscroll = () => {
    clearTimeout(timer);
    const top = $("document-scroll").scrollTop;
    timer = setTimeout(() => rpc("view_state", { key: "scroll:" + d.id, value: { top } }).catch(error), 250);
  };
}
async function flushDocument() {
  clearTimeout(saveTimer);
  if (saving) await saving;
  if (!state.dirty || !state.doc) return;
  if (editor?.isComposing()) throw Error("请完成当前中文输入后再切换");
  const doc = { ...state.doc }, seq = editingSequence;
  saving = (async () => {
    await rpc("recover_draft", { document_id: doc.id, value: { markdown: doc.text, title: doc.title, base_revision: doc.revision, base_hash: doc.content_hash } });
    const result = await rpc("save", { document_id: doc.id, markdown: doc.text, title: doc.title, expected_revision: doc.revision, expected_hash: doc.content_hash, request_id: uid() });
    if (state.doc?.id === doc.id) {
      state.doc.revision = result.revision;
      state.doc.content_hash = result.content_hash;
      if (seq === editingSequence) {
        state.dirty = false;
        state.doc.display_char_count = result.display_char_count;
        $("words").textContent = result.display_char_count + " 字";
        $("save-state").textContent = "已保存";
        await rpc("recover_draft", { document_id: doc.id, value: { cleared: true } });
      }
      dirtyStatus();
      const row = state.rows.find((r) => r.id === doc.id);
      if (row) {
        row.title = doc.title;
        row.char_count = [...doc.text].length;
        row.display_char_count = result.display_char_count;
        renderRows();
      }
    }
  })().catch((e) => {
    if ($("save-state")) {
      $("save-state").textContent = "保存未完成";
      $("save-state").classList.add("save-conflict");
    }
    if (e.code === "RevisionConflict") showSaveConflict().catch(error);
    throw e;
  }).finally(() => saving = null);
  await saving;
  if (state.dirty) return flushDocument();
}
async function showSaveConflict() {
  const doc = { ...state.doc }, seq = editingSequence;
  const latest = await rpc("snapshot", { document_id: doc.id });
  if (state.doc?.id !== doc.id || editingSequence !== seq) return;
  dialog("文稿出现了两份修改", `<p>当前编辑已保留为恢复草稿。你可以另存副本，或保留草稿后载入磁盘上的最新文稿。</p><h3>当前编辑</h3><pre class="source-text">${esc(doc.text)}</pre><h3>磁盘上的最新内容</h3><pre class="source-text">${esc(latest.text)}</pre><div class="dialog-actions"><button id="conflict-latest" class="secondary">保留草稿，载入最新</button><button id="conflict-copy" class="primary">当前编辑另存副本</button></div>`);
  bind("conflict-copy", async () => {
    if (state.doc?.id !== doc.id || seq !== editingSequence) {
      $("dialog").close();
      toast("当前编辑又有变化，请重新查看冲突");
      return;
    }
    const copy = await rpc("create_document", { request_id: uid(), title: doc.title + " 副本", markdown: doc.text, folder_id: doc.folder_id || null });
    if (state.doc?.id !== doc.id || seq !== editingSequence) {
      toast("副本已保存，新的编辑仍保留");
      return;
    }
    state.dirty = false;
    dirtyStatus();
    $("dialog").close();
    await choose(copy.document_id);
    await refresh();
  });
  bind("conflict-latest", async () => {
    if (state.doc?.id !== doc.id || seq !== editingSequence) {
      $("dialog").close();
      toast("当前编辑又有变化，请重新查看冲突");
      return;
    }
    state.dirty = false;
    dirtyStatus();
    $("dialog").close();
    await choose(doc.id);
  });
}
let checkingExternal = false;
async function checkExternalEdit() {
  if (checkingExternal || !state.visible || document.hidden) return;
  if (!state.ready || state.loading || state.kind !== "documents" || !state.doc || state.doc.read_only || state.dirty || saving || $("dialog").open || editor?.isComposing()) return;
  checkingExternal = true;
  try {
    const id = state.doc.id, seq = editingSequence, doc = await rpc("snapshot", { document_id: id });
    if (state.doc?.id !== id || state.dirty || seq !== editingSequence || state.loading) return;
    if (doc.content_hash === state.doc.content_hash && doc.revision === state.doc.revision) return;
    if (editor?.replaceMarkdown?.(doc.text)) {
      state.doc = doc;
      renderReferences();
      renderMessages();
      $("doc-title").textContent = doc.title;
      $("words").textContent = (doc.display_char_count ?? doc.char_count) + " 字";
      toast("已载入其他入口保存的新修改");
      await refresh();
    } else await choose(id);
  } finally { checkingExternal = false; }
}
async function flushDraft() {
  clearTimeout(draftTimer);
  if (draftSaving) await draftSaving;
  if (!state.chat || !state.draftDirty) return;
  const chat = state.chat, seq = draftSequence, draft = { text: $("prompt")?.value ?? chat.draft.text ?? "", references: state.refs, reference_locations: state.refLocations || {}, next_reference: state.nextRef, outbox: state.outbox || null, options: state.options || {}, queued_followup: state.queuedFollowup || null, scroll: state.chatScroll };
  draftSaving = rpc("save_draft", { conversation_id: chat.id, draft, expected_revision: chat.draft_revision }).then((value) => {
    if (state.chat?.id === chat.id) {
      state.chat = value;
      if (seq === draftSequence) state.draftDirty = false;
      dirtyStatus();
    }
  }).finally(() => draftSaving = null);
  await draftSaving;
  if (state.draftDirty) return flushDraft();
}
async function flush() {
  await flushDocument();
  await flushDraft();
}
function draftChanged() {
  state.draftDirty = true;
  draftSequence++;
  dirtyStatus();
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => flushDraft().catch(error), 300);
}
function selectionChanged() {
  clearTimeout(selectionTimer);
  selectionTimer = setTimeout(updateSelection, 60);
}
function updateSelection() {
  const started = performance.now();
  try {
    const selected = editor?.selection();
    state.selection = selected;
    if (!selected) {
      $("selection-menu").hidden = true;
      return;
    }
    const r = selected.rect;
    const count = selected.ranges?.length || 1;
    $("ask-selection").textContent = count > 1 ? `✧ 引用这 ${count} 段` : state.refs.length ? "＋ 添加到引用" : "✧ 让 AI 改这段";
    $("selection-menu").style.left = Math.max(0, Math.min(innerWidth - 160, r.left)) + "px";
    $("selection-menu").style.top = Math.max(0, r.bottom + 8) + "px";
    $("selection-menu").hidden = false;
    bridge.performanceSample('selection', performance.now() - started);
  } catch {
    state.selection = null;
    $("selection-menu").hidden = true;
  }
}
async function addReference() {
  const s = state.selection, cid = state.chat?.id, docId = state.doc?.id;
  if (!s || addingReferences) return;
  addingReferences = true;
  try {
    await flushDocument();
    if (docId !== state.doc?.id || cid !== state.chat?.id) return;
    if (referencesStale()) throw Error("先移除已变化的旧引用，再添加新片段；写好的要求会保留");
    const d = { ...state.doc }, generation = referenceGeneration, seq = editingSequence;
    const current = () => docId === state.doc?.id && cid === state.chat?.id && seq === editingSequence && generation === referenceGeneration;
    const refs = structuredClone(state.refs), locations = { ...state.refLocations }, added = [], removed = [];
    let next = !refs.length && !$("prompt").value.trim() ? 1 : state.nextRef, changed = false;
    for (const piece of s.ranges || [s]) {
      const ref = await rpc("reference", { document_id: d.id, canonical: piece.canonical, start: piece.start, end: piece.end, expected_revision: d.revision, expected_hash: d.content_hash });
      if (!current()) return;
      const overlaps = refs.filter((r) => r.content_hash === ref.content_hash && r.start <= ref.end && ref.start <= r.end);
      if (overlaps.length) {
        const first = overlaps[0], start = Math.min(ref.start, ...overlaps.map((r) => r.start)), end = Math.max(ref.end, ...overlaps.map((r) => r.end));
        if (overlaps.length === 1 && first.start === start && first.end === end) continue;
        const merged = await rpc("reference", { document_id: d.id, canonical: d.text, start, end, expected_revision: d.revision, expected_hash: d.content_hash });
        if (!current()) return;
        const hints = [...overlaps.map((r) => locations[r.id]), piece.location];
        locations[first.id] = hints.every((h) => h && h.kind === hints[0].kind) ? { kind: hints[0].kind, from: Math.min(...hints.map((h) => h.from)), to: Math.max(...hints.map((h) => h.to)) } : void 0;
        Object.assign(first, merged, { id: first.id, label_number: first.label_number });
        for (const old of overlaps.slice(1)) {
          refs.splice(refs.indexOf(old), 1);
          removed.push(old);
          delete locations[old.id];
        }
      } else {
        if (refs.length >= 100 || next > 1e4) throw Error("本次引用已达上限，请先发送或减少片段");
        ref.label_number = next++;
        refs.push(ref);
        added.push(ref);
        if (piece.location) locations[ref.id] = piece.location;
      }
      changed = true;
    }
    if (!current()) return;
    if (!changed) {
      toast("这段已在引用中");
      return;
    }
    state.refs = refs;
    state.refLocations = Object.fromEntries(Object.entries(locations).filter(([, v]) => v));
    state.nextRef = next;
    referenceGeneration++;
    const input = $("prompt");
    input.value = withoutEmptyReferenceLabels(input.value, removed);
    if (added.length) input.value += (input.value && !input.value.endsWith("\n") ? "\n" : "") + added.map((r) => `片段${chineseNumber(r.label_number)}：`).join("\n");
    if (removed.length) toast("重叠引用已合并，已写的要求仍保留，请检查对应编号");
    draftChanged();
    renderReferences();
    $("selection-menu").hidden = true;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  } finally {
    addingReferences = false;
  }
}
function chineseNumber(n) {
  return n < 11 ? ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"][n] : String(n);
}
function renderReferences() {
  const result = [...(state.messages || [])].reverse().flatMap(m => [...(m.payload?.results || [])].reverse())
    .find(r => r.receipt?.document_id === state.doc?.id && r.receipt?.revision === state.doc?.revision && Array.isArray(r.references));
  if (result && state.refs.length && state.refs.every(r => result.references.some(n => n.id === r.id)) && state.refs.some(r => r.content_hash !== state.doc.content_hash)) {
    state.refs = state.refs.map(r => result.references.find(n => n.id === r.id));
    state.refLocations = {};
    referenceGeneration++;
    draftChanged();
  }

  const box = $("reference-chips");
  if (!box) return;
  box.innerHTML = state.refs.length ? state.refs.map((r) => `<span class="scope-chip reference-chip"><button data-ref="${esc(r.id)}" title="${esc(r.markdown)}">片段${chineseNumber(r.label_number)}</button><button data-remove="${esc(r.id)}" aria-label="移除片段${chineseNumber(r.label_number)}">×</button></span>`).join("") : '<span class="scope-chip">当前文稿</span>';
  box.querySelectorAll("[data-ref]").forEach((b) => b.onclick = () => showReferences(b.dataset.ref));
  box.querySelectorAll("[data-remove]").forEach((b) => b.onclick = () => removeReference(b.dataset.remove));
  $("manage-references").hidden = !state.refs.length;
  $("manage-references").textContent = referencesStale() ? "引用已变动" : `共 ${state.refs.length} 段`;

  box.scrollLeft = box.scrollWidth;
}
function referencesStale() {
  return state.refs.some((r) => state.dirty || r.document_id !== state.doc?.id || r.revision !== state.doc?.revision || r.content_hash !== state.doc?.content_hash);
}
function withoutEmptyReferenceLabels(text, refs) {
  const labels = new Set(refs.flatMap((r) => ["片段" + chineseNumber(r.label_number), "片段" + r.label_number]));
  return text.split("\n").filter((line) => !labels.has(line.trim().replace(/[：:]\s*$/, "")) || !/[：:]\s*$/.test(line)).join("\n");
}
function removeReference(id) {
  const removed = id ? state.refs.filter((r) => r.id === id) : state.refs;
  state.refs = id ? state.refs.filter((r) => r.id !== id) : [];
  for (const r of removed) delete state.refLocations[r.id];
  referenceGeneration++;
  const input = $("prompt");
  input.value = withoutEmptyReferenceLabels(input.value, removed);
  if (!state.refs.length && !input.value.trim()) state.nextRef = 1;
  if (removed.some((r) => input.value.includes("片段" + chineseNumber(r.label_number)))) toast("引用已移除，写好的要求保留；发送前请调整对应编号");
  draftChanged();
  renderReferences();
}
function showReferences(focusId) {
  const stale = referencesStale();
  dialog(`本次引用 · ${state.refs.length} 段`, `<p class="muted">${esc(state.doc.title)} · ${stale ? "文稿已有修改，请重新选择引用" : "只修改这些文字，其他内容保留"}</p><div class="reference-manager">${state.refs.map((r) => `<section class="reference-preview ${r.id === focusId ? "focused-reference" : ""}" data-reference-card="${esc(r.id)}"><header><b>片段${chineseNumber(r.label_number)}</b><span class="grow"></span><button data-locate="${esc(r.id)}" ${stale ? "disabled" : ""}>定位原文</button><button data-delete-ref="${esc(r.id)}">移除</button></header><p>${esc(r.markdown)}</p></section>`).join("")}</div><div class="dialog-actions"><button id="clear-references" class="text-button">清空引用</button><span class="grow"></span><button id="references-done" class="primary">继续写要求</button></div>`);
  document.querySelectorAll("[data-locate]").forEach((b) => b.onclick = () => locateReference(b.dataset.locate).catch(error));
  document.querySelectorAll("[data-delete-ref]").forEach((b) => b.onclick = () => {
    removeReference(b.dataset.deleteRef);
    state.refs.length ? showReferences() : $("dialog").close();
  });
  bind("clear-references", () => {
    removeReference();
    $("dialog").close();
    $("prompt").focus();
  });
  bind("references-done", () => {
    $("dialog").close();
    $("prompt").focus();
  });
  document.querySelectorAll("[data-reference-card]").forEach((el) => {
    if (el.dataset.referenceCard === focusId) el.scrollIntoView({ block: "nearest" });
  });
}
async function locateReference(id) {
  if (referencesStale()) throw Error("原文已有修改，请重新选择引用");
  const ref = state.refs.find((r) => r.id === id), generation = referenceGeneration, seq = editingSequence;
  const anchor = editor?.range(state.refLocations[id]);
  if (!ref || !anchor) throw Error("这条旧引用没有可用的定位信息，请重新选择；原有要求已保留");
  const current = await rpc("reference", { document_id: ref.document_id, canonical: anchor.canonical, start: anchor.start, end: anchor.end, expected_revision: ref.revision, expected_hash: ref.content_hash });
  if (generation !== referenceGeneration || seq !== editingSequence || referencesStale()) return;
  if (current.start !== ref.start || current.end !== ref.end || current.before_hash !== ref.before_hash) throw Error("引用位置已有变化，请重新选择");
  $("dialog").close();
  if (!editor.locate(anchor)) throw Error("引用位置已有变化，请重新选择");
  $("selection-menu").hidden = true;
}
function renderChat(workspace = null) {
  if (!state.chat) {
    $("agent").innerHTML = `<div class="agent-empty"><p>${esc(workspace?.warning || '当前正文可以阅读，原资料已保留。')}</p><button id="recover-workspace" class="secondary">资料保护与恢复</button></div>`;
    bind('recover-workspace', showStorage);
    return;
  }
  $("agent").innerHTML = `<div class="agent-top"><b class="accent">✧ Lulu AI<small class="agent-purpose">创作与学习</small></b><span class="grow"></span><button id="conversations" class="text-button">会话记录</button><button id="new-conversation" class="icon-button" aria-label="新建会话">${icon("plus")}</button></div><div id="chat-scroll" class="chat-scroll" role="log" aria-label="当前文稿的对话"></div><div class="reference-bar"><button id="agent-mode" class="agent-mode" title="协作可按要求改稿；仅讨论不会写入正文"></button><div id="reference-chips" class="reference-chips" aria-label="本次引用"></div><button id="manage-references" class="text-button" hidden></button></div><div class="composer"><div id="composer-popover" class="composer-popover" hidden></div><textarea id="prompt" placeholder="聊聊想法，或告诉我怎么改这篇稿…" aria-label="发给 Lulu 的要求"></textarea><div id="queued-followup" class="queued-followup" hidden></div><div class="composer-bottom"><button id="prompt-menu" class="composer-tool" aria-label="选择技能" aria-expanded="false">${icon("plus")}</button><div class="context-control"><button id="context-ring" class="context-ring" aria-label="上下文窗口，点击压缩"><span class="ring-track"></span><span id="context-percent"></span></button><div class="context-tooltip" role="tooltip"><b>上下文窗口</b><div id="context-details" class="context-usage-line">选择模型后预估</div><div class="context-meter"><i id="context-meter"></i></div><small><span id="context-threshold">达到 80% 自动压缩</span> · 已预留回答空间<br>点击压缩会话 · 完整记录保留</small></div></div><span class="grow"></span><div class="model-control"><button id="model" class="model-trigger" aria-label="当前会话模型" aria-haspopup="dialog" aria-expanded="false"><span>选择模型</span>${icon("chevron")}</button><div id="model-menu" class="model-menu" role="dialog" aria-label="选择会话模型" hidden></div></div><button id="queue-followup" class="secondary queue-followup-button" hidden>加入下一轮</button><button id="send" class="send" aria-label="发送，Enter；Shift Enter 换行">${icon("arrow")}</button></div></div>`;
  $("prompt").value = state.chat.draft.text || "";
  $("prompt").oninput = () => { draftChanged(); updateRunning(); syncSlashMenu(); };
  $("prompt").onkeydown = (e) => {
    if (skillMenuKey(e)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send().catch(error);
    }
  };
  bind("conversations", showConversations);
  bind("new-conversation", () => serializeTransition(async () => {
    await flushDraft();
    const chat = await rpc("new_conversation", { document_id: state.doc.id, request_id: uid() });
    loadChat(await rpc("open_conversation", { conversation_id: chat.id }));
    renderChat();
    $("prompt").focus();
  }));
  const mode = $('agent-mode');
  const renderMode = () => { mode.textContent = state.options?.read_only ? '仅讨论' : '协作'; mode.setAttribute('aria-pressed', String(Boolean(state.options?.read_only))); };
  renderMode();
  mode.onclick = () => { state.options = {...state.options, read_only: !state.options?.read_only}; draftChanged(); renderMode(); };
  bind("manage-references", () => showReferences());
  $("prompt-menu").onclick = () => promptMenu().catch(error);
  bind("queue-followup", queueFollowup);
  bind("send", sendOrStop);
  const sendButton = $("send");
  const flashSendPress = () => {
    if (sendButton.disabled) return;
    sendButton.classList.remove("press-flash");
    void sendButton.offsetWidth;
    sendButton.classList.add("press-flash");
  };
  sendButton.addEventListener("pointerdown", flashSendPress);
  sendButton.addEventListener("keydown", (event) => {
    if (!event.repeat && (event.key === "Enter" || event.key === " ")) flashSendPress();
  });
  updateRunning();
  bind("context-ring", compact);
  renderReferences();
  renderMessages();
  if (state.chatScroll !== null) $("chat-scroll").scrollTop = state.chatScroll;
  $("chat-scroll").onscroll = () => {
    state.chatScroll = $("chat-scroll").scrollTop;
    draftChanged();
  };
  loadModels(workspace?.connections, workspace?.context).catch(error);
  restoreRuns().catch(error);
  restoreMedia().catch(error);
}
function renderMessages() {
  const box = $("chat-scroll");
  if (!box) return;
  const atBottom = box.scrollHeight - box.clientHeight - box.scrollTop < 48, scroll = box.scrollTop;
  box.classList.toggle("empty", !state.messages.length && !state.proposals.length);
  const shown = /* @__PURE__ */ new Set();
  const mediaMessages = new Map(state.messages.filter((m) => m.payload?.media_job_id).map((m) => [m.payload.media_job_id, m.id]));
  const latestToolId = [...state.messages].reverse().find((m) => m.role === "tool")?.id;
  box.innerHTML = (!state.messages.length ? `<div class="agent-welcome"><span class="agent-welcome-symbol">✧</span><h3>从这篇内容开始</h3><p>一起打磨表达，理清思路，把学到的留下来。</p><div class="agent-starters">${['把这篇稿改得更自然','先分析内容结构，不改原文','帮我理解这篇内容的核心观点'].map(t => `<button data-starter="${esc(t)}">${esc(t)}${icon('arrow')}</button>`).join('')}</div></div>` : '') + (state.hasOlderMessages ? '<button id="older-messages" class="list-more">查看更早消息</button>' : "") + state.messages.map((m) => {
    const related = (m.payload?.proposals || []).map((p) => state.proposals.find((v) => v.id === (typeof p === "string" ? p : p.id))).filter(Boolean);
    related.forEach((p) => shown.add(p.id));
    const media = m.payload?.media_job_id && mediaMessages.get(m.payload.media_job_id) === m.id ? mediaMarkup(m.payload.media_job_id) : "";
    const content = m.role === "tool" ? toolStepMarkup(m, m.id === latestToolId) : `<div class="message-text" data-message="${esc(m.id)}">${esc(m.content)}</div>`;
    return `<div class="chat-message ${esc(m.role)}" data-message-id="${esc(m.id)}"><div class="message-by">${m.role === "user" ? "你" : m.role === "assistant" ? "Lulu" : m.role === "tool" ? "Lulu 的任务步骤" : "任务记录"}</div>${content}${m.payload?.partial ? `<details class="partial-result"><summary>查看未完成的回复</summary><div class="message-text">${esc(m.payload.partial)}</div></details>` : ""}${media}</div>` + related.map(proposalMarkup).join("");
  }).join("");
  if (!state.messages.length && !state.proposals.length) {
    box.innerHTML = '<div class="chat-welcome"><span class="welcome-mark">✧</span><h3>一起把这篇稿写好</h3><p>可以从一句话开始，也可以选中左侧的文字，让我帮你改。</p><div class="welcome-prompts"><button data-welcome="帮我润色，保留自己的表达">帮我润色，保留自己的表达</button><button data-welcome="分析这篇文稿的结构和表达方式">一起看看这篇稿的结构</button></div></div>';
    box.querySelectorAll("[data-welcome]").forEach((b) => b.onclick = () => {
      $("prompt").value = b.dataset.welcome;
      draftChanged();
      $("prompt").focus();
    });
  }
  for (const p of state.proposals.filter((p2) => !shown.has(p2.id))) box.insertAdjacentHTML("beforeend", proposalMarkup(p));
  for (const r of mediaStates.values()) {
    if (r.conversation_id === state.chat?.id && !mediaMessages.has(r.job_id) && ["prepared", "queued", "running", "cancel_requested", "recovery_required"].includes(r.state)) {
      box.insertAdjacentHTML("beforeend", `<div class="chat-message status"><div class="message-by">后台转录</div><div class="message-text">${esc(r.filename)}</div>${mediaMarkup(r.job_id)}</div>`);
    }
  }
  bind("older-messages", loadOlderMessages);
  box.querySelectorAll("[data-choose-media]").forEach((b) => b.onclick = () => chooseMedia().catch(error));
  box.querySelectorAll("[data-cancel-media]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { mediaStates.set(b.dataset.cancelMedia, await rpc("cancel_media", { job_id: b.dataset.cancelMedia })); renderMessages(); }
    catch (e) { b.disabled = false; error(e); }
  });
  box.querySelectorAll("[data-recover-media]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { const r = await rpc("recover_media", { job_id: b.dataset.recoverMedia }); mediaStates.set(r.job_id, r); await reloadChat(r.conversation_id); await refresh(); }
    catch (e) { b.disabled = false; error(e); }
  });
  box.querySelectorAll("[data-media-document]").forEach((b) => b.onclick = () => choose(b.dataset.mediaDocument).catch(error));
  box.querySelectorAll("[data-handoff-message]").forEach((b) => b.onclick = async () => {
    try {
      await flush();
      await native("tool_handoff", { conversation_id: state.chat.id, message_id: b.dataset.handoffMessage, call_id: b.dataset.handoffCall });
    } catch (e) { error(e); }
  });
  box.querySelectorAll('[data-starter]').forEach(b => b.onclick = () => { $('prompt').value = b.dataset.starter; draftChanged(); $('prompt').focus(); });
  box.querySelectorAll('[data-undo-edit]').forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {
      await flushDocument();
      const cid = state.chat.id;
      await rpc('undo_edit', {request_id:b.dataset.undoEdit, expected_revision:state.doc.revision, expected_hash:state.doc.content_hash, undo_request_id:uid()});
      await syncConversationDocument(cid);
      toast('已撤销这次修改，历史仍保留');
    } catch (e) { b.disabled = false; error(e); }
  });
  box.querySelectorAll('[data-created-document]').forEach(b => b.onclick = () => choose(b.dataset.createdDocument).catch(error));
  for (const message of state.messages.filter(m => m.role === 'assistant')) {
    const node = [...box.querySelectorAll('[data-message]')].find(el => el.dataset.message === message.id);
    if (node) renderMarkdownPreview(node, message.content);
  }
  for (const proposal of state.proposals) for (const op of proposal.operations) {
    const node = [...box.querySelectorAll('[data-edit-preview]')].find(el => el.dataset.editPreview === op.id);
    if (node) renderMarkdownPreview(node, op.markdown);
  }
  box.scrollTop = atBottom ? box.scrollHeight : scroll;
}
function toolStepMarkup(message, open) {
  const payload = message.payload || {}, calls = Array.isArray(payload.calls) ? payload.calls : [];
  const results = Array.isArray(payload.results) ? payload.results : [];
  if (!results.length) return `<div class="message-text">${esc(message.content)}</div>`;
  const waiting = results.filter((r) => r.state === "needs_input").length;
  const steps = results.map((result, index) => {
    const call = calls[index] || {};
    const stateLabels = { completed: "已完成", succeeded: "已完成", needs_input: "等待你操作", paused: "已暂停", error: "失败", unavailable: "暂不可用" };
    const statusLabel = stateLabels[result.state] || "已返回";
    const targetCount = (result.target?.reference_ids || []).length;
    const target = result.target ? `<details class="partial-result"><summary>${targetCount ? `查看 ${targetCount} 个引用片段的配音文字` : "查看当前文稿的配音文字"}</summary><div class="message-text">${esc(result.target.text)}</div></details>` : "";
    const actions = (result.state === "needs_input" || (result.state === "completed" && result.action === "skills")) ? `<div class="result-actions">${target}${result.action === "transcription" && !result.url ? '<button class="secondary" data-choose-media>选择文件 · SenseVoice</button>' : ""}${call.id ? `<button class="secondary" data-handoff-message="${esc(message.id)}" data-handoff-call="${esc(call.id)}">${esc(result.label || "前往工作台")}</button>` : ""}</div>` : "";
    const changes = result.changes?.length ? `<details class="edit-diff"><summary>查看修改内容</summary>${result.changes.map(c => `<div class="change-block old"><small>修改前</small><div>${esc(c.before || '（空白）')}</div></div><div class="change-block new"><small>修改后</small><div>${esc(c.after)}</div></div>`).join('')}</details>` : '';
    const receipt = result.receipt;
    const undo = receipt ? `<button class="text-button" data-undo-edit="${esc(receipt.request_id)}" ${state.doc?.revision === receipt.revision && state.doc?.content_hash === receipt.content_hash ? '' : 'disabled'}>撤销这次修改</button>` : '';
    const output = call.name === 'create_document' && result.document_id ? `<button class="text-button" data-created-document="${esc(result.document_id)}">打开新文稿</button>` : '';
    return `<article class="agent-tool-step"><header><b>${esc(result.label || call.name || `步骤 ${index + 1}`)}</b><small>${esc(statusLabel)}</small></header><p>${esc(result.message || "已收到这一步的结果。")}</p>${changes}${actions}${undo}${output}</article>`;
  }).join("");
  const intro = payload.assistant_text ? `<div class="agent-tool-intro">${esc(payload.assistant_text)}</div>` : "";
  return `${intro}<details class="agent-tool-group" ${open ? "open" : ""}><summary><span>执行了 ${results.length} 个步骤</span><small>${waiting ? `有 ${waiting} 项等你操作` : "查看过程"}</small></summary><div class="agent-tool-steps">${steps}</div></details>`;
}
function proposalMarkup(p) {
  const pending = ['pending', 'partly_applied'].includes(p.state);
  const stale = pending && (p.base_revision !== state.doc?.revision || p.base_hash !== state.doc?.content_hash);
  const labels = {applied:'已写入', discarded:'已放弃', superseded:'已有更新方案', pending:'尚未写入'};
  return `<section class="ai-result conversation-proposal" aria-label="完整修改方案"><h3>${pending ? '可供讨论的方案' : labels[p.state] || '修改方案'}</h3>${p.operations.map((o, i) => `<div class="result-meta">${p.operations.length > 1 ? `第 ${i + 1} 项 · ` : ''}${o.label_number ? '片段' + chineseNumber(o.label_number) : o.action === 'append' ? '追加到文稿末尾' : '修改当前文稿'} · ${labels[o.state] || o.state}</div><div class="new" data-edit-preview="${esc(o.id)}">${esc(o.markdown)}</div>`).join('')}${stale ? '<p class="proposal-note">正文已有新版本。使用这份方案前会重新核对。</p>' : ''}</section>`;
}
async function loadOlderMessages() {
  const cid = state.chat?.id, before = state.messages[0]?.sequence;
  if (!cid || !before) return;
  $("older-messages").disabled = true;
  try {
    const rows = await rpc("messages", { conversation_id: cid, before });
    if (cid !== state.chat?.id) return;
    const box = $("chat-scroll"), top = box.scrollTop, height = box.scrollHeight;
    const known = new Set(state.messages.map((m) => m.id));
    state.messages = [...rows.filter((m) => !known.has(m.id)), ...state.messages];
    state.hasOlderMessages = rows.length === 100;
    renderMessages();
    box.scrollTop = top + box.scrollHeight - height;
  } finally {
    if (cid === state.chat?.id && $("older-messages")) $("older-messages").disabled = false;
  }
}
async function reloadChat(cid = state.chat?.id) {
  if (!cid) return;
  const data = await rpc("conversation_snapshot", { conversation_id: cid });
  if (cid !== state.chat?.id) return;
  const messages = [...state.messages.filter((m) => m.sequence < (data.messages[0]?.sequence || 0)), ...data.messages];
  if (JSON.stringify([state.messages, state.proposals]) === JSON.stringify([messages, data.proposals])) return;
  state.messages = messages;
  state.proposals = data.proposals;
  renderMessages();
}
function settleRunStatus(box, messageId, phase, label) {
  const row = [...box.querySelectorAll("[data-message-id]")].find((item) => item.dataset.messageId === messageId);
  if (!row) return;
  let status = row.querySelector(".live-status");
  if (!status) {
    status = document.createElement("div");
    status.className = "live-status";
    status.setAttribute("role", "status");
    row.append(status);
  }
  status.classList.add("settled");
  status.dataset.phase = phase;
  status.textContent = label;
}
async function syncConversationDocument(cid) {
  if (cid !== state.chat?.id || state.dirty || saving || editor?.isComposing()) return;
  const id = state.doc.id, seq = editingSequence;
  const doc = await rpc('snapshot', {document_id:id});
  if (cid !== state.chat?.id || state.doc?.id !== id || state.dirty || seq !== editingSequence) return;
  if (doc.content_hash === state.doc.content_hash && doc.revision === state.doc.revision) return;
  if (editor?.replaceMarkdown?.(doc.text)) {
    state.doc = doc;
    renderReferences(); renderMessages();
    $('words').textContent = (doc.display_char_count ?? doc.char_count) + ' 字';
    $('save-state').textContent = '已保存';
    await refresh();
  } else await choose(id);
}
async function showConversations(archived = false) {
  await flushDraft();
  const did = state.doc.id, cid = state.chat.id;
  const rows = await rpc('conversations', {document_id:did, archived});
  if (state.doc?.id !== did) return;
  dialog('这篇文稿的会话', `<div class="conversation-tabs"><button id="active-chats" class="text-button ${!archived ? 'accent' : ''}">进行中</button><button id="archived-chats" class="text-button ${archived ? 'accent' : ''}">已归档</button></div>` + '<input id="conversation-search" class="conversation-search" aria-label="搜索会话名称" placeholder="搜索会话名称"><div id="conversation-results"></div>');
  const render = () => {
    const query = $('conversation-search').value.trim().toLocaleLowerCase();
    const matches = rows.filter(c => c.title.toLocaleLowerCase().includes(query));
    $('conversation-results').innerHTML = matches.map(c => `<div class="dialog-row"><button class="grow" data-chat="${esc(c.id)}" ${c.id === cid ? 'aria-current="true"' : ''}>${esc(c.title)}${c.id === cid ? ' · 当前' : ''}<small class="conversation-date">${esc(versionDate(c.updated_at))}</small></button><button data-rename-chat="${esc(c.id)}" class="text-button" aria-label="重命名 ${esc(c.title)}">重命名</button><button data-archive-chat="${esc(c.id)}" class="text-button">${archived ? "恢复" : "归档"}</button><button data-delete-chat="${esc(c.id)}" class="text-button conversation-delete">彻底删除</button></div>`).join('') || '<p class="muted">没有匹配的会话。</p>';
    document.querySelectorAll('[data-chat]').forEach(b => b.onclick = () => serializeTransition(async () => {
      await flushDraft();
      if (state.doc?.id !== did) return;
      const data = await rpc('open_conversation', {conversation_id:b.dataset.chat});
      if (state.doc?.id !== did) return;
      loadChat(data); $('dialog').close(); renderChat(); $('prompt').focus();
    }).catch(error));
    document.querySelectorAll('[data-archive-chat]').forEach(b => b.onclick = async () => { try { await rpc('archive_conversation', {conversation_id:b.dataset.archiveChat, archived:!archived}); await showConversations(archived); } catch(e) { error(e); } });
    document.querySelectorAll('[data-delete-chat]').forEach(b => b.onclick = () => confirmDeleteConversation(rows.find(c => c.id === b.dataset.deleteChat)));
    document.querySelectorAll('[data-rename-chat]').forEach(b => b.onclick = () => {
      const row = rows.find(c => c.id === b.dataset.renameChat);
      dialog('重命名会话', `<input id="conversation-title" maxlength="100" aria-label="会话名称" value="${esc(row.title)}"><div class="dialog-actions"><button id="save-conversation-title" class="primary">保存</button></div>`);
      bind('save-conversation-title', async () => {
        const value = await rpc('rename_conversation', {conversation_id:row.id, title:$('conversation-title').value});
        if (state.chat?.id === row.id) state.chat.title = value.title;
        await showConversations();
      });
    });
  };
  bind('active-chats', () => showConversations(false)); bind('archived-chats', () => showConversations(true));
  $('conversation-search').oninput = render; render(); $('conversation-search').focus();
}

async function confirmDeleteConversation(chat) {
  dialog("彻底删除这段会话？", `<p>将永久删除“${esc(chat.title)}”的对话、草稿、修改建议和 Lulu 可恢复备份中的副本，无法撤销。</p><p class="muted">已应用到文稿的内容、单独保存的转录文稿和原音视频会保留。</p><div class="dialog-actions"><button id="cancel-delete-chat" class="text-button">返回</button><button id="confirm-delete-chat" class="primary">彻底删除</button></div>`);
  bind("cancel-delete-chat", showConversations);
  bind("confirm-delete-chat", async () => {
    const button = $("confirm-delete-chat");
    button.disabled = true;
    try {
      const result = await rpc("delete_conversation", { conversation_id: chat.id });
      if (state.chat?.id === chat.id) {
        state.chat = null;
        state.messages = [];
        state.proposals = [];
        state.refs = [];
        state.draftDirty = false;
        dirtyStatus();
        const nextId = result.next_conversation_id;
        const next = nextId
          ? await rpc("open_conversation", { conversation_id: nextId })
          : await rpc("open_conversation", {
              conversation_id: (await rpc("new_conversation", {
                document_id: state.doc.id, request_id: uid()
              })).id
            });
        loadChat(next);
        renderChat();
      }
      await showConversations();
      toast(`会话已彻底删除，${result.removed_backup_copies} 份旧备份中的副本也已清除`);
    } catch (e) {
      error(e);
      await showConversations();
    }
  });
}
function closeSkillMenu() {
  skillMenuSerial++;
  skillMenuSource = null;
  if ($('composer-popover')) $('composer-popover').hidden = true;
  $('prompt-menu')?.setAttribute('aria-expanded', 'false');
}
function slashQuery() {
  return /^\s*\/([^\s/]*)$/.exec($('prompt')?.value || '');
}
function syncSlashMenu() {
  if (slashQuery()) promptMenu('slash').catch(error);
  else if (skillMenuSource === 'slash') closeSkillMenu();
}
function skillMenuKey(event) {
  const host = $('composer-popover');
  if (!host || host.hidden || event.isComposing || event.keyCode === 229) return false;
  if (event.key === 'Escape') { closeSkillMenu(); event.preventDefault(); return true; }
  const options = [...host.querySelectorAll('[data-skill]')];
  if (['ArrowDown', 'ArrowUp'].includes(event.key) && options.length) {
    const index = options.findIndex(b => b.classList.contains('keyboard-choice'));
    const next = (index + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
    options.forEach((b,i) => b.classList.toggle('keyboard-choice', i === next));
    options[next].scrollIntoView({block:'nearest'});
    event.preventDefault(); return true;
  }
  if (event.key === 'Enter' && !event.shiftKey && skillMenuSource === 'slash') {
    (options.find(b => b.classList.contains('keyboard-choice')) || options[0])?.click();
    event.preventDefault(); return true;
  }
  return false;
}
async function promptMenu(source = 'button') {
  const host = $('composer-popover');
  if (!host) return;
  if (source === 'button' && !host.hidden) { closeSkillMenu(); return; }
  skillMenuSource = source;
  const serial = ++skillMenuSerial;
  host.hidden = false;
  $('prompt-menu').setAttribute('aria-expanded', 'true');
  const heading = '<div class="popover-heading">技能<span>按对话需要自动使用，也可手动指定</span></div>';
  if (!skillEntries) host.innerHTML = heading + '<p class="muted" role="status">正在读取技能…</p>';
  try {
    if (!skillEntries) {
      const request = skillRequest ||= rpc('skills').then(value => value.entries);
      let entries;
      try { entries = await request; }
      finally { if (skillRequest === request) skillRequest = null; }
      if (serial !== skillMenuSerial || !host.isConnected || host.hidden) return;
      skillEntries = entries;
    }
    if (serial !== skillMenuSerial || !host.isConnected || host.hidden) return;
    const query = source === 'slash' ? (slashQuery()?.[1] || '').toLocaleLowerCase() : '';
    const entries = skillEntries.filter(e => [e.name, e.title, e.description].join(' ').toLocaleLowerCase().includes(query));
    host.innerHTML = heading + entries.map(e => `<button class="prompt-option" data-skill="${esc(e.name)}">${icon('spark')}<span><b>${esc(e.title || e.name)}</b><small>${esc(e.description)}</small></span></button>`).join('') + (entries.length ? '' : '<p class="muted">没有匹配的已安装技能</p>') + '<button id="skill-settings" class="add-preset">管理技能 · 前往设置</button>';
    host.querySelectorAll('[data-skill]').forEach(b => b.onclick = () => {
      const input = $('prompt');
      input.value = '/' + b.dataset.skill + ' ' + input.value.replace(/^\s*\/[^\s/]*(?:\s+|$)/, '');
      closeSkillMenu(); draftChanged(); updateRunning(); input.focus();
    });
    bind('skill-settings', () => { closeSkillMenu(); return native('skill_settings', {}); });
  } catch (e) {
    if (serial === skillMenuSerial && host.isConnected && !host.hidden)
      host.innerHTML = heading + '<p role="status">技能暂时未能读取，请关闭后重试。</p>';
    throw e;
  }
}
function recoveryDialog(draft) {
  dialog("找到上次未完成的编辑", `<p>当前磁盘文稿与恢复草稿都已保留。你可以先查看再决定。</p><pre class="source-text">${esc(draft.markdown)}</pre><div class="dialog-actions"><button id="recovery-later" class="secondary">暂时保留</button><button id="recovery-load" class="primary">载入草稿继续编辑</button></div>`);
  bind("recovery-later", () => $("dialog").close());
  bind("recovery-load", async () => {
    state.doc.text = draft.markdown;
    state.doc.title = draft.title;
    state.dirty = true;
    editingSequence++;
    dirtyStatus();
    await editor.destroy();
    await renderDocument();
    $("save-state").textContent = "恢复草稿待保存";
    $("dialog").close();
  });
}
async function showHistory() {
  await flushDocument();
  const d = state.doc, rows = await rpc("history", { document_id: d.id });
  dialog("修改记录", `<div class="history-intro"><span>${esc(d.title)}</span><small>选择记录预览，恢复前会保留当前版本。</small></div>${rows.length ? '<div class="version-browser"><div class="version-list" role="listbox" aria-label="历史版本">' + rows.map((r) => `<button class="version-item" role="option" aria-selected="false" data-version="${esc(r.name)}"><strong>${esc(versionDate(r.saved_at))}</strong><span>备份 · 版本 ${r.revision}</span></button>`).join("") + '</div><section class="version-preview"><div id="version-label" class="preview-label">选择左侧记录查看正文</div><pre id="version-text" class="source-text"></pre><footer><span id="version-feedback" role="status"></span><button id="version-restore" class="primary" disabled>恢复此版本</button></footer></section></div>' : '<div class="utility-empty">还没有早期版本。修改保存后，之前的正文会保留在这里。</div>'}`);
  const modal = $("dialog");
  modal.classList.add('history-modal');
  modal.addEventListener('close', () => modal.classList.remove('history-modal'), {once:true});
  const list = document.querySelector(".version-list");
  const active = () => modal.open && list?.isConnected && state.doc?.id === d.id;
  let chosen = null, serial = 0;
  document.querySelectorAll('[data-version]').forEach(button => button.onclick = async () => {
    const generation = ++serial;
    chosen = null;
    $("version-restore").textContent = "恢复此版本";
    delete $("version-restore").dataset.confirm;
    $("version-restore").disabled = true;
    $("version-feedback").textContent = "";
    document.querySelectorAll('[data-version]').forEach(b => b.setAttribute('aria-selected', String(b === button)));
    $("version-label").textContent = '正在读取版本…';
    try {
      const version = await rpc('history', {document_id:d.id, name:button.dataset.version});
      if (!active() || generation !== serial) return;
      const row = rows.find(r => r.name === button.dataset.version);
      chosen = row;
      renderMarkdownPreview($("version-text"), version.text);
      $("version-label").textContent = `备份于 ${versionDate(row.saved_at)} · 版本 ${row.revision}`;
      $("version-feedback").textContent = version.text === d.text ? '与当前正文相同' : '';
      $("version-restore").disabled = version.text === d.text;
    } catch(e) { if (active() && generation === serial) { $("version-label").textContent = '此版本暂时无法读取'; error(e); } }
  });
  bind('version-restore', async () => {
    if (!chosen) return;
    if ($('version-restore').dataset.confirm !== chosen.name) {
      $('version-restore').dataset.confirm = chosen.name;
      $('version-restore').textContent = '确认恢复';
      $('version-feedback').textContent = '将替换当前正文，当前版本仍会保留。';
      return;
    }
    $('version-restore').disabled = true;
    try {
      await rpc('restore', {document_id:d.id, name:chosen.name, expected_revision:d.revision, expected_hash:d.content_hash, request_id:uid()});
      modal.close(); await choose(d.id); toast('已恢复所选版本');
    } catch(e) { error(e); $('version-restore').disabled = false; }
  });
  document.querySelector('[data-version]')?.click();
}

function documentMenu() {
  const id = state.doc.id;
  openMenu($("doc-more"), [
    {id:'move-doc', label:'移动到文件夹', icon:'folder', run:() => moveDialog([id])},
    {id:'voice-doc', label:'带入文案配音', icon:'mic', run:async () => { await flushDocument(); await native('voice', {document_id:id, references:state.refs}); }},
    {id:'copy-doc', label:'另存为副本', icon:'doc', run:async () => {
      await flush(); const d = state.doc;
      const copy = await rpc('create_document', {request_id:uid(), title:d.title+' 副本', markdown:d.text, folder_id:d.folder_id || null});
      await refresh(); await choose(copy.document_id);
    }},
    {separator:true},
    {id:'source-mode', label:'查看 Markdown 源码', icon:'doc', run:() => dialog('Markdown 源码', `<pre class="source-text">${esc(state.doc.text)}</pre>`)},
    {id:'markdown-guide', label:'Markdown 语法指南', icon:'book', run:() => showMarkdownGuide({dialog, esc, copy:text => native('copy_text', {text}), onError:error})},
    {separator:true},
    {id:'delete-doc', label:'移至最近删除', icon:'trash', danger:true, run:() => deleteDialog([id])}
  ], {label:'文稿操作', onError:error});
}
function moveDialog(ids, source = null) {
  const anchor = source || (state.batch ? ($("batch-more") || $("batch-move")) : $("doc-more"));
  const folder = state.doc?.folder_id || '';
  openMenu(anchor, [{id:'', name:'未分类'}, ...state.folders].map(f => ({
    label:f.name, selected:ids.length === 1 && f.id === folder,
    run:async () => {
      await flush();
      const results = await rpc('move', {document_ids:ids, folder_id:f.id || null});
      const failures = results.filter(r => !r.ok);
      if (failures.length) throw Error(failures.map(r => r.message).join('\n'));
      await refresh();
      if (state.doc && ids.includes(state.doc.id)) await choose(state.doc.id);
    }
  })), {label:'移动到文件夹', choice:true, onError:error});
}
function deleteDialog(ids) {
  dialog("移至最近删除", `<p>将 ${ids.length} 篇资料移至最近删除，可以恢复。</p><div class="dialog-actions"><button id="confirm-delete" class="primary">移至最近删除</button></div>`);
  bind("confirm-delete", async () => {
    await flush();
    const done = [], failed = [];
    for (const id of ids) {
      try {
        await rpc("delete", { document_id: id });
        done.push(id);
      } catch (e) {
        failed.push({ id, message: e.message });
      }
    }
    if (failed.length) dialog("删除结果", `<p>已移至最近删除 ${done.length} 篇，${failed.length} 篇未完成。</p>${failed.map((f) => `<p>${esc(state.rows.find((r) => r.id === f.id)?.title || f.id)}：${esc(f.message)}</p>`).join("")}`);
    else $("dialog").close();
    if (done.includes(state.doc?.id)) await choose(null);
    state.selected.clear();
    renderBatch();
    await refresh();
  });
}
function listMenu() {
  const voice = state.kind === 'voice';
  openMenu($("list-menu"), [
    {id:'new-folder', label:'新建文件夹', icon:'folder', run:newFolderDialog},
    {id:'manage-folders', label:'管理文件夹', icon:'folder', disabled:!state.folders.length, run:manageFoldersDialog},
    ...(!voice ? [
      {id:'import-doc', label:'导入文稿', icon:'doc', run:() => native('import')},
    ] : []),
    {separator:true},
    {id:'trash', label:'最近删除', icon:'trash', run:() => native('library_page', {kind:'trash'})},
    {id:'storage-protection', label:'资料保护与恢复', icon:'clock', run:showStorage}
  ], {label:voice ? '音频库操作' : '文稿库操作', onError:error});
}
function newFolderDialog() {
  const kind = state.kind;
  dialog('新建文件夹', '<form id="folder-form" class="preset-form"><label>名称<input id="folder-name" maxlength="80" required></label><button class="primary">创建</button></form>');
  $("folder-form").onsubmit = async e => {
    e.preventDefault();
    try { await rpc('create_folder', {name:$("folder-name").value, kind}); $("dialog").close(); await refresh(); } catch (e) { error(e); }
  };
}
function manageFoldersDialog() {
  dialog('管理文件夹', `<p>删除时可以保留素材，也可以将所属素材一起移至最近删除。</p>${state.folders.map(f => `<div class="dialog-row"><span>${esc(f.name)}</span><button class="text-button" data-delete-folder="${esc(f.id)}">删除文件夹</button></div>`).join('')}`);
  document.querySelectorAll('[data-delete-folder]').forEach(button => button.onclick = () => {
    const folder = state.folders.find(f => f.id === button.dataset.deleteFolder);
    if (!folder) return;
    dialog('删除文件夹', `<p>删除「${esc(folder.name)}」时，如何处理里面的素材？</p><p class="muted">保留素材：移到未分类，内容和音频文件均保留。<br>一起删除：所属素材移至最近删除，可以恢复；外部原文件保留。</p><div class="dialog-actions"><button id="cancel-folder-delete" class="secondary">取消</button><button id="confirm-folder-delete" class="primary">删除文件夹，保留素材</button><button id="delete-folder-materials" class="danger-button">删除文件夹及所属素材</button></div>`);
    $('cancel-folder-delete').onclick = manageFoldersDialog;
    let deleting = false;
    const remove = async deleteMaterials => {
      if (deleting) return;
      deleting = true;
      const modal = $('dialog');
      const buttons = [...modal.querySelectorAll('button')];
      buttons.forEach(b => b.disabled = true);
      const preventClose = e => e.preventDefault();
      modal.addEventListener('cancel', preventClose);
      try {
        await flush();
        const result = await rpc('delete_folder', {folder_id:folder.id, delete_materials:deleteMaterials});
        const removed = result.removed_ids || [];
        if (removed.length) await native('audio_stop', {document_ids:removed});
        if (removed.includes(state.doc?.id)) await choose(null);
        if (result.deleted) {
          if ($('folder').value === folder.id) $('folder').value = '';
          if (state.doc?.folder_id === folder.id) {
            state.doc.folder_id = null;
            const label = document.querySelector('.detail-folder');
            if (label) { label.textContent = '未分类'; label.title = '未分类'; }
          }
        }
        state.selected.clear();
        await refresh();
        if (result.failed?.length) {
          dialog('删除未全部完成', `<p>已移至最近删除 ${removed.length} 项。文件夹仍保留，可重试剩余项。</p>${result.failed.map(f => `<p>${esc(f.title)}：${esc(f.message)}</p>`).join('')}<div class="dialog-actions"><button id="retry-folder-delete" class="secondary">返回文件夹管理</button></div>`);
          $('retry-folder-delete').onclick = manageFoldersDialog;
        } else {
          modal.close();
          toast(deleteMaterials ? `文件夹已删除，${removed.length} 项素材已移至最近删除` : '文件夹已删除，素材已保留');
        }
      } catch (e) { error(e); }
      finally {
        deleting = false;
        buttons.forEach(b => b.disabled = false);
        modal.removeEventListener('cancel', preventClose);
      }
    };
    $('confirm-folder-delete').onclick = () => remove(false);
    $('delete-folder-materials').onclick = () => remove(true);
  });
}
async function showStorage() {
  if (state.doc) await flush();
  const value = await rpc("storage_status");
  dialog("资料保护与恢复", `<p>会话、草稿、连接设置和修改建议有自动备份。文稿正文另有修改历史。</p><p class="muted">有改动时每 5 分钟保存一次，轮换保留最近 3 份自动备份。手动备份单独保留。</p>${value.warning ? `<p>${esc(value.warning)}</p>` : ""}${value.state !== "ready" ? '<p>当前会话记录暂不可用。原文件保留，可以选择经过检查的备份恢复。</p>' : '<button id="make-backup" class="secondary">立即备份会话</button>'}<div class="backup-list">${value.backups.map((b) => `<div class="dialog-row"><span>${b.available ? `${esc(b.created_at.replace("T", " ").slice(0, 19))} UTC · ${b.automatic ? "自动" : "手动"}<small class="muted"> · ${b.counts.conversations} 个会话 · ${b.counts.messages} 条记录</small>` : esc(b.message)}</span>${b.available && value.fingerprint ? `<button class="text-button" data-restore-backup="${esc(b.id)}">查看恢复</button>` : ""}</div>`).join("") || '<p class="muted">暂时没有可用备份。</p>'}</div>`);
  bind("make-backup", async () => {
    await rpc("backup");
    await showStorage();
    toast("会话备份已保存并通过检查");
  });
  document.querySelectorAll("[data-restore-backup]").forEach((button) => button.onclick = () => {
    const backup = value.backups.find((b) => b.id === button.dataset.restoreBackup);
    dialog("恢复这份会话备份", `<p>恢复到 ${esc(backup.created_at.replace("T", " ").slice(0, 19))} UTC 的 ${backup.counts.conversations} 个会话、${backup.counts.messages} 条记录。此时间之后的会话改动不会出现在恢复后的列表中。</p><p>当前文稿正文保持原样。恢复前的会话文件会单独保留；旧修改建议仍会核对正文版本。</p><div class="dialog-actions"><button id="cancel-restore" class="text-button">返回</button><button id="confirm-restore" class="primary">恢复并重新打开工作区</button></div>`);
    bind("cancel-restore", showStorage);
    bind("confirm-restore", async () => {
      await rpc("restore_backup", { backup_id: backup.id, expected_fingerprint: value.fingerprint, expected_backup_hash: backup.sha256 });
      window.location.reload();
    });
  });
}
function utilityPage(page, html) {
  closeMenu();
  $("selection-menu").hidden = true;
  state.page = page;
  $("workspace").hidden = true;
  $("utility-page").hidden = false;
  $("utility-page").innerHTML = html;
}
async function showTrash(options = {}) {
  await flush();
  const policy = await rpc("trash_policy");
  const rows = await rpc("trash");
  const query = String(options.query || "");
  let selectionMode = Boolean(options.selectionMode), anchorToken = options.previewToken || null;
  const selectedTokens = new Set((options.selectedTokens || []).filter(token => rows.some(row => row.token === token)));
  let previewToken = rows.some(row => row.token === options.previewToken) ? options.previewToken : null;
  if (!previewToken && !selectionMode) previewToken = rows[0]?.token || null;
  const rowMarkup = rows.map(row => {
    const title = row.title || "未命名资料";
    const kind = row.kind === "voice" ? "音频" : row.kind ? "文稿" : "资料";
    const kindIcon = row.kind === "voice" ? "audio" : row.kind ? "doc" : "trash";
    return `<button class="trash-item" role="option" aria-selected="false" data-trash="${esc(row.token)}" data-search="${esc(title.toLocaleLowerCase())}">
      <span class="trash-check" aria-hidden="true">${icon("check")}</span>
      <span class="trash-item-copy"><span class="trash-kind">${icon(kindIcon)}${kind}</span>
      <strong>${esc(title)}</strong><span class="trash-item-date">删除于 ${esc(date(row.deleted_at) || "时间未知")}</span></span>
    </button>`;
  }).join("");
  utilityPage("trash", `<header class="utility-heading trash-heading"><div><h1>最近删除 <small>${rows.length} 项</small></h1><p>找回已删除的资料，或手动彻底删除。</p></div><button id="trash-auto" class="trash-auto-policy" type="button" aria-label="自动清理设置，${policy.enabled ? "已开启，保留 7 天" : "未开启"}">${icon("clock")}<span>自动清理</span><span id="trash-auto-state" class="trash-auto-state" data-enabled="${Boolean(policy.enabled)}">${policy.enabled ? "7 天后" : "未开启"}</span>${icon("chevron")}</button></header><div id="trash-status" class="trash-status" role="status" aria-live="polite"${options.statusMessage ? "" : " hidden"}>${esc(options.statusMessage || "")}</div>${rows.length ? `
    <div class="trash-toolbar">
      <label class="trash-search"><span>${icon("search")}</span><input id="trash-search" type="search" value="${esc(query)}" placeholder="搜索已删除的名称" aria-label="搜索已删除资料名称"><kbd>⌘ F</kbd></label>

    </div>
    <div class="trash-browser"><div class="trash-sidebar">      <div class="trash-selection-controls"><span id="trash-count" class="muted"></span><button id="trash-select-visible" class="text-button">全选当前结果</button><button id="trash-select-mode" class="secondary" type="button" aria-pressed="${selectionMode}">${selectionMode ? "取消选择" : "选择"}</button></div><div id="trash-list" class="version-list trash-list" role="listbox" aria-label="已删除资料"${selectionMode ? " aria-multiselectable=\"true\"" : ""}>${rowMarkup}<div id="trash-no-results" class="trash-no-results" hidden>没有找到符合条件的资料</div></div></div>
      <section class="trash-preview" aria-label="资料预览"><div class="trash-preview-head"><div><span class="trash-preview-kicker">资料详情</span><h2 id="trash-title">选择资料查看</h2><span id="trash-meta" class="muted"></span></div><span class="trash-retention">${icon("clock")} 可恢复</span></div>
        <div id="trash-preview-empty" class="trash-preview-empty"><span class="trash-preview-symbol">${icon("doc")}</span><strong id="trash-preview-empty-title">先选中一项</strong><p id="trash-preview-empty-copy">预览能帮助你确认内容。即使预览暂时不可用，也可以尝试恢复。</p></div>
        <div id="trash-text" class="source-text trash-text" hidden></div>
        <footer class="trash-preview-footer"><span id="trash-restore-hint">恢复后会回到原来的文稿库或音频库。</span><button id="trash-purge" class="danger-button" type="button" disabled>彻底删除</button><button id="trash-restore" class="primary" type="button" disabled>恢复到素材库</button></footer>
      </section></div>` : `<div class="trash-empty-state"><span class="trash-empty-symbol">${icon("trash")}</span><h2>最近删除为空</h2><p>删除文稿或音频后，可以在这里找回。</p></div>`}`);

  bind("trash-auto", () => {
    const modal = $("dialog");
    dialog("自动清理", `
      <label class="auto-setting"><span><strong>自动清理</strong><span id="auto-description">${policy.enabled ? "删除 7 天后自动清理" : "一直保留，由你手动处理"}</span></span><input id="auto-enabled" type="checkbox" role="switch" aria-label="开启自动清理" ${policy.enabled ? "checked" : ""}></label>
      <div id="auto-warning" class="auto-warning"${policy.enabled ? "" : " hidden"}><strong>到期后彻底删除，无法恢复</strong><p>现有已到期的资料也会清理。退出期间到期的资料，会在下次打开时检查。</p></div>
      <div class="auto-scope">
        <div><span>清理内容</span><p>最近删除中的文稿、音频记录及修改历史</p></div>
        <div><span>原文件</span><p>原音频文件和独立备份保留</p></div>
        <div><span>会话</span><p>在会话列表中单独彻底删除，不进入最近删除</p></div>
      </div>
      <p id="auto-error" class="auto-error" role="alert" hidden></p>
      <div class="dialog-actions"><button id="auto-cancel" class="secondary">取消</button><button id="auto-confirm" class="primary" disabled>保存设置</button></div>`);
    modal.classList.add("auto-cleanup-modal");
    modal.addEventListener("close", () => modal.classList.remove("auto-cleanup-modal"), {once:true});
    const toggle = $("auto-enabled"), confirm = $("auto-confirm"), cancel = $("auto-cancel"), close = $("dialog-close");
    let busy = false;
    const preventCancel = event => { if (busy) event.preventDefault(); };
    modal.addEventListener("cancel", preventCancel);
    modal.addEventListener("close", () => modal.removeEventListener("cancel", preventCancel), {once:true});
    toggle.onchange = () => {
      $("auto-description").textContent = toggle.checked ? "删除 7 天后自动清理" : "一直保留，由你手动处理";
      $("auto-warning").hidden = !toggle.checked;
      confirm.disabled = toggle.checked === Boolean(policy.enabled);
      confirm.className = toggle.checked ? "danger-button" : "primary";
      confirm.textContent = toggle.checked ? "开启并清理到期资料" : "关闭自动清理";
      $("auto-error").hidden = true;
    };
    bind("auto-cancel", () => modal.close());
    bind("auto-confirm", async () => {
      if (busy || toggle.checked === Boolean(policy.enabled)) return;
      const enabling = toggle.checked;
      busy = true;
      [toggle, confirm, cancel, close].forEach(control => control.disabled = true);
      confirm.textContent = "正在保存…";
      try {
        await rpc("trash_policy", {enabled:enabling});
      } catch (e) {
        busy = false;
        [toggle, confirm, cancel, close].forEach(control => control.disabled = false);
        confirm.textContent = enabling ? "开启并清理到期资料" : "关闭自动清理";
        $("auto-error").textContent = "设置未保存，请重试。" + (e.message || "");
        $("auto-error").hidden = false;
        return;
      }
      close.disabled = false;
      modal.close();
      let result = {deleted:[], failures:[]}, cleanupError = "";
      if (enabling) {
        try { result = await rpc("cleanup_trash"); }
        catch (e) { cleanupError = "设置已保存，清理检查未完成，请稍后重试。"; }
      }
      const feedback = cleanupError || (result.failures.length ? `${result.failures.length} 项未能清理，资料已保留，请选择后重试。` : "");
      if (state.page === "trash") await showTrash({statusMessage:feedback});
      toast(feedback || (enabling ? `自动清理已开启，保留 7 天${result.deleted.length ? `；已清理 ${result.deleted.length} 项` : ""}。` : "自动清理已关闭，资料继续保留。"));
    });
  });
  if (!rows.length) return;

  const list = $("trash-list");
  const active = () => state.page === "trash" && list?.isConnected;
  const itemButtons = [...document.querySelectorAll("[data-trash]")];
  let serial = 0, busy = false;
  const visibleItems = () => itemButtons.filter(button => !button.hidden);
  const setStatus = message => {
    const status = $("trash-status");
    if (!status) return;
    status.textContent = message || "";
    status.hidden = !message;
  };
  const updateSelection = () => {
    if (!active()) return;
    if (selectionMode) list.setAttribute("aria-multiselectable", "true");
    else list.removeAttribute("aria-multiselectable");
    itemButtons.forEach(button => {
      const isSelected = selectionMode ? selectedTokens.has(button.dataset.trash) : previewToken === button.dataset.trash;
      button.setAttribute("aria-selected", String(isSelected));
      button.dataset.previewed = String(previewToken === button.dataset.trash);
      button.disabled = busy;
    });
    const visible = visibleItems();
    $("trash-count").textContent = selectionMode
      ? `已选 ${selectedTokens.size} 项 · 显示 ${visible.length} / ${rows.length} 项`
      : `显示 ${visible.length} / ${rows.length} 项`;
    const selectVisible = $("trash-select-visible");
    selectVisible.hidden = false;
    selectVisible.disabled = busy || visible.length === 0;
    const allVisibleSelected = visible.length > 0 && visible.every(button => selectedTokens.has(button.dataset.trash));
    selectVisible.textContent = allVisibleSelected ? "取消选择当前结果" : "全选当前结果";
    $("trash-select-mode").textContent = selectionMode ? "取消选择" : "选择";
    $("trash-select-mode").setAttribute("aria-pressed", String(selectionMode));
    $("trash-select-mode").disabled = busy;
    $("trash-search").disabled = busy;
    const restore = $("trash-restore");
    const targetCount = selectionMode ? selectedTokens.size : Number(Boolean(previewToken));
    restore.disabled = busy || targetCount === 0;
    $("trash-purge").disabled = busy || targetCount === 0;
    $("trash-purge").textContent = targetCount > 1 ? `彻底删除 · ${targetCount} 项` : "彻底删除";
    $("trash-auto").disabled = busy;
    restore.textContent = selectionMode ? `恢复所选${targetCount ? ` · ${targetCount} 项` : ""}` : "恢复到素材库";
    restore.setAttribute("aria-label", selectionMode && targetCount ? `恢复所选 ${targetCount} 项` : "恢复到素材库");
  };
  const showPreviewMessage = (title, message) => {
    $("trash-preview-empty-title").textContent = title;
    $("trash-preview-empty-copy").textContent = message;
    $("trash-preview-empty").hidden = false;
    $("trash-text").hidden = true;
  };
  const preview = async token => {
    previewToken = token || null;
    const generation = ++serial;
    updateSelection();
    setStatus("");
    $("trash-title").textContent = token ? "正在读取…" : "选择资料查看";
    $("trash-meta").textContent = "";
    showPreviewMessage(token ? "正在载入预览" : "先选中一项", token ? "正在读取保留的内容。" : "预览能帮助你确认内容。即使预览暂时不可用，也可以尝试恢复。");
    if (!token) return;
    try {
      const value = await rpc("trash", {token});
      if (!active() || serial !== generation) return;
      const kind = value.kind === "voice" ? "音频" : "文稿";
      const item = itemButtons.find(button => button.dataset.trash === token);
      if (item) {
        item.querySelector(".trash-kind").innerHTML = `${icon(value.kind === "voice" ? "audio" : "doc")}${kind}`;
      }
      $("trash-title").textContent = value.title || "未命名资料";
      $("trash-meta").textContent = `${kind}${value.kind === "voice" ? " · 生成时的文字" : ""} · 删除于 ${date(value.deleted_at) || "时间未知"}`;
      const text = value.text || "";
      if (text) {
        renderMarkdownPreview($("trash-text"), text);
        $("trash-text").hidden = false;
        $("trash-preview-empty").hidden = true;
      } else {
        showPreviewMessage("这份资料没有可预览的文字", "恢复后仍可在原来的素材库中打开资料。");
      }
    } catch {
      if (!active() || serial !== generation) return;
      $("trash-title").textContent = "暂时无法预览";
      $("trash-meta").textContent = "资料仍保留在最近删除中";
      showPreviewMessage("预览没有加载出来", "你仍可以尝试恢复这份资料。恢复遇到冲突时，原件会留在这里。 ");
    }
    updateSelection();
  };
  const filterItems = (loadPreview = true) => {
    const q = $("trash-search").value.trim().toLocaleLowerCase();
    itemButtons.forEach(button => button.hidden = !button.dataset.search.includes(q));
    const visible = visibleItems();
    for (const token of selectedTokens) if (!visible.some(b => b.dataset.trash === token)) selectedTokens.delete(token);
    if (!visible.some(b => b.dataset.trash === anchorToken)) anchorToken = null;
    $("trash-no-results").hidden = visible.length !== 0;
    const previewedItem = itemButtons.find(button => button.dataset.trash === previewToken);
    if ((!previewToken && visible.length) || previewedItem?.hidden) {
      const nextToken = visible[0]?.dataset.trash || null;
      if (loadPreview) preview(nextToken);
      else { previewToken = nextToken; updateSelection(); }
    } else updateSelection();
  };

  itemButtons.forEach(button => {
    button.onmousedown = e => { if (e.shiftKey || e.metaKey || e.ctrlKey) e.preventDefault(); };
    button.onclick = async event => {
      if (busy) return;
      const token = button.dataset.trash, additive = event.metaKey || event.ctrlKey;
      if ((event.shiftKey || additive) && !selectionMode) {
        selectionMode = true;
        if (previewToken) selectedTokens.add(previewToken);
        anchorToken ||= previewToken;
      }
      if (event.shiftKey) {
        const visible = visibleItems().map(b => b.dataset.trash);
        const end = visible.indexOf(token), start = Math.max(0, visible.indexOf(anchorToken || token));
        if (!additive) selectedTokens.clear();
        visible.slice(Math.min(start,end), Math.max(start,end)+1).forEach(t => selectedTokens.add(t));
        anchorToken ||= token;
      } else {
        if (selectionMode) selectedTokens.has(token) ? selectedTokens.delete(token) : selectedTokens.add(token);
        anchorToken = token;
      }
      await preview(token);
    };
  });
  $("trash-search").oninput = filterItems;
  $("trash-select-mode").onclick = () => {
    if (selectionMode) selectedTokens.clear();
    else if (previewToken) selectedTokens.add(previewToken);
    selectionMode = !selectionMode;
    updateSelection();
  };
  $("trash-select-visible").onclick = () => {
    if (busy) return;
    selectionMode = true;
    const visible = visibleItems();
    const allSelected = visible.length > 0 && visible.every(button => selectedTokens.has(button.dataset.trash));
    visible.forEach(button => allSelected ? selectedTokens.delete(button.dataset.trash) : selectedTokens.add(button.dataset.trash));
    updateSelection();
  };
  const runTrashAction = async (operation, targets) => {
    if (!targets.length || busy) return;
    const purging = operation === "purge_deleted", verb = purging ? "彻底删除" : "恢复";
    busy = true;
    serial++;
    updateSelection();
    const restored = [], failures = [];
    for (const [index, token] of targets.entries()) {
      setStatus(`正在${verb} ${index + 1} / ${targets.length} 项…`);
      try {
        await rpc(operation, {token, ...(purging ? {confirmed:true} : {})});
        restored.push(token);
      } catch (e) {
        failures.push({token, message: e?.message || String(e)});
      }
    }
    const summary = failures.length
      ? `已${verb} ${restored.length} 项。${failures.length} 项仍在最近删除中，可以选择后重试。${failures[0].message}`
      : `已${verb} ${restored.length} 项${purging ? "。" : "，资料已回到原来的素材库。"}`;
    const searchValue = $("trash-search")?.value || query;
    if (!active()) {
      await refresh();
      toast(summary);
      return;
    }
    try {
      await showTrash({query: searchValue, selectionMode: failures.length > 0, selectedTokens: failures.map(item => item.token), previewToken: failures[0]?.token, statusMessage: summary});
      await refresh();
    } catch (e) {
      busy = false;
      updateSelection();
      error(e);
    }
  };
  const targets = () => selectionMode ? [...selectedTokens] : previewToken ? [previewToken] : [];
  $("trash-restore").onclick = () => runTrashAction("restore_deleted", targets()).catch(error);
  $("trash-purge").onclick = () => {
    const chosen = targets();
    if (busy || !chosen.length) return;
    const titles = chosen.map(token => rows.find(row => row.token === token)?.title || "未命名资料");
    dialog(`彻底删除 ${chosen.length} 项资料？`, `<p>将删除这些资料的记录、正文和修改历史，无法从最近删除恢复。原音频文件和独立备份保留；会话需在会话列表中单独删除。</p><ul>${titles.map(title => `<li>${esc(title)}</li>`).join("")}</ul><div class="dialog-actions"><button id="purge-cancel" class="secondary">取消</button><button id="purge-confirm" class="danger-button">彻底删除 ${chosen.length} 项</button></div>`);
    bind("purge-cancel", () => $("dialog").close());
    bind("purge-confirm", async () => { $("dialog").close(); await runTrashAction("purge_deleted", chosen); });
  };
  filterItems(false);
  updateSelection();
  if (previewToken) await preview(previewToken);
  if (options.statusMessage) setStatus(options.statusMessage);
}
async function showModels() {
  await flush();
  await native('models');
}

async function native(action, args = {}) {
  return new Promise((resolve, reject) => {
    const id = uid();
    pending.set(id, { resolve, reject });
    bridge.nativeAction(id, action, JSON.stringify(args));
  });
}
async function chooseMedia() {
  const cid = state.chat?.id;
  if (!cid) return;
  await flush();
  const plan = await native("choose_media", { conversation_id: cid });
  if (!plan) return;
  // The plan remains bound to the originating chat even if navigation happened
  // while the native picker or the Core readiness check was open.
  dialog("转录为新文稿", `<p><b>${esc(plan.filename)}</b></p><p>${esc(plan.message)}</p><p class="muted">${(plan.bytes / 1024 / 1024).toFixed(1)} MB · 当前模型：SenseVoice<br>需要其他模型时，可前往文案提取工作台选择。</p><div class="dialog-actions"><button id="media-back" class="text-button">返回</button><button id="media-start" class="primary">开始本地转录</button></div>`);
  bind("media-back", () => $("dialog").close());
  bind("media-start", async () => {
    const button = $("media-start");
    button.disabled = true;
    try {
      const r = await rpc("start_media", { plan_id: plan.plan_id, expected_hash: plan.plan_hash });
      $("dialog").close();
      mediaStates.set(r.job_id, r);
      await reloadChat(cid);
      watchMedia(r);
    } catch (e) { button.disabled = false; throw e; }
  });
}
function mediaMarkup(jid) {
  const r = mediaStates.get(jid);
  if (!r) return '<div class="muted">正在读取任务状态…</div>';
  const active = ["prepared", "queued", "running", "cancel_requested"].includes(r.state);
  const label = { prepared: "准备中", queued: "等待处理", running: `正在转录 · ${r.progress}%`, cancel_requested: "正在停止…", cancelled: "已停止", failed: "转录未完成", recovery_required: r.recoverable ? "转录结果已保留，等待存入文稿库" : "任务已中断，未自动重新转录", succeeded: "已存入文稿库" }[r.state];
  return `<div class="result-actions" data-media-job="${esc(jid)}"><span class="muted">${esc(label)}</span>${active && r.state !== "cancel_requested" ? `<button class="secondary" data-cancel-media="${esc(jid)}">停止</button>` : ""}${r.recoverable ? `<button class="secondary" data-recover-media="${esc(jid)}">恢复保存</button>` : ""}${r.document_id ? `<button class="secondary" data-media-document="${esc(r.document_id)}">打开新文稿</button>` : ""}</div>`;
}
async function restoreMedia() {
  const cid = state.chat?.id;
  if (!cid) return;
  const rows = await rpc("media_tasks", { conversation_id: cid });
  for (const r of rows) {
    mediaStates.set(r.job_id, r);
    if (["prepared", "queued", "running", "cancel_requested"].includes(r.state)) watchMedia(r);
  }
  if (state.chat?.id === cid) renderMessages();
}
async function watchMedia(initial) {
  if (mediaWatching.has(initial.job_id)) return;
  mediaWatching.set(initial.job_id, true);
  try {
    while (true) {
      const r = await rpc("media_status", { job_id: initial.job_id });
      mediaStates.set(r.job_id, r);
      if (r.conversation_id === state.chat?.id) {
        await reloadChat(r.conversation_id);
        if (r.conversation_id === state.chat?.id) renderMessages();
      }
      if (!["prepared", "queued", "running", "cancel_requested"].includes(r.state)) {
        if (r.state === "succeeded") await refresh();
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  } catch (e) { error(e); }
  finally { mediaWatching.delete(initial.job_id); }
}
function audioClock(ms) {
  if (ms == null) return "--:--";
  const seconds = Math.max(0, Math.floor((ms || 0) / 1000));
  return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
}
function audioNote(id, text, owner = "file") {
  if (state.doc?.id !== id || !$("audio-status")) return;
  $("audio-status").textContent = text;
  $("audio-status").dataset.owner = owner;
}
async function audioFileAction(action, d) {
  if (audioOperations.has(d.id)) return;
  audioOperations.add(d.id);
  $("audio-export").disabled = $("audio-relink").disabled = true;
  audioNote(d.id, action === "audio_export" ? "正在导出…" : "请选择音频文件");
  try {
    const result = await native(action, { document_id: d.id });
    if (!result) { audioNote(d.id, d.missing_flags?.audio_missing ? "音频文件不在原位置，生成时的文字仍保留。" : ""); return; }
    if (action === "audio_relink") {
      const fresh = await rpc("audio", { document_id: d.id });
      if (state.doc?.id === d.id) { state.doc = fresh; renderAudio(); }
      await refresh();
      audioNote(d.id, "已重新定位，可以播放或导出");
    } else {
      audioNote(d.id, "已导出：" + result.filename);
      if (state.doc?.id !== d.id) toast("音频已导出：" + result.filename);
    }
  } catch (e) {
    audioNote(d.id, (action === "audio_export" ? "导出未完成，可重试。" : "未能重新定位，可重试。") + e.message);
  } finally {
    audioOperations.delete(d.id);
    if (state.doc?.id === d.id && $("audio-export")) {
      $("audio-export").disabled = Boolean(state.doc.missing_flags?.audio_missing);
      $("audio-relink").disabled = false;
    }
  }
}
function audioGenerationInfo(d) {
  const fields = [["生成模型", voiceModel(d)], ["音色", d.voice_name || "未记录"],
    ["生成设置", d.config_summary || "未记录"], ["生成时间", date(d.created_at)],
    ["来源文稿", d.source_title || (d.source_id ? "关联文稿" : "手动输入")],
    ["音频文件", d.audio_filename || "未记录"]];
  dialog("这份音频的生成信息", `<table class="detail-table">${fields.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table><p class="muted">下方文字保留生成当时的内容，修改当前文稿不会改变这份记录。</p>`);
}
function audioProgress(fraction) {
  const wave = $("audio-wave");
  if (!wave) return;
  const bars = wave.querySelectorAll("i");
  bars.forEach((bar, i) => bar.classList.toggle("played", fraction > 0 && i / bars.length < fraction));
}
function renderAudio() {
  const d = state.doc;
  const missing = Boolean(d.missing_flags?.audio_missing), busy = audioOperations.has(d.id);
  const format = audioFormat(d.audio_filename);
  $("detail").innerHTML = `<header class="detail-head">${icon("audio")}<span class="detail-folder" title="${esc(folderName(d.folder_id))}">${esc(folderName(d.folder_id))}</span><span class="grow"></span><button id="audio-export" class="text-button" ${missing || busy ? "disabled" : ""}>${icon("export")} 导出</button></header>
    <div class="document-scroll"><article class="document audio-document"><div class="document-kicker">音频作品</div><h1 class="doc-title">${esc(d.title)}</h1>
    <div class="doc-meta"><span>${esc(date(d.created_at))}</span><span>·</span><span>${esc(d.voice_name || "未记录音色")}</span><span>·</span><button id="audio-info">${icon("link")} 生成信息</button><span class="grow"></span><span>${esc(format)} · 本地文件</span></div>
    <div id="audio-wave" class="audio-art" role="img" aria-label="正在读取真实波形">${missing ? "音频文件不在原位置" : "正在读取真实波形…"}</div>
    <div class="audio-controls"><button id="audio-play" class="play-main" aria-label="播放" title="播放" ${missing ? "disabled" : ""}>${icon("play")}</button><input id="audio-seek" type="range" min="0" max="1000" value="0" disabled aria-label="播放进度"><span id="audio-time">00:00 / ${audioClock(d.duration_seconds == null ? null : d.duration_seconds * 1000)}</span></div>
    <div class="audio-feedback"><span id="audio-status" role="status" data-owner="file">${missing ? "音频文件不在原位置，生成时的文字仍保留。" : ""}</span><button id="audio-relink" class="text-button" ${missing ? "" : "hidden"} ${busy ? "disabled" : ""}>重新定位</button></div>
    <div class="audio-source"><h3>生成时的文字</h3><div class="source-text">${esc(d.source_text_snapshot ?? d.text ?? "")}</div><button id="audio-source" class="secondary" ${!d.source_id ? "hidden" : ""} ${d.missing_flags?.source_missing ? "disabled" : ""}>${d.missing_flags?.source_missing ? "来源文稿已移除" : "打开当前文稿"}</button></div></article></div>`;
  bind("audio-play", async () => {
    try { await native("audio_toggle", { document_id: d.id }); }
    catch (e) {
      audioNote(d.id, e.message);
      if (state.doc?.id === d.id) $("audio-relink").hidden = false;
    }
  });
  $("audio-seek").oninput = () => audioProgress(Number($("audio-seek").value) / 1e3);
  $("audio-seek").onchange = () => native("audio_seek", { document_id: d.id, fraction: Number($("audio-seek").value) / 1e3 }).catch(error);
  bind("audio-export", () => audioFileAction("audio_export", d));
  bind("audio-relink", () => audioFileAction("audio_relink", d));
  bind("audio-source", () => window.lulu.setKind("documents", d.source_id));
  bind("audio-info", () => audioGenerationInfo(d));
  native("audio_status", { document_id: d.id }).then(window.lulu.audioState).catch(error);
  native("audio_waveform", { document_id: d.id }).then(window.lulu.waveformState).catch(() => {
    window.lulu.waveformState({ id: d.id, state: "failed", description: "暂时无法读取波形，仍可试听完整文件", peaks: [] });
  });
}
function resize() {
  const w = innerWidth + (innerWidth <= 1024 ? 176 : 232), compact2 = w <= 1220;
  $("app").classList.toggle("compact", compact2);
  let list = compact2 ? 276 : w >= 1680 ? 380 : 340, agent = compact2 ? 308 : w >= 1680 ? 380 : 344;
  const widths = state.widths;
  if (widths) {
    list = widths.list;
    agent = widths.agent;
  }
  const minimum = compact2 ? 350 : 420;
  list = Math.max(260, Math.min(list, 500, innerWidth - (state.kind === "documents" ? agent + 10 : 5) - minimum));
  agent = Math.max(280, Math.min(agent, 520, innerWidth - list - 10 - minimum));
  $("workspace").style.setProperty("--list-width", list + "px");
  $("workspace").style.setProperty("--agent-width", agent + "px");
}
function divider(id, side) {
  const node = $(id);
  let drag;
  node.onpointerdown = (e) => {
    drag = { x: e.clientX, list: $("list-pane").getBoundingClientRect().width, agent: $("agent-column").getBoundingClientRect().width };
    node.setPointerCapture(e.pointerId);
  };
  node.onpointermove = (e) => {
    if (!drag) return;
    state.widths = { list: drag.list + (side === "list" ? e.clientX - drag.x : 0), agent: drag.agent + (side === "agent" ? drag.x - e.clientX : 0) };
    resize();
  };
  const finish = () => {
    if (drag) {
      drag = null;
      rpc("view_state", { key: "columns", value: state.widths }).catch(error);
    }
  };
  node.onpointerup = node.onpointercancel = finish;
  node.onkeydown = (e) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    state.widths = { list: $("list-pane").offsetWidth, agent: $("agent-column").offsetWidth };
    state.widths[side] += e.key === "ArrowRight" ? 10 : -10;
    resize();
    rpc("view_state", { key: "columns", value: state.widths }).catch(error);
  };
}
async function loadModels(prefetched, context) {
  const cid = state.chat?.id;
  if (!cid || !$("model")) return;
  let result = prefetched || await rpc("connections");
  if (cid !== state.chat?.id || !$("model")) return;
  const button = $("model"), menu = $("model-menu");
  let request = 0;
  const close = (focus = false) => {
    request++;
    menu.hidden = true;
    menu.removeAttribute("aria-busy");
    button.setAttribute("aria-expanded", "false");
    if (focus) button.focus();
  };
  const describe = () => {
    const selected = result.find(c => c.id === state.chat?.connection_id);
    button.innerHTML = `<span>${esc(selected?.label || "选择模型")}</span>${icon("chevron")}`;
    button.title = selected?.tools_supported === false ? "支持对话和改稿；采集、转录、配音请进入对应工作台" : selected?.label || "选择当前会话使用的模型";
  };
  const select = async (id) => {
    close(true);
    button.disabled = true;
    try {
      const chat = await rpc("select_connection", { conversation_id: cid, connection_id: id });
      if (cid !== state.chat?.id) return;
      state.chat = chat;
      describe();
      await updateContext();
    } catch (e) { error(e); }
    finally { if (cid === state.chat?.id) updateRunning(); }
  };
  describe();
  const render = (loading = false) => {
    const items = loading ? [] : result;
    menu.innerHTML = `<div class="model-menu-heading">会话模型</div>${items.length > 5 ? '<input id="model-search" class="model-search" placeholder="搜索模型" aria-label="搜索已接入模型">' : ''}<div class="model-options" role="listbox" aria-label="已接入模型">${items.map(c => `<button class="model-option" role="option" aria-selected="${c.id === state.chat.connection_id}" data-connection="${esc(c.id)}"><span class="model-option-info"><b>${esc(c.label)}</b><small>${esc(c.model)}${c.tools_supported === false ? " · 仅文字" : ""}</small></span><span class="model-check">${c.id === state.chat.connection_id ? "✓" : ""}</span></button>`).join('') || `<div class="model-empty" role="status">${loading ? "正在读取已接入模型…" : "还没有配置模型"}</div>`}</div><button id="model-settings" class="model-settings">${icon("plus")} 管理云端模型</button>`;
    menu.querySelectorAll('[data-connection]').forEach(node => node.onclick = () => select(node.dataset.connection));
    $("model-settings").onclick = () => { close(); native("models").catch(error); };
    if ($("model-search")) $("model-search").oninput = e => {
      const query = e.target.value.toLowerCase();
      menu.querySelectorAll('[data-connection]').forEach(node => node.hidden = !node.textContent.toLowerCase().includes(query));
    };
    ($( "model-search") || menu.querySelector('[aria-selected="true"]') || menu.querySelector('button'))?.focus();
  };
  button.onclick = async () => {
    if (!menu.hidden) { close(); return; }
    if ($("composer-popover")) $("composer-popover").hidden = true;
    menu.hidden = false;
    menu.setAttribute("aria-busy", "true");
    button.setAttribute("aria-expanded", "true");
    const generation = ++request;
    render(true);
    try {
      const choices = await rpc("connections");
      if (generation !== request || cid !== state.chat?.id || button !== $("model") || menu.hidden) return;
      result = choices;
      menu.removeAttribute("aria-busy");
      render();
    } catch (e) {
      if (generation !== request || cid !== state.chat?.id || button !== $("model") || menu.hidden) return;
      close(true);
      error(e);
    }
  };
  menu.onkeydown = e => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      const items = [...menu.querySelectorAll('button:not([hidden])')], current = items.indexOf(document.activeElement);
      const next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (current + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  };
  button.onkeydown = e => { if (["ArrowDown", "ArrowUp"].includes(e.key)) {e.preventDefault(); button.click();} };
  menu.onfocusout = e => { if (!button.parentElement.contains(e.relatedTarget)) close(); };
  await updateContext(context);
}
async function updateContext(prefetched) {
  const cid = state.chat?.id;
  if (!cid || !$("context-percent")) return;
  const c = prefetched || await rpc("context", { conversation_id: cid });
  if (cid !== state.chat?.id || !$("context-percent")) return;
  $("context-percent").textContent = c.percent == null ? "" : c.percent + "%";
  $("context-ring").style.setProperty("--used", (c.percent || 0) + "%");
  $("context-details").textContent = c.percent == null ? "选择模型后预估" : `预估已用 ${c.percent}% · 剩余 ${100 - c.percent}%`;
  $("context-meter").style.width = (c.percent || 0) + "%";
  $("context-threshold").textContent = `达到 ${c.threshold || 80}% 自动压缩`;
}
function runningFor(cid) {
  return [...watching.values()].find((r) => r.conversation_id === cid && ["queued", "running", "cancel_requested"].includes(r.state));
}
async function queueFollowup() {
  const cid = state.chat?.id, run = runningFor(cid), input = $("prompt");
  if (!cid || !run || state.queuedFollowup || !input?.value.trim()) return;
  await flushDraft();
  if (state.chat?.id !== cid || runningFor(cid)?.job_id !== run.job_id) return;
  state.queuedFollowup = {
    job_id: run.job_id,
    request_id: uid(),
    text: input.value.trim(),
    references: structuredClone(state.refs),
    reference_locations: structuredClone(state.refLocations || {}),
    next_reference: state.nextRef,
    options: structuredClone(state.options || {})
  };
  input.value = "";
  state.refs = [];
  state.refLocations = {};
  referenceGeneration++;
  state.nextRef = 1;
  draftChanged();
  renderReferences();
  updateRunning();
  await flushDraft();
  toast("已排入下一轮。当前回复结束后会自动发送；若本轮暂停或失败，补充内容会保留供你确认。");
}
async function cancelQueuedFollowup() {
  const queued = state.queuedFollowup;
  if (!queued) return;
  state.queuedFollowup = null;
  $("prompt").readOnly = false;
  if (!$("prompt").value.trim()) {
    $("prompt").value = queued.text;
    state.refs = queued.references || [];
    state.refLocations = queued.reference_locations || {};
    state.nextRef = queued.next_reference || Math.max(0, ...state.refs.map((r) => r.label_number)) + 1;
    state.options = queued.options || {};
  }
  draftChanged();
  renderReferences();
  updateRunning();
  await flushDraft();
  $("prompt").focus();
}
async function sendQueuedFollowup() {
  const queued = state.queuedFollowup, cid = state.chat?.id;
  if (!queued || !cid || runningFor(cid) || submitting.has(cid)) return;
  state.queuedFollowup = null;
  $("prompt").readOnly = false;
  $("prompt").value = queued.text;
  state.refs = queued.references || [];
  state.refLocations = queued.reference_locations || {};
  state.nextRef = queued.next_reference || Math.max(0, ...state.refs.map((r) => r.label_number)) + 1;
  state.options = queued.options || {};
  draftChanged();
  renderReferences();
  // Rebase the queued scope from the completed edit receipt before freezing
  // the retryable request. Manual/external edits still fail Core validation.
  state.outbox = {
    conversation_id: cid,
    request_id: queued.request_id,
    text: queued.text,
    references: structuredClone(state.refs),
    options: structuredClone(state.options)
  };
  updateRunning();
  await flushDraft();
  await send(true);
}
function renderQueuedFollowup(run) {
  const box = $("queued-followup"), input = $("prompt"), queueButton = $("queue-followup");
  if (!box || !input || !queueButton) return;
  const queued = state.queuedFollowup;
  if (queued) {
    const stillRunning = run?.job_id === queued.job_id;
    box.hidden = false;
    box.innerHTML = `<span>${stillRunning ? "已排入下一轮；当前回复结束后自动发送" : "补充内容已保留，可以作为下一条消息发送"}</span><button id="retrieve-queued-followup" class="text-button">取回编辑</button>${stillRunning ? "" : '<button id="send-queued-followup" class="text-button">发送补充</button>'}`;
    input.readOnly = true;
    queueButton.hidden = true;
    bind("retrieve-queued-followup", cancelQueuedFollowup);
    bind("send-queued-followup", sendQueuedFollowup);
  } else {
    box.hidden = true;
    box.innerHTML = "";
    input.readOnly = false;
    queueButton.hidden = !run || !input.value.trim();
  }
}
function updateRunning() {
  const run = runningFor(state.chat?.id);
  if (!$("send")) return;
  const sending = submitting.has(state.chat?.id);
  $("send").innerHTML = run ? "■" : sending ? '<span class="send-spinner" aria-hidden="true"></span>' : icon("arrow");
  $("send").setAttribute('aria-busy', String(sending));
  let status = $('chat-scroll')?.querySelector('[data-sending]');
  if (sending && !run && !status) {
    status = document.createElement('div'); status.dataset.sending = 'true';
    status.className = 'live-status'; status.setAttribute('role','status'); status.textContent = '正在发送…';
    $('chat-scroll').append(status);
  } else if (!sending || run) status?.remove();
  $("send").setAttribute("aria-label", run ? "停止当前任务" : sending ? "正在发送" : "发送，Enter；Shift Enter 换行");
  $("send").disabled = sending || run?.state === "cancel_requested";
  $("model").disabled = sending || !!run;
  $("context-ring").disabled = sending || !!run;
  $("prompt-menu").disabled = sending;
  $("agent-mode").disabled = sending;
  renderQueuedFollowup(run);
  if (sending) $('prompt').readOnly = true;
}
function awaitingHandoff(run) {
  return run?.state === "needs_input" || (run?.state === "succeeded" && run.result?.state === "needs_input");
}
async function restoreRuns() {
  const cid = state.chat?.id;
  if (!cid) return;
  const rows = await rpc("runs", { conversation_id: cid });
  for (const r of rows) if (["queued", "running", "cancel_requested"].includes(r.state)) watch(r.job_id, r);
  for (const r of rows) {
    if (awaitingHandoff(r)) settleRunStatus($("chat-scroll"), `${r.job_id}-assistant`, "waiting", "等待你完成上方选择");
    else if (r.state === "partial") settleRunStatus($("chat-scroll"), `${r.job_id}-assistant`, "paused", "本轮已暂停，进度已保留");
  }
  updateRunning();
  const queued = state.queuedFollowup;
  const previous = queued && rows.find((row) => row.job_id === queued.job_id);
  if (previous?.state === "succeeded" && !awaitingHandoff(previous) && !runningFor(cid)) await sendQueuedFollowup();
}
async function sendOrStop() {
  const run = runningFor(state.chat?.id);
  if (run) {
    const result = await rpc("cancel_run", { job_id: run.job_id });
    watching.set(run.job_id, result);
    updateRunning();
    return;
  }
  return send();
}
async function compact() {
  const cid = state.chat?.id;
  if (!cid || submitting.has(cid)) return;
  submitting.add(cid);
  try {
    await flushDraft();
    const r = await rpc("compact", { conversation_id: cid, request_id: uid() });
    await reloadChat(cid);
    watch(r.job_id, { ...r, conversation_id: cid });
  } finally {
    submitting.delete(cid);
  }
}
async function send(retryPending = false) {
  const cid = state.chat?.id;
  if (!cid || submitting.has(cid) || runningFor(cid) || (!state.outbox && !$("prompt").value.trim())) return;
  submitting.add(cid);
  closeSkillMenu();
  updateRunning();
  try {
    await flush();
    if (state.chat?.id !== cid) return;
    if (state.outbox && !retryPending && ($("prompt").value !== state.outbox.text || JSON.stringify(state.refs) !== JSON.stringify(state.outbox.references) || JSON.stringify(state.options || {}) !== JSON.stringify(state.outbox.options || {}))) {
      dialog("上一条发送尚未确认", `<p>输入框的新内容已保留。先用原编号确认上一条请求，避免重复调用。</p><pre class="source-text">${esc(state.outbox.text)}</pre><button id="retry-outbox" class="primary">确认上一条发送结果</button>`);
      bind("retry-outbox", () => {
        $("dialog").close();
        return send(true);
      });
      return;
    }
    const intent = state.outbox || { conversation_id: cid, request_id: uid(), text: $("prompt").value, references: structuredClone(state.refs), options: structuredClone(state.options || {}) };
    if (!state.outbox) {
      state.outbox = intent;
      draftChanged();
      await flushDraft();
    }
    let result;
    try {
      result = await rpc("send", intent);
    } catch (e) {
      if (["ValueError", "RevisionConflict"].includes(e.code) && state.chat?.id === cid) {
        state.outbox = null;
        draftChanged();
        await flushDraft();
      }
      throw e;
    }
    if (state.chat?.id === cid) {
      state.outbox = null;
      if ($("prompt").value === intent.text && JSON.stringify(state.refs) === JSON.stringify(intent.references) && JSON.stringify(state.options || {}) === JSON.stringify(intent.options || {})) {
        $("prompt").value = "";
        // Keep the visible selection scope for follow-up editing. Removing a chip explicitly returns to the whole document.
      }
      draftChanged();
      renderReferences();
      await flushDraft();
    }
    await reloadChat(cid);
    watch(result.job_id, result);
  } finally {
    submitting.delete(cid);
    updateRunning();
  }
}
async function watch(job_id, initial) {
  if (watching.has(job_id)) return;
  watching.set(job_id, initial || { job_id, state: "running" });
  let finalStatus = null;
  updateRunning();
  try {
    while (true) {
      const r = await rpc("run_status", { job_id });
      finalStatus = r;
      watching.set(job_id, r);
      const cid = r.conversation_id;
      if (cid === state.chat?.id) {
        const box = $("chat-scroll"), atBottom = box.scrollHeight - box.clientHeight - box.scrollTop < 48, top = box.scrollTop;
        await reloadChat(cid);
        if (cid === state.chat?.id) {
          const current = $("chat-scroll");
          let live = [...current.querySelectorAll("[data-live-job]")].find((el) => el.dataset.liveJob === job_id);
          if (["queued", "running", "cancel_requested"].includes(r.state)) {
            if (!live) {
              live = document.createElement("div");
              live.className = "chat-message assistant";
              live.dataset.liveJob = job_id;
              live.innerHTML = '<div class="message-by">Lulu</div><div class="message-text"></div><div class="live-status" role="status" aria-live="polite"></div>';
              current.append(live);
            }
            const message = live.querySelector(".message-text");
            renderMarkdownPreview(message, r.partial || "");
            const status = live.querySelector(".live-status");
            const phase = r.phase || (r.partial ? "streaming" : "preparing");
            const labels = {
              preparing: "正在准备本轮内容…",
              connecting: "正在连接模型…",
              waiting_first_token: "正在思考…",
              streaming: "正在回复…",
              processing: "正在处理本轮步骤…",
              compacting: "正在整理较早的交流…",
              finalizing: "正在整理结果…",
              stopping: "正在停止…"
            };
            status.dataset.phase = phase;
            status.textContent = labels[phase] || "正在处理…";
          } else {
            live?.remove();
            if (awaitingHandoff(r)) settleRunStatus(current, `${job_id}-assistant`, "waiting", "等待你完成上方选择");
            else if (r.state === "succeeded") settleRunStatus(current, `${job_id}-assistant`, "complete", "本轮已完成");
            else if (r.state === "partial") settleRunStatus(current, `${job_id}-assistant`, "paused", "本轮已暂停，进度已保留");
            else if (r.state === "failed") settleRunStatus(current, `${job_id}-status`, "error", "本轮出错");
            else if (r.state === "cancelled") settleRunStatus(current, `${job_id}-status`, "stopped", "本轮已停止");
          }
          current.scrollTop = atBottom ? current.scrollHeight : top;
        }
      }
      updateRunning();
      if (!["queued", "running", "cancel_requested"].includes(r.state)) {
        await syncConversationDocument(cid);
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
    await updateContext();
  } catch (e) {
    error(e);
  } finally {
    watching.delete(job_id);
    updateRunning();
    if (finalStatus?.state === "succeeded" && !awaitingHandoff(finalStatus) && finalStatus.conversation_id === state.chat?.id
        && state.queuedFollowup?.job_id === job_id) sendQueuedFollowup().catch(error);
  }
}
window.lulu = {
  notify: toast,
  editStatus: () => document.activeElement?.closest('.ProseMirror') ? editor?.editStatus?.() || {} : {},
  editAction: action => editor?.editAction?.(action), state, flush, ready: false, visible: value => { state.visible = value; if (!value) { closeMenu(); closeSkillMenu(); skillEntries = null; skillRequest = null; } }, showModels, setKind: (kind, id = null) => serializeTransition(async () => {
  await flush();
  if (kind === 'trash') return showTrash();
  if (kind === 'models') return showModels();
  const targetKind = kind === 'voice' ? 'voice' : 'documents';
  const changedKind = state.kind !== targetKind;
  let screen;
  if (changedKind || !state.doc || (id && id !== state.doc.id)) {
    const serial = ++querySerial;
    // Freeze old inputs after saving, and keep the old screen intact if the read fails.
    $('workspace').inert = true;
    $('workspace').setAttribute('aria-busy', 'true');
    try {
      screen = await rpc('library_view', {kind:targetKind, search:$('search').value,
        folder_id:$('folder').value || null, sort:$('sort').value, document_id:id});
      if (serial !== querySerial) return;
    } finally {
      $('workspace').inert = false;
      $('workspace').setAttribute('aria-busy', 'false');
    }
  }
  state.page = 'library';
  $('utility-page').hidden = true;
  $('utility-page').innerHTML = '';
  $('workspace').hidden = false;
  state.kind = targetKind;
  $('library-title').textContent = state.kind === 'voice' ? '音频库' : '文稿库';
  $('agent-column').hidden = $('ai-divider').hidden = state.kind === 'voice';
  $('new-doc').hidden = state.kind === 'voice';
  $('list-menu').setAttribute('aria-label', state.kind === 'voice' ? '音频库操作' : '文稿库操作');
  closeMenu(); closeSkillMenu(); resize();
  if (screen) {
    applyListing(screen.listing);
    await chooseNow(screen.document_id, screen.content);
    if (screen.warnings?.length) toast(screen.warnings.join(' '));
  } else {
    // Returning to the same page keeps the editor, selection, scroll and chat.
    await refresh();
  }
}), open: choose, refresh, trash: showTrash, audioState: (v) => {
  if (v.id === state.doc?.id && $("audio-seek")) {
    $("audio-seek").value = v.duration ? 1e3 * v.position / v.duration : 0;
    $("audio-seek").disabled = !v.duration || Boolean(v.error) || Boolean(state.doc.missing_flags?.audio_missing);
    $("audio-time").textContent = audioClock(v.position) + " / " + audioClock(v.duration || (state.doc.duration_seconds || 0) * 1000 || null);
    const playLabel = v.loading ? "载入中…" : v.playing ? "暂停" : "播放";
    $("audio-play").setAttribute("aria-label", playLabel);
    $("audio-play").title = playLabel;
    $("audio-play").innerHTML = icon(v.playing ? "pause" : "play");
    audioProgress(v.duration ? v.position / v.duration : 0);
    if (v.error) {
      audioNote(v.id, v.error, "player");
      $("audio-relink").hidden = false;
    } else if ($("audio-status").dataset.owner === "player") audioNote(v.id, "", "player");
  }
}, waveformState: (v) => {
  if (v.id !== state.doc?.id || !$("audio-wave")) return;
  const wave = $("audio-wave");
  wave.setAttribute("aria-label", v.description);
  wave.dataset.state = v.state;
  if (v.state === "ready" && v.peaks?.length) {
    const count = Math.min(110, v.peaks.length);
    wave.innerHTML = Array.from({ length: count }, (_, i) => {
      const amplitude = Math.max(...v.peaks.slice(Math.floor(i * v.peaks.length / count), Math.floor((i + 1) * v.peaks.length / count)));
      return `<i aria-hidden="true" style="height:${Math.max(2, Math.min(1, amplitude) * 110)}px"></i>`;
    }).join("");
    audioProgress(Number($("audio-seek").value) / 1e3);
  } else wave.textContent = v.description;
}, prepareClose: async () => {
  try {
    await flush();
    bridge.closeReady(true);
  } catch (e) {
    error(e);
    bridge.closeReady(false);
  }
} };
new QWebChannel(qt.webChannelTransport, async (channel) => {
  bridge = channel.objects.workspace;
  bridge.initialized(false);
  bridge.reply.connect((id, payload) => {
    const p = pending.get(id);
    if (!p) return;
    pending.delete(id);
    const result = JSON.parse(payload);
    result.ok ? p.resolve(result.value) : p.reject(Object.assign(Error(result.message), { code: result.code }));
  });
  bridge.command.connect((action, payload) => {
    const args = JSON.parse(payload);
    if (action === "audioState" || action === "waveformState") {
      window.lulu[action](args);
      return;
    }
    commands = commands.then(() => window.lulu[action]?.(...Array.isArray(args) ? args : [])).catch(error);
  });
  $("search-icon").innerHTML = icon("search");
  $("new-doc").innerHTML = icon("plus");
  $("list-menu").innerHTML = icon("more");
  bind("dialog-close", () => $("dialog").close());
  bind("clear-selection", () => choose(null));
  bind("new-doc", async () => {
    await flush();
    const d = await rpc("create_document", { request_id: uid() });
    await refresh();
    await choose(d.document_id);
    $("doc-title").focus();
  });
  bind("list-menu", listMenu);
  bind("select-all", selectAllResults);
  bind("select-mode", () => {
    state.batch = !state.batch;
    state.selected.clear();
    selectionAnchor = state.doc?.id || null;
    renderRows();
    renderBatch();
  });
  $("search").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refresh(false, false).catch(error), 180);
  };
  observeSelects();
  $("folder").onchange = $("sort").onchange = () => refresh(false, false).catch(error);
  $("ask-selection").onmousedown = (e) => e.preventDefault();
  bind("ask-selection", addReference);
  divider("divider", "list");
  divider("ai-divider", "agent");
  window.onresize = resize;
  document.addEventListener("pointerdown", e => {
    if (!e.target.closest('#composer-popover, #prompt-menu') && !(skillMenuSource === 'slash' && e.target.closest('#prompt'))) closeSkillMenu();
    if (!e.target.closest('.model-control') && $("model-menu")) {
      $("model-menu").hidden = true;
      $("model").setAttribute("aria-expanded", "false");
    }
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link) return;
    event.preventDefault();
    if (link.getAttribute('href').startsWith('#')) {
      document.getElementById(link.getAttribute('href').slice(1))?.scrollIntoView({block:'center'});
      return;
    }
    native('open_url', {url:link.href}).catch(error);
  });
  document.onkeydown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a" && !$("dialog").open && !e.target.closest('input,textarea,[contenteditable="true"]')) {
      if (state.page === "trash") { e.preventDefault(); if ($("trash-select-visible")?.textContent === "全选当前结果") $("trash-select-visible").click(); }
      else if (state.page === "library" && e.target.closest('#list-pane')) { e.preventDefault(); selectAllResults().catch(error); }
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "f" && !$("dialog").open) {
      e.preventDefault();
      (state.page === "trash" ? $("trash-search") : $("search"))?.focus();
    }
    if (e.key === "Escape") {
      $("selection-menu").hidden = true;
      if ($("model-menu")) { $("model-menu").hidden = true; $("model").setAttribute("aria-expanded", "false"); }
      closeSkillMenu();
    }
  };
  try {
    const initial = await rpc('bootstrap');
    if (initial.migration.errors.length) toast("部分旧资料暂时无法读取，原文件已保留。");
    state.widths = initial.columns;
    renderEmpty(); resize();
    applyListing(initial.screen.listing);
    await chooseNow(initial.screen.document_id, initial.screen.content);
    if (initial.screen.warnings?.length || initial.migration.phase === 'unavailable') {
      toast(initial.screen.warnings?.join(' ') || '部分旧资料登记暂不可用，已显示可读取的文稿。');
      if (!state.doc) {
        $('agent').innerHTML = '<div class="agent-empty"><p>原资料已保留，可选择文稿阅读。</p><button id="recover-workspace" class="secondary">资料保护与恢复</button></div>';
        bind('recover-workspace', showStorage);
      }
    }
    window.lulu.ready = state.ready = true;
    bridge.initialized(true);
    setInterval(() => checkExternalEdit().catch(error), 5e3);
    let cleaning = false;
    const cleanTrash = async () => {
      if (cleaning || $("dialog").open || (state.page === "trash" && $("trash-auto")?.disabled)) return;
      cleaning = true;
      try {
        const result = await rpc("cleanup_trash");
        if (result.deleted.length || result.failures.length) {
          const message = `自动清理 ${result.deleted.length} 项${result.failures.length ? `，${result.failures.length} 项失败，已保留待重试` : ""}`;
          if (state.page === "trash") await showTrash({statusMessage:result.failures.length ? message : ""});
          toast(message);
        }
      } finally { cleaning = false; }
    };
    cleanTrash().catch(error);
    setInterval(() => cleanTrash().catch(error), 60000);
    bridge.ready();
  } catch (e) {
    bridge.initialized(false);
    error(e);
    $("detail").innerHTML = '<div class="workspace-error">' + esc(e.message) + '<p>原资料已保留。</p><p><button id="retry-workspace" class="secondary">重新读取文稿</button> <button id="recover-workspace" class="secondary">资料保护与恢复</button></p></div>';
    bind('retry-workspace', () => window.location.reload());
    bind("recover-workspace", showStorage);
    bridge.ready();
  }
});

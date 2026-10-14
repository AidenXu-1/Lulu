import { Editor, rootCtx, defaultValueCtx, editorViewCtx, serializerCtx, parserCtx } from "@milkdown/kit/core";
import { commonmark, imageSchema, bulletListSchema, orderedListSchema } from "@milkdown/kit/preset/commonmark";
import { gfm, extendListItemSchemaForTask } from "@milkdown/kit/preset/gfm";
import { listener, listenerCtx } from "@milkdown/kit/plugin/listener";
import { history } from "@milkdown/kit/plugin/history";
import { $prose } from "@milkdown/kit/utils";
import { closeHistory, undo, redo, undoDepth, redoDepth } from "@milkdown/kit/prose/history";
import { DOMSerializer } from "@milkdown/kit/prose/model";
import { EditorState, Plugin, TextSelection } from "@milkdown/kit/prose/state";
import "prosemirror-view/style/prosemirror.css";
let previewParser;
export function renderMarkdownPreview(root, markdown) {
  root.replaceChildren();
  root.classList.remove('readable-preview');
  if (!previewParser || envelope(markdown).source) { root.textContent = markdown; return; }
  try {
    const doc = previewParser(envelope(markdown).body);
    const fragment = DOMSerializer.fromSchema(doc.type.schema).serializeFragment(doc.content);
    // Previews are inert: no remote media loading or link navigation.
    fragment.querySelectorAll('img').forEach(img => img.replaceWith(document.createTextNode(img.alt || '图片')));
    fragment.querySelectorAll('a').forEach(link => link.removeAttribute('href'));
    root.append(fragment); root.classList.add('readable-preview');
  } catch { root.textContent = markdown; }
}
const bytes = (text) => new TextEncoder().encode(text).length;
function envelope(markdown, titleAliases = []) {
  const frontmatter = markdown.match(/^(?:\uFEFF)?---\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)(?:\r?\n|$)/)?.[0] || "";
  let body = markdown.slice(frontmatter.length), heading = "";
  const normalize = (value) => (value || "").replace(/\\([\\`*_[\]<>])/g, "$1").replace(/\.(mp3|wav|m4a|mp4|mov|flac|aac|ogg|webm|txt|md)$/i, "").trim();
  const first = body.match(/^# +([^\r\n]+)(?:\r?\n)+/);
  if (first && titleAliases.some((title) => normalize(title) === normalize(first[1]))) {
    heading = first[0];
    body = body.slice(heading.length);
  }
  const outsideCode = body.replace(/^\s*(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\s*\1\s*$/gm, "")
    .replace(/^<br\s*\/?>\r?$/gim, "");
  return { frontmatter, heading, body, crlf: markdown.includes("\r\n"), source: /<\/?[A-Za-z][A-Za-z0-9-]*(?:\s|\/?>)|<!--|^\[\^|^\s*:::|\$\$|\[\[/m.test(outsideCode) };
}
function sourceEditor(root, markdown, onChange, onSelection) {
  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = "此文稿含扩展语法，保留源码直接编辑。";
  const input = document.createElement("textarea");
  input.className = "editor-source";
  input.value = markdown;
  input.setAttribute("aria-label", "Markdown 源码，直接编辑");
  root.append(note, input);
  let composing = false;
  input.oncompositionstart = () => composing = true;
  input.oncompositionend = () => composing = false;
  input.oninput = () => onChange(input.value);
  input.onselect = onSelection;
  const range = (location) => {
    if (location?.kind !== "source" || !Number.isInteger(location.from) || !Number.isInteger(location.to) || location.from < 0 || location.to <= location.from || location.to > input.value.length) return null;
    return { canonical: input.value, start: bytes(input.value.slice(0, location.from)), end: bytes(input.value.slice(0, location.to)), location, text: input.value.slice(location.from, location.to), rect: input.getBoundingClientRect() };
  };
  return {
    destroy: async () => root.replaceChildren(),
    focus: () => input.focus(),
    isComposing: () => composing,
    markdown: () => input.value,
    characterCount: () => null,
    range,
    locate: (anchor) => {
      const next = range(anchor.location);
      if (!next || next.canonical !== anchor.canonical || next.start !== anchor.start || next.end !== anchor.end) return false;
      input.focus();
      input.setSelectionRange(anchor.location.from, anchor.location.to);
      input.scrollTop = Math.max(0, (input.value.slice(0, anchor.location.from).split("\n").length - 3) * (parseFloat(getComputedStyle(input).lineHeight) || 24));
      return true;
    },
    selection: () => input.selectionStart === input.selectionEnd ? null : range({ kind: "source", from: input.selectionStart, to: input.selectionEnd })
  };
}
async function createEditor(root, markdown, onChange, onSelection, titleAliases = []) {
  let { frontmatter, heading, body, crlf, source } = envelope(markdown, titleAliases);
  if (source) {
    return sourceEditor(root, markdown, onChange, onSelection);
  }
  // The legacy exported title remains in every saved/canonical Markdown value.
  // It is represented by the existing title field instead of a second body H1.
  const withEnvelope = (text) => frontmatter + heading + (crlf ? text.replace(/\r?\n/g, "\r\n") : text);
  let ready = false, composing = false, externalUpdate = false;
  const editor = await Editor.make().config((ctx) => {
    // Milkdown 7.19.2 list parsers pass string spread values into boolean
    // attributes. ProseMirror rejects the whole list, silently losing its text.
    for (const plugin of [bulletListSchema, orderedListSchema, extendListItemSchemaForTask]) {
      ctx.update(plugin.key, previous => context => {
        const schema = previous(context), item = plugin === extendListItemSchemaForTask;
        const ordered = plugin === orderedListSchema;
        return { ...schema,
          parseDOM: schema.parseDOM.map(rule => ({ ...rule, getAttrs: dom => {
            const attrs = rule.getAttrs?.(dom);
            return attrs === false ? false : { ...attrs, spread: dom.dataset.spread === 'true' };
          }})),
          ...(ordered ? { toDOM: node => ['ol', { start: node.attrs.order, 'data-spread': node.attrs.spread }, 0] } : {}),
          parseMarkdown: { ...schema.parseMarkdown, runner: (state, node, type) => {
            const attrs = { spread: node.spread === true };
            if (item) Object.assign(attrs, { label: node.label != null ? `${node.label}.` : '•',
              listType: node.label != null ? 'ordered' : 'bullet', checked: node.checked ?? null });
            if (ordered) attrs.order = node.start ?? 1;
            state.openNode(type, attrs).next(node.children).closeNode();
          }},
          toMarkdown: { ...schema.toMarkdown, runner: (state, node) => {
            const attrs = { spread: node.attrs.spread === true };
            if (item) attrs.checked = node.attrs.checked;
            else Object.assign(attrs, { ordered, ...(ordered ? { start: node.attrs.order } : {}) });
            state.openNode(item ? 'listItem' : 'list', undefined, attrs).next(node.content).closeNode();
          }}
        };
      });
    }
    // Markdown images without an optional title parse as null upstream, while
    // the image schema requires strings. Preserve the node instead of losing it.
    ctx.update(imageSchema.key, previous => context => {
      const schema = previous(context);
      return { ...schema, parseMarkdown: { ...schema.parseMarkdown,
        runner: (state, node, type) => state.addNode(type, {
          src: node.url || "", alt: node.alt || "", title: node.title || ""
        })
      } };
    });
    ctx.set(rootCtx, root);
    ctx.set(defaultValueCtx, body);
    ctx.get(listenerCtx).selectionUpdated(() => queueMicrotask(() => {
      if (ready && !composing) onSelection();
    }));
  }).use(commonmark).use(gfm).use(history).use(listener).use($prose((ctx) => new Plugin({
    view: () => ({ update: (view2, previous) => {
      if (ready && !externalUpdate && !view2.state.doc.eq(previous.doc)) onChange(withEnvelope(ctx.get(serializerCtx)(view2.state.doc)));
    } })
  }))).create();
  const view = editor.action((ctx) => ctx.get(editorViewCtx));
  previewParser = editor.action(ctx => ctx.get(parserCtx));
  // Keep only immutable parsed content, never another document's selection or
  // undo state. Every open still reads the authoritative document from Core.
  const parsedDocuments = new Map();
  let cachedCharacters = 0;
  function remember(body, doc) {
    if (body.length > 262144 || parsedDocuments.has(body)) return;
    while (parsedDocuments.size >= 4 || cachedCharacters + body.length > 524288) {
      const oldest = parsedDocuments.keys().next().value;
      cachedCharacters -= oldest.length;
      parsedDocuments.delete(oldest);
    }
    parsedDocuments.set(body, doc);
    cachedCharacters += body.length;
  }
  remember(body, view.state.doc);
  view.dom.setAttribute("aria-label", "文稿正文，直接编辑");
  view.dom.addEventListener("compositionstart", () => {
    composing = true;
  });
  view.dom.addEventListener("compositionend", () => {
    composing = false;
  });
  const range = (location) => editor.action((ctx) => {
    if (location?.kind !== "prosemirror" || !Number.isInteger(location.from) || !Number.isInteger(location.to)) return null;
    const { from, to } = location;
    if (from < 0 || to <= from || to > view.state.doc.content.size) return null;
    const nonce = crypto.randomUUID().replaceAll("-", "");
    const left = "LULUSTART" + nonce, right = "LULUEND" + nonce;
    const tr = view.state.tr.insertText(right, to).insertText(left, from);
    const serialize = (doc) => withEnvelope(ctx.get(serializerCtx)(doc)), canonical = serialize(view.state.doc), marked = serialize(tr.doc);
    const a = marked.indexOf(left), b = marked.indexOf(right);
    if (a < 0 || b < a) return null;
    const clean = marked.slice(0, a) + marked.slice(a + left.length, b) + marked.slice(b + right.length);
    if (clean !== canonical) return null;
    let sourceStart = a, sourceEnd = b - left.length;
    const first = view.state.doc.resolve(from), last = view.state.doc.resolve(to);
    // At a whole block edge, include its Markdown delimiters as well. Leaving
    // the opening ** outside a selected timestamp would corrupt a later edit.
    if (first.parent.isTextblock && from === first.start())
      sourceStart = canonical.lastIndexOf("\n", sourceStart - 1) + 1;
    if (last.parent.isTextblock && to === last.end()) {
      const newline = canonical.indexOf("\n", sourceEnd);
      sourceEnd = newline < 0 ? canonical.length : newline;
    }
    return {
      canonical,
      start: bytes(canonical.slice(0, sourceStart)),
      end: bytes(canonical.slice(0, sourceEnd)),
      text: view.state.doc.textBetween(from, to, "\n"),
      location,
      rect: view.coordsAtPos(to)
    };
  });
  ready = true;
  return {
    loadDocument: (markdown, aliases = []) => editor.action((ctx) => {
      const next = envelope(markdown, aliases);
      if (next.source || composing || view.composing) return false;
      const parsed = parsedDocuments.get(next.body) || ctx.get(parserCtx)(next.body);
      // New EditorState initializes fresh plugin state, including native undo.
      // Replacing only the text would allow undo to leak the previous manuscript.
      externalUpdate = true;
      try {
        view.updateState(EditorState.create({ schema: view.state.schema, doc: parsed, plugins: view.state.plugins }));
        ({ frontmatter, heading, body, crlf } = next);
        titleAliases = aliases;
        remember(body, parsed);
      } finally { externalUpdate = false; }
      return true;
    }),
    destroy: () => {
      ready = false;
      return editor.destroy();
    },
    replaceMarkdown: (next) => editor.action((ctx) => {
      const nextEnvelope = envelope(next, titleAliases);
      if (nextEnvelope.frontmatter !== frontmatter || nextEnvelope.heading !== heading || nextEnvelope.crlf !== crlf || nextEnvelope.source) return false;
      const parsed = ctx.get(parserCtx)(nextEnvelope.body);
      externalUpdate = true;
      try {
        view.dispatch(closeHistory(view.state.tr.replaceWith(0, view.state.doc.content.size, parsed.content)));
        view.dispatch(closeHistory(view.state.tr));
      } finally {
        externalUpdate = false;
      }
      return true;
    }),
    focus: () => view.focus(),
    editStatus: () => ({editor:true, undo:undoDepth(view.state) > 0, redo:redoDepth(view.state) > 0}),
    editAction: (action) => {
      view.focus();
      return action === 'undo' ? undo(view.state, view.dispatch) : action === 'redo' ? redo(view.state, view.dispatch) : false;
    },
    setTitle: (title) => {
      if (!heading || !title.trim()) return null;
      const newline = crlf ? "\r\n" : "\n";
      heading = "# " + title.replace(/[\r\n]+/g, " ").replace(/[\\`*_[\]<>]/g, "\\$&") + newline + newline;
      titleAliases = [...titleAliases, title];
      return editor.action((ctx) => withEnvelope(ctx.get(serializerCtx)(view.state.doc)));
    },
    isComposing: () => composing || view.composing,
    markdown: () => editor.action((ctx) => withEnvelope(ctx.get(serializerCtx)(view.state.doc))),
    characterCount: () => [...view.state.doc.textContent.replace(/\s/g, "")].length,
    range,
    locate: (anchor) => {
      const current = range(anchor.location);
      if (!current || current.canonical !== anchor.canonical || current.start !== anchor.start || current.end !== anchor.end) return false;
      view.focus();
      view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, anchor.location.from, anchor.location.to)).scrollIntoView());
      return true;
    },
    selection: () => {
      const { from, to, empty } = view.state.selection;
      if (empty || view.composing) return null;
      if (!view.state.doc.textBetween(from, to, "\n").trim()) return null;
      const selected = range({ kind: "prosemirror", from, to });
      return selected && { ...selected, ranges: [selected] };
    }
  };
}
export {
  createEditor
};

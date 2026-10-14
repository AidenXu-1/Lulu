// Examples match the editor's CommonMark + GFM presets. Preview HTML is fixed,
// inert content: no user input, images, external links or another editor instance.
export const markdownExamples = [
  {title:'标题', hint:'在行首输入 # 和空格。# 越多，标题层级越低。', source:'# 大标题\n\n## 章节标题\n\n### 小节标题', preview:'<h3>大标题</h3><h4>章节标题</h4><h5>小节标题</h5>'},
  {title:'粗体', hint:'用两个星号包住要强调的文字。', source:'这是 **重点内容**。', preview:'这是 <strong>重点内容</strong>。'},
  {title:'斜体', hint:'用一个星号包住文字。', source:'这是 *斜体文字*。', preview:'这是 <em>斜体文字</em>。'},
  {title:'删除线', hint:'用两个波浪号包住不再采用的内容。', source:'保留 ~~旧方案~~ 的修改痕迹。', preview:'保留 <s>旧方案</s> 的修改痕迹。'},
  {title:'无序列表', hint:'在行首输入短横线和空格。', source:'- 第一个要点\n- 第二个要点', preview:'<ul><li>第一个要点</li><li>第二个要点</li></ul>'},
  {title:'有序列表', hint:'在行首输入数字、英文句点和空格。', source:'1. 整理素材\n2. 开始写稿', preview:'<ol><li>整理素材</li><li>开始写稿</li></ol>'},
  {title:'待办清单', hint:'方括号里留空表示待办，填 x 表示完成。', source:'- [ ] 待完成\n- [x] 已完成', preview:'<div>☐ 待完成</div><div>☑ 已完成</div>'},
  {title:'引用', hint:'在行首输入 > 和空格。', source:'> 把灵感变成作品。', preview:'<blockquote>把灵感变成作品。</blockquote>'},
  {title:'链接', hint:'方括号内写名称，圆括号内写完整网址。', source:'[链接名称](https://example.com)', preview:'<span class="guide-link">链接名称</span>'},
  {title:'行内代码', hint:'用一对英文反引号包住短代码或命令。', source:'使用 `print("Hello")` 输出文字。', preview:'使用 <code>print("Hello")</code> 输出文字。'},
  {title:'代码块', hint:'用两行三个反引号包住代码，可在开头注明语言。', source:'```python\nprint("Hello")\n```', preview:'<pre>print("Hello")</pre>'},
  {title:'表格', hint:'用竖线分列，第二行的短横线分隔表头。', source:'| 名称 | 状态 |\n| --- | --- |\n| 文稿 | 完成 |', preview:'<table><thead><tr><th>名称</th><th>状态</th></tr></thead><tbody><tr><td>文稿</td><td>完成</td></tr></tbody></table>'},
  {title:'分隔线', hint:'单独一行写三个短横线，与上下段落各空一行。', source:'上面的内容\n\n---\n\n下面的内容', preview:'上面的内容<hr>下面的内容'},
  {title:'段落与换行', hint:'空一行分段。在同一段内换行，可按 Shift + Enter。', source:'第一段内容。\n\n第二段内容。', preview:'<p>第一段内容。</p><p>第二段内容。</p>'},
  {title:'显示符号本身', hint:'在 Markdown 符号前加反斜杠，按普通文字显示。', source:'\\*这段文字不会变成斜体\\*', preview:'*这段文字不会变成斜体*'},
];

export function showMarkdownGuide({dialog, esc, copy, onError}) {
  dialog('Markdown 语法指南', `<div class="markdown-guide">
    <div class="guide-toolbar"><p>对照示例输入，也可以复制写法。复制不会改动当前文稿。</p>
      <input id="guide-search" type="search" aria-label="查找 Markdown 语法" placeholder="查找语法，如标题、表格、列表" autocomplete="off"></div>
    <div class="guide-examples">${markdownExamples.map((entry, index) => `<section class="guide-entry" data-guide-entry="${index}">
      <header><h3>${esc(entry.title)}</h3><button type="button" class="secondary" data-copy-example="${index}" aria-label="复制${esc(entry.title)}示例">复制写法</button></header>
      <p>${esc(entry.hint)}</p><div class="guide-columns"><div><span class="guide-label">写法</span><pre tabindex="0" aria-label="${esc(entry.title)}的 Markdown 写法">${esc(entry.source)}</pre></div>
      <div><span class="guide-label">显示效果</span><div class="guide-preview">${entry.preview}</div></div></div>
    </section>`).join('')}</div>
    <p id="guide-empty" hidden>没有找到，试试“列表”“代码”或“链接”。</p>
    <aside class="guide-note"><strong>其他写法</strong><p>HTML、脚注、双链（如 [[文稿名]]）、数学公式等扩展语法会保留为源码，供你直接编辑。表格等复杂内容也可以通过导入 Markdown 文件使用。</p></aside>
    <span id="guide-feedback" role="status" aria-live="polite"></span>
  </div>`);
  const modal = document.getElementById('dialog');
  modal.classList.add('guide-modal');
  modal.addEventListener('close', () => modal.classList.remove('guide-modal'), {once:true});
  const search = document.getElementById('guide-search');
  search.oninput = () => {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    document.querySelectorAll('[data-guide-entry]').forEach(row => {
      const entry = markdownExamples[Number(row.dataset.guideEntry)];
      row.hidden = !`${entry.title} ${entry.hint} ${entry.source}`.toLowerCase().includes(query);
      if (!row.hidden) visible++;
    });
    document.getElementById('guide-empty').hidden = visible > 0;
  };
  const feedback = document.getElementById('guide-feedback');
  document.querySelectorAll('[data-copy-example]').forEach(button => {
    button.onclick = async () => {
      const entry = markdownExamples[Number(button.dataset.copyExample)];
      try {
        await copy(entry.source);
        feedback.textContent = `已复制${entry.title}写法`;
      } catch (error) {
        feedback.textContent = '未能复制，可以选中示例后按 ⌘C 复制。';
        onError(error);
      }
    };
  });
  search.focus();
}

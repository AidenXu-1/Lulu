"""Text-only shortcuts. Selecting a preset never grants tool permission."""
SYSTEM_PRESETS = [
    dict(id="proofread", title="错别字校正", hint="修正错字与标点，保留原意", icon="check", text="校正引用内容的错别字、漏字和明显标点错误。保留原意、专名、数字、语气与段落；有歧义的地方注明，别擅改。直接修正文稿，简要说明改动。"),
    dict(id="polish", title="口语化润色", hint="缩短绕口长句，保留个人表达", icon="spark", text="把引用内容润色为自然口语：缩短绕口长句，删掉重复和套话。保留事实、数字、关键观点与个人语气，不补编情节。直接修改文稿，保留个人表达。"),
    dict(id="voice", title="生成配音", hint="使用配音页已选的模型与音色", icon="mic", text="用配音页已选的模型与音色朗读引用内容，完成后存入音频库。配置未就绪时说明缺少什么，不擅自替换模型或音色。"),
    dict(id="translate", title="文案翻译", hint="默认英文，发送前可换成其他语言", icon="doc", text="Translate the referenced text into natural English. Preserve meaning, tone, names, numbers, and Markdown structure. Do not add information. Replace the selected text with the complete translation."),
    dict(id="summary", title="智能摘要", hint="提炼主旨与要点，在对话中阅读", icon="doc", text="仅依据引用内容，用一句话概括主旨，再列出 3–5 个要点；内容较短时少列。不添加原文没有的事实或结论。在对话中给我完整摘要，暂不修改正文。"),
    dict(id="analysis", title="文案拆解", hint="结合原句分析结构与表达方法", icon="grid", text="拆解引用内容的开头、展开逻辑、表达方法和结尾。每项用一处简短原句说明作用；没有的结构不硬套，推断要注明。在对话中给我完整分析，暂不修改正文。"),
]

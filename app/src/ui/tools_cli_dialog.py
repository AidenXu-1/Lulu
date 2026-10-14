"""Read-only bundled tool and CLI reference, opened from Settings."""
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPushButton, QTextBrowser, QVBoxLayout

from ..core.agent_workspace.context import tool_schemas
from ..core.agent_workspace.contracts import COMMANDS
from .control_foundation import adopt_button


class ToolsCliDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('工具与 CLI 入口')
        self.resize(680, 620)
        self.setAttribute(Qt.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        self.reference = QTextBrowser(self)
        self.reference.setObjectName('toolsCliReference')
        self.reference.setAccessibleName('Lulu 工具与 CLI 使用说明')
        self.reference.setOpenLinks(False)
        labels = {
            'edit_document': '直接修改文稿', 'propose_edits': '提供修改方案',
            'resolve_proposal': '使用已有方案', 'read_targets': '读取当前内容',
            'search_documents': '查找文稿', 'read_document': '阅读参考资料',
            'create_document': '创建文章或学习笔记', 'load_skill': '按需使用技能',
            'read_skill_reference': '阅读技能参考', 'list_tasks': '查询后台任务',
            'inspect_link': '识别作品与主页链接', 'prepare_voice': '打开配音工作台',
            'prepare_transcription': '准备转录', 'prepare_collection': '准备主页采集',
            'search_skills': '查找本机技能',
            'prepare_skill_install': '前往安装技能', 'create_skill_draft': '制作技能草稿',
            'list_skill_drafts': '读取待安装技能', 'install_skill': '按要求安装技能',
        }
        tools = ' · '.join(escape(labels.get(tool['name'], tool['name'])) for tool in tool_schemas(loaded_skills=('find-skills', 'skill-creator')))
        self.reference.setHtml(f'''
            <h2>Lulu 的工具</h2>
            <p>改稿与学习使用当前文稿和你选择的资料。转录、配音与采集沿用 Lulu 的工作台。</p>
            <p>{tools}</p>
            <p>技能发现与制作工具在使用相关技能时开放；技能安装、停用、卸载和恢复位于设置的“技能”页。</p>
            <h2>让其他 Agent 调用 Lulu</h2>
            <p>在设置中安装 Lulu Agent Skill 后，外部助手可通过同一套文稿与任务服务工作。</p>
            <p>正式入口：<code>~/.local/bin/lulu</code></p>
            <p>先查询当前安装实际开放的能力：</p>
            <pre>lulu capabilities --output json</pre>
            <p>本地配音支持 IndexTTS 2.5、IndexTTS 2、VoxCPM2 和 Qwen3-TTS，可查询音色、参数与任务进度：</p>
            <pre>lulu tts status --output json</pre>
            <p>准备内容为 {{}} 的 JSON 请求文件后，查询工作区参数目录：</p>
            <pre>lulu workspace tools catalog --request /绝对路径/request.json --output json</pre>
            <p>当前版本提供 {len(COMMANDS)} 个工作区入口。实际可用性以正式安装和工具就绪检查为准。</p>
            <p>可选中文字复制命令；此页面不会执行命令或启动模型。</p>
        ''')
        layout.addWidget(self.reference)
        close = QPushButton('关闭', self)
        adopt_button(close, role='secondary', accessible_name='关闭工具与 CLI 说明')
        close.clicked.connect(self.close)
        layout.addWidget(close, 0, Qt.AlignRight)

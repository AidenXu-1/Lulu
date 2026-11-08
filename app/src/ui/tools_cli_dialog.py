"""Read-only bundled tool and CLI reference, opened from Settings."""
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QPushButton, QTextBrowser, QVBoxLayout
from .theme import TextScaledDialog as QDialog

from ..core.agent_workspace.context import tool_schemas
from ..core.agent_workspace.contracts import COMMANDS
from ..version import __version__
from .cli_info import agent_cli_path
from .. import edition
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
        option = self.reference.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.reference.document().setDefaultTextOption(option)
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
        import shlex
        path = agent_cli_path()
        cli = escape(shlex.quote(path))
        shortcut = str(edition.optional_integration_targets()[1]) if edition.is_experience() else '~/.local/bin/lulu'
        connection = ('在设置中复制连接提示词，使用当前开发体验版的完整 CLI 入口；保留稳定版的 Skill 和快捷命令。'
                      if edition.is_experience() else
                      '在设置中复制安装提示词，交给你常用的 Agent 安装 Lulu Agent Skill。外部助手可通过同一套文稿与任务服务工作。')
        examples = (
            ('抖音主页采集准备', 'collect.prepare'),
            ('批量音视频下载准备', 'batches.download'),
            ('本地或云端转录批次准备', 'batches.prepare'),
            ('读取 RSS 订阅', 'rss.fetch'),
            ('文稿导出', 'documents.export'),
            ('保存到 Obsidian', 'documents.obsidian'),
            ('直播状态与控制', 'live.control'),
            ('查询模型状态', 'models.list'),
            ('查询或管理软件更新', 'updates.control'),
        )
        commands = ''.join(
            f'<p>{escape(title)}</p><p><code>{cli} workspace {escape(name.replace(".", " "))} '
            '--request /绝对路径/request.json --output json</code></p>'
            for title, name in examples
        )
        self.reference.setHtml(f'''
            <h2>Lulu 的工具</h2>
            <p>改稿与学习使用当前文稿和你选择的资料。转录、配音与采集沿用 Lulu 的工作台。</p>
            <p>{tools}</p>
            <p>技能发现与制作工具在使用相关技能时开放；技能安装、停用、卸载和恢复位于设置的“技能”页。</p>
            <h2>让其他 Agent 调用 Lulu</h2>
            <p>{connection}</p>
            <p>当前程序为 Lulu {escape(__version__)}。CLI 随 Lulu 更新；Skill 可独立更新，具体功能以本机 App 和模型状态为准。</p>
            <p>正式入口：<code>{escape(path)}</code>。<code>{escape(shortcut)}</code> 是可选快捷入口。</p>
            <p>先核对安装与连接，再查询当前版本和实际开放的能力：</p>
            <p><code>{cli} version --output json</code></p>
            <p><code>{cli} capabilities --output json</code></p>
            <p>本地配音支持 IndexTTS 2.5、IndexTTS 2、VoxCPM2 和 Qwen3-TTS，可查询音色、参数与任务进度：</p>
            <p><code>{cli} tts status --output json</code></p>
            <p>准备内容为 {{}} 的 JSON 请求文件后，查询工作区参数目录。下方业务命令需要按目录填写各自的请求内容：</p>
            <p><code>{cli} workspace tools catalog --request /绝对路径/request.json --output json</code></p>
            <p>当前版本提供 {len(COMMANDS)} 个工作区入口。实际可用性以正式安装和工具就绪检查为准。</p>
            <h2>常用调用入口</h2>
            {commands}
            <p>采集与批次准备后，按返回结果通过 <code>workspace batches start</code> 启动、<code>workspace batches show</code> 查询；仅准备不会开始下载或转录。</p>
            <p>本地转录按顺序处理；云端批量转录会上传音频并消耗账户余额，需明确同意。直播控制需先打开 Lulu 的直播页面，退出 Lulu 后停止监控与录制。</p>
            <p>可选中文字复制命令；此页面不会执行命令或启动模型。</p>
        ''')
        layout.addWidget(self.reference)
        close = QPushButton('关闭', self)
        adopt_button(close, role='secondary', accessible_name='关闭工具与 CLI 说明')
        close.clicked.connect(self.close)
        layout.addWidget(close, 0, Qt.AlignRight)

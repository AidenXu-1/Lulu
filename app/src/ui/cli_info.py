"""Consumer-facing scope of the shipped CLI; no commands run from this page."""
from pathlib import Path
import shlex
import sys
from .. import edition

# Set only after the public Skill distribution has been verified.
AGENT_SKILL_REPOSITORY_URL = "https://github.com/AidenXu-1/lulu-agent"

def agent_cli_path() -> str:
    if getattr(sys, 'frozen', False):
        app = Path(sys.executable).parent.parent.parent
    elif edition.is_experience():
        app = Path.home() / 'Applications/Lulu 开发体验版.app'
    else:
        app = Path('/Applications/Lulu.app')
    return str(app / 'Contents/MacOS/lulu-agent-cli')

CLI_FEATURES = (
    ("文稿与资料", "查找、读取、创建和修改文稿，撤销或恢复历史版本；管理文件夹、移动文稿及从回收站恢复。"),
    ("文稿导出", "导出 TXT、Markdown 或保存到 Obsidian；有有效时间信息的转录结果可导出 SRT 字幕。"),
    ("AI 对话", "使用已配置的模型处理内容；创建会话、查询进度或取消任务。"),
    ("音视频转录", "转录本机音视频或作品链接，可优先读取已有字幕。支持 SenseVoice Q8、Whisper MLX、Whisper 4bit、MOSS Q5。"),
    ("批量下载与转录", "批量下载音视频，或逐项转录并保存结果。本地转录按顺序处理；云端可并行，需明确同意上传音频及计费。"),
    ("抖音主页采集", "采集作品列表、查询历史，选择音视频下载或转录，导出 CSV / Excel。匿名受限时需由你配合登录。"),
    ("RSS 播客", "读取订阅与单集列表，按你选择的单集加入转录批次，查询进度和完整结果。"),
    ("链接素材", "下载视频、音频、封面和公开作品资料，无需启动转录模型。"),
    ("本地配音与音色", "使用已准备的 IndexTTS 2.5 / 2、VoxCPM2、Qwen3-TTS；管理参考音色，查询参数和生成结果。"),
    ("直播录制", "管理抖音主播、监控和录制，读取直播数据与弹幕，导出单段或已结束场次的完整录像。需先打开 Lulu 的直播页面，退出 Lulu 后停止监控与录制。"),
    ("设置、模型与更新", "查询和调整常用设置，管理本地模型与运行环境，检查或安装软件更新。下载、删除模型或安装更新需明确确认。"),
    ("任务与技能", "查询与取消任务、读取完成回执；管理 Lulu 内部技能，以及工作区备份与恢复。"),
)

CLI_UNAVAILABLE = (
    ("录音与云端配音", "麦克风、系统声音录制及云端配音仍需在 Lulu 应用内操作。"),
    ("飞书导出", "飞书知识库导出仍需在 Lulu 应用内操作。"),
    ("其他主页与图文", "小红书 / B站主页采集，以及图文、文章正文和 OCR 尚未接入 CLI。"),
)


def agent_skill_install_prompt(repository_url: str) -> str:
    if edition.is_experience():
        cli = shlex.quote(agent_cli_path())
        return (
            '请仅连接我正在使用的 Lulu 开发体验版。此版通过包内 CLI 调用，公开的稳定版 Skill 不适用于此连接。\n'
            f'固定入口：{agent_cli_path()}\n'
            f'先运行 {cli} version --output json，再运行 {cli} capabilities --output json。\n'
            '确认返回的版本和可用能力；后续命令始终使用这个完整入口。连接失败或版本不匹配就停止，不切换到其他 Lulu。\n'
            '保留已有 Skill 和快捷命令。本次只验证连接，不下载模型、不修改文稿，也不开始转录、配音或云端调用。'
        )
    if not repository_url:
        raise ValueError("Skill 仓库地址尚未配置")
    return (
        f"请访问 {repository_url}，阅读安装说明，为你当前运行的 Agent 下载并安装 lulu-agent Skill。\n"
        "这是用于调用我电脑上 Lulu 的外部技能，请装到你自己的技能目录，保留完整的脚本和参考文件。\n"
        "安装前检查来源、所需权限和与本机 Lulu 的兼容性；已有同名技能时先比较并保留可恢复副本。\n"
        "不要覆盖 Lulu 管理的 ~/.agents/skills/lulu-agent；若你直接使用这个目录，校验后复用当前随包版本。\n"
        "安装后按 Skill 说明校验 /Applications/Lulu.app，并通过 /Applications/Lulu.app/Contents/MacOS/lulu-agent-cli 查询实际可用功能。\n"
        "若仓库缺少技能包、版本不匹配或当前 Agent 不支持此类技能，请明确说明并停止。"
        "本次只安装和验证连接，不下载模型、不修改我的文稿，也不自动开始转录、配音或云端调用。"
    )

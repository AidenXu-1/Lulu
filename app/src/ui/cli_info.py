"""Consumer-facing scope of the shipped CLI; no commands run from this page."""

# Set only after the public Skill distribution has been verified.
AGENT_SKILL_REPOSITORY_URL = "https://github.com/AidenXu-1/lulu-agent"

CLI_FEATURES = (
    ("文稿与版本", "查找、读取和创建文稿；整稿保存、局部修改、撤销与恢复历史版本。"),
    ("AI 对话", "使用已配置的模型处理内容；创建会话、查询进度或取消任务。"),
    ("本地音视频转录", "转录本机音视频，查询模型与完整结果。支持 SenseVoice Q8、Whisper MLX、Whisper 4bit、MOSS Q5。"),
    ("本地配音", "使用已准备的 IndexTTS 2.5 / 2、VoxCPM2、Qwen3-TTS；查询音色、参数和生成结果。"),
    ("链接素材", "下载视频、音频、封面和公开作品资料，无需启动转录模型。"),
    ("任务与技能", "查询与取消任务、读取完成回执；管理 Lulu 内部技能，以及工作区备份与恢复。"),
)

# GUI-owned live commands are internal; they are not public Agent CLI commands.
CLI_UNAVAILABLE = (
    ("直播录制", "主播监控、开始录制、录像导出和直播数据管理，请在 Lulu 的「直播录制」中操作。"),
    ("其他功能", "主页批量采集、录音、云端配音，以及飞书 / Obsidian 导出，暂未向 CLI 开放。"),
)


def agent_skill_install_prompt(repository_url: str) -> str:
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

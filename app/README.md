# Lulu 应用源码

Lulu 是面向 Apple Silicon、macOS 15 及以上的本地内容工作台。当前产品版本为 **4.2.1 / 20261015.2**，提供音视频转录、主页采集、直播录制、文稿与素材管理、文案处理、配音和飞书导出。

运行源码和生成 App 的步骤见 [源码与构建](../BUILDING.md)。日常使用可以直接下载 [正式安装包](https://github.com/AidenXu-1/Lulu/releases/latest)。

本机接续开发位置为项目根目录的 `app/`，沿用根目录 `.venv/`；工作规则和当前需求见 [当前交接](../docs/handoff.md)。本地保留的历史辅助文件不作为正式产品入口，不在 `scratch/` 的旧副本继续开发。

| 路径 | 用途 |
| --- | --- |
| `run_app.py` | 从源码启动产品界面 |
| `run_agent_cli.py`、`run_agent_core.py` | 产品命令行入口与后台服务 |
| `src/core/`、`src/ui/`、`src/agent_cli/` | 业务功能、界面及 AI 调用能力 |
| `native/` | macOS 本机通信与身份验证组件 |
| `packaging/` | 当前 App、安装镜像、更新包和配音运行环境所需的构建与校验文件 |
| `tests/` | 当前产品构建执行的功能检查及必要夹具 |
| `skills/`、`internal-skills/` | 随 App 提供的调用技能和内置文案技能 |

`internal-skills/` 是应用中实际提供的校对、润色、分析等技能，属于产品功能。独立安装给 AI 助手的 Skill 位于仓库根目录 [agent-skill/](../agent-skill/README.md)。

第一方代码采用 [MIT 许可](../LICENSE)，第三方组件和模型保持各自许可，见 [第三方说明](../THIRD_PARTY_NOTICES.md)。模型权重通过应用中的模型设置另行准备。

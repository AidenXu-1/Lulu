# 第三方组件

Lulu 的 MIT 许可仅适用于本项目第一方代码和有权授予许可的资源，不替代以下组件及模型的原许可。重新分发时保留原组件的版权、许可、声明和适用义务。

| 范围 | 许可与来源 |
| --- | --- |
| 桌面界面 PySide6 / Qt | LGPL-3.0、GPL-3.0 或商业许可，按实际模块适用；官方说明：https://doc.qt.io/qtforpython-6/licenses.html。动态库及原许可随发行保留，使用者可依法替换相应库。 |
| 本地转录与模型 | FunASR、SenseVoice、Whisper MLX、Qwen-ASR、PyTorch、ModelScope 等按各自代码及模型许可使用；依赖见 `app/requirements.txt`。模型权重未纳入源码仓库。 |
| 主页采集 | 原来源及修改说明见 `app/THIRD_PARTY_NOTICES_DOUYIN_COLLECT.md`。 |
| 直播录制 | StreamGet 来源、版本和修改说明见 `app/THIRD_PARTY_NOTICES_LIVE_RECORDING.md`；MIT 原文保留在 `app/src/core/live_recording/STREAMGET_LICENSE.txt`。 |
| 音视频处理 | imageio-ffmpeg 及实际 FFmpeg 二进制按各自构建许可使用；FFmpeg 官方许可与对应源码：https://ffmpeg.org/legal.html。 |
| 文稿编辑器 | Milkdown、ProseMirror、CodeMirror 及其他前端依赖的原声明保留在 `app/src/ui/agent_workspace/assets/THIRD_PARTY_NOTICES.txt`；开发依赖与锁定版本见该目录上级的 package 文件。 |
| 配音运行时 | VoxCPM2 原说明见 `app/THIRD_PARTY_NOTICES_VOXCPM2.md`；IndexTTS、VoxCPM2、Qwen3-TTS 的构建、依赖、固定版本和原来源保留在 `app/packaging/tts_runtimes/`。 |
| 打包 | PyInstaller 按其 GPL 及 Bootloader Exception 使用：https://pyinstaller.org/en/stable/license.html。 |

作者联系名片和署名是产品公开资料。第三方商标及图标用于识别对应服务，不代表本项目拥有这些商标或第三方为本项目背书。

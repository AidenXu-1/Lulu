<p align="center">
  <img src="docs/assets/lulu-mascot.png" width="180" alt="Lulu 吉祥物">
</p>

<h1 align="center">Lulu</h1>

<p align="center"><strong>把散落在视频里的内容，安静地带回你的 Mac。</strong></p>

<p align="center">
  <strong>当前版本 v0.2.15 · Build 20260811.1</strong><br>
  macOS · Apple Silicon · 本地优先 · 无遥测
</p>

<p align="center">
  <a href="https://github.com/AidenXu-1/Lulu/releases/latest"><strong>下载最新版</strong></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/AidenXu-1/Lulu/releases/tag/v0.2.15">查看本版更新</a>
  &nbsp;·&nbsp;
  <a href="#用-ai-完成首次安装">让 AI 安装</a>
</p>

Lulu 是一款运行在 Mac 上的本地内容工作台。你可以导入音视频或粘贴内容链接，将它们转成逐字稿，再继续完成批量采集、素材管理、文案处理、配音与飞书整理。

## v0.2.15 更新重点

这一版重点解决下载失败、任务重开丢失和进度看不懂的问题：

- **未完成任务可恢复**：关闭 Lulu 后，待处理和中断任务会在下次打开时回到“待继续”，已完成任务不再长期占用列表。
- **进度更真实**：下载、转录和模型安装有真实进度时显示百分比；无法准确估算时，明确显示当前阶段和已用时间。
- **下载与模型安装更稳**：修复断网、官方 CDN 跳转和安装中断后的失败问题，已下载部分可以安全续传。
- **抖音链接兼容性提升**：修复部分新版抖音分享链接无法抓取的问题。
- **更新缓存自动收尾**：安装成功后自动清理本次下载包和解压缓存；暂停时保留可续传进度，不影响当前可用版本。
- **日常使用细节更顺手**：同步优化主页采集、素材库选择、文件定位和飞书写入，并收紧模型与更新链路的安全边界。

[查看 v0.2.15 完整 Release](https://github.com/AidenXu-1/Lulu/releases/tag/v0.2.15)

## 从内容到可用资料

| 步骤 | 你可以做什么 |
| --- | --- |
| 1. 导入 | 选择本地音频或视频，也可以粘贴受支持的内容链接。 |
| 2. 处理 | 在 Mac 上完成转录；主页作品可以按范围批量采集，并组织成任务组。 |
| 3. 整理 | 把音视频、封面、逐字稿和创作资料收进受管素材库与文稿库。 |
| 4. 使用 | 继续处理文案、使用应用内配音能力，或把所选内容写入飞书多维表格。 |

## 核心能力

| 能力 | 适合的场景 |
| --- | --- |
| 本地语音转文字 | 导入本地音频、视频或常见内容链接，在 Mac 上生成并整理逐字稿。 |
| 主页批量采集 | 按时间范围选择作品，批量采集、转录，并用任务组管理进度。 |
| 受管素材与文稿 | 统一保存已选择的音视频、封面、逐字稿、提示词和创作资料，并可在访达中定位。 |
| 文案处理与配音 | 在逐字稿基础上继续整理文案，并使用应用内提供的配音能力。 |
| 飞书多维表格 | 主动选择后，将作品信息、逐字稿、封面及已下载附件写入你的飞书空间。 |

## 四步开始使用

1. 前往 [Latest Release](https://github.com/AidenXu-1/Lulu/releases/latest)，下载 Apple Silicon 版本的 DMG。
2. 打开 DMG，将 `Lulu.app` 拖入“应用程序”。
3. 首次打开时，按下方说明完成 macOS 手动允许。
4. 导入本地文件或粘贴内容链接，选择保存位置后开始处理。

### macOS 首次打开说明

当前公开安装包使用 ad-hoc 签名，尚未使用 Apple Developer ID，也未经过 Apple 公证。macOS 可能阻止首次启动，这是系统对未公证应用的正常提醒。

请先确认安装包来自本仓库，再使用下面任一方式打开：

- 在访达的“应用程序”中按住 Control 点击 `Lulu.app`，选择“打开”，然后再次确认。
- 打开“系统设置 → 隐私与安全性”，在安全提示处选择“仍要打开”。

不要关闭 Gatekeeper，不要重新签名，也不要使用 `xattr -dr` 绕过系统安全机制。

## 用 AI 完成首次安装

如果你使用的 AI 可以操作这台 Mac 的终端和文件，可以展开并复制完整提示词。它会要求 AI 只从本仓库取得 Latest Release，并完成校验、备份、安装与启动复查。

<details>
<summary><strong>展开完整 AI 安装提示词</strong></summary>

```text
请帮我在这台 Mac 上安装 Lulu 最新稳定版，官方仓库只有：
https://github.com/AidenXu-1/Lulu

请直接完成安装，但必须严格遵守以下边界：

1. 只使用这个官方仓库及其 Latest Release。不要使用搜索引擎、第三方镜像、转载下载站或其他仓库中的安装包。
2. 先确认这台 Mac 使用 Apple Silicon（arm64）。如果不是，请停止并告诉我当前版本不适用。
3. 从 Latest Release 读取最新版本和构建号，选择对应的 arm64 DMG 与 `.sha256` 文件，下载到权限收紧的临时目录。完整计算 SHA-256，并同时核对校验文件和 GitHub 提供的服务器摘要；任一不一致立即停止。
4. 安装前只检查标准安装位置中的 Lulu。若 Lulu 正在运行，只正常退出该 Lulu 进程；不要终止其他应用。若已有旧版，先将旧 App 移到带时间戳的可恢复备份目录，保留原有用户数据，不要先删除旧版。
5. 只读挂载已验证的 DMG，核对其中 App 的名称、版本、构建号与架构。使用暂存目录完成安装，并尽可能采用同卷原子替换；如果无法安全替换，请停止并说明原因。
6. 不要读取、记录或输出钥匙串、授权码、Cookie、Token、浏览器资料、逐字稿或其他业务内容。不要上传任何本地文件。
7. 不要运行 `xattr -dr`、不要关闭 Gatekeeper、不要重新签名或伪造签名，也不要用其他方式绕过 macOS 安全机制。如果系统要求确认，请暂停并告诉我如何在“隐私与安全性”中手动允许打开。
8. 安装后卸载本次 DMG，确认 `/Applications` 中的 Lulu 版本和构建号与 Latest Release 一致。启动一次 Lulu，确认只有一个 Lulu 实例、有可见窗口，并在约 2 秒和 8–10 秒后仍正常运行；出现崩溃或异常就停止并保留现场。
9. 最后只向我报告：官方 Release 地址、安装包 SHA-256、安装路径、安装后的版本与构建号、旧版备份路径，以及是否需要我完成 macOS 手动允许。不要删除备份。
```

</details>

这段提示词适合第一次安装或需要重新安装时使用。安装完成后，后续版本请优先使用 Lulu 的应用内更新；只有内置更新无法使用时，再回到 [Latest Release](https://github.com/AidenXu-1/Lulu/releases/latest) 手动安装。

## 本地、联网与隐私边界

| 场景 | 数据如何流动 |
| --- | --- |
| 本地转录 | 默认在本机处理，音频、视频和逐字稿保留在你的 Mac。 |
| 遥测 | Lulu 不加入遥测，不会在本地模式中上传音频或逐字稿。 |
| 链接采集 | 需要访问对应内容平台，关闭或不使用链接采集时不触发这类访问。 |
| 飞书导出 | 只有你主动执行导出时，所选文本、封面或附件才会发送到你的飞书空间。 |
| 数据位置 | 可以在 Lulu 的“数据位置”中查看并管理本地数据目录。 |

## 应用内更新

Lulu 通过本仓库发布 production 更新清单。更新清单带有独立 RSA-3072 签名，应用会在下载前校验版本、文件大小与 SHA-256，避免安装到不完整或被替换的包。

安装成功并确认新版本可启动后，Lulu 会自动清理本次更新的下载包和解压缓存。如果用户暂停下载，已下载部分会为续传而保留，不会删除模型、素材库、文稿或音色。

如果自动更新暂时不可用，可以随时回到 [Releases](https://github.com/AidenXu-1/Lulu/releases) 手动下载当前 Latest 版本。

## 系统与分发范围

- **平台**：macOS
- **处理器**：Apple Silicon（arm64）
- **分发方式**：GitHub 可信用户分发
- **签名状态**：ad-hoc 签名，未使用 Developer ID，未经 Apple 公证
- **代码状态**：这个仓库用于承载安装包、版本记录与应用内更新清单，当前不公开 Lulu 源代码

## 常见问题

<details>
<summary><strong>Lulu 会把我的音视频上传到云端吗？</strong></summary>

默认的本地转录不会上传音视频或逐字稿。链接采集需要联网；飞书导出仅在你主动操作时上传所选内容。

</details>

<details>
<summary><strong>为什么 macOS 提示无法验证开发者？</strong></summary>

当前版本使用 ad-hoc 签名，尚未使用 Developer ID，也未经 Apple 公证。请确认安装包来自本仓库，再按照“macOS 首次打开说明”手动允许。

</details>

<details>
<summary><strong>更新失败会影响现有版本吗？</strong></summary>

更新流程会校验更新清单与安装包，并改善了更新缓存的自动清理和失败保留逻辑。如果应用内更新暂时不可用，可以保留现场并改用本仓库的 Latest Release。

</details>

<details>
<summary><strong>遇到问题应该准备哪些信息？</strong></summary>

请记录 Lulu 版本、构建号、macOS 版本、操作步骤和界面提示，并联系向你提供 Lulu 的授权方。不要公开逐字稿、Token 或其他私人内容。

</details>

---

<p align="center">
  <strong>Lulu，让内容回到你手里。</strong><br>
  <a href="https://github.com/AidenXu-1/Lulu/releases/latest">下载 v0.2.15</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/AidenXu-1/Lulu/releases">查看全部版本</a>
</p>

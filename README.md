<p align="center">

  <img src="docs/assets/lulu-mascot.png" width="180" alt="Lulu 吉祥物">
</p>

<h1 align="center">Lulu</h1>

<p align="center"><strong>把散落在视频里的内容，安静地带回你的 Mac。</strong></p>

<p align="center">
  <strong>当前版本 v4.1.5 · Build 20261011.5</strong><br>
  macOS · Apple Silicon · 本地优先 · 无遥测
</p>

<p align="center">
  <a href="https://github.com/AidenXu-1/Lulu/releases/latest"><strong>下载 Lulu 最新版</strong></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/AidenXu-1/Lulu/releases/tag/v4.1.5">查看本版更新</a>
  &nbsp;·&nbsp;
  <a href="#用-ai-完成首次安装">让 AI 安装</a>
  &nbsp;·&nbsp;
  <a href="#让-ai-调用-lulu">Agent Skill</a>
</p>

Lulu 是一款运行在 Mac 上的本地内容工作台。你可以导入音视频或粘贴内容链接，将它们转成逐字稿，再继续完成批量采集、素材管理、文案处理、配音与飞书整理。

## v4.1.5 更新重点

- **任务可以明确恢复**：修复升级后任务保存失败和继续按钮无响应；支持单条继续、继续这组与全部继续，已完成或已取消的任务不会重复执行。
- **更新操作及时反馈**：检查更新立即显示检查状态；慢网下下载、暂停、继续的按钮与实际状态保持一致。
- **保留原有资料**：沿用独立保存的文稿、音频、模型与设置，保留安装包校验及更新失败回退保护。

**4.1.4 用户可在应用内检查更新；4.1.3 及更早版本请下载 DMG，退出 Lulu 后手动覆盖安装一次。** 无需先卸载，也不要删除个人资料或钥匙串记录。

任务故障恢复、批量恢复与慢网中断续传已通过隔离检查；相同功能的本地修复版已验证成功换包、重开及原文稿、历史任务和模型保留。本次 4.1.5 仅更新版本标识并重新打包核验。

[查看 v4.1.5 完整 Release](https://github.com/AidenXu-1/Lulu/releases/tag/v4.1.5)

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
| 直播录制与数据 | 监控主播开播、自动分段保存，查看每场数据趋势，并导出视频、音频或 CSV。 |
| 主页批量采集 | 按时间范围选择作品，批量采集、转录，并用任务组管理进度。 |
| 受管素材与文稿 | 统一保存已选择的音视频、封面、逐字稿、提示词和创作资料，并可在访达中定位。 |
| 文案处理与配音 | 在逐字稿基础上继续整理文案，并使用应用内提供的配音能力。 |
| 飞书多维表格 | 主动选择后，将作品信息、逐字稿、封面及已下载附件写入你的飞书空间。 |

## 四步开始使用

1. 前往 [Latest Release](https://github.com/AidenXu-1/Lulu/releases/latest)，下载 Apple Silicon 版本的 **DMG**。
2. 退出已有 Lulu，打开 DMG，将 **Lulu.app 拖入 Applications**；已有版本时确认替换。
3. 从“应用程序”打开 Lulu，按系统提示亲自完成首次打开或系统授权。
4. 导入本地文件或粘贴内容链接，选择保存位置后开始处理。

4.1.3 及更早版本请通过 DMG 手动覆盖一次，4.1.4 起可使用修复后的应用内更新。程序替换保留本地文稿、音频、模型与设置；升级前请先保存当前编辑并完成或自行取消待处理任务。普通安装只需下载 DMG，其余三个附件供应用内更新使用。更新说明可直接在本页和各版本 Release 正文查看。

### macOS 首次打开说明

当前公开安装包使用 ad-hoc 签名，尚未使用 Apple Developer ID，也未经过 Apple 公证。macOS 可能阻止首次启动，这是系统对未公证应用的正常提醒。

请先确认安装包来自本仓库，再使用下面任一方式打开：

- 在访达中按住 Control 点击已安装的 `Lulu.app`，选择“打开”，然后按系统提示确认。
- 打开“系统设置 → 隐私与安全性”，在安全提示处选择“仍要打开”。

不要关闭 Gatekeeper，不要重新签名，也不要使用 `xattr -dr` 绕过系统安全机制。

## 用 AI 完成首次安装

如果你使用的 AI 可以操作这台 Mac 的终端和文件，可以展开并复制下方提示词。

<details>
<summary><strong>展开 AI 安装提示词</strong></summary>

```text
请帮我安装 Lulu 最新稳定版，官方分发仓库只有：
https://github.com/AidenXu-1/Lulu

1. 先确认 Mac 使用 Apple Silicon（arm64），然后从官方 Latest Release 读取版本、构建号和 arm64 DMG。
2. 下载到权限收紧的临时目录，核对完整 SHA-256 与 GitHub Release API 提供的该资产摘要。不一致或无法取得摘要就停止。无需下载独立校验文件。
3. 检查 /Applications/Lulu.app。已有版本时先保存编辑并正常退出，确认没有正在执行的任务；不要擅自取消任务。保留可恢复的旧 App 副本。
4. 只读挂载 DMG，核对包内 Lulu.app 的版本、构建号和完整签名。通过标准拖拽或保留符号链接及权限的复制安装到 /Applications，保持用户资料原位，不手工注册后台或改写安装记录。
5. 不读取或输出密钥、Cookie、Token 或业务内容，不上传本地文件，不关闭 Gatekeeper、不重签名、不用 xattr 绕过系统安全保护。系统授权由我亲自完成。
6. 从 /Applications 启动一次 Lulu，检查安装版本、可见主窗口以及启动后 2 秒和 8–10 秒的运行情况。异常则保留现场，说明原因，不循环重装。
7. 卸载本次 DMG，报告官方 Release、包体摘要、安装路径、版本及旧 App 副本位置。不要删除用户资料或回退副本。
```

</details>

Lulu Release 中的 App ZIP 是应用更新校验资产，安装 App 请使用 DMG；下面的 Skill ZIP 用于安装 AI 助手的调用技能。

## 让 AI 调用 Lulu

安装 **Lulu Agent Skill** 后，你可以让支持本地 Skill 和终端操作的 AI 助手调用这台 Mac 上的 Lulu，查找文稿、转录录音、生成配音或下载链接素材。

**[查看 Skill 与安装说明](https://github.com/AidenXu-1/lulu-agent) · [下载完整 Skill 包（ZIP）](https://github.com/AidenXu-1/lulu-agent/archive/refs/heads/main.zip)**

1. 先安装并打开 Lulu，完成系统授权。CLI 已随 Lulu 提供，无需另下一个命令行程序。
2. 把下面的提示词交给你常用的 AI 助手，让它安装 Skill 并检查连接。
3. 连接成功后，直接描述任务，例如“用 Lulu 把这段录音转成文字”。

```text
请从 https://github.com/AidenXu-1/lulu-agent 阅读安装说明，下载完整 Skill 并安装到你当前使用的技能目录。先检查兼容性和已有同名技能，保留 scripts 与 references，不覆盖 Lulu 管理的旧副本。按 SKILL.md 验证本机 Lulu，再查询实际可用功能；如果发现两个同名入口，按说明选择唯一启用项。本次只安装和检查连接，不下载模型、不修改文稿、不执行转录、配音或云端调用。
```

也可以手动下载 ZIP，解压后按其中的 README 安装；请保留整个 `lulu-agent` 文件夹，只下载 `SKILL.md` 会缺少必要文件。Codex 的安装示例及其他 Agent 的目录选择均在 [Skill 安装说明](https://github.com/AidenXu-1/lulu-agent#安装) 中。

使用示例：“在 Lulu 里找上周的访谈文稿”“用我保存的音色朗读这篇文稿”“下载这个作品链接的封面”。实际可用功能和模型以本机 Lulu 检查结果为准。Skill 可独立更新、卸载，不附带模型；仅安装 Skill 不会自动执行这些任务。

## 本地、联网与隐私边界

| 场景 | 数据如何流动 |
| --- | --- |
| 本地转录 | 默认在本机处理，音频、视频和逐字稿保留在你的 Mac。 |
| 遥测 | Lulu 不加入遥测，不会在本地模式中上传音频或逐字稿。 |
| 直播监控 | 访问对应直播平台，录像与已采集的公开数据保存到本地；平台限制或缺少字段时会明确显示。 |
| 链接采集 | 需要访问对应内容平台，关闭或不使用链接采集时不触发这类访问。 |
| 云端 AI 对话 | 主动发送时，本轮必要文稿、引用与对话上下文会交给你配置的模型服务商。 |
| 飞书导出 | 只有你主动执行导出时，所选文本、封面或附件才会发送到你的飞书空间。 |
| 数据位置 | 可以在 Lulu 的“数据位置”中查看并管理本地数据目录。 |

## 应用内更新

Lulu 从本仓库获取带 RSA-3072 签名的更新清单，核验版本、文件大小和 SHA-256。

4.1.4 起可使用修复后的应用内更新。4.1.3 及更早版本会收到手动升级提示，请下载 DMG、退出 Lulu 后拖入 Applications 并选择替换。没有更新入口的旧版也可直接下载 DMG。无需先卸载或删除资料。升级前先保存编辑并结束正在执行的任务，系统授权由本人完成。模型和资料沿用原位置。

## 系统与分发范围

- **平台**：macOS 15 或更新版本
- **处理器**：Apple Silicon（arm64）
- **分发方式**：GitHub 可信用户分发
- **签名状态**：ad-hoc 签名，未使用 Developer ID，未经 Apple 公证
- **代码状态**：这个仓库用于承载安装包、版本记录与应用内更新清单，当前不公开 Lulu 源代码

## 常见问题

<details>
<summary><strong>Lulu 会把我的音视频上传到云端吗？</strong></summary>

默认的本地转录不会上传音视频或逐字稿。链接采集需要联网；云端 AI 对话会发送本轮必要上下文；飞书导出仅在你主动操作时上传所选内容。

</details>

<details>
<summary><strong>为什么 macOS 提示无法验证开发者？</strong></summary>

当前版本使用 ad-hoc 签名，尚未使用 Developer ID，也未经 Apple 公证。请确认安装包来自本仓库，再按照“macOS 首次打开说明”手动允许。

</details>

<details>
<summary><strong>更新失败会影响现有版本吗？</strong></summary>

4.1.4 的本机验证已覆盖更新失败后恢复旧版、回退后重新打开及再次更新成功。更新前仍请保存编辑并结束正在执行的任务；遇到异常时保留提示和现场，不要删除文稿、模型或钥匙串记录。

</details>

<details>
<summary><strong>遇到问题应该准备哪些信息？</strong></summary>

请记录 Lulu 版本、构建号、macOS 版本、操作步骤和界面提示，并联系向你提供 Lulu 的授权方。不要公开逐字稿、Token 或其他私人内容。

</details>

---

<p align="center">
  <strong>Lulu，让内容回到你手里。</strong><br>
  <a href="https://github.com/AidenXu-1/Lulu/releases/latest">下载最新版</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/AidenXu-1/Lulu/releases">查看全部版本</a>
</p>


<p align="center">
  <img src="docs/assets/lulu-mascot.png" width="180" alt="Lulu 吉祥物">
</p>

<h1 align="center">Lulu</h1>

<p align="center"><strong>把散落在视频里的内容，安静地带回你的 Mac。</strong></p>

<p align="center">macOS · Apple Silicon · 本地优先 · 无遥测</p>

<p align="center">
  <a href="https://github.com/AidenXu-1/Lulu/releases/latest"><strong>下载最新版</strong></a>
  &nbsp;·&nbsp;
  <a href="#用-ai-一键完成首次安装">让 AI 安装</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/AidenXu-1/Lulu/releases">查看历史版本</a>
</p>

## 用 AI 一键完成首次安装

如果你使用的 AI 可以操作这台 Mac 的终端和文件，把下面整段提示词复制给它。AI 会从本仓库取得最新版 Lulu，完成校验、备份和安装。

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

这段提示词适合**第一次安装或需要重新安装**时使用。安装完成后，后续版本请优先使用 Lulu App 内置的更新功能，无需再次让 AI 替换应用；只有内置更新无法使用时，再回到本仓库的 [Latest Release](https://github.com/AidenXu-1/Lulu/releases/latest)。

## Lulu 可以做什么

| 能力 | 说明 |
| --- | --- |
| 本地语音转文字 | 导入本地音频、视频或常见内容链接，在 Mac 上完成转录与逐字稿整理。 |
| 主页批量采集 | 按时间范围选择作品，批量采集、转录与管理任务组。 |
| 受管素材库 | 统一保存已选择的音频、视频与封面，支持在访达中定位。 |
| 文稿与创作整理 | 管理逐字稿、提示词与创作资料，减少重复搬运。 |
| 飞书多维表格 | 将作品信息、逐字稿、封面和已下载的音视频附件写入飞书。 |

## 四步开始使用

1. 前往 [Latest Release](https://github.com/AidenXu-1/Lulu/releases/latest) 下载 Apple Silicon 版本的 DMG。
2. 打开 DMG，将 `Lulu.app` 拖入“应用程序”。
3. 首次打开时，根据下方说明完成 macOS 手动允许。
4. 导入本地文件或粘贴内容链接，选择保存位置后开始处理。

## macOS 首次打开说明

当前公开安装包尚未使用 Apple Developer ID 签名，也未经过 Apple 公证。macOS 可能阻止首次启动，这是系统对未公证应用的正常提醒。

可以使用下面任一方式打开：

- 在访达的“应用程序”中按住 Control 点击 `Lulu.app`，选择“打开”，然后再次确认。
- 打开“系统设置 → 隐私与安全性”，在安全提示处选择“仍要打开”。

请始终从本仓库的 [Releases](https://github.com/AidenXu-1/Lulu/releases) 下载 Lulu，不要使用第三方转载的安装包。

## 本地与隐私

- 本地转录是默认模式，音频、视频和逐字稿默认保留在你的 Mac。
- Lulu 不加入遥测，也不会在本地模式中上传音频或逐字稿。
- 链接采集需要访问对应内容平台。
- 只有在你主动使用飞书导出时，所选文本、封面或附件才会发送到你的飞书空间。
- 数据目录可以在 Lulu 的“数据位置”中查看。

## 应用内更新

Lulu 通过本仓库发布 production 更新清单。更新清单带有独立 RSA-3072 签名，应用会在下载前校验版本、文件大小和 SHA-256，避免安装到不完整或被替换的包。

如果自动更新暂时不可用，可以随时回到 [Releases](https://github.com/AidenXu-1/Lulu/releases) 手动下载当前 Latest 版本。

## 系统与分发范围

- 平台：macOS
- 处理器：Apple Silicon（arm64）
- 分发方式：GitHub 可信用户分发
- 签名状态：ad-hoc 签名，未使用 Developer ID，未公证

## 常见问题

<details>
<summary><strong>Lulu 会把我的音视频上传到云端吗？</strong></summary>

默认的本地转录不会上传音视频或逐字稿。链接采集需要联网；飞书导出仅在你主动操作时上传所选内容。

</details>

<details>
<summary><strong>为什么 macOS 提示无法验证开发者？</strong></summary>

当前版本尚未使用 Developer ID 与 Apple 公证。请确认安装包来自本仓库，再按照“macOS 首次打开说明”手动允许。

</details>

<details>
<summary><strong>这个仓库为什么看不到源代码？</strong></summary>

这里专门承载官方安装包、版本记录与应用内更新清单。Lulu 当前没有公开源代码。

</details>

<details>
<summary><strong>遇到问题应该怎么办？</strong></summary>

请记录 Lulu 版本、macOS 版本、操作步骤和界面提示，并联系向你提供 Lulu 的授权方。

</details>

---

<p align="center">Lulu，让内容回到你手里。</p>

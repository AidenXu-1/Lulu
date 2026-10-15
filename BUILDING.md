# 从源码构建和运行 Lulu

本仓库提供 Lulu **4.2.1 / 20261015.2** 的完整产品源码、界面资源、必要构建文件和 Agent Skill。第一方采用 [MIT 许可](LICENSE)，第三方组件及模型保持各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 准备环境

使用 Apple Silicon Mac、macOS 15 或更新版本、Python 3.11 和 Xcode Command Line Tools。可用 `python3.11 --version` 和 `xcode-select -p` 检查；缺少命令行工具时先运行 `xcode-select --install`，按 macOS 提示完成安装。首次安装 Python 依赖需要联网。

从[发布页](https://github.com/AidenXu-1/Lulu/releases/tag/v4.2.1)下载 **Source code (zip)** 或 **Source code (tar.gz)**，解压并在终端进入该文件夹。以下命令均从能看到 `README.md`、`SOURCE_MANIFEST.json` 和 `app/` 的根目录执行。无需另行克隆仓库，也无需预先安装 Lulu。

## 安装依赖并打包完整 App

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
.venv/bin/python -m pip install 'pyinstaller==6.20.0'

export LULU_BUILD_ROOT="$PWD/app/build/phase10-production-20261015.2"
export LULU_DIST_ROOT="$PWD/app/dist/phase10-production-20261015.2"
export LULU_PRODUCTION_RELEASE_AUTHORIZED=1
bash app/packaging/build_app.sh --identity production --venv "$PWD/.venv"
```

首次处理源码压缩包时，脚本先逐件核对源码清单，再自动建立这份源码的本地版本记录。此过程不联网，不下载旧程序，也不依赖维护者的私人开发历史。记录属于本地构建快照，不冒充维护者的发布提交。正常 Git 克隆仍使用原有版本记录。

界面库、语音处理核心和打包工具固定为本流程验收使用的版本；先按这些版本构建，再单独验证依赖升级。

构建执行全部明确列出的产品检查，并校验源码、界面资源、隐私、签名和包内运行。成功后生成 `$LULU_DIST_ROOT/Lulu.app`；不会自动安装、替换程序或上传文件。输出目录须位于当前源码的 `app/build/` 和 `app/dist/`，首次使用新目录。重试仅复用脚本确认归属的同次构建目录，勿混入其他版本产物。

## 安装并运行自己构建的 Lulu

在 Finder 打开 `app/dist/phase10-production-20261015.2/`，把 `Lulu.app` 拖入“应用程序”文件夹，再从“应用程序”打开。后台准备完成后使用完整的文稿、任务及 Agent 功能。已有 Lulu 时，覆盖前先备份自己的资料；系统密码和授权提示由使用者亲自处理。

代码不附带模型权重。转录或配音前，在应用的“AI 大模型”中选择并下载所需的本地模型；云端功能需要使用者主动配置服务。未下载模型不影响启动界面和打包，也不代表已经完成转录或配音实测。

## 单独检查或开发界面

在源码根目录检查当前产品：

```sh
.venv/bin/python -B app/packaging/product_capability_gate.py \
  --repo "$PWD" --output /private/tmp/lulu-product-checks.json
```

此入口只运行明确列出的当前产品检查，并隔离配置、模型目录、文稿、网络和凭据。基础检查不代替所有模型、云服务和长时间直播实测。源码包可直接运行此入口，首次同样会建立本地版本记录。

开发界面时可使用：

```sh
cd app
../.venv/bin/python -B run_app.py
```

该窗口用于源码开发。完整的后台服务和 Agent 使用前面自己构建并安装的 App。源码界面使用本机配置，开发时使用独立 macOS 用户或隔离资料，避免写入日常文稿。

如需改源码，先运行 `.venv/bin/python app/packaging/source_repository.py --repo "$PWD"` 完成原始源码核对，再修改并用 Git 提交；构建要求工作区干净。修改前直接删除文件或改写清单会使原始源码核对失败。

## 生成安装镜像

在源码根目录，用刚构建的 App 生成标准拖拽安装镜像，输出文件须尚不存在：

```sh
PYTHONPATH="$PWD/app" .venv/bin/python app/packaging/create_drag_delivery.py \
  --app "$LULU_DIST_ROOT/Lulu.app" --output /private/tmp/Lulu-local.dmg
```

应用内更新包由 `app/packaging/make_update_release.py` 生成。官方更新清单须由维护者使用源码仓库外的私钥签署；自己构建的 App 不自动成为官方更新。安装身份、调用方验证、资料归属及更新验签限制均保留。

当前官方安装包使用 ad-hoc 签名，尚未经过 Apple Developer ID 签名和公证；首次打开按 macOS 提示处理。文稿编辑器源码及构建入口在 `app/src/ui/agent_workspace/`，已提交的网页资源可直接使用。配音运行环境的依赖、固定版本和构建入口在 `app/packaging/tts_runtimes/`，与主程序分开；模型及第三方运行环境依各自许可另行取得。

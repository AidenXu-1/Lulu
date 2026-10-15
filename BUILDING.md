# Lulu 源码与构建

本仓库提供 Lulu **4.2.1 / 20261015.1** 的产品源码、界面资源、依赖说明、必要构建文件和 Agent Skill。第一方采用 [MIT 许可](LICENSE)，第三方组件和模型保持各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 准备环境

使用 Apple Silicon Mac、macOS 15 或更新版本、Python 3.11 和 Xcode Command Line Tools。以下命令均从仓库根目录执行。首次安装依赖需要联网；模型权重通过应用的模型设置另行下载。

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
```

## 运行源码

```sh
cd app
../.venv/bin/python run_app.py
```

源码界面使用本机配置。调试时请使用独立的 macOS 用户或隔离的资料目录，避免让开发程序写入日常文稿。源码启动与正式 App 的安装身份不同；完整的后台服务和 Agent CLI 体验请使用下方构建生成的 App，或下载正式安装包。

## 检查当前产品

回到仓库根目录执行：

```sh
.venv/bin/python -B app/packaging/product_capability_gate.py \
  --repo "$PWD" --output /private/tmp/lulu-product-checks.json
```

该入口执行明确列出的 757 项产品检查，并隔离配置、模型目录和文稿写入。公开测试保留这套检查及必要夹具。请使用此入口运行检查，避免未经核查的全量测试。基础检查不代替所有模型、云服务和长时间直播的完整实测。

## 构建 App

构建还需要 PyInstaller 6.20 或以上：

```sh
.venv/bin/python -m pip install 'pyinstaller>=6.20'
```

使用正常 Git 克隆取得源码，保留 Git 历史；工作区须干净，修改后先提交。`LULU_BUILD_ROOT` 和 `LULU_DIST_ROOT` 必须使用下面的绝对路径，分别位于当前仓库的 `app/build/` 和 `app/dist/`。首次构建使用新目录或空目录。

```sh
export LULU_BUILD_ROOT="$PWD/app/build/phase10-production-20261015.1"
export LULU_DIST_ROOT="$PWD/app/dist/phase10-production-20261015.1"
export LULU_PRODUCTION_RELEASE_AUTHORIZED=1
bash app/packaging/build_app.sh --identity production --venv "$PWD/.venv"
```

完成后，App 位于 `$LULU_DIST_ROOT/Lulu.app`。脚本保留源码完整性、产品功能、隐私、签名、界面资源及包内运行检查。构建不会自动安装、替换现有 App 或上传文件。中断后重试可复用脚本确认归属的同次构建目录；不要混入其他版本文件。

公开构建默认核对已发布的公开源码基线，不依赖维护者私人提交。源码 ZIP 可以阅读和运行界面；生成正式结构的 App 请使用 Git 克隆。

## 生成安装镜像和更新包

在仓库根目录，用已构建的 App 生成标准拖拽安装镜像，输出文件须尚不存在：

```sh
PYTHONPATH="$PWD/app" .venv/bin/python app/packaging/create_drag_delivery.py \
  --app "$LULU_DIST_ROOT/Lulu.app" --output /private/tmp/Lulu-local.dmg
```

标准拖拽镜像的 Python 入口是 `app/packaging/create_drag_delivery.py` 中的 `create_delivery(app, output)`。应用内更新包由 `app/packaging/make_update_release.py` 生成。官方更新清单须由维护者使用仓库外的签名私钥签署；自行构建的 App 不自动成为官方更新。请保留安装身份、调用方验证、资料归属和更新验签检查。

当前官方安装包使用 ad-hoc 签名，尚未经过 Apple Developer ID 签名和公证。首次打开及系统授权由使用者按 macOS 提示处理。

## 其他产品资源

文稿编辑器源码和构建入口在 `app/src/ui/agent_workspace/`，已提交的网页资源可以直接使用。配音运行环境的依赖、固定版本和构建入口在 `app/packaging/tts_runtimes/`，与主程序分开。模型权重及第三方运行环境依照各自许可另行取得。

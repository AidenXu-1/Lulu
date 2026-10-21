# 从源码构建 Lulu

本版为 **4.3.1 / 20261021.7**，提供实际产品的完整第一方源码、资源、CLI、后台、Skill 与构建入口。第一方 MIT，第三方保留原许可。无需激活码、维护者私钥或维护者的个人资料。

## 环境与依赖

使用 Apple Silicon、macOS 15+、Python 3.11、Xcode Command Line Tools 和 Git。以下命令从源码根目录执行。依赖和上游源码首次获取需要联网，不会下载模型权重。

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
.venv/bin/python -m pip install 'pyinstaller==6.20.0' 'cmake==4.1.2'
```

## 原生 ASR 工具

SenseVoice Q8 和 MOSS 需要随包原生工具。其固定源码版本和完整性检查位于 app/packaging/build_asr_runtime.py。先取得以下公开源码，保持源码原样：

```sh
mkdir -p app/build/asr-sources

git clone --no-checkout https://github.com/QwenAudio/SenseVoice.git app/build/asr-sources/sensevoice-source
git -C app/build/asr-sources/sensevoice-source checkout --detach 6991744856587fa44379e8b5dcc432debffeb1be

git clone --no-checkout https://github.com/ggml-org/llama.cpp.git app/build/asr-sources/llama-source
git -C app/build/asr-sources/llama-source checkout --detach 8086439a4cea94c71a5dfb8fe4ad1546aebd640f

mkdir app/build/asr-sources/moss-source
curl --fail --location https://github.com/handy-computer/transcribe.cpp/archive/10eefba7e33d6a2fcbed0b6af82c752fe0369b42.tar.gz -o app/build/asr-sources/moss-source.tar.gz
tar -xzf app/build/asr-sources/moss-source.tar.gz --strip-components=1 -C app/build/asr-sources/moss-source

.venv/bin/python -B app/packaging/build_asr_runtime.py   --build-root "$PWD/app/build/asr-sources"   --cmake "$PWD/.venv/bin/cmake" --output "$PWD/app/build/asr-runtime"
```

构建脚本验证固定源码、MOSS 源码树摘要、arm64、macOS 15 最低目标、依赖、权限和签名。原生运行工具不含模型权重。若上游源码树不匹配，停止并检查来源，不修改期望摘要来绕过检查。

## 正式产品检查与完整 App

```sh
.venv/bin/python -B app/packaging/product_capability_gate.py   --repo "$PWD" --output /private/tmp/lulu-product-checks.json

export LULU_BUILD_ROOT="$PWD/app/build/phase10-production-20261021.7"
export LULU_DIST_ROOT="$PWD/app/dist/phase10-production-20261021.7"
export LULU_PRODUCTION_RELEASE_AUTHORIZED=1
export LULU_UPDATE_RELEASE=0
export LULU_UPDATE_PUBLIC_KEY_PATH="$PWD/app/packaging/update-verification-key.pem"
bash app/packaging/build_app.sh --identity production --venv "$PWD/.venv"
```

打包时会再次执行正式产品检查、源码与运行依赖校验、隐私、签名和包内检查。构建开关只确认要生成产品包，不上传或发布。随源码提供的 RSA 验证公钥来自正式 App，可公开；官方更新签署私钥由维护者保存在源码仓库外，构建无需该私钥。

Git 克隆使用公开祖先版本，源码 ZIP 通过 SOURCE_MANIFEST.json 校验后自动建立本地构建记录，均不依赖私人开发提交。修改源码前先按 source_repository.py 的流程准备 Git 记录，再修改并提交；构建要求干净工作区。使用新的输出目录，避免不同构建相互覆盖。

将生成的 Lulu.app 放入“应用程序”运行。已有 Lulu 时先保存资料并退出，再按系统标准方式覆盖。模型与资料独立存放，普通覆盖不会清空。

## GUI、安装镜像与配音运行时

源码开发可在 app/ 运行 ../.venv/bin/python -B run_app.py；此窗口使用本机配置，建议隔离开发资料。完整后台和 CLI 请使用自己构建并安装的 App。

标准拖拽镜像使用 app/packaging/create_drag_delivery.py，应用更新包使用 make_update_release.py。正式发布必须以最终冻结的公开源码构建、签署和核验，源码检查通过不能代替新安装包验收。

文稿编辑器的前端源码和锁文件位于 app/src/ui/agent_workspace/，已提交资源可直接使用。配音运行时的依赖、版本和构建入口位于 app/packaging/tts_runtimes/；第三方模型权重单独取得并遵守原许可。

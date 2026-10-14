# Lulu 源码与构建

Lulu 自 4.2.1 起公开完整产品源码，第一方代码采用 MIT 许可。第三方代码和模型保持各自许可，见 [第三方说明](THIRD_PARTY_NOTICES.md)。公开快照包含产品、正式测试、原生通信桥、构建工具、界面资源和随包 Agent Skill；不包含用户资料、模型权重、签名私钥或私人开发历史。

## 运行源码

正式安装包面向 Apple Silicon、macOS 15 及以上。开发与构建使用 Python 3.11；原生桥需要 Xcode Command Line Tools。首次创建环境会下载依赖，模型通过应用中的模型设置另行准备。

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r app/requirements.txt
cd app
../.venv/bin/python run_app.py
```

源码模式与正式安装包的安装身份不同，不要将源码程序指向生产任务目录来模拟正式升级。测试应使用临时 HOME、配置、资料和替代凭据；不直接运行未核查的全量测试发现。正式检查清单由 `app/packaging/product_capability_gate.py` 管理。

## 构建 App

安装与当前项目兼容的 PyInstaller 6.20 或以上。构建脚本不会创建环境、安装依赖或下载模型。必须使用干净的 Git 工作区和全新的绝对输出目录。

```sh
export LULU_BUILD_ROOT="$PWD/app/build/phase10-production-20261015.1"
export LULU_DIST_ROOT="$PWD/app/dist/phase10-production-20261015.1"
export LULU_PRODUCTION_RELEASE_AUTHORIZED=1
bash app/packaging/build_app.sh --identity production \
  --venv "$PWD/.venv" --baseline v4.2.1
```

`--baseline v4.2.1` 将已验收的公开版本作为源码完整性基线，要求候选继承该版本，并继续执行完整能力、包体、签名、隐私和运行检查。维护者原有私有历史构建不传此参数，继续核对原始完整产品基线。公开标签在正式发布时提供。

主程序的生产构建须保留任务目录、安装身份、调用方及文件归属检查。不要为了通过构建或测试关闭这些检查。正式更新包还须由维护者使用项目之外的私钥签署版本清单；第三方不能生成会被官方客户端接受的更新签名。

当前官方安装包使用 ad-hoc 签名，尚未经过 Apple Developer ID 签名和公证。系统授权须由使用者本人处理；不关闭系统安全保护、不删除隔离属性来冒充首次打开验收。

界面编辑器的开发源码及构建入口在 `app/src/ui/agent_workspace/`；提交的网页资源可直接使用。配音运行时的构建入口在 `app/packaging/tts_runtimes/`，它们与主程序分开。模型权重及下载运行时按各自许可另行取得。

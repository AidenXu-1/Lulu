# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格:Lulu.app

策略:
- 模型权重不打进 App(首次运行下载到受管模型目录),保持体积可控。
- funasr 动态导入多,用 collect_all 全量收集。
- modelscope 只保留 snapshot_download 所需 hub/fileio/utils,不全量收集模型/管线。
- imageio_ffmpeg 的 ffmpeg 二进制作为数据文件带上。
- 跑 build:  cd app && bash packaging/build_app.sh --identity candidate --venv /absolute/existing-venv
"""

import os
import json
import hashlib
import runpy
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

BUILD_PROFILE = os.environ.get("LULU_BUILD_PROFILE_FOR_SPEC", "")
if BUILD_PROFILE != "external_candidate":
    raise SystemExit("PyInstaller 构建必须由 build_app.sh 显式固定 build profile")
BUILD_IDENTITY = os.environ.get("LULU_BUILD_IDENTITY_FOR_SPEC", "")
if (BUILD_PROFILE, BUILD_IDENTITY) not in {
    ("external_candidate", "candidate"),
    ("external_candidate", "production"),
}:
    raise SystemExit("PyInstaller 构建必须显式固定 candidate 或 production 身份")
PROFILE_META = {
    "app_name": "Lulu",
    "artifact_prefix": "Lulu",
    "bundle_id": "com.lulu.transcribe",
    "entry": "run_external.py",
    "upload_target": "github-release-candidate",
}
if BUILD_IDENTITY == "candidate":
    PROFILE_META = dict(PROFILE_META, upload_target="none")
APP_NAME = PROFILE_META["app_name"]
VERSION_SOURCE = runpy.run_path(os.path.join(os.path.dirname(SPECPATH), "src", "version.py"))
PACKAGE_VERSION = VERSION_SOURCE["__version__"]
PACKAGE_BUILD = VERSION_SOURCE["__build__"]
# PyInstaller 在 spec 里注入 SPECPATH = spec 文件所在目录(即 app/packaging)
APP_DIR = os.path.dirname(SPECPATH)                  # app/
ENTRY = os.path.join(APP_DIR, PROFILE_META["entry"])
AGENT_CLI_ENTRY = os.path.join(APP_DIR, "run_agent_cli.py")
AGENT_CORE_ENTRY = os.path.join(APP_DIR, "run_agent_core.py")
AGENT_CLI_ENABLED = BUILD_PROFILE == "external_candidate"
TRUSTED_TEST_SAFE_E2E_ENABLED = False
ANALYSIS_ENTRIES = [ENTRY]
if AGENT_CLI_ENABLED:
    ANALYSIS_ENTRIES.append(AGENT_CLI_ENTRY)
    ANALYSIS_ENTRIES.append(AGENT_CORE_ENTRY)
ICON = os.path.join(SPECPATH, "AppIcon.icns")
AUTHORSHIP = os.path.join(SPECPATH, "AUTHORSHIP.json")
UPDATE_PUBLIC_KEY = os.path.join(APP_DIR, "update_public_key.pem")
UPDATE_PUBLIC_KEYS = os.path.join(APP_DIR, "update_public_keys.json")
BUILD_PROFILE_FILE = os.path.join(APP_DIR, "build_profile.json")
PRODUCT_SOURCE_FILE = os.path.join(APP_DIR, "product_source.json")
RELEASE_FEATURES_FILE = os.path.join(APP_DIR, "release_features.json")
AGENT_PRODUCT_MANIFEST = os.path.join(SPECPATH, "component_delivery.v2.json")
AGENT_CAPABILITY_CATALOG = os.path.join(APP_DIR, "src", "agent_cli", "capability_catalog.json")
AGENT_SKILL_SOURCE = os.path.join(APP_DIR, "skills", "lulu-agent")
AGENT_XPC_BRIDGE = os.environ.get("LULU_AGENT_XPC_BRIDGE_FOR_SPEC", "")
AGENT_LAUNCH_PLIST = os.path.join(SPECPATH, "com.lulu.transcribe.agent-core.plist")
AGENT_TRANSPORT_POLICY = os.path.join(SPECPATH, "LuluTransportPolicy.v1.plist")
PROFILE_GATE = runpy.run_path(os.path.join(SPECPATH, "profile_gate.py"))
PRIVACY_GATE = runpy.run_path(os.path.join(SPECPATH, "privacy_gate.py"))

datas, binaries, hiddenimports = [], [], []

# Mandatory anonymous download runtime: never depend on the developer's PATH.
from importlib.metadata import version as distribution_version
from deno import find_deno_bin
download_versions = runpy.run_path(os.path.join(APP_DIR, "src/core/audio/download_runtime.py"))["VERSIONS"]
for package, expected in download_versions.items():
    if distribution_version(package) != expected:
        raise SystemExit(f"下载组件版本不匹配: {package}, expected {expected}")
    datas += copy_metadata(package)
ejs_data, ejs_binaries, ejs_imports = collect_all("yt_dlp_ejs")
datas += ejs_data
binaries += ejs_binaries + [(find_deno_bin(), "download_runtime")]
hiddenimports += ejs_imports

def _module_allowed(name, *, exact=(), prefixes=()):
    return name in exact or any(name.startswith(prefix + ".") for prefix in prefixes)


def _dest_matches(dest, prefixes):
    normalized = dest.replace(os.sep, "/")
    return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in prefixes)


def _drop_datas_by_dest(items, prefixes):
    return [item for item in items if not _dest_matches(item[1], prefixes)]


def _drop_toc_entries(items, markers):
    kept = []
    for item in items:
        haystack = " ".join(str(part).replace(os.sep, "/") for part in item)
        if any(marker in haystack for marker in markers):
            continue
        kept.append(item)
    return kept


# 动态导入/数据文件大户,全量收集。本地多模型运行时:qwen_asr(Qwen3)、mlx*(MLX Whisper)
for pkg in ("funasr", "dashscope",
            "qwen_asr", "qwen_omni_utils", "mlx", "mlx_whisper",
            "nagisa", "nagisa_utils",
            "DrissionPage"):  # 路B 登录态采集:动态导入多,全量收集,否则打包后路B 崩
            # 注:voxcpm/配音模型已移出主程序(ADR-0015 独立运行时包),不再随 App 打包。
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # mlx 等仅 Apple Silicon 有;非该平台打包时跳过

# MLX loads its Metal library next to the loaded libmlx.dylib. PyInstaller
# hoists @rpath/libmlx.dylib to Frameworks; collect_all only keeps the original
# mlx/lib data. Collect at the root too so both native load locations work.
from importlib.metadata import distribution
mlx_metallib = distribution("mlx-metal").locate_file("mlx/lib/mlx.metallib")
if not mlx_metallib.is_file():
    raise SystemExit("MLX GPU 运算文件缺失，停止打包")
datas.append((str(mlx_metallib), "."))

# transformers.audio_utils 会在 Qwen3-ASR 导入链路里通过 importlib.metadata.version("torchcodec")
# 判断音频解码能力。PyInstaller 默认可能只收模块、不收 dist-info,导致打包态用户可见
# "No package metadata was found for torchcodec"。
try:
    datas += copy_metadata("torchcodec")
except Exception:
    pass

# ModelScope 在 App 内只用于 snapshot_download/模型受管目录下载。
# 不收集 models/pipelines/trainers 等全量模块,避免把大量非转录/下载链路依赖塞进主包。
try:
    d, b, h = collect_all("modelscope")
    datas += _drop_datas_by_dest(d, (
        "modelscope/models/cv",
        "modelscope/models/multi_modal",
        "modelscope/models/nlp",
        "modelscope/models/science",
        "modelscope/pipelines/cv",
        "modelscope/pipelines/multi_modal",
        "modelscope/pipelines/nlp",
        "modelscope/pipelines/science",
        "modelscope/trainers",
        "modelscope/msdatasets",
        "modelscope/preprocessors",
        "modelscope/metrics",
        "modelscope/exporters",
        "modelscope/server",
        "modelscope/ops",
    ))
    binaries += b
    hiddenimports += [
        name for name in h
        if _module_allowed(
            name,
            exact=("modelscope", "modelscope.version", "modelscope.metainfo"),
            prefixes=("modelscope.hub", "modelscope.utils", "modelscope.fileio", "modelscope.outputs"),
        )
    ]
except Exception:
    pass

# imageio_ffmpeg 自带的 ffmpeg 二进制
import imageio_ffmpeg
_ff = imageio_ffmpeg.get_ffmpeg_exe()
datas += [(_ff, "imageio_ffmpeg/binaries")]

# 界面 assets:品牌 logo + 侧栏 Obsidian/飞书图标(整目录打包,新增图片自动收)
datas += [(os.path.join(APP_DIR, "src", "ui", "assets"), "src/ui/assets"),
          (os.path.join(APP_DIR, "src", "ui", "extraction", "assets"), "src/ui/extraction/assets"),
          (os.path.join(APP_DIR, "src", "ui", "extraction", "index.html"), "src/ui/extraction")]
datas += [(os.path.join(APP_DIR, "src", "ui", "model_settings", "assets"), "src/ui/model_settings/assets"),
          (os.path.join(APP_DIR, "src", "ui", "model_settings", "public.json"), "src/ui/model_settings")]
datas += [(os.path.join(APP_DIR, "src", "ui", "agent_workspace", "assets"), "src/ui/agent_workspace/assets"),
          (os.path.join(APP_DIR, "src", "ui", "agent_workspace", "index.html"), "src/ui/agent_workspace"),
          (os.path.join(APP_DIR, "src", "core", "agent_workspace", "catalog.json"), "src/core/agent_workspace")]

# 首版包作者归属元数据/取证水印:只作为 Resources 静态文件,不参与运行时逻辑。
if os.path.exists(AUTHORSHIP):
    datas += [(AUTHORSHIP, ".")]

# Free official builds contain no legacy license endpoint configuration.
if BUILD_PROFILE == "external_candidate" and os.path.exists(UPDATE_PUBLIC_KEY):
    datas += [(UPDATE_PUBLIC_KEY, ".")]
if BUILD_PROFILE == "external_candidate" and os.path.exists(UPDATE_PUBLIC_KEYS):
    datas += [(UPDATE_PUBLIC_KEYS, ".")]
if not os.path.exists(BUILD_PROFILE_FILE):
    raise SystemExit("缺少构建身份文件")
datas += [(BUILD_PROFILE_FILE, ".")]
if not os.path.exists(PRODUCT_SOURCE_FILE):
    raise SystemExit("缺少固定产品源码封印")
datas += [(PRODUCT_SOURCE_FILE, ".")]
if AGENT_CLI_ENABLED:
    if not os.path.exists(AGENT_PRODUCT_MANIFEST):
        raise SystemExit("Agent CLI产品入口缺少产品清单")
    datas += [(AGENT_PRODUCT_MANIFEST, ".")]
    if not os.path.isfile(AGENT_CAPABILITY_CATALOG):
        raise SystemExit("Agent CLI产品入口缺少能力目录")
    datas += [(AGENT_CAPABILITY_CATALOG, "src/agent_cli")]
    if not os.path.isdir(AGENT_SKILL_SOURCE):
        raise SystemExit("Agent CLI产品入口缺少Skill源")
    datas += [(AGENT_SKILL_SOURCE, "skills/lulu-agent")]
    if not os.path.isfile(AGENT_XPC_BRIDGE):
        raise SystemExit("Agent Core缺少已编译XPC bridge")
    datas += [(AGENT_XPC_BRIDGE, ".")]
    datas += [(AGENT_LAUNCH_PLIST, "."), (AGENT_TRANSPORT_POLICY, ".")]
# Ship only the supported system skill catalog and its local packages.
_internal_root = os.path.join(APP_DIR, "internal-skills")
datas += [(os.path.join(_internal_root, "catalog.json"), "internal-skills")]
with open(os.path.join(_internal_root, "catalog.json"), encoding="utf-8") as _catalog_file:
    for _skill in json.load(_catalog_file):
        if _skill["origin"] == "system":
            datas += [(os.path.join(_internal_root, "packages", _skill["name"]),
                       "internal-skills/packages/" + _skill["name"])]
with open(BUILD_PROFILE_FILE, "rb") as _profile_handle:
    BUILD_PROFILE_SHA256 = hashlib.sha256(_profile_handle.read()).hexdigest()

# 本地 TTS 独立运行时 worker:打包态不能用 sys.executable 再执行脚本,
# 需把 stdlib-only worker 源文件随包放入 Resources,再由受管运行时 Python 执行。
datas += [
    (os.path.join(APP_DIR, "src", "core", "synthesis", "indextts2_worker.py"), "src/core/synthesis"),
    (os.path.join(APP_DIR, "src", "core", "synthesis", "voxcpm2_worker.py"), "src/core/synthesis"),
    (os.path.join(APP_DIR, "src", "core", "synthesis", "qwen3_worker.py"), "src/core/synthesis"),
]

# VoxCPM2 第一模型切片第三方许可证/NOTICE 入口
datas += [(os.path.join(APP_DIR, "THIRD_PARTY_NOTICES_VOXCPM2.md"), ".")]

# 抖音采集第三方许可证/NOTICE 入口
# (F2 Apache-2.0 署名 + DrissionPage 自定义「非商业」许可——许可风险见 NOTICE/ADR-0013)
datas += [(os.path.join(APP_DIR, "THIRD_PARTY_NOTICES_DOUYIN_COLLECT.md"), ".")]
datas += [(os.path.join(APP_DIR, "THIRD_PARTY_NOTICES_LIVE_RECORDING.md"), "."),
          (os.path.join(APP_DIR, "src/core/live_recording/STREAMGET_LICENSE.txt"), "src/core/live_recording")]

# 一些懒导入 / 间接依赖,显式补上以防漏
hiddenimports += collect_submodules("torchaudio")
hiddenimports += [
    "torch", "torchaudio", "kaldiio", "jieba", "jaconv",
    "yt_dlp", "PySide6.QtSvg", "PySide6.QtMultimedia", "requests", "soundfile", "sox",
    "mlx", "mlx.core", "mlx_whisper", "qwen_asr",
    "src",
    "src.core.pipeline", "src.core.engines.local_sensevoice",
    "src.core.engines.online_api", "src.core.engines.registry",
    "src.core.audio.extractor", "src.core.audio.converter",
    "src.core.output.writer",
    # ADR-0024：macOS 任务结果通知的 UserNotifications 窄 PyObjC 桥。依赖版本由 requirements 锁定。
    "objc", "Foundation", "UserNotifications",
]
if not TRUSTED_TEST_SAFE_E2E_ENABLED:
    hiddenimports += ["src.ui.main_window", "src.ui.worker"]
if BUILD_PROFILE == "external_candidate":
    hiddenimports += [
        "keyring.backends.macOS", "keyring.backends.macOS.api", "keyring.backends.SecretService",
        "keyring.backends.chainer", "keyring.backends.fail",
    ]
if BUILD_PROFILE == "external_candidate":
    hiddenimports += ["src.main"]

RUNTIME_HOOKS = [os.path.join(SPECPATH, "rthook_qwen_stubs.py")]
if BUILD_PROFILE == "external_candidate":
    RUNTIME_HOOKS.append(
        os.path.join(APP_DIR, "src", "agent_cli", "gui_host_runtime_hook.py")
    )


a = Analysis(
    ANALYSIS_ENTRIES,
    pathex=[APP_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(SPECPATH, "hooks")],
    hooksconfig={},
    runtime_hooks=RUNTIME_HOOKS,
    excludes=[
        "src.ui.live_clips_page", "src.core.live_clips", "src.core.content_output",
        "tkinter", "matplotlib", "PyQt5", "PyQt6", "PIL",
        "tensorboard", "tensorflow", "notebook", "IPython",
        # funasr/modelscope 的传递依赖,App 代码零 import、且实测屏蔽后本地 SenseVoice
        # 转录仍正常(数据集加载用,非模型下载/推理用)。排除省 ~170MB。
        # (首次模型下载的最终确认放干净机器重打时;若下载报缺再回收。)
        "pyarrow", "av", "pandas", "datasets",
        # 注:numba/llvmlite(~110MB)被 mlx_whisper 的 timing 模块依赖,不能排除。
        # The document workspace uses one WebEngine and WebChannel instance.
        # Their PyInstaller hooks must collect the helper process, locales and
        # resources. Qt Quick's Python API is not used by this Widgets shell.
        "PySide6.QtWebEngineQuick", "PySide6.QtWebSockets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
        "PySide6.QtQml", "PySide6.QtQmlModels",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
        # QtMultimedia 用于麦克风录音,保留;其视频/3D 音频附件不需要,排除
        "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
        "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
        "PySide6.QtSql", "PySide6.QtTest",
        "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
        "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtSerialBus", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
        "PySide6.QtNetworkAuth", "PySide6.QtHttpServer", "PySide6.QtTextToSpeech",
        "PySide6.QtShaderTools",
        # ModelScope 仅作下载器,不把模型库/管线库编入主 App。
        "modelscope.models.cv", "modelscope.models.multi_modal", "modelscope.models.nlp",
        "modelscope.models.science", "modelscope.pipelines.cv", "modelscope.pipelines.multi_modal",
        "modelscope.pipelines.nlp", "modelscope.pipelines.science", "modelscope.trainers",
        "modelscope.msdatasets", "modelscope.preprocessors", "modelscope.metrics",
        "modelscope.exporters", "modelscope.server", "modelscope.ops",
        # Retired activation and internal entries must never enter product bundles.
        "src.internal_bootstrap", "src.internal_main", "src.core.license.mock_transport",
        "src.core.license.service", "src.core.license._service_core",
        "src.core.license.external_service", "src.core.license.core_service",
        "src.core.license.transport", "src.core.license.device",
        "src.core.cli_runtime.license_projection", "src.ui.license_gate",
        "src.ui.license_device_page", "src.ui.license_contact",
    ],
    noarchive=False,
    # voxcpm 已移出主程序(ADR-0015 独立运行时包),不再需要其 TorchScript 源码收集。
)

# PyInstaller 的 PySide6 钩子有时仍会带入排除列表里的 QtPdf/QtQuick/Qml framework。
# WebEngine 的原生库依赖 QtQuick/Qml，即使没有导入对应 Python API 也必须保留。
# 只移除不在实际运行依赖中的 PDF 附件，同时移除其源、目的和链接。
_QT_UNUSED_FRAMEWORK_MARKERS = (
    "QtPdf.framework",
)
a.binaries = _drop_toc_entries(a.binaries, _QT_UNUSED_FRAMEWORK_MARKERS)
a.datas = _drop_toc_entries(a.datas, _QT_UNUSED_FRAMEWORK_MARKERS)
# collect_all/copy_metadata 偶尔会把第三方包自身的 __pycache__ / .pyc
# 当作数据文件带入。它们对运行无用，还可能携带构建机绝对路径。
a.binaries = PRIVACY_GATE["drop_cache_toc_entries"](a.binaries)
a.datas = PRIVACY_GATE["drop_cache_toc_entries"](a.datas)
# collect_all 可能携带第三方项目自己的测试数据。这里只删除已经逐项确认
# 不参与 Lulu 运行时导入的精确目录，避免用宽泛 tests 规则误伤模型资源。
a.binaries = PRIVACY_GATE["drop_non_runtime_toc_entries"](a.binaries)
a.datas = PRIVACY_GATE["drop_non_runtime_toc_entries"](a.datas)

PROFILE_GATE["write_analysis_receipt"](
    os.path.join(workpath, "profile-analysis.json"),
    BUILD_PROFILE,
    list(a.pure) + list(a.scripts),
    list(a.datas) + list(a.binaries) + [("AppIcon.icns", ICON, "DATA")],
    BUILD_PROFILE_SHA256,
)

pyz = PYZ(a.pure)


def _scripts_for_entry(entry_path):
    """Keep all runtime hooks while selecting exactly one program script."""
    program_names = {
        os.path.splitext(os.path.basename(path))[0]
        for path in ANALYSIS_ENTRIES
    }
    entry_name = os.path.splitext(os.path.basename(entry_path))[0]
    return [
        item
        for item in a.scripts
        if item[0] not in program_names or item[0] == entry_name
    ]


gui_exe = EXE(
    pyz,
    _scripts_for_entry(ENTRY),
    [],
    exclude_binaries=True,
    name=PROFILE_META["artifact_prefix"],
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,                # strip 原生库符号表,省 ~10-20%;UPX 仍关(会破坏 codesign)
    upx=False,
    console=TRUSTED_TEST_SAFE_E2E_ENABLED,
    disable_windowed_traceback=False,
    argv_emulation=not TRUSTED_TEST_SAFE_E2E_ENABLED,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

agent_cli_exe = None
agent_core_exe = None
if AGENT_CLI_ENABLED:
    agent_cli_exe = EXE(
        pyz,
        _scripts_for_entry(AGENT_CLI_ENTRY),
        [],
        exclude_binaries=True,
        name="lulu-agent-cli",
        debug=False,
        bootloader_ignore_signals=False,
        strip=True,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch="arm64",
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
    agent_core_exe = EXE(
        pyz,
        _scripts_for_entry(AGENT_CORE_ENTRY),
        [],
        exclude_binaries=True,
        name="lulu-agent-core",
        debug=False,
        bootloader_ignore_signals=False,
        strip=True,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch="arm64",
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )

collect_executables = [gui_exe]
if agent_cli_exe is not None:
    collect_executables.append(agent_cli_exe)
if agent_core_exe is not None:
    collect_executables.append(agent_core_exe)

coll = COLLECT(
    *collect_executables,
    a.binaries,
    a.datas,
    strip=True,
    upx=False,
    upx_exclude=[],
    name=PROFILE_META["artifact_prefix"],
)

app = BUNDLE(
    coll,
    name=f"{PROFILE_META['artifact_prefix']}.app",
    icon=ICON,
    bundle_identifier=PROFILE_META["bundle_id"],
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "15.0",
        "CFBundleShortVersionString": PACKAGE_VERSION,
        "CFBundleVersion": PACKAGE_BUILD,
        "LuluBuildProfile": BUILD_PROFILE,
        "LuluBuildIdentity": BUILD_IDENTITY,
        "LuluUploadTarget": PROFILE_META["upload_target"],
        "CFBundleGetInfoString": f"{APP_NAME} {PACKAGE_VERSION} ({PACKAGE_BUILD}) · 作者:兆基 · 2026",
        "NSHumanReadableCopyright": "Copyright © 2026 兆基. Lulu.",
        # 麦克风录音权限说明(缺这条 macOS 会直接杀进程)
        "NSMicrophoneUsageDescription": "Lulu 需要使用麦克风来录制音频并转写为文字(录音只在本机处理)。",
    },
)

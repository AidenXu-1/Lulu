"""应用配置:轻量、本地持久化。

只存无隐私的偏好(上次用的引擎、输出格式、链接开关、输出目录)。
绝不存音频/逐字稿内容。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
import tempfile
from threading import RLock

from .core.engines.registry import DEFAULT_ENGINE

_CONFIG_DIR = Path.home() / ".本地转录app"
_CONFIG_PATH = _CONFIG_DIR / "config.json"
_CONFIG_SAVE_LOCK = RLock()
COLLECT_COVER_CONTRACT_VERSION = 1
COLLECTION_MODES = frozenset({"partial", "full"})
FEISHU_STORAGE_CONTRACT_VERSION = 3
FEISHU_DRIVE_ROOT_PRESET_ID = "feishu-destination-drive-root"
_FEISHU_TARGET_STATUSES = frozenset(
    {"pending_validation", "ready", "unavailable"}
)
_FEISHU_TARGET_SOURCE_KINDS = frozenset({"existing", "created"})
_FEISHU_CREATION_DESTINATION_KINDS = frozenset({"drive", "wiki"})


def _fsync_config_file(handle: object) -> None:
    os.fsync(handle.fileno())  # type: ignore[attr-defined]


def _fsync_config_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_config(source: Path, target: Path) -> None:
    os.replace(source, target)


def _write_config_temp(parent: Path, payload: bytes) -> Path:
    descriptor, raw_path = tempfile.mkstemp(dir=parent, prefix=".config.json.tmp-")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            _fsync_config_file(handle)
        return path
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def _validate_config_payload(payload: bytes) -> dict:
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("配置顶层必须是对象")
    return document


def _normalize_collect_cover(data: dict) -> None:
    value = data.get("collect_cover", False)
    marker = data.get("collect_cover_contract_version", 0)
    if (
        not isinstance(value, bool)
        or isinstance(marker, bool)
        or marker != COLLECT_COVER_CONTRACT_VERSION
    ):
        data["collect_cover"] = False


def _normalize_material_preferences(data: dict) -> None:
    if data.get("link_media_mode", "audio") not in {"audio", "video"}:
        data["link_media_mode"] = "audio"
    if not isinstance(data.get("keep_media", False), bool):
        data["keep_media"] = False


def _normalize_collection_mode(data: dict) -> None:
    if data.get("collection_mode") not in COLLECTION_MODES:
        data["collection_mode"] = "partial"


def _config_text(value: object, *, limit: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _normalize_feishu_creation_target(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    space_id = _config_text(raw.get("space_id"), limit=1024)
    destination_kind = _config_text(raw.get("destination_kind"), limit=32)
    if destination_kind not in _FEISHU_CREATION_DESTINATION_KINDS:
        destination_kind = "wiki" if space_id else ""
    if not destination_kind:
        return {}
    if destination_kind == "wiki" and not space_id:
        return {}
    if destination_kind == "drive":
        return {
            "destination_kind": "drive",
            "name": _config_text(raw.get("name"), limit=240)
            or "云盘（我的文稿库）",
        }
    return {
        "destination_kind": "wiki",
        "space_id": space_id,
        "node_token": _config_text(raw.get("node_token"), limit=1024),
        "name": _config_text(raw.get("name"), limit=240) or "飞书知识库",
    }


def _stable_feishu_creation_preset_id(target: dict) -> str:
    kind = str(target.get("destination_kind") or "")
    if kind == "drive":
        return FEISHU_DRIVE_ROOT_PRESET_ID
    identity = "\0".join(
        (
            kind,
            str(target.get("space_id") or ""),
            str(target.get("node_token") or ""),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "feishu-destination-" + digest


def _normalize_feishu_creation_preset(value: object) -> dict | None:
    raw = value if isinstance(value, dict) else {}
    target = _normalize_feishu_creation_target(raw)
    if not target:
        return None
    preset_id = _config_text(raw.get("preset_id"), limit=160)
    if target["destination_kind"] == "drive":
        preset_id = FEISHU_DRIVE_ROOT_PRESET_ID
    elif not preset_id:
        preset_id = _stable_feishu_creation_preset_id(target)
    return {"preset_id": preset_id, **target}


def _feishu_creation_identity(target: dict) -> str:
    return "\0".join(
        (
            str(target.get("destination_kind") or ""),
            str(target.get("space_id") or ""),
            str(target.get("node_token") or ""),
        )
    )


def _stable_feishu_target_id(
    *,
    url: str,
    app_token: str,
    table_id: str,
) -> str:
    identity = "\0".join((url, app_token, table_id))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "feishu-table-" + digest


def _normalize_feishu_table_target(value: object) -> dict | None:
    raw = value if isinstance(value, dict) else {}
    url = _config_text(raw.get("url"))
    app_token = _config_text(raw.get("app_token"), limit=1024)
    table_id = _config_text(raw.get("table_id"), limit=1024)
    if not url and not (app_token and table_id):
        return None

    target_id = _config_text(raw.get("target_id"), limit=160) or (
        _stable_feishu_target_id(
            url=url,
            app_token=app_token,
            table_id=table_id,
        )
    )
    name = _config_text(raw.get("name"), limit=240) or "飞书作品库"
    status = _config_text(raw.get("status"), limit=64)
    if status not in _FEISHU_TARGET_STATUSES:
        status = "ready" if app_token and table_id else "pending_validation"
    schema_version = raw.get("schema_version", 0)
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 0
    ):
        schema_version = 0
    field_map = raw.get("field_map")
    if not isinstance(field_map, dict):
        field_map = {}
    source_kind = _config_text(raw.get("source_kind"), limit=32)
    if source_kind not in _FEISHU_TARGET_SOURCE_KINDS:
        # 旧版本没有记录目标来源。保守归入“导入已有表格”，避免升级后
        # 把用户自己的表误标成由 Lulu 创建。
        source_kind = "existing"

    normalized = {
        "target_id": target_id,
        "name": name,
        "url": url,
        "app_token": app_token,
        "table_id": table_id,
        "template_id": _config_text(raw.get("template_id"), limit=160),
        "schema_version": schema_version,
        "field_map": dict(field_map),
        "status": status,
        "source_kind": source_kind,
    }
    for key in ("last_verified_at", "last_used_at", "account_id"):
        text = _config_text(raw.get(key), limit=80)
        if text:
            normalized[key] = text
    return normalized


def _feishu_target_identity(target: dict) -> str:
    url = str(target.get("url") or "")
    account = str(target.get('account_id') or '')
    prefix = account + '\0' if account else ''
    if url:
        return prefix + f"url:{url}"
    return prefix + "table:" + "\0".join(
        (str(target.get("app_token") or ""), str(target.get("table_id") or ""))
    )


def _normalize_feishu_storage(data: dict) -> None:
    """纯本地迁移旧飞书配置，并保持多目标配置可重复规范化。"""

    legacy = data.get("feishu_target")
    legacy = dict(legacy) if isinstance(legacy, dict) else {}
    data["feishu_target"] = legacy

    creation_target = _normalize_feishu_creation_target(
        data.get("feishu_creation_target")
    )
    if not creation_target:
        creation_target = _normalize_feishu_creation_target(legacy)
    data["feishu_creation_target"] = creation_target

    raw_creation_presets = data.get("feishu_creation_presets")
    creation_candidates = (
        list(raw_creation_presets) if isinstance(raw_creation_presets, list) else []
    )
    creation_candidates.insert(
        0,
        {
            "preset_id": FEISHU_DRIVE_ROOT_PRESET_ID,
            "destination_kind": "drive",
            "name": "云盘（我的文稿库）",
        },
    )
    # 只在旧配置尚无目的地列表时迁移单一位置。v3 用户主动忘记某个
    # 知识库预设后，不能再被兼容字段自动加回来。
    if creation_target and not raw_creation_presets:
        creation_candidates.append(creation_target)
    normalized_creation_presets: list[dict] = []
    creation_identities: set[str] = set()
    creation_ids: set[str] = set()
    for candidate in creation_candidates:
        preset = _normalize_feishu_creation_preset(candidate)
        if preset is None:
            continue
        identity = _feishu_creation_identity(preset)
        if identity in creation_identities:
            continue
        preset_id = str(preset["preset_id"])
        if preset_id in creation_ids:
            preset["preset_id"] = _stable_feishu_creation_preset_id(preset)
            preset_id = str(preset["preset_id"])
            if preset_id in creation_ids:
                continue
        creation_identities.add(identity)
        creation_ids.add(preset_id)
        normalized_creation_presets.append(preset)
    data["feishu_creation_presets"] = normalized_creation_presets

    raw_targets = data.get("feishu_table_targets")
    candidates = list(raw_targets) if isinstance(raw_targets, list) else []
    legacy_url = _config_text(legacy.get("bitable_url"))
    if legacy_url and not any(isinstance(item, dict) and _config_text(item.get('url')) == legacy_url for item in candidates):
        candidates.append(
            {
                "name": _config_text(legacy.get("bitable_name"), limit=240)
                or "已有飞书作品库",
                "url": legacy_url,
                "status": "pending_validation",
            }
        )

    normalized_targets: list[dict] = []
    identities: set[str] = set()
    target_ids: set[str] = set()
    for candidate in candidates:
        target = _normalize_feishu_table_target(candidate)
        if target is None:
            continue
        identity = _feishu_target_identity(target)
        if identity in identities:
            continue
        target_id = str(target["target_id"])
        if target_id in target_ids:
            target["target_id"] = _stable_feishu_target_id(
                url=str(target.get("url") or ""),
                app_token=str(target.get("app_token") or ""),
                table_id=str(target.get("table_id") or ""),
            )
            target_id = str(target["target_id"])
            if target_id in target_ids:
                continue
        identities.add(identity)
        target_ids.add(target_id)
        normalized_targets.append(target)
    data["feishu_table_targets"] = normalized_targets

    raw_preferences = data.get("feishu_storage_preferences")
    raw_preferences = raw_preferences if isinstance(raw_preferences, dict) else {}
    preference_keys = (
        "single_target_id",
        "selection_target_id",
        "existing_single_target_id",
        "existing_selection_target_id",
        "created_single_destination_id",
        "created_selection_destination_id",
        "recent_target_id",
    )
    preferences = {
        key: _config_text(raw_preferences.get(key), limit=160)
        for key in preference_keys
    }
    if legacy_url and normalized_targets and not preferences["recent_target_id"]:
        legacy_identity = f"url:{legacy_url}"
        migrated = next(
            (
                target
                for target in normalized_targets
                if _feishu_target_identity(target) == legacy_identity
            ),
            None,
        )
        if migrated is not None:
            preferences["recent_target_id"] = str(migrated["target_id"])
            if not preferences["selection_target_id"]:
                preferences["selection_target_id"] = str(migrated["target_id"])
    # v1 只有一组单篇/主页默认值，且所有旧目标按 existing 迁移。
    # 首次升级时复制到“导入已有表格”分组，原键继续保留供业务出口选择。
    if not preferences["existing_single_target_id"]:
        preferences["existing_single_target_id"] = preferences["single_target_id"]
    if not preferences["existing_selection_target_id"]:
        preferences["existing_selection_target_id"] = preferences["selection_target_id"]
    for key in (
        "single_target_id",
        "selection_target_id",
        "existing_single_target_id",
        "existing_selection_target_id",
        "recent_target_id",
    ):
        target_id = preferences[key]
        if target_id and target_id not in target_ids:
            preferences[key] = ""
    for key in (
        "created_single_destination_id",
        "created_selection_destination_id",
    ):
        if preferences[key] and preferences[key] not in creation_ids:
            preferences[key] = ""
    data["feishu_storage_preferences"] = preferences
    data["feishu_storage_contract_version"] = FEISHU_STORAGE_CONTRACT_VERSION


def _normalize_feishu_storage_config(config: object) -> None:
    data = {
        "feishu_target": getattr(config, "feishu_target", {}),
        "feishu_creation_target": getattr(config, "feishu_creation_target", {}),
        "feishu_creation_presets": getattr(config, "feishu_creation_presets", []),
        "feishu_table_targets": getattr(config, "feishu_table_targets", []),
        "feishu_storage_preferences": getattr(
            config, "feishu_storage_preferences", {}
        ),
        "feishu_storage_contract_version": getattr(
            config, "feishu_storage_contract_version", 0
        ),
    }
    _normalize_feishu_storage(data)
    for key, value in data.items():
        setattr(config, key, value)


@dataclass
class AppConfig:
    engine_name: str = DEFAULT_ENGINE
    theme: str = "warm"                 # 兼容旧配置字段；当前固定使用 Lulu 宣纸。
    window_geometry: dict = field(default_factory=dict)
    enable_link_download: bool = True   # 链接取音频开关(置 False = 纯离线,拒绝任何链接)
    last_output_dir: str = str(Path.home() / "Desktop")
    keep_media: bool = False            # 是否保留从链接下载的原始音视频
    media_dir: str = str(Path.home() / "Downloads")  # 保留时的存放目录
    link_media_mode: str = "audio"      # 链接下载模式:audio=仅音频(更快)|video=音视频(保留原视频)
    recording_dir: str = str(Path.home() / "Downloads")  # 麦克风录音的存放目录
    default_tts_model: str = "local_indextts2"  # 新打开的配音工作台默认模型
    tts_output_dir: str = str(Path.home() / "Downloads")  # 克隆音导出的默认目录
    indextts2_generation_options: dict = field(default_factory=dict)
    qwen3_generation_options: dict = field(default_factory=dict)
    voxcpm2_generation_options: dict = field(default_factory=dict)
    voxcpm2_pronunciations: list = field(default_factory=list)
    indextts25_generation_options: dict = field(default_factory=dict)
    voice_library_dir: str = ""           # 音色库保存目录(空=App 受管默认目录)
    tts_model_dir: str = ""                 # 克隆音模型目录(空=App 受管模型目录)
    models_dir: str = ""                    # 本地模型安装根目录(空=App 受管目录);ASR models/ + TTS tts/ 都在它下
    obsidian_dir: str = ""               # 「存入 Obsidian」的目标文件夹(空=未设置)
    # 「存入飞书知识库」的目标位置(非敏感;凭据 app_id/app_secret 走 keyring):
    # {"space_id": str, "node_token": str, "name": str}
    feishu_target: dict = field(default_factory=dict)
    # 旧版单一新建位置；保留用于配置迁移，不再作为设置页独立板块。
    feishu_creation_target: dict = field(default_factory=dict)
    # 新建多维表格的目的地预设：云盘（我的文稿库）或知识库层级。
    feishu_creation_presets: list[dict] = field(default_factory=list)
    # 多个长期作品库目标；只存非敏感标识、链接、模板与字段映射。
    feishu_table_targets: list[dict] = field(default_factory=list)
    # 单条/筛选归档默认目标与最近目标的 target_id。
    feishu_storage_preferences: dict = field(default_factory=dict)
    # Only the last successful destination, isolated by its verified account identity.
    feishu_last_save: dict = field(default_factory=dict)
    feishu_storage_contract_version: int = FEISHU_STORAGE_CONTRACT_VERSION
    prefer_subtitles: bool = False       # 链接有平台字幕时,优先直接用字幕(跳过本地转写)
    # 文案处理各方向上次选的提示词(非敏感偏好):
    # {"analyze": {"mode": "template|custom", "template_id": str, "custom_text": str}, ...}
    copywriting_prompts: dict = field(default_factory=dict)
    copywriting_cloud_provider: str = "agnes"  # 文案处理云端模型:agnes|deepseek(非敏感偏好)
    # 用户自建的命名模板,按方向各一套(有序,顺序=展示顺序):
    # {"analyze": [{"id": str, "name": str, "text": str}, ...], ...}
    copywriting_user_templates: dict = field(default_factory=dict)
    auto_update_enabled: bool = True    # 每24小时匿名检查；手动检查始终可用
    notification_policy: str = "never"  # always | inactive | never；默认不申请系统权限
    notify_on_failure: bool = True      # 失败任务沿用通知策略发送固定隐私提醒
    notification_sound_enabled: bool = True  # 完成通知是否附带系统默认提示音
    prevent_sleep_during_tasks: bool = False  # 处理任务时用系统 caffeinate 防止自动休眠
    confirm_exit_during_tasks: bool = True  # 活动任务期间退出前确认
    completion_action: str = "stay"    # stay=停留当前页 | open_result=打开对应结果
    collect_cover: bool = False         # 新任务是否尝试安全下载封面；默认关闭
    collect_cover_contract_version: int = 0  # 用户确认过真实下载语义的迁移标记
    collection_mode: str = "partial"    # 主页采集方式:partial|full；非法值保守回部分采集

    def __post_init__(self) -> None:
        self.theme = "warm"
        _normalize_feishu_storage_config(self)

    # --- 持久化 ---
    @classmethod
    def load(cls) -> "AppConfig":
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            _normalize_collect_cover(known)
            _normalize_material_preferences(known)
            _normalize_collection_mode(known)
            _normalize_feishu_storage(known)
            return cls(**known)
        except Exception:
            return cls()

    def _restore_memory(self, previous: bytes | None) -> None:
        """保存失败时恢复磁盘上的上一份偏好；无可读旧文件则回到默认值。"""
        restored = type(self)()
        if previous is not None:
            try:
                data = _validate_config_payload(previous)
                known = {
                    key: value
                    for key, value in data.items()
                    if key in type(self).__dataclass_fields__
                }
                _normalize_collect_cover(known)
                _normalize_material_preferences(known)
                _normalize_collection_mode(known)
                _normalize_feishu_storage(known)
                restored = type(self)(**known)
            except Exception:
                pass
        for config_field in fields(self):
            setattr(self, config_field.name, getattr(restored, config_field.name))

    def save(self) -> bool:
        with _CONFIG_SAVE_LOCK:
            self.theme = "warm"
            _normalize_feishu_storage_config(self)
            config_dir = _CONFIG_PATH.parent
            previous: bytes | None = None
            temp_path: Path | None = None
            rollback_temp: Path | None = None
            rollback_path = _CONFIG_PATH.with_name(f"{_CONFIG_PATH.name}.rollback")
            replaced = False
            try:
                if _CONFIG_PATH.exists():
                    previous = _CONFIG_PATH.read_bytes()
                config_dir.mkdir(parents=True, exist_ok=True)
                payload = (json.dumps(
                    asdict(self), ensure_ascii=False, indent=2, sort_keys=True
                ) + "\n").encode("utf-8")
                _validate_config_payload(payload)
                temp_path = _write_config_temp(config_dir, payload)
                if _CONFIG_PATH.exists():
                    rollback_temp = _write_config_temp(config_dir, previous or b"")
                    _replace_config(rollback_temp, rollback_path)
                    rollback_temp = None
                    _fsync_config_directory(config_dir)

                _replace_config(temp_path, _CONFIG_PATH)
                temp_path = None
                replaced = True
                _fsync_config_directory(config_dir)
                if _CONFIG_PATH.read_bytes() != payload:
                    raise OSError("配置替换后校验不一致")
                _validate_config_payload(_CONFIG_PATH.read_bytes())
                rollback_path.unlink(missing_ok=True)
                _fsync_config_directory(config_dir)
                return True
            except Exception:
                restored = False
                if replaced:
                    try:
                        if previous is None:
                            _CONFIG_PATH.unlink(missing_ok=True)
                            _fsync_config_directory(config_dir)
                            restored = True
                        else:
                            restore_temp = _write_config_temp(config_dir, previous)
                            try:
                                _replace_config(restore_temp, _CONFIG_PATH)
                                _fsync_config_directory(config_dir)
                                restored = True
                            finally:
                                restore_temp.unlink(missing_ok=True)
                    except Exception:
                        # 留下 rollback 文件供下次诊断或人工恢复，绝不再覆盖。
                        pass
                if restored:
                    rollback_path.unlink(missing_ok=True)
                self._restore_memory(previous)
                return False
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
                if rollback_temp is not None:
                    rollback_temp.unlink(missing_ok=True)
                if not replaced:
                    rollback_path.unlink(missing_ok=True)

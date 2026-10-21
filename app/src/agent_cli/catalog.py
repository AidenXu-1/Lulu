"""Load and fail-closed validate the bundled Phase 3 capability snapshot."""

from __future__ import annotations

from collections import Counter
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import stat


_SOURCE_SHA256 = "3c7f2becf77765a88cd306121919be335db106c2f6640fc74e5b3f959be3bdea"
_NORMATIVE_JSON_SHA256 = "c142b0b0c3d6167b4ae7b10ab97aa59b571ee7c9c2d5f53e7a88848decbcdb9a"
_MATRIX_SHA256 = "5b66d00dc50b568bf8558fa8995ce95a918bcef29c0b8b72e249d9538d5418f9"
_CATALOG_SHA256 = "fb42c639529093bff7e608a656efa9a4584a88c0e42e215f6a3a2dfb97a8758f"
_PROFILE_REGISTRIES = {
    "request_schema_profile": "request_schema_profiles",
    "result_schema_profile": "result_schema_profiles",
    "effect_profile": "effect_profiles",
    "approval_profile": "approval_profiles",
    "lock_profile": "lock_profiles",
    "job_profile": "job_profiles",
    "fixture_profile": "fixture_profiles",
    "oracle_profile": "oracle_profiles",
}
_CATALOG_RESOURCE_ERROR = "包内能力目录必须是当前用户可信的产品资源普通文件"
RETIRED_LICENSE_CAPABILITIES = frozenset(f"AUTH-{number:02d}" for number in range(1, 7))


def _assert_no_symlink_ancestry(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise RuntimeError(_CATALOG_RESOURCE_ERROR)


def _explicit_catalog_resource(resource_root: Path) -> Path:
    root = Path(resource_root)
    if not root.is_absolute():
        raise RuntimeError("包内能力目录资源根必须是绝对路径")
    contents = root.parent
    bundle = contents.parent
    if (
        root.name != "Resources"
        or contents.name != "Contents"
        or bundle.suffix != ".app"
    ):
        raise RuntimeError(_CATALOG_RESOURCE_ERROR)
    resource = root / "src" / "agent_cli" / "capability_catalog.json"
    try:
        _assert_no_symlink_ancestry(resource)
        for directory in (
            bundle,
            contents,
            root,
            root / "src",
            root / "src" / "agent_cli",
        ):
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022
            ):
                raise RuntimeError(_CATALOG_RESOURCE_ERROR)
        metadata = resource.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise RuntimeError(_CATALOG_RESOURCE_ERROR)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("包内能力目录不可用") from exc
    return resource


def load_capability_catalog(
    *,
    resource_root: Path | None = None,
) -> dict[str, object]:
    if resource_root is None:
        resource = files(__package__).joinpath("capability_catalog.json")
    else:
        resource = _explicit_catalog_resource(resource_root)
    try:
        raw_catalog = resource.read_bytes()
        if hashlib.sha256(raw_catalog).hexdigest() != _CATALOG_SHA256:
            raise RuntimeError("包内能力目录字节身份无效")
        snapshot = json.loads(raw_catalog.decode("utf-8"))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("包内能力目录不可用") from exc
    if type(snapshot) is not dict:
        raise RuntimeError("包内能力目录格式无效")
    if snapshot.get("snapshot_kind") != "lulu_agent_cli_runtime_catalog":
        raise RuntimeError("包内能力目录类型无效")
    if snapshot.get("snapshot_version") != "1.0.0":
        raise RuntimeError("包内能力目录版本无效")
    if snapshot.get("source_catalog") != {
        "path": "docs/spec/lulu-agent-cli-contract-catalog.md",
        "sha256": _SOURCE_SHA256,
        "normative_json_sha256": _NORMATIVE_JSON_SHA256,
    }:
        raise RuntimeError("包内能力目录来源绑定无效")
    catalog = snapshot.get("catalog")
    if type(catalog) is not dict:
        raise RuntimeError("包内能力目录正文无效")
    if catalog.get("catalog_kind") != "lulu_agent_cli_capability_contract":
        raise RuntimeError("包内能力目录合同类型无效")
    if catalog.get("catalog_version") != "1.0.0":
        raise RuntimeError("包内能力目录合同版本无效")
    if catalog.get("source_matrix") != {
        "path": "docs/spec/lulu-agent-cli-capability-matrix.md",
        "sha256": _MATRIX_SHA256,
        "expected_capability_count": 143,
    }:
        raise RuntimeError("包内能力目录矩阵绑定无效")
    capabilities = catalog.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) != 143:
        raise RuntimeError("包内能力目录数量无效")
    ids = [item.get("capability_id") for item in capabilities if type(item) is dict]
    if len(ids) != 143 or len(set(ids)) != 143 or any(type(item) is not str for item in ids):
        raise RuntimeError("包内能力目录 ID 无效")
    states = Counter(item.get("current_cli_state") for item in capabilities)
    if states != Counter({"unavailable": 135, "gui_only": 8}):
        raise RuntimeError("包内能力目录状态无效")
    for capability in capabilities:
        for field_name, registry_name in _PROFILE_REGISTRIES.items():
            reference = capability.get(field_name)
            registry = catalog.get(registry_name)
            if not isinstance(registry, dict) or reference not in registry:
                raise RuntimeError("包内能力目录 profile 引用无效")
    return snapshot



def active_capabilities(catalog: dict) -> list[dict]:
    """Project the immutable historical contract onto the current product."""
    result = []
    for capability in catalog["capabilities"]:
        if capability["capability_id"] in {"OUT-01", "OUT-02", "OUT-03", "OUT-04"}:
            continue
        item = dict(capability)
        if item["capability_id"] == "LIB-15":
            item["argv_template"] = "lulu content save --kind {rewrite|voice_result}"
            item["capability_name"] = "保存改写和配音结果"
        result.append(item)
    return result


def capability_discovery(
    *,
    resource_root: Path | None = None,
    activated_capability_ids: frozenset[str] = frozenset(),
) -> dict[str, object]:
    snapshot = load_capability_catalog(resource_root=resource_root)
    source = snapshot["source_catalog"]
    catalog = snapshot["catalog"]
    capabilities = active_capabilities(catalog)
    projected = []
    for capability in capabilities:
        capability_id = capability["capability_id"]
        retired = capability_id in RETIRED_LICENSE_CAPABILITIES
        state = (
            "unavailable" if retired else
            "available"
            if capability_id in activated_capability_ids
            else capability["current_cli_state"]
        )
        projected.append(
            {
                "capability_id": capability_id,
                "command_id": capability["command_id"],
                "argv_template": capability["argv_template"],
                "state": state,
                "reason_code": (
                    "LICENSE_FEATURE_RETIRED" if retired else
                    "FORMAL_CLI_NOT_IMPLEMENTED"
                    if state == "unavailable"
                    else None
                ),
                "executable": state == "available",
                "display_target_state": "unavailable" if retired else capability["target_state"],
            }
        )
        if state == "available" and capability_id in {"LIB-01", "LIB-02", "COPY-07"}:
            projected[-1]["argv_template"] = {
                "LIB-01": "lulu library list [--kind <kind>] [--search <text>] [--folder-id <id>] [--sort <sort>] [--offset <n>] [--limit <n>] --output json",
                "LIB-02": "lulu library show --entry-id <id> --output json",
                "COPY-07": "lulu copy templates {list|show} [--direction <direction>] [--template-id <id>] --output json",
            }[capability_id]
        if state == 'available' and capability_id in {'TTS-01', 'TTS-02'}:
            projected[-1]['argv_template'] = (
                'lulu tts status [--engine <engine>] --output json' if capability_id == 'TTS-01' else
                'lulu tts synthesize --engine <engine> --text-file <path> [--voice-id <id>] [--options-file <path>] --destination <new.wav> --output json')
            projected[-1]['engines'] = ['local_indextts2', 'local_indextts2_0', 'local_voxcpm2', 'local_qwen3_tts']
        # The immutable historical matrix predates the closed JSON adapters.
        # Discovery must describe executable syntax, including required flags.
        current_templates = {
            'SYS-01': 'lulu version --output json',
            'SYS-02': 'lulu capabilities --output json',
            'SYS-03': 'lulu status --output json',
            'SYS-04': 'lulu doctor --output json',
            'TR-01': 'lulu transcribe prepare <file> [--engine <engine>] --output json',
            'TR-02': 'lulu transcribe run <file> [--engine <engine>] --output json',
            'TR-03': 'lulu transcribe run <file> --engine {local_sensevoice_q8|local_whisper_mlx|local_whisper_mlx_4bit|local_moss_q5} --output json',
            'TR-05': 'lulu transcribe {prepare|run} <url> --output json',
            'TR-06': 'lulu transcribe {prepare|run} <url> --prefer-subtitles --output json',
            'TR-07': 'lulu transcribe {prepare|run} <url> --save-media --media-directory <directory> --output json',
            'TR-08': 'lulu transcribe {prepare|run} <url> --save-cover --output json',
            'TR-11': 'lulu jobs show --job-id <job-id> --output json',
            'TR-12': 'lulu jobs cancel --job-id <job-id> --output json',
            'JOB-01': 'lulu jobs list --output json',
            'JOB-02': 'lulu jobs wait --job-id <job-id> --after-sequence <n> --output jsonl',
            'JOB-03': 'lulu jobs cancel --job-id <job-id> --output json',
            'JOB-06': 'lulu jobs receipt --job-id <job-id> --output json',
        }
        if state == 'available' and capability_id in current_templates:
            projected[-1]['argv_template'] = current_templates[capability_id]
        if state == 'available' and capability_id in {'TR-01', 'TR-02', 'TR-03'}:
            from ..core.engines.local_models import LOCAL_MODELS
            projected[-1]['engines'] = list(LOCAL_MODELS)
            projected[-1]['status_command'] = 'lulu transcribe status [--engine <engine>] --output json'
        if state == 'available' and capability_id == 'JOB-06':
            projected[-1]['result_command'] = 'lulu jobs result --job-id <job-id> --offset <byte-offset> --output json'
    from ..core.agent_workspace.contracts import discovery as workspace_discovery
    projected.extend(workspace_discovery(activated_capability_ids))
    from ..core.link_assets import CAPABILITIES as EXTRACTION_CAPABILITIES
    for capability_id, kind in EXTRACTION_CAPABILITIES.items():
        available = capability_id in activated_capability_ids
        projected.append(dict(capability_id=capability_id, command_id='extract.' + kind,
            argv_template='lulu extract {prepare|run} <url> --outputs <video,audio,cover,data> --destination <directory> [--plan-digest <digest> --request-id <id>] --output json',
            state='available' if available else 'unavailable', executable=available,
            reason_code=None if available else 'LINK_EXTRACTION_RUNTIME_UNAVAILABLE',
            display_target_state='available', output_kind=kind, models_required=False,
            result_command='lulu jobs receipt --job-id <job-id> --output json'))
    return {
        "catalog_version": catalog["catalog_version"],
        "source_catalog_sha256": source["sha256"],
        "source_matrix_sha256": catalog["source_matrix"]["sha256"],
        "capability_count": len(projected),
        "state_counts": dict(Counter(item["state"] for item in projected)),
        "capabilities": projected,
    }

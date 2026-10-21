"""Fail-closed compatibility contract for the bundled proposal-only Skill."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping
from urllib.parse import urlsplit


_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_MANIFEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "name",
        "skill_version",
        "product_identity",
        "cli_protocol",
        "catalog_version",
        "cli_schema",
        "product_manifest",
        "trust_anchor",
        "installation",
        "proposal",
    }
)
_PRODUCT_IDENTITY_FIELDS = frozenset(
    {
        "product_version",
        "product_build",
        "channel",
        "release_state",
        "evidence_status",
    }
)
_CLI_PROTOCOL_FIELDS = frozenset(
    {
        "range",
        "minimum",
        "maximum_exclusive",
        "prerelease_policy",
        "exact_prerelease_pin",
    }
)
_CLI_SCHEMA_FIELDS = frozenset({"schema_id", "schema_revision"})
_PRODUCT_MANIFEST_FIELDS = frozenset({"schema_id", "schema_revision"})
_TRUST_ANCHOR_FIELDS = frozenset(
    {
        "state",
        "canonical_cli_relative_path",
        "allowed_bundle_ids",
        "signed_manifest_sha256",
        "designated_requirement",
        "team_id",
    }
)
_INSTALLATION_FIELDS = frozenset({"mode", "automatic"})
_PROPOSAL_FIELDS = frozenset({"version", "trust_level", "dispatchable"})
_CLI_CLAIM_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "cli_protocol_version",
        "catalog_version",
        "product_version",
        "product_build",
        "installation_identity_scope",
        "signed_manifest_sha256",
    }
)
_INSTALLATION_REQUIRED = frozenset(
    {
        "schema_id",
        "verified",
        "product_version",
        "product_build",
        "installation_identity_scope",
        "signed_manifest_sha256",
    }
)
_INSTALLATION_OPTIONAL = frozenset(
    {
        "canonical_app",
        "canonical_cli",
        "convenience_path",
        "convenience_path_equivalent",
        "bundle_id",
        "designated_requirement",
        "team_id",
        "source_commit",
        "source_tree",
        "cli_protocol_version",
    }
)
_WORKFLOW_FIELDS = frozenset(
    {
        "schema_id",
        "schema_revision",
        "code",
        "capability_id_or_null",
        "job_id_or_null",
        "idempotency_key_or_null",
        "successful_item_refs",
        "failed_item_refs",
    }
)
_WORKFLOW_ACTIONS = {
    "CAPABILITY_UNAVAILABLE": "stop_unavailable",
    "GUI_ONLY": "trusted_gui_handoff",
    "AUTH_REQUIRED": "trusted_gui_activation",
    "HUMAN_ACTION_REQUIRED": "trusted_human_handoff",
    "RESOURCE_BUSY": "wait_and_reread_status",
    "CONFLICT": "require_explicit_resolution",
    "CANCELLED": "stop_cancelled_attempt",
    "RECOVERY_REQUIRED": "inspect_authoritative_recovery",
    "PARTIAL": "preserve_success_and_repropose_failures",
}


class SkillContractError(ValueError):
    """A Skill/CLI value is outside the frozen Phase 9 contract."""

    def __init__(self, code: str, field: str) -> None:
        super().__init__(f"{code}:{field}")
        self.code = code
        self.field = field


@dataclass(frozen=True)
class _Version:
    core: tuple[int, int, int]
    prerelease: tuple[str, ...] | None
    original: str


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SkillContractError("INVALID_ARGUMENT", field)
    return value


def _closed(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    field: str,
) -> None:
    keys = set(value)
    unknown = keys - required - optional
    missing = required - keys
    if unknown:
        raise SkillContractError("UNKNOWN_FIELD", f"{field}.{sorted(unknown)[0]}")
    if missing:
        raise SkillContractError("MISSING_FIELD", f"{field}.{sorted(missing)[0]}")


def _version(value: object, field: str) -> _Version:
    if not isinstance(value, str):
        raise SkillContractError("SEMVER_INVALID", field)
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise SkillContractError("SEMVER_INVALID", field)
    prerelease_text = match.group(4)
    prerelease = tuple(prerelease_text.split(".")) if prerelease_text else None
    if prerelease is not None:
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise SkillContractError("SEMVER_INVALID", field)
    return _Version(
        core=(int(match.group(1)), int(match.group(2)), int(match.group(3))),
        prerelease=prerelease,
        original=value,
    )


def _compare(left: _Version, right: _Version) -> int:
    if left.core != right.core:
        return -1 if left.core < right.core else 1
    if left.prerelease is None or right.prerelease is None:
        if left.prerelease is right.prerelease:
            return 0
        return 1 if left.prerelease is None else -1
    for left_item, right_item in zip(left.prerelease, right.prerelease):
        if left_item == right_item:
            continue
        left_numeric = left_item.isdigit()
        right_numeric = right_item.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_item) < int(right_item) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_item < right_item else 1
    if len(left.prerelease) == len(right.prerelease):
        return 0
    return -1 if len(left.prerelease) < len(right.prerelease) else 1


def _fixed(value: object, expected: object, code: str, field: str) -> None:
    if value != expected:
        raise SkillContractError(code, field)


def _validate_manifest(manifest: Mapping[str, object]) -> Mapping[str, object]:
    _closed(manifest, required=_MANIFEST_FIELDS, field="skill_manifest")
    _fixed(manifest["schema_id"], "lulu.agent-skill-manifest/v1", "SCHEMA_INCOMPATIBLE", "schema_id")
    _fixed(manifest["schema_revision"], 1, "SCHEMA_INCOMPATIBLE", "schema_revision")
    _fixed(manifest["name"], "lulu-agent", "SKILL_INCOMPATIBLE", "name")
    _fixed(manifest["skill_version"], "1.0.0", "SKILL_INCOMPATIBLE", "skill_version")

    product_identity = _object(manifest["product_identity"], "product_identity")
    _closed(
        product_identity,
        required=_PRODUCT_IDENTITY_FIELDS,
        field="product_identity",
    )
    for field, expected in {
        "product_version": "0.3.2",
        "product_build": "20260823.1",
        "channel": "candidate",
        "release_state": "non_release",
        "evidence_status": "source_unverified",
    }.items():
        _fixed(
            product_identity[field],
            expected,
            "SKILL_INCOMPATIBLE",
            f"product_identity.{field}",
        )

    protocol = _object(manifest["cli_protocol"], "cli_protocol")
    _closed(protocol, required=_CLI_PROTOCOL_FIELDS, field="cli_protocol")
    _fixed(protocol["range"], ">=1.0.0,<2.0.0", "PROTOCOL_RANGE_INVALID", "cli_protocol.range")
    _fixed(protocol["minimum"], "1.0.0", "PROTOCOL_RANGE_INVALID", "cli_protocol.minimum")
    _fixed(protocol["maximum_exclusive"], "2.0.0", "PROTOCOL_RANGE_INVALID", "cli_protocol.maximum_exclusive")
    _fixed(
        protocol["prerelease_policy"],
        "reject_unless_exactly_pinned",
        "PROTOCOL_RANGE_INVALID",
        "cli_protocol.prerelease_policy",
    )
    pin = protocol["exact_prerelease_pin"]
    if pin is not None and _version(pin, "cli_protocol.exact_prerelease_pin").prerelease is None:
        raise SkillContractError("SEMVER_INVALID", "cli_protocol.exact_prerelease_pin")

    cli_schema = _object(manifest["cli_schema"], "cli_schema")
    _closed(cli_schema, required=_CLI_SCHEMA_FIELDS, field="cli_schema")
    _fixed(cli_schema["schema_id"], "lulu.agent/v1", "SCHEMA_INCOMPATIBLE", "cli_schema.schema_id")
    _fixed(cli_schema["schema_revision"], 1, "SCHEMA_INCOMPATIBLE", "cli_schema.schema_revision")

    product = _object(manifest["product_manifest"], "product_manifest")
    _closed(product, required=_PRODUCT_MANIFEST_FIELDS, field="product_manifest")
    _fixed(product["schema_id"], "lulu.agent-product-manifest/v1", "SCHEMA_INCOMPATIBLE", "product_manifest.schema_id")
    _fixed(product["schema_revision"], 1, "SCHEMA_INCOMPATIBLE", "product_manifest.schema_revision")

    trust = _object(manifest["trust_anchor"], "trust_anchor")
    _closed(trust, required=_TRUST_ANCHOR_FIELDS, field="trust_anchor")
    if trust["state"] not in {"unverified", "pinned"}:
        raise SkillContractError("UNKNOWN_ENUM", "trust_anchor.state")
    bundle_ids = trust["allowed_bundle_ids"]
    if not isinstance(bundle_ids, list) or not bundle_ids or not all(isinstance(item, str) and item for item in bundle_ids):
        raise SkillContractError("INVALID_ARGUMENT", "trust_anchor.allowed_bundle_ids")

    installation = _object(manifest["installation"], "installation")
    _closed(installation, required=_INSTALLATION_FIELDS, field="installation")
    _fixed(installation["mode"], "bundled_source_only", "UNKNOWN_ENUM", "installation.mode")
    _fixed(installation["automatic"], False, "SKILL_INCOMPATIBLE", "installation.automatic")

    proposal = _object(manifest["proposal"], "proposal")
    _closed(proposal, required=_PROPOSAL_FIELDS, field="proposal")
    _fixed(proposal["version"], "lulu.intent-proposal/v1", "SCHEMA_INCOMPATIBLE", "proposal.version")
    _fixed(proposal["trust_level"], "untrusted_data_plane", "UNKNOWN_ENUM", "proposal.trust_level")
    _fixed(proposal["dispatchable"], False, "SKILL_INCOMPATIBLE", "proposal.dispatchable")
    _fixed(manifest["catalog_version"], "1.0.0", "CATALOG_INCOMPATIBLE", "catalog_version")
    return protocol


def validate_skill_compatibility(
    *,
    skill_manifest: Mapping[str, object],
    cli_claim: Mapping[str, object],
    verified_installation: Mapping[str, object],
) -> dict[str, object]:
    """Validate compatibility and independently verified installation identity."""

    manifest = _object(skill_manifest, "skill_manifest")
    protocol = _validate_manifest(manifest)
    claim = _object(cli_claim, "cli_claim")
    _closed(claim, required=_CLI_CLAIM_FIELDS, field="cli_claim")
    installation = _object(verified_installation, "verified_installation")
    _closed(
        installation,
        required=_INSTALLATION_REQUIRED,
        optional=_INSTALLATION_OPTIONAL,
        field="verified_installation",
    )
    _fixed(installation["schema_id"], "lulu.install-verification/v1", "SCHEMA_INCOMPATIBLE", "verified_installation.schema_id")
    _fixed(installation["verified"], True, "TRUST_ANCHOR_INVALID", "verified_installation.verified")
    _fixed(claim["schema_id"], "lulu.agent/v1", "SCHEMA_INCOMPATIBLE", "cli_claim.schema_id")
    _fixed(claim["schema_revision"], 1, "SCHEMA_INCOMPATIBLE", "cli_claim.schema_revision")
    _fixed(claim["catalog_version"], manifest["catalog_version"], "CATALOG_INCOMPATIBLE", "cli_claim.catalog_version")

    cli_version = _version(claim["cli_protocol_version"], "cli_claim.cli_protocol_version")
    minimum = _version(protocol["minimum"], "cli_protocol.minimum")
    maximum = _version(protocol["maximum_exclusive"], "cli_protocol.maximum_exclusive")
    if _compare(cli_version, minimum) < 0 or _compare(cli_version, maximum) >= 0:
        raise SkillContractError("PROTOCOL_INCOMPATIBLE", "cli_claim.cli_protocol_version")
    if cli_version.prerelease is not None and protocol["exact_prerelease_pin"] != cli_version.original:
        raise SkillContractError("PRERELEASE_NOT_PINNED", "cli_claim.cli_protocol_version")

    identity_fields = (
        "product_version",
        "product_build",
        "installation_identity_scope",
        "signed_manifest_sha256",
    )
    if not _SHA256.fullmatch(str(claim["signed_manifest_sha256"])):
        raise SkillContractError("INSTALL_IDENTITY_MISMATCH", "cli_claim.signed_manifest_sha256")
    for field in identity_fields:
        if claim[field] != installation[field]:
            raise SkillContractError("INSTALL_IDENTITY_MISMATCH", field)

    return {
        "schema_id": "lulu.skill-compatibility/v1",
        "compatible": True,
        "skill_version": manifest["skill_version"],
        "cli_protocol_version": claim["cli_protocol_version"],
        "catalog_version": claim["catalog_version"],
        "cli_schema_id": claim["schema_id"],
        "cli_schema_revision": claim["schema_revision"],
        "installation_identity_scope": claim["installation_identity_scope"],
    }


def decide_skill_next_step(signal: Mapping[str, object]) -> dict[str, object]:
    """Project a safe next step without granting authority or replay permission."""

    value = _object(signal, "workflow_signal")
    _closed(value, required=_WORKFLOW_FIELDS, field="workflow_signal")
    _fixed(value["schema_id"], "lulu.skill-workflow-signal/v1", "SCHEMA_INCOMPATIBLE", "workflow_signal.schema_id")
    _fixed(value["schema_revision"], 1, "SCHEMA_INCOMPATIBLE", "workflow_signal.schema_revision")
    code = value["code"]
    if not isinstance(code, str) or code not in _WORKFLOW_ACTIONS:
        raise SkillContractError("UNKNOWN_ENUM", "workflow_signal.code")
    for field in ("capability_id_or_null", "job_id_or_null", "idempotency_key_or_null"):
        if value[field] is not None and (type(value[field]) is not str or not value[field]):
            raise SkillContractError("INVALID_ARGUMENT", f"workflow_signal.{field}")
    successful = value["successful_item_refs"]
    failed = value["failed_item_refs"]
    if (
        not isinstance(successful, list)
        or not isinstance(failed, list)
        or not all(type(item) is str and item for item in successful + failed)
    ):
        raise SkillContractError("INVALID_ARGUMENT", "workflow_signal.item_refs")
    if code != "PARTIAL" and (successful or failed):
        successful_projection: list[str] = []
        failed_projection: list[str] = []
    else:
        successful_projection = list(successful)
        failed_projection = list(failed)
    return {
        "schema_id": "lulu.skill-next-step/v1",
        "schema_revision": 1,
        "signal_code": code,
        "action": _WORKFLOW_ACTIONS[code],
        "business_dispatch_allowed": False,
        "replay_allowed": False,
        "idempotency_key_or_null": value["idempotency_key_or_null"],
        "preserved_item_refs": successful_projection,
        "reproposal_item_refs": failed_projection,
    }


_LINK_CONFIGURATION_FIELDS = frozenset(
    {
        "platform",
        "url",
        "engine",
        "prefer_subtitles",
        "subtitle_fallback",
        "media_mode",
        "save_media",
        "media_directory",
        "save_cover",
        "format",
        "output_destination",
    }
)
_LINK_MUTABLE_FIELDS = _LINK_CONFIGURATION_FIELDS - {"platform", "url"}
_LINK_ACCEPT_REPLIES = frozenset({"ok", "按默认", "继续"})


def _link_platform(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SkillContractError("INVALID_ARGUMENT", "url") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SkillContractError("INVALID_ARGUMENT", "url")
    host = parsed.hostname.lower()
    if host.endswith("douyin.com"):
        return "抖音"
    if host.endswith("xiaohongshu.com") or host == "xhslink.com":
        return "小红书"
    if host.endswith("bilibili.com") or host == "b23.tv":
        return "B站"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "YouTube"
    return "未知平台"


def _link_missing(configuration: Mapping[str, object]) -> list[str]:
    missing: list[str] = []
    if configuration.get("save_media") is True and not configuration.get(
        "media_directory"
    ):
        missing.append("media_directory")
    output = configuration.get("output_destination")
    if type(output) is not str or not output:
        missing.append("output_destination")
    return missing


def _link_projection(
    configuration: Mapping[str, object],
    *,
    stage: str,
    changed_fields: list[str] | None = None,
) -> dict[str, object]:
    config = dict(configuration)
    missing = _link_missing(config)
    display_items = [
        f"平台/链接：{config['platform']} · {config['url']}",
        f"引擎：{config['engine']}",
        "字幕优先与无字幕回退："
        + (
            f"优先字幕，回退={config['subtitle_fallback']}"
            if config["prefer_subtitles"]
            else f"不优先字幕，回退={config['subtitle_fallback']}"
        ),
        f"下载音频或视频：{config['media_mode']}",
        "是否保存媒体：" + ("是" if config["save_media"] else "否"),
        "是否保存封面：" + ("是" if config["save_cover"] else "否"),
        f"逐字稿格式：{config['format']}",
        f"保存位置：{config['output_destination']}",
    ]
    return {
        "schema_id": "lulu.skill-link-configuration/v1",
        "schema_revision": 1,
        "stage": stage,
        "configuration": config,
        "display_heading": "当前设置/本次默认",
        "display_items": display_items,
        "changed_fields": list(changed_fields or []),
        "missing_questions": missing,
        "prepare_allowed": stage == "configuration_accepted" and not missing,
        "run_allowed": False,
        "approved": False,
        "dispatchable": False,
        "authority_mutations": [],
    }


def _validated_link_state(value: object) -> Mapping[str, object]:
    state = _object(value, "state")
    if (
        state.get("schema_id") != "lulu.skill-link-configuration/v1"
        or state.get("schema_revision") != 1
        or state.get("approved") is not False
        or state.get("dispatchable") is not False
        or state.get("authority_mutations") != []
    ):
        raise SkillContractError("INVALID_ARGUMENT", "state")
    configuration = _object(state.get("configuration"), "state.configuration")
    if frozenset(configuration) != _LINK_CONFIGURATION_FIELDS:
        raise SkillContractError("INVALID_ARGUMENT", "state.configuration")
    return configuration


def decide_link_configuration(event: Mapping[str, object]) -> dict[str, object]:
    """Project URL clarification only; it never prepares, runs, or grants authority."""
    value = _object(event, "link_configuration_event")
    kind = value.get("event")
    if kind == "start" and frozenset(value) == {"event", "url"}:
        url = value.get("url")
        if type(url) is not str:
            raise SkillContractError("INVALID_ARGUMENT", "url")
        configuration = {
            "platform": _link_platform(url),
            "url": url,
            "engine": "local_sensevoice_q8",
            "prefer_subtitles": False,
            "subtitle_fallback": "local_transcription",
            "media_mode": "audio",
            "save_media": False,
            "media_directory": None,
            "save_cover": False,
            "format": "txt",
            "output_destination": "shared_lulu_transcript_default",
        }
        return _link_projection(configuration, stage="review_configuration")
    if kind == "reply" and frozenset(value) == {"event", "state", "text"}:
        configuration = _validated_link_state(value.get("state"))
        text = value.get("text")
        accepted = type(text) is str and text.strip().lower() in _LINK_ACCEPT_REPLIES
        if accepted and not _link_missing(configuration):
            return _link_projection(
                configuration, stage="configuration_accepted"
            )
        return _link_projection(configuration, stage="needs_input")
    if kind == "modify" and frozenset(value) == {"event", "state", "changes"}:
        configuration = dict(_validated_link_state(value.get("state")))
        changes = _object(value.get("changes"), "changes")
        if not changes or not set(changes).issubset(_LINK_MUTABLE_FIELDS):
            raise SkillContractError("UNKNOWN_FIELD", "changes")
        candidate = {**configuration, **dict(changes)}
        from ..core.engines.local_models import LOCAL_MODELS
        if candidate["engine"] not in LOCAL_MODELS:
            raise SkillContractError("UNKNOWN_ENUM", "changes.engine")
        if candidate["subtitle_fallback"] not in {"local_transcription", "fail"}:
            raise SkillContractError("UNKNOWN_ENUM", "changes.subtitle_fallback")
        if candidate["media_mode"] not in {"audio", "video"}:
            raise SkillContractError("UNKNOWN_ENUM", "changes.media_mode")
        if candidate["format"] not in {"txt", "md", "srt"}:
            raise SkillContractError("UNKNOWN_ENUM", "changes.format")
        for field in ("prefer_subtitles", "save_media", "save_cover"):
            if type(candidate[field]) is not bool:
                raise SkillContractError("INVALID_ARGUMENT", f"changes.{field}")
        directory = candidate["media_directory"]
        if directory is not None and (type(directory) is not str or not directory.startswith("/")):
            raise SkillContractError("INVALID_ARGUMENT", "changes.media_directory")
        output = candidate["output_destination"]
        if type(output) is not str or not output:
            raise SkillContractError("INVALID_ARGUMENT", "changes.output_destination")
        return _link_projection(
            candidate,
            stage="review_configuration",
            changed_fields=sorted(changes),
        )
    raise SkillContractError("INVALID_ARGUMENT", "link_configuration_event")

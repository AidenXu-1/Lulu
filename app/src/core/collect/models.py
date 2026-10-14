"""采集条目数据模型:兼容旧抖音字段的跨平台作品合同。

纯数据 + 纯函数,无网络、无第三方依赖,可独立测试。
合同见 ADR-0025;旧抖音字段来源见 docs/spec/douyin-author-bulk-collection.md §2。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
import re
from urllib.parse import urlsplit

# 抖音 aweme_type=68 是图文帖(无视频/无音频),时长为 0,只保留元数据并标注。
AWEME_TYPE_IMAGE = 68

# 东八区(抖音 create_time 为 unix 秒,按北京时间展示发布日期)
_CST = timezone(timedelta(hours=8))

_PLATFORMS = frozenset({"douyin", "xiaohongshu", "bilibili"})
_CONTENT_KINDS = frozenset({"video", "image", "mixed", "unknown"})
_LOCAL_STATUSES = frozenset({
    "not_requested", "url_only", "pending", "downloaded", "unavailable",
    "failed", "skipped_privacy",
})
_TRANSCRIPT_STATUSES = frozenset({
    "not_started", "queued", "running", "completed", "failed", "unavailable",
})
_FEISHU_STATUSES = frozenset({
    "not_configured", "pending", "uploading", "uploaded", "retryable_failed",
    "final_failed", "skipped",
})
_RISK_STATUSES = frozenset({
    "ok", "rate_limited", "login_required", "login_expired", "captcha",
    "permission_denied", "platform_changed", "cancelled",
})

TRUTHFUL_NUMERIC_FIELDS = (
    "create_time", "duration_ms", "like", "collect", "comment", "share",
)
FIELD_STATE_PREFIX = "_field_state_"
_FIELD_STATES = frozenset({"known", "unknown", "not_applicable"})
_MAX_PUBLIC_TIMESTAMP = 4_102_444_800  # 2100-01-01T00:00:00Z
_DOUYIN_WORK_ID_RE = re.compile(r"^[0-9]{1,64}$")


def _douyin_cover_url(video: object) -> str:
    """从同一作品的视频对象中提取可交给安全下载层的 HTTPS 封面地址。"""
    if not isinstance(video, dict):
        return ""
    # 平台的 cover/origin_cover 可能是首帧；作者自选封面单独放在此字段。
    for key in ("cover_original_scale", "cover", "origin_cover", "dynamic_cover"):
        value = video.get(key)
        if not isinstance(value, dict):
            continue
        urls = value.get("url_list")
        if not isinstance(urls, (list, tuple)):
            continue
        for candidate in urls:
            if not isinstance(candidate, str):
                continue
            url = candidate.strip()
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if (
                parsed.scheme.casefold() == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
            ):
                return url
    return ""


@dataclass
class CollectedItem:
    """一条采集到的作品。

    前 11 个字段保持旧抖音构造顺序,避免现有 UI/导出/测试被 v2 合同打断。
    新平台必须显式提供 ``platform`` 和 ``work_id``。
    """

    aweme_id: str
    author_name: str = ""
    title: str = ""               # desc
    create_time: int = 0          # unix 秒
    duration_ms: int = 0          # video.duration;图文帖为 0
    like: int = 0                 # digg_count
    collect: int = 0              # collect_count
    comment: int = 0              # comment_count
    share: int = 0                # share_count
    play_url: str = ""            # video.play_addr.url_list[0],按需下载用
    aweme_type: int = 0
    schema_version: int = 2
    platform: str = "douyin"      # douyin|xiaohongshu|bilibili
    work_id: str = ""              # 跨平台身份真相
    author_id: str = ""
    homepage_url: str = ""
    work_url: str = ""
    extra_stats: dict[str, int | str] = field(default_factory=dict)
    content_kind: str = "video"   # video|image|mixed|unknown
    cover_url: str = ""
    cover_local_path: str = ""
    cover_status: str = "not_requested"
    media_local_path: str = ""
    media_status: str = "not_requested"
    transcript_status: str = "not_started"
    feishu_video_status: str = "not_configured"
    feishu_cover_status: str = "not_configured"
    source_method: str = "anonymous_public"
    risk_status: str = "ok"
    last_error_code: str = ""

    def __post_init__(self) -> None:
        self.platform = str(self.platform or "douyin").lower()
        self.aweme_id = str(self.aweme_id or "")
        self.work_id = str(self.work_id or "")
        if self.platform not in _PLATFORMS:
            raise ValueError(f"未知采集平台: {self.platform}")
        if self.platform == "douyin" and not self.work_id:
            self.work_id = self.aweme_id
        if (
            self.platform == "douyin"
            and not self.work_url
            and _DOUYIN_WORK_ID_RE.fullmatch(self.work_id)
        ):
            self.work_url = f"https://www.douyin.com/video/{self.work_id}"
        if self.platform != "douyin" and self.aweme_id:
            raise ValueError("非抖音记录不得写入 aweme_id")
        if self.schema_version != 2:
            raise ValueError("CollectedItem 只接受 schema_version=2")
        if self.content_kind not in _CONTENT_KINDS:
            raise ValueError(f"未知内容类型: {self.content_kind}")
        if self.cover_status not in _LOCAL_STATUSES or self.media_status not in _LOCAL_STATUSES:
            raise ValueError("未知本地资源状态")
        if self.transcript_status not in _TRANSCRIPT_STATUSES:
            raise ValueError(f"未知转录状态: {self.transcript_status}")
        if (self.feishu_video_status not in _FEISHU_STATUSES
                or self.feishu_cover_status not in _FEISHU_STATUSES):
            raise ValueError("未知飞书附件状态")
        if self.risk_status not in _RISK_STATUSES:
            raise ValueError(f"未知风险状态: {self.risk_status}")
        for name in TRUTHFUL_NUMERIC_FIELDS:
            key = f"{FIELD_STATE_PREFIX}{name}"
            if key not in self.extra_stats:
                continue
            state = self.extra_stats[key]
            if state not in _FIELD_STATES:
                raise ValueError(f"未知字段状态: {key}={state}")
            if state == "not_applicable" and not (
                name == "duration_ms" and self.is_image
            ):
                raise ValueError("not_applicable 只适用于已确认图文作品的时长")
            if state == "known":
                value = getattr(self, name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"已知字段值非法: {name}")
                if name == "create_time" and value > _MAX_PUBLIC_TIMESTAMP:
                    raise ValueError("已知发布时间超出允许范围")

    @property
    def stable_key(self) -> str:
        """跨平台稳定键;缺少作品 ID 时返回空,由调用方决定失败或保留。"""
        if not self.platform or not self.work_id:
            return ""
        return f"{self.platform}:{self.work_id}"

    @property
    def is_image(self) -> bool:
        """图文帖(无音频,不可转录)。"""
        return self.aweme_type == AWEME_TYPE_IMAGE or self.content_kind == "image"

    def has_field_state(self, name: str) -> bool:
        """记录是否显式携带六类字段之一的真实性状态。"""
        if name not in TRUTHFUL_NUMERIC_FIELDS:
            raise ValueError(f"不支持的字段状态查询: {name}")
        return f"{FIELD_STATE_PREFIX}{name}" in self.extra_stats

    def field_state(self, name: str) -> str:
        """返回字段真实性状态，并集中解释旧 schema v2 记录。

        旧小红书记录没有状态标记时一律按 unknown 解释；已确认图文作品的
        duration_ms 为 not_applicable。旧抖音无标记记录沿用原数值语义。
        """
        if name not in TRUTHFUL_NUMERIC_FIELDS:
            raise ValueError(f"不支持的字段状态查询: {name}")
        key = f"{FIELD_STATE_PREFIX}{name}"
        if key in self.extra_stats:
            return str(self.extra_stats[key])
        if self.platform == "xiaohongshu":
            if name == "duration_ms" and self.is_image:
                return "not_applicable"
            return "unknown"
        return "known"

    def known_numeric(self, name: str) -> int | None:
        """仅在字段真实已知时返回数值；unknown/不适用返回 None。"""
        return getattr(self, name) if self.field_state(name) == "known" else None

    @property
    def publish_date(self) -> str:
        """发布日期 YYYY-MM-DD(北京时间);无有效时间返回空串。"""
        if not self.create_time:
            return ""
        return datetime.fromtimestamp(self.create_time, _CST).strftime("%Y-%m-%d")

    @property
    def duration_text(self) -> str:
        """时长 mm:ss;图文/无时长返回「图文」或空。"""
        if self.is_image:
            return "图文"
        if self.duration_ms <= 0:
            return ""
        total = round(self.duration_ms / 1000)
        return f"{total // 60}:{total % 60:02d}"

    def to_dict(self) -> dict:
        return asdict(self)


def from_aweme(aweme: dict) -> CollectedItem:
    """从抖音 aweme 对象解析出 CollectedItem。多字段容错,缺失给默认值。"""
    aweme = aweme or {}
    video = aweme.get("video") or {}
    stats = aweme.get("statistics") or {}
    author = aweme.get("author") or {}
    url_list = ((video.get("play_addr") or {}).get("url_list")) or []
    cover_url = _douyin_cover_url(video)

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    aweme_id = str(aweme.get("aweme_id") or "")
    work_url = (
        f"https://www.douyin.com/video/{aweme_id}"
        if _DOUYIN_WORK_ID_RE.fullmatch(aweme_id)
        else ""
    )

    return CollectedItem(
        aweme_id=aweme_id,
        author_name=str(author.get("nickname") or ""),
        title=str(aweme.get("desc") or ""),
        create_time=_int(aweme.get("create_time")),
        duration_ms=_int(video.get("duration")),
        like=_int(stats.get("digg_count")),
        collect=_int(stats.get("collect_count")),
        comment=_int(stats.get("comment_count")),
        share=_int(stats.get("share_count")),
        play_url=str(url_list[0]) if url_list else "",
        aweme_type=_int(aweme.get("aweme_type")),
        platform="douyin",
        work_id=aweme_id,
        author_id=str(author.get("sec_uid") or author.get("uid") or ""),
        work_url=work_url,
        content_kind=("image" if _int(aweme.get("aweme_type")) == AWEME_TYPE_IMAGE
                      else "video"),
        cover_url=cover_url,
        cover_status="url_only" if cover_url else "not_requested",
    )


def dedupe(items: list[CollectedItem]) -> list[CollectedItem]:
    """按 ``platform:work_id`` 去重并保序;空 ID 不静默合并或丢弃。"""
    seen: set[str] = set()
    out: list[CollectedItem] = []
    for it in items:
        key = it.stable_key
        if not key:
            out.append(it)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

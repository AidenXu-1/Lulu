"""转录结果的通用数据结构。

设计成足够通用,能容纳不同引擎的输出:纯文本、带时间戳、带标点。
不同引擎(SenseVoice / Paraformer / Whisper / 在线 API)都产出同一种 Transcript。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit, urlunsplit


_COVER_STATUSES = frozenset(
    {"not_requested", "url_only", "downloaded", "failed"}
)
_COVER_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_COVER_EXTENSIONS = frozenset({".jpg", ".png", ".webp"})
COVER_CDN_SUFFIXES = {
    "douyin": ("douyinpic.com",),
    "xiaohongshu": ("xhscdn.com",),
    "bilibili": ("hdslb.com",),
    "youtube": ("ytimg.com",),
}


def _https_cover_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return raw


def normalize_official_cover_url(platform: str, value: object) -> str:
    """仅把已确认平台的官方 CDN 缩略图从 HTTP 升级为 HTTPS。"""

    raw = str(value or "").strip()
    if not raw:
        return ""
    existing = _https_cover_url(raw)
    if existing:
        return existing
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return ""
    suffixes = COVER_CDN_SUFFIXES.get(str(platform or "").casefold(), ())
    if (
        parsed.scheme.casefold() != "http"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80)
        or not any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)
    ):
        return ""
    return _https_cover_url(
        urlunsplit(("https", host, parsed.path, parsed.query, parsed.fragment))
    )


def _managed_cover_path(platform: str, value: object) -> str:
    raw = str(value or "").strip()
    if not platform or not raw or "\\" in raw:
        return ""
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != platform
        or path.parts[1] in {"", ".", ".."}
        or path.suffix.casefold() not in _COVER_EXTENSIONS
    ):
        return ""
    return raw


@dataclass(frozen=True)
class TranscriptCover:
    """单链接转录旁路的封面事实，不改变 Transcript 主结果。"""

    platform: str = ""
    work_id: str = ""
    url: str = ""
    status: str = "not_requested"
    local_path: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        platform = str(self.platform or "").strip().casefold()
        if platform and not _COVER_PLATFORM_RE.fullmatch(platform):
            platform = ""
        work_id = str(self.work_id or "").strip()
        url = _https_cover_url(self.url)
        status = (
            str(self.status or "")
            if str(self.status or "") in _COVER_STATUSES
            else ("url_only" if url else "not_requested")
        )
        local_path = _managed_cover_path(platform, self.local_path)
        error_code = str(self.error_code or "").strip()

        if status == "downloaded" and (
            not platform or not work_id or not url or not local_path
        ):
            status = "failed"
            local_path = ""
            error_code = "cover_path_blocked"
        elif status != "downloaded":
            local_path = ""
        if not url and status in {"url_only", "downloaded"}:
            status = "not_requested"
            local_path = ""
        if status != "failed":
            error_code = ""

        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "local_path", local_path)
        object.__setattr__(self, "error_code", error_code)

    @property
    def eligible(self) -> bool:
        return bool(self.platform and self.work_id and self.url)

    @classmethod
    def from_meta(cls, meta: object) -> TranscriptCover:
        data = meta if isinstance(meta, dict) else {}
        explicit_url = _https_cover_url(data.get("cover_url"))
        compatible_thumbnail = _https_cover_url(data.get("thumbnail"))
        url = explicit_url or compatible_thumbnail
        default_status = "url_only" if url else "not_requested"
        return cls(
            platform=data.get("cover_platform", ""),
            work_id=data.get("cover_work_id", ""),
            url=url,
            status=data.get("cover_status", default_status),
            local_path=data.get("cover_local_path", ""),
            error_code=data.get("cover_error_code", ""),
        )

    def merge_meta(self, meta: object) -> dict:
        merged = dict(meta) if isinstance(meta, dict) else {}
        has_cover_context = bool(
            self.platform
            or self.work_id
            or self.url
            or self.local_path
            or self.error_code
            or self.status != "not_requested"
            or "thumbnail" in merged
            or any(
                key in merged
                for key in (
                    "cover_platform",
                    "cover_work_id",
                    "cover_url",
                    "cover_local_path",
                    "cover_status",
                    "cover_error_code",
                )
            )
        )
        if not has_cover_context:
            return merged
        merged.update(
            {
                "cover_platform": self.platform,
                "cover_work_id": self.work_id,
                "cover_url": self.url,
                "cover_local_path": self.local_path,
                "cover_status": self.status,
            }
        )
        if self.error_code:
            merged["cover_error_code"] = self.error_code
        else:
            merged.pop("cover_error_code", None)
        return merged


@dataclass
class Segment:
    """一段话。时间戳可能为空(并非所有引擎都给)。"""

    text: str
    start: float | None = None  # 起始秒
    end: float | None = None    # 结束秒


@dataclass
class Transcript:
    """一次转录的完整结果。"""

    text: str                                    # 完整逐字稿(已拼接)
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None                  # 识别到的语种(若有)
    engine: str | None = None                    # 产出它的引擎名
    source: str | None = None                    # 原始输入(文件路径或链接)
    title: str | None = None                     # 链接来源的真实标题(如视频/单集名)
    meta: dict = field(default_factory=dict)     # 链接来源的中性元数据(作者/日期/时长/封面/标签…,不含互动数据)
    saved_media: str | None = None               # 若勾选保留,下载的音视频另存到的路径

    @property
    def has_timestamps(self) -> bool:
        return not (self.meta or {}).get("segments_stale", False) and any(s.start is not None for s in self.segments)

    @property
    def cover(self) -> TranscriptCover:
        return TranscriptCover.from_meta(self.meta)

    def set_cover(self, cover: TranscriptCover) -> None:
        if not isinstance(cover, TranscriptCover):
            raise TypeError("cover 必须是 TranscriptCover")
        self.meta = cover.merge_meta(self.meta)

"""飞书作品库存储的纯逻辑合同。

本模块只负责把不同入口的作品事实归一为同一种记录，并生成飞书多维
表格字段。它不访问网络、不读取凭据，也不负责上传附件。单条转录、主页
采集和筛选后的作品由上层共用这一个合同，避免各入口保存的信息逐渐分叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BT_TEXT = 1
BT_NUMBER = 2
BT_DATETIME = 5
BT_URL = 15
BT_ATTACHMENT = 17

STANDARD_LIBRARY_TEMPLATE_VERSION = "1"
# 只为兼容现有主页 13 字段保留；CDN 地址可能过期，长期身份始终以
# 「原作品链接」和「Lulu作品键」为准。
LEGACY_TRANSIENT_MEDIA_FIELD = "音视频直链"

_PLATFORM_RE = re.compile(r"[^a-z0-9_-]+")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "feature",
        "share_from",
        "share_source",
        "share_token",
        "si",
        "spm",
        "spm_id_from",
        "xsec_source",
        "xsec_token",
    }
)
_PLATFORM_HOST_SUFFIXES = (
    ("douyin", ("douyin.com", "iesdouyin.com")),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com")),
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("youtube", ("youtube.com", "youtu.be")),
)
_SOURCE_LABELS = {
    "single_transcription": "单条转录",
    "homepage_collection": "主页采集",
    "collection_selection": "采集筛选",
}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def extract_transcript_body(value: object) -> str:
    """从 Lulu 成稿 Markdown 中只取「逐字稿」小节。

    普通纯文本原样保留；若存在 YAML、作品信息、简介等成稿结构，
    仅返回「逐字稿」标题下的内容。时间戳行不作任何改写。
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    structured = bool(re.search(r"(?m)^#{1,6}[ \t]+\S", text))
    if text.startswith("---\n"):
        structured = True
        closing = re.search(r"(?m)^(?:---|\.\.\.)[ \t]*$", text[4:])
        if closing is not None:
            text = text[4 + closing.end():].lstrip("\n")

    transcript_heading = re.search(
        r"(?m)^(#{1,6})[ \t]+逐字稿[ \t]*#*[ \t]*$",
        text,
    )
    if transcript_heading is None:
        return "" if structured else text.strip()

    level = len(transcript_heading.group(1))
    section_start = transcript_heading.end()
    section_end = len(text)
    for heading in re.finditer(r"(?m)^(#{1,6})[ \t]+.+$", text[section_start:]):
        if len(heading.group(1)) <= level:
            section_end = section_start + heading.start()
            break
    return text[section_start:section_end].strip()


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _normalize_platform(value: object) -> str:
    platform = _clean_text(value).casefold().replace(" ", "-")
    platform = _PLATFORM_RE.sub("-", platform).strip("-_")
    return platform[:64]


def _safe_http_url(value: object) -> str:
    """返回可存储的 HTTP(S) URL；拒绝用户信息和异常端口。"""

    raw = _clean_text(value)
    if not raw or len(raw) > 8192:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        rendered_host = f"{rendered_host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, rendered_host, path, parsed.query, parsed.fragment))


def _normalize_percent_escape(match: re.Match[str]) -> str:
    escaped = match.group(0).upper()
    decoded = chr(int(escaped[1:], 16))
    return decoded if decoded in _UNRESERVED else escaped


def normalize_work_url(value: object) -> str:
    """生成只用于作品身份比较的规范 URL。

    展示和打开仍使用记录里的原链接；规范化会移除 fragment 和常见分享追踪
    参数，并排序剩余参数，使同一作品的常见分享链接可以稳定去重。
    """

    safe = _safe_http_url(value)
    if not safe:
        return ""
    parsed = urlsplit(safe)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    known_platform_host = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for _platform, suffixes in _PLATFORM_HOST_SUFFIXES
        for suffix in suffixes
    )
    path = _PERCENT_ESCAPE_RE.sub(_normalize_percent_escape, parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") or "/"
    query_items = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or (
            known_platform_host and normalized_key in _TRACKING_QUERY_KEYS
        ):
            continue
        query_items.append((key, query_value))
    query_items.sort(key=lambda pair: (pair[0].casefold(), pair[0], pair[1]))
    query = urlencode(query_items, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def make_work_key(platform: object, work_id: object, work_url: object = "") -> str:
    """优先使用平台作品 ID，缺失时以规范 URL 的摘要生成稳定键。"""

    normalized_platform = _normalize_platform(platform)
    normalized_id = _clean_text(work_id)
    if normalized_platform and normalized_id and len(normalized_id) <= 512:
        return f"{normalized_platform}:{normalized_id}"
    normalized_url = normalize_work_url(work_url)
    if not normalized_url:
        return ""
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"url:{digest}"


@dataclass(frozen=True)
class WorkArchiveRecord:
    """平台无关的一篇作品归档记录；未知事实统一使用 ``None`` 或空值。"""

    title: str = ""
    platform: str = ""
    content_kind: str = "unknown"
    work_id: str = ""
    work_url: str = ""
    media_url: str = ""
    author_name: str = ""
    author_id: str = ""
    author_homepage: str = ""
    author_fans: int | str | None = None
    author_total_likes: int | None = None
    author_works: int | None = None
    author_verify: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    published_at_ms: int | None = None
    duration_seconds: float | None = None
    like_count: int | None = None
    collect_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    view_count: int | None = None
    coin_count: int | None = None
    danmaku_count: int | None = None
    transcript_text: str = ""
    original_transcript_text: str = ""
    summary: str = ""
    cover_url: str = ""
    cover_local_path: str = ""
    media_local_path: str = ""
    language: str = ""
    engine: str = ""
    source_entry: str = ""
    captured_at_ms: int | None = None
    archived_at_ms: int | None = None
    source_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "title",
            "content_kind",
            "work_id",
            "author_name",
            "author_id",
            "author_verify",
            "description",
            "transcript_text",
            "original_transcript_text",
            "summary",
            "cover_local_path",
            "media_local_path",
            "language",
            "engine",
            "source_entry",
            "source_id",
        ):
            object.__setattr__(self, name, _clean_text(getattr(self, name)))
        object.__setattr__(self, "platform", _normalize_platform(self.platform))
        for name in ("work_url", "media_url", "author_homepage", "cover_url"):
            object.__setattr__(self, name, _safe_http_url(getattr(self, name)))
        clean_tags: list[str] = []
        seen_tags: set[str] = set()
        for raw_tag in self.tags or ():
            tag = _clean_text(raw_tag)
            if not tag or tag in seen_tags:
                continue
            seen_tags.add(tag)
            clean_tags.append(tag)
        object.__setattr__(self, "tags", tuple(clean_tags))
        if isinstance(self.author_fans, bool):
            author_fans: int | str | None = None
        elif isinstance(self.author_fans, int):
            author_fans = self.author_fans if self.author_fans >= 0 else None
        else:
            author_fans = _clean_text(self.author_fans) or None
        object.__setattr__(self, "author_fans", author_fans)
        for name in (
            "author_total_likes",
            "author_works",
            "published_at_ms",
            "like_count",
            "collect_count",
            "comment_count",
            "share_count",
            "view_count",
            "coin_count",
            "danmaku_count",
            "captured_at_ms",
            "archived_at_ms",
        ):
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name)))
        object.__setattr__(
            self,
            "duration_seconds",
            _nonnegative_float(self.duration_seconds),
        )

    @property
    def normalized_work_url(self) -> str:
        return normalize_work_url(self.work_url)

    @property
    def work_key(self) -> str:
        return make_work_key(self.platform, self.work_id, self.work_url)


@dataclass(frozen=True)
class BitableFieldSpec:
    """不可变的标准字段定义，可按现有 ``BITABLE_FIELDS`` 形态导出。"""

    key: str
    name: str
    field_type: int
    property_items: tuple[tuple[str, object], ...] | None = ()

    def as_bitable_field(self) -> tuple:
        if self.property_items is None:
            return self.name, self.field_type, None
        if self.property_items:
            return self.name, self.field_type, dict(self.property_items)
        return self.name, self.field_type


@dataclass(frozen=True)
class WorkLibraryTemplate:
    version: str
    display_name: str
    fields: tuple[BitableFieldSpec, ...]

    def bitable_fields(self) -> tuple[tuple, ...]:
        return tuple(field.as_bitable_field() for field in self.fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


_INTEGER_PROPERTY = (("formatter", "0"),)


STANDARD_LIBRARY_TEMPLATE_V1 = WorkLibraryTemplate(
    version=STANDARD_LIBRARY_TEMPLATE_VERSION,
    display_name="Lulu 标准作品库",
    fields=(
        # 与主页采集导出对齐：13 项用户可见信息，不创建内部调试列。
        BitableFieldSpec("title", "标题", BT_TEXT),
        BitableFieldSpec("author_name", "作者昵称", BT_TEXT),
        BitableFieldSpec("published_at_ms", "发布日期", BT_DATETIME),
        BitableFieldSpec("duration_seconds", "时长", BT_TEXT),
        BitableFieldSpec("like_count", "点赞", BT_NUMBER, _INTEGER_PROPERTY),
        BitableFieldSpec("collect_count", "收藏", BT_NUMBER, _INTEGER_PROPERTY),
        BitableFieldSpec("comment_count", "评论", BT_NUMBER, _INTEGER_PROPERTY),
        BitableFieldSpec("share_count", "分享", BT_NUMBER, _INTEGER_PROPERTY),
        BitableFieldSpec("work_url", "原作品链接", BT_URL),
        BitableFieldSpec("media_url", "音视频直链", BT_URL),
        BitableFieldSpec("transcript_text", "转录文稿", BT_TEXT),
        BitableFieldSpec("cover", "封面", BT_ATTACHMENT, None),
        BitableFieldSpec("media_attachment", "音视频附件", BT_ATTACHMENT, None),
    ),
)


def standard_library_fields_v1() -> tuple[tuple, ...]:
    """以当前飞书建表函数可直接消费的结构返回一份新字段定义。"""

    return STANDARD_LIBRARY_TEMPLATE_V1.bitable_fields()


# 与现有飞书建表函数可以直接拼接；旧主页 13 字段表由显式兼容分支处理。
STANDARD_BITABLE_FIELDS = standard_library_fields_v1()
STANDARD_LIBRARY_FIELDS_V1 = STANDARD_BITABLE_FIELDS


def _known_item_number(item: object, name: str) -> int | None:
    resolver = getattr(item, "known_numeric", None)
    if callable(resolver):
        try:
            return _nonnegative_int(resolver(name))
        except (TypeError, ValueError):
            return None
    return _nonnegative_int(getattr(item, name, None))


def _mapped_transcript_text(item: object, transcripts: Mapping | None) -> str:
    values = transcripts if isinstance(transcripts, Mapping) else {}
    stable_key = _clean_text(getattr(item, "stable_key", ""))
    legacy_key = _clean_text(getattr(item, "aweme_id", ""))
    for key in (stable_key, legacy_key):
        if not key or key not in values:
            continue
        value = values[key]
        text = _clean_text(getattr(value, "text", value))
        if text:
            return text
    return ""


def record_from_collected_item(
    item: object,
    transcripts: Mapping | None = None,
    *,
    source_entry: str = "homepage_collection",
    captured_at_ms: int | None = None,
) -> WorkArchiveRecord:
    """把现有 ``CollectedItem`` 适配成统一作品记录，保留未知值语义。"""

    extra = getattr(item, "extra_stats", None)
    extra_stats = extra if isinstance(extra, Mapping) else {}
    create_time = _known_item_number(item, "create_time")
    duration_ms = _known_item_number(item, "duration_ms")
    tags_value = extra_stats.get("tags", ())
    tags = (
        tuple(tags_value)
        if isinstance(tags_value, (list, tuple, set, frozenset))
        else ()
    )
    return WorkArchiveRecord(
        title=getattr(item, "title", ""),
        platform=getattr(item, "platform", ""),
        content_kind=getattr(item, "content_kind", "unknown"),
        work_id=getattr(item, "work_id", ""),
        work_url=getattr(item, "work_url", ""),
        media_url=getattr(item, "play_url", ""),
        author_name=getattr(item, "author_name", ""),
        author_id=getattr(item, "author_id", ""),
        author_homepage=getattr(item, "homepage_url", ""),
        author_fans=extra_stats.get("author_fans"),
        author_total_likes=_nonnegative_int(extra_stats.get("author_total_likes")),
        author_works=_nonnegative_int(extra_stats.get("author_works")),
        author_verify=extra_stats.get("author_verify", ""),
        tags=tags,
        published_at_ms=(create_time * 1000 if create_time is not None else None),
        duration_seconds=(duration_ms / 1000 if duration_ms is not None else None),
        like_count=_known_item_number(item, "like"),
        collect_count=_known_item_number(item, "collect"),
        comment_count=_known_item_number(item, "comment"),
        share_count=_known_item_number(item, "share"),
        view_count=_nonnegative_int(extra_stats.get("view")),
        coin_count=_nonnegative_int(extra_stats.get("coin")),
        danmaku_count=_nonnegative_int(extra_stats.get("danmaku")),
        transcript_text=_mapped_transcript_text(item, transcripts),
        cover_url=getattr(item, "cover_url", ""),
        cover_local_path=getattr(item, "cover_local_path", ""),
        media_local_path=getattr(item, "media_local_path", ""),
        source_entry=source_entry,
        captured_at_ms=captured_at_ms,
        archived_at_ms=captured_at_ms,
    )


def transcript_meta_from_collected_item(item: object) -> dict:
    """把主页采集事实转换为文稿库可直接展示的中性元数据。

    只写采集主档中明确已知的字段，避免用 0 冒充未知互动数据。该结构与
    单链接解析器的 ``Transcript.meta`` 对齐，可在批量转录完成后安全合并。
    """

    record = record_from_collected_item(item)
    author = {
        key: value
        for key, value in {
            "name": record.author_name,
            "handle": record.author_id,
            "homepage_url": record.author_homepage,
            "fans": record.author_fans,
            "likes_total": record.author_total_likes,
            "works": record.author_works,
            "verify": record.author_verify,
        }.items()
        if value not in {None, ""}
    }
    stats = {
        key: value
        for key, value in {
            "like": record.like_count,
            "favorite": record.collect_count,
            "comment": record.comment_count,
            "share": record.share_count,
            "view": record.view_count,
            "coin": record.coin_count,
            "danmaku": record.danmaku_count,
        }.items()
        if value is not None
    }
    meta = {
        "title": record.title,
        "platform": record.platform,
        "content_kind": record.content_kind,
        "work_id": record.work_id,
        "webpage_url": record.work_url,
        "original_url": record.work_url,
        "media_url": record.media_url,
        "timestamp": (
            record.published_at_ms / 1000
            if record.published_at_ms is not None
            else None
        ),
        "duration": record.duration_seconds,
        "author": author,
        "uploader": record.author_name,
        "stats": stats,
        "cover_url": record.cover_url,
        "cover_platform": record.platform,
        "cover_work_id": record.work_id,
        "cover_local_path": record.cover_local_path,
        "cover_status": str(getattr(item, "cover_status", "not_requested") or "not_requested"),
        "tags": list(record.tags),
    }
    return {
        key: value
        for key, value in meta.items()
        if value is not None and value != "" and value != () and value != []
    }


def _infer_platform(url: str) -> str:
    safe = _safe_http_url(url)
    hostname = (urlsplit(safe).hostname or "").casefold() if safe else ""
    for platform, suffixes in _PLATFORM_HOST_SUFFIXES:
        if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes):
            return platform
    return ""


def _upload_date_ms(value: object) -> int | None:
    raw = _clean_text(value)
    if not (len(raw) == 8 and raw.isdigit()):
        return None
    try:
        date = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(date.timestamp() * 1000)


def transcript_work_url(meta: Mapping, source: object = "") -> str:
    """读取不同采集器的作品链接别名，返回可安全保存的原作品 URL。"""

    for key in ("webpage_url", "original_url", "work_url", "source_url"):
        url = _safe_http_url(meta.get(key))
        if url:
            return url
    return _safe_http_url(source)


def transcript_media_url(meta: Mapping) -> str:
    """读取媒体直链别名；直链只作抓取时快照，不作为长期作品身份。"""

    for key in ("media_url", "play_url", "video_url", "audio_url"):
        url = _safe_http_url(meta.get(key))
        if url:
            return url
    return ""


def transcript_published_at_ms(meta: Mapping) -> int | None:
    """兼容 YYYYMMDD、unix 秒/毫秒及常见发布时间别名。"""

    for key in ("upload_date", "published_date"):
        value = _upload_date_ms(meta.get(key))
        if value is not None:
            return value
    for key in ("timestamp", "create_time", "publish_time", "published_at"):
        value = _nonnegative_float(meta.get(key))
        if value is None:
            continue
        seconds = value / 1000 if value > 10_000_000_000 else value
        return int(seconds * 1000)
    return None


def transcript_duration_seconds(meta: Mapping) -> float | None:
    """统一秒、毫秒两类时长合同，未知值保持为空。"""

    for key in ("duration", "duration_seconds"):
        value = _nonnegative_float(meta.get(key))
        if value is not None:
            return value
    value = _nonnegative_float(meta.get("duration_ms"))
    return value / 1000 if value is not None else None


def _source_title(source: str) -> str:
    if not source or _safe_http_url(source):
        return ""
    leaf = source.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    return leaf.strip()


def record_from_transcript(
    transcript: object,
    *,
    title: str | None = None,
    text_override: str | None = None,
    summary: str | None = None,
    source_entry: str = "single_transcription",
    captured_at_ms: int | None = None,
) -> WorkArchiveRecord:
    """把现有 ``Transcript`` 适配成统一作品记录。

    ``text_override`` 用于保存编辑器当前正文；若它和原始转录不同，模板同时
    保留原始转录，避免用户整理后的结果覆盖来源事实。
    """

    raw_meta = getattr(transcript, "meta", None)
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    raw_author = meta.get("author")
    author = raw_author if isinstance(raw_author, Mapping) else {}
    raw_stats = meta.get("stats")
    stats = raw_stats if isinstance(raw_stats, Mapping) else {}
    source = _clean_text(getattr(transcript, "source", ""))
    work_url = transcript_work_url(meta, source)
    cover = getattr(transcript, "cover", None)
    cover_platform = _clean_text(getattr(cover, "platform", ""))
    cover_work_id = _clean_text(getattr(cover, "work_id", ""))
    platform = cover_platform or _clean_text(meta.get("platform")) or _infer_platform(work_url)
    work_id = cover_work_id or _clean_text(meta.get("work_id"))
    from .collect.export import format_transcript_for_export

    original_text = extract_transcript_body(
        format_transcript_for_export(transcript)
    )
    if text_override is None:
        final_text = original_text
    else:
        override_text = extract_transcript_body(text_override)
        plain_source = extract_transcript_body(getattr(transcript, "text", ""))
        # 右栏未编辑时会把同一份纯文本作为 override 传入。此时
        # 仍使用 segments 生成的时间戳版；用户真正改过则以编辑结果为准。
        final_text = original_text if override_text == plain_source else override_text
    tags_value = meta.get("tags", ())
    if isinstance(tags_value, str):
        tags = (tags_value,)
    elif isinstance(tags_value, (list, tuple, set, frozenset)):
        tags = tuple(tags_value)
    else:
        tags = ()
    published_at_ms = transcript_published_at_ms(meta)
    display_title = (
        _clean_text(title)
        or _clean_text(getattr(transcript, "title", ""))
        or _clean_text(meta.get("title"))
        or _source_title(source)
    )
    author_name = _clean_text(author.get("name")) or _clean_text(meta.get("uploader"))
    author_homepage = (
        _safe_http_url(author.get("homepage_url"))
        or _safe_http_url(author.get("url"))
        or _safe_http_url(meta.get("uploader_url"))
        or _safe_http_url(meta.get("channel_url"))
    )
    cover_url = _clean_text(getattr(cover, "url", "")) or _clean_text(meta.get("thumbnail"))
    original_for_archive = original_text if final_text != original_text else ""
    return WorkArchiveRecord(
        source_id=("library:" + str(meta["lulu_entry_id"]) if meta.get("lulu_entry_id") else "transcript:" + hashlib.sha256((source + "\0" + original_text).encode()).hexdigest()),
        title=display_title,
        platform=platform,
        content_kind=meta.get("content_kind", "unknown"),
        work_id=work_id,
        work_url=work_url,
        media_url=transcript_media_url(meta),
        author_name=author_name,
        author_id=author.get("handle", ""),
        author_homepage=author_homepage,
        author_fans=author.get("fans"),
        author_total_likes=_nonnegative_int(author.get("likes_total")),
        author_works=_nonnegative_int(author.get("works")),
        author_verify=author.get("verify", ""),
        description=meta.get("description", ""),
        tags=tags,
        published_at_ms=published_at_ms,
        duration_seconds=transcript_duration_seconds(meta),
        like_count=_nonnegative_int(stats.get("like")),
        collect_count=_nonnegative_int(stats.get("favorite", stats.get("collect"))),
        comment_count=_nonnegative_int(stats.get("comment")),
        share_count=_nonnegative_int(stats.get("share")),
        view_count=_nonnegative_int(stats.get("view")),
        coin_count=_nonnegative_int(stats.get("coin")),
        danmaku_count=_nonnegative_int(stats.get("danmaku")),
        transcript_text=final_text,
        original_transcript_text=original_for_archive,
        summary=summary or "",
        cover_url=cover_url,
        cover_local_path=getattr(cover, "local_path", ""),
        media_local_path=getattr(transcript, "saved_media", ""),
        language=getattr(transcript, "language", "") or meta.get("language", ""),
        engine=getattr(transcript, "engine", ""),
        source_entry=source_entry,
        captured_at_ms=captured_at_ms,
        archived_at_ms=captured_at_ms,
    )


def _duration_text(record: WorkArchiveRecord) -> str:
    if record.content_kind == "image":
        return "图文"
    if record.duration_seconds is None:
        return ""
    total = int(round(record.duration_seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if total == 0:
        return "0"
    return f"{minutes}:{seconds:02d}"


def record_to_bitable_fields(
    record: WorkArchiveRecord,
    *,
    allowed_fields: Iterable[str] | None = None,
    write_id: str = "",
) -> dict:
    """生成飞书 records API 的 ``fields``；空值不会伪造或占位。"""

    allowed = set(allowed_fields) if allowed_fields is not None else None
    fields: dict = {}

    def accepts(name: str) -> bool:
        return allowed is None or name in allowed

    def put_text(name: str, value: object) -> None:
        text = _clean_text(value)
        if text and accepts(name):
            fields[name] = text

    def put_number(name: str, value: object) -> None:
        number = _nonnegative_int(value)
        if number is not None and accepts(name):
            fields[name] = number

    def put_link(name: str, value: object) -> None:
        url = _safe_http_url(value)
        if url and accepts(name):
            fields[name] = {"link": url, "text": url}

    put_text("标题", record.title)
    put_text("平台", record.platform)
    put_text("作品类型", record.content_kind if record.content_kind != "unknown" else "")
    put_text("作品ID", record.work_id)
    put_text("作者昵称", record.author_name)
    put_text("作者平台号", record.author_id)
    put_link("作者主页", record.author_homepage)
    put_text("粉丝数", record.author_fans)
    put_number("作者总获赞", record.author_total_likes)
    put_number("作者作品数", record.author_works)
    put_text("作者认证", record.author_verify)
    if record.published_at_ms is not None and accepts("发布日期"):
        fields["发布日期"] = record.published_at_ms
    put_text("时长", _duration_text(record))
    put_number("点赞", record.like_count)
    put_number("收藏", record.collect_count)
    put_number("评论", record.comment_count)
    put_number("分享", record.share_count)
    put_number("播放", record.view_count)
    put_number("投币", record.coin_count)
    put_number("弹幕", record.danmaku_count)
    put_link("原作品链接", record.work_url)
    put_link("音视频直链", record.media_url)
    put_text("作品简介", record.description)
    put_text("标签", "、".join(record.tags))
    put_text("转录文稿", record.transcript_text)
    put_text("原始转录", record.original_transcript_text)
    put_text("摘要", record.summary)
    put_link("封面链接", record.cover_url)
    put_text("语种", record.language)
    put_text("转录引擎", record.engine)
    put_text("来源入口", _SOURCE_LABELS.get(record.source_entry, record.source_entry))
    if record.captured_at_ms is not None and accepts("采集时间"):
        fields["采集时间"] = record.captured_at_ms
    if record.archived_at_ms is not None and accepts("收录时间"):
        fields["收录时间"] = record.archived_at_ms
    put_text("Lulu作品键", record.work_key)
    put_text("Lulu写入ID", write_id)
    put_text("模板版本", STANDARD_LIBRARY_TEMPLATE_VERSION)
    return fields


def record_to_bitable_record(
    record: WorkArchiveRecord,
    *,
    allowed_fields: Iterable[str] | None = None,
    write_id: str = "",
) -> dict:
    return {
        "fields": record_to_bitable_fields(
            record,
            allowed_fields=allowed_fields,
            write_id=write_id,
        )
    }


@dataclass(frozen=True)
class DuplicateArchiveRecord:
    first_index: int
    duplicate_index: int
    work_key: str


@dataclass(frozen=True)
class BatchDedupeResult:
    records: tuple[WorkArchiveRecord, ...]
    duplicates: tuple[DuplicateArchiveRecord, ...]
    source_indexes: tuple[int, ...] = ()
    existing_duplicate_count: int = 0

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates) + self.existing_duplicate_count


def dedupe_archive_records(
    records: Iterable[WorkArchiveRecord],
    *,
    existing_work_keys: Iterable[str] = (),
    existing_work_urls: Iterable[str] = (),
) -> BatchDedupeResult:
    """按作品身份做批内及目标表去重；缺少可靠身份的记录全部保留。

    标准 13 字段表没有内部 ``Lulu作品键`` 列，因此长期已有表使用公开且
    稳定的「原作品链接」查重。旧扩展表仍可通过 ``existing_work_keys`` 兼容。
    """

    unique: list[WorkArchiveRecord] = []
    source_indexes: list[int] = []
    duplicates: list[DuplicateArchiveRecord] = []
    blocked = {
        _clean_text(value)
        for value in existing_work_keys
        if _clean_text(value)
    }
    blocked_urls = {
        normalized
        for value in existing_work_urls
        if (normalized := normalize_work_url(value))
    }
    existing_duplicate_count = 0
    first_indexes: dict[str, int] = {}
    for source_index, record in enumerate(records):
        if not isinstance(record, WorkArchiveRecord):
            raise TypeError("批内去重只接受 WorkArchiveRecord")
        key = record.work_key
        normalized_url = record.normalized_work_url
        if normalized_url and normalized_url in blocked_urls:
            existing_duplicate_count += 1
            continue
        if not key:
            unique.append(record)
            source_indexes.append(source_index)
            continue
        if key in blocked:
            existing_duplicate_count += 1
            continue
        if key in first_indexes:
            duplicates.append(
                DuplicateArchiveRecord(
                    first_index=first_indexes[key],
                    duplicate_index=source_index,
                    work_key=key,
                )
            )
            continue
        first_indexes[key] = source_index
        unique.append(record)
        source_indexes.append(source_index)
    return BatchDedupeResult(
        tuple(unique),
        tuple(duplicates),
        tuple(source_indexes),
        existing_duplicate_count,
    )


def bitable_url_with_table(url: object, table_id: object) -> str:
    """把新建结果补成可再次精确定位同一数据表的完整链接。"""

    safe = _safe_http_url(url)
    current_table = _clean_text(table_id)
    if not safe or not current_table:
        return safe
    parsed = urlsplit(safe)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key in {"table", "table_id"} and value for key, value in query):
        query.append(("table", current_table))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def make_library_target(
    *,
    name: object,
    url: object,
    app_token: object = "",
    table_id: object = "",
    status: str = "ready",
    source_kind: str = "existing",
) -> dict:
    """生成 AppConfig 可持久化的非敏感作品库目标。"""

    clean_url = bitable_url_with_table(url, table_id)
    clean_app = _clean_text(app_token)
    clean_table = _clean_text(table_id)
    identity = (
        "table\0" + "\0".join((clean_app, clean_table))
        if clean_app and clean_table
        else "url\0" + clean_url
    )
    target_id = "feishu-table-" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:20]
    return {
        "target_id": target_id,
        "name": _clean_text(name) or "飞书作品库",
        "url": clean_url,
        "app_token": clean_app,
        "table_id": clean_table,
        "template_id": "lulu.standard-work-library",
        "schema_version": int(STANDARD_LIBRARY_TEMPLATE_VERSION),
        "field_map": {},
        "status": status if status in {"pending_validation", "ready", "unavailable"} else "ready",
        "source_kind": source_kind if source_kind in {"existing", "created"} else "existing",
    }


def upsert_library_target(targets: Iterable[dict], target: dict) -> list[dict]:
    """按 target_id 或完整 URL 替换同一作品库，并保留用户的其他分类。"""

    target_id = _clean_text(target.get("target_id"))
    target_url = _clean_text(target.get("url"))
    target_app = _clean_text(target.get("app_token"))
    target_table = _clean_text(target.get("table_id"))
    out: list[dict] = []
    inserted = False
    for current in targets or ():
        if not isinstance(current, dict):
            continue
        same_cloud_table = bool(
            target_app
            and target_table
            and _clean_text(current.get("app_token")) == target_app
            and _clean_text(current.get("table_id")) == target_table
        )
        same = bool(
            (target_id and _clean_text(current.get("target_id")) == target_id)
            or same_cloud_table
            or (target_url and _clean_text(current.get("url")) == target_url)
        )
        same = same and (not _clean_text(current.get('account_id')) or _clean_text(current.get('account_id')) == _clean_text(target.get('account_id')))
        if same:
            if not inserted:
                out.append(dict(target))
                inserted = True
            continue
        out.append(dict(current))
    if not inserted:
        out.append(dict(target))
    return out


def preferred_library_target(
    targets: Iterable[dict],
    preferences: Mapping | None,
    *,
    context: str,
) -> dict | None:
    """按入口拿默认作品库；缺失时回退最近目标，再回退最后一个可用目标。"""

    available = [
        dict(target)
        for target in targets or ()
        if isinstance(target, dict)
        and _clean_text(target.get("url"))
        and target.get("status") != "unavailable"
    ]
    if not available:
        return None
    prefs = preferences if isinstance(preferences, Mapping) else {}
    key = "selection_target_id" if context == "selection" else "single_target_id"
    configured_key = f"existing_{key}"
    wanted = (
        _clean_text(prefs.get(configured_key))
        or _clean_text(prefs.get(key))
        or _clean_text(prefs.get("recent_target_id"))
    )
    if wanted:
        match = next(
            (
                target
                for target in available
                if _clean_text(target.get("target_id")) == wanted
            ),
            None,
        )
        if match is not None:
            return match
    return available[-1]

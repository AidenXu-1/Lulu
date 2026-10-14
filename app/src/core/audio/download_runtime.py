"""The complete, versioned anonymous downloader used by media and subtitles."""
from pathlib import Path
import sys
from urllib.parse import urlsplit

VERSIONS = {"yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0", "deno": "2.9.5"}


class DownloadFailure(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

    @classmethod
    def from_exception(cls, exc):
        if isinstance(exc, cls):
            return exc
        text = str(exc).lower()
        if any(word in text for word in ("403", "429", "sign in", "login", "captcha", "登录")):
            return cls("CORE_DOWNLOAD_ACCESS_DENIED",
                       "平台未允许本次匿名下载。Lulu 不会读取账号或绕过登录要求；请稍后重试或导入本地文件。")
        if any(word in text for word in ("no video", "requested format", "format is not available")):
            return cls("CORE_DOWNLOAD_FORMAT_UNAVAILABLE",
                       "未取得可下载的音视频格式，可能是平台限制或解析方式变化。")
        if "图文笔记" in text:
            return cls("CORE_DOWNLOAD_NO_AUDIO", "这条作品是图文笔记，没有可提取的音视频。")
        if "身份不一致" in text:
            return cls("CORE_DOWNLOAD_IDENTITY_MISMATCH", "平台返回的作品身份不一致，已停止下载。")
        if any(word in text for word in ("timed out", "timeout", "connection", "ssl", "network")):
            return cls("CORE_DOWNLOAD_NETWORK", "媒体连接中断或超时，请检查网络后重试。")
        return cls("CORE_DOWNLOAD_FAILED", "未能取得公开作品媒体，请重新复制分享链接或导入本地文件。")


class QuietDownloadLogger:
    # yt-dlp may include signed media URLs in its messages. Keep them out of
    # stderr/log files; callers record only the fixed error category above.
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def deno_path():
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "download_runtime" / "deno"
    else:
        try:
            from deno import find_deno_bin
            path = Path(find_deno_bin())
        except (ImportError, OSError):
            path = Path("/__lulu_missing_download_runtime__")
    if not path.is_file():
        raise DownloadFailure("CORE_DOWNLOAD_RUNTIME_MISSING", "下载组件不完整，请重新安装完整的 Lulu 安装包。")
    return path


def public_download_options(url):
    options = {
        "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
        "cachedir": False, "remote_components": set(), "logger": QuietDownloadLogger(),
    }
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com"):
        options["js_runtimes"] = {"deno": {"path": str(deno_path())}}
    return options

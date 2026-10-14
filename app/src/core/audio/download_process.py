"""Own one anonymous download and its descendants until fully reaped."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import time

from .download_runtime import DownloadFailure

_MAX_FRAME = 2 * 1024 * 1024


def _send(pipe, value):
    data = json.dumps(value, ensure_ascii=True).encode()
    if len(data) > _MAX_FRAME:
        raise DownloadFailure("CORE_DOWNLOAD_PROTOCOL", "下载结果过大，已停止提交。")
    pipe.send_bytes(data)


def _worker(pipe, run_gate, url, directory, mode, metadata, download, browser_gate=None):
    try:
        if os.name == "posix":
            os.setsid()
        os.environ["DENO_NO_UPDATE_CHECK"] = "1"
        _send(pipe, {"event": "ready"})
        # No downloader or descendant may start until the parent has recorded
        # ownership of this session. Cancellation before ready can kill only
        # this waiting process without leaving a downloader behind.
        run_gate.wait()
        if browser_gate is not None:
            from ..collect import macos_browser
            def observe_browser(event, ticket):
                if event == "browser_launch":
                    browser_gate.clear()
                _send(pipe, {"event": event, "ticket": ticket})
                if event == "browser_launch":
                    browser_gate.wait()
            macos_browser.launch_observer = observe_browser
        if download is None:
            from .extractor import _download_via_ytdlp
            download = _download_via_ytdlp
        def progress(message, fraction):
            _send(pipe, {"event": "progress", "message": message, "fraction": fraction})
        path = download(url, Path(directory), progress, metadata, link_media_mode=mode)
        _send(pipe, {"event": "result", "path": str(path), "meta": metadata})
    except Exception as exc:
        failure = DownloadFailure.from_exception(exc)
        try:
            _send(pipe, {"event": "error", "code": failure.code, "message": str(failure)})
        except (OSError, ValueError):
            pass
    finally:
        pipe.close()


def _stop(process, group_ready):
    def send(sig):
        try:
            if group_ready and os.name == "posix":
                os.killpg(process.pid, sig)
            elif process.is_alive():
                os.kill(process.pid, sig)
        except ProcessLookupError:
            pass
    # Descendants can still exist after their Python parent exits.
    send(signal.SIGTERM)
    process.join(0.6)
    send(signal.SIGKILL)
    process.join(1.0)
    if process.is_alive():
        raise DownloadFailure("CORE_DOWNLOAD_CLEANUP", "下载进程尚未退出，请稍后重试。")


def download_in_process(url, work_dir, progress, meta=None, *, link_media_mode="audio",
                        is_cancelled=None, timeout=1800, _download=None):
    """Only return an artifact after its producer has exited; bounded cancellation."""
    is_cancelled = is_cancelled or (lambda: False)
    if is_cancelled():
        raise InterruptedError("下载已取消")
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    run_gate = context.Event()
    browser_gate = context.Event()
    browser_tickets = {}
    process = context.Process(target=_worker, args=(
        child, run_gate, url, str(work_dir), link_media_mode, dict(meta or {}), _download, browser_gate,
    ))
    started = False
    group_ready = False
    result = None
    deadline = time.monotonic() + timeout
    try:
        process.start()
        started = True
        child.close()
        while True:
            if is_cancelled():
                raise InterruptedError("下载已取消")
            if time.monotonic() >= deadline:
                raise DownloadFailure("CORE_DOWNLOAD_TIMEOUT", "本次下载超过允许时长，已结束；可重试或导入本地文件。")
            if parent.poll(0.1):
                try:
                    frame = json.loads(parent.recv_bytes(_MAX_FRAME))
                except EOFError:
                    break
                event = frame.get("event")
                if event == "ready":
                    group_ready = True
                    run_gate.set()
                elif event == "browser_launch":
                    ticket = frame["ticket"]
                    browser_tickets[ticket["profile"]] = ticket
                    browser_gate.set()
                elif event == "browser_released":
                    ticket = frame["ticket"]
                    if browser_tickets.get(ticket["profile"]) == ticket:
                        browser_tickets.pop(ticket["profile"], None)
                elif event == "progress":
                    progress(frame["message"], frame["fraction"])
                elif event == "error":
                    raise DownloadFailure(frame["code"], frame["message"])
                elif event == "result":
                    result = frame
                else:
                    raise DownloadFailure("CORE_DOWNLOAD_PROTOCOL", "下载进程返回了无法识别的结果。")
            elif not process.is_alive():
                break
        process.join(1.0)
        if is_cancelled():
            raise InterruptedError("下载已取消")
        if process.exitcode != 0 or result is None:
            raise DownloadFailure("CORE_DOWNLOAD_WORKER_EXIT", "下载进程提前结束，未提交不完整文件；可直接重试。")
        path = Path(result["path"]).resolve()
        if (not path.is_relative_to(work_dir) or not path.is_file()
                or path.suffix in {".part", ".tmp", ".ytdl"} or path.stat().st_size == 0):
            raise DownloadFailure("CORE_DOWNLOAD_INCOMPLETE", "下载未产生完整媒体文件。")
    finally:
        try:
            if started:
                try:
                    _stop(process, group_ready)
                finally:
                    if browser_tickets:
                        from ..collect.macos_browser import reap_launch
                        for ticket in browser_tickets.values():
                            reap_launch(ticket)
        finally:
            parent.close()
            child.close()
            if started and not process.is_alive():
                process.close()
    # Commit caller-visible state only after cleanup has succeeded.
    if meta is not None:
        meta.update(result["meta"])
    return path

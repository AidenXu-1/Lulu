"""Owned, interruptible FFmpeg processes and verified, non-overwriting copies."""
import csv
import hashlib
import multiprocessing
import os
from pathlib import Path
import re
import signal
import subprocess
from threading import RLock
import time
import tempfile
from uuid import uuid4
from requests.certs import where as certificate_file

from .contracts import LiveError
from .index import directory

def bundled_ffmpeg():
    import imageio_ffmpeg
    path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise LiveError('随包录制组件不可用，请重新安装 Lulu')
    import sys
    if getattr(sys, 'frozen', False):
        bundle = Path(sys.executable).parent.parent
        if not path.resolve().is_relative_to(bundle.resolve()):
            raise LiveError('随包录制组件路径无效')
    return str(path)

def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def managed_file(path, parent):
    path, parent = Path(path), directory(parent)
    if path.parent != parent or path.is_symlink() or not path.is_file():
        raise LiveError('原录像不存在或位置已变化，未处理其他文件')
    return path

def _flush_process(process):
    """Called inside the guardian on parent EOF or explicit stop."""
    if process.poll() is not None:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        return
    try:
        process.stdin.write(b'q\n')
        process.stdin.flush()
        process.wait(timeout=3)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()

def _recording_guard(connection, command, duration):
    """Existing frozen multiprocessing entry, not an additional public CLI.

    The sole parent pipe closes on a Core crash. The child owns and reaps FFmpeg
    in its own process group; no detached recorder can outlive that pipe.
    """
    process = None
    try:
        os.setsid()
        connection.send({'ready': True})
        if connection.recv() != 'start':
            return
        if connection.poll():
            connection.recv()  # Detect parent EOF before launching media work.
            return
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        os.set_blocking(process.stdout.fileno(), False)
        connection.send({'pid': process.pid})
        pending = b''
        while process.poll() is None:
            # Drain FFmpeg progress without delaying the existing owner/stop pipe.
            # A shared scalar cannot fill a pipe while Core validates a segment.
            try:
                output = os.read(process.stdout.fileno(), 65536)
            except BlockingIOError:
                output = b''
            lines = (pending + output).split(b'\n')
            pending = lines.pop()[-1024:]
            for line in lines:
                if line.startswith(b'out_time_us='):
                    try:
                        duration.value = max(duration.value, int(line.split(b'=', 1)[1]) / 1000000)
                    except ValueError:
                        pass
            if connection.poll(.25):
                try:
                    connection.recv()
                except EOFError:
                    pass
                break
        _flush_process(process)
        raise SystemExit(0 if process.returncode == 0 else 1)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if process:
            _flush_process(process)
            if process.stdout:
                process.stdout.close()
        connection.close()

class _ProcessView:
    def __init__(self, process):
        self._process = process
        self.pid = process.pid

    def poll(self):
        return self._process.exitcode

    def wait(self, timeout=None):
        self._process.join(timeout)
        if self._process.is_alive():
            raise subprocess.TimeoutExpired('owned live producer', timeout)
        return self._process.exitcode

class OwnedRecordingProcess:
    def __init__(self, command):
        self.lock = RLock()
        context = multiprocessing.get_context('spawn')
        self.media_duration = context.Value('d', 0, lock=False)
        self.connection, child = context.Pipe()
        worker = context.Process(target=_recording_guard, args=(child, command, self.media_duration),
                                 name='lulu-live-guardian')
        worker.start()
        child.close()
        self.process = _ProcessView(worker)
        self.group_ready = False
        try:
            if not self.connection.poll(10) or self.connection.recv() != {'ready': True}:
                raise LiveError('录制进程启动未就绪，原文件保留')
            self.group_ready = True
            self.connection.send('start')
            if not self.connection.poll(10):
                raise LiveError('录制组件启动超时，原文件保留')
            self.child_pid = self.connection.recv()['pid']
        except Exception:
            self.stop()
            raise

    def stop(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    self.connection.send('stop')
                    self.process.wait(timeout=6)
                except (BrokenPipeError, EOFError, OSError, subprocess.TimeoutExpired):
                    def send(sig):
                        try:
                            if self.group_ready:
                                os.killpg(self.process.pid, sig)
                            else:
                                os.kill(self.process.pid, sig)
                        except ProcessLookupError:
                            pass
                    send(signal.SIGTERM)
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        send(signal.SIGKILL)
                        self.process.wait(timeout=3)
            self.connection.close()

class OwnedProcess:
    def __init__(self, command, *, capture=False):
        self.lock = RLock()
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL if capture else subprocess.PIPE,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            start_new_session=True, close_fds=True)

    def stop(self):
        with self.lock:
            process = self.process
            if process.poll() is not None:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                return
            try:
                if process.stdin is None:
                    raise BrokenPipeError()
                process.stdin.write(b'q\n')
                process.stdin.flush()
                process.wait(timeout=3)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            finally:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()

    def collect(self, timeout=45):
        try:
            return self.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            raise LiveError('录像检查超时，原文件保留') from None

def probe(path, ffmpeg, *, register=None):
    """Demux the complete file, inspect actual streams and final media timestamp."""
    process = OwnedProcess([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'info', '-xerror',
        '-i', str(path), '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
        '-progress', 'pipe:1', '-f', 'null', '-'], capture=True)
    if register:
        register(process)
    try:
        output, errors = process.collect()
        streams = re.findall(rb'Stream #0:\d+[^\n]*: (Video|Audio):', errors)
        times = re.findall(rb'out_time_us=(\d+)', output)
        duration = int(times[-1]) / 1000000 if times else 0
        if process.process.returncode or not streams or not 0 < duration < 7 * 24 * 3600:
            raise LiveError('分段媒体不完整，原文件保留，可在文件夹中检查')
        return {'duration': duration, 'bytes': Path(path).stat().st_size,
                'sha256': fingerprint(path), 'audio': b'Audio' in streams}
    finally:
        process.stop()

class SegmentProducer:
    def __init__(self, ffmpeg, stream, folder, *, start_number=0, segment_seconds=1800):
        self.folder = directory(folder)
        self.list_path = self.folder / ('segments-' + uuid4().hex + '.csv')
        self.started = time.time()
        # Take the baseline before starting FFmpeg, so its first write is fresh.
        files = [p for p in self.folder.glob('part-*.ts') if p.is_file() and not p.is_symlink()]
        self.last_bytes = sum(p.stat().st_size for p in files)
        self.file_count = len(files)
        self.has_data = False
        tls = ['-tls_verify', '1', '-ca_file', certificate_file()] if stream.url.startswith('https://') else []
        self.process = OwnedRecordingProcess([ffmpeg, '-hide_banner', '-loglevel', 'error', '-n',
            '-progress', 'pipe:1',
            '-rw_timeout', '15000000', '-protocol_whitelist', 'http,https,tcp,tls,crypto',
            *tls, '-user_agent', 'Mozilla/5.0', '-i', stream.url,
            '-map', '0:v?', '-map', '0:a?', '-c', 'copy', '-f', 'segment',
            '-segment_format', 'mpegts', '-segment_time', str(segment_seconds),
            '-segment_start_number', str(start_number), '-reset_timestamps', '1',
            '-segment_list', str(self.list_path), '-segment_list_type', 'csv',
            str(self.folder / 'part-%06d.ts')])
        self.last_growth = time.monotonic()

    @property
    def duration(self):
        return self.process.media_duration.value

    def completed(self):
        if not self.list_path.is_file() or self.list_path.is_symlink():
            return []
        result = []
        with self.list_path.open(newline='') as handle:
            for row in csv.reader(handle):
                if len(row) != 3 or not re.fullmatch(r'part-\d{6,}\.ts', Path(row[0]).name):
                    continue
                try:
                    result.append((self.folder / Path(row[0]).name, self.started + float(row[1])))
                except ValueError:
                    continue
        return result

    def growing(self):
        files = [p for p in self.folder.glob('part-*.ts') if p.is_file() and not p.is_symlink()]
        self.file_count = len(files)
        size = sum(p.stat().st_size for p in files)
        if size > self.last_bytes:
            self.last_bytes, self.last_growth = size, time.monotonic()
            self.has_data = True
        return time.monotonic() - self.last_growth < 30

    def stop(self):
        self.process.stop()

def export_media(segment, recording, ffmpeg, *, format='mp4', target=None, register=None, cancelled=lambda: False):
    source = managed_file(segment['path'], recording['directory'])
    if not segment['closed'] or not segment['valid'] or fingerprint(source) != segment['sha256']:
        raise LiveError('此分段尚未完成或已有变化，原文件保留')
    if format not in {'mp4', 'mp3'}:
        raise LiveError('请选择 MP4 视频或 MP3 音频')
    target = Path(target) if target is not None else source.with_name(source.stem + '-' + uuid4().hex[:10] + '.' + format)
    if not target.is_absolute() or target.suffix.lower() != '.' + format:
        raise LiveError('导出位置或文件格式无效，请重新选择')
    directory(target.parent)
    if target.exists() or target.is_symlink():
        raise LiveError('这个文件名已存在，请换一个名称；原文件未改动')
    temp = target.with_name('.' + target.stem + '-' + uuid4().hex + '.partial.' + format)
    try:
        if cancelled():
            raise LiveError('已取消导出，原录像保留')
        output = (['-map', '0:v?', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart']
                  if format == 'mp4' else ['-map', '0:a:0', '-vn', '-c:a', 'libmp3lame', '-q:a', '2'])
        process = OwnedProcess([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
            '-i', str(source), *output, str(temp)])
        if register:
            register(process)
        process.process.wait(timeout=120)
        if process.process.returncode:
            raise LiveError('这个分段无法导出 ' + format.upper() + '，原录像保留' +
                            ('；请确认分段包含声音' if format == 'mp3' else ''))
        value = probe(temp, ffmpeg, register=register)
        if abs(value['duration'] - segment['duration']) > max(2, segment['duration'] * .01):
            raise LiveError('导出时长不一致，原 TS 文件保留')
        if fingerprint(source) != segment['sha256']:
            raise LiveError('原分段已变化，未发布导出文件')
        if cancelled():
            raise LiveError('已取消导出，原录像保留')
        # Hard link publication is atomic and refuses an existing destination.
        os.link(temp, target, follow_symlinks=False)
        return str(target)
    except subprocess.TimeoutExpired:
        process.stop()
        raise LiveError('导出超时，原 TS 文件保留') from None
    finally:
        if 'process' in locals():
            process.stop()
        if temp.exists() and not temp.is_symlink():
            temp.unlink()


def _media_pass(command, *, register=None, cancelled=lambda: False, progress=lambda value: None):
    """Drain progress continuously: neither a long movie nor its log lives in RAM."""
    with tempfile.TemporaryFile() as errors:
        owned = OwnedProcess(command, capture=True)
        # stderr is drained separately to avoid a full pipe blocking FFmpeg.
        tail = bytearray()
        def drain_errors():
            while chunk := owned.process.stderr.read(8192):
                errors.write(chunk)
        import threading
        reader = threading.Thread(target=drain_errors, daemon=True)
        reader.start()
        try:
            if register:
                register(owned)
            os.set_blocking(owned.process.stdout.fileno(), False)
            pending, duration = b'', 0.0
            while True:
                if cancelled():
                    raise LiveError('已取消导出，原录像保留')
                try:
                    chunk = os.read(owned.process.stdout.fileno(), 65536)
                except BlockingIOError:
                    chunk = b''
                lines = (pending + chunk).split(b'\n')
                pending = lines.pop()[-1024:]
                for line in lines:
                    if line.startswith(b'out_time_us='):
                        try:
                            duration = max(duration, int(line.split(b'=', 1)[1]) / 1000000)
                            progress(duration)
                        except ValueError:
                            pass
                if owned.process.poll() is not None and not chunk:
                    break
                time.sleep(.03)
            reader.join()
            errors.seek(0)
            tail.extend(errors.read(131072))
            if owned.process.returncode:
                raise LiveError('媒体检查或合并失败，原录像保留；请检查分段是否损坏或格式不兼容')
            return duration, bytes(tail)
        finally:
            owned.stop()
            reader.join(timeout=5)
            owned.process.stdout.close()
            owned.process.stderr.close()


def _stream_signature(log):
    """Compare the input streams, excluding changing PID, bitrate and wall time."""
    header = log.split(b'Output #', 1)[0].decode('utf-8', 'replace')
    result = []
    for kind, description in re.findall(r'Stream #0:\d+[^\n]*?: (Video|Audio): ([^\n]+)', header):
        codec = re.match(r'\w+(?: \([^)]*\))?', description).group()
        if kind == 'Video':
            size = re.search(r'\b\d{2,6}x\d{2,6}\b', description)
            pixel = re.search(r',\s*((?:yuv|yuva|rgb|bgr|gray)[\w]+)', description)
            if not size or not pixel:
                raise LiveError('无法确认录像画面格式，请先使用单段导出')
            result.append((kind, codec, size.group(), pixel.group(1)))
        else:
            audio = re.search(r'(\d+ Hz),\s*([^,]+),\s*([^,]+)', description)
            if not audio:
                raise LiveError('无法确认录像声音格式，请先使用单段导出')
            result.append((kind, codec, *audio.groups()))
    if not result or not any(s[0] == 'Video' for s in result):
        raise LiveError('本场没有可确认的视频画面，请使用单段导出检查')
    return result


def export_recording(segments, recording, ffmpeg, *, target, register=None,
                     cancelled=lambda: False, progress=lambda phase, value: None):
    """Export exactly the frozen ordered manifest. Reject any missing/bad segment."""
    if recording.get('ended') is None or not segments:
        raise LiveError('请等本场录像结束并保存全部分段后再导出整场')
    target = Path(target)
    if not target.is_absolute() or target.suffix.lower() != '.mp4':
        raise LiveError('请选择完整的 MP4 导出位置')
    directory(target.parent)
    if target.exists() or target.is_symlink():
        raise LiveError('这个文件名已存在，请换一个名称；原文件未改动')
    sources, identities = [], []
    total = sum(s['duration'] for s in segments)
    def check_cancel():
        if cancelled():
            raise LiveError('已取消导出，原录像保留')
    def checked_hash(source):
        digest = hashlib.sha256()
        with source.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                check_cancel()
                digest.update(chunk)
        return digest.hexdigest()
    signature = None
    for number, segment in enumerate(segments):
        check_cancel()
        source = managed_file(segment['path'], recording['directory'])
        if (not segment['closed'] or not segment['valid'] or segment['duration'] <= 0
                or checked_hash(source) != segment['sha256']):
            raise LiveError(f'第 {number + 1} 段未保存完整、已损坏或变化，未跳过此段；原录像保留')
        meta = source.stat()
        identities.append((meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns))
        sources.append(source)
        duration, log = _media_pass([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'info',
            '-xerror', '-i', str(source), '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
            '-progress', 'pipe:1', '-f', 'null', '-'], register=register, cancelled=cancelled)
        current = _stream_signature(log)
        if signature is not None and current != signature:
            raise LiveError(f'第 {number + 1} 段的画面或音轨格式变化，无法无损合并；请分别导出分段，未降画质或丢弃声音')
        signature = current
        if abs(duration - segment['duration']) > max(2, segment['duration'] * .01):
            raise LiveError('分段时长与登记不一致，未导出整场')
        progress('checking', 20 * (number + 1) / len(segments))
    temp = target.with_name('.' + target.stem + '-' + uuid4().hex + '.partial.mp4')
    listing = target.with_name('.lulu-concat-' + uuid4().hex + '.txt')
    try:
        # ffconcat quoting is independent of shell quoting; no shell is involved.
        with listing.open('x', encoding='utf-8') as handle:
            os.chmod(listing, 0o600)
            for source in sources:
                if '\n' in str(source) or '\r' in str(source):
                    raise LiveError('录像路径包含换行，无法安全合并；原文件保留')
                handle.write("file '" + str(source).replace("'", "'\\''") + "'\n")
        _media_pass([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror', '-n',
            '-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v?', '-map', '0:a?',
            '-c', 'copy', '-movflags', '+faststart', '-progress', 'pipe:1', str(temp)],
            register=register, cancelled=cancelled,
            progress=lambda d: progress('merging', 20 + min(55, 55 * d / max(total, .001))))
        duration, log = _media_pass([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'info',
            '-xerror', '-i', str(temp), '-map', '0:v?', '-map', '0:a?',
            '-progress', 'pipe:1', '-f', 'null', '-'], register=register, cancelled=cancelled,
            progress=lambda d: progress('verifying', 75 + min(20, 20 * d / max(total, .001))))
        if _stream_signature(log) != signature:
            raise LiveError('合并后的画面或音轨与原段不一致，未发布文件')
        if abs(duration - total) > max(2, len(segments) * .1, total * .0001):
            raise LiveError('合并视频时长与全部分段不一致，未发布文件；原录像保留')
        for source, segment, expected in zip(sources, segments, identities):
            check_cancel()
            source = managed_file(source, recording['directory'])
            meta = source.stat()
            if ((meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns) != expected
                    or checked_hash(source) != segment['sha256']):
                raise LiveError('导出期间原分段已变化，未发布合并文件')
        check_cancel()
        with temp.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(temp, target, follow_symlinks=False)
        progress('complete', 100)
        return dict(path=str(target), duration=duration, segment_count=len(segments),
                    bytes=target.stat().st_size, sha256=fingerprint(target))
    finally:
        for path in (temp, listing):
            if path.exists() and not path.is_symlink():
                path.unlink()

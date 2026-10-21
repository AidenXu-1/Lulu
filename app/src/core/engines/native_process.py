"""Bounded, offline invocation of the packaged ASR tools and their owner helper."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


class NativeProcessError(RuntimeError):
    def __init__(self, message, stdout):
        self.stdout = stdout
        super().__init__(message)


def runtime_root():
    if getattr(sys, 'frozen', False): return Path(sys._MEIPASS) / 'asr-runtime'
    return Path(__file__).resolve().parents[3] / 'build' / 'asr-runtime'


def runtime_data_root(root):
    if root.parent.name == 'Frameworks' and root.parent.parent.name == 'Contents':
        return root.parent.parent / 'Resources' / 'asr-runtime'
    return root


def checked_tools(tool):
    root = runtime_root()
    data = runtime_data_root(root)
    if any(p.is_symlink() for base in (root, data) for p in (base, *base.parents)):
        raise RuntimeError('本地转录工具路径无效，请重新准备运行工具。')
    manifest = json.loads((data / 'manifest.json').read_text())
    if manifest.get('schema') != 1 or tool not in {'sensevoice-q8', 'moss'}:
        raise RuntimeError('本地转录工具清单无效。')
    # Includes the owner helper, dynamic libraries and any Metal resources.
    for name, digest in manifest['files'].items():
        path = (root if name in manifest['audit'] else data) / name
        if (Path(name).name != name or path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise RuntimeError('本地转录工具校验失败，请重新准备运行工具。')
    binary = 'llama-funasr-sensevoice' if tool == 'sensevoice-q8' else 'transcribe-cli'
    if any(name not in manifest['files'] for name in ('asr-runner', binary)):
        raise RuntimeError('本地转录工具不完整。')
    return root / 'asr-runner', root / binary, manifest['versions'][tool]


def run_native(tool, make_arguments, *, is_cancelled, progress):
    runner, binary, version = checked_tools(tool)
    from .asr_resource import resource_fd
    with tempfile.TemporaryDirectory(prefix='lulu-asr-') as directory:
        work = Path(directory)
        args = make_arguments(work)
        env = dict(PATH='/usr/bin:/bin', HOME=directory, TMPDIR=directory,
                   HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1')
        if is_cancelled(): raise InterruptedError('转录已取消')
        # Files prevent a full pipe deadlock and are polled with a strict size cap.
        with (work / 'output').open('w+b') as out, (work / 'error').open('w+b') as err:
            fd = resource_fd()
            proc = subprocess.Popen([str(runner), str(os.getpid()), str(binary), *args],
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=work,
                start_new_session=True, close_fds=True, pass_fds=(fd,))
            try:
                while proc.poll() is None:
                    if is_cancelled(): raise InterruptedError('转录已取消')
                    if os.fstat(out.fileno()).st_size > 32*1024*1024 or os.fstat(err.fileno()).st_size > 4*1024*1024:
                        raise RuntimeError('转录工具输出异常，已停止处理，原文件保留。')
                    time.sleep(.1)
                if is_cancelled(): raise InterruptedError('转录已取消')
                out.seek(0); err.seek(0)
                stdout = out.read(32*1024*1024 + 1)
                stderr = err.read(4*1024*1024 + 1)
                if len(stdout) > 32*1024*1024 or len(stderr) > 4*1024*1024:
                    raise RuntimeError('转录工具输出超过限制，原文件保留。')
                if b'compute failed' in stderr.lower():
                    raise NativeProcessError('本地转录计算未完成，原文件保留，请换用其他本地模型。', stdout.decode('utf-8', errors='replace'))
                if proc.returncode:
                    raise NativeProcessError(f'本地转录工具未完成（{proc.returncode}），原文件保留。', stdout.decode('utf-8', errors='replace'))
                return stdout.decode('utf-8'), version
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)

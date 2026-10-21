"""Fixed-version download plans, measured progress, and full-content verification.

The SDK still owns HTTP, retries and resume files. Only private installation
candidates are inspected here; installed models are never removed by a plan.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import threading
import time


def format_bytes(value: float) -> str:
    value = max(0, value)
    return f"{value / 10**9:.2f} GB" if value >= 10**9 else f"{value / 10**6:.1f} MB"


@dataclass
class File:
    path: Path
    size: int
    digest: str
    algorithm: str = "sha256"
    partials: tuple[Path, ...] = ()
    verified: bool = False
    reused: bool = False

    def held(self) -> int:
        if self.verified:
            return self.size
        sizes = []
        for path in (self.path, *self.partials):
            try:
                sizes.append(path.stat().st_size if path.is_file() else 0)
            except OSError:
                pass
        return min(self.size, max(sizes, default=0))

    def valid(self, cancel=lambda: False) -> bool:
        try:
            if not self.path.is_file() or self.path.stat().st_size != self.size:
                return False
            stream = self.path.open("rb")
        except OSError:
            return False
        digest = hashlib.sha256() if self.algorithm == "sha256" else hashlib.sha1()
        if self.algorithm == "git-sha1":
            digest.update(f"blob {self.size}\0".encode())
        with stream:
            while True:
                cancel()
                try:
                    data = stream.read(1024 * 1024)
                except OSError:
                    return False
                if not data:
                    break
                digest.update(data)
        return digest.hexdigest() == self.digest


@dataclass
class Snapshot:
    repo: str
    revision: str
    root: Path
    files: list[File]
    source: str = "hf"
    local_dir: bool = True
    probe_url: str = ""
    probe_size: int | None = None

    def isolate_invalid(self, path):
        if path.is_file():
            target = path.resolve()
            if self.root.is_symlink() or self.root.resolve() not in target.parents:
                raise RuntimeError("模型缓存路径异常，已停止安装。")
            target.replace(target.with_name(target.name + ".lulu-invalid"))

    def verify(self, check, report):
        report("正在校验模型文件，完成后继续准备…", -1)
        for entry in self.files:
            check()
            if not entry.valid(check):
                # Preserve the suspect candidate for inspection. SDK must fetch a
                # new copy on retry; never turn corrupt bytes into a valid receipt.
                self.isolate_invalid(entry.path)
                entry.verified = False
                raise RuntimeError(f"模型文件校验未通过：{entry.path.name}。已保留其他进度，请继续安装以重新取得此文件。")
            entry.verified = True


def _safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts
            or "\\" in name or "\0" in name):
        raise RuntimeError("模型文件清单包含异常路径。")
    return path


def _metadata(source, repo, revision, fetch):
    """Reuse privately saved metadata for this immutable revision, also offline."""
    from . import models_manager as mm
    from .artifact_trust import _assert_private_entry
    base = mm.app_data_dir() / "model-download-plans"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_private_entry(base, directory=True)
    key = hashlib.sha256(f"{source}:{repo}:{revision}".encode()).hexdigest()
    path = base / f"{key}.json"
    if path.exists():
        _assert_private_entry(path, directory=False)
        saved = json.loads(path.read_text())
        if saved.get("identity") == [source, repo, revision]:
            return saved["files"]
        raise RuntimeError("本机下载清单身份不一致，请保留现场检查。")
    result = fetch()
    fd, temp = tempfile.mkstemp(dir=base, prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"identity": [source, repo, revision], "files": result}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temp).replace(path)
    finally:
        Path(temp).unlink(missing_ok=True)
    return result


def hf_plan(repo, revision, root, *, local_dir=True, allow_patterns=None, read_only=False):
    from huggingface_hub import HfApi, hf_hub_url
    from huggingface_hub._local_folder import get_local_download_paths
    from .artifact_trust import HF_MODEL_REVISIONS
    from .synthesis.qwen3_model_lock import MODELS
    qwen = next((lock for lock in MODELS.values() if lock["repo"] == repo), None)
    pinned = qwen["revision"] if qwen else HF_MODEL_REVISIONS.get(repo)
    if pinned != revision:
        raise RuntimeError("模型版本未列入 Lulu 的可信清单。")
    root = Path(root)
    def fetch():
        info = HfApi(endpoint="https://huggingface.co", token=False).model_info(
            repo, revision=revision, files_metadata=True, timeout=15,
        )
        if info.sha != revision:
            raise RuntimeError("模型服务返回的版本与下载计划不一致。")
        values = [{"name": item.rfilename, "size": item.size,
                   "digest": item.lfs.sha256 if item.lfs else item.blob_id,
                   "lfs": bool(item.lfs)} for item in info.siblings]
        for item in values:
            _safe_name(item["name"])
            digest = item["digest"]
            if (type(item["size"]) is not int or item["size"] < 0 or not isinstance(digest, str)
                    or len(digest) != (64 if item["lfs"] else 40)
                    or any(ch not in "0123456789abcdef" for ch in digest)):
                raise RuntimeError("模型文件清单缺少大小或内容摘要，请稍后重试。")
        if not values:
            raise RuntimeError("模型版本没有可下载文件，请稍后重试。")
        return values
    metadata = _metadata("hf", repo, revision, fetch)
    files = []
    first = ""
    first_size = -1
    for item in metadata:
        name = item["name"]
        _safe_name(name)
        if allow_patterns is not None and not any(fnmatch.fnmatch(name, p) for p in allow_patterns):
            continue
        lfs = item["lfs"]
        digest = item["digest"]
        algorithm = "sha256" if lfs else "git-sha1"
        if (not isinstance(item["size"], int) or item["size"] < 0 or not isinstance(digest, str)
                or len(digest) != (64 if lfs else 40)
                or any(ch not in "0123456789abcdef" for ch in digest)):
            raise RuntimeError("模型文件清单缺少大小或内容摘要，暂不能安全安装。")
        if local_dir and read_only:
            # Existing installations must not gain SDK metadata or lock files
            # merely because we are checking their official content hashes.
            path, partials = root / name, ()
        elif local_dir:
            paths = get_local_download_paths(root, name)
            path, partials = paths.file_path, (paths.incomplete_path(digest),)
        else:
            storage = root / ("models--" + repo.replace("/", "--"))
            path = storage / "snapshots" / revision / name
            partials = (storage / "blobs" / (digest + ".incomplete"),)
        if qwen:
            expected = next((entry for entry in qwen["files"] if entry["path"] == name), None)
            if (expected is None or expected["size"] != item["size"]
                    or lfs and expected["sha256"] != digest):
                raise RuntimeError("模型服务返回的文件与 Lulu 固定清单不一致。")
            digest, algorithm = expected["sha256"], "sha256"
        files.append(File(path, item["size"], digest, algorithm, partials))
        # A weight probes the same redirected content host as the large transfer.
        if item["size"] > first_size:
            first = name
            first_size = item["size"]
    if not files:
        raise RuntimeError("所选模型版本没有可下载文件。")
    return Snapshot(repo, revision, root, files, local_dir=local_dir,
                    probe_url=hf_hub_url(repo, first, revision=revision, endpoint="https://huggingface.co"),
                    probe_size=first_size)


def modelscope_plan(repo, revision, root):
    from modelscope.hub.api import HubApi
    from modelscope.hub.file_download import get_file_download_url
    from .artifact_trust import model_revision, _trusted_modelscope_endpoint, _trusted_modelscope_files
    if model_revision(repo, "modelscope") != revision:
        raise RuntimeError("模型版本未列入 Lulu 的可信清单。")
    api = HubApi()
    endpoint = _trusted_modelscope_endpoint(api.endpoint)
    metadata = _trusted_modelscope_files(_metadata("modelscope", repo, revision, lambda: _trusted_modelscope_files(api.get_model_files(
        model_id=repo, revision=revision, recursive=True, use_cookies=False, endpoint=endpoint,
    ))))
    root = Path(root)
    files = [File(root / f["Path"], f["Size"], f["Sha256"],
                  partials=(root / "._____temp" / f["Path"],)) for f in metadata]
    weights = [f for f in metadata if Path(f["Path"]).suffix.lower() in {".bin", ".pt", ".pth", ".safetensors", ".onnx"}]
    largest = max(weights or metadata, key=lambda f: f["Size"])
    return Snapshot(repo, revision, root, files, "modelscope", probe_url=get_file_download_url(
        repo, largest["Path"], revision, endpoint=endpoint,
    ), probe_size=largest["Size"])


def check_space(root: Path, required: int):
    from .synthesis.install_support import require_free_space
    root = Path(root)
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK):
        raise RuntimeError("模型保存位置不可写，请检查磁盘连接和目录权限。")
    require_free_space(ancestor, required)


def _probe_source_once(url, check, source="hf", expected_size=None):
    if source == "hf":
        from huggingface_hub.utils import get_session
        client = get_session()
    else:
        import requests
        client = requests
    check()
    try:
        with client.get(url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"}, stream=True, timeout=(10, 15)) as response:
            response.raise_for_status()
            if response.status_code not in (200, 206) or not next(response.iter_content(1), b""):
                raise RuntimeError("empty response")
            if expected_size is not None:
                content_range = response.headers.get("Content-Range")
                # ModelScope may send a correct one-byte Content-Range with 200.
                # Validate the range itself instead of rejecting a usable CDN.
                if content_range and content_range != f"bytes 0-0/{expected_size}":
                    raise RuntimeError("wrong content range")
                if response.status_code == 206 and not content_range:
                    raise RuntimeError("missing content range")
                if not content_range and response.headers.get("Content-Length") not in (None, str(expected_size)):
                    raise RuntimeError("wrong content size")
    except Exception as exc:
        from .synthesis.install_support import InstallError
        name = "Hugging Face" if source == "hf" else "ModelScope"
        status = getattr(getattr(exc, "response", None), "status_code", None)
        retryable = isinstance(exc, OSError) and (status is None or status in {408, 429} or status >= 500)
        raise InstallError("network" if retryable else "source",
                           f"暂时无法读取 {name} 模型文件。请检查网络或代理后重试，已保留下载进度。",
                           retryable=retryable) from exc
    check()


def probe_source(url, check, source="hf", expected_size=None):
    from .synthesis.install_support import retry_call
    retry_call(lambda _attempt: _probe_source_once(url, check, source, expected_size),
               is_cancelled=lambda: check() or False, attempts=3)


def probe_runtime(url, size, check, report=lambda *_: None):
    from .synthesis import runtime_packages as rp
    from .synthesis.install_support import retry_call
    def attempt(_number):
        with rp._open_runtime_url(url, 15, headers={"Range": "bytes=0-0"}) as response:
            if (rp._runtime_response_status(response) != 206
                    or response.headers.get("Content-Range") != f"bytes 0-0/{size}"
                    or not response.read(1)):
                raise RuntimeError("运行环境下载连接未返回正确文件分片，请检查网络或代理后重试。")
    retry_call(attempt, is_cancelled=lambda: check() or False, attempts=3,
               on_retry=lambda n, _code: report(f"下载连接暂时中断，正在重新检查（{n}/3）…", -1))


_active = threading.local()


class Session:
    def __init__(self, report, check, *, interval=0.25):
        self.report, self.check = report, check
        self.snapshots = []
        self._stop = threading.Event()
        self._thread = None
        self._downloading = False
        self.reservations = []
        self.interval = max(0.05, interval)

    def add(self, plan):
        self.check()
        self.report("正在检查本机文件与下载空间…", -1)
        for entry in plan.files:
            for path in (entry.path, *entry.partials):
                if plan.root.is_symlink() or plan.root.resolve() not in path.resolve().parents:
                    raise RuntimeError("模型缓存路径异常，已停止安装。")
            entry.reused = entry.verified = entry.valid(self.check)
            if not entry.reused:
                plan.isolate_invalid(entry.path)
                for path in entry.partials:
                    if path.is_file() and path.stat().st_size >= entry.size:
                        partial = File(path, entry.size, entry.digest, entry.algorithm)
                        if not partial.valid(self.check):
                            plan.isolate_invalid(path)
        # Candidate + SDK temporary data, while keeping the old installation.
        remaining = sum(f.size - f.held() for f in plan.files if not f.reused)
        check_space(plan.root, remaining + 256 * 1024**2)
        if any(not f.reused for f in plan.files):
            self.report("正在检查模型下载连接…", -1)
            if plan.source == "runtime":
                probe_runtime(plan.probe_url, plan.files[0].size, self.check, self.report)
            else:
                probe_source(plan.probe_url, self.check, plan.source, plan.probe_size)
        self.snapshots.append(plan)
        return plan

    def find(self, repo, revision, root):
        return next((p for p in self.snapshots if p.repo == repo and p.revision == revision
                     and p.root == Path(root)), None)

    def measure(self):
        seen = set()
        files = []
        for plan in self.snapshots:
            for entry in plan.files:
                key = (str(plan.root), plan.repo, entry.digest) if not plan.local_dir else str(entry.path)
                if entry.reused or key in seen:
                    continue
                seen.add(key)
                files.append(entry)
        return sum(f.held() for f in files), sum(f.size for f in files)

    def prepare_space(self):
        # All plans are known before transfer. Sum reservations on the same disk.
        groups = {}
        for plan in self.snapshots:
            ancestor = plan.root
            while not ancestor.exists():
                ancestor = ancestor.parent
            device = ancestor.stat().st_dev
            required = sum(f.size - f.held() for f in plan.files if not f.reused)
            previous = groups.get(device, (ancestor, 0))
            groups[device] = (ancestor, previous[1] + required)
        for root, size in self.reservations:
            ancestor = Path(root)
            while not ancestor.exists():
                ancestor = ancestor.parent
            device = ancestor.stat().st_dev
            previous = groups.get(device, (ancestor, 0))
            groups[device] = (ancestor, previous[1] + size)
        for ancestor, size in groups.values():
            check_space(ancestor, size + 256 * 1024**2)

    def _observe(self):
        previous, _ = self.measure()
        previous_at = time.monotonic()
        while not self._stop.wait(self.interval):
            if not self._downloading:
                previous, _ = self.measure()
                previous_at = time.monotonic()
                continue
            done, total = self.measure()
            now = time.monotonic()
            speed = max(0, done - previous) / max(.001, now - previous_at)
            previous, previous_at = done, now
            if total:
                pct = min(100, done * 100 // total)
                self.report(f"{format_bytes(done)} / {format_bytes(total)} · {pct}% · {format_bytes(speed)}/s", done / total)

    @contextmanager
    def downloading(self):
        self._downloading = True
        try:
            yield
        finally:
            self._downloading = False

    def __enter__(self):
        self.previous = getattr(_active, "session", None)
        _active.session = self
        self._thread = threading.Thread(target=self._observe, name="model-byte-progress", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        _active.session = self.previous


def current():
    return getattr(_active, "session", None)


@contextmanager
def incremental_hf_transport():
    """Use the SDK's HTTP fallback: partial length must mean received bytes.

    Xet may preallocate a complete file, whose size is not download progress.
    Keep this setting scoped to the single installation, and restore afterward.
    """
    from huggingface_hub import constants
    old_xet = constants.HF_HUB_DISABLE_XET
    old_transfer = constants.HF_HUB_ENABLE_HF_TRANSFER
    constants.HF_HUB_DISABLE_XET = True
    constants.HF_HUB_ENABLE_HF_TRANSFER = False
    try:
        yield
    finally:
        constants.HF_HUB_DISABLE_XET = old_xet
        constants.HF_HUB_ENABLE_HF_TRANSFER = old_transfer


from .engines.asr_resource import model_maintenance


@model_maintenance
def prepare_asr(session, model_id):
    from . import models_manager as mm
    from .artifact_trust import model_revision, recover_device_receipt
    session.report("安装前检查 · 正在确认模型版本…", -1)
    model = mm.LOCAL_MODELS[model_id]
    session.check()
    if mm.is_downloaded(model_id):
        return
    for trust in mm._trust_options(model_id):
        if recover_device_receipt(mm._artifact_id(model_id), mm.model_path(model_id), trust, mm._receipt_base(), session.check):
            session.report("已有模型内容校验通过，已恢复识别。", -1)
            return
    from .asr_model_reuse import recover_legacy_asr_receipt
    if recover_legacy_asr_receipt(session, model_id):
        session.report("已有模型校验完成，可直接使用，无需重新下载。", -1)
        return
    if model_id in {"local_whisper_mlx", "local_whisper_mlx_4bit"} and (issue := mm.runtime_issue(model_id)):
        raise RuntimeError(issue)
    _, candidate = mm._private_candidate(model_id)
    if model_id in mm.LOCAL_MODEL_FILES:
        for repo, files in mm.LOCAL_MODEL_FILES[model_id].items():
            session.check()
            session.add(hf_plan(repo, model_revision(repo, 'hf'), candidate, allow_patterns=list(files)))
        session.prepare_space()
        return
    session.add(hf_plan(model.repo, model_revision(model.repo, 'hf'), candidate))
    session.prepare_space()


def prepare_runtime(session, model_id, local_archive=None):
    from .synthesis import runtime_packages as rp
    from .artifact_trust import recover_device_receipt
    spec = rp.TTS_RUNTIMES[model_id]
    if rp.current_platform() is not None:
        for manifest in rp._runtime_manifests(spec, rp.current_platform()):
            if recover_device_receipt(rp._receipt_id("tts-runtime", model_id), rp.runtime_dir(model_id),
                                      manifest, rp.mm.app_data_dir() / ".artifact-trust", session.check):
                break
    if rp.is_runtime_installed(model_id):
        manifest = rp._installed_runtime_manifest(model_id)
        session.report("正在检查已有运行环境…", -1)
        try:
            rp.check_runtime_health(model_id, rp.runtime_dir(model_id), lambda: session.check() or False)
        except RuntimeError:
            info = rp.runtime_python(model_id).stat()
            rp._write_json_atomic(rp.model_dir(model_id) / ".runtime-health-failure.json", {
                "version": manifest["version"], "device": info.st_dev, "inode": info.st_ino,
            })
            raise
        return
    if rp.current_platform() is None:
        raise RuntimeError("当前平台暂不支持此模型的运行环境。")
    if local_archive is None and not spec.runtime_download_available:
        raise RuntimeError("此模型的安装资源暂未上线，现有模型和下载进度会保留。")
    manifest = rp._trusted_manifest(spec, rp.current_platform())
    cache = rp._runtime_download_cache(model_id, manifest["sha256"])
    path = cache / manifest["file"]
    entry = File(Path(local_archive) if local_archive else path, manifest["size"], manifest["sha256"],
                 partials=(path.parent / f".{path.name}.lulu-resume",))
    # The archive format has a hard 12 GiB extraction cap. Until the archive is
    # available, reserve that ceiling rather than claiming an unknown exact size.
    from .artifact_trust import TTS_RUNTIME_UNPACKED_BYTES
    reserve = TTS_RUNTIME_UNPACKED_BYTES.get(manifest["sha256"], 12 * 1024**3)
    if entry.valid(session.check):
        import tarfile
        with tarfile.open(entry.path, "r:gz") as archive:
            reserve = sum(m.size for m in archive.getmembers() if m.isfile())
    session.report(f"正在检查运行环境空间 · 预留 {format_bytes(reserve)}…", -1)
    check_space(rp.model_dir(model_id), reserve + 256 * 1024**2)
    session.reservations.append((rp.model_dir(model_id), reserve))
    if local_archive:
        if not entry.valid(session.check):
            raise RuntimeError("本地运行环境包未通过完整性校验。")
        return
    session.add(Snapshot(model_id, manifest["sha256"], cache, [entry], "runtime",
                         probe_url=f"{rp._release_base(spec, rp.current_platform())}/{manifest['file']}"))


def prepare_tts(session, model_id, local_archive=None, names=None):
    from .synthesis import runtime_packages as rp, local_models as lm
    from .artifact_trust import HF_MODEL_REVISIONS, recover_device_receipt
    session.report("安装前检查 · 正在确认所需文件…", -1)
    spec = rp.TTS_RUNTIMES[model_id]
    from .synthesis.weights_reuse import recover_qwen_weights, recover_voxcpm_weights
    if spec.weights_handler == 'voxcpm2':
        recover_voxcpm_weights(session.check, session.report)
    elif spec.weights_handler == 'qwen3':
        from .synthesis import qwen3_deployment as dp, qwen3_weights as qw
        base = dp.root()
        with qw.deployment_lock(base, lambda: session.check() or False, session.report):
            for name in names if names is not None else qw.MODELS:
                qw._recover(base, name)
                recover_qwen_weights(base, name, session.check, session.report)
    prepare_runtime(session, model_id, local_archive)
    if spec.weights_handler == "voxcpm2":
        recover_device_receipt(f"tts-weights:{model_id}", lm.model_dir(lm.VOXCPM2.id),
                               lm._trust(model_id), lm._receipt_base(), session.check)
    elif spec.weights_handler == "indextts2":
        from .synthesis import indextts2_weights as iw
        options = rp.index_weights_options(model_id)
        profile = iw.weight_profile(**options)
        recover_device_receipt(profile.receipt_id, rp.weights_dir(model_id), iw._weights_trust(**options),
                               iw._receipt_base(), session.check)
    if spec.weights_handler == "qwen3":
        from .synthesis import qwen3_weights as qw, qwen3_deployment as dp
        base = dp.root()
        grouped = {}
        for name, entry in dp._shared_entries():
            if qw._shared_source(base, entry, session.check) is None:
                grouped.setdefault(name, []).append(entry["path"])
        for name, paths in grouped.items():
            lock = qw.MODELS[name]
            session.add(hf_plan(lock["repo"], lock["revision"], base.parent / "downloads/shared",
                                local_dir=False, allow_patterns=paths))
        shared = qw._shared_keys()
        for name in names if names is not None else qw.MODELS:
            if qw.is_installed(base, name):
                continue
            lock = qw.MODELS[name]
            paths = [e["path"] for e in lock["files"] if qw._key(e) not in shared and not any(
                qw._valid(p, e, session.check) for p in (base / name / e["path"], qw._snapshot(base, name) / e["path"])
            )]
            if paths:
                session.add(hf_plan(lock["repo"], lock["revision"], base.parent / "downloads" / name,
                                    local_dir=False, allow_patterns=paths))
    elif not rp.is_weights_installed(model_id):
        if spec.weights_handler == "indextts2":
            from .synthesis import indextts2_weights as iw
            options = rp.index_weights_options(model_id)
            profile = iw.weight_profile(**options)
            rp.weights_dir(model_id).parent.mkdir(parents=True, exist_ok=True)
            cache, candidate = iw._resume_candidate(rp.weights_dir(model_id), **options)
            requests = [(profile.repo, profile.revision, candidate, None),
                        (iw._W2V_REPO, profile.auxiliary[iw._W2V_REPO], candidate / "hf_cache/w2v-bert-2.0",
                         ["config.json", "preprocessor_config.json", "model.safetensors"] if options.get("version") == "2.0" else None),
                        (iw._BIGVGAN_REPO, profile.auxiliary[iw._BIGVGAN_REPO], candidate / "hf_cache/bigvgan",
                         ["config.json", "bigvgan_generator.pt"])]
            requests.extend((repo, profile.auxiliary[repo], cache / "aux-source" / repo.replace("/", "--"), [remote])
                            for repo, remote, _ in profile.single_files)
            for repo, revision, root, patterns in requests:
                plan = session.add(hf_plan(repo, revision, root, allow_patterns=patterns))
                if "aux-source" in root.parts:
                    session.reservations.append((candidate, sum(f.size for f in plan.files)))
        else:
            revision = HF_MODEL_REVISIONS[spec.weights_repo]
            root = lm.model_dir(lm.VOXCPM2.id) / f".weights-download-{revision}"
            session.add(hf_plan(spec.weights_repo, revision, root, local_dir=False))
    session.prepare_space()


def hf_snapshot(repo, *, revision, local_dir, tqdm_class=None, allow_patterns=None):
    """Use existing SDK transport, then verify every resumed byte before promotion."""
    from huggingface_hub import snapshot_download
    session = current()
    if session is None:
        # Non-UI callers receive the same verification with silent reporting.
        with Session(lambda *_: None, lambda: None):
            return hf_snapshot(repo, revision=revision, local_dir=local_dir,
                               tqdm_class=tqdm_class, allow_patterns=allow_patterns)
    plan = session.find(repo, revision, local_dir)
    if plan is None:
        plan = session.add(hf_plan(repo, revision, local_dir, allow_patterns=allow_patterns))
    if all(f.verified for f in plan.files):
        return str(local_dir)
    with session.downloading(), incremental_hf_transport():
        result = snapshot_download(repo, revision=revision, local_dir=local_dir,
                                   tqdm_class=tqdm_class, allow_patterns=allow_patterns,
                                   endpoint="https://huggingface.co", token=False)
    plan.verify(session.check, session.report)
    return result

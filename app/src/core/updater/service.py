from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import http.client
import os
from pathlib import Path
import re
import secrets
import time
from typing import Callable, Mapping

from ...version import __build__, __version__, runtime_channel
from .archive import extract_zip, validate_staged_app
from .cleanup import cleanup_stale_on_startup
from .constants import CONNECT_TIMEOUT_SECONDS, MANIFEST_TOTAL_TIMEOUT_SECONDS, MANIFEST_URL, MAX_MANIFEST_BYTES, MAX_SIG_BASE64_BYTES, MAX_ZIP_BYTES, READ_IDLE_TIMEOUT_SECONDS, SIGNATURE_URL, SIG_TOTAL_TIMEOUT_SECONDS, ZIP_TOTAL_TIMEOUT_SECONDS
from .manifest import build_tuple, parse_manifest, verify_signature, version_tuple
from .models import UpdateError, UpdateManifest, UpdateSnapshot
from .network import SecureHttpClient
from .secure_io import assert_regular_nofollow, atomic_json, ensure_private_dir, load_json, read_nofollow
from .state import StateStore
from .constants import KEY_ID

_META = {"url", "version", "build", "sha256", "etag", "downloaded_bytes", "expected_size", "filename", "txn"}


class UpdateService:
    def __init__(self, *, root: Path | None = None, client: SecureHttpClient | None = None, public_keys: Mapping[str, str] | None = None):
        self.store = StateStore(root)
        self.client = client or SecureHttpClient()
        self.public_keys = dict(_default_public_keys() if public_keys is None else public_keys)
        try:
            self.snapshot = self.store.load()
            cleanup_stale_on_startup(self.store.root, self.snapshot)
        except UpdateError:
            self.snapshot = UpdateSnapshot(state="failed_state_corrupt", ui_state="check_error", error_code="state_corrupt", message="更新状态损坏")
        self.manifest: UpdateManifest | None = None
        # A persisted worker state is not a running worker. The external helper
        # owns installation while its lock exists; never reset its progress.
        installing = os.path.lexists(self.store.root / "transactions/update.lock")
        if not installing and self._retire_superseded_update():
            cleanup_stale_on_startup(self.store.root, self.snapshot)
        if not installing and self.snapshot.state in {"installing", "waiting_ready", "recovering"}:
            self._save(state="failed", ui_state="install_interrupted", verified_manifest=False,
                       error_code="interrupted_install", message="上次安装没有留下完整结果，请重新检查或手动下载安装。")
        elif not installing and self.snapshot.ui_state in {"downloading", "verifying", "preparing"}:
            self._save(state="download_paused", ui_state="download_paused", verified_manifest=False,
                       error_code="interrupted", message="上次更新已中断，可重新校验并继续下载。")
        elif not installing and self.snapshot.ui_state in {"checking_auto", "checking_manual"}:
            if self.snapshot.state == "download_paused":
                self._save(ui_state="download_paused", verified_manifest=False,
                           error_code="interrupted", message="上次检查已中断，可重新校验并继续下载。")
            else:
                self._save(state="idle", ui_state="check_error", verified_manifest=False,
                           error_code="interrupted", message="上次检查已中断，请重新检查更新。")
        if self.snapshot.ui_state == "update_available":
            self.snapshot.state = "idle"
            self.snapshot.ui_state = "check_error"
            self.snapshot.error_code = "manifest_recheck_required"
            self.snapshot.message = "请重新检查更新后再下载。"
            self.snapshot.update_available = False
            self.snapshot.verified_manifest = False
            try:
                self.store.save(self.snapshot)
            except OSError:
                pass

    def _retire_superseded_update(self):
        """A manual replacement can supersede another version's cached update."""
        snapshot = self.snapshot
        try:
            installed = (version_tuple(__version__), build_tuple(__build__))
            target = (version_tuple(snapshot.latest_version), build_tuple(snapshot.latest_build))
            previous = ((version_tuple(snapshot.current_version), build_tuple(snapshot.current_build))
                        if snapshot.current_version and snapshot.current_build else None)
        except UpdateError:
            # Old/incomplete metadata requires a fresh check, never a downgrade.
            if snapshot.ui_state in {'ready', 'ready_blocked'}:
                self._save(state='idle', ui_state='check_error', error_code='version_recheck',
                    message='更新版本信息不完整，请重新检查更新。', staged_app='',
                    staged_manifest_sha256='', verified_manifest=False, update_available=False, txn='',
                    downloaded=0, total=0, asset_size=0)
                return True
            return False
        if target <= installed and ((previous is not None and previous < installed)
                                   or snapshot.ui_state in {'ready', 'ready_blocked'}):
            self._save(state='installed', ui_state='installed', error_code='',
                message='当前程序已更新，旧安装提示已清除。', staged_app='', staged_manifest_sha256='',
                verified_manifest=False, update_available=False, txn='', downloaded=0, total=0,
                asset_size=0, deferred=False, notes='', latest_version=__version__, latest_build=__build__)
            return True
        return False

    def refresh_external(self) -> bool:
        """Read the helper's result even while automatic network checks are off."""
        if self.snapshot.state not in {"installing", "waiting_ready", "recovering"} and self.snapshot.error_code != 'interrupted_install':
            return False
        latest = self.store.load()
        if latest.txn != self.snapshot.txn or latest.to_dict() == self.snapshot.to_dict():
            return False
        self.snapshot = latest
        return True

    def _save(self, **changes) -> UpdateSnapshot:
        for key, value in changes.items():
            setattr(self.snapshot, key, value)
        self.snapshot.current_version = __version__
        self.snapshot.current_build = __build__
        self.store.save(self.snapshot)
        return self.snapshot

    def check(self, *, manual: bool) -> UpdateSnapshot:
        # Completion is durable before the helper finishes its cleanup. Keep a
        # new check/download from racing with that still-owned installation.
        if os.path.lexists(self.store.root / 'transactions/update.lock'):
            return self.snapshot
        source = "manual" if manual else "auto"
        # Checking a signature must not erase an interrupted download's lifetime.
        # Its binding and complete digest are still checked before use below.
        pending_download = self.snapshot.state == "download_paused" and 0 < self.snapshot.downloaded < self.snapshot.total
        self.manifest = None
        self._save(state="download_paused" if pending_download else "checking", ui_state="checking_manual" if manual else "checking_auto", source=source, error_code="", message="", verified_manifest=False)
        headers = {"User-Agent": f"Lulu/{__version__} macOS arm64"}
        try:
            manifest_response = self.client.open(MANIFEST_URL, headers=headers, connect_timeout=CONNECT_TIMEOUT_SECONDS, total_timeout=MANIFEST_TOTAL_TIMEOUT_SECONDS)
            manifest_bytes = self.client.read_limited(manifest_response, limit=MAX_MANIFEST_BYTES, total_timeout=MANIFEST_TOTAL_TIMEOUT_SECONDS)
            sig_response = self.client.open(SIGNATURE_URL, headers=headers, connect_timeout=CONNECT_TIMEOUT_SECONDS, total_timeout=SIG_TOTAL_TIMEOUT_SECONDS)
            sig_bytes = self.client.read_limited(sig_response, limit=MAX_SIG_BASE64_BYTES, total_timeout=SIG_TOTAL_TIMEOUT_SECONDS)
            verified_key_id = verify_signature(manifest_bytes, sig_bytes, self.public_keys)
            manifest = parse_manifest(manifest_bytes, local_channel=runtime_channel(), verified_key_id=verified_key_id)
        except UpdateError as exc:
            if pending_download and exc.code in {"network", "timeout", "http"}:
                return self._save(state="download_paused", ui_state="download_paused", error_code=exc.code, message=str(exc), verified_manifest=False, last_checked_at=datetime.now(timezone.utc).isoformat())
            ui = "manifest_verify_error" if exc.code == "signature" else "check_error"
            return self._save(state="failed_signature" if exc.code == "signature" else "failed_security_rejected" if exc.code.startswith("unsafe") else "failed", ui_state=ui, error_code=exc.code, message=str(exc), verified_manifest=False, last_checked_at=datetime.now(timezone.utc).isoformat())
        self.manifest = manifest
        common = dict(latest_version=manifest.latest_version, latest_build=manifest.latest_build, asset_size=manifest.zip_asset.size, published_at=manifest.published_at, notes=manifest.notes_md, manual_url=manifest.dmg_asset.url, verified_manifest=True, last_checked_at=datetime.now(timezone.utc).isoformat(), error_code="", message="")
        if version_tuple(__version__) < version_tuple(manifest.min_supported_version):
            return self._save(state="available", ui_state="manual_required", update_available=True, **common)
        newer = version_tuple(manifest.latest_version) > version_tuple(__version__) or (manifest.latest_version == __version__ and build_tuple(manifest.latest_build) > build_tuple(__build__))
        if not newer:
            return self._save(state="idle", ui_state="up_to_date", update_available=False, **common)
        if pending_download and (manifest.latest_version, manifest.latest_build, manifest.zip_asset.size) == (self.snapshot.latest_version, self.snapshot.latest_build, self.snapshot.total):
            return self._save(state="download_paused", ui_state="download_paused", update_available=True, **common)
        return self._save(state="available", ui_state="update_available", update_available=True, **common)

    def download(self, *, progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> Path:
        if not self.manifest or not self.snapshot.verified_manifest:
            checked = self.check(manual=True)
            if checked.ui_state not in {"update_available", "download_paused"} or not self.manifest:
                raise UpdateError("recheck", checked.message or "更新信息已变化，请查看当前检查结果。")
        asset = self.manifest.zip_asset
        downloads = ensure_private_dir(self.store.root / "downloads")
        part = downloads / (asset.filename + ".part")
        meta_path = downloads / (asset.filename + ".part.meta.json")
        txn = self.snapshot.txn or secrets.token_hex(32)
        expected = {"url": asset.url, "version": self.manifest.latest_version, "build": self.manifest.latest_build, "sha256": asset.sha256, "expected_size": asset.size, "filename": asset.filename}
        offset = 0; etag = ""
        if part.exists() and meta_path.exists():
            try:
                meta = load_json(meta_path, allowed=_META, required=_META)
                part_stat=assert_regular_nofollow(part,mode=0o600)
                txn_value=meta.get("txn")
                if type(meta.get("downloaded_bytes")) is not int or not isinstance(meta.get("etag"),str) or not isinstance(txn_value,str) or len(txn_value)!=64 or any(c not in "0123456789abcdef" for c in txn_value) or any(meta.get(key) != value for key, value in expected.items()) or meta.get("downloaded_bytes") != part_stat.st_size or not meta.get("etag"):
                    raise UpdateError("resume", "续传缓存已失效")
                offset = part_stat.st_size; etag = str(meta["etag"]); txn = txn_value
            except Exception:
                part.unlink(missing_ok=True); meta_path.unlink(missing_ok=True); offset = 0; etag = ""
        headers = {"User-Agent": f"Lulu/{__version__} macOS arm64"}
        if offset:
            headers["Range"] = f"bytes={offset}-"

        def interrupted(exc: UpdateError) -> None:
            # Only retain a closed, bounded file with its complete resume binding.
            # A partial file alone must never become an installable package.
            size = assert_regular_nofollow(part, mode=0o600).st_size if part.exists() else 0
            resumable = bool(etag) and 0 < size < asset.size
            if resumable:
                atomic_json(meta_path, {**expected, "etag": etag, "downloaded_bytes": size, "txn": txn})
            else:
                part.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            self._save(state="download_paused" if resumable else "failed",
                       ui_state="download_paused" if resumable else "download_error",
                       downloaded=size if resumable else 0, total=asset.size, txn=txn,
                       error_code=exc.code, message=str(exc))

        self._save(state="downloading", ui_state="downloading", downloaded=offset,
                   total=asset.size, txn=txn, error_code="", message="")
        try:
            if cancelled and cancelled():
                raise UpdateError("cancelled", "下载已暂停")
            response = self.client.open(asset.url, headers=headers, connect_timeout=CONNECT_TIMEOUT_SECONDS, total_timeout=ZIP_TOTAL_TIMEOUT_SECONDS)
        except UpdateError as exc:
            if exc.code in {"network", "timeout", "http", "cancelled"}:
                interrupted(exc)
            raise
        if not offset and response.status != 200:
            response.stream.close(); raise UpdateError("range", "下载响应异常")
        if offset:
            content_range = response.headers.get("content-range", "")
            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
            valid = response.status == 206 and response.headers.get("etag") == etag and match and int(match.group(1)) == offset and int(match.group(2)) < asset.size and int(match.group(3)) == asset.size
            if not valid:
                response.stream.close(); part.unlink(missing_ok=True); meta_path.unlink(missing_ok=True)
                return self.download(progress=progress, cancelled=cancelled)
        else:
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) != asset.size):
                response.stream.close(); raise UpdateError("size", "下载大小异常")
            etag = response.headers.get("etag", "")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if offset else os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        total = offset; started = time.monotonic()
        try:
            try:
                with os.fdopen(fd, "ab" if offset else "wb") as handle:
                    while True:
                        if cancelled and cancelled():
                            raise UpdateError("cancelled", "下载已暂停")
                        if time.monotonic() - started > ZIP_TOTAL_TIMEOUT_SECONDS:
                            raise UpdateError("timeout", "下载超时")
                        try:
                            chunk = response.stream.read(1024 * 1024)
                        except TimeoutError as exc:
                            raise UpdateError("timeout", "下载连接超时") from exc
                        except (OSError, http.client.HTTPException) as exc:
                            raise UpdateError("network", "下载连接中断") from exc
                        if not chunk: break
                        total += len(chunk)
                        if total > asset.size or total > MAX_ZIP_BYTES:
                            raise UpdateError("size", "下载大小异常")
                        handle.write(chunk)
                        if progress: progress(total, asset.size)
                        self._save(state="downloading", ui_state="downloading", downloaded=total, total=asset.size, txn=txn)
                if total != asset.size:
                    raise UpdateError("download", "下载没有完成")
            finally:
                response.stream.close()
        except Exception as exc:
            if isinstance(exc, UpdateError) and exc.code in {"cancelled", "network", "timeout", "download"}:
                interrupted(exc)
            else:
                part.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            raise
        digest = hashlib.sha256(read_nofollow(part, mode=0o600, maximum=asset.size)).hexdigest()
        if digest != asset.sha256:
            part.unlink(missing_ok=True); meta_path.unlink(missing_ok=True)
            self._save(state="failed_security_rejected", ui_state="verify_error", error_code="sha256")
            raise UpdateError("sha256", "安装包校验失败")
        final = downloads / asset.filename
        os.replace(part, final); os.chmod(final, 0o600); meta_path.unlink(missing_ok=True)
        self._save(state="downloaded", ui_state="verifying", downloaded=total, total=asset.size, txn=txn)
        return final

    def stage(self, zip_path: Path, *, verify_system: bool = True) -> Path:
        if not self.manifest or not self.snapshot.verified_manifest:
            raise UpdateError("unverified", "更新清单尚未验证")
        txn = secrets.token_hex(32)
        staged = self.store.root / "staged" / txn
        self._save(state="downloaded", ui_state="preparing", txn=txn, staged_app="", staged_manifest_sha256="", error_code="", message="")
        try:
            app = extract_zip(zip_path, staged, expected_tree_sha=self.manifest.zip_asset.tree_manifest_sha256)
            manifest_sha = validate_staged_app(app, version=self.manifest.latest_version, build=self.manifest.latest_build, verify_system=verify_system)
            if manifest_sha != self.manifest.zip_asset.tree_manifest_sha256:
                raise UpdateError("tree_manifest", "安装包树校验失败")
        except UpdateError as exc:
            preparation = exc.code == "staged_exists"
            self._save(state="failed" if preparation else "failed_security_rejected",
                       ui_state="preparation_error" if preparation else "zip_structure_error", error_code=exc.code)
            raise
        except OSError as exc:
            code = "storage_full" if exc.errno == errno.ENOSPC else "staging_io"
            self._save(state="failed", ui_state="preparation_error", error_code=code)
            raise UpdateError(code, "更新准备未完成，请检查可用空间和更新目录权限后重试。") from exc
        self._save(state="ready_to_install", ui_state="ready", staged_app=str(app), staged_manifest_sha256=manifest_sha)
        return app


def _default_public_keys() -> dict[str, str]:
    import sys
    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        # PyInstaller puts data in Resources and symlinks it from Frameworks.
        # Anchor trust in this running .app, never follow those data aliases.
        executable = Path(sys.executable)
        macos = executable.parent
        contents = macos.parent
        app = contents.parent
        resources = contents / "Resources"
        if (not executable.is_absolute() or executable.is_symlink()
                or not executable.is_file() or macos.name != "MacOS"
                or contents.name != "Contents" or app.suffix != ".app"
                or any(path.is_symlink() or not path.is_dir()
                       for path in (app, contents, macos, resources))):
            return {}
        bases = [resources]
    else:
        bases = [Path(__file__).resolve().parents[3]]
        base = getattr(sys, "_MEIPASS", None)
        if base: bases.insert(0, Path(base))
    for root in bases:
        bundle = root / "update_public_keys.json"
        try:
            if bundle.is_file() and not bundle.is_symlink():
                import json
                value = json.loads(read_nofollow(bundle, maximum=65536).decode("ascii"))
                if isinstance(value, dict) and 1 <= len(value) <= 4 and all(isinstance(key, str) and 1 <= len(key.encode()) <= 96 and isinstance(pem, str) and "BEGIN PUBLIC KEY" in pem for key, pem in value.items()):
                    return dict(value)
        except Exception:
            pass
        legacy = root / "update_public_key.pem"
        try:
            if legacy.is_file() and not legacy.is_symlink():
                pem = read_nofollow(legacy, maximum=16384).decode("ascii")
                if "BEGIN PUBLIC KEY" in pem: return {KEY_ID: pem}
        except Exception:
            pass
    return {}

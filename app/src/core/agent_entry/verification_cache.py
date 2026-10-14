"""Verification cache bound to both bundle bytes and installed authority files."""
from pathlib import Path
import os
import stat
from threading import RLock
from functools import lru_cache
from copy import deepcopy


def bundle_stamp(root: Path):
    # DirEntry.stat retains the same no-follow metadata boundary, without
    # constructing and relativizing a Path for every bundled framework file.
    root = Path(root)
    rows = []
    def add(name, st):
        rows.append((name, st.st_dev, st.st_ino, st.st_mode,
                     st.st_size, st.st_mtime_ns, st.st_ctime_ns))
    st = root.lstat()
    add('.', st)
    if not stat.S_ISDIR(st.st_mode):
        return tuple(rows)
    pending = [(str(root), '')]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = prefix + entry.name
                st = entry.stat(follow_symlinks=False)
                add(name, st)
                if stat.S_ISDIR(st.st_mode):
                    pending.append((entry.path, name + '/'))
    return tuple(sorted(rows))


class BundleVerificationCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._stamp = None
        self._result = None
        self._lock = RLock()

    def verify(self, verifier, *, authority_paths=()):
        with self._lock:
            def stamp():
                extras = []
                for raw_path in authority_paths:
                    path = Path(raw_path)
                    try:
                        value = bundle_stamp(path)
                    except FileNotFoundError:
                        # The verifier decides whether a missing optional object is valid.
                        if os.path.lexists(path):
                            raise
                        value = None
                    extras.append((str(path), value))
                return bundle_stamp(self.root), tuple(extras)
            before = stamp()
            if before == self._stamp:
                return self._result
            self._stamp = None
            result = verifier()
            after = stamp()
            if before != after:
                raise RuntimeError("COMPONENT_GENERATION_CHANGED_DURING_VERIFICATION")
            self._stamp, self._result = after, result
            return result


@lru_cache(maxsize=4)
def _runtime_component_cache(app: Path, manifest: Path):
    return BundleVerificationCache(app)


def verify_runtime_component_manifest(app, manifest):
    """Reuse process-local evidence only while all package/manifest metadata matches.

    Startup identity, peer requirements and runtime construction describe the
    same package. Keep their shared verification in memory; installation and
    recovery still use the uncached verifier and recheck ownership separately.
    """
    from .component_manifest import verify_component_manifest
    app, manifest = Path(app), Path(manifest)
    result = _runtime_component_cache(app, manifest).verify(
        lambda: verify_component_manifest(app, manifest),
        authority_paths=(manifest,),
    )
    return deepcopy(result)

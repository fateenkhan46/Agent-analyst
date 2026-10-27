"""Object storage behind a narrow interface.

``LocalObjectStore`` is the implemented backend. An S3 backend can be dropped
in without touching callers, but it is **not implemented** here and
``get_store`` will refuse rather than silently fall back and lose data.

Keys are namespaced ``<project_id>/<category>/<name>`` and every write is
confined to the storage root: ``_resolve`` rejects traversal, so a filename
from an upload cannot escape the project workspace.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path

from ...config import get_settings

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def safe_segment(name: str, fallback: str = "file") -> str:
    """Collapse an arbitrary (possibly hostile) filename into one safe path
    segment. Keeps the extension where there is one."""
    name = name.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_SEGMENT.sub("_", name).strip("._-")
    if not name:
        name = fallback
    return name[:180]


class ObjectStore(ABC):
    @abstractmethod
    def put_bytes(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def local_path(self, key: str) -> Path:
        """Filesystem path for readers that need one (pandas, duckdb).
        Backends without local files must materialise to a temp file."""

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int: ...

    @abstractmethod
    def zip_directory(self, dir_key: str, zip_key: str) -> tuple[str, list[str]]: ...


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_settings().storage_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- internals ------------------------------------------------------
    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/"):
            raise ValueError("storage key must be a non-empty relative path")
        parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ValueError(f"storage key escapes the root: {key!r}")
        path = (self.root / "/".join(parts)).resolve()
        if not str(path).startswith(str(self.root)):
            raise ValueError(f"storage key escapes the root: {key!r}")
        return path

    # -- interface ------------------------------------------------------
    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic within a filesystem
        return key

    def put_text(self, key: str, text: str) -> str:
        return self.put_bytes(key, text.encode("utf-8"))

    def get_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def get_text(self, key: str) -> str:
        return self._resolve(key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    def ensure_dir(self, key: str) -> Path:
        p = self._resolve(key)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def size(self, key: str) -> int:
        p = self._resolve(key)
        return p.stat().st_size if p.exists() else 0

    def delete_prefix(self, prefix: str) -> int:
        p = self._resolve(prefix)
        if not p.exists():
            return 0
        if p.is_file():
            p.unlink()
            return 1
        n = sum(1 for _ in p.rglob("*") if _.is_file())
        shutil.rmtree(p)
        return n

    def list_prefix(self, prefix: str) -> list[str]:
        p = self._resolve(prefix)
        if not p.exists():
            return []
        return sorted(
            str(f.relative_to(self.root)) for f in p.rglob("*") if f.is_file()
        )

    def zip_directory(self, dir_key: str, zip_key: str) -> tuple[str, list[str]]:
        """Zip a stored directory. Returns (zip_key, manifest of inner paths)."""
        src = self._resolve(dir_key)
        if not src.is_dir():
            raise FileNotFoundError(f"not a stored directory: {dir_key}")
        buf = io.BytesIO()
        manifest: list[str] = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    inner = str(f.relative_to(src))
                    zf.write(f, inner)
                    manifest.append(inner)
        self.put_bytes(zip_key, buf.getvalue())
        return zip_key, manifest


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_store: ObjectStore | None = None


def get_store() -> LocalObjectStore:
    global _store
    settings = get_settings()
    if settings.storage_backend != "local":
        raise NotImplementedError(
            f"storage_backend={settings.storage_backend!r} is not implemented. "
            "Only 'local' is available; see docs/INTEGRATION_LIMITATIONS.md."
        )
    if _store is None:
        _store = LocalObjectStore()
    return _store  # type: ignore[return-value]


def reset_store() -> None:
    global _store
    _store = None


def project_key(project_id: str, *parts: str) -> str:
    return "/".join([project_id, *[safe_segment(p) for p in parts if p]])

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path


class FilesystemBlobStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"unsafe blob key: {key!r}")
        return self.root / key

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, key, data)

    def _put_sync(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
            raise

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(lambda: self._path(key).read_bytes())

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(lambda: self._path(key).is_file())

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            with contextlib.suppress(FileNotFoundError):
                self._path(key).unlink()

        await asyncio.to_thread(_delete)

    async def delete_prefix(self, prefix: str) -> None:
        """Recursively delete every blob whose key starts with `prefix`.

        Treats `prefix` as a directory boundary: callers pass a trailing slash
        when they mean "everything inside this directory" (e.g. `derived/<id>/`).
        """

        def _delete_tree() -> None:
            target = self._path(prefix)
            if target.is_dir():
                for child in sorted(target.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        with contextlib.suppress(FileNotFoundError):
                            child.unlink()
                    elif child.is_dir():
                        with contextlib.suppress(OSError):
                            child.rmdir()
                with contextlib.suppress(OSError):
                    target.rmdir()

        await asyncio.to_thread(_delete_tree)

    def url_for(self, key: str) -> str | None:
        return None

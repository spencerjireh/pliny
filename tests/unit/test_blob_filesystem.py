from pathlib import Path

import pytest

from pliny.storage.filesystem import FilesystemBlobStore


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path)


async def test_put_get_roundtrip(store: FilesystemBlobStore) -> None:
    await store.put("raw/abc", b"hello")
    assert await store.exists("raw/abc")
    assert await store.get("raw/abc") == b"hello"


async def test_overwrite(store: FilesystemBlobStore) -> None:
    await store.put("raw/abc", b"first")
    await store.put("raw/abc", b"second")
    assert await store.get("raw/abc") == b"second"


async def test_exists_missing(store: FilesystemBlobStore) -> None:
    assert not await store.exists("raw/nope")


async def test_delete(store: FilesystemBlobStore) -> None:
    await store.put("raw/abc", b"data")
    await store.delete("raw/abc")
    assert not await store.exists("raw/abc")


async def test_delete_missing_no_error(store: FilesystemBlobStore) -> None:
    await store.delete("raw/never-existed")


async def test_url_for_returns_none(store: FilesystemBlobStore) -> None:
    assert store.url_for("anything") is None


async def test_unsafe_key_rejected(store: FilesystemBlobStore) -> None:
    with pytest.raises(ValueError):
        await store.put("/etc/passwd", b"x")
    with pytest.raises(ValueError):
        await store.put("../escape", b"x")


async def test_atomic_put_no_partial_files(store: FilesystemBlobStore, tmp_path: Path) -> None:
    await store.put("raw/abc", b"payload")
    files = sorted(p.name for p in (tmp_path / "raw").iterdir())
    assert files == ["abc"]


async def test_delete_prefix_removes_directory_tree(
    store: FilesystemBlobStore, tmp_path: Path
) -> None:
    await store.put("derived/abc/screenshot.png", b"png")
    await store.put("derived/abc/metadata.json", b"{}")
    await store.put("derived/abc/nested/extra.bin", b"x")
    await store.put("derived/other/keep.txt", b"keep")
    await store.put("raw/keep-me", b"keep")

    await store.delete_prefix("derived/abc/")

    assert not await store.exists("derived/abc/screenshot.png")
    assert not await store.exists("derived/abc/metadata.json")
    assert not await store.exists("derived/abc/nested/extra.bin")
    assert not (tmp_path / "derived" / "abc").exists()
    # Sibling and unrelated keys survive.
    assert await store.exists("derived/other/keep.txt")
    assert await store.exists("raw/keep-me")


async def test_delete_prefix_missing_no_error(store: FilesystemBlobStore) -> None:
    await store.delete_prefix("derived/never-existed/")

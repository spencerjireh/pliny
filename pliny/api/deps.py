import hmac
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pliny.config import Settings, get_settings
from pliny.storage.blob import BlobStore
from pliny.storage.filesystem import FilesystemBlobStore

_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None
_blob_store: BlobStore | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, future=True)
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None:
        _session_maker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_maker


async def get_db() -> AsyncIterator[AsyncSession]:
    sm = get_session_maker()
    async with sm() as session:
        yield session


def get_blob() -> BlobStore:
    global _blob_store
    if _blob_store is None:
        _blob_store = FilesystemBlobStore(get_settings().blob_root)
    return _blob_store


def reset_state() -> None:
    """Reset module-level singletons. Used by tests to swap engines."""
    global _engine, _session_maker, _blob_store
    _engine = None
    _session_maker = None
    _blob_store = None


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings.api_key.encode()
    if not authorization or not authorization.startswith("Bearer "):
        # constant-time comparison even when malformed
        hmac.compare_digest(b"", expected)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    token = authorization.removeprefix("Bearer ").encode()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

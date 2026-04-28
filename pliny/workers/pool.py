import asyncio
import contextlib
from typing import Literal

import psycopg
from psycopg import sql
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pliny.config import get_settings
from pliny.logging import get_logger
from pliny.storage.blob import BlobStore
from pliny.workers.runner import run_one_job
from pliny.workers.sweeper import sweeper_loop

_LOGGER = get_logger("pliny.workers.pool")

PoolName = Literal["fast", "slow"]


def _to_psycopg_dsn(database_url: str) -> str:
    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


class WorkerPool:
    def __init__(
        self,
        *,
        pool_name: PoolName,
        concurrency: int,
        blob: BlobStore,
        llm: object | None = None,
        neo4j: object | None = None,
        snapshotter: object | None = None,
    ) -> None:
        self.pool_name = pool_name
        self.concurrency = concurrency
        self.blob = blob
        self.llm = llm
        self.neo4j = neo4j
        self.snapshotter = snapshotter
        self._settings = get_settings()
        self._engine = create_async_engine(self._settings.database_url, future=True)
        self._sm = async_sessionmaker(self._engine, expire_on_commit=False)
        self._notify_event = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._channel = f"job_pool_{pool_name}"
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        for i in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._slot_loop(i), name=f"slot-{i}"))
        self._tasks.append(asyncio.create_task(self._listener_loop(), name="listener"))
        self._tasks.append(
            asyncio.create_task(
                sweeper_loop(
                    self._sm,
                    stage_timeout_seconds=self._settings.stage_timeout_seconds,
                    shutdown=self._shutdown,
                ),
                name="sweeper",
            )
        )

    async def shutdown(self) -> None:
        self._shutdown.set()
        self._notify_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._engine.dispose()

    async def _slot_loop(self, index: int) -> None:
        log = _LOGGER
        while not self._shutdown.is_set():
            try:
                processed = await run_one_job(
                    sm=self._sm,
                    pool_name=self.pool_name,
                    blob=self.blob,
                    llm=self.llm,
                    neo4j=self.neo4j,
                    snapshotter=self.snapshotter,
                )
            except Exception as exc:
                log.warning("slot_error", extra={"slot": index, "error": repr(exc)})
                await asyncio.sleep(1)
                continue
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._notify_event.wait(), timeout=2.0)
                self._notify_event.clear()

    async def _listener_loop(self) -> None:
        dsn = _to_psycopg_dsn(self._settings.database_url)
        while not self._shutdown.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
                    async for _ in conn.notifies():
                        self._notify_event.set()
                        if self._shutdown.is_set():
                            break
            except Exception as exc:
                _LOGGER.warning("listener_error", extra={"error": repr(exc)})
                await asyncio.sleep(1)

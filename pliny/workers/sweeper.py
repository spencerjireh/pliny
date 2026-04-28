import asyncio
import contextlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pliny.logging import get_logger

_LOGGER = get_logger("pliny.workers.sweeper")


async def sweep_once(session: AsyncSession, *, stage_timeout_seconds: int) -> int:
    """Reset jobs whose worker died mid-stage. Returns rows reset."""
    result = await session.execute(
        text(
            """
            UPDATE processing_jobs
               SET status='pending', claim_token=NULL, next_attempt_at=now()
             WHERE status='running'
               AND started_at < now() - make_interval(secs => :secs)
            """
        ),
        {"secs": stage_timeout_seconds},
    )
    await session.commit()
    return result.rowcount  # type: ignore[no-any-return,attr-defined]


async def sweeper_loop(
    sm: async_sessionmaker[AsyncSession],
    *,
    stage_timeout_seconds: int,
    interval_seconds: float = 60.0,
    shutdown: asyncio.Event,
) -> None:
    while not shutdown.is_set():
        try:
            async with sm() as session:
                reset = await sweep_once(session, stage_timeout_seconds=stage_timeout_seconds)
                if reset:
                    _LOGGER.info("sweeper_reset", extra={"reset_count": reset})
        except Exception as exc:
            _LOGGER.warning("sweeper_error", extra={"error": repr(exc)})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)

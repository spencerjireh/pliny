import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from pliny.api.routes import admin, health, items, metrics, search
from pliny.bot.runner import SHUTDOWN_GRACE_S
from pliny.config import get_settings
from pliny.logging import configure_logging, get_logger
from pliny.workers.pool import WorkerPool


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    log = get_logger("pliny.api")
    bot_stop = asyncio.Event()
    bot_task: asyncio.Task[None] | None = None
    worker_pool: WorkerPool | None = None

    if settings.embed_fast_worker:
        from pliny.api import deps
        from pliny.pipeline import import_stages

        import_stages()
        worker_pool = WorkerPool(
            pool_name="fast",
            concurrency=settings.fast_worker_concurrency,
            blob=deps.get_blob(),
            llm=deps.get_llm(),
            neo4j=deps.get_neo4j_driver(),
            snapshotter=None,
        )
        await worker_pool.start()
        log.info(
            "embedded_worker_started",
            extra={"pool": "fast", "concurrency": settings.fast_worker_concurrency},
        )

    if settings.embed_bot and settings.telegram_bot_token:
        from pliny.bot.config import load_allowed_user_ids
        from pliny.bot.runner import run_bot

        allowed = load_allowed_user_ids(
            settings.telegram_allowed_user_ids,
            log,
            event="embedded_bot_no_allowed_user_ids; bot will drop every message",
        )
        # Loopback HTTP keeps the bot a pure API client even when colocated.
        bot_task = asyncio.create_task(
            run_bot(
                bot_token=settings.telegram_bot_token,
                pliny_base_url=settings.pliny_api_base_url,
                pliny_api_key=settings.api_key,
                allowed_user_ids=allowed,
                stop=bot_stop,
                install_signal_handlers=False,
            )
        )
        log.info("embedded_bot_started")

    try:
        yield
    finally:
        bot_stop.set()
        if bot_task is not None:
            try:
                await asyncio.wait_for(bot_task, timeout=SHUTDOWN_GRACE_S + 5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                log.warning("embedded_bot_shutdown_error", extra={"error": repr(exc)})
        if worker_pool is not None:
            await worker_pool.shutdown()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Pliny", version="0.1.0", lifespan=_lifespan)

    settings = get_settings()
    if settings.cors_allowed_origins:
        origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=False,
        )

    app.include_router(health.router, tags=["health"])
    app.include_router(metrics.router, tags=["metrics"])

    v1 = APIRouter(prefix="/v1")
    v1.include_router(items.router, prefix="/items", tags=["items"])
    v1.include_router(search.router, prefix="/search", tags=["search"])
    v1.include_router(admin.router, prefix="/admin", tags=["admin"])
    app.include_router(v1)

    return app

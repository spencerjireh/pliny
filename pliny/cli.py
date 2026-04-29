import argparse
import asyncio
import signal
import sys
from typing import Literal

from pliny.config import get_settings
from pliny.logging import configure_logging, get_logger


def _run_api() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "pliny.api.app:create_app",
        host=settings.api_bind_host,
        port=settings.api_bind_port,
        factory=True,
    )


def _import_stages() -> None:
    """Side-effect import: registers every pipeline stage handler."""
    import pliny.pipeline.chunk  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.embed  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.entities  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.extract  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.graph_sync  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.snapshot  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.summarize  # pyright: ignore[reportUnusedImport]
    import pliny.pipeline.wayback_fallback  # noqa: F401  # pyright: ignore[reportUnusedImport]


async def _run_worker_async(pool: Literal["fast", "slow"]) -> None:
    configure_logging()
    _import_stages()
    log = get_logger("pliny.cli")
    settings = get_settings()
    from pliny.api import deps
    from pliny.workers.pool import WorkerPool

    concurrency = (
        settings.fast_worker_concurrency if pool == "fast" else settings.slow_worker_concurrency
    )
    worker = WorkerPool(
        pool_name=pool,
        concurrency=concurrency,
        blob=deps.get_blob(),
        llm=deps.get_llm(),
        neo4j=deps.get_neo4j_driver(),
        snapshotter=deps.get_snapshotter() if pool == "slow" else None,
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal() -> None:
        log.info("worker_shutdown_requested")
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await worker.start()
    log.info("worker_started", extra={"pool": pool, "concurrency": concurrency})
    await stop.wait()
    await worker.shutdown()
    log.info("worker_stopped")


def _run_worker(pool: str) -> None:
    if pool not in ("fast", "slow"):
        raise SystemExit(f"unknown pool: {pool}")
    asyncio.run(_run_worker_async(pool))  # type: ignore[arg-type]


def _run_bot() -> None:
    configure_logging()
    log = get_logger("pliny.cli")
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    from pliny.bot.config import parse_allowed_user_ids
    from pliny.bot.runner import run_bot

    allowed = parse_allowed_user_ids(settings.telegram_allowed_user_ids)
    if not allowed:
        log.warning("no_allowed_user_ids; bot will drop every message")

    asyncio.run(
        run_bot(
            bot_token=settings.telegram_bot_token,
            pliny_base_url=settings.pliny_api_base_url,
            pliny_api_key=settings.api_key,
            allowed_user_ids=allowed,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pliny")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("api", help="Run the FastAPI server")

    worker = sub.add_parser("worker", help="Run a worker pool")
    worker.add_argument("--pool", choices=["fast", "slow"], required=True)

    sub.add_parser("bot", help="Run the Telegram bot")

    args = parser.parse_args(argv)

    if args.cmd == "api":
        _run_api()
    elif args.cmd == "worker":
        _run_worker(args.pool)
    elif args.cmd == "bot":
        _run_bot()
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

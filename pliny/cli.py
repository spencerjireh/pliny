import argparse
import sys

from pliny.config import get_settings


def _run_api() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "pliny.api.app:create_app",
        host=settings.api_bind_host,
        port=settings.api_bind_port,
        factory=True,
    )


def _run_worker(pool: str) -> None:
    raise NotImplementedError("worker entrypoint ships in chunk 7")


def _run_bot() -> None:
    raise NotImplementedError("bot ships with build-order step 11")


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

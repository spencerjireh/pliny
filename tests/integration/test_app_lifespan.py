import asyncio
import os
from typing import Any

import pytest

from pliny.api import deps
from pliny.config import get_settings


class _ExplodingPool:
    def __init__(self, **kwargs: Any) -> None:
        raise AssertionError(f"WorkerPool constructed unexpectedly: {kwargs}")


_pool_instances: list["_RecordingPool"] = []


class _RecordingPool:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.shutdown_called = False
        _pool_instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_called = True


def _override_env(**values: str) -> dict[str, str | None]:
    prev: dict[str, str | None] = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        os.environ[k] = v
    get_settings.cache_clear()
    deps.reset_state()
    return prev


def _restore_env(prev: dict[str, str | None]) -> None:
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    deps.reset_state()


async def test_lifespan_skips_embedding_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev = _override_env(EMBED_FAST_WORKER="false", EMBED_BOT="false")
    try:
        bot_called: list[bool] = []

        async def _bot_should_not_run(**kwargs: Any) -> None:
            bot_called.append(True)

        monkeypatch.setattr("pliny.api.app.WorkerPool", _ExplodingPool)
        monkeypatch.setattr("pliny.bot.runner.run_bot", _bot_should_not_run)

        from pliny.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            pass

        assert bot_called == []
    finally:
        _restore_env(prev)


async def test_lifespan_starts_fast_worker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev = _override_env(EMBED_FAST_WORKER="true", EMBED_BOT="false")
    try:
        _pool_instances.clear()
        monkeypatch.setattr("pliny.api.app.WorkerPool", _RecordingPool)

        from pliny.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            assert len(_pool_instances) == 1
            pool = _pool_instances[0]
            assert pool.started
            assert pool.kwargs["pool_name"] == "fast"
            assert pool.kwargs["snapshotter"] is None
            assert not pool.shutdown_called

        assert _pool_instances[0].shutdown_called
    finally:
        _restore_env(prev)


async def test_lifespan_skips_bot_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev = _override_env(
        EMBED_FAST_WORKER="false",
        EMBED_BOT="true",
        TELEGRAM_BOT_TOKEN="",
    )
    try:
        bot_called: list[bool] = []

        async def _bot_should_not_run(**kwargs: Any) -> None:
            bot_called.append(True)

        monkeypatch.setattr("pliny.bot.runner.run_bot", _bot_should_not_run)

        from pliny.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            pass

        assert bot_called == []
    finally:
        _restore_env(prev)


async def test_lifespan_runs_bot_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev = _override_env(
        EMBED_FAST_WORKER="false",
        EMBED_BOT="true",
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_ALLOWED_USER_IDS="123",
    )
    try:
        bot_kwargs: dict[str, Any] = {}
        bot_started = asyncio.Event()
        bot_finished = asyncio.Event()

        async def _fake_run_bot(**kwargs: Any) -> None:
            bot_kwargs.update(kwargs)
            bot_started.set()
            stop: asyncio.Event = kwargs["stop"]
            await stop.wait()
            bot_finished.set()

        monkeypatch.setattr("pliny.bot.runner.run_bot", _fake_run_bot)

        from pliny.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(bot_started.wait(), timeout=2.0)
            assert bot_kwargs["bot_token"] == "test-token"
            assert bot_kwargs["install_signal_handlers"] is False
            assert bot_kwargs["allowed_user_ids"] == frozenset({123})
            assert not bot_finished.is_set()

        assert bot_finished.is_set()
    finally:
        _restore_env(prev)

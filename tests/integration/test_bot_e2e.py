import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.chunk  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.embed  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.entities  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.extract  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.graph_sync  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.snapshot  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.summarize  # pyright: ignore[reportUnusedImport]  # noqa: F401
from pliny.bot.pliny_client import PlinyClient
from pliny.bot.runner import run_bot
from pliny.bot.telegram_api import TelegramClient
from pliny.workers.pool import WorkerPool

BOT_TOKEN = "TESTBOT"
ALLOWED_USER = 12345


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, "
            "embeddings_1536, chunks, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture
async def fast_pool(fake_llm, neo4j_driver: Any) -> AsyncIterator[WorkerPool]:  # type: ignore[no-untyped-def]
    from pliny.api import deps

    pool = WorkerPool(
        pool_name="fast",
        concurrency=2,
        blob=deps.get_blob(),
        llm=fake_llm,
        neo4j=neo4j_driver,
    )
    await pool.start()
    try:
        yield pool
    finally:
        await pool.shutdown()


def _telegram_message(
    text_value: str, *, message_id: int = 1, user_id: int = ALLOWED_USER
) -> dict[str, Any]:
    return {
        "update_id": message_id * 100,
        "message": {
            "message_id": message_id,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False},
            "text": text_value,
        },
    }


async def _wait_for_edit(edits: list[dict[str, Any]], timeout_s: float = 15.0) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if edits:
            return edits[0]
        await asyncio.sleep(0.1)
    raise AssertionError("timed out waiting for editMessageText")


@respx.mock
async def test_bot_text_message_round_trip(
    app: FastAPI,
    db_session: AsyncSession,
    fast_pool: WorkerPool,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)

    update_batches: list[list[dict[str, Any]]] = [[_telegram_message("hello world")], []]
    sent_messages: list[dict[str, Any]] = []
    edits: list[dict[str, Any]] = []
    next_message_id = 1000

    def get_updates_handler(request: httpx.Request) -> httpx.Response:
        batch = update_batches.pop(0) if update_batches else []
        return httpx.Response(200, json={"ok": True, "result": batch})

    def send_message_handler(request: httpx.Request) -> httpx.Response:
        nonlocal next_message_id
        body = request.read()
        sent_messages.append({"raw": body.decode()})
        mid = next_message_id
        next_message_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": mid}})

    def edit_message_handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        edits.append({"raw": body})
        return httpx.Response(200, json={"ok": True, "result": True})

    respx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates").mock(
        side_effect=get_updates_handler
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
        side_effect=send_message_handler
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText").mock(
        side_effect=edit_message_handler
    )

    transport = ASGITransport(app=app)
    pliny_http = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
        timeout=30.0,
    )
    telegram_http = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))

    pliny = PlinyClient(base_url="http://test", api_key="test-key", client=pliny_http)
    telegram = TelegramClient(token=BOT_TOKEN, client=telegram_http)
    stop = asyncio.Event()

    bot_task = asyncio.create_task(
        run_bot(
            bot_token=BOT_TOKEN,
            pliny_base_url="http://test",
            pliny_api_key="test-key",
            allowed_user_ids=frozenset({ALLOWED_USER}),
            telegram=telegram,
            pliny=pliny,
            stop=stop,
            install_signal_handlers=False,
        )
    )

    try:
        edit = await _wait_for_edit(edits)
        assert "Test Item" in edit["raw"]
        assert "Test summary." in edit["raw"]
        assert sent_messages, "expected an ack send_message call"
        assert "Captured." in sent_messages[0]["raw"]
    finally:
        stop.set()
        await bot_task
        await pliny_http.aclose()
        await telegram_http.aclose()


@respx.mock
async def test_bot_drops_unauthorized_user(
    app: FastAPI,
    db_session: AsyncSession,
) -> None:
    await _truncate(db_session)

    intruder = _telegram_message("malicious", message_id=2, user_id=99999)
    update_batches: list[list[dict[str, Any]]] = [[intruder], []]
    sent_messages: list[dict[str, Any]] = []

    def get_updates_handler(request: httpx.Request) -> httpx.Response:
        batch = update_batches.pop(0) if update_batches else []
        return httpx.Response(200, json={"ok": True, "result": batch})

    def send_message_handler(request: httpx.Request) -> httpx.Response:
        sent_messages.append({})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    respx.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates").mock(
        side_effect=get_updates_handler
    )
    respx.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage").mock(
        side_effect=send_message_handler
    )

    transport = ASGITransport(app=app)
    pliny_http = httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-key"},
        timeout=30.0,
    )
    telegram_http = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))

    pliny = PlinyClient(base_url="http://test", api_key="test-key", client=pliny_http)
    telegram = TelegramClient(token=BOT_TOKEN, client=telegram_http)
    stop = asyncio.Event()

    bot_task = asyncio.create_task(
        run_bot(
            bot_token=BOT_TOKEN,
            pliny_base_url="http://test",
            pliny_api_key="test-key",
            allowed_user_ids=frozenset({ALLOWED_USER}),
            telegram=telegram,
            pliny=pliny,
            stop=stop,
            install_signal_handlers=False,
        )
    )

    try:
        # give the loop a chance to consume both batches and observe the drop
        await asyncio.sleep(0.5)
        # No items were ingested for the intruder.
        count = (await db_session.execute(text("SELECT count(*)::int FROM items"))).scalar_one()
        assert count == 0
        # And no message was sent in response.
        assert sent_messages == []
    finally:
        stop.set()
        await bot_task
        await pliny_http.aclose()
        await telegram_http.aclose()

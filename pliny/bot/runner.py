import asyncio
import contextlib
import signal
from typing import Any

from pliny.bot.dispatcher import dispatch_message
from pliny.bot.pliny_client import PlinyClient
from pliny.bot.poller import wait_and_render_message
from pliny.bot.telegram_api import TelegramClient
from pliny.logging import get_logger

_GET_UPDATES_BACKOFF_S = 2.0
SHUTDOWN_GRACE_S = 10.0
# Tiny yield between empty get_updates calls. In production the long-poll
# already blocks for ~30s so this is invisible; in tests with mocked instant
# responses it prevents the loop from starving worker tasks.
_EMPTY_POLL_YIELD_S = 0.05


async def run_bot(
    *,
    bot_token: str,
    pliny_base_url: str,
    pliny_api_key: str,
    allowed_user_ids: frozenset[int],
    telegram: TelegramClient | None = None,
    pliny: PlinyClient | None = None,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    log = get_logger("pliny.bot")
    tg = telegram or TelegramClient(token=bot_token)
    pl = pliny or PlinyClient(base_url=pliny_base_url, api_key=pliny_api_key)
    stop_event = stop or asyncio.Event()
    pollers: set[asyncio.Task[None]] = set()

    if install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

    try:
        offset = 0
        while not stop_event.is_set():
            try:
                updates = await tg.get_updates(offset=offset, timeout_s=30)
            except Exception as exc:
                log.warning("get_updates_error", extra={"error": repr(exc)})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=_GET_UPDATES_BACKOFF_S)
                continue

            for update in updates:
                offset = update.update_id + 1
                if update.message is None:
                    continue
                from_id = (update.message.get("from") or {}).get("id")
                if from_id not in allowed_user_ids:
                    log.info("dropped_unauthorized", extra={"from_id": from_id})
                    continue

                task = asyncio.create_task(
                    _process_message(update.message, telegram=tg, pliny=pl, stop=stop_event)
                )
                pollers.add(task)
                task.add_done_callback(pollers.discard)

            if not updates:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=_EMPTY_POLL_YIELD_S)
    finally:
        log.info("bot_shutdown_requested")
        if pollers:
            await asyncio.wait(pollers, timeout=SHUTDOWN_GRACE_S)
            for t in pollers:
                if not t.done():
                    t.cancel()
        await tg.aclose()
        await pl.aclose()
        log.info("bot_stopped")


async def _process_message(
    msg: dict[str, Any],
    *,
    telegram: TelegramClient,
    pliny: PlinyClient,
    stop: asyncio.Event,
) -> None:
    log = get_logger("pliny.bot")
    chat_id = msg["chat"]["id"]
    src_message_id = msg["message_id"]
    try:
        result = await dispatch_message(msg, telegram=telegram, pliny=pliny)
    except Exception as exc:
        log.warning("dispatch_error", extra={"error": repr(exc)})
        with contextlib.suppress(Exception):
            await telegram.send_message(chat_id, f"Capture failed: {exc}", reply_to=src_message_id)
        return

    if not result.items:
        if result.note:
            with contextlib.suppress(Exception):
                await telegram.send_message(
                    chat_id, f"Skipped: {result.note}", reply_to=src_message_id
                )
        return

    item_ids = [str(i["item_id"]) for i in result.items]
    ack = "Captured. " + ", ".join(item_ids[:3])
    if len(item_ids) > 3:
        ack += f" +{len(item_ids) - 3} more"
    try:
        bot_message_id = await telegram.send_message(chat_id, ack, reply_to=src_message_id)
    except Exception as exc:
        log.warning("ack_send_failed", extra={"error": repr(exc)})
        return

    await wait_and_render_message(
        chat_id=chat_id,
        bot_message_id=bot_message_id,
        item_ids=item_ids,
        pliny=pliny,
        telegram=telegram,
        stop=stop,
    )

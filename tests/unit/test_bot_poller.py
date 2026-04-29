import asyncio
from typing import Any

import pytest

from pliny.bot.poller import wait_and_render_message


class StubPliny:
    def __init__(self) -> None:
        self.status_responses: dict[str, list[dict[str, Any]]] = {}
        self.item_responses: dict[str, dict[str, Any]] = {}
        self.status_calls: list[str] = []
        self.item_calls: list[str] = []

    def queue_status(self, item_id: str, sequence: list[dict[str, Any]]) -> None:
        self.status_responses[item_id] = list(sequence)

    def set_item(self, item_id: str, item: dict[str, Any]) -> None:
        self.item_responses[item_id] = item

    async def get_status(self, item_id: str) -> dict[str, Any]:
        self.status_calls.append(item_id)
        seq = self.status_responses.get(item_id, [])
        if len(seq) == 1:
            return seq[0]
        if seq:
            return seq.pop(0)
        return {"id": item_id, "stages": {"summarize": {"status": "pending"}}, "overall": "pending"}

    async def get_item(self, item_id: str) -> dict[str, Any]:
        self.item_calls.append(item_id)
        return self.item_responses.get(item_id, {})


class StubTelegram:
    def __init__(self) -> None:
        self.edits: list[dict[str, Any]] = []

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})


def _stages_with_summarize(status: str, **other: str) -> dict[str, Any]:
    s: dict[str, Any] = {"summarize": {"status": status}}
    for k, v in other.items():
        s[k] = {"status": v}
    return s


@pytest.fixture
def telegram() -> StubTelegram:
    return StubTelegram()


@pytest.fixture
def pliny() -> StubPliny:
    return StubPliny()


async def test_single_item_ready(telegram: StubTelegram, pliny: StubPliny) -> None:
    pliny.queue_status(
        "i1",
        [
            {"id": "i1", "stages": _stages_with_summarize("pending"), "overall": "processing"},
            {"id": "i1", "stages": _stages_with_summarize("done"), "overall": "ready"},
        ],
    )
    pliny.set_item("i1", {"id": "i1", "title": "Hello", "summary": "World"})

    await wait_and_render_message(
        chat_id=1,
        bot_message_id=10,
        item_ids=["i1"],
        pliny=pliny,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        stop=asyncio.Event(),
        poll_interval_s=0.0,
    )

    assert telegram.edits == [{"chat_id": 1, "message_id": 10, "text": "Hello\n\nWorld"}]
    assert pliny.item_calls == ["i1"]


async def test_overall_failed_terminates(telegram: StubTelegram, pliny: StubPliny) -> None:
    pliny.queue_status(
        "i1",
        [
            {
                "id": "i1",
                "stages": _stages_with_summarize("pending", extract="failed"),
                "overall": "failed",
            }
        ],
    )
    pliny.set_item("i1", {"id": "i1"})
    # Mark the failed stage with an error string so the renderer surfaces it.
    pliny.status_responses["i1"][0]["stages"]["extract"]["error"] = "no_handler"

    await wait_and_render_message(
        chat_id=1,
        bot_message_id=10,
        item_ids=["i1"],
        pliny=pliny,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        stop=asyncio.Event(),
        poll_interval_s=0.0,
    )

    assert telegram.edits[0]["text"] == "Capture failed: extract: no_handler"
    assert pliny.item_calls == []  # never fetched the item


async def test_redirected_follows_survivor(telegram: StubTelegram, pliny: StubPliny) -> None:
    pliny.status_responses["i1"] = [{"redirect_to": "i2"}]
    pliny.queue_status(
        "i2",
        [{"id": "i2", "stages": _stages_with_summarize("done"), "overall": "ready"}],
    )
    pliny.set_item("i2", {"id": "i2", "title": "Survivor", "summary": "Existed"})

    await wait_and_render_message(
        chat_id=1,
        bot_message_id=10,
        item_ids=["i1"],
        pliny=pliny,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        stop=asyncio.Event(),
        poll_interval_s=0.0,
    )

    text = telegram.edits[0]["text"]
    assert text.startswith("Captured (already had this).")
    assert "Survivor" in text
    assert "Existed" in text


async def test_multi_item_summary(telegram: StubTelegram, pliny: StubPliny) -> None:
    pliny.queue_status(
        "i1",
        [{"id": "i1", "stages": _stages_with_summarize("done"), "overall": "ready"}],
    )
    pliny.set_item("i1", {"title": "First", "summary": "one"})
    pliny.queue_status(
        "i2",
        [{"id": "i2", "stages": _stages_with_summarize("done"), "overall": "ready"}],
    )
    pliny.set_item("i2", {"title": "Second", "summary": "two"})

    await wait_and_render_message(
        chat_id=1,
        bot_message_id=10,
        item_ids=["i1", "i2"],
        pliny=pliny,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        stop=asyncio.Event(),
        poll_interval_s=0.0,
    )

    text = telegram.edits[0]["text"]
    assert text.startswith("Captured 2 items.")
    assert "• First: one" in text
    assert "• Second: two" in text


async def test_stop_event_short_circuits(telegram: StubTelegram, pliny: StubPliny) -> None:
    pliny.queue_status(
        "i1",
        [{"id": "i1", "stages": _stages_with_summarize("running"), "overall": "processing"}],
    )

    stop = asyncio.Event()
    stop.set()

    await wait_and_render_message(
        chat_id=1,
        bot_message_id=10,
        item_ids=["i1"],
        pliny=pliny,  # type: ignore[arg-type]
        telegram=telegram,  # type: ignore[arg-type]
        stop=stop,
        poll_interval_s=0.0,
    )

    # We should still attempt to edit even when stopped early; body is "(still processing)".
    assert telegram.edits
    assert "still processing" in telegram.edits[0]["text"]

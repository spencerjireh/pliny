from typing import Any

import pytest

from pliny.bot.dispatcher import dispatch_message


class StubTelegram:
    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.downloads: list[str] = []
        self.payloads = payloads or {}

    async def download_file(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self.payloads.get(file_id, b"\x00")


class StubPliny:
    def __init__(self) -> None:
        self.json_calls: list[dict[str, Any]] = []
        self.file_calls: list[dict[str, Any]] = []
        self.next_items: list[dict[str, Any]] = [
            {"item_id": "i1", "type": "x", "deduplicated": False}
        ]

    async def ingest_json(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.json_calls.append(kwargs)
        return self.next_items

    async def ingest_file(
        self, body: bytes, mime: str | None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.file_calls.append({"body": body, "mime": mime, **kwargs})
        return self.next_items


@pytest.fixture
def telegram() -> StubTelegram:
    return StubTelegram()


@pytest.fixture
def pliny() -> StubPliny:
    return StubPliny()


def _msg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "chat": {"id": 100},
        "message_id": 7,
        "from": {"id": 5000},
    }
    base.update(overrides)
    return base


async def test_text_message(telegram: StubTelegram, pliny: StubPliny) -> None:
    result = await dispatch_message(
        _msg(text="hello world"),
        telegram=telegram,
        pliny=pliny,  # type: ignore[arg-type]
    )

    assert result.note is None
    assert result.items
    assert pliny.json_calls == [
        {
            "text": "hello world",
            "source_ref": "tg:100:7",
            "metadata": {"telegram": {"chat_id": 100, "message_id": 7, "from_user_id": 5000}},
        }
    ]
    assert pliny.file_calls == []


async def test_caption_used_when_no_text(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(caption="caption only")
    result = await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert result.items
    assert pliny.json_calls[0]["text"] == "caption only"


async def test_photo_picks_largest(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(
        photo=[
            {"file_id": "small", "file_size": 100},
            {"file_id": "big", "file_size": 9999},
            {"file_id": "med", "file_size": 500},
        ]
    )
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert telegram.downloads == ["big"]
    assert pliny.file_calls[0]["mime"] == "image/jpeg"
    assert pliny.file_calls[0]["filename"] == "photo.jpg"
    assert pliny.file_calls[0]["source_ref"] == "tg:100:7"


async def test_document_uses_mime_and_filename(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(
        document={
            "file_id": "doc1",
            "mime_type": "application/pdf",
            "file_name": "paper.pdf",
        }
    )
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert pliny.file_calls[0]["mime"] == "application/pdf"
    assert pliny.file_calls[0]["filename"] == "paper.pdf"


async def test_voice_defaults_to_audio_ogg(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(voice={"file_id": "v1"})
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert pliny.file_calls[0]["mime"] == "audio/ogg"


async def test_audio_uses_mime_when_present(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(audio={"file_id": "a1", "mime_type": "audio/mpeg"})
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert pliny.file_calls[0]["mime"] == "audio/mpeg"


async def test_video_dispatched(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(video={"file_id": "vid", "mime_type": "video/mp4"})
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert pliny.file_calls[0]["mime"] == "video/mp4"


async def test_forwarded_metadata(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(
        text="forwarded",
        forward_from={"id": 999, "username": "alice"},
        forward_from_chat={"id": -1001, "title": "Channel"},
        forward_date=1700000000,
    )
    await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    md = pliny.json_calls[0]["metadata"]
    assert md["forwarded_from"]["sender"] == {"id": 999, "username": "alice"}
    assert md["forwarded_from"]["original_chat"]["title"] == "Channel"
    assert md["forwarded_from"]["forwarded_at"] == 1700000000


async def test_unsupported_returns_note(telegram: StubTelegram, pliny: StubPliny) -> None:
    msg = _msg(sticker={"file_id": "stk"})
    result = await dispatch_message(msg, telegram=telegram, pliny=pliny)  # type: ignore[arg-type]
    assert result.items == []
    assert result.note == "unsupported message type"
    assert pliny.file_calls == []
    assert pliny.json_calls == []

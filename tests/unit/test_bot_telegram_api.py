import httpx
import pytest
import respx

from pliny.bot.telegram_api import TelegramAPIError, TelegramClient


@pytest.fixture
def token() -> str:
    return "TEST_TOKEN"


@respx.mock
async def test_get_updates_parses_messages(token: str) -> None:
    route = respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 100, "message": {"chat": {"id": 1}, "message_id": 5}},
                    {"update_id": 101, "edited_message": {"chat": {"id": 1}}},
                ],
            },
        )
    )
    client = TelegramClient(token=token)
    updates = await client.get_updates(offset=0, timeout_s=1)
    await client.aclose()

    assert route.called
    assert len(updates) == 2
    assert updates[0].update_id == 100
    assert updates[0].message == {"chat": {"id": 1}, "message_id": 5}
    assert updates[1].update_id == 101
    assert updates[1].message is None


@respx.mock
async def test_get_updates_raises_on_not_ok(token: str) -> None:
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
    )
    client = TelegramClient(token=token)
    with pytest.raises(TelegramAPIError, match="Unauthorized"):
        await client.get_updates(offset=0, timeout_s=1)
    await client.aclose()


@respx.mock
async def test_send_message_returns_message_id(token: str) -> None:
    route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    client = TelegramClient(token=token)
    mid = await client.send_message(chat_id=99, text="hi", reply_to=7)
    await client.aclose()

    assert mid == 42
    sent = route.calls.last.request
    body = sent.read().decode()
    assert '"chat_id":99' in body
    assert '"reply_to_message_id":7' in body


@respx.mock
async def test_edit_message_text(token: str) -> None:
    route = respx.post(f"https://api.telegram.org/bot{token}/editMessageText").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    client = TelegramClient(token=token)
    await client.edit_message_text(chat_id=99, message_id=42, text="updated")
    await client.aclose()

    assert route.called


@respx.mock
async def test_download_file(token: str) -> None:
    respx.get(f"https://api.telegram.org/bot{token}/getFile").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"file_path": "photos/file_1.jpg"}}
        )
    )
    respx.get(f"https://api.telegram.org/file/bot{token}/photos/file_1.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff")
    )
    client = TelegramClient(token=token)
    body = await client.download_file("ABCD")
    await client.aclose()

    assert body == b"\xff\xd8\xff"

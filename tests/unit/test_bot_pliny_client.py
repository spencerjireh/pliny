import json

import httpx
import pytest
import respx

from pliny.bot.pliny_client import PlinyClient


@pytest.fixture
def base_url() -> str:
    return "http://pliny.test"


@pytest.fixture
def api_key() -> str:
    return "k"


@respx.mock
async def test_ingest_json_text(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/v1/items").mock(
        return_value=httpx.Response(202, json={"items": [{"item_id": "abc", "type": "text"}]})
    )
    client = PlinyClient(base_url=base_url, api_key=api_key)
    items = await client.ingest_json(text="hello", source_ref="r1", metadata={"k": "v"})
    await client.aclose()

    assert route.called
    sent = json.loads(route.calls.last.request.read())
    assert sent == {
        "source": "telegram",
        "source_ref": "r1",
        "text": "hello",
        "metadata": {"k": "v"},
    }
    assert items == [{"item_id": "abc", "type": "text"}]


@respx.mock
async def test_ingest_json_url(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/v1/items").mock(
        return_value=httpx.Response(202, json={"items": [{"item_id": "u1", "type": "url"}]})
    )
    client = PlinyClient(base_url=base_url, api_key=api_key)
    await client.ingest_json(url="https://example.com", source_ref="r2")
    await client.aclose()

    sent = json.loads(route.calls.last.request.read())
    assert sent == {"source": "telegram", "source_ref": "r2", "url": "https://example.com"}


@respx.mock
async def test_ingest_file(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/v1/items").mock(
        return_value=httpx.Response(202, json={"items": [{"item_id": "f1", "type": "image"}]})
    )
    client = PlinyClient(base_url=base_url, api_key=api_key)
    items = await client.ingest_file(
        b"\xff\xd8", "image/jpeg", filename="photo.jpg", source_ref="r3"
    )
    await client.aclose()

    assert route.called
    body = route.calls.last.request.read()
    assert b"image/jpeg" in body
    assert b"photo.jpg" in body
    assert b'name="source"' in body
    assert b"telegram" in body
    assert items == [{"item_id": "f1", "type": "image"}]


@respx.mock
async def test_get_status_and_get_item(base_url: str, api_key: str) -> None:
    respx.get(f"{base_url}/v1/items/x/status").mock(
        return_value=httpx.Response(200, json={"id": "x", "stages": {}, "overall": "ready"})
    )
    respx.get(f"{base_url}/v1/items/x").mock(
        return_value=httpx.Response(200, json={"id": "x", "title": "T", "summary": "S"})
    )
    client = PlinyClient(base_url=base_url, api_key=api_key)
    status = await client.get_status("x")
    item = await client.get_item("x")
    await client.aclose()

    assert status["overall"] == "ready"
    assert item == {"id": "x", "title": "T", "summary": "S"}


@respx.mock
async def test_authorization_header_sent(base_url: str, api_key: str) -> None:
    route = respx.get(f"{base_url}/v1/items/x").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    client = PlinyClient(base_url=base_url, api_key=api_key)
    await client.get_item("x")
    await client.aclose()

    assert route.calls.last.request.headers["authorization"] == f"Bearer {api_key}"

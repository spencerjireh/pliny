import hashlib
import json

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.canonicalize import canonicalize
from pliny.db.models import Item, ItemSource, ProcessingJob


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def test_ingest_text_only(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"text": "hello world", "source": "api", "source_ref": "t-1"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 202
    body = r.json()
    assert len(body["items"]) == 1
    item_id = body["items"][0]["item_id"]
    assert body["items"][0]["type"] == "text"
    assert body["items"][0]["deduplicated"] is False

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.type == "text"
    assert item.content_hash == hashlib.sha256(b"hello world").hexdigest()
    assert item.raw_ref == f"raw/{item.content_hash}"

    sources = (
        (await db_session.execute(select(ItemSource).where(ItemSource.item_id == item_id)))
        .scalars()
        .all()
    )
    assert len(sources) == 1
    assert sources[0].source == "api"
    assert sources[0].source_ref == "t-1"

    jobs = (
        (await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == item_id)))
        .scalars()
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].stage == "extract"
    assert jobs[0].pool == "fast"
    assert jobs[0].status == "pending"


async def test_ingest_url_only(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    raw_url = "HTTPS://Example.COM:443/foo/?utm_source=x&b=2&a=1#frag"
    payload = {"url": raw_url, "source": "api", "source_ref": "u-1"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 202
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "url"
    item_id = body["items"][0]["item_id"]

    canonical = canonicalize(raw_url)
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.canonical_url == canonical
    assert item.content_hash == expected_hash
    assert item.raw_ref is None


async def test_url_repeated_appends_source(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    raw_url = "https://example.com/foo"
    p1 = {"url": raw_url, "source": "api", "source_ref": "first"}
    p2 = {"url": raw_url, "source": "api", "source_ref": "second"}
    r1 = await client.post("/v1/items", json=p1, headers=auth_headers)
    r2 = await client.post("/v1/items", json=p2, headers=auth_headers)
    assert r1.status_code == 202
    assert r2.status_code == 202
    id1 = r1.json()["items"][0]["item_id"]
    id2 = r2.json()["items"][0]["item_id"]
    assert id1 == id2
    assert r2.json()["items"][0]["deduplicated"] is True

    sources = (
        (await db_session.execute(select(ItemSource).where(ItemSource.item_id == id1)))
        .scalars()
        .all()
    )
    assert len(sources) == 2

    jobs = (
        (await db_session.execute(select(ProcessingJob).where(ProcessingJob.item_id == id1)))
        .scalars()
        .all()
    )
    assert len(jobs) == 1


async def test_url_idempotent_on_source_ref(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {"url": "https://example.com/x", "source": "api", "source_ref": "same"}
    await client.post("/v1/items", json=payload, headers=auth_headers)
    r2 = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r2.status_code == 202
    item_id = r2.json()["items"][0]["item_id"]
    sources = (
        (await db_session.execute(select(ItemSource).where(ItemSource.item_id == item_id)))
        .scalars()
        .all()
    )
    assert len(sources) == 1


async def test_text_with_url_splits(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    payload = {
        "text": "check this out https://example.com/article and tell me",
        "source": "telegram",
        "source_ref": "msg-1",
    }
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 202
    body = r.json()
    assert len(body["items"]) == 2
    types = sorted(item["type"] for item in body["items"])
    assert types == ["text", "url"]

    ids = [item["item_id"] for item in body["items"]]
    sources = (
        (await db_session.execute(select(ItemSource).where(ItemSource.item_id.in_(ids))))
        .scalars()
        .all()
    )
    assert len(sources) == 2
    assert all(s.source_ref == "msg-1" for s in sources)


async def test_multipart_file_upload(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    file_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    files = {"file": ("x.png", file_bytes, "image/png")}
    data = {"source": "api", "source_ref": "img-1"}
    r = await client.post("/v1/items", files=files, data=data, headers=auth_headers)
    assert r.status_code == 202, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "image"
    item_id = body["items"][0]["item_id"]
    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.content_hash == hashlib.sha256(file_bytes).hexdigest()
    assert item.raw_ref == f"raw/{item.content_hash}"
    assert item.meta is not None
    assert item.meta.get("mime") == "image/png"


async def test_multipart_pdf_metadata(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
) -> None:
    await _truncate(db_session)
    files = {"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")}
    data = {
        "source": "api",
        "source_ref": "pdf-1",
        "metadata": json.dumps({"forwarded_from": {"sender": "alice"}}),
    }
    r = await client.post("/v1/items", files=files, data=data, headers=auth_headers)
    assert r.status_code == 202
    body = r.json()
    assert body["items"][0]["type"] == "pdf"
    item_id = body["items"][0]["item_id"]
    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    assert item.meta is not None
    assert item.meta.get("forwarded_from", {}).get("sender") == "alice"
    assert item.meta.get("mime") == "application/pdf"


async def test_oversize_body_rejected(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    big = "a" * (26 * 1024 * 1024)
    payload = {"text": big, "source": "api", "source_ref": "big"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 413


async def test_missing_auth(client: AsyncClient) -> None:
    payload = {"text": "x", "source": "api", "source_ref": "noauth"}
    r = await client.post("/v1/items", json=payload)
    assert r.status_code == 401


async def test_bad_auth(client: AsyncClient) -> None:
    payload = {"text": "x", "source": "api", "source_ref": "badauth"}
    r = await client.post("/v1/items", json=payload, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


async def test_validation_error_returns_4xx(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    payload = {"source": "api", "source_ref": "empty"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code in (400, 422)


async def test_unsupported_content_type(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    r = await client.post(
        "/v1/items",
        content=b"raw bytes",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert r.status_code == 415

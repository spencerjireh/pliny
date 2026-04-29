import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.chunk  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.embed  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.entities  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.extract  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.graph_sync  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.snapshot  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.summarize  # pyright: ignore[reportUnusedImport]
import pliny.pipeline.wayback_fallback  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.db.models import Content, Item
from pliny.workers.pool import WorkerPool

SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>E2E</title></head><body>
<p>The quick brown fox jumps over the lazy dog. Pliny ingests this article
and extracts the body text via trafilatura.</p>
</body></html>
"""


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_entities, entities, item_tags, tags, items "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


_ENTITIES_RESPONSE = (
    '{"entities":[{"name":"Pliny","type":"work","mention_text":"Pliny","confidence":0.9}]}'
)


def _route_chat(fake_llm: Any) -> Any:
    """Switch the FakeLLM chat response between summarize and entities prompts."""

    def provider(kwargs: dict[str, Any]) -> str:
        msgs = kwargs.get("messages") or []
        sys_msg = next((m for m in msgs if m.get("role") == "system"), {}).get("content", "")
        if "extract named entities" in sys_msg:
            return _ENTITIES_RESPONSE
        return fake_llm.chat_response_text

    return provider


@pytest.fixture
async def fast_pool(fake_llm, neo4j_driver: Any) -> AsyncIterator[WorkerPool]:  # type: ignore[no-untyped-def]
    from pliny.api import deps

    fake_llm.chat_response_provider = _route_chat(fake_llm)
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


@pytest.fixture
async def slow_pool(fake_snapshotter, neo4j_driver: Any) -> AsyncIterator[WorkerPool]:  # type: ignore[no-untyped-def]
    from pliny.api import deps

    pool = WorkerPool(
        pool_name="slow",
        concurrency=1,
        blob=deps.get_blob(),
        llm=None,
        neo4j=neo4j_driver,
        snapshotter=fake_snapshotter,
    )
    await pool.start()
    try:
        yield pool
    finally:
        await pool.shutdown()


async def _wait_for_status(
    client: AsyncClient,
    auth_headers: dict[str, str],
    item_id: str,
    *,
    stage: str,
    target: str,
    timeout_s: float = 10.0,
) -> dict[str, object]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/items/{item_id}/status", headers=auth_headers)
        body = r.json()
        if body.get("stages", {}).get(stage, {}).get("status") == target:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {stage}={target}: last body={body}")


async def test_text_ingest_flows_through_extract(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fast_pool: WorkerPool,
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    payload = {"text": "pliny end to end", "source": "api", "source_ref": "e2e-text"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    assert r.status_code == 202
    item_id = r.json()["items"][0]["item_id"]

    status = await _wait_for_status(client, auth_headers, item_id, stage="extract", target="done")
    assert status["stages"]["extract"]["version"] >= 1  # type: ignore[index]
    await _wait_for_status(client, auth_headers, item_id, stage="summarize", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="chunk", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="embed", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="entities", target="done")
    final = await _wait_for_status(client, auth_headers, item_id, stage="graph_sync", target="done")
    assert final["overall"] == "ready"

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item_id))
    ).scalar_one()
    assert content.extracted_text == "pliny end to end"

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.title == "Test Item"
    assert item.summary == "Test summary."

    chunk_count = (
        await db_session.execute(
            text("SELECT count(*)::int FROM chunks WHERE item_id = :id"),
            {"id": item_id},
        )
    ).scalar_one()
    assert chunk_count >= 1

    embed_count = (
        await db_session.execute(
            text("SELECT count(*)::int FROM embeddings_1536 WHERE item_id = :id"),
            {"id": item_id},
        )
    ).scalar_one()
    assert embed_count >= 1

    item_entity_count = (
        await db_session.execute(
            text("SELECT count(*)::int FROM item_entities WHERE item_id = :id"),
            {"id": item_id},
        )
    ).scalar_one()
    assert item_entity_count >= 1

    async with neo4j_driver.session() as s:
        result = await s.run(
            "MATCH (i:Item {id:$id})-[:MENTIONS]->(e:Entity) RETURN count(e) AS c",
            id=str(item_id),
        )
        record = await result.single()
    assert record is not None
    assert int(record["c"]) >= 1


@respx.mock
async def test_url_ingest_flows_through_snapshot_and_extract(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fast_pool: WorkerPool,
    slow_pool: WorkerPool,
    fake_snapshotter,  # type: ignore[no-untyped-def]
    neo4j_driver: Any,
) -> None:
    await _truncate(db_session)
    canonical = "https://example.com/e2e"
    respx.head(canonical).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    fake_snapshotter.rendered_html = SAMPLE_HTML.encode("utf-8")
    fake_snapshotter.page_title = "E2E Page"

    payload = {"url": canonical, "source": "api", "source_ref": "e2e-url"}
    r = await client.post("/v1/items", json=payload, headers=auth_headers)
    item_id = r.json()["items"][0]["item_id"]

    await _wait_for_status(client, auth_headers, item_id, stage="snapshot", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="extract", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="summarize", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="chunk", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="embed", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="entities", target="done")
    final = await _wait_for_status(client, auth_headers, item_id, stage="graph_sync", target="done")
    assert final["overall"] == "ready"
    # wayback_fallback should not appear in the response when snapshot succeeded.
    assert "wayback_fallback" not in final["stages"]  # type: ignore[operator]

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item_id))
    ).scalar_one()
    assert content.extracted_text is not None
    assert "quick brown fox" in content.extracted_text

    item = (await db_session.execute(select(Item).where(Item.id == item_id))).scalar_one()
    await db_session.refresh(item)
    assert item.raw_ref is not None
    assert item.snapshot_version >= 1
    assert item.meta is not None
    assert item.meta.get("page_title") == "E2E Page"

    from pliny.api import deps

    blob = deps.get_blob()
    assert await blob.exists(f"derived/{item_id}/screenshot.png")
    assert await blob.exists(f"derived/{item_id}/metadata.json")
    assert fake_snapshotter.calls, "expected fake snapshotter to be invoked"


async def test_image_ingest_flows_through_extract(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fast_pool: WorkerPool,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    import io

    from PIL import Image

    await _truncate(db_session)
    img = Image.new("RGB", (16, 16), (10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()

    files = {"file": ("img.png", raw, "image/png")}
    data = {"source": "api", "source_ref": "e2e-img"}
    r = await client.post("/v1/items", files=files, data=data, headers=auth_headers)
    item_id = r.json()["items"][0]["item_id"]

    await _wait_for_status(client, auth_headers, item_id, stage="extract", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="summarize", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="chunk", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="embed", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="entities", target="done")
    await _wait_for_status(client, auth_headers, item_id, stage="graph_sync", target="done")

    content = (
        await db_session.execute(select(Content).where(Content.item_id == item_id))
    ).scalar_one()
    assert content.extracted_text == fake_llm.vision_response_text

    assert any(call.get("model") == "gpt-4o-mini" for call in fake_llm.vision_calls)

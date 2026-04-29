import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api import deps
from pliny.config import get_settings
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.schemas.search import QueryCursor, encode_cursor


async def _truncate(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            "TRUNCATE processing_jobs, item_sources, item_redirects, content, "
            "image_phashes, item_tags, tags, items RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture
def logger() -> logging.LoggerAdapter[logging.Logger]:
    return get_logger("test")


def _one_hot(idx: int, dim: int = 1536) -> list[float]:
    """Distinct unit vectors so cosine ordering is predictable."""
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


async def _seed_item(
    db_session: AsyncSession,
    *,
    type_: str,
    title: str,
    summary: str,
    content_text: str,
    chunks: list[str],
    summary_vector: list[float],
    chunk_vectors: list[list[float]],
    tag_names: list[str] | None = None,
    captured_at: datetime | None = None,
    extra_meta: dict[str, object] | None = None,
) -> uuid.UUID:
    settings = get_settings()
    item = await insert_item(
        db_session,
        type=type_,
        content_hash=uuid.uuid4().hex,
        raw_ref=None,
    )
    await db_session.execute(
        text("UPDATE items SET title=:t, summary=:s WHERE id=:id"),
        {"t": title, "s": summary, "id": item.id},
    )
    if captured_at is not None:
        await db_session.execute(
            text("UPDATE items SET captured_at=:c WHERE id=:id"),
            {"c": captured_at, "id": item.id},
        )
    if extra_meta:
        await db_session.execute(
            text(
                "UPDATE items SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS jsonb) "
                "WHERE id = :id"
            ),
            {"id": item.id, "m": __import__("json").dumps(extra_meta)},
        )
    await db_session.execute(
        text(
            "INSERT INTO content (item_id, extracted_text, extraction_method, extract_version) "
            "VALUES (:id, :t, 'identity', 1)"
        ),
        {"id": item.id, "t": content_text},
    )

    chunk_ids: list[uuid.UUID] = []
    for i, ct in enumerate(chunks):
        cid = uuid.uuid4()
        chunk_ids.append(cid)
        await db_session.execute(
            text(
                "INSERT INTO chunks (id, item_id, chunk_index, text, token_count, chunker_version) "
                "VALUES (:id, :item_id, :idx, :t, :tc, 1)"
            ),
            {"id": cid, "item_id": item.id, "idx": i, "t": ct, "tc": len(ct.split())},
        )

    model = settings.current_embedding_model
    model_version = settings.embedding_model_version
    await db_session.execute(
        text(
            "INSERT INTO embeddings_1536 "
            "(id, item_id, chunk_id, granularity, model_name, model_version, vector) "
            "VALUES (gen_random_uuid(), :item_id, NULL, 'summary', :m, :v, CAST(:vec AS vector))"
        ),
        {"item_id": item.id, "m": model, "v": model_version, "vec": str(summary_vector)},
    )
    for cid, vec in zip(chunk_ids, chunk_vectors, strict=True):
        await db_session.execute(
            text(
                "INSERT INTO embeddings_1536 "
                "(id, item_id, chunk_id, granularity, model_name, model_version, vector) "
                "VALUES (gen_random_uuid(), :item_id, :chunk_id, 'chunk', :m, :v, "
                "CAST(:vec AS vector))"
            ),
            {
                "item_id": item.id,
                "chunk_id": cid,
                "m": model,
                "v": model_version,
                "vec": str(vec),
            },
        )

    if tag_names:
        for name in tag_names:
            tid = uuid.uuid4()
            await db_session.execute(
                text("INSERT INTO tags (id, name) VALUES (:id, :n) ON CONFLICT (name) DO NOTHING"),
                {"id": tid, "n": name},
            )
            await db_session.execute(
                text(
                    "INSERT INTO item_tags (item_id, tag_id) "
                    "SELECT :item_id, id FROM tags WHERE name = :n"
                ),
                {"item_id": item.id, "n": name},
            )

    await db_session.commit()
    return item.id


async def test_search_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/search?q=hello")
    assert r.status_code == 401


async def test_browse_returns_items_in_recency_order(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)
    ids: list[uuid.UUID] = []
    for i in range(3):
        ids.append(
            await _seed_item(
                db_session,
                type_="text",
                title=f"item {i}",
                summary=f"summary {i}",
                content_text=f"content body {i}",
                chunks=[f"chunk a {i}"],
                summary_vector=_one_hot(i),
                chunk_vectors=[_one_hot(i)],
                captured_at=base + timedelta(days=i),
            )
        )

    r = await client.get("/v1/search", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    titles = [item["title"] for item in body["items"]]
    assert titles == ["item 2", "item 1", "item 0"]
    assert body["items"][0].get("score") is None


async def test_browse_cursor_pagination(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)
    for i in range(4):
        await _seed_item(
            db_session,
            type_="text",
            title=f"item {i}",
            summary="s",
            content_text=f"body {i}",
            chunks=[f"chunk {i}"],
            summary_vector=_one_hot(i),
            chunk_vectors=[_one_hot(i)],
            captured_at=base + timedelta(days=i),
        )

    r1 = await client.get("/v1/search?limit=2", headers=auth_headers)
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    r2 = await client.get(f"/v1/search?limit=2&cursor={body1['next_cursor']}", headers=auth_headers)
    body2 = r2.json()
    assert len(body2["items"]) == 2
    titles = [item["title"] for item in body1["items"] + body2["items"]]
    assert titles == ["item 3", "item 2", "item 1", "item 0"]


async def test_query_mode_bm25_hit(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _truncate(db_session)
    monkeypatch.setattr(deps, "_llm", fake_llm)

    target = await _seed_item(
        db_session,
        type_="text",
        title="Astronaut diary",
        summary="A diary of astronaut life on the moon.",
        content_text="The astronaut took moonwalks and recorded findings.",
        chunks=["astronaut moonwalk findings"],
        summary_vector=_one_hot(0),
        chunk_vectors=[_one_hot(0)],
    )
    await _seed_item(
        db_session,
        type_="text",
        title="Cooking notes",
        summary="A book about pasta.",
        content_text="Boil water, cook pasta, serve with sauce.",
        chunks=["pasta sauce technique"],
        summary_vector=_one_hot(1),
        chunk_vectors=[_one_hot(1)],
    )

    fake_llm.embed_response_vectors = [_one_hot(99)]

    r = await client.get("/v1/search?q=astronaut", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["id"] == str(target)
    assert body["items"][0]["score"] is not None


async def test_query_mode_chunk_ann_with_highlights(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _truncate(db_session)
    monkeypatch.setattr(deps, "_llm", fake_llm)

    target = await _seed_item(
        db_session,
        type_="text",
        title="A target item",
        summary="Target summary unrelated.",
        content_text="The body has nothing about ferrets.",
        chunks=["ferret habits and behavior in the wild"],
        summary_vector=_one_hot(0),
        chunk_vectors=[_one_hot(50)],
    )

    fake_llm.embed_response_vectors = [_one_hot(50)]

    r = await client.get("/v1/search?q=ferret", headers=auth_headers)
    body = r.json()
    assert body["items"][0]["id"] == str(target)
    chunks = body["items"][0]["matching_chunks"]
    assert len(chunks) == 1
    assert "<b>ferret</b>" in chunks[0]["highlights"].lower() or "ferret" in chunks[0]["text"]


async def test_filter_type(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)
    await _seed_item(
        db_session,
        type_="text",
        title="Text item",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(0),
        chunk_vectors=[_one_hot(0)],
    )
    url_id = await _seed_item(
        db_session,
        type_="url",
        title="URL item",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(1),
        chunk_vectors=[_one_hot(1)],
    )

    r = await client.get("/v1/search?type=url", headers=auth_headers)
    body = r.json()
    assert {item["id"] for item in body["items"]} == {str(url_id)}


async def test_filter_possible_duplicate(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)
    twin = await _seed_item(
        db_session,
        type_="image",
        title="Twin A",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(0),
        chunk_vectors=[_one_hot(0)],
    )
    await _seed_item(
        db_session,
        type_="image",
        title="Twin B (dup)",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(1),
        chunk_vectors=[_one_hot(1)],
        extra_meta={"possible_duplicate_of": str(twin)},
    )
    await _seed_item(
        db_session,
        type_="image",
        title="Solo",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(2),
        chunk_vectors=[_one_hot(2)],
    )

    r = await client.get("/v1/search?possible_duplicate=true", headers=auth_headers)
    body = r.json()
    titles = [item["title"] for item in body["items"]]
    assert titles == ["Twin B (dup)"]
    assert body["items"][0]["possible_duplicate_of"] == str(twin)

    # Items without the marker still expose the field as null on browse + search.
    r = await client.get("/v1/search", headers=auth_headers)
    body = r.json()
    by_title = {item["title"]: item for item in body["items"]}
    assert by_title["Twin B (dup)"]["possible_duplicate_of"] == str(twin)
    assert by_title["Solo"]["possible_duplicate_of"] is None
    assert by_title["Twin A"]["possible_duplicate_of"] is None


async def test_filter_tag(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    await _truncate(db_session)
    target = await _seed_item(
        db_session,
        type_="text",
        title="Tagged item",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(0),
        chunk_vectors=[_one_hot(0)],
        tag_names=["alpha"],
    )
    await _seed_item(
        db_session,
        type_="text",
        title="Untagged item",
        summary="s",
        content_text="content",
        chunks=["c"],
        summary_vector=_one_hot(1),
        chunk_vectors=[_one_hot(1)],
    )

    r = await client.get("/v1/search?tag=alpha", headers=auth_headers)
    body = r.json()
    assert {item["id"] for item in body["items"]} == {str(target)}


async def test_invalid_cursor_mismatch(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
) -> None:
    cursor = encode_cursor(QueryCursor(mode="q", score=0.5, id=uuid.uuid4()))
    r = await client.get(f"/v1/search?cursor={cursor}", headers=auth_headers)
    assert r.status_code == 400

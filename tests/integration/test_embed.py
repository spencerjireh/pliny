import logging
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import pliny.pipeline.embed  # noqa: F401  # pyright: ignore[reportUnusedImport]
from pliny.api import deps
from pliny.db.queries import insert_item
from pliny.logging import get_logger
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import get_handler


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


async def _seed_with_chunks(
    db_session: AsyncSession, *, summary: str | None, chunk_texts: list[str]
) -> uuid.UUID:
    item = await insert_item(
        db_session,
        type="text",
        content_hash=uuid.uuid4().hex,
        raw_ref=None,
    )
    if summary is not None:
        await db_session.execute(
            text("UPDATE items SET summary=:s WHERE id=:id"),
            {"s": summary, "id": item.id},
        )
    for i, t in enumerate(chunk_texts):
        await db_session.execute(
            text(
                "INSERT INTO chunks (id, item_id, chunk_index, text, token_count, chunker_version) "
                "VALUES (gen_random_uuid(), :id, :idx, :t, :tc, 1)"
            ),
            {"id": item.id, "idx": i, "t": t, "tc": len(t.split())},
        )
    await db_session.commit()
    return item.id


async def _run_handler(
    item_id: uuid.UUID, llm: object, logger: logging.LoggerAdapter[logging.Logger]
) -> None:
    sm = deps.get_session_maker()
    async with sm() as session:
        ctx = StageContext(
            item_id=item_id,
            stage="embed",
            attempt=1,
            claim_token=uuid.uuid4(),
            db=session,
            blob=deps.get_blob(),
            llm=llm,
            logger=logger,
        )
        await get_handler("embed")(ctx)
        await session.commit()


async def _count_embeddings(db_session: AsyncSession, item_id: uuid.UUID) -> dict[str, int]:
    rows = (
        (
            await db_session.execute(
                text(
                    "SELECT granularity, count(*)::int AS n "
                    "FROM embeddings_1536 WHERE item_id = :id GROUP BY granularity"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .all()
    )
    return {r["granularity"]: r["n"] for r in rows}


async def test_embed_multi_chunk_writes_summary_and_chunks(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_chunks(
        db_session,
        summary="The summary",
        chunk_texts=["chunk one", "chunk two", "chunk three"],
    )
    fake_llm.embed_response_vectors = [[float(i)] * 1536 for i in range(10)]

    await _run_handler(item_id, fake_llm, logger)

    counts = await _count_embeddings(db_session, item_id)
    assert counts == {"summary": 1, "chunk": 3}


async def test_embed_single_chunk_only_summary(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_chunks(
        db_session,
        summary="The summary",
        chunk_texts=["only one chunk"],
    )
    fake_llm.embed_response_vectors = [[1.0] * 1536]

    await _run_handler(item_id, fake_llm, logger)

    counts = await _count_embeddings(db_session, item_id)
    assert counts == {"summary": 1}


async def test_embed_no_summary_no_chunks_writes_nothing(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_chunks(db_session, summary=None, chunk_texts=[])

    await _run_handler(item_id, fake_llm, logger)

    counts = await _count_embeddings(db_session, item_id)
    assert counts == {}
    assert fake_llm.embed_calls == []


async def test_embed_idempotent_replaces_rows(
    db_session: AsyncSession,
    fake_llm,  # type: ignore[no-untyped-def]
    logger: logging.LoggerAdapter[logging.Logger],
) -> None:
    await _truncate(db_session)
    item_id = await _seed_with_chunks(
        db_session,
        summary="Summary",
        chunk_texts=["one", "two"],
    )
    fake_llm.embed_response_vectors = [[float(i)] * 1536 for i in range(10)]

    await _run_handler(item_id, fake_llm, logger)
    first = await _count_embeddings(db_session, item_id)
    await _run_handler(item_id, fake_llm, logger)
    second = await _count_embeddings(db_session, item_id)
    assert first == second == {"summary": 1, "chunk": 2}

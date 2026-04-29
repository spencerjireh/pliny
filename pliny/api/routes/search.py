from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api.deps import get_db, get_llm, require_api_key
from pliny.config import get_settings
from pliny.llm.base import LLM
from pliny.schemas.search import (
    MatchingChunk,
    QueryCursor,
    RankedHit,
    SearchResponse,
    SearchResultItem,
    decode_cursor,
    encode_cursor,
    rrf_fuse,
)

router = APIRouter()

ARM_TOPN = 200
CHUNKS_PER_ITEM = 3


def _build_filters(
    types: list[str] | None,
    from_: datetime | None,
    to: datetime | None,
    tags: list[str] | None,
    entities: list[UUID] | None,
    possible_duplicate: bool | None,
) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    params: dict[str, Any] = {}
    if types:
        parts.append("i.type = ANY(:p_types)")
        params["p_types"] = list(types)
    if from_ is not None:
        parts.append("i.captured_at >= :p_from")
        params["p_from"] = from_
    if to is not None:
        parts.append("i.captured_at <= :p_to")
        params["p_to"] = to
    if tags:
        parts.append(
            "EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.item_id = i.id AND t.name = ANY(:p_tags))"
        )
        params["p_tags"] = list(tags)
    if entities:
        parts.append(
            "EXISTS (SELECT 1 FROM item_entities ie WHERE ie.item_id = i.id "
            "AND ie.entity_id = ANY(:p_entities))"
        )
        params["p_entities"] = [str(e) for e in entities]
    if possible_duplicate:
        parts.append("i.metadata ? 'possible_duplicate_of'")
    if not parts:
        return "TRUE", params
    return " AND ".join(parts), params


async def _bm25_arm(
    db: AsyncSession, *, q: str, filter_sql: str, params: dict[str, Any]
) -> list[RankedHit]:
    sql = (
        "SELECT c.item_id AS item_id, "
        "ts_rank_cd(c.tsv, websearch_to_tsquery('english', :q)) AS score "
        "FROM content c JOIN items i ON i.id = c.item_id "
        f"WHERE c.tsv @@ websearch_to_tsquery('english', :q) AND ({filter_sql}) "
        "ORDER BY score DESC LIMIT :topn"
    )
    rows = (await db.execute(sql_text(sql), {**params, "q": q, "topn": ARM_TOPN})).mappings().all()
    return [
        RankedHit(item_id=r["item_id"], rank=i + 1, score=float(r["score"]))
        for i, r in enumerate(rows)
    ]


async def _ann_arm(
    db: AsyncSession,
    *,
    granularity: str,
    vector: list[float],
    model: str,
    filter_sql: str,
    params: dict[str, Any],
    include_chunk_id: bool,
) -> list[RankedHit]:
    select_chunk = ", e.chunk_id AS chunk_id" if include_chunk_id else ""
    sql = (
        f"SELECT e.item_id AS item_id, 1 - (e.vector <=> CAST(:vec AS vector)) AS score{select_chunk} "
        "FROM embeddings_1536 e JOIN items i ON i.id = e.item_id "
        f"WHERE e.granularity = :gran AND e.model_name = :model AND ({filter_sql}) "
        "ORDER BY e.vector <=> CAST(:vec AS vector) LIMIT :topn"
    )
    qparams: dict[str, Any] = {
        **params,
        "vec": str(vector),
        "gran": granularity,
        "model": model,
        "topn": ARM_TOPN,
    }
    rows = (await db.execute(sql_text(sql), qparams)).mappings().all()
    return [
        RankedHit(
            item_id=r["item_id"],
            rank=i + 1,
            score=float(r["score"]),
            chunk_id=r.get("chunk_id") if include_chunk_id else None,
        )
        for i, r in enumerate(rows)
    ]


async def _materialize_items(
    db: AsyncSession, ordered_ids: list[UUID]
) -> dict[UUID, dict[str, Any]]:
    if not ordered_ids:
        return {}
    rows = (
        (
            await db.execute(
                sql_text(
                    "SELECT id, title, summary, type, captured_at, "
                    "metadata->>'possible_duplicate_of' AS possible_duplicate_of "
                    "FROM items WHERE id = ANY(:ids)"
                ),
                {"ids": [str(i) for i in ordered_ids]},
            )
        )
        .mappings()
        .all()
    )
    return {r["id"]: dict(r) for r in rows}


async def _matching_chunks(
    db: AsyncSession,
    *,
    q: str,
    item_id: UUID,
    chunk_hits: list[RankedHit],
) -> list[MatchingChunk]:
    if not chunk_hits:
        return []
    chunk_ids = [str(h.chunk_id) for h in chunk_hits if h.chunk_id is not None]
    if not chunk_ids:
        return []
    rows = (
        (
            await db.execute(
                sql_text(
                    "SELECT id, text, "
                    "ts_headline('english', text, websearch_to_tsquery('english', :q)) AS hl "
                    "FROM chunks WHERE id = ANY(:ids)"
                ),
                {"q": q, "ids": chunk_ids},
            )
        )
        .mappings()
        .all()
    )
    by_id = {r["id"]: r for r in rows}
    out: list[MatchingChunk] = []
    for h in chunk_hits:
        r = by_id.get(h.chunk_id)
        if r is None:
            continue
        out.append(
            MatchingChunk(
                chunk_id=h.chunk_id,  # type: ignore[arg-type]
                text=r["text"],
                score=h.score,
                highlights=r["hl"],
            )
        )
    return out


def _validate_cursor(cursor_str: str | None, q: str | None) -> QueryCursor | None:
    if cursor_str is None:
        return None
    try:
        c = decode_cursor(cursor_str)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid cursor"
        ) from exc
    expected_mode = "q" if q else "b"
    if c.mode != expected_mode:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cursor mode mismatch")
    return c


@router.get("", response_model=SearchResponse)
async def search(
    _: Annotated[None, Depends(require_api_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = None,
    type: list[str] | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    tag: list[str] | None = Query(default=None),
    entity: list[UUID] | None = Query(default=None),
    possible_duplicate: bool | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
) -> SearchResponse:
    cursor_obj = _validate_cursor(cursor, q)
    filter_sql, params = _build_filters(type, from_, to, tag, entity, possible_duplicate)

    if q is None:
        return await _browse(db, filter_sql, params, limit=limit, cursor_obj=cursor_obj)
    return await _hybrid_search(db, q, filter_sql, params, limit=limit, cursor_obj=cursor_obj)


async def _browse(
    db: AsyncSession,
    filter_sql: str,
    params: dict[str, Any],
    *,
    limit: int,
    cursor_obj: QueryCursor | None,
) -> SearchResponse:
    where = [filter_sql]
    qparams = {**params, "lim": limit + 1}
    if cursor_obj is not None and cursor_obj.captured_at is not None:
        where.append("(i.captured_at, i.id) < (:c_at, :c_id)")
        qparams["c_at"] = cursor_obj.captured_at
        qparams["c_id"] = str(cursor_obj.id)
    sql = (
        "SELECT i.id, i.title, i.summary, i.type, i.captured_at, "
        "i.metadata->>'possible_duplicate_of' AS possible_duplicate_of "
        f"FROM items i WHERE ({') AND ('.join(where)}) "
        "ORDER BY i.captured_at DESC, i.id DESC LIMIT :lim"
    )
    rows = (await db.execute(sql_text(sql), qparams)).mappings().all()
    page = rows[:limit]
    items = [
        SearchResultItem(
            id=r["id"],
            title=r["title"],
            summary=r["summary"],
            type=r["type"],
            captured_at=r["captured_at"],
            possible_duplicate_of=r["possible_duplicate_of"],
        )
        for r in page
    ]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(
            QueryCursor(mode="b", captured_at=last["captured_at"], id=last["id"])
        )
    return SearchResponse(items=items, next_cursor=next_cursor)


async def _hybrid_search(
    db: AsyncSession,
    q: str,
    filter_sql: str,
    params: dict[str, Any],
    *,
    limit: int,
    cursor_obj: QueryCursor | None,
) -> SearchResponse:
    settings = get_settings()
    llm: LLM = get_llm()  # type: ignore[assignment]
    vector = (await llm.embed([q], model=settings.current_embedding_model))[0]
    model = settings.current_embedding_model

    bm25 = await _bm25_arm(db, q=q, filter_sql=filter_sql, params=params)
    summary_arm = await _ann_arm(
        db,
        granularity="summary",
        vector=vector,
        model=model,
        filter_sql=filter_sql,
        params=params,
        include_chunk_id=False,
    )
    chunk_arm = await _ann_arm(
        db,
        granularity="chunk",
        vector=vector,
        model=model,
        filter_sql=filter_sql,
        params=params,
        include_chunk_id=True,
    )

    fused = rrf_fuse([bm25, summary_arm, chunk_arm])
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], -int(kv[0].int)))

    if cursor_obj is not None and cursor_obj.score is not None:
        threshold = (cursor_obj.score, cursor_obj.id.int)
        ordered = [(i, s) for i, s in ordered if (s, i.int) < threshold]

    page = ordered[:limit]
    has_more = len(ordered) > limit
    page_ids = [i for i, _ in page]
    materialized = await _materialize_items(db, page_ids)

    chunks_by_item: dict[UUID, list[RankedHit]] = {}
    for hit in chunk_arm:
        chunks_by_item.setdefault(hit.item_id, []).append(hit)
    for k in chunks_by_item:
        chunks_by_item[k] = chunks_by_item[k][:CHUNKS_PER_ITEM]

    items: list[SearchResultItem] = []
    for item_id, score in page:
        m = materialized.get(item_id)
        if m is None:
            continue
        chunks = await _matching_chunks(
            db, q=q, item_id=item_id, chunk_hits=chunks_by_item.get(item_id, [])
        )
        items.append(
            SearchResultItem(
                id=m["id"],
                title=m["title"],
                summary=m["summary"],
                type=m["type"],
                captured_at=m["captured_at"],
                score=score,
                matching_chunks=chunks,
                possible_duplicate_of=m["possible_duplicate_of"],
            )
        )

    next_cursor = None
    if has_more and page:
        last_id, last_score = page[-1]
        next_cursor = encode_cursor(QueryCursor(mode="q", score=last_score, id=last_id))
    return SearchResponse(items=items, next_cursor=next_cursor)

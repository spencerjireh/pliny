import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile

from pliny.api.deps import get_blob, get_db, require_api_key
from pliny.canonicalize import canonicalize
from pliny.db.models import (
    Chunk,
    Content,
    Entity,
    Item,
    ItemEntity,
    ItemRedirect,
    ItemSource,
    ItemTag,
    Tag,
)
from pliny.db.queries import (
    append_item_source,
    enqueue_job,
    find_item_by_content_hash,
    insert_item,
    notify,
)
from pliny.schemas.api import IngestItemResult, IngestJSON, IngestResponse
from pliny.storage.blob import BlobStore

router = APIRouter()

MAX_BODY_BYTES = 25 * 1024 * 1024
URL_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass
class _IngestTask:
    type: str
    content_hash: str
    canonical_url: str | None = None
    raw_bytes: bytes | None = None
    metadata: dict[str, Any] | None = None


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _classify_mime(mime: str | None) -> str:
    if mime is None:
        return "file"
    mime = mime.split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "file"


def _split_text_and_urls(text: str) -> tuple[str | None, list[str]]:
    """Return (residual_text_or_None, list_of_url_strings)."""
    urls = URL_REGEX.findall(text)
    if not urls:
        return text or None, []
    residual = URL_REGEX.sub(" ", text).strip()
    return (residual or None), urls


def _make_text_task(text: str, metadata: dict[str, Any] | None) -> _IngestTask:
    raw = text.encode("utf-8")
    return _IngestTask(
        type="text",
        content_hash=_hash_bytes(raw),
        raw_bytes=raw,
        metadata=metadata,
    )


def _make_url_task(url: str, metadata: dict[str, Any] | None) -> _IngestTask:
    canonical = canonicalize(url)
    return _IngestTask(
        type="url",
        content_hash=_hash_url(canonical),
        canonical_url=canonical,
        metadata=metadata,
    )


def _make_byte_task(raw: bytes, mime: str | None, metadata: dict[str, Any] | None) -> _IngestTask:
    item_type = _classify_mime(mime)
    md = dict(metadata or {})
    if mime is not None:
        md.setdefault("mime", mime)
    return _IngestTask(
        type=item_type,
        content_hash=_hash_bytes(raw),
        raw_bytes=raw,
        metadata=md or None,
    )


def _build_tasks_from_json(payload: IngestJSON) -> list[_IngestTask]:
    tasks: list[_IngestTask] = []
    if payload.url:
        tasks.append(_make_url_task(payload.url, payload.metadata))
    if payload.text:
        residual, urls = _split_text_and_urls(payload.text)
        for url in urls:
            tasks.append(_make_url_task(url, payload.metadata))
        if residual:
            tasks.append(_make_text_task(residual, payload.metadata))
    return tasks


async def _process_task(
    task: _IngestTask,
    *,
    source: str,
    source_ref: str | None,
    db: AsyncSession,
    blob: BlobStore,
) -> IngestItemResult:
    existing = await find_item_by_content_hash(db, task.content_hash)
    if existing is not None:
        await append_item_source(db, item_id=existing.id, source=source, source_ref=source_ref)
        return IngestItemResult(item_id=existing.id, type=existing.type, deduplicated=True)

    raw_ref: str | None = None
    if task.raw_bytes is not None:
        raw_ref = f"raw/{task.content_hash}"
        await blob.put(raw_ref, task.raw_bytes)

    item = await insert_item(
        db,
        type=task.type,
        content_hash=task.content_hash,
        canonical_url=task.canonical_url,
        raw_ref=raw_ref,
        metadata=task.metadata,
    )
    await append_item_source(db, item_id=item.id, source=source, source_ref=source_ref)

    if task.type == "url":
        initial_stage, pool = "snapshot", "slow"
    else:
        initial_stage, pool = "extract", "fast"
    enqueued = await enqueue_job(db, item_id=item.id, stage=initial_stage, pool=pool)
    if enqueued:
        await notify(db, f"job_pool_{pool}", str(item.id))

    return IngestItemResult(item_id=item.id, type=item.type, deduplicated=False)


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=IngestResponse)
async def create_items(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    blob: Annotated[BlobStore, Depends(get_blob)],
    _: Annotated[None, Depends(require_api_key)],
) -> IngestResponse:
    cl = request.headers.get("content-length")
    if cl is not None and int(cl) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="body too large"
        )

    content_type = request.headers.get("content-type", "").lower()

    tasks: list[_IngestTask] = []
    source: str
    source_ref: str | None

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        src = form.get("source")
        if not isinstance(src, str) or not src:
            raise HTTPException(status_code=400, detail="multipart 'source' is required")
        source = src
        sref = form.get("source_ref")
        source_ref = sref if isinstance(sref, str) and sref else None
        metadata: dict[str, Any] | None = None
        meta_raw = form.get("metadata")
        if isinstance(meta_raw, str) and meta_raw:
            try:
                parsed = json.loads(meta_raw)
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"invalid metadata JSON: {e}") from e
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="metadata must be an object")
            metadata = parsed

        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile) and not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="multipart requires 'file' part")
        body = await upload.read()
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="body too large",
            )
        tasks.append(_make_byte_task(body, upload.content_type, metadata))

    elif content_type.startswith("application/json"):
        body_bytes = await request.body()
        if len(body_bytes) > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="body too large",
            )
        try:
            payload = IngestJSON.model_validate_json(body_bytes)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        source = payload.source
        source_ref = payload.source_ref
        tasks = _build_tasks_from_json(payload)
        if not tasks:
            raise HTTPException(status_code=400, detail="no ingest tasks produced")

    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported content-type: {content_type!r}",
        )

    results: list[IngestItemResult] = []
    for task in tasks:
        result = await _process_task(task, source=source, source_ref=source_ref, db=db, blob=blob)
        results.append(result)
    await db.commit()

    return IngestResponse(items=results)


@router.get("/{item_id}")
async def get_item(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
) -> dict[str, Any]:
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if item is None:
        redirect = (
            await db.execute(select(ItemRedirect).where(ItemRedirect.from_id == item_id))
        ).scalar_one_or_none()
        if redirect is not None:
            return {"redirect_to": str(redirect.to_id)}
        raise HTTPException(status_code=404, detail="not found")

    content = (
        await db.execute(select(Content).where(Content.item_id == item_id))
    ).scalar_one_or_none()
    chunks = (
        (
            await db.execute(
                select(Chunk).where(Chunk.item_id == item_id).order_by(Chunk.chunk_index)
            )
        )
        .scalars()
        .all()
    )
    sources = (
        (await db.execute(select(ItemSource).where(ItemSource.item_id == item_id))).scalars().all()
    )
    entity_rows = (
        await db.execute(
            select(Entity, ItemEntity)
            .join(ItemEntity, ItemEntity.entity_id == Entity.id)
            .where(ItemEntity.item_id == item_id)
        )
    ).all()
    tag_rows = (
        (
            await db.execute(
                select(Tag)
                .join(ItemTag, ItemTag.tag_id == Tag.id)
                .where(ItemTag.item_id == item_id)
            )
        )
        .scalars()
        .all()
    )

    return {
        "id": str(item.id),
        "type": item.type,
        "title": item.title,
        "summary": item.summary,
        "captured_at": item.captured_at.isoformat(),
        "canonical_url": item.canonical_url,
        "content_hash": item.content_hash,
        "raw_ref": item.raw_ref,
        "metadata": item.meta or {},
        "content": ({"extracted_text": content.extracted_text} if content is not None else None),
        "chunks": [{"index": c.chunk_index, "text": c.text} for c in chunks],
        "sources": [
            {
                "source": s.source,
                "source_ref": s.source_ref,
                "captured_at": s.captured_at.isoformat(),
            }
            for s in sources
        ],
        "entities": [
            {
                "name": e.canonical_name,
                "type": e.type,
                "mention_text": ie.mention_text,
                "confidence": ie.confidence,
            }
            for e, ie in entity_rows
        ],
        "tags": [t.name for t in tag_rows],
    }


_NON_URL_STAGES: tuple[str, ...] = (
    "extract",
    "summarize",
    "chunk",
    "embed",
    "entities",
    "graph_sync",
)
_URL_STAGES: tuple[str, ...] = ("snapshot", *_NON_URL_STAGES)
_TYPES_WITH_SNAPSHOT = frozenset({"url", "audio", "video"})


def _applicable_stages(item_type: str) -> tuple[str, ...]:
    """URL items go through `snapshot`. So do `audio`/`video` items, which
    were typed `url` at ingest and mutated by snapshot's classifier — their
    snapshot stage has already run and its status should still be visible."""
    return _URL_STAGES if item_type in _TYPES_WITH_SNAPSHOT else _NON_URL_STAGES


def _derive_overall(stages: dict[str, dict[str, Any]]) -> str:
    statuses = [s["status"] for s in stages.values()]
    if any(s == "failed" for s in statuses):
        return "failed"
    if any(s in ("pending", "running") for s in statuses):
        return "processing"
    if all(s == "done" for s in statuses):
        return "ready"
    return "pending"


@router.get("/{item_id}/status")
async def item_status(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _: Annotated[None, Depends(require_api_key)],
) -> dict[str, Any]:
    item = (await db.execute(select(Item).where(Item.id == item_id))).scalar_one_or_none()
    if item is None:
        redirect = (
            await db.execute(select(ItemRedirect).where(ItemRedirect.from_id == item_id))
        ).scalar_one_or_none()
        if redirect is not None:
            return {"redirect_to": str(redirect.to_id)}
        raise HTTPException(status_code=404, detail="not found")

    applicable = _applicable_stages(item.type)

    rows = (
        (
            await db.execute(
                text(
                    "SELECT stage, status, attempts, error FROM processing_jobs WHERE item_id = :id"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .all()
    )
    by_stage: dict[str, dict[str, Any]] = {r["stage"]: dict(r) for r in rows}

    versions: dict[str, int] = {
        "snapshot": item.snapshot_version,
        "extract": item.extract_version,
        "summarize": item.summarize_version,
        "chunk": item.chunk_version,
        "embed": item.embed_version,
        "entities": item.entities_version,
        "graph_sync": item.graph_sync_version,
        "wayback_fallback": item.wayback_fallback_version,
    }

    stages: dict[str, dict[str, Any]] = {}
    for stage in applicable:
        version = versions.get(stage, 0)
        job = by_stage.get(stage)
        if job is not None:
            stages[stage] = {
                "status": job["status"],
                "version": version,
                "attempts": job["attempts"],
                "error": job["error"],
            }
        elif version > 0:
            stages[stage] = {"status": "done", "version": version, "attempts": 0, "error": None}
        else:
            stages[stage] = {"status": "pending", "version": 0, "attempts": 0, "error": None}

    # wayback_fallback is recovery-only; surface it only when an attempt has been made.
    wayback_job = by_stage.get("wayback_fallback")
    if wayback_job is not None:
        stages["wayback_fallback"] = {
            "status": wayback_job["status"],
            "version": versions["wayback_fallback"],
            "attempts": wayback_job["attempts"],
            "error": wayback_job["error"],
        }

    return {
        "id": str(item.id),
        "stages": stages,
        "overall": _derive_overall(stages),
    }

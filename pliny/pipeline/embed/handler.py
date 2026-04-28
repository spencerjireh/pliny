import uuid

from sqlalchemy import insert
from sqlalchemy import text as sql_text

from pliny.config import get_settings
from pliny.db.models import Embedding1536
from pliny.llm.base import LLM
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register

EMBED_VERSION = 1
BATCH = 128


@register("embed")
async def embed_handler(ctx: StageContext) -> None:
    settings = get_settings()
    model = settings.current_embedding_model
    model_version = settings.embedding_model_version

    summary = (
        await ctx.db.execute(
            sql_text("SELECT summary FROM items WHERE id = :id"),
            {"id": ctx.item_id},
        )
    ).scalar_one_or_none()

    chunk_rows = (
        (
            await ctx.db.execute(
                sql_text("SELECT id, text FROM chunks WHERE item_id = :id ORDER BY chunk_index"),
                {"id": ctx.item_id},
            )
        )
        .mappings()
        .all()
    )

    await ctx.db.execute(
        sql_text(
            "DELETE FROM embeddings_1536 "
            "WHERE item_id = :id AND model_name = :m AND model_version = :v"
        ),
        {"id": ctx.item_id, "m": model, "v": model_version},
    )

    write_chunk_embeddings = len(chunk_rows) > 1

    inputs: list[str] = []
    plan: list[tuple[str, uuid.UUID | None]] = []
    if summary:
        inputs.append(summary)
        plan.append(("summary", None))
    if write_chunk_embeddings:
        for c in chunk_rows:
            inputs.append(c["text"])
            plan.append(("chunk", c["id"]))

    if not inputs:
        return

    if ctx.llm is None:
        raise RuntimeError("LLM client required for embed stage")
    llm: LLM = ctx.llm  # type: ignore[assignment]

    vectors: list[list[float]] = []
    for i in range(0, len(inputs), BATCH):
        vectors.extend(await llm.embed(inputs[i : i + BATCH], model=model))

    rows = [
        {
            "id": uuid.uuid4(),
            "item_id": ctx.item_id,
            "chunk_id": chunk_id,
            "granularity": granularity,
            "model_name": model,
            "model_version": model_version,
            "vector": vec,
        }
        for vec, (granularity, chunk_id) in zip(vectors, plan, strict=True)
    ]
    await ctx.db.execute(insert(Embedding1536), rows)

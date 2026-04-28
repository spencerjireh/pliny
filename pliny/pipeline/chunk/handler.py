from sqlalchemy import text as sql_text

from pliny.db.queries import replace_chunks
from pliny.pipeline.chunk import chunker
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register


@register("chunk")
async def chunk_handler(ctx: StageContext) -> None:
    extracted = (
        await ctx.db.execute(
            sql_text("SELECT extracted_text FROM content WHERE item_id = :id"),
            {"id": ctx.item_id},
        )
    ).scalar_one_or_none()

    pieces, original_count = chunker.chunk_text(extracted or "")
    await replace_chunks(
        ctx.db, item_id=ctx.item_id, pieces=pieces, version=chunker.CHUNKER_VERSION
    )

    if original_count > chunker.MAX_CHUNKS:
        await ctx.db.execute(
            sql_text(
                "UPDATE items SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| jsonb_build_object('chunk_overflow', true, "
                "'original_chunk_count', cast(:n as int)) "
                "WHERE id = :id"
            ),
            {"id": ctx.item_id, "n": original_count},
        )

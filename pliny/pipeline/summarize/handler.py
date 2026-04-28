import json

from sqlalchemy import text as sql_text

from pliny.db.queries import link_item_tag, upsert_tag
from pliny.llm.base import LLM
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register
from pliny.prompts.summarize import MAX_INPUT_CHARS, MODEL, PROMPT

MAX_TAGS = 8


@register("summarize")
async def summarize_handler(ctx: StageContext) -> None:
    extracted_raw = (
        await ctx.db.execute(
            sql_text("SELECT extracted_text FROM content WHERE item_id = :id"),
            {"id": ctx.item_id},
        )
    ).scalar_one_or_none()
    extracted = (extracted_raw or "")[:MAX_INPUT_CHARS].strip()

    if not extracted:
        await ctx.db.execute(
            sql_text("UPDATE items SET title=NULL, summary=NULL WHERE id=:id"),
            {"id": ctx.item_id},
        )
        await ctx.db.execute(
            sql_text("DELETE FROM item_tags WHERE item_id=:id"),
            {"id": ctx.item_id},
        )
        return

    if ctx.llm is None:
        raise RuntimeError("LLM client required for summarize stage")
    llm: LLM = ctx.llm  # type: ignore[assignment]
    response = await llm.chat(
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": extracted},
        ],
        model=MODEL,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.text)
    title = (parsed.get("title") or "").strip() or None
    summary = (parsed.get("summary") or "").strip() or None
    tags_raw = parsed.get("tags") or []
    tags = [t.strip().lower() for t in tags_raw if isinstance(t, str) and t.strip()][:MAX_TAGS]

    await ctx.db.execute(
        sql_text("UPDATE items SET title=:t, summary=:s WHERE id=:id"),
        {"t": title, "s": summary, "id": ctx.item_id},
    )
    await ctx.db.execute(
        sql_text("DELETE FROM item_tags WHERE item_id=:id"),
        {"id": ctx.item_id},
    )
    for name in tags:
        tag_id = await upsert_tag(ctx.db, name=name)
        await link_item_tag(ctx.db, item_id=ctx.item_id, tag_id=tag_id)

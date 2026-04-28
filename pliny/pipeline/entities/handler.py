import json
from typing import Any

from sqlalchemy import text as sql_text

from pliny.db.queries import EntityMention, replace_item_entities
from pliny.llm.base import LLM
from pliny.pipeline.context import StageContext
from pliny.pipeline.stages import register
from pliny.prompts.entities import MAX_INPUT_CHARS, MODEL, PROMPT, VERSION

VALID_TYPES = {"person", "place", "org", "concept", "work", "other"}
MAX_ENTITIES = 30


@register("entities")
async def entities_handler(ctx: StageContext) -> None:
    row = (
        (
            await ctx.db.execute(
                sql_text(
                    "SELECT c.extracted_text, i.summary "
                    "FROM content c JOIN items i ON i.id = c.item_id "
                    "WHERE c.item_id = :id"
                ),
                {"id": ctx.item_id},
            )
        )
        .mappings()
        .one_or_none()
    )

    extracted_raw = (row or {}).get("extracted_text") or ""
    summary_raw = (row or {}).get("summary") or ""
    extracted = extracted_raw[:MAX_INPUT_CHARS].strip()
    summary = summary_raw.strip()

    if not extracted:
        await replace_item_entities(ctx.db, item_id=ctx.item_id, mentions=[], version=VERSION)
        return

    if ctx.llm is None:
        raise RuntimeError("LLM client required for entities stage")
    llm: LLM = ctx.llm  # type: ignore[assignment]

    user_payload = extracted if not summary else f"SUMMARY:\n{summary}\n\nARTICLE:\n{extracted}"
    response = await llm.chat(
        messages=[
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": user_payload},
        ],
        model=MODEL,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.text)
    raw_entities = parsed.get("entities") or []

    mentions: list[EntityMention] = []
    for e in raw_entities[:MAX_ENTITIES]:
        if not isinstance(e, dict):
            continue
        e_dict: dict[str, Any] = e
        name = (e_dict.get("name") or "").strip()
        etype = (e_dict.get("type") or "").strip().lower()
        if not name or etype not in VALID_TYPES:
            continue
        confidence_raw = e_dict.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None
        aliases_raw = e_dict.get("aliases")
        aliases = (
            [str(a) for a in aliases_raw if isinstance(a, str)]
            if isinstance(aliases_raw, list)
            else None
        )
        mention_text = (e_dict.get("mention_text") or "").strip() or None
        mentions.append(
            EntityMention(
                name=name,
                type=etype,
                mention_text=mention_text,
                confidence=confidence,
                aliases=aliases,
            )
        )

    await replace_item_entities(ctx.db, item_id=ctx.item_id, mentions=mentions, version=VERSION)

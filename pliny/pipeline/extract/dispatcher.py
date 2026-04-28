from sqlalchemy import select

from pliny.db.models import Item
from pliny.pipeline.context import StageContext
from pliny.pipeline.extract import image, text, url_html
from pliny.pipeline.stages import NoHandlerError, register


@register("extract")
async def extract_handler(ctx: StageContext) -> None:
    item = (await ctx.db.execute(select(Item).where(Item.id == ctx.item_id))).scalar_one()
    if item.type == "text":
        await text.run(ctx, item)
    elif item.type == "url":
        await url_html.run(ctx, item)
    elif item.type == "image":
        await image.run(ctx, item)
    else:
        raise NoHandlerError(f"no extract handler for type={item.type!r}")

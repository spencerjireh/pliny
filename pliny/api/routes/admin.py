from typing import Annotated, cast

from fastapi import APIRouter, Depends
from neo4j import AsyncDriver
from sqlalchemy.ext.asyncio import AsyncSession

from pliny.api.deps import get_db, get_neo4j_driver, require_api_key
from pliny.graph.rebuild import rebuild_from_postgres

router = APIRouter()


@router.post("/rebuild_graph")
async def rebuild_graph(
    _: Annotated[None, Depends(require_api_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    driver = cast(AsyncDriver, get_neo4j_driver())
    return await rebuild_from_postgres(driver, db)

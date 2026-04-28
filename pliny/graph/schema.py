from neo4j import AsyncDriver

_ENSURED: set[int] = set()


async def ensure_constraints(driver: AsyncDriver) -> None:
    """Idempotent: creates uniqueness constraints on Item.id and Entity.id."""
    if id(driver) in _ENSURED:
        return
    async with driver.session() as session:
        await session.run(
            "CREATE CONSTRAINT item_id_unique IF NOT EXISTS FOR (i:Item) REQUIRE i.id IS UNIQUE"
        )
        await session.run(
            "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE"
        )
    _ENSURED.add(id(driver))


def reset_ensured_for_tests() -> None:
    _ENSURED.clear()

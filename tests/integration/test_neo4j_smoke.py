from typing import Any

from pliny.graph.schema import ensure_constraints


async def test_driver_connects_and_constraints_create(neo4j_driver: Any) -> None:
    await ensure_constraints(neo4j_driver)
    async with neo4j_driver.session() as s:
        result = await s.run("SHOW CONSTRAINTS YIELD name RETURN name")
        names = {r["name"] async for r in result}
    assert "item_id_unique" in names
    assert "entity_id_unique" in names


async def test_reset_neo4j_wipes_between_tests(neo4j_driver: Any) -> None:
    async with neo4j_driver.session() as s:
        result = await s.run("MATCH (n) RETURN count(n) AS c")
        record = await result.single()
        assert record is not None
        assert record["c"] == 0

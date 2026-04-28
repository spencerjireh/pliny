from neo4j import AsyncDriver, AsyncGraphDatabase

from pliny.config import get_settings

_driver: AsyncDriver | None = None


def get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        s = get_settings()
        _driver = AsyncGraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


def force_reset_driver_for_tests() -> None:
    """Tests create fresh drivers per test to avoid event-loop binding errors."""
    global _driver
    _driver = None

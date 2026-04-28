import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.postgres import PostgresContainer

from pliny.api import deps
from pliny.config import get_settings


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("pgvector/pgvector:pg15", driver="psycopg")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def test_database_url(postgres_container: PostgresContainer) -> str:
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _env_override(
    test_database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    blob_root = tmp_path_factory.mktemp("blob")
    prev = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "API_KEY": os.environ.get("API_KEY"),
        "BLOB_ROOT": os.environ.get("BLOB_ROOT"),
        "OPENAI_DAILY_USD_CAP": os.environ.get("OPENAI_DAILY_USD_CAP"),
    }
    os.environ["DATABASE_URL"] = test_database_url
    os.environ["API_KEY"] = "test-key"
    os.environ["BLOB_ROOT"] = str(blob_root)
    os.environ["OPENAI_DAILY_USD_CAP"] = "100"
    get_settings.cache_clear()
    deps.reset_state()
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    deps.reset_state()


@pytest.fixture(scope="session", autouse=True)
def _run_migrations(test_database_url: str, _env_override: None) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_database_url)
    command.upgrade(cfg, "head")


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    from pliny.api.app import create_app

    application = create_app()
    yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    sm = deps.get_session_maker()
    async with sm() as session:
        yield session
